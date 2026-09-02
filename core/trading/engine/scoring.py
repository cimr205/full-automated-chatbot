from __future__ import annotations

"""
Deterministic scoring engine (spec sections 12/13). Computes a 0-100 score
from concrete, named features -- never a free-form AI guess. Weights are
profile-specific (config.FOREX_PROFILE / config.GOLD_PROFILE); this
function is the single place Forex and Gold logic actually diverge, and it
never applies one profile's weights to the other's symbol.

Frame convention used throughout: ohlcv_htf = H4 (the trend/bias frame for
both profiles per spec sections 3 & 4), ohlcv_1h = execution/structure
frame, ohlcv_15m = confirmation + liquidity/session-range frame,
ohlcv_5m = optional precision-entry frame.
"""
from datetime import datetime, timezone

from . import market_structure, sessions as sessions_engine, volatility, regime as regime_engine
from . import liquidity, fvg as fvg_engine, break_retest, trend_pullback, liquidity_grab
from .config import TradingProfile, score_band
from .signal_object import Signal, NoTradeResult

Candle = list


def _strategy_key(setup_type: str) -> str:
    if "liq" in setup_type:
        return "liquidity_grab"
    if "fvg" in setup_type:
        return "fvg"
    if "break_retest" in setup_type:
        return "break_retest"
    if "pullback" in setup_type:
        return "trend_pullback"
    return "unknown"


def score_symbol(
    symbol: str,
    profile: TradingProfile,
    ohlcv_htf: list[Candle],
    ohlcv_1h: list[Candle],
    ohlcv_15m: list[Candle] | None = None,
    ohlcv_daily: list[Candle] | None = None,
    ohlcv_5m: list[Candle] | None = None,
    at: datetime | None = None,
    spread_pct: float = 0.0,
    news_blocked: tuple[bool, str] = (False, ""),
    atr_sl_mult: float | None = None,
    atr_tp_mult: float | None = None,
) -> Signal | NoTradeResult:
    at = at or datetime.now(timezone.utc)

    if not ohlcv_htf or not ohlcv_1h or len(ohlcv_1h) < 60 or len(ohlcv_htf) < 60:
        return NoTradeResult(symbol, profile.name, ["Utilstrækkelig candle-historik"], score=0)

    # ── 1. Timeframe bias — the HTF (H4) frame decides direction, never RSI/oscillators alone ──
    frames = {"daily": ohlcv_daily or [], "h4": ohlcv_htf, "h1": ohlcv_1h,
              "m15": ohlcv_15m or [], "m5": ohlcv_5m or []}
    tf_bias = market_structure.multi_timeframe_bias(frames)
    htf_bias = market_structure.bias(ohlcv_htf)
    if htf_bias == "neutral":
        return NoTradeResult(symbol, profile.name, ["H4 uden klar retning (ranging)"], score=0)
    direction = "long" if htf_bias == "bullish" else "short"

    # ── 2. Session gate ──
    session = sessions_engine.current_session(at)
    if not sessions_engine.session_allowed(session, profile):
        return NoTradeResult(symbol, profile.name,
                              [f"Session '{session.name}' ikke tilladt for {profile.name}-profilen"], score=0)

    # ── 3. News gate ──
    blocked, news_reason = news_blocked
    if blocked:
        return NoTradeResult(symbol, profile.name, [news_reason or "Nyheds-filter aktivt"], score=0)

    # ── 4. Spread gate (section 26) ──
    if spread_pct > profile.max_spread_pct:
        return NoTradeResult(symbol, profile.name,
                              [f"Spread {spread_pct:.3f}% over max {profile.max_spread_pct:.3f}%"], score=0)

    # ── 5. Volatility / regime gate ──
    vol = volatility.classify_regime(ohlcv_1h)
    vol_adj = volatility.volatility_adjustment(vol["regime"])
    mkt_regime = regime_engine.classify_market_regime(htf_bias, vol["regime"])
    if vol_adj["disable_entries"]:
        return NoTradeResult(symbol, profile.name,
                              [f"Ekstrem volatilitet (ATR {vol['percentile']:.0%} percentil) — entries deaktiveret"],
                              score=0)

    # ── 6. Liquidity event ──
    swings_1h = market_structure.find_swings(ohlcv_1h)
    levels = liquidity.gather_levels(ohlcv_1h, ohlcv_15m or ohlcv_1h, swings_1h, at)
    side = "sell_side" if direction == "long" else "buy_side"
    cand_levels = levels["sell_side_levels"] if direction == "long" else levels["buy_side_levels"]
    sweep = liquidity.find_best_sweep(ohlcv_1h, cand_levels, side)

    # ── 7. Setup detection, per profile strategy priority ──
    setups: list[dict] = []

    lg = liquidity_grab.detect(ohlcv_1h, ohlcv_1h, ohlcv_15m or ohlcv_1h)
    if lg and lg["direction"] == direction:
        setups.append(lg)

    br = break_retest.detect(ohlcv_1h)
    if br and br["direction"] == direction:
        setups.append(br)

    tp = trend_pullback.detect(ohlcv_htf, ohlcv_15m)
    if tp and tp["direction"] == direction:
        setups.append(tp)

    # Gold-specific: Asian Range Sweep is the single most reliable XAUUSD
    # intraday pattern (see core/trading/asian_range.py) and the existing
    # 7-factor gold confluence checker (core/trading/confluences.py) is
    # reused here rather than reimplemented — both are already backtested
    # and profile.name == "gold" gates them so forex never sees this path.
    asian_sweep = None
    gold_confluence_boost = 0.0
    gold_confluence_labels: list[str] = []
    if profile.name == "gold" and ohlcv_15m:
        from .. import asian_range as _asian_range
        from .. import confluences as _confluences
        asian_sweep = _asian_range.detect(ohlcv_15m, at)
        if asian_sweep and asian_sweep["direction"] == direction:
            setups.insert(0, {
                "type": asian_sweep["type"], "direction": direction,
                "label": asian_sweep["label"],
                "sl_level": asian_sweep["sl_level"], "tp_level": asian_sweep["tp_level"],
            })
        gold_confluence_boost, gold_confluence_labels = _confluences.check_all(
            direction, ohlcv_15m, ohlcv_1h, ohlcv_htf, asian_sweep)

    price_now = ohlcv_1h[-1][4]
    gap = fvg_engine.nearest_unmitigated(ohlcv_1h, direction, price_now)
    fvg_setup = {**gap, "direction": direction} if gap else None

    if not setups:
        # Section 3/7: FVG alone may not trigger a trade unless the profile
        # explicitly opts out of that guard (both default profiles require it).
        if fvg_setup and not profile.fvg_requires_confluence:
            pass
        else:
            reason = ("Kun FVG fundet — kræver mindst én anden strategi som confluence" if fvg_setup
                       else "Intet gyldigt setup fundet i denne retning")
            return NoTradeResult(symbol, profile.name, [reason], score=0)

    setup_types = [s["type"] for s in setups]
    if fvg_setup:
        setup_types.append(fvg_setup["type"])

    # ── 8. Structure confirmation ──
    structure_event = market_structure.detect_bos_choch(ohlcv_1h)
    structure_confirms = (structure_event.get("event") in ("BOS", "CHOCH")
                           and structure_event.get("direction") == direction)

    # ── 9. Displacement ──
    displacement = (any(s.get("sweep", {}).get("quality", 0) > 0.6 for s in setups)
                     or (fvg_setup is not None and fvg_setup.get("displacement", False)))

    # ── 10. Score components (0-1 raw, scaled by profile weight) ──
    w = profile.score_weights
    reasoning: list[str] = [f"HTF bias: {tf_bias}"]

    htf_agree = tf_bias.get("h4") != "neutral" and tf_bias.get("h4") == tf_bias.get("h1")
    htf_component = 1.0 if htf_agree else 0.5

    if sweep:
        liquidity_component = sweep["quality"]
        reasoning.append(f"Liquidity sweep @ {sweep.get('level_name', '?')} (kvalitet {sweep['quality']})")
    elif any(s["type"].startswith(("bullish_liq", "bearish_liq", "bullish_asian", "bearish_asian")) for s in setups):
        liquidity_component = 0.5 if asian_sweep else 0.3
    else:
        liquidity_component = 0.0
    if gold_confluence_boost:
        liquidity_component = min(1.0, liquidity_component + gold_confluence_boost)
        reasoning.extend(gold_confluence_labels)

    structure_component = 1.0 if structure_confirms else (0.5 if setups else 0.0)
    if structure_confirms:
        reasoning.append(f"{structure_event['event']} bekræfter retning ({structure_event['label']})")

    displacement_component = 1.0 if displacement else 0.4

    if fvg_setup:
        fvg_component = fvg_engine.score_fvg_quality(
            fvg_setup, bool(sweep), direction, fvg_setup.get("filled_pct", 0.0))
        reasoning.append(f"FVG confluence ({fvg_setup['type']}, fyldt {fvg_setup.get('filled_pct', 0):.0%})")
    else:
        fvg_component = 0.0

    session_component = 1.0 if sessions_engine.session_is_high_priority(session, profile) else 0.6
    reasoning.append(f"Session: {session.name}")

    volatility_component = max(0.0, 1.0 - vol_adj["score_penalty"] / 10)

    # ── 11. Entry / SL / TP levels ──
    entry_price = price_now
    order_type, limit_price = "market", None
    for s in setups:
        if s["type"] in ("bullish_break_retest", "bearish_break_retest"):
            # Resolve the limit entry BEFORE computing SL below -- SL/TP must
            # be measured from the actual planned entry, not the current
            # market price, or a pending limit order gets the wrong risk distance.
            order_type, limit_price = "limit", s["limit_price"]
            entry_price = limit_price

    atr_val = vol["atr"] if vol.get("atr") else entry_price * 0.005
    custom_tp = None
    if setups and setups[0]["type"] in ("bullish_asian_sweep", "bearish_asian_sweep"):
        # Asian Range Sweep provides its own tuned SL/TP (sweep-wick-based
        # stop, 1.5x-range target) — use those directly instead of the
        # generic sweep/structure/ATR logic below.
        stop_loss = setups[0]["sl_level"]
        custom_tp = setups[0]["tp_level"]
    elif sweep:
        # SL beyond the sweep's extreme (section 15).
        buffer = atr_val * 0.15
        swept_candle = ohlcv_1h[min(sweep["swept_index"], len(ohlcv_1h) - 1)]
        stop_loss = swept_candle[3] - buffer if direction == "long" else swept_candle[2] + buffer
    else:
        # SL beyond structure if available, else ATR-based volatility buffer (section 15).
        sl_mult = atr_sl_mult if atr_sl_mult is not None else 1.0
        stop_loss = entry_price - sl_mult * atr_val if direction == "long" else entry_price + sl_mult * atr_val
        relevant = [s for s in swings_1h if s["type"] == ("low" if direction == "long" else "high")]
        if relevant:
            structural_level = relevant[-1]["price"]
            stop_loss = (min(stop_loss, structural_level - atr_val * 0.1) if direction == "long"
                         else max(stop_loss, structural_level + atr_val * 0.1))

    risk = abs(entry_price - stop_loss)
    if risk <= 0:
        return NoTradeResult(symbol, profile.name, ["Ugyldig SL-afstand (0)"], score=0)

    if custom_tp is not None:
        take_profit = custom_tp
    elif atr_tp_mult is not None:
        take_profit = (entry_price + atr_tp_mult * atr_val if direction == "long"
                       else entry_price - atr_tp_mult * atr_val)
    else:
        take_profit = (entry_price + risk * max(profile.min_rr, 2.0) if direction == "long"
                       else entry_price - risk * max(profile.min_rr, 2.0))

        # Opposing liquidity as an extended TP target where it beats the fixed-RR level (section 16).
        opposing = levels["buy_side_levels"] if direction == "long" else levels["sell_side_levels"]
        if opposing:
            target = max((lv for _, lv in opposing), default=take_profit) if direction == "long" \
                     else min((lv for _, lv in opposing), default=take_profit)
            beats_fixed_rr = target > take_profit if direction == "long" else target < take_profit
            if beats_fixed_rr:
                take_profit = target

    rr_ratio = abs(take_profit - entry_price) / risk
    if rr_ratio < profile.min_rr:
        return NoTradeResult(symbol, profile.name,
                              [f"R:R {rr_ratio:.2f} under minimum {profile.min_rr}"], score=0)
    rr_component = min(1.0, rr_ratio / max(profile.min_rr, 1e-6))

    macro_component = 0.5   # section 18: stub pending a live macro-strength feed beyond the news gate above

    breakdown = {
        "htf_alignment":          round(htf_component * w.htf_alignment, 1),
        "liquidity_event":        round(liquidity_component * w.liquidity_event, 1),
        "structure_confirmation": round(structure_component * w.structure_confirmation, 1),
        "displacement":           round(displacement_component * w.displacement, 1),
        "fvg_confluence":         round(fvg_component * w.fvg_confluence, 1),
        "session_quality":        round(session_component * w.session_quality, 1),
        "volatility_condition":   round(volatility_component * w.volatility_condition, 1),
        "risk_reward":            round(rr_component * w.risk_reward, 1),
        "macro_news_alignment":   round(macro_component * w.macro_news_alignment, 1),
    }

    lead_type = setups[0]["type"] if setups else (fvg_setup["type"] if fvg_setup else "")
    regime_mult = regime_engine.strategy_weight(mkt_regime, _strategy_key(lead_type))
    total_score = max(0.0, min(100.0, round(sum(breakdown.values()) * regime_mult, 1)))

    if total_score < 60:
        return NoTradeResult(symbol, profile.name,
                              [f"Score {total_score}/100 under minimumstærskel (60)"], score=total_score)

    status = "VALID" if total_score >= profile.minimum_score else "WATCHLIST"
    reasoning.append(f"Regime: {mkt_regime} (vægt x{regime_mult})")
    reasoning.append(f"Score: {total_score}/100 ({score_band(total_score)})")
    reasoning.append(f"R:R {rr_ratio:.2f} (min {profile.min_rr})")

    return Signal(
        symbol=symbol, profile=profile.name, direction=direction.upper(),
        setup=setup_types, timeframe_bias=tf_bias,
        entry=round(entry_price, 6), stop_loss=round(stop_loss, 6), take_profit=round(take_profit, 6),
        risk_reward=round(rr_ratio, 2), confidence_score=total_score,
        session=session.name, reasoning=reasoning, status=status,
        order_type=order_type, limit_price=limit_price,
        partial_r=1.5, regime=mkt_regime, score_breakdown=breakdown,
    )
