"""
Parameter sweep — finds the (confidence threshold, confluence requirement,
SL/TP multipliers) combination with the best real backtested expectancy
across a basket of symbols.

This does NOT touch risk management (position sizing, daily-loss lock,
stop loss enforcement) — those stay fixed regardless of what this finds.
It only tunes which setups the signal engine treats as good enough to act
on. Fetches each symbol's history once, then monkeypatches
signal_engine's module constants to replay it under many parameter
combinations without re-hitting Yahoo Finance per combination.

Run standalone: python -m core.trading.optimize
"""
import asyncio
import itertools
import json
import sys
import time

from . import signal_engine as se
from .backtest import fetch_history, simulate

DEFAULT_SYMBOLS = ["EURUSD=X", "GBPUSD=X", "GC=F", "USDJPY=X", "GBPJPY=X"]

# Kept deliberately small — an earlier 3x3x3x3x6 grid (2430 simulate() calls)
# ran for over 2 hours without finishing; looser thresholds trigger far more
# signals (each needing its own forward-walk to find the exit), so per-call
# cost varies a lot more than a single-combo timing test suggested. This
# grid finishes in well under an hour and still covers the parameters that
# matter most (confidence + SL/TP sizing).
PARAM_GRID = {
    "MIN_CONFLUENCE": [2, 3],
    "ATR_SL_MULT":    [1.0, 1.5, 2.0],
    "ATR_TP_MULT":    [2.0, 3.0],
    "MIN_RR":         [1.5, 2.0],
}
CONFIDENCE_GRID = [0.60, 0.65, 0.68, 0.72]

MIN_SAMPLE_SIZE = 15   # don't trust a win rate/expectancy computed from too few trades


async def run_sweep(symbols: list = None, progress: bool = False) -> dict:
    symbols = symbols or DEFAULT_SYMBOLS
    histories = {}
    for sym in symbols:
        try:
            ohlcv = await fetch_history(sym, "1h", "730d")
            if len(ohlcv) > 200:
                histories[sym] = ohlcv
        except Exception:
            continue

    original = {k: getattr(se, k) for k in PARAM_GRID}
    results = []
    keys   = list(PARAM_GRID.keys())
    combos = list(itertools.product(*PARAM_GRID.values()))
    total_settings = len(combos) * len(CONFIDENCE_GRID)
    done = 0
    t_start = time.time()
    try:
        for combo in combos:
            params = dict(zip(keys, combo))
            for k, v in params.items():
                setattr(se, k, v)
            for conf in CONFIDENCE_GRID:
                agg = {"wins": 0, "losses": 0, "total_r": 0.0}
                for ohlcv in histories.values():
                    o = simulate(ohlcv, confidence_thresh=conf)["overall"]
                    agg["wins"]    += o["wins"]
                    agg["losses"]  += o["losses"]
                    agg["total_r"] += o["total_r"]
                total = agg["wins"] + agg["losses"]
                done += 1
                if progress and done % 5 == 0:
                    elapsed = time.time() - t_start
                    rate = elapsed / done
                    eta_min = (total_settings - done) * rate / 60
                    print(f"[{done}/{total_settings}] elapsed={elapsed/60:.1f}min "
                          f"eta={eta_min:.1f}min", file=sys.stderr, flush=True)
                if total < MIN_SAMPLE_SIZE:
                    continue
                results.append({
                    **params, "confidence_thresh": conf,
                    "total_trades": total,
                    "win_rate": round(agg["wins"] / total, 4),
                    "avg_r":    round(agg["total_r"] / total, 4),
                    "total_r":  round(agg["total_r"], 2),
                })
    finally:
        for k, v in original.items():
            setattr(se, k, v)

    results.sort(key=lambda r: r["avg_r"], reverse=True)
    return {
        "symbols_tested":      list(histories.keys()),
        "combinations_tested": len(results),
        "top_10":              results[:10],
        "current_defaults": {
            **original,
            "CONFIDENCE_THRESH": "set via SIGNAL_CONFIDENCE env in market_monitor.py",
        },
    }


def _cli():
    result = asyncio.run(run_sweep(progress=True))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    _cli()
