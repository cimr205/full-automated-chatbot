"""
Pure technical analysis functions. All inputs are plain Python lists.
No external dependencies beyond numpy.
"""
import numpy as np


def rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    arr = np.array(closes[-(period + 1):], dtype=float)
    deltas = np.diff(arr)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains.mean()
    avg_loss = losses.mean()
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


def ema(closes: list, period: int) -> float:
    if not closes:
        return 0.0
    arr = np.array(closes, dtype=float)
    if len(arr) < period:
        return float(arr[-1])
    k = 2.0 / (period + 1)
    val = arr[0]
    for c in arr[1:]:
        val = c * k + val * (1 - k)
    return float(val)


def macd(closes: list) -> tuple[float, float, float]:
    """Returns (macd_line, signal_line, histogram)."""
    if len(closes) < 35:
        return 0.0, 0.0, 0.0
    e12 = ema(closes, 12)
    e26 = ema(closes, 26)
    line = e12 - e26
    # Approximate signal as 9-period EMA of the last computed macd values
    # We build a mini MACD series from the last 9 windows for better accuracy
    macd_series = []
    for i in range(9, 0, -1):
        sl = closes[:-i] if i > 0 else closes
        if len(sl) >= 26:
            macd_series.append(ema(sl, 12) - ema(sl, 26))
    macd_series.append(line)
    signal = ema(macd_series, 9)
    return line, signal, line - signal


def bollinger(closes: list, period: int = 20, num_std: float = 2.0):
    """Returns (upper, middle, lower, pct_b)."""
    if len(closes) < period:
        c = float(closes[-1]) if closes else 0.0
        return c, c, c, 0.5
    recent = np.array(closes[-period:], dtype=float)
    mid = float(recent.mean())
    std = float(recent.std())
    upper = mid + num_std * std
    lower = mid - num_std * std
    current = float(closes[-1])
    pct_b = (current - lower) / (upper - lower) if upper != lower else 0.5
    return upper, mid, lower, pct_b


def atr(highs: list, lows: list, closes: list, period: int = 14) -> float:
    """Average True Range — used for stop-loss / take-profit sizing."""
    if len(closes) < period + 1:
        return 0.0
    trs = []
    for i in range(1, period + 1):
        h = highs[-i]
        l = lows[-i]
        prev_c = closes[-i - 1]
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    return float(np.mean(trs))


def volume_ratio(volumes: list, period: int = 20) -> float:
    """Current volume vs. N-period average. >1.5 = elevated activity."""
    if len(volumes) < period + 1:
        return 1.0
    avg = np.mean(volumes[-period - 1:-1])
    return float(volumes[-1] / avg) if avg else 1.0


def stochastic(highs: list, lows: list, closes: list, k_period: int = 14) -> float:
    """Stochastic %K oscillator."""
    if len(closes) < k_period:
        return 50.0
    h = max(highs[-k_period:])
    l = min(lows[-k_period:])
    if h == l:
        return 50.0
    return (closes[-1] - l) / (h - l) * 100
