from __future__ import annotations

"""
Liquidity engine (spec section 6): previous day/week high-low, session
highs/lows, equal-highs/equal-lows clusters, and sweep detection.

A sweep is NOT just "a wick poked through a level" -- detect_sweep() also
requires the candle to close back on the origin side (a pure wick-through
without a close-back is still an open test, not a confirmed sweep) and
scores quality from penetration depth + rejection strength, matching the
spec's explicit rejection of "sweep == wick" as a definition.
"""
from datetime import datetime, timezone

from .sessions import TOKYO, LONDON, NEW_YORK, ASIAN_LOCAL, LONDON_LOCAL, NY_LOCAL

Candle = list


def previous_day_levels(ohlcv: list[Candle], at: datetime | None = None) -> dict:
    """PDH/PDL — most recently completed UTC calendar day."""
    moment = at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    today = moment.date()
    prior = [c for c in ohlcv if datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc).date() < today]
    if not prior:
        return {}
    last_day = datetime.fromtimestamp(prior[-1][0] / 1000, tz=timezone.utc).date()
    day_candles = [c for c in prior if datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc).date() == last_day]
    if not day_candles:
        return {}
    return {"pdh": max(c[2] for c in day_candles), "pdl": min(c[3] for c in day_candles), "date": str(last_day)}


def previous_week_levels(ohlcv: list[Candle], at: datetime | None = None) -> dict:
    """PWH/PWL — most recently completed ISO week."""
    moment = at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    cur_y, cur_w, _ = moment.isocalendar()
    tagged = []
    for c in ohlcv:
        dt = datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc)
        y, w, _ = dt.isocalendar()
        if (y, w) < (cur_y, cur_w):
            tagged.append((y, w, c))
    if not tagged:
        return {}
    last_yw = max((y, w) for y, w, _ in tagged)
    week_candles = [c for y, w, c in tagged if (y, w) == last_yw]
    return {"pwh": max(c[2] for c in week_candles), "pwl": min(c[3] for c in week_candles)}


def session_levels(ohlcv_intraday: list[Candle], at: datetime | None = None) -> dict:
    """Today's Asian/London/New York high-low so far, using the same
    exchange-timezone windows as sessions.current_session()."""
    moment = at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    out = {}
    for name, tz, window in (
        ("asian", TOKYO, ASIAN_LOCAL),
        ("london", LONDON, LONDON_LOCAL),
        ("new_york", NEW_YORK, NY_LOCAL),
    ):
        today_local = moment.astimezone(tz).date()
        candles = [
            c for c in ohlcv_intraday
            if (dt := datetime.fromtimestamp(c[0] / 1000, tz=tz)).date() == today_local
            and window[0] <= dt.hour < window[1]
        ]
        if candles:
            out[name] = {"high": max(c[2] for c in candles), "low": min(c[3] for c in candles)}
    return out


def equal_levels(swings: list[dict], tolerance_pct: float = 0.05) -> dict:
    """Clusters swing highs/lows within tolerance_pct of each other -- the
    "obvious liquidity pools" retail stops cluster around. Only clusters
    with 2+ touches are returned (a single swing isn't "equal" anything)."""
    highs = sorted(s["price"] for s in swings if s["type"] == "high")
    lows = sorted(s["price"] for s in swings if s["type"] == "low")

    def _cluster(vals: list[float]) -> list[dict]:
        clusters: list[list[float]] = []
        for v in vals:
            placed = False
            for cl in clusters:
                if abs(v - cl[-1]) / cl[-1] * 100 <= tolerance_pct:
                    cl.append(v)
                    placed = True
                    break
            if not placed:
                clusters.append([v])
        return [{"level": sum(cl) / len(cl), "touches": len(cl)} for cl in clusters if len(cl) >= 2]

    return {"equal_highs": _cluster(highs), "equal_lows": _cluster(lows)}


def detect_sweep(ohlcv: list[Candle], level: float, direction: str, lookback: int = 3) -> dict | None:
    """
    direction: 'sell_side' (sweep below a low-side level -- sell-stops/
    short-entry liquidity resting there) or 'buy_side' (sweep above a
    high-side level).

    Requires BOTH: (1) the level was genuinely penetrated within the last
    `lookback` candles, and (2) the current candle closed back on the
    origin side. A wick-only penetration without a close-back is not
    treated as a confirmed sweep -- it's still an open test of the level.
    """
    if len(ohlcv) < lookback + 1 or level <= 0:
        return None

    recent = list(enumerate(ohlcv))[-lookback:]
    if direction == "sell_side":
        idx, spike = min(recent, key=lambda pair: pair[1][3])
        penetrated = spike[3] < level
        closed_back = ohlcv[-1][4] > level
    elif direction == "buy_side":
        idx, spike = max(recent, key=lambda pair: pair[1][2])
        penetrated = spike[2] > level
        closed_back = ohlcv[-1][4] < level
    else:
        raise ValueError("direction must be 'sell_side' or 'buy_side'")

    if not penetrated or not closed_back:
        return None

    candle_range = (spike[2] - spike[3]) or 1e-10
    if direction == "sell_side":
        penetration_pct = (level - spike[3]) / level * 100
        rejection = (ohlcv[-1][4] - spike[3]) / candle_range
    else:
        penetration_pct = (spike[2] - level) / level * 100
        rejection = (spike[2] - ohlcv[-1][4]) / candle_range

    # Penetration quality peaks around a modest ~0.05% overshoot (a genuine
    # stop-hunt); much deeper starts to look like a real break, not a sweep.
    penetration_score = max(0.0, 1 - abs(penetration_pct - 0.05) / 0.5)
    rejection_score = max(0.0, min(1.0, rejection))
    quality = round(0.5 * rejection_score + 0.5 * min(1.0, penetration_score), 3)

    return {
        "liquidity_event": True,
        "type": f"{direction}_sweep",
        "level": level,
        "penetration_pct": round(penetration_pct, 4),
        "rejection": round(rejection_score, 3),
        "quality": quality,
        "swept_index": idx,
    }


def find_best_sweep(ohlcv: list[Candle], candidate_levels: list[tuple[str, float]],
                     direction: str, lookback: int = 3) -> dict | None:
    """Tries every candidate liquidity level (PDH/PDL, session H/L, equal
    highs/lows, ...) and returns the highest-quality confirmed sweep."""
    best = None
    for name, level in candidate_levels:
        result = detect_sweep(ohlcv, level, direction, lookback)
        if result and (best is None or result["quality"] > best["quality"]):
            result["level_name"] = name
            best = result
    return best


def gather_levels(ohlcv_1h: list[Candle], ohlcv_intraday: list[Candle],
                   swings: list[dict], at: datetime | None = None) -> dict:
    """One-stop collection of every liquidity reference level for a symbol,
    used to build the `candidate_levels` list passed to find_best_sweep()."""
    pd = previous_day_levels(ohlcv_1h, at)
    pw = previous_week_levels(ohlcv_1h, at)
    sess = session_levels(ohlcv_intraday, at)
    eq = equal_levels(swings)

    sell_side: list[tuple[str, float]] = []   # levels below price -- sweeps of these are bullish signals
    buy_side: list[tuple[str, float]] = []    # levels above price -- sweeps of these are bearish signals

    if "pdl" in pd:
        sell_side.append(("pdl", pd["pdl"]))
    if "pdh" in pd:
        buy_side.append(("pdh", pd["pdh"]))
    if "pwl" in pw:
        sell_side.append(("pwl", pw["pwl"]))
    if "pwh" in pw:
        buy_side.append(("pwh", pw["pwh"]))
    for name, hl in sess.items():
        sell_side.append((f"{name}_low", hl["low"]))
        buy_side.append((f"{name}_high", hl["high"]))
    for cl in eq["equal_lows"]:
        sell_side.append(("equal_low", cl["level"]))
    for cl in eq["equal_highs"]:
        buy_side.append(("equal_high", cl["level"]))

    return {"previous_day": pd, "previous_week": pw, "sessions": sess, "equal": eq,
            "sell_side_levels": sell_side, "buy_side_levels": buy_side}
