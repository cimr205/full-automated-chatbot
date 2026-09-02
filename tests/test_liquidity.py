from core.trading.engine import liquidity as liq


def test_wick_only_penetration_is_not_a_confirmed_sweep():
    """A candle that wicks below a level but does NOT close back above it
    must not count as a confirmed sweep -- it's still an open test of the
    level (spec: sweep != wick)."""
    candles = [
        [0, 101, 102, 100.5, 101.5, 100],
        [3_600_000, 101.5, 102, 101, 101.8, 100],
        [7_200_000, 101.8, 102, 95, 96, 100],   # wicks to 95, closes at 96 -- still below level
    ]
    assert liq.detect_sweep(candles, level=100, direction="sell_side", lookback=3) is None


def test_sweep_confirmed_when_candle_closes_back_beyond_level():
    candles = [
        [0, 101, 102, 100.5, 101.5, 100],
        [3_600_000, 101.5, 102, 101, 101.8, 100],
        [7_200_000, 101.8, 102, 95, 96, 100],
        [10_800_000, 96, 103, 95.5, 102, 100],   # closes back above the level
    ]
    result = liq.detect_sweep(candles, level=100, direction="sell_side", lookback=3)
    assert result is not None
    assert result["liquidity_event"] is True
    assert result["type"] == "sell_side_sweep"
    assert 0 < result["quality"] <= 1.0
    assert result["swept_index"] == 2


def test_no_sweep_when_level_never_penetrated():
    candles = [
        [0, 101, 102, 100.5, 101.5, 100],
        [3_600_000, 101.5, 102, 101, 101.8, 100],
        [7_200_000, 101.8, 102.5, 101.2, 102, 100],
    ]
    assert liq.detect_sweep(candles, level=100, direction="sell_side", lookback=3) is None


def test_buy_side_sweep_symmetry():
    candles = [
        [0, 99, 99.5, 98.5, 99.2, 100],
        [3_600_000, 99.2, 99.6, 98.8, 99.4, 100],
        [7_200_000, 99.4, 105, 99.2, 104, 100],   # spikes above 100
        [10_800_000, 104, 104.5, 98.9, 99, 100],  # closes back below the level
    ]
    result = liq.detect_sweep(candles, level=100, direction="buy_side", lookback=3)
    assert result is not None
    assert result["type"] == "buy_side_sweep"


def test_find_best_sweep_picks_highest_quality():
    candles = [
        [0, 101.2, 101.5, 101, 101.3, 100],
        [3_600_000, 101, 102, 100.5, 101.5, 100],
        [7_200_000, 101.5, 102, 101, 101.8, 100],
        [10_800_000, 101.8, 102, 95, 102, 100],   # deep wick, strong full-range rejection back to 102
    ]
    best = liq.find_best_sweep(candles, [("pdl", 100), ("asian_low", 97)], "sell_side", lookback=3)
    assert best is not None
    assert best["level_name"] in ("pdl", "asian_low")


def test_equal_levels_requires_at_least_two_touches():
    swings = [
        {"type": "high", "price": 110.00},
        {"type": "high", "price": 110.03},   # within tolerance of the one above
        {"type": "high", "price": 130.00},   # isolated, no cluster partner
        {"type": "low", "price": 90.00},
    ]
    out = liq.equal_levels(swings, tolerance_pct=0.05)
    assert len(out["equal_highs"]) == 1
    assert out["equal_highs"][0]["touches"] == 2
    assert out["equal_lows"] == []


def test_previous_day_levels_uses_most_recent_completed_day():
    import datetime as dt
    day1 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    day2 = dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)
    candles = [
        [int(day1.timestamp() * 1000), 100, 105, 95, 102, 100],
        [int((day1 + dt.timedelta(hours=12)).timestamp() * 1000), 102, 108, 101, 103, 100],
        [int(day2.timestamp() * 1000), 103, 104, 102, 103.5, 100],
    ]
    at = day2 + dt.timedelta(hours=6)
    levels = liq.previous_day_levels(candles, at=at)
    assert levels["pdh"] == 108
    assert levels["pdl"] == 95
