from __future__ import annotations

"""
Break & Retest engine (spec section 8). A valid break requires a candle
CLOSE beyond the structural level with real momentum -- a wick-only poke
through the level is explicitly NOT treated as a valid break (spec:
"Et wick-only break må IKKE automatisk betragtes som et valid break").
After a valid break, price must retest back toward the level from the new
side before the setup is considered live.
"""
from . import market_structure

Candle = list


def _breakout_momentum(ohlcv: list[Candle], index: int) -> float:
    """Body-to-range ratio of the breakout candle -- a genuine break moves
    with conviction, not on a thin doji that happens to close past a level."""
    c = ohlcv[index]
    rng = (c[2] - c[3]) or 1e-10
    body = abs(c[4] - c[1])
    return body / rng


def _find_break_index(ohlcv: list[Candle], swing_index: int, level: float,
                       direction: str, min_momentum: float) -> int | None:
    """First candle after the swing whose CLOSE (not wick) breaks the level
    with sufficient body-to-range momentum."""
    for i in range(swing_index + 1, len(ohlcv)):
        c = ohlcv[i]
        broke = c[4] > level if direction == "bullish" else c[4] < level
        if broke and _breakout_momentum(ohlcv, i) >= min_momentum:
            return i
    return None


def detect(ohlcv: list[Candle], min_momentum: float = 0.5,
           retest_tolerance_pct: float = 0.4) -> dict | None:
    """
    1. Take the most recent swing high/low as the structural level
       (market_structure.find_swings).
    2. Require a genuine close-through break with momentum >= min_momentum.
    3. Require current price to have come back within retest_tolerance_pct
       of that level from the breakout side (not crossed back through it).
    """
    if len(ohlcv) < 45:
        return None
    swings = market_structure.find_swings(ohlcv, left=2, right=2)
    if len(swings) < 2:
        return None
    current = ohlcv[-1][4]

    highs = [s for s in swings if s["type"] == "high"]
    if highs:
        level = highs[-1]["price"]
        break_idx = _find_break_index(ohlcv, highs[-1]["index"], level, "bullish", min_momentum)
        if break_idx is not None:
            near = abs(current - level) / level * 100 < retest_tolerance_pct
            still_above = current >= level * (1 - retest_tolerance_pct / 100)
            if near and still_above:
                return {
                    "type": "bullish_break_retest", "direction": "long",
                    "label": f"Bullish Break & Retest @ {level:.5g}",
                    "level": level, "limit_price": level, "break_index": break_idx,
                }

    lows = [s for s in swings if s["type"] == "low"]
    if lows:
        level = lows[-1]["price"]
        break_idx = _find_break_index(ohlcv, lows[-1]["index"], level, "bearish", min_momentum)
        if break_idx is not None:
            near = abs(current - level) / level * 100 < retest_tolerance_pct
            still_below = current <= level * (1 + retest_tolerance_pct / 100)
            if near and still_below:
                return {
                    "type": "bearish_break_retest", "direction": "short",
                    "label": f"Bearish Break & Retest @ {level:.5g}",
                    "level": level, "limit_price": level, "break_index": break_idx,
                }
    return None
