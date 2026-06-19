"""
Pure technical-analysis math. No external deps beyond numpy.
All functions take plain lists/arrays of floats (oldest → newest).
"""
import numpy as np


def rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    arr = np.asarray(closes, dtype=float)
    deltas = np.diff(arr)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))


def ema(closes: list[float], period: int) -> float:
    arr = np.asarray(closes, dtype=float)
    if len(arr) < period:
        return float(arr[-1]) if len(arr) else 0.0
    weights = np.exp(np.linspace(-1.0, 0.0, period))
    weights /= weights.sum()
    return float(np.convolve(arr, weights, mode="valid")[-1])


def ema_series(closes: list[float], period: int) -> list[float]:
    arr = np.asarray(closes, dtype=float)
    if len(arr) == 0:
        return []
    alpha = 2 / (period + 1)
    out = [arr[0]]
    for v in arr[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def macd(closes: list[float]) -> tuple[float, float, float]:
    """Returns (macd_line, signal_line, histogram) using 12/26/9 EMA."""
    if len(closes) < 26:
        return 0.0, 0.0, 0.0
    ema12 = ema_series(closes, 12)
    ema26 = ema_series(closes, 26)
    macd_line_series = [a - b for a, b in zip(ema12[-len(ema26):], ema26)]
    signal_series = ema_series(macd_line_series, 9)
    macd_line = macd_line_series[-1]
    signal_line = signal_series[-1]
    return float(macd_line), float(signal_line), float(macd_line - signal_line)


def bollinger(closes: list[float], period: int = 20, num_std: float = 2.0):
    arr = np.asarray(closes[-period:], dtype=float)
    if len(arr) == 0:
        return 0.0, 0.0, 0.0
    mid = float(np.mean(arr))
    std = float(np.std(arr))
    return mid - num_std * std, mid, mid + num_std * std


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    n = min(len(highs), len(lows), len(closes))
    if n < 2:
        return 0.0
    h, l, c = np.asarray(highs[-n:]), np.asarray(lows[-n:]), np.asarray(closes[-n:])
    prev_close = np.roll(c, 1)
    prev_close[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_close), np.abs(l - prev_close)))
    window = tr[-period:] if len(tr) >= period else tr
    return float(np.mean(window))


def volume_ratio(volumes: list[float], period: int = 20) -> float:
    arr = np.asarray(volumes, dtype=float)
    arr = arr[arr > 0]
    if len(arr) < 2:
        return 1.0
    avg = np.mean(arr[-period:-1]) if len(arr) > period else np.mean(arr[:-1])
    if avg == 0:
        return 1.0
    return float(arr[-1] / avg)


def stochastic(highs: list[float], lows: list[float], closes: list[float], k_period: int = 14) -> float:
    n = min(len(highs), len(lows), len(closes))
    if n < k_period:
        k_period = n
    if k_period < 1:
        return 50.0
    h = np.asarray(highs[-k_period:])
    l = np.asarray(lows[-k_period:])
    c = closes[-1]
    hh, ll = float(np.max(h)), float(np.min(l))
    if hh == ll:
        return 50.0
    return float((c - ll) / (hh - ll) * 100)
