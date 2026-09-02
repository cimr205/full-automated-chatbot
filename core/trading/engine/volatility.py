from __future__ import annotations

"""
Volatility engine (spec section 11): classifies current ATR into a regime
relative to its OWN recent history (percentile-based) rather than a fixed
multiple -- a fixed "ATR > 2.0" threshold means something completely
different on EURUSD vs XAUUSD, so it can't be a single constant shared by
both profiles.
"""

Candle = list


def _true_ranges(ohlcv: list[Candle]) -> list[float]:
    trs = []
    for i in range(1, len(ohlcv)):
        h, l = ohlcv[i][2], ohlcv[i][3]
        prev_c = ohlcv[i - 1][4]
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    return trs


def atr_series(ohlcv: list[Candle], period: int = 14) -> list[float]:
    """Rolling simple-moving-average ATR, one value per candle after warmup."""
    trs = _true_ranges(ohlcv)
    if len(trs) < period:
        return []
    return [sum(trs[i - period:i]) / period for i in range(period, len(trs) + 1)]


def classify_regime(ohlcv: list[Candle], period: int = 14, lookback: int = 100) -> dict:
    """Percentile rank of the current ATR reading against its own trailing
    `lookback` history. >=95th percentile = EXTREME, >=75th = HIGH,
    <=25th = LOW, else NORMAL."""
    series = atr_series(ohlcv, period)
    if len(series) < 20:
        return {"regime": "NORMAL", "atr": 0.0, "percentile": 0.5, "reason": "insufficient history"}

    recent = series[-lookback:]
    current = recent[-1]
    rank = sum(1 for v in recent if v <= current)
    percentile = rank / len(recent)

    if percentile >= 0.95:
        regime = "EXTREME"
    elif percentile >= 0.75:
        regime = "HIGH"
    elif percentile <= 0.25:
        regime = "LOW"
    else:
        regime = "NORMAL"

    return {"regime": regime, "atr": current, "percentile": round(percentile, 3)}


# Section 11: at EXTREME the engine must reduce size, require higher
# confidence, or disable entries. We disable entries outright at EXTREME
# (safest default -- a missed trade costs nothing, a bad one in an extreme
# tape can) and reduce size + apply a score penalty at HIGH.
VOLATILITY_ADJUSTMENT = {
    "LOW":     {"size_mult": 1.0,  "score_penalty": 0,  "disable_entries": False},
    "NORMAL":  {"size_mult": 1.0,  "score_penalty": 0,  "disable_entries": False},
    "HIGH":    {"size_mult": 0.75, "score_penalty": 3,  "disable_entries": False},
    "EXTREME": {"size_mult": 0.5,  "score_penalty": 10, "disable_entries": True},
}


def volatility_adjustment(regime: str) -> dict:
    return VOLATILITY_ADJUSTMENT.get(regime, VOLATILITY_ADJUSTMENT["NORMAL"])
