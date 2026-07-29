"""
Nightly parameter re-tune for XAUUSD.

Runs on a Railway cron schedule (see ops/nightly_tune/railway.json) — re-runs
the parameter sweep (core/trading/optimize.py) against the latest gold data
and writes the winning combo to Redis. MarketMonitor._tuned_overrides() reads
it on the next scan cycle, so it goes live without a redeploy.

Like optimize.py, this only tunes which setups the signal engine treats as
good enough to act on (confidence threshold, confluence, SL/TP sizing).
It never touches risk management — position sizing, the daily-loss lock,
and stop-loss enforcement stay exactly as configured regardless of what
a given night's sweep finds.

Run standalone: python -m core.trading.nightly_tune
"""
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone

import redis.asyncio as aioredis

from .mt5_bridge import MT5Bridge
from .optimize import run_sweep

log = logging.getLogger(__name__)

TUNED_KEY_PREFIX     = "trading:tuned_params:"
CANDIDATE_KEY_PREFIX = "trading:tune_candidate:"
SYMBOLS = ["GC=F"]   # gold-only account — see DEFAULT_FOREX in market_monitor.py

# A single night's sweep is one sample of noisy, short-window data — pushing
# whatever it finds straight to live trading is how the 0.75x/5.0x SL/TP
# mismatch happened (a value from one sweep silently paired with an unrelated
# default). Require the SAME combo to win REQUIRED_STREAK nights in a row
# before it actually goes live; a one-off outlier night just resets the
# streak instead of taking effect immediately.
REQUIRED_STREAK = 3
TRACKED_FIELDS  = ("MIN_CONFLUENCE", "ATR_SL_MULT", "ATR_TP_MULT", "MIN_RR", "confidence_thresh")


async def _notify(redis, message: str):
    await redis.publish("supervisor:notifications", json.dumps({
        "message": message, "parse_mode": "Markdown", "task_id": "nightly_tune",
    }))


async def run():
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        print("FEJL: REDIS_URL ikke sat.", file=sys.stderr)
        sys.exit(1)

    redis = aioredis.from_url(redis_url, decode_responses=True)
    bridge = MT5Bridge(redis)
    listen_task = asyncio.create_task(bridge.run())
    try:
        await asyncio.sleep(1)   # let the results listener subscribe before we fire commands

        if not await bridge.ping():
            await _notify(redis,
                "🌙 *Nightly tune sprunget over* — MT5 Worker er offline, "
                "kan ikke hente frisk data. Prøver igen i morgen."
            )
            return

        log.info("Starting nightly XAUUSD sweep...")
        result = await run_sweep(bridge, symbols=SYMBOLS, progress=True)

        if not result["top_10"]:
            await _notify(redis,
                "🌙 *Nightly tune*: ingen parameterkombination havde nok samples "
                "(min. 15 trades) i dagens data — beholder nuværende parametre."
            )
            return

        best = result["top_10"][0]
        symbol = SYMBOLS[0]
        candidate_key = f"{CANDIDATE_KEY_PREFIX}{symbol}"
        stored = await redis.hgetall(candidate_key)

        same_as_stored = bool(stored) and all(
            abs(float(stored.get(f, "nan") or "nan") - float(best[f])) < 1e-9
            for f in TRACKED_FIELDS
        )
        streak = int(stored.get("streak", 0)) + 1 if same_as_stored else 1

        candidate_values = {f: best[f] for f in TRACKED_FIELDS}
        await redis.hset(candidate_key, mapping={**candidate_values, "streak": streak})

        if streak < REQUIRED_STREAK:
            await _notify(redis,
                f"🌙 *Nightly tune — XAUUSD*: ny kandidat set ({streak}/{REQUIRED_STREAK} nætter) — "
                f"SL {best['ATR_SL_MULT']}x / TP {best['ATR_TP_MULT']}x ATR, "
                f"{best['win_rate']:.0%} win rate, {best['avg_r']:+.2f}R/trade i backtest.\n"
                f"Skal bekræftes {REQUIRED_STREAK - streak} nat(-ter) mere før den går live — "
                f"nuværende parametre er uændrede."
            )
            log.info("Nightly tune: candidate streak %d/%d, not yet live: %s",
                      streak, REQUIRED_STREAK, candidate_values)
            return

        key = f"{TUNED_KEY_PREFIX}{symbol}"
        old = await redis.hgetall(key)

        new_values = {
            "min_confluence":    best["MIN_CONFLUENCE"],
            "atr_sl_mult":       best["ATR_SL_MULT"],
            "atr_tp_mult":       best["ATR_TP_MULT"],
            "min_rr":            best["MIN_RR"],
            "confidence_thresh": best["confidence_thresh"],
            "win_rate":          best["win_rate"],
            "avg_r":             best["avg_r"],
            "total_trades":      best["total_trades"],
            "updated_at":        datetime.now(timezone.utc).isoformat(),
        }
        await redis.hset(key, mapping=new_values)

        def diff(field, new_val, suffix=""):
            old_val = old.get(field)
            if old_val is None:
                return f"{new_val}{suffix} (første gang)"
            return f"{old_val}{suffix} → {new_val}{suffix}"

        msg = (
            f"🌙 *Nightly tune — XAUUSD*: bekræftet {REQUIRED_STREAK} nætter i træk, nu LIVE\n"
            f"{best['total_trades']} trades i backtest, {best['win_rate']:.0%} win rate, "
            f"{best['avg_r']:+.2f}R/trade forventning\n\n"
            f"SL: {diff('atr_sl_mult', best['ATR_SL_MULT'], 'x ATR')}\n"
            f"TP: {diff('atr_tp_mult', best['ATR_TP_MULT'], 'x ATR')}\n"
            f"Min. confluence: {diff('min_confluence', best['MIN_CONFLUENCE'])}\n"
            f"Min. R:R: {diff('min_rr', best['MIN_RR'])}\n"
            f"Min. confidence: {diff('confidence_thresh', best['confidence_thresh'])}\n\n"
            f"Aktiv fra næste scan. Risikostyring (position-størrelse, daglig "
            f"tabsgrænse, stop-loss) er uændret."
        )
        await _notify(redis, msg)
        log.info("Nightly tune complete: %s", new_values)

    except Exception as e:
        log.exception("Nightly tune failed")
        await _notify(redis, f"🌙 *Nightly tune fejlede*: {e}")
        raise
    finally:
        bridge.stop()
        listen_task.cancel()
        await redis.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    asyncio.run(run())
