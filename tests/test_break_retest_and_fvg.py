from core.trading.engine import break_retest, fvg as fvg_engine


def _build_break_fixture(strong_momentum: bool):
    """35 candles ramping up to a clean swing high at 150.5, a pullback,
    then either a genuine close-through breakout with a retest (strong_momentum
    =True) or a wick-only "breakout" that closes back below the level
    (strong_momentum=False, the fake-breakout case)."""
    ts, step = 0, 3_600_000
    candles = []
    for i in range(37):
        p = 60 + i * 2.2
        candles.append([ts, p, p + 0.5, p - 0.5, p, 100])
        ts += step
    candles.append([ts, 141, 150.5, 140.5, 150, 100])   # swing high candle
    ts += step
    for p in [145, 138, 130, 125, 120]:
        candles.append([ts, p + 5, p + 1, p - 1, p, 100])
        ts += step
    if strong_momentum:
        candles.append([ts, 120, 156, 119.5, 155, 100])          # genuine close-through break
    else:
        candles.append([ts, 120, 156, 119.5, 121, 100])          # wick above 150.5, closes back below
    ts += step
    candles.append([ts, 155 if strong_momentum else 121, 155.5, 149.8, 150.2, 100])   # retest
    return candles


def test_wick_only_break_is_rejected_as_fake_breakout():
    fixture = _build_break_fixture(strong_momentum=False)
    assert break_retest.detect(fixture) is None


def test_genuine_close_through_break_with_retest_is_detected():
    fixture = _build_break_fixture(strong_momentum=True)
    result = break_retest.detect(fixture)
    assert result is not None
    assert result["type"] == "bullish_break_retest"
    assert result["direction"] == "long"
    assert result["level"] == 150.5


def _fvg_fixture():
    return [
        [0, 99, 99.2, 98.8, 99, 100],
        [3_600_000, 99, 99.3, 98.9, 99.1, 100],
        [7_200_000, 100, 101, 99.5, 100.8, 100],       # c1: high = 101
        [10_800_000, 100.8, 102, 100.7, 101.8, 100],   # c2: strong displacement candle
        [14_400_000, 103, 104, 103, 103.5, 100],       # c3: low = 103 -> gap 101-103
    ]


def test_find_fvgs_detects_bullish_displacement_gap():
    gaps = fvg_engine.find_fvgs(_fvg_fixture())
    gap = next(g for g in gaps if g["bottom"] == 101 and g["top"] == 103)
    assert gap["type"] == "bullish_fvg"
    assert gap["displacement"] is True


def test_mitigation_status_tracks_fill_percentage():
    base = _fvg_fixture()
    gap = next(g for g in fvg_engine.find_fvgs(base) if g["bottom"] == 101 and g["top"] == 103)

    unfilled = fvg_engine.mitigation_status(gap, base)
    assert unfilled["filled_pct"] == 0.0
    assert unfilled["mitigated"] is False

    half_filled = base + [[18_000_000, 103.5, 103.6, 102.0, 102.5, 100]]   # wicks to the gap midpoint (101-103)
    half = fvg_engine.mitigation_status(gap, half_filled)
    assert 0.4 < half["filled_pct"] < 0.6

    fully_filled = base + [[18_000_000, 103.5, 103.6, 100.5, 101, 100]]  # trades all the way through
    full = fvg_engine.mitigation_status(gap, fully_filled)
    assert full["mitigated"] is True


def test_score_fvg_quality_rewards_sweep_and_htf_alignment():
    gap = {"type": "bullish_fvg", "direction": "long", "displacement": True}
    low_quality = fvg_engine.score_fvg_quality(gap, followed_sweep=False, htf_direction="short", filled_pct=0.8)
    high_quality = fvg_engine.score_fvg_quality(gap, followed_sweep=True, htf_direction="long", filled_pct=0.0)
    assert high_quality > low_quality
    assert 0.0 <= low_quality <= 1.0
    assert 0.0 <= high_quality <= 1.0
