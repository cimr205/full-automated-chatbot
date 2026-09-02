from __future__ import annotations

"""
Reusable market structure engine — swing points, HH/HL/LH/LL classification,
Break of Structure (BOS) and Change of Character (CHOCH), and per-timeframe
bias. Used by every strategy engine (FVG, break & retest, trend pullback,
liquidity grab) instead of each one re-deriving structure independently.

Candle format everywhere in this package: [ts_ms, open, high, low, close, volume].
"""

Candle = list
Swing = dict  # {"index": int, "price": float, "type": "high"|"low", "ts": int}


def find_swings(ohlcv: list[Candle], left: int = 2, right: int = 2) -> list[Swing]:
    """Fractal swing points: a swing high/low is the extreme of a
    `left + 1 + right` candle window centered on it."""
    if len(ohlcv) < left + right + 1:
        return []
    highs = [c[2] for c in ohlcv]
    lows  = [c[3] for c in ohlcv]
    swings: list[Swing] = []
    for i in range(left, len(ohlcv) - right):
        window_h = highs[i - left:i + right + 1]
        if highs[i] == max(window_h) and window_h.count(highs[i]) == 1:
            swings.append({"index": i, "price": highs[i], "type": "high", "ts": ohlcv[i][0]})
        window_l = lows[i - left:i + right + 1]
        if lows[i] == min(window_l) and window_l.count(lows[i]) == 1:
            swings.append({"index": i, "price": lows[i], "type": "low", "ts": ohlcv[i][0]})
    swings.sort(key=lambda s: s["index"])
    return _alternate(swings)


def _alternate(swings: list[Swing]) -> list[Swing]:
    """Collapse consecutive same-type swings to the single most extreme one,
    so highs/lows strictly alternate (a precondition for HH/HL/LH/LL labeling)."""
    cleaned: list[Swing] = []
    for s in swings:
        if cleaned and cleaned[-1]["type"] == s["type"]:
            more_extreme = (s["price"] > cleaned[-1]["price"] if s["type"] == "high"
                             else s["price"] < cleaned[-1]["price"])
            if more_extreme:
                cleaned[-1] = s
        else:
            cleaned.append(s)
    return cleaned


def classify_structure(swings: list[Swing]) -> dict:
    """Labels each swing high as HH/LH and each swing low as HL/LL relative
    to the previous swing of the same type."""
    highs = [s for s in swings if s["type"] == "high"]
    lows  = [s for s in swings if s["type"] == "low"]
    high_labels = [
        ("HH" if highs[i]["price"] > highs[i - 1]["price"] else "LH", highs[i])
        for i in range(1, len(highs))
    ]
    low_labels = [
        ("HL" if lows[i]["price"] > lows[i - 1]["price"] else "LL", lows[i])
        for i in range(1, len(lows))
    ]
    return {"swings": swings, "highs": highs, "lows": lows,
            "high_labels": high_labels, "low_labels": low_labels}


def structure_bias(classified: dict) -> str:
    """bullish = latest swing high is HH AND latest swing low is HL.
    bearish = latest swing high is LH AND latest swing low is LL.
    Anything mixed (one HH one LL, insufficient history, etc.) = neutral —
    i.e. a genuine range, not force-fit into a direction."""
    hl = classified["high_labels"][-1][0] if classified["high_labels"] else None
    ll = classified["low_labels"][-1][0] if classified["low_labels"] else None
    if hl == "HH" and ll == "HL":
        return "bullish"
    if hl == "LH" and ll == "LL":
        return "bearish"
    return "neutral"


def bias(ohlcv: list[Candle], left: int = 2, right: int = 2) -> str:
    if len(ohlcv) < 20:
        return "neutral"
    swings = find_swings(ohlcv, left, right)
    return structure_bias(classify_structure(swings))


def multi_timeframe_bias(frames: dict[str, list[Candle]]) -> dict[str, str]:
    """frames e.g. {"daily": ohlcv_1d, "h4": ohlcv_4h, "h1": ohlcv_1h,
    "m15": ohlcv_15m, "m5": ohlcv_5m}. Missing/short frames come back
    'neutral' rather than raising, since not every caller has all 5."""
    return {tf: (bias(ohlcv) if ohlcv else "neutral") for tf, ohlcv in frames.items()}


def detect_bos_choch(ohlcv: list[Candle], left: int = 2, right: int = 2) -> dict:
    """
    BOS (Break of Structure) = close beyond the last swing extreme in the
    direction of the prior established bias -> trend continuation.
    CHOCH (Change of Character) = close beyond the last swing extreme
    AGAINST the prior established bias -> first sign of a reversal.

    Prior bias is computed from swing history EXCLUDING the two most recent
    swings, so the event isn't judged against a bias that already includes
    the very break being tested.
    """
    swings = find_swings(ohlcv, left, right)
    if len(swings) < 5:
        return {"event": None, "reason": "insufficient swing history"}

    highs = [s for s in swings if s["type"] == "high"]
    lows  = [s for s in swings if s["type"] == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return {"event": None, "reason": "insufficient swing history"}

    # swings[:-2] needs >= 2 highs and >= 2 lows to produce any HH/HL/LH/LL
    # label at all -- i.e. at least 6 total swings (2 held back + 4 to
    # classify). The old ">6" threshold was off by one and silently forced
    # prior_bias to "neutral" on the very first candle where a real prior
    # bias first becomes computable.
    prior_bias = (structure_bias(classify_structure(swings[:-2]))
                  if len(swings) >= 6 else "neutral")

    last_high, last_low = highs[-1], lows[-1]
    last_close = ohlcv[-1][4]

    event = None
    direction = None
    if last_close > last_high["price"]:
        direction = "bullish"
        event = "BOS" if prior_bias in ("bullish", "neutral") else "CHOCH"
    elif last_close < last_low["price"]:
        direction = "bearish"
        event = "BOS" if prior_bias in ("bearish", "neutral") else "CHOCH"

    if not event:
        return {"event": None, "prior_bias": prior_bias}

    level = last_high["price"] if direction == "bullish" else last_low["price"]
    swing_index = (last_high if direction == "bullish" else last_low)["index"]
    return {
        "event": event, "direction": direction, "prior_bias": prior_bias,
        "level": level, "swing_index": swing_index,
        "label": f"{event} ({direction}) over {level:.5g}",
    }
