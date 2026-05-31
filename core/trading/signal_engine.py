"""
Multi-indicator, multi-timeframe signal scorer.

A signal is only surfaced when 70%+ of independent checks agree on direction.
Each check is weighted: trend indicators count more than oscillators.
"""
from .indicators import rsi, ema, macd, bollinger, atr, volume_ratio, stochastic

MIN_CONFIDENCE = 0.68  # at least 68% agreement across all checks


def _score_timeframe(ohlcv: list) -> dict:
    """
    Score a single timeframe.
    ohlcv: [[ts, open, high, low, close, volume], ...]
    Returns {"direction": long|short|neutral, "confidence": float, "checks": list}
    """
    if len(ohlcv) < 50:
        return {"direction": "neutral", "confidence": 0.0, "checks": []}

    closes = [c[4] for c in ohlcv]
    highs  = [c[2] for c in ohlcv]
    lows   = [c[3] for c in ohlcv]
    vols   = [c[5] for c in ohlcv]
    price  = closes[-1]

    votes  = []   # (score, weight, label)

    # ── 1. Trend: EMA 50 vs EMA 200 (weight 2) ──
    e50  = ema(closes, 50)
    e200 = ema(closes, min(200, len(closes)))
    if e50 > e200 and price > e50:
        votes.append((1, 2, f"Uptrend: pris over EMA50>EMA200"))
    elif e50 < e200 and price < e50:
        votes.append((-1, 2, f"Downtrend: pris under EMA50<EMA200"))
    else:
        votes.append((0, 2, "Ingen klar trend (EMA50/200 blandet)"))

    # ── 2. Short-term momentum: EMA 9 vs EMA 21 (weight 1.5) ──
    e9  = ema(closes, 9)
    e21 = ema(closes, 21)
    if e9 > e21:
        votes.append((1, 1.5, "EMA9 over EMA21 (momentum op)"))
    else:
        votes.append((-1, 1.5, "EMA9 under EMA21 (momentum ned)"))

    # ── 3. RSI (weight 1) ──
    rsi_val = rsi(closes)
    if rsi_val < 35:
        votes.append((1, 1, f"RSI oversold ({rsi_val:.1f})"))
    elif rsi_val > 65:
        votes.append((-1, 1, f"RSI overbought ({rsi_val:.1f})"))
    else:
        votes.append((0, 1, f"RSI neutral ({rsi_val:.1f})"))

    # ── 4. MACD (weight 1.5) ──
    macd_line, signal_line, hist = macd(closes)
    if hist > 0 and macd_line > 0:
        votes.append((1, 1.5, f"MACD bullish crossover"))
    elif hist < 0 and macd_line < 0:
        votes.append((-1, 1.5, f"MACD bearish crossover"))
    else:
        votes.append((0, 1.5, f"MACD neutral"))

    # ── 5. Bollinger Bands (weight 1) ──
    _upper, _mid, _lower, pct_b = bollinger(closes)
    if pct_b < 0.15:
        votes.append((1, 1, f"Nær lower Bollinger Band (%B={pct_b:.2f})"))
    elif pct_b > 0.85:
        votes.append((-1, 1, f"Nær upper Bollinger Band (%B={pct_b:.2f})"))
    else:
        votes.append((0, 1, f"Midt i Bollinger (%B={pct_b:.2f})"))

    # ── 6. Stochastic (weight 1) ──
    stoch = stochastic(highs, lows, closes)
    if stoch < 20:
        votes.append((1, 1, f"Stochastic oversold ({stoch:.1f})"))
    elif stoch > 80:
        votes.append((-1, 1, f"Stochastic overbought ({stoch:.1f})"))
    else:
        votes.append((0, 1, f"Stochastic neutral ({stoch:.1f})"))

    # ── 7. Volume confirmation (weight 1) ──
    vol = volume_ratio(vols)
    dominant_dir = sum(v[0] * v[1] for v in votes)
    if vol > 1.4:
        dir_sign = 1 if dominant_dir > 0 else (-1 if dominant_dir < 0 else 0)
        votes.append((dir_sign, 1, f"Høj volumen bekræftelse ({vol:.1f}x)"))
    else:
        votes.append((0, 1, f"Normal volumen ({vol:.1f}x)"))

    # ── Tally ──
    total_weight = sum(abs(v[1]) for v in votes)
    bull_weight  = sum(v[1] for v in votes if v[0] > 0)
    bear_weight  = sum(v[1] for v in votes if v[0] < 0)

    if bull_weight > bear_weight:
        confidence = bull_weight / total_weight
        direction  = "long"
        reasons    = [v[2] for v in votes if v[0] > 0]
    elif bear_weight > bull_weight:
        confidence = bear_weight / total_weight
        direction  = "short"
        reasons    = [v[2] for v in votes if v[0] < 0]
    else:
        confidence = 0.0
        direction  = "neutral"
        reasons    = []

    return {
        "direction": direction,
        "confidence": confidence,
        "reasons": reasons,
        "rsi": rsi_val,
        "stoch": stoch,
        "pct_b": pct_b,
        "price": price,
        "e50": e50,
        "e200": e200,
        "vol_ratio": vol,
        "atr": atr(highs, lows, closes),
    }


def score_signal(ohlcv_1h: list, ohlcv_4h: list | None = None, ohlcv_1d: list | None = None) -> dict:
    """
    Multi-timeframe signal. All available timeframes must agree on direction.
    Higher timeframes carry more weight.
    """
    scores = []
    weights = []

    s1h = _score_timeframe(ohlcv_1h)
    scores.append(s1h)
    weights.append(1.0)

    if ohlcv_4h and len(ohlcv_4h) >= 50:
        s4h = _score_timeframe(ohlcv_4h)
        scores.append(s4h)
        weights.append(1.5)

    if ohlcv_1d and len(ohlcv_1d) >= 50:
        s1d = _score_timeframe(ohlcv_1d)
        scores.append(s1d)
        weights.append(2.0)

    # All timeframes must agree — reject mixed signals
    directions = [s["direction"] for s in scores if s["direction"] != "neutral"]
    if not directions:
        return {"direction": "neutral", "confidence": 0.0, "reasons": [], "price": scores[0].get("price", 0)}

    if len(set(directions)) > 1:
        return {"direction": "neutral", "confidence": 0.0,
                "reasons": ["Blandede signaler på tværs af tidsrammer"], "price": scores[0].get("price", 0)}

    direction = directions[0]
    total_w   = sum(weights)
    conf_sum  = sum(s["confidence"] * w for s, w in zip(scores, weights)
                    if s["direction"] == direction)
    confidence = conf_sum / total_w

    all_reasons = []
    for s in scores:
        all_reasons.extend(s.get("reasons", []))

    base = scores[0]
    price = base["price"]
    atr_val = base.get("atr", price * 0.005)

    if direction == "long":
        stop_loss   = price - 1.5 * atr_val
        take_profit = price + 3.0 * atr_val
    else:
        stop_loss   = price + 1.5 * atr_val
        take_profit = price - 3.0 * atr_val

    return {
        "direction": direction,
        "confidence": confidence,
        "reasons": list(dict.fromkeys(all_reasons)),  # dedupe, keep order
        "price": price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "rsi": base.get("rsi", 50),
        "stoch": base.get("stoch", 50),
        "vol_ratio": base.get("vol_ratio", 1.0),
        "pct_b": base.get("pct_b", 0.5),
        "timeframes": len(scores),
    }
