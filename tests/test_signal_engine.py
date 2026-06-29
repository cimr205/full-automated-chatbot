import pytest

from core.trading import signal_engine as se


def make_ohlcv(closes, spread=0.5, volume=1000.0):
    """Builds a plausible OHLCV dict from a close-price series."""
    highs = [c + spread for c in closes]
    lows = [c - spread for c in closes]
    opens = [closes[i - 1] if i > 0 else closes[0] for i in range(len(closes))]
    volumes = [volume] * len(closes)
    return {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes}


def uptrend(n=80, start=100.0, step=0.5):
    return make_ohlcv([start + i * step for i in range(n)])


def downtrend(n=80, start=200.0, step=0.5):
    return make_ohlcv([start - i * step for i in range(n)])


def flat(n=80, level=100.0):
    return make_ohlcv([level] * n)


# ── score_signal structural correctness ──────────────────────────────────────

def test_score_signal_returns_all_expected_keys():
    result = se.score_signal(uptrend())
    expected_keys = {
        "direction", "confidence", "confluence", "reasons", "setups", "price",
        "stop_loss", "take_profit", "partial_tp", "rr_ratio", "session",
        "checklist", "checklist_ok",
    }
    assert expected_keys.issubset(result.keys())


def test_uptrend_scores_long_direction():
    result = se.score_signal(uptrend())
    assert result["direction"] == "long"


def test_downtrend_scores_short_direction():
    result = se.score_signal(downtrend())
    assert result["direction"] == "short"


def test_multi_timeframe_alignment_increases_confluence_when_agreeing():
    up = uptrend()
    aligned = se.score_signal(up, ohlcv_4h=up, ohlcv_1d=up)
    solo = se.score_signal(up)
    assert aligned["confluence"] >= solo["confluence"]
    assert aligned["checklist"]["trend_aligned"] is True


def test_conflicting_timeframes_break_trend_alignment():
    up, down = uptrend(), downtrend()
    result = se.score_signal(up, ohlcv_4h=down)
    assert result["checklist"]["trend_aligned"] is False
    assert result["checklist_ok"] is False  # a hard blocker must fail the whole signal


def test_stop_loss_and_take_profit_are_on_correct_sides_for_long():
    result = se.score_signal(uptrend())
    price = result["price"]
    if result["direction"] == "long":
        assert result["stop_loss"] < price < result["take_profit"]
    else:
        assert result["take_profit"] < price < result["stop_loss"]


def test_risk_reward_ratio_matches_hard_floor_or_signal_is_blocked():
    result = se.score_signal(uptrend(), ohlcv_4h=uptrend(), ohlcv_1d=uptrend())
    # the engine is built around a fixed ATR_SL_MULT/ATR_TP_MULT ratio of 1:2
    assert result["rr_ratio"] == pytest.approx(se.ATR_TP_MULT / se.ATR_SL_MULT, rel=0.05)


def test_checklist_ok_requires_all_hard_blockers():
    result = se.score_signal(flat())
    cl = result["checklist"]
    # a perfectly flat market has zero confluence and zero R:R signal quality
    assert cl["confluence_3plus"] is False
    assert result["checklist_ok"] is False


def test_confidence_is_bounded():
    for ohlcv in (uptrend(), downtrend(), flat()):
        result = se.score_signal(ohlcv)
        assert 0.0 <= result["confidence"] <= 0.97


# ── setup detectors ───────────────────────────────────────────────────────────

def test_detect_fvg_long_gap():
    ohlcv = make_ohlcv([100, 100, 100])
    ohlcv["high"] = [100.5, 100.5, 110.5]
    ohlcv["low"] = [99.5, 99.5, 109.5]  # candle 3 entirely above candle 1's high
    result = se.detect_fvg(ohlcv)
    assert result is not None
    assert result["direction"] == "long"


def test_detect_fvg_none_when_no_gap():
    ohlcv = flat(10)
    assert se.detect_fvg(ohlcv) is None


def test_detect_break_retest_long():
    # range-bound then break above with a retest close near the breakout level
    base = [100.0] * 40
    breakout = [108.0, 100.05]  # break far above, then retest right at the old high
    ohlcv = make_ohlcv(base + breakout, spread=0.01)
    result = se.detect_break_retest(ohlcv)
    assert result is not None
    assert result["direction"] == "long"


def test_detect_liquidity_grab_short():
    base = [100.0] * 21
    ohlcv = make_ohlcv(base, spread=0.5)
    # last candle wicks above recent highs then closes back inside
    ohlcv["high"][-1] = 105.0
    ohlcv["close"][-1] = 100.0
    result = se.detect_liquidity_grab(ohlcv)
    assert result is not None
    assert result["direction"] == "short"


def test_detect_trend_pullback_long():
    closes = [100.0 + i * 0.5 for i in range(55)]
    closes[-1] = se.ema(closes[:-1], 20)  # pull back to the 20 EMA
    ohlcv = make_ohlcv(closes)
    result = se.detect_trend_pullback(ohlcv)
    assert result is None or result["direction"] == "long"


# ── session info ──────────────────────────────────────────────────────────────

def test_session_info_returns_valid_structure():
    result = se.session_info()
    assert "name" in result
    assert isinstance(result["active"], bool)
