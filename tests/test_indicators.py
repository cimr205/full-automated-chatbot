import numpy as np
import pytest

from core.trading.indicators import rsi, ema, macd, bollinger, atr, volume_ratio, stochastic


def test_rsi_all_gains_is_100():
    closes = [float(i) for i in range(1, 30)]  # strictly increasing
    assert rsi(closes) == 100.0


def test_rsi_all_losses_is_0():
    closes = [float(i) for i in range(30, 1, -1)]  # strictly decreasing
    assert rsi(closes) == 0.0


def test_rsi_insufficient_data_returns_neutral():
    assert rsi([1.0, 2.0, 3.0], period=14) == 50.0


def test_rsi_bounded_0_100():
    rng = np.random.default_rng(42)
    closes = list(100 + np.cumsum(rng.normal(0, 1, 100)))
    val = rsi(closes)
    assert 0.0 <= val <= 100.0


def test_ema_short_series_returns_last_value():
    assert ema([5.0, 6.0, 7.0], period=20) == 7.0


def test_ema_tracks_constant_series():
    closes = [10.0] * 30
    assert ema(closes, 10) == pytest.approx(10.0)


def test_ema_responds_to_trend():
    closes = [float(i) for i in range(1, 60)]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    # in a strict uptrend the faster EMA must sit above the slower EMA
    assert e20 > e50


def test_macd_insufficient_data_is_zero():
    assert macd([1.0, 2.0, 3.0]) == (0.0, 0.0, 0.0)


def test_macd_uptrend_is_bullish():
    closes = [float(i) for i in range(1, 60)]
    macd_line, signal_line, hist = macd(closes)
    assert macd_line > 0
    assert hist == pytest.approx(macd_line - signal_line)


def test_bollinger_bands_ordering():
    rng = np.random.default_rng(1)
    closes = list(100 + rng.normal(0, 2, 50))
    low, mid, high = bollinger(closes)
    assert low < mid < high


def test_bollinger_zero_std_collapses_bands():
    closes = [50.0] * 25
    low, mid, high = bollinger(closes)
    assert low == mid == high == 50.0


def test_atr_flat_market_is_zero():
    n = 20
    highs = lows = closes = [100.0] * n
    assert atr(highs, lows, closes) == 0.0


def test_atr_positive_for_volatile_market():
    highs  = [101.0, 102.0, 103.0, 104.0]
    lows   = [99.0, 100.0, 101.0, 102.0]
    closes = [100.0, 101.0, 102.0, 103.0]
    assert atr(highs, lows, closes) > 0


def test_volume_ratio_spike_detected():
    volumes = [100.0] * 19 + [500.0]
    assert volume_ratio(volumes) == pytest.approx(5.0)


def test_volume_ratio_no_data_defaults_to_1():
    assert volume_ratio([]) == 1.0


def test_stochastic_at_range_high_is_100():
    highs = [10.0, 11.0, 12.0]
    lows  = [8.0, 9.0, 10.0]
    closes = [8.0, 9.0, 12.0]
    assert stochastic(highs, lows, closes, k_period=3) == pytest.approx(100.0)


def test_stochastic_at_range_low_is_0():
    highs = [10.0, 11.0, 12.0]
    lows  = [8.0, 9.0, 10.0]
    closes = [8.0, 9.0, 8.0]
    assert stochastic(highs, lows, closes, k_period=3) == pytest.approx(0.0)


def test_stochastic_flat_range_returns_neutral():
    highs = lows = closes = [10.0, 10.0, 10.0]
    assert stochastic(highs, lows, closes, k_period=3) == 50.0
