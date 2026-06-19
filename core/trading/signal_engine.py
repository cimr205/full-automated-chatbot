"""
Signal engine — the trading rulebook.

Scores a symbol's OHLCV data against a strict, multi-factor confluence
model and only returns a tradeable signal when every hard rule passes:

  1. Multi-timeframe trend alignment (1h confirms 4h confirms 1d bias)
  2. Minimum 3-factor confluence (indicators + price-action setup)
  3. Minimum 1:2 risk/reward (stop distance vs take-profit distance)
  4. Inside an active trading session (London / NY / overlap)

Nothing here guarantees winning trades — markets are probabilistic.
This engine's job is to reject everything that doesn't meet the bar,
so only the highest-quality setups reach execution.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from .indicators import rsi, ema, macd, bollinger, atr, volume_ratio, stochastic

CET = ZoneInfo("Europe/Paris")

MIN_CONFLUENCE  = 3      # minimum number of agreeing factors
MIN_CONFIDENCE  = 0.55   # floor below which we never publish
ATR_SL_MULT     = 1.5
ATR_TP_MULT     = 3.0
PARTIAL_R       = 1.5    # take partial profit at 1.5R
MIN_RR          = 2.0    # hard floor: reward must be >= 2x risk


# ── Sessions ──────────────────────────────────────────────────────────────────

def session_info() -> dict:
    """Returns the current trading session in CET, and whether it's tradeable."""
    now = datetime.now(CET)
    h = now.hour + now.minute / 60
    weekday = now.weekday()  # 0=Mon .. 6=Sun

    if weekday >= 5:
        return {"name": "Weekend", "active": False}

    # London: 09:00-17:30 CET · NY: 14:30-23:00 CET · Overlap: 14:30-17:30 CET
    if 14.5 <= h < 17.5:
        return {"name": "London/NY Overlap", "active": True, "quality": "high"}
    if 9.0 <= h < 11.0:
        return {"name": "London Open", "active": True, "quality": "high"}
    if 11.0 <= h < 14.5:
        return {"name": "London", "active": True, "quality": "medium"}
    if 17.5 <= h < 23.0:
        return {"name": "New York", "active": True, "quality": "medium"}
    return {"name": "Asia/Off-hours", "active": False, "quality": "low"}


# ── Setup detection (price action) ───────────────────────────────────────────

def detect_fvg(ohlcv: dict) -> dict | None:
    """Fair Value Gap: 3-candle imbalance pattern."""
    highs, lows = ohlcv["high"], ohlcv["low"]
    if len(highs) < 3:
        return None
    h0, l0 = highs[-3], lows[-3]
    h2, l2 = highs[-1], lows[-1]
    if l2 > h0:
        return {"setup": "FVG", "direction": "long", "gap_low": h0, "gap_high": l2}
    if h2 < l0:
        return {"setup": "FVG", "direction": "short", "gap_low": h2, "gap_high": l0}
    return None


def detect_break_retest(ohlcv: dict, lookback: int = 40) -> dict | None:
    """Break of recent range high/low followed by a retest close."""
    highs, lows, closes = ohlcv["high"], ohlcv["low"], ohlcv["close"]
    if len(closes) < lookback + 2:
        return None
    window_high = max(highs[-lookback - 2:-2])
    window_low  = min(lows[-lookback - 2:-2])
    prev_close, last_close = closes[-2], closes[-1]

    if prev_close > window_high and abs(last_close - window_high) / window_high < 0.002:
        return {"setup": "Break & Retest", "direction": "long", "level": window_high}
    if prev_close < window_low and abs(last_close - window_low) / window_low < 0.002:
        return {"setup": "Break & Retest", "direction": "short", "level": window_low}
    return None


def detect_liquidity_grab(ohlcv: dict, lookback: int = 20) -> dict | None:
    """Wick sweep beyond recent extreme, then close back inside the range."""
    highs, lows, closes = ohlcv["high"], ohlcv["low"], ohlcv["close"]
    if len(closes) < lookback + 1:
        return None
    recent_high = max(highs[-lookback - 1:-1])
    recent_low  = min(lows[-lookback - 1:-1])
    h, l, c = highs[-1], lows[-1], closes[-1]

    if h > recent_high and c < recent_high:
        return {"setup": "Liquidity Grab", "direction": "short", "level": recent_high}
    if l < recent_low and c > recent_low:
        return {"setup": "Liquidity Grab", "direction": "long", "level": recent_low}
    return None


def detect_trend_pullback(ohlcv: dict) -> dict | None:
    """Price pulls back to the 20 EMA within an established trend."""
    closes = ohlcv["close"]
    if len(closes) < 50:
        return None
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    last = closes[-1]
    dist_pct = abs(last - ema20) / ema20

    if ema20 > ema50 and dist_pct < 0.003 and last > ema50:
        return {"setup": "Trend Pullback", "direction": "long", "ema20": ema20}
    if ema20 < ema50 and dist_pct < 0.003 and last < ema50:
        return {"setup": "Trend Pullback", "direction": "short", "ema20": ema20}
    return None


SETUP_DETECTORS = [detect_fvg, detect_break_retest, detect_liquidity_grab, detect_trend_pullback]


# ── Indicator scoring per timeframe ──────────────────────────────────────────

def _score_timeframe(ohlcv: dict) -> dict:
    closes, highs, lows = ohlcv["close"], ohlcv["high"], ohlcv["low"]
    volumes = ohlcv.get("volume", [0] * len(closes))

    r = rsi(closes)
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, min(200, len(closes) - 1) or 1)
    macd_line, signal_line, hist = macd(closes)
    bb_low, bb_mid, bb_high = bollinger(closes)
    a = atr(highs, lows, closes)
    vr = volume_ratio(volumes)
    stoch = stochastic(highs, lows, closes)
    last = closes[-1]

    long_score, short_score, reasons = 0.0, 0.0, []

    if e20 > e50:
        long_score += 1.5; reasons.append("EMA20>EMA50 (opadgående trend)")
    elif e20 < e50:
        short_score += 1.5; reasons.append("EMA20<EMA50 (nedadgående trend)")

    if 30 < r < 70:
        pass
    elif r <= 30:
        long_score += 1.0; reasons.append(f"RSI oversold ({r:.0f})")
    elif r >= 70:
        short_score += 1.0; reasons.append(f"RSI overbought ({r:.0f})")

    if hist > 0 and macd_line > signal_line:
        long_score += 1.0; reasons.append("MACD bullish crossover")
    elif hist < 0 and macd_line < signal_line:
        short_score += 1.0; reasons.append("MACD bearish crossover")

    if last < bb_low:
        long_score += 0.8; reasons.append("Pris under Bollinger lower band")
    elif last > bb_high:
        short_score += 0.8; reasons.append("Pris over Bollinger upper band")

    if stoch < 20:
        long_score += 0.7; reasons.append(f"Stochastic oversold ({stoch:.0f})")
    elif stoch > 80:
        short_score += 0.7; reasons.append(f"Stochastic overbought ({stoch:.0f})")

    if vr > 1.5:
        reasons.append(f"Volumen {vr:.1f}x normal")
        long_score *= 1.1
        short_score *= 1.1

    direction = "long" if long_score > short_score else "short"
    score = max(long_score, short_score)
    return {"direction": direction, "score": score, "reasons": reasons, "atr": a, "price": last}


# ── Main scoring entrypoint ───────────────────────────────────────────────────

def score_signal(ohlcv_1h: dict, ohlcv_4h: dict | None = None, ohlcv_1d: dict | None = None) -> dict:
    tf1h = _score_timeframe(ohlcv_1h)
    tf4h = _score_timeframe(ohlcv_4h) if ohlcv_4h else None
    tf1d = _score_timeframe(ohlcv_1d) if ohlcv_1d else None

    direction = tf1h["direction"]
    confluence = 0
    reasons = list(tf1h["reasons"])

    trend_aligned = True
    if tf4h:
        if tf4h["direction"] == direction:
            confluence += 1
            reasons.append(f"4h bekræfter {direction} bias")
        else:
            trend_aligned = False
    if tf1d:
        if tf1d["direction"] == direction:
            confluence += 1
            reasons.append(f"1d bekræfter {direction} bias")
        else:
            trend_aligned = False

    confluence += min(len(tf1h["reasons"]), 4)

    setups = []
    for detector in SETUP_DETECTORS:
        result = detector(ohlcv_1h)
        if result and result["direction"] == direction:
            setups.append(result)
            confluence += 1

    price = tf1h["price"]
    a = tf1h["atr"] or (price * 0.001)

    if direction == "long":
        stop_loss   = price - a * ATR_SL_MULT
        take_profit = price + a * ATR_TP_MULT
        partial_tp  = price + a * ATR_SL_MULT * PARTIAL_R
    else:
        stop_loss   = price + a * ATR_SL_MULT
        take_profit = price - a * ATR_TP_MULT
        partial_tp  = price - a * ATR_SL_MULT * PARTIAL_R

    risk   = abs(price - stop_loss)
    reward = abs(take_profit - price)
    rr_ratio = round(reward / risk, 2) if risk else 0.0

    confidence = min(0.5 + confluence * 0.07, 0.97)

    sess = session_info()

    checklist = {
        "trend_aligned":    trend_aligned,
        "confluence_3plus": confluence >= MIN_CONFLUENCE,
        "rr_min_1_2":       rr_ratio >= MIN_RR,
        "active_session":   sess["active"],
        "setup_detected":   len(setups) > 0,
        "volume_confirmed": "Volumen" in " ".join(reasons),
        "no_major_news":    True,   # placeholder — news filter not yet wired up
        "risk_acceptable":  risk > 0,
    }
    checklist_ok = all([
        checklist["trend_aligned"],
        checklist["confluence_3plus"],
        checklist["rr_min_1_2"],
        checklist["active_session"],
    ])

    return {
        "direction":    direction,
        "confidence":   round(confidence, 2),
        "confluence":   confluence,
        "reasons":      reasons,
        "setups":       [s["setup"] for s in setups],
        "price":        price,
        "stop_loss":    round(stop_loss, 5),
        "take_profit":  round(take_profit, 5),
        "partial_tp":   round(partial_tp, 5),
        "rr_ratio":     rr_ratio,
        "session":      sess["name"],
        "checklist":    checklist,
        "checklist_ok": checklist_ok,
    }
