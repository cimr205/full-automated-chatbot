from core.trading.engine import market_structure as ms
from helpers import zigzag, flat_series, BULLISH_SWINGS, BEARISH_SWINGS


def test_bias_detects_bullish_trend():
    assert ms.bias(zigzag(BULLISH_SWINGS)) == "bullish"


def test_bias_detects_bearish_trend():
    assert ms.bias(zigzag(BEARISH_SWINGS)) == "bearish"


def test_bias_neutral_on_flat_range():
    assert ms.bias(flat_series(60)) == "neutral"


def test_bias_neutral_on_short_history():
    assert ms.bias(zigzag(BULLISH_SWINGS)[:10]) == "neutral"


def test_find_swings_detects_v_shape_low():
    # Monotonic down then up -> exactly one swing low at the bottom.
    down = [50 - i for i in range(6)]
    up = [50 - 5 + i for i in range(1, 6)]
    prices = down + up
    candles = [[i * 3_600_000, p, p + 0.2, p - 0.2, p, 100] for i, p in enumerate(prices)]
    swings = ms.find_swings(candles, left=2, right=2)
    lows = [s for s in swings if s["type"] == "low"]
    assert len(lows) == 1
    assert lows[0]["price"] == candles[prices.index(min(prices))][3]


def test_bos_continuation_in_uptrend():
    candles = zigzag(BULLISH_SWINGS)
    event = ms.detect_bos_choch(candles)
    assert event["event"] == "BOS"
    assert event["direction"] == "bullish"


def test_choch_reversal_breaks_uptrend():
    # Same uptrend, but the final leg plunges well below the last swing low
    # instead of making a new high -- a genuine change of character.
    candles = zigzag(BULLISH_SWINGS)
    truncated = candles[:-1]
    reversal_candle = [truncated[-1][0] + 3_600_000, 165, 165.3, 99.7, 100, 100]
    seq = truncated + [reversal_candle]

    event = ms.detect_bos_choch(seq)
    assert event["event"] == "CHOCH"
    assert event["direction"] == "bearish"
    assert event["prior_bias"] == "bullish"


def test_classify_structure_labels_hh_hl():
    swings = [
        {"index": 0, "type": "low", "price": 100},
        {"index": 1, "type": "high", "price": 110},
        {"index": 2, "type": "low", "price": 105},
        {"index": 3, "type": "high", "price": 120},
    ]
    classified = ms.classify_structure(swings)
    assert classified["high_labels"][-1][0] == "HH"
    assert classified["low_labels"][-1][0] == "HL"
    assert ms.structure_bias(classified) == "bullish"


def test_multi_timeframe_bias_defaults_missing_frames_to_neutral():
    out = ms.multi_timeframe_bias({"h4": zigzag(BULLISH_SWINGS), "h1": []})
    assert out["h4"] == "bullish"
    assert out["h1"] == "neutral"
