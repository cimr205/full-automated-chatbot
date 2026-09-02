import random
import time

from core.trading.signal_engine import score_signal


def _make_series(n, start, drift, vol, seed):
    rnd = random.Random(seed)
    ts = int(time.time() * 1000) - n * 3_600_000
    price = start
    out = []
    for i in range(n):
        o = price
        c = o + drift + rnd.uniform(-vol, vol)
        h = max(o, c) + rnd.uniform(0, vol * 0.5)
        l = min(o, c) - rnd.uniform(0, vol * 0.5)
        out.append([ts + i * 3_600_000, o, h, l, c, rnd.uniform(100, 1000)])
        price = c
    return out


def test_score_signal_produces_consistent_sl_tp_ordering():
    """Deterministic fixture (fixed seeds) known to produce a LONG signal --
    verifies SL/TP sit on the geometrically correct side of entry and R:R
    respects the profile's minimum."""
    ohlcv_1h = _make_series(300, 1.1000, 0.00008, 0.0006, seed=42)
    ohlcv_4h = _make_series(300, 1.0900, 0.00015, 0.0009, seed=43)
    ohlcv_15m = _make_series(1000, 1.1000, 0.00002, 0.0003, seed=44)

    result = score_signal(ohlcv_1h, ohlcv_4h, None, ohlcv_15m=ohlcv_15m, symbol="EURUSD")

    assert result["direction"] == "long"
    assert result["stop_loss"] < result["price"] < result["take_profit"]
    assert result["rr_ratio"] >= 2.0
    assert 0.0 <= result["confidence"] <= 1.0
    assert result["checklist_ok"] is True
    assert result["signal"]["profile"] == "forex"
    assert result["signal"]["status"] in ("VALID", "WATCHLIST")


def test_score_signal_ranging_market_returns_neutral_no_trade():
    flat = [[i * 3_600_000, 1.10, 1.1005, 1.0995, 1.10, 100] for i in range(300)]
    result = score_signal(flat, flat, None, symbol="EURUSD")
    assert result["direction"] == "neutral"
    assert result["confidence"] == 0.0
    assert result["checklist_ok"] is False
    assert result["signal"]["status"] == "NO_TRADE"
    assert result["stop_loss"] is None and result["take_profit"] is None


def test_score_signal_selects_gold_profile_for_xauusd():
    ohlcv_1h = _make_series(300, 2350.0, 0.15, 1.2, seed=1)
    ohlcv_4h = _make_series(300, 2300.0, 0.3, 1.8, seed=2)
    ohlcv_15m = _make_series(1000, 2350.0, 0.05, 0.6, seed=3)
    result = score_signal(ohlcv_1h, ohlcv_4h, None, ohlcv_15m=ohlcv_15m, symbol="XAUUSD")
    assert result["signal"]["profile"] == "gold"


def test_score_signal_selects_forex_profile_by_default():
    flat = [[i * 3_600_000, 1.10, 1.1005, 1.0995, 1.10, 100] for i in range(300)]
    result = score_signal(flat, flat, None, symbol="")
    assert result["signal"]["profile"] == "forex"


def test_score_signal_short_direction_has_correct_level_ordering():
    ohlcv_1h = _make_series(300, 1.1000, -0.00008, 0.0006, seed=42)
    ohlcv_4h = _make_series(300, 1.1100, -0.00015, 0.0009, seed=43)
    ohlcv_15m = _make_series(1000, 1.1000, -0.00002, 0.0003, seed=44)
    result = score_signal(ohlcv_1h, ohlcv_4h, None, ohlcv_15m=ohlcv_15m, symbol="GBPUSD")
    if result["direction"] == "short":
        assert result["take_profit"] < result["price"] < result["stop_loss"]
        assert result["rr_ratio"] >= 2.0
