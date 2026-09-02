from __future__ import annotations

"""
Fair Value Gap engine (spec section 7). Detects 3-candle imbalances, tracks
mitigation over time, and scores quality: higher when the gap follows a
liquidity sweep, follows strong displacement, sits in the HTF trend
direction, and isn't already deeply filled.
"""

Candle = list


def find_fvgs(ohlcv: list[Candle], lookback: int = 40, timeframe: str = "") -> list[dict]:
    """Scans the last `lookback` 3-candle windows for imbalances. Returns
    ALL gaps found (not just the nearest to current price) so callers can
    check each one's mitigation status independently."""
    if len(ohlcv) < 5:
        return []
    gaps = []
    start = max(0, len(ohlcv) - lookback - 2)
    for i in range(start, len(ohlcv) - 2):
        c1, c2, c3 = ohlcv[i], ohlcv[i + 1], ohlcv[i + 2]
        if c1[2] < c3[3]:      # bullish: candle1 high < candle3 low
            gaps.append(_build_gap("bullish", c1[2], c3[3], c2, i, timeframe))
        elif c3[2] < c1[3]:    # bearish: candle3 high < candle1 low
            gaps.append(_build_gap("bearish", c3[2], c1[3], c2, i, timeframe))
    return gaps


def _build_gap(kind: str, bot: float, top: float, c2: Candle, index: int, timeframe: str) -> dict:
    mid_range = (c2[2] - c2[3]) or 1e-10
    # Displacement: the middle candle's body dominates its own range and
    # moves strongly in the gap's direction -- distinguishes a genuine
    # imbalance from three quiet, overlapping candles that technically
    # leave a small gap.
    body = abs(c2[4] - c2[1])
    displacement = body / mid_range >= 0.6
    return {
        "type": f"{kind}_fvg", "direction": "long" if kind == "bullish" else "short",
        "bottom": bot, "top": top, "size": top - bot,
        "timeframe": timeframe, "created_index": index, "created_ts": c2[0],
        "displacement": displacement,
    }


def mitigation_status(gap: dict, ohlcv: list[Candle]) -> dict:
    """How much of the gap has since been traded through, using candles
    after its creation index. >=95% filled counts as mitigated."""
    after = ohlcv[gap["created_index"] + 3:]
    if not after:
        return {**gap, "filled_pct": 0.0, "mitigated": False}
    bot, top = gap["bottom"], gap["top"]
    size = (top - bot) or 1e-10
    if gap["direction"] == "long":
        deepest = min((c[3] for c in after), default=top)
        filled = max(0.0, min(1.0, (top - deepest) / size))
    else:
        deepest = max((c[2] for c in after), default=bot)
        filled = max(0.0, min(1.0, (deepest - bot) / size))
    return {**gap, "filled_pct": round(filled, 3), "mitigated": filled >= 0.95}


def current_price_in_zone(gap: dict, price: float, tolerance: float = 0.003) -> bool:
    bot, top = gap["bottom"], gap["top"]
    return bot * (1 - tolerance) <= price <= top * (1 + tolerance)


def score_fvg_quality(gap: dict, followed_sweep: bool, htf_direction: str, filled_pct: float) -> float:
    """0-1 quality score. Higher if: follows a liquidity sweep, formed by
    displacement, aligned with HTF trend direction, not deeply mitigated."""
    score = 0.4
    if gap.get("displacement"):
        score += 0.25
    if followed_sweep:
        score += 0.2
    if htf_direction and htf_direction == gap["direction"]:
        score += 0.15
    score -= filled_pct * 0.3
    return round(max(0.0, min(1.0, score)), 3)


def nearest_unmitigated(ohlcv: list[Candle], direction: str, current_price: float,
                         lookback: int = 40, timeframe: str = "") -> dict | None:
    """Nearest not-yet-mitigated gap matching `direction` ('long'/'short'),
    closest to current_price -- the one relevant to a live entry decision."""
    gaps = [g for g in find_fvgs(ohlcv, lookback, timeframe) if g["direction"] == direction]
    candidates = [m for g in gaps if not (m := mitigation_status(g, ohlcv))["mitigated"]]
    if not candidates:
        return None
    return min(candidates, key=lambda g: abs(current_price - (g["bottom"] + g["top"]) / 2))
