"""
Walk-forward backtest of the signal engine against historical OHLCV data.

Turns "optimize to a realistic win rate" into an actual measured number
instead of a guess: replays the same score_signal() logic the live scanner
uses, candle by candle, and simulates each accepted signal forward until
its SL or TP is hit. Also used to pre-seed core/trading/learning.py with
real historical outcomes, so a fresh account doesn't have to lose 5 live
trades on a bad setup before the bot learns to stop using it.

Run standalone:  python -m core.trading.backtest EURUSD=X
"""
import asyncio
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone

import httpx

from .market_monitor import _resample_4h, CONFIDENCE_THRESH
from .signal_engine import score_signal

WINDOW   = 200   # candles fed to score_signal at each step (matches live ~200-candle history)
MAX_HOLD = 200   # max candles to hold a simulated trade before giving up (counted as "no exit")


async def fetch_history(symbol: str, interval: str = "1h", range_: str = "730d") -> list:
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval={interval}&range={range_}&includePrePost=false"
    )
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

    result = data.get("chart", {}).get("result", [])
    if not result:
        return []
    r = result[0]
    timestamps = r.get("timestamp", [])
    q = r.get("indicators", {}).get("quote", [{}])[0]
    ohlcv = []
    for i, ts in enumerate(timestamps):
        try:
            c = q["close"][i]
            if c is None:
                continue
            ohlcv.append([ts * 1000, q["open"][i] or c, q["high"][i] or c,
                          q["low"][i] or c, c, q["volume"][i] or 0])
        except (IndexError, TypeError):
            continue
    return ohlcv


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


def simulate(ohlcv: list, confidence_thresh: float = CONFIDENCE_THRESH) -> dict:
    """
    The actual backtest loop, separated from data-fetching so a parameter
    sweep (see optimize.py) can fetch each symbol's history once and replay
    it many times with different thresholds — without re-hitting Yahoo
    Finance for every combination.
    """
    by_setup = defaultdict(lambda: {"wins": 0, "losses": 0, "total_r": 0.0})
    overall  = {"wins": 0, "losses": 0, "total_r": 0.0, "skipped_no_exit": 0}

    i = WINDOW
    while i < len(ohlcv) - 1:
        window = ohlcv[max(0, i - WINDOW):i + 1]
        candle_time = datetime.fromtimestamp(window[-1][0] / 1000, tz=timezone.utc)
        signal = score_signal(window, _resample_4h(window), None, at=candle_time)

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


async def run_backtest(symbol: str, range_: str = "730d", confidence_thresh: float = CONFIDENCE_THRESH) -> dict:
    ohlcv = await fetch_history(symbol, "1h", range_)
    if len(ohlcv) < WINDOW + 10:
        return {"error": f"Ikke nok historik for {symbol} ({len(ohlcv)} candles)"}
    result = simulate(ohlcv, confidence_thresh=confidence_thresh)
    return {"symbol": symbol, "candles": len(ohlcv), **result}


async def seed_learning(redis, symbols: list[str]) -> dict:
    """
    Pre-seeds core/trading/learning.py's win/loss counters from backtest
    results, so a fresh deployment already knows which setups have a bad
    real track record instead of needing 5+ live trades to find out.
    """
    from . import learning
    seeded = {}
    for symbol in symbols:
        try:
            result = await run_backtest(symbol)
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
    symbol = sys.argv[1] if len(sys.argv) > 1 else "EURUSD=X"
    result = asyncio.run(run_backtest(symbol))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    _cli()
