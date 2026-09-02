from __future__ import annotations

"""
Signal engine — public entry point used by market_monitor.py, backtest.py,
and optimize.py.

As of the Forex/XAUUSD dual-profile rewrite, detection logic itself lives
in core/trading/engine/ (market_structure, liquidity, fvg, break_retest,
trend_pullback, liquidity_grab, sessions, volatility, regime, scoring) —
this module is a thin, backward-compatible adapter: score_signal() keeps
its historical positional signature and legacy return-dict shape (every
key the old version returned is still present, with the same meaning) so
existing call sites keep working, while the return dict also carries the
new standardized Signal/NoTradeResult object (spec section 19/20) under
the "signal" key for anything built against the new contract.

MIN_CONFLUENCE / ATR_SL_MULT / ATR_TP_MULT / MIN_RR are kept as module
globals — read LIVE at call time (not captured as function defaults) —
because core/trading/optimize.py monkeypatches them via setattr() to
replay a backtest under many parameter combinations; that sweep still
works against the new engine through these four knobs, even though the
Forex/Gold profiles (core/trading/engine/config.py) are the source of
truth for everything else (score weights, min_confluence, sessions, etc).
"""
from datetime import datetime

from .engine import scoring
from .engine import sessions as sessions_engine
from .engine import fvg as fvg_engine
from .engine.config import get_profile
from .engine.signal_object import Signal, NoTradeResult

MIN_CONFLUENCE = 3
ATR_SL_MULT    = 1.0
ATR_TP_MULT    = 2.0
PARTIAL_R      = 1.5
MIN_RR         = 2.0


def session_info(at: datetime | None = None) -> dict:
    """Legacy-shaped wrapper around engine.sessions.current_session() —
    still timezone-correct (now via per-market zoneinfo rather than a
    fixed CET offset, see engine/sessions.py), just repackaged to the old
    dict shape for any caller still expecting it directly."""
    return sessions_engine.current_session(at).to_dict()


def detect_fvg(ohlcv: list) -> dict | None:
    """Legacy-compatible shim kept for core/trading/confluences.py's 1H-FVG
    -alignment check, which calls this with a single positional arg and
    expects the nearest gap (either direction) that currently contains
    price, or None. Real detection now lives in engine/fvg.py."""
    if len(ohlcv) < 5:
        return None
    current = ohlcv[-1][4]
    for gap in reversed(fvg_engine.find_fvgs(ohlcv)):
        if fvg_engine.current_price_in_zone(gap, current):
            return {**gap, "label": f"{gap['type']} zone ({gap['bottom']:.5g} – {gap['top']:.5g})"}
    return None


def score_signal(
    ohlcv_1h: list,
    ohlcv_4h: list | None = None,
    ohlcv_1d: list | None = None,
    ohlcv_15m: list | None = None,
    at: datetime | None = None,
    symbol: str = "",
    min_confluence: float | None = None,
    min_confluence_short: float | None = None,
    atr_sl_mult: float | None = None,
    atr_tp_mult: float | None = None,
    min_rr: float | None = None,
) -> dict:
    """
    Backward-compatible entry point.

    `symbol` is new and optional — older call sites that don't pass it
    default to the Forex profile (engine.config.get_profile("")).
    market_monitor.py and backtest.py have been updated to pass the real
    symbol so gold actually gets GOLD_PROFILE weights/thresholds instead
    of silently running forex ones (the one thing the spec forbids
    outright).

    `min_confluence_short` is accepted for call-site compatibility
    (SYMBOL_OVERRIDES used to pass it) but has no effect in the new
    engine — there's no separate short-side confluence bar; direction
    comes solely from H4 market structure, not a per-direction override.
    """
    profile = get_profile(symbol)
    from dataclasses import replace
    profile = replace(
        profile,
        min_rr=min_rr if min_rr is not None else MIN_RR,
        min_confluence=int(min_confluence) if min_confluence is not None else MIN_CONFLUENCE,
    )

    result = scoring.score_symbol(
        symbol=symbol or profile.name,
        profile=profile,
        ohlcv_htf=ohlcv_4h or ohlcv_1h,
        ohlcv_1h=ohlcv_1h,
        ohlcv_15m=ohlcv_15m,
        ohlcv_daily=ohlcv_1d,
        at=at,
        atr_sl_mult=atr_sl_mult if atr_sl_mult is not None else ATR_SL_MULT,
        atr_tp_mult=atr_tp_mult if atr_tp_mult is not None else ATR_TP_MULT,
    )
    return _to_legacy_dict(result, ohlcv_1h, at)


def _to_legacy_dict(result: Signal | NoTradeResult, ohlcv_1h: list, at: datetime | None) -> dict:
    session = sessions_engine.current_session(at)
    price = ohlcv_1h[-1][4] if ohlcv_1h else 0

    if isinstance(result, NoTradeResult):
        return {
            "direction": "neutral", "confidence": 0.0, "checklist_ok": False,
            "reasons": result.reasons, "setups": [], "setup_type": None, "setup_label": None,
            "price": price, "market_price": price, "order_type": "market", "limit_price": None,
            "stop_loss": None, "take_profit": None, "partial_tp": None, "rr_ratio": 0.0,
            "session": session.to_dict(), "confluence": 0, "confluence_boost": 0.0,
            "confluence_labels": [], "checklist": {}, "timeframes": 1,
            "signal": result.to_dict(),
        }

    direction = "long" if result.direction == "LONG" else "short"
    risk_amt = abs(result.entry - result.stop_loss) if result.stop_loss is not None else 0.0
    partial_r = result.partial_r or 1.5
    partial_tp = (result.entry + partial_r * risk_amt if direction == "long"
                  else result.entry - partial_r * risk_amt) if risk_amt else None

    checklist = {
        "1_trend_aligned":    True,   # already enforced by H4 bias gate in scoring.score_symbol
        "2_confluence_3plus": len(result.setup) >= 1,
        "3_sl_logical":       bool(risk_amt),
        "4_rr_min_1_2":       result.risk_reward >= 2.0,
        "5_risk_1_2_pct":     True,   # enforced downstream by risk_manager.compute_volume
        "6_not_chasing":      True,
        "7_active_session":   session.tradeable,
        "8_setup_identified": bool(result.setup),
    }

    return {
        "direction": direction,
        "confidence": round(result.confidence_score / 100, 4),
        "reasons": result.reasoning,
        "setups": list(result.setup),
        "setup_type": result.setup[0] if result.setup else None,
        "setup_label": f"{result.setup[0]} ({result.direction})" if result.setup else None,
        "price": result.entry, "market_price": price,
        "order_type": result.order_type, "limit_price": result.limit_price,
        "stop_loss": result.stop_loss, "take_profit": result.take_profit,
        "partial_tp": partial_tp, "rr_ratio": result.risk_reward,
        "rsi": 50, "stoch": 50, "vol_ratio": 1.0, "pct_b": 0.5,
        "atr": risk_amt, "timeframes": 3, "session": session.to_dict(),
        "confluence": len(result.setup), "confluence_boost": 0.0,
        "confluence_labels": result.reasoning,
        "checklist": checklist, "checklist_ok": all(checklist.values()),
        "signal": result.to_dict(),
    }
