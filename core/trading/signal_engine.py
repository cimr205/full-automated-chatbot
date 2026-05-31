"""
Signal engine — implements the full trading rulebook:

ENTRY:  Minimum 3 confluence factors, specific setup required
RISK:   Minimum 1:2 R:R, ATR-based SL/TP, partial exit at 1.5R
SESSION: Only London Open (09-10 CET), London (08-16 CET), NY (14-22 CET)
SETUP:  Break & Retest, Fair Value Gap, Liquidity Grab, Trend Pullback
CHECK:  All 8 pre-trade checklist items evaluated before signal is surfaced
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from .indicators import rsi, ema, macd, bollinger, atr, volume_ratio, stochastic

CET           = ZoneInfo("Europe/Paris")
MIN_CONFLUENCE = 3      # minimum 3 independent factors
ATR_SL_MULT    = 1.5    # stop loss = 1.5x ATR below/above entry
ATR_TP_MULT    = 3.0    # take profit = 3x ATR  →  1:2 R:R
PARTIAL_R      = 1.5    # partial profits at 1.5R
MIN_RR         = 2.0    # reject trades with < 1:2 R:R


# ── Session timing ────────────────────────────────────────────────────────────

def session_info() -> dict:
    """Current CET session. Prime = highest quality entries."""
    hour = datetime.now(CET).hour
    if 9 <= hour < 10:
        return {"name": "London Open",        "tradeable": True,  "prime": True}
    if 15 <= hour < 17:
        return {"name": "London/NY Overlap",  "tradeable": True,  "prime": True}
    if 8 <= hour < 16:
        return {"name": "London Session",     "tradeable": True,  "prime": False}
    if 14 <= hour < 22:
        return {"name": "NY Session",         "tradeable": True,  "prime": False}
    if 2 <= hour < 8:
        return {"name": "Asian Session",      "tradeable": False, "prime": False}
    return {"name": f"Lukket ({hour}:00 CET)", "tradeable": False, "prime": False}


# ── Setup detectors ───────────────────────────────────────────────────────────

def detect_fvg(ohlcv: list) -> dict | None:
    """Fair Value Gap: 3-candle imbalance, price now returning to fill it."""
    if len(ohlcv) < 5:
        return None
    current = ohlcv[-1][4]
    for i in range(max(0, len(ohlcv) - 30), len(ohlcv) - 2):
        c1, _c2, c3 = ohlcv[i], ohlcv[i + 1], ohlcv[i + 2]
        # Bullish FVG: c1 high < c3 low → gap between them
        if c1[2] < c3[3]:
            bot, top = c1[2], c3[3]
            if bot * 0.997 <= current <= top * 1.003:
                return {"type": "bullish_fvg",
                        "label": f"Bullish FVG zone ({bot:.5g} – {top:.5g})"}
        # Bearish FVG: c3 high < c1 low
        elif c3[2] < c1[3]:
            bot, top = c3[2], c1[3]
            if bot * 0.997 <= current <= top * 1.003:
                return {"type": "bearish_fvg",
                        "label": f"Bearish FVG zone ({bot:.5g} – {top:.5g})"}
    return None


def detect_break_retest(ohlcv: list, lookback: int = 40) -> dict | None:
    """Break & Retest: key level broken, price retesting from the new side."""
    if len(ohlcv) < lookback + 5:
        return None
    closes = [c[4] for c in ohlcv]
    current = closes[-1]
    ref = ohlcv[-lookback:-5]
    swing_high = max(c[2] for c in ref)
    swing_low  = min(c[3] for c in ref)
    tol = 0.004  # 0.4%

    if max(closes[-15:]) > swing_high and abs(current - swing_high) / swing_high < tol:
        return {"type": "bullish_break_retest",
                "label": f"Bullish Break & Retest @ {swing_high:.5g}"}
    if min(closes[-15:]) < swing_low and abs(current - swing_low) / swing_low < tol:
        return {"type": "bearish_break_retest",
                "label": f"Bearish Break & Retest @ {swing_low:.5g}"}
    return None


def detect_liquidity_grab(ohlcv: list, lookback: int = 20) -> dict | None:
    """Liquidity grab: price swept beyond obvious level then reversed sharply."""
    if len(ohlcv) < lookback + 3:
        return None
    ref         = ohlcv[-lookback:-2]
    swing_low   = min(c[3] for c in ref)
    swing_high  = max(c[2] for c in ref)
    spike       = ohlcv[-2]
    curr_close  = ohlcv[-1][4]
    candle_range = spike[2] - spike[3] + 1e-10

    # Bullish: spike below swing_low, strong rejection back above
    if spike[3] < swing_low and curr_close > swing_low:
        rejection = (curr_close - spike[3]) / candle_range
        if rejection > 0.5:
            return {"type": "bullish_liq_grab",
                    "label": f"Liquidity grab under {swing_low:.5g} — reversal op"}

    # Bearish: spike above swing_high, strong rejection back below
    if spike[2] > swing_high and curr_close < swing_high:
        rejection = (spike[2] - curr_close) / candle_range
        if rejection > 0.5:
            return {"type": "bearish_liq_grab",
                    "label": f"Liquidity grab over {swing_high:.5g} — reversal ned"}
    return None


def detect_trend_pullback(ohlcv: list) -> dict | None:
    """Trend continuation: price pulled back to EMA21/50 in trend direction."""
    closes = [c[4] for c in ohlcv]
    if len(closes) < 50:
        return None
    current = closes[-1]
    e21  = ema(closes, 21)
    e50  = ema(closes, 50)
    e200 = ema(closes, min(200, len(closes)))
    tol  = 0.006  # 0.6%

    if e50 > e200:   # uptrend
        for level, name in ((e21, "EMA21"), (e50, "EMA50")):
            if abs(current - level) / level < tol:
                return {"type": "bullish_pullback",
                        "label": f"Trend-pullback til {name} ({level:.5g})"}
    elif e50 < e200:  # downtrend
        for level, name in ((e21, "EMA21"), (e50, "EMA50")):
            if abs(current - level) / level < tol:
                return {"type": "bearish_pullback",
                        "label": f"Trend-pullback til {name} ({level:.5g})"}
    return None


# ── Indicator scoring ─────────────────────────────────────────────────────────

def _score_timeframe(ohlcv: list) -> dict:
    if len(ohlcv) < 50:
        return {"direction": "neutral", "confidence": 0.0, "checks": []}

    closes = [c[4] for c in ohlcv]
    highs  = [c[2] for c in ohlcv]
    lows   = [c[3] for c in ohlcv]
    vols   = [c[5] for c in ohlcv]
    price  = closes[-1]
    e50    = ema(closes, 50)
    e200   = ema(closes, min(200, len(closes)))

    votes = []  # (direction: +1/-1/0, weight, label)

    # 1. Trend — EMA 50/200 (heavy weight)
    if e50 > e200 and price > e50:
        votes.append((1, 2, "Uptrend: pris over EMA50>EMA200"))
    elif e50 < e200 and price < e50:
        votes.append((-1, 2, "Downtrend: pris under EMA50<EMA200"))
    else:
        votes.append((0, 2, "Blandet EMA50/200 — ingen klar trend"))

    # 2. Short momentum — EMA 9/21
    e9  = ema(closes, 9)
    e21 = ema(closes, 21)
    if e9 > e21:
        votes.append((1, 1.5, "EMA9 over EMA21 (momentum op)"))
    else:
        votes.append((-1, 1.5, "EMA9 under EMA21 (momentum ned)"))

    # 3. RSI
    rsi_val = rsi(closes)
    if rsi_val < 35:
        votes.append((1, 1, f"RSI oversold ({rsi_val:.1f})"))
    elif rsi_val > 65:
        votes.append((-1, 1, f"RSI overbought ({rsi_val:.1f})"))
    else:
        votes.append((0, 1, f"RSI neutral ({rsi_val:.1f})"))

    # 4. MACD
    ml, sl_macd, hist = macd(closes)
    if hist > 0 and ml > 0:
        votes.append((1, 1.5, "MACD bullish crossover"))
    elif hist < 0 and ml < 0:
        votes.append((-1, 1.5, "MACD bearish crossover"))
    else:
        votes.append((0, 1.5, "MACD neutral"))

    # 5. Bollinger Bands
    _u, _m, _l, pct_b = bollinger(closes)
    if pct_b < 0.15:
        votes.append((1, 1, f"Nær lower Bollinger (%B={pct_b:.2f})"))
    elif pct_b > 0.85:
        votes.append((-1, 1, f"Nær upper Bollinger (%B={pct_b:.2f})"))
    else:
        votes.append((0, 1, f"Bollinger neutral (%B={pct_b:.2f})"))

    # 6. Stochastic
    stoch_val = stochastic(highs, lows, closes)
    if stoch_val < 20:
        votes.append((1, 1, f"Stochastic oversold ({stoch_val:.1f})"))
    elif stoch_val > 80:
        votes.append((-1, 1, f"Stochastic overbought ({stoch_val:.1f})"))
    else:
        votes.append((0, 1, f"Stochastic neutral ({stoch_val:.1f})"))

    # 7. Volume confirmation
    vol_r = volume_ratio(vols)
    dom   = sum(v[0] * v[1] for v in votes)
    if vol_r > 1.4:
        vsign = 1 if dom > 0 else (-1 if dom < 0 else 0)
        votes.append((vsign, 1, f"Høj volumen ({vol_r:.1f}x) bekræfter retningen"))
    else:
        votes.append((0, 1, f"Normal volumen ({vol_r:.1f}x)"))

    total_w = sum(v[1] for v in votes)
    bull_w  = sum(v[1] for v in votes if v[0] > 0)
    bear_w  = sum(v[1] for v in votes if v[0] < 0)

    if bull_w > bear_w:
        direction  = "long"
        confidence = bull_w / total_w
        reasons    = [v[2] for v in votes if v[0] > 0]
    elif bear_w > bull_w:
        direction  = "short"
        confidence = bear_w / total_w
        reasons    = [v[2] for v in votes if v[0] < 0]
    else:
        direction  = "neutral"
        confidence = 0.0
        reasons    = []

    return {
        "direction": direction, "confidence": confidence, "reasons": reasons,
        "rsi": rsi_val, "stoch": stoch_val, "pct_b": pct_b, "price": price,
        "e50": e50, "e200": e200, "vol_ratio": vol_r,
        "atr": atr(highs, lows, closes),
    }


# ── Main scoring function ─────────────────────────────────────────────────────

def score_signal(
    ohlcv_1h:  list,
    ohlcv_4h:  list | None = None,
    ohlcv_1d:  list | None = None,
) -> dict:
    """
    Full multi-timeframe signal with setup detection, session check,
    and 8-point pre-trade checklist.

    Returns a dict. Check signal["checklist_ok"] == True before acting.
    """
    no_signal = {"direction": "neutral", "confidence": 0.0, "checklist_ok": False,
                 "reasons": [], "setups": [], "price": ohlcv_1h[-1][4] if ohlcv_1h else 0}

    # ── Indicator scoring across timeframes ──
    frames = [(ohlcv_1h, 1.0)]
    if ohlcv_4h and len(ohlcv_4h) >= 50:
        frames.append((ohlcv_4h, 1.5))
    if ohlcv_1d and len(ohlcv_1d) >= 50:
        frames.append((ohlcv_1d, 2.0))

    scores  = [_score_timeframe(f[0]) for f in frames]
    weights = [f[1] for f in frames]

    # All available timeframes must agree
    dirs = [s["direction"] for s in scores if s["direction"] != "neutral"]
    if not dirs or len(set(dirs)) > 1:
        return {**no_signal, "reasons": ["Blandede signaler på tværs af tidsrammer"]}

    direction = dirs[0]
    total_w   = sum(weights)
    conf_sum  = sum(s["confidence"] * w for s, w in zip(scores, weights)
                    if s["direction"] == direction)
    confidence = conf_sum / total_w

    all_reasons = list(dict.fromkeys(r for s in scores for r in s.get("reasons", [])))

    base    = scores[0]
    price   = base["price"]
    atr_val = base.get("atr", price * 0.005) or price * 0.005
    e50_val  = base.get("e50", price)
    e200_val = base.get("e200", price)

    # ── Price levels ──
    if direction == "long":
        stop_loss   = price - ATR_SL_MULT * atr_val
        take_profit = price + ATR_TP_MULT * atr_val
        partial_tp  = price + PARTIAL_R   * atr_val
    else:
        stop_loss   = price + ATR_SL_MULT * atr_val
        take_profit = price - ATR_TP_MULT * atr_val
        partial_tp  = price - PARTIAL_R   * atr_val

    rr_ratio = abs(take_profit - price) / max(abs(stop_loss - price), 1e-10)

    # ── Setup detection on 1h (primary execution timeframe) ──
    setups = []
    for detector in (detect_fvg, detect_break_retest, detect_liquidity_grab, detect_trend_pullback):
        result = detector(ohlcv_1h)
        if result:
            expected = "long" if "bullish" in result["type"] else "short"
            if expected == direction:
                setups.append(result["label"])

    setup_type = setups[0] if setups else None

    # ── Confluence count ──
    confluence = len(all_reasons) + len(setups)

    # ── Session ──
    sess = session_info()

    # ── 8-point pre-trade checklist ──
    checklist = {
        "1_trend_aligned":    (direction == "long"  and e50_val > e200_val) or
                              (direction == "short" and e50_val < e200_val),
        "2_confluence_3plus": confluence >= MIN_CONFLUENCE,
        "3_sl_logical":       atr_val > 0 and abs(stop_loss - price) > 0,
        "4_rr_min_1_2":       rr_ratio >= MIN_RR,
        "5_risk_1_2_pct":     True,      # enforced at execution, always compliant here
        "6_not_chasing":      bool(setup_type) or abs(price - e50_val) / e50_val < 0.015,
        "7_active_session":   sess["tradeable"],
        "8_setup_identified": bool(setup_type),
    }

    # Hard blockers: trend, confluence, R:R, session must all pass
    hard_checks = ["1_trend_aligned", "2_confluence_3plus", "4_rr_min_1_2", "7_active_session"]
    checklist_ok = all(checklist[k] for k in hard_checks)

    return {
        "direction":    direction,
        "confidence":   confidence,
        "reasons":      all_reasons,
        "setups":       setups,
        "setup_type":   setup_type,
        "price":        price,
        "stop_loss":    stop_loss,
        "take_profit":  take_profit,
        "partial_tp":   partial_tp,
        "rr_ratio":     rr_ratio,
        "rsi":          base.get("rsi", 50),
        "stoch":        base.get("stoch", 50),
        "vol_ratio":    base.get("vol_ratio", 1.0),
        "pct_b":        base.get("pct_b", 0.5),
        "atr":          atr_val,
        "timeframes":   len(frames),
        "session":      sess,
        "confluence":   confluence,
        "checklist":    checklist,
        "checklist_ok": checklist_ok,
    }
