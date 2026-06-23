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

from . import signal_engine as se
from .backtest import fetch_history, simulate

DEFAULT_SYMBOLS = ["EURUSD=X", "GBPUSD=X", "GC=F", "USDJPY=X", "GBPJPY=X"]

PARAM_GRID = {
    "MIN_CONFLUENCE": [2, 3, 4],
    "ATR_SL_MULT":    [1.0, 1.5, 2.0],
    "ATR_TP_MULT":    [2.0, 3.0, 4.0],
    "MIN_RR":         [1.5, 2.0, 2.5],
}
CONFIDENCE_GRID = [0.55, 0.60, 0.65, 0.68, 0.72, 0.76]

MIN_SAMPLE_SIZE = 15   # don't trust a win rate/expectancy computed from too few trades


async def run_sweep(symbols: list = None) -> dict:
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
    try:
        keys = list(PARAM_GRID.keys())
        combos = list(itertools.product(*PARAM_GRID.values()))
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
    result = asyncio.run(run_sweep())
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    _cli()
