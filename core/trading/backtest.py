"""
Walk-forward backtest of the signal engine against historical OHLCV data.

Turns "optimize to a realistic win rate" into an actual measured number
instead of a guess: replays the same score_signal() logic the live scanner
uses, candle by candle, and simulates each accepted signal forward until
its SL or TP is hit. Also used to pre-seed core/trading/learning.py with
real historical outcomes, so a fresh account doesn't have to lose 5 live
trades on a bad setup before the bot learns to stop using it.

Run standalone:  python -m core.trading.backtest EURUSD=X
(needs REDIS_URL set and the MT5 Worker connected — history now comes
straight from the broker via MT5, not Yahoo Finance; see fetch_history.)
"""
import asyncio
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone

from .market_monitor import _resample_4h, CONFIDENCE_THRESH
from .signal_engine import score_signal

WINDOW   = 200   # candles fed to score_signal at each step (matches live ~200-candle history)
MAX_HOLD = 200   # max candles to hold a simulated trade before giving up (counted as "no exit")


async def fetch_history(mt5_bridge, symbol: str, timeframe: str = "1h", count: int = 5000) -> list:
    """Historical OHLCV via the MT5 Worker (broker's own history) — replaces
    Yahoo Finance, which blocks/rate-limits Railway's outbound IP."""
    result = await mt5_bridge.get_rates(symbol, timeframe, count)
    if "error" in result:
        return []
    return result.get("ohlcv") or []


def _pnl_r(direction: str, entry: float, exit_price: float, risk: float) -> float:
    if direction == "long":
        return (exit_price - entry) / risk
    return (entry - exit_price) / risk


def _simulate_exit(ohlcv: list, start_idx: int, direction: str,
                   entry: float, sl: float, tp: float) -> float | None:
    """Walk forward until SL or TP is touched. None if neither hits within MAX_HOLD."""
    risk = abs(entry - sl)
    if risk == 0:
        return None
    for i in range(start_idx + 1, min(start_idx + 1 + MAX_HOLD, len(ohlcv))):
        high, low = ohlcv[i][2], ohlcv[i][3]
        if direction == "long":
            sl_hit, tp_hit = low <= sl, high >= tp
        else:
            sl_hit, tp_hit = high >= sl, low <= tp
        if sl_hit:   # conservative: if both touched in the same candle, assume SL first
            return _pnl_r(direction, entry, sl, risk)
        if tp_hit:
            return _pnl_r(direction, entry, tp, risk)
    return None


def simulate(ohlcv: list, ohlcv_15m: list | None = None, symbol: str = "",
             confidence_thresh: float = CONFIDENCE_THRESH) -> dict:
    """
    The actual backtest loop, separated from data-fetching so a parameter
    sweep (see optimize.py) can fetch each symbol's history once and replay
    it many times with different thresholds — without re-hitting the
    broker for every combination.

    `ohlcv_15m`, if supplied, lets gold's Asian Range Sweep / confluence
    checks (which need 15m data) actually fire during backtests instead of
    silently never triggering — they were previously untestable here since
    this function never received 15m candles at all. Each step slices
    ohlcv_15m to bars at-or-before the current 1h candle's own close, so no
    future 15m data leaks into a signal being scored "as of" an earlier time.
    """
    by_setup = defaultdict(lambda: {"wins": 0, "losses": 0, "total_r": 0.0})
    overall  = {"wins": 0, "losses": 0, "total_r": 0.0, "skipped_no_exit": 0}

    i = WINDOW
    while i < len(ohlcv) - 1:
        window = ohlcv[max(0, i - WINDOW):i + 1]
        candle_time = datetime.fromtimestamp(window[-1][0] / 1000, tz=timezone.utc)

        window_15m = None
        if ohlcv_15m:
            cutoff_ms = window[-1][0] + 3600_000   # this 1h candle's own close
            window_15m = [c for c in ohlcv_15m if c[0] <= cutoff_ms][-500:]

        signal = score_signal(window, _resample_4h(window), None, ohlcv_15m=window_15m,
                               at=candle_time, symbol=symbol)

        if (signal["direction"] != "neutral" and signal.get("checklist_ok")
                and signal["confidence"] >= confidence_thresh):
            setup = signal.get("setup_type") or "unknown"
            pnl_r = _simulate_exit(ohlcv, i, signal["direction"],
                                   signal["price"], signal["stop_loss"], signal["take_profit"])
            if pnl_r is None:
                overall["skipped_no_exit"] += 1
                i += 1
                continue

            bucket = by_setup[setup]
            if pnl_r > 0:
                bucket["wins"] += 1
                overall["wins"] += 1
            else:
                bucket["losses"] += 1
                overall["losses"] += 1
            bucket["total_r"] += pnl_r
            overall["total_r"] += pnl_r
            i += MAX_HOLD   # skip past the simulated trade — no overlapping positions
        else:
            i += 1

    def _finish(b: dict) -> dict:
        total = b["wins"] + b["losses"]
        return {**b, "total": total,
                "win_rate": b["wins"] / total if total else 0,
                "avg_r":    b["total_r"] / total if total else 0}

    return {
        "overall": _finish(overall),
        "by_setup": {k: _finish(v) for k, v in by_setup.items()},
    }


async def run_backtest(mt5_bridge, symbol: str, count: int = 5000,
                       confidence_thresh: float = CONFIDENCE_THRESH) -> dict:
    ohlcv = await fetch_history(mt5_bridge, symbol, "1h", count)
    if len(ohlcv) < WINDOW + 10:
        return {"error": f"Ikke nok historik for {symbol} ({len(ohlcv)} candles) — er MT5 Worker online?"}
    ohlcv_15m = await fetch_history(mt5_bridge, symbol, "15m", count * 4)
    result = simulate(ohlcv, ohlcv_15m=ohlcv_15m, symbol=symbol, confidence_thresh=confidence_thresh)
    return {"symbol": symbol, "candles": len(ohlcv), **result}


async def seed_learning(redis, mt5_bridge, symbols: list[str]) -> dict:
    """
    Pre-seeds core/trading/learning.py's win/loss counters from backtest
    results, so a fresh deployment already knows which setups have a bad
    real track record instead of needing 5+ live trades to find out.
    """
    from . import learning
    seeded = {}
    for symbol in symbols:
        try:
            result = await run_backtest(mt5_bridge, symbol)
        except Exception:
            continue
        if "error" in result:
            continue
        for setup, stats in result["by_setup"].items():
            key = f"{learning.KEY_PREFIX}{setup}"
            await redis.hincrby(key, "wins", stats["wins"])
            await redis.hincrby(key, "losses", stats["losses"])
        seeded[symbol] = result["overall"]
    return seeded


def _cli():
    import os
    import redis.asyncio as aioredis
    from .mt5_bridge import MT5Bridge

    async def _run():
        redis_url = os.environ.get("REDIS_URL")
        if not redis_url:
            print("FEJL: sæt REDIS_URL (samme som mt5_agent-servicen bruger).", file=sys.stderr)
            sys.exit(1)
        redis = aioredis.from_url(redis_url, decode_responses=True)
        bridge = MT5Bridge(redis)
        listen_task = asyncio.create_task(bridge.run())
        try:
            symbol = sys.argv[1] if len(sys.argv) > 1 else "EURUSD=X"
            result = await run_backtest(bridge, symbol)
            print(json.dumps(result, indent=2))
        finally:
            bridge.stop()
            listen_task.cancel()
            await redis.close()

    asyncio.run(_run())


if __name__ == "__main__":
    _cli()
