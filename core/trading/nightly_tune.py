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

TUNED_KEY_PREFIX = "trading:tuned_params:"
SYMBOLS = ["GC=F"]   # gold-only account — see DEFAULT_FOREX in market_monitor.py


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
        key  = f"{TUNED_KEY_PREFIX}{SYMBOLS[0]}"
        old  = await redis.hgetall(key)

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
            f"🌙 *Nightly tune — XAUUSD*\n"
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
