from __future__ import annotations

"""
Market regime classification (spec section 33). Combines market-structure
bias with the volatility regime so strategy weighting can differ: trending
markets favour Trend Pullback, ranging markets favour Liquidity Grab /
false-breakout setups, extreme volatility suppresses everything.
"""

REGIMES = (
    "TRENDING_BULLISH", "TRENDING_BEARISH", "RANGING",
    "HIGH_VOLATILITY", "LOW_VOLATILITY", "UNCERTAIN",
)

# Multiplier applied to each strategy's raw structure_confirmation score
# component (scoring.py) based on the current regime.
STRATEGY_REGIME_WEIGHTS = {
    "TRENDING_BULLISH": {"trend_pullback": 1.2, "break_retest": 1.0, "liquidity_grab": 0.8, "fvg": 1.0},
    "TRENDING_BEARISH": {"trend_pullback": 1.2, "break_retest": 1.0, "liquidity_grab": 0.8, "fvg": 1.0},
    "RANGING":          {"trend_pullback": 0.5, "break_retest": 0.8, "liquidity_grab": 1.3, "fvg": 1.0},
    "HIGH_VOLATILITY":  {"trend_pullback": 0.5, "break_retest": 0.6, "liquidity_grab": 0.6, "fvg": 0.6},
    "LOW_VOLATILITY":   {"trend_pullback": 1.0, "break_retest": 1.0, "liquidity_grab": 1.0, "fvg": 1.0},
    "UNCERTAIN":        {"trend_pullback": 0.7, "break_retest": 0.7, "liquidity_grab": 0.7, "fvg": 0.7},
}


def classify_market_regime(structure_bias: str, vol_regime: str) -> str:
    """structure_bias: 'bullish'/'bearish'/'neutral' from market_structure.bias()
    on the HTF frame that decided direction. vol_regime: output of
    volatility.classify_regime()['regime']."""
    if vol_regime == "EXTREME":
        return "HIGH_VOLATILITY"
    if vol_regime == "LOW":
        return "LOW_VOLATILITY"
    if structure_bias == "bullish":
        return "TRENDING_BULLISH"
    if structure_bias == "bearish":
        return "TRENDING_BEARISH"
    if structure_bias == "neutral":
        return "RANGING"
    return "UNCERTAIN"


def strategy_weight(regime: str, setup_type: str) -> float:
    weights = STRATEGY_REGIME_WEIGHTS.get(regime, STRATEGY_REGIME_WEIGHTS["UNCERTAIN"])
    return weights.get(setup_type, 1.0)
