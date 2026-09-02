from __future__ import annotations

"""
Trend Pullback engine (spec section 9). Only activates with a clear HTF
trend from market structure (HH/HL or LH/LL), never from an oscillator
reading alone -- the spec is explicit: "Botten må ikke bare købe fordi RSI
er oversold." Price must have pulled back into a defined area (dynamic
EMA21/50 trend zone, or an unmitigated FVG in the trend direction), and a
lower-timeframe structural break in the trend direction is used as an
optional confirmation flag rather than a hard requirement (not every
caller has LTF data).
"""
from . import market_structure
from . import fvg as fvg_engine

Candle = list


def _ema(closes: list[float], period: int) -> float:
    if not closes:
        return 0.0
    if len(closes) < period:
        return closes[-1]
    k = 2 / (period + 1)
    val = closes[0]
    for c in closes[1:]:
        val = c * k + val * (1 - k)
    return val


def detect(ohlcv_htf: list[Candle], ohlcv_ltf: list[Candle] | None = None,
           tol_pct: float = 0.6) -> dict | None:
    """
    ohlcv_htf: the frame that decided directional bias (H4 for forex, H1
    for gold). ohlcv_ltf: optional M15/M5 data for confirmation.
    """
    if len(ohlcv_htf) < 60:
        return None

    trend = market_structure.bias(ohlcv_htf)
    if trend == "neutral":
        return None   # no clear trend -> no trend-pullback setup, period

    closes = [c[4] for c in ohlcv_htf]
    current = closes[-1]
    e21, e50 = _ema(closes, 21), _ema(closes, 50)
    direction = "long" if trend == "bullish" else "short"

    pullback_zone = None
    for level, tag in ((e21, "EMA21"), (e50, "EMA50")):
        if level and abs(current - level) / level * 100 < tol_pct:
            pullback_zone = (level, tag)
            break

    if pullback_zone is None:
        gap = fvg_engine.nearest_unmitigated(ohlcv_htf, direction, current)
        if gap and fvg_engine.current_price_in_zone(gap, current, tolerance=0.004):
            pullback_zone = (round((gap["bottom"] + gap["top"]) / 2, 5), "FVG")

    if pullback_zone is None:
        return None

    ltf_confirmed = False
    if ohlcv_ltf and len(ohlcv_ltf) >= 20:
        ltf_event = market_structure.detect_bos_choch(ohlcv_ltf)
        ltf_confirmed = ltf_event.get("event") == "BOS" and ltf_event.get("direction") == direction

    level, tag = pullback_zone
    return {
        "type": "bullish_pullback" if direction == "long" else "bearish_pullback",
        "direction": direction,
        "label": f"Trend-pullback ({trend}) til {tag} ({level:.5g})",
        "level": level, "trend": trend, "ltf_confirmed": ltf_confirmed,
    }
