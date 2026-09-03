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


async def run_sweep(mt5_bridge, symbols: list = None, progress: bool = False) -> dict:
    symbols = symbols or DEFAULT_SYMBOLS
    histories     = {}   # sym -> ohlcv_1h
    histories_15m = {}   # sym -> ohlcv_15m
    for sym in symbols:
        try:
            ohlcv = await fetch_history(mt5_bridge, sym, "1h", 5000)
            if len(ohlcv) > 200:
                histories[sym] = ohlcv
                # 15m data needed for gold's Asian-range-sweep/confluence logic
                # (core/trading/asian_range.py, core/trading/confluences.py) --
                # without it those checks silently never fire during the sweep.
                histories_15m[sym] = await fetch_history(mt5_bridge, sym, "15m", 5000 * 4)
        except Exception:
            continue

    original = {k: getattr(se, k) for k in PARAM_GRID}
    results = []
    # Per-symbol breakdown, not just the blended basket total -- forex and
    # gold use different score_signal profiles (core/trading/engine/config.py)
    # now, so "best settings overall" can obscure gold-specific results that
    # a blended average would average away.
    per_symbol_results = {sym: [] for sym in histories}
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
                per_sym_agg = {sym: {"wins": 0, "losses": 0, "total_r": 0.0} for sym in histories}
                for sym, ohlcv in histories.items():
                    o = simulate(ohlcv, ohlcv_15m=histories_15m.get(sym), symbol=sym,
                                 confidence_thresh=conf)["overall"]
                    agg["wins"]    += o["wins"]
                    agg["losses"]  += o["losses"]
                    agg["total_r"] += o["total_r"]
                    per_sym_agg[sym]["wins"]    += o["wins"]
                    per_sym_agg[sym]["losses"]  += o["losses"]
                    per_sym_agg[sym]["total_r"] += o["total_r"]
                total = agg["wins"] + agg["losses"]
                done += 1
                if progress and done % 5 == 0:
                    elapsed = time.time() - t_start
                    rate = elapsed / done
                    eta_min = (total_settings - done) * rate / 60
                    print(f"[{done}/{total_settings}] elapsed={elapsed/60:.1f}min "
                          f"eta={eta_min:.1f}min", file=sys.stderr, flush=True)
                if total >= MIN_SAMPLE_SIZE:
                    results.append({
                        **params, "confidence_thresh": conf,
                        "total_trades": total,
                        "win_rate": round(agg["wins"] / total, 4),
                        "avg_r":    round(agg["total_r"] / total, 4),
                        "total_r":  round(agg["total_r"], 2),
                    })
                for sym, s in per_sym_agg.items():
                    sym_total = s["wins"] + s["losses"]
                    if sym_total >= MIN_SAMPLE_SIZE:
                        per_symbol_results[sym].append({
                            **params, "confidence_thresh": conf,
                            "total_trades": sym_total,
                            "win_rate": round(s["wins"] / sym_total, 4),
                            "avg_r":    round(s["total_r"] / sym_total, 4),
                            "total_r":  round(s["total_r"], 2),
                        })
    finally:
        for k, v in original.items():
            setattr(se, k, v)

    results.sort(key=lambda r: r["avg_r"], reverse=True)
    for sym in per_symbol_results:
        per_symbol_results[sym].sort(key=lambda r: r["avg_r"], reverse=True)

    return {
        "symbols_tested":      list(histories.keys()),
        "combinations_tested": len(results),
        "top_10":              results[:10],
        "top_5_by_symbol":     {sym: res[:5] for sym, res in per_symbol_results.items()},
        "current_defaults": {
            **original,
            "CONFIDENCE_THRESH": "set via SIGNAL_CONFIDENCE env in market_monitor.py",
        },
    }


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
            result = await run_sweep(bridge, progress=True)
            print(json.dumps(result, indent=2))
        finally:
            bridge.stop()
            listen_task.cancel()
            await redis.close()

    asyncio.run(_run())


if __name__ == "__main__":
    _cli()
