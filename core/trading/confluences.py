from __future__ import annotations

"""
Gold-specific confluence checkers.

Each function returns (confirmed: bool, label: str, weight: float).
All weights are added to the base confidence score when confirmed.
A trade only gets stronger — no factor can reduce confidence.

The 7 factors (max combined boost ≈ +0.31):
  1. bos_15m          Break of structure on 15m after the Asian sweep
  2. sweep_volume     Institutional volume spike on the sweep candle
  3. rsi_divergence   Bullish/bearish RSI divergence at the sweep point
  4. prev_day_level   Swept previous day's high or low (double liquidity)
  5. round_number     Asian extreme near a key $50 round number
  6. fvg_1h           1H fair value gap aligned with trade direction
  7. htf_trend        4H trend (EMA50/200) agrees with the trade direction
"""
from .indicators import ema, rsi as _rsi, volume_ratio
from .signal_engine import detect_fvg


def check_all(
    direction: str,              # "long" or "short"
    ohlcv_15m: list,
    ohlcv_1h:  list,
    ohlcv_4h:  list | None,
    asian_sweep: dict | None,    # result from asian_range.detect()
) -> tuple[float, list[str]]:
    """
    Run all 7 gold confluence checks. Returns:
      (total_boost, confirmed_labels)

    Plug `total_boost` into confidence = min(0.97, base_confidence + total_boost).
    """
    boost  = 0.0
    labels = []

    checks = [
        _bos_15m,
        _sweep_volume,
        _rsi_divergence,
        _prev_day_level,
        _round_number,
        _fvg_1h,
        _htf_trend,
    ]
    args = (direction, ohlcv_15m, ohlcv_1h, ohlcv_4h, asian_sweep)

    for fn in checks:
        try:
            ok, label, weight = fn(*args)
            if ok:
                boost  += weight
                labels.append(label)
        except Exception:
            pass

    return round(boost, 3), labels


# ── 1. Break of Structure on 15m ─────────────────────────────────────────────

def _bos_15m(direction, ohlcv_15m, ohlcv_1h, ohlcv_4h, asian_sweep):
    """
    After a bullish Asian sweep, did price break above a recent 15m swing high?
    That means the reversal already has momentum — we're not early, we're confirmed.
    """
    if not ohlcv_15m or len(ohlcv_15m) < 12:
        return False, "", 0.06

    recent = ohlcv_15m[-12:]
    current_close = ohlcv_15m[-1][4]

    # Find recent swing high/low in the last 10 candles (exclude last 2)
    swing_highs = [c[2] for c in recent[:-2]]
    swing_lows  = [c[3] for c in recent[:-2]]

    if direction == "long":
        recent_swing_high = max(swing_highs)
        if current_close > recent_swing_high:
            return True, f"✅ BOS 15m: pris brød over swing high {recent_swing_high:.2f}", 0.06
    else:
        recent_swing_low = min(swing_lows)
        if current_close < recent_swing_low:
            return True, f"✅ BOS 15m: pris brød under swing low {recent_swing_low:.2f}", 0.06

    return False, "", 0.06


# ── 2. Volume spike at the sweep candle ──────────────────────────────────────

def _sweep_volume(direction, ohlcv_15m, ohlcv_1h, ohlcv_4h, asian_sweep):
    """
    Institutional players leave a volume fingerprint when they execute the sweep.
    If the sweep candle had >1.5x normal volume, they were behind it.
    """
    if not ohlcv_15m or len(ohlcv_15m) < 5:
        return False, "", 0.05

    vols = [c[5] for c in ohlcv_15m if c[5] > 0]
    if len(vols) < 5:
        return False, "", 0.05

    avg_vol = sum(vols[-20:]) / min(20, len(vols[-20:]))
    if avg_vol == 0:
        return False, "", 0.05

    # Check the sweep candle (likely 1-3 candles ago — during London open)
    recent_vols = [c[5] for c in ohlcv_15m[-4:-1]]
    peak_vol = max(recent_vols) if recent_vols else 0

    ratio = peak_vol / avg_vol if avg_vol > 0 else 0
    if ratio >= 1.5:
        return True, f"✅ Institutionel volumen: {ratio:.1f}x normalt på sweep", 0.05

    return False, "", 0.05


# ── 3. RSI divergence at sweep point ─────────────────────────────────────────

def _rsi_divergence(direction, ohlcv_15m, ohlcv_1h, ohlcv_4h, asian_sweep):
    """
    Bullish divergence: price makes a lower low but RSI makes a higher low.
    Means sellers are exhausted — buyers stepping in at the sweep.
    """
    if not ohlcv_15m or len(ohlcv_15m) < 30:
        return False, "", 0.04

    closes = [c[4] for c in ohlcv_15m]
    lows   = [c[3] for c in ohlcv_15m]
    highs  = [c[2] for c in ohlcv_15m]

    # Compare RSI at last two swing points
    mid     = len(closes) // 2
    rsi_now = _rsi(closes[-14:])
    rsi_mid = _rsi(closes[mid - 7:mid + 7])

    if direction == "long":
        price_now = min(lows[-10:])
        price_mid = min(lows[mid - 5:mid + 5])
        # Bullish div: lower price low, higher RSI low
        if price_now < price_mid and rsi_now > rsi_mid + 3:
            return True, f"✅ RSI bullish divergens: lavere low, højere RSI ({rsi_now:.0f} vs {rsi_mid:.0f})", 0.04
    else:
        price_now = max(highs[-10:])
        price_mid = max(highs[mid - 5:mid + 5])
        # Bearish div: higher price high, lower RSI high
        if price_now > price_mid and rsi_now < rsi_mid - 3:
            return True, f"✅ RSI bearish divergens: højere high, lavere RSI ({rsi_now:.0f} vs {rsi_mid:.0f})", 0.04

    return False, "", 0.04


# ── 4. Previous Day High/Low swept ───────────────────────────────────────────

def _prev_day_level(direction, ohlcv_15m, ohlcv_1h, ohlcv_4h, asian_sweep):
    """
    If London also swept the Previous Day's Low (for bullish) or High (for bearish),
    that's double liquidity — extra stops were resting there, making the reversal
    stronger and the move after it larger.
    """
    if not ohlcv_1h or len(ohlcv_1h) < 48:
        return False, "", 0.05

    # Previous day = 24 candles ago on 1h data
    prev_day = ohlcv_1h[-48:-24]
    if not prev_day:
        return False, "", 0.05

    pdh = max(c[2] for c in prev_day)   # Previous Day High
    pdl = min(c[3] for c in prev_day)   # Previous Day Low

    if not asian_sweep:
        return False, "", 0.05

    tol = 0.003   # 0.3% — gold moves fast

    if direction == "long":
        sweep_low = asian_sweep.get("sweep_low", asian_sweep.get("asian_low", 0))
        if sweep_low > 0 and abs(sweep_low - pdl) / pdl < tol:
            return True, f"✅ Previous Day Low swept ({pdl:.2f}) — dobbelt likviditet", 0.05
    else:
        sweep_high = asian_sweep.get("sweep_high", asian_sweep.get("asian_high", 0))
        if sweep_high > 0 and abs(sweep_high - pdh) / pdh < tol:
            return True, f"✅ Previous Day High swept ({pdh:.2f}) — dobbelt likviditet", 0.05

    return False, "", 0.05


# ── 5. Round number ($50 increments) ─────────────────────────────────────────

def _round_number(direction, ohlcv_15m, ohlcv_1h, ohlcv_4h, asian_sweep):
    """
    Gold traders place stops at round numbers ($2300, $2350, $2400 etc.).
    When the Asian range extreme is near one of these, there are MORE stops there,
    so the sweep is larger and the reversal more violent.
    """
    if not asian_sweep:
        return False, "", 0.04

    ROUND_STEP   = 50.0   # $50 increments
    PROXIMITY    = 4.0    # within $4 of a round number

    if direction == "long":
        level = asian_sweep.get("sweep_low", asian_sweep.get("asian_low", 0))
    else:
        level = asian_sweep.get("sweep_high", asian_sweep.get("asian_high", 0))

    if level <= 0:
        return False, "", 0.04

    nearest_round = round(level / ROUND_STEP) * ROUND_STEP
    if abs(level - nearest_round) <= PROXIMITY:
        return True, f"✅ Rundt tal: sweep nær ${nearest_round:.0f} (psykologisk niveau)", 0.04

    return False, "", 0.04


# ── 6. 1H Fair Value Gap aligned ─────────────────────────────────────────────

def _fvg_1h(direction, ohlcv_15m, ohlcv_1h, ohlcv_4h, asian_sweep):
    """
    An unfilled 1H FVG in our trade direction means price is likely to move
    there to fill it — adds a structural target and confluence.
    """
    if not ohlcv_1h:
        return False, "", 0.04

    fvg = detect_fvg(ohlcv_1h)
    if not fvg:
        return False, "", 0.04

    fvg_type = fvg.get("type", "")
    expected = "bullish" if direction == "long" else "bearish"

    if expected in fvg_type:
        return True, f"✅ 1H FVG i tradingsretning: {fvg.get('label', fvg_type)}", 0.04

    return False, "", 0.04


# ── 7. Higher timeframe trend (4H EMA50/200) ─────────────────────────────────

def _htf_trend(direction, ohlcv_15m, ohlcv_1h, ohlcv_4h, asian_sweep):
    """
    Trading WITH the 4H trend gives the highest probability of a large move.
    If the 4H trend disagrees, the Asian sweep reversal might be a fakeout of a fakeout.
    """
    candles = ohlcv_4h if (ohlcv_4h and len(ohlcv_4h) >= 50) else ohlcv_1h
    if not candles or len(candles) < 50:
        return False, "", 0.03

    closes = [c[4] for c in candles]
    e50    = ema(closes, 50)
    e200   = ema(closes, min(200, len(closes)))

    if direction == "long" and e50 > e200:
        return True, f"✅ 4H uptrend: EMA50 ({e50:.2f}) over EMA200 ({e200:.2f})", 0.03
    if direction == "short" and e50 < e200:
        return True, f"✅ 4H downtrend: EMA50 ({e50:.2f}) under EMA200 ({e200:.2f})", 0.03

    return False, "", 0.03
