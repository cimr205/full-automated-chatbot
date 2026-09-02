"""Shared synthetic-candle builders for engine tests."""


def candle(ts, o, h, l, c, v=100):
    return [ts, o, h, l, c, v]


def flat_series(n, price=100.0, ts=0, step_ms=3_600_000, wiggle=0.05):
    """n candles oscillating tightly around `price` -- no clear trend."""
    out = []
    for i in range(n):
        p = price + (wiggle if i % 2 == 0 else -wiggle)
        out.append(candle(ts + i * step_ms, p, p + 0.1, p - 0.1, p))
    return out


def zigzag(swing_prices, steps=6, start_ts=0, step_ms=3_600_000):
    """Builds a candle sequence whose local extrema land exactly at each
    value in `swing_prices` (alternating low/high or high/low), by
    linearly interpolating `steps` candles between each consecutive pair.
    """
    prices = [swing_prices[0]]
    for a, b in zip(swing_prices, swing_prices[1:]):
        for s in range(1, steps + 1):
            prices.append(a + (b - a) * s / steps)
    out = []
    ts = start_ts
    for p in prices:
        out.append(candle(ts, p, p + 0.3, p - 0.3, p))
        ts += step_ms
    return out


BULLISH_SWINGS = [100, 130, 112, 145, 125, 158, 140, 170]
BEARISH_SWINGS = list(reversed(BULLISH_SWINGS))
