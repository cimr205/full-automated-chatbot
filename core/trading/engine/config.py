from __future__ import annotations

"""
Central configuration for the trading engine.

This is the only place profile-specific magic numbers should live (spec
requirement: no hardcoded thresholds scattered through detector code).
Forex and Gold get separate TradingProfile instances — the engine must
never apply one profile's weights/thresholds to the other's symbols.
"""
import os
from dataclasses import dataclass, fields


@dataclass(frozen=True)
class ScoreWeights:
    """Section 12 scoring buckets. Must sum to 100 for the 0-100 score to
    be meaningful — validated by TradingProfile.__post_init__."""
    htf_alignment:          float = 20
    liquidity_event:        float = 20
    structure_confirmation: float = 15
    displacement:           float = 10
    fvg_confluence:         float = 10
    session_quality:        float = 10
    volatility_condition:   float = 5
    risk_reward:            float = 5
    macro_news_alignment:   float = 5

    def total(self) -> float:
        return sum(getattr(self, f.name) for f in fields(self))


@dataclass(frozen=True)
class SessionRules:
    allowed: tuple[str, ...]
    high_priority: tuple[str, ...] = ()


@dataclass(frozen=True)
class TradingProfile:
    name: str
    symbols: tuple[str, ...]
    # Order in which strategies are preferred when multiple setups fire on
    # the same candle — used as a tiebreaker, not an exclusion filter.
    strategy_priority: tuple[str, ...]
    score_weights: ScoreWeights
    minimum_score: float            # execution threshold, section 12 (default 75/78)
    watchlist_score: float          # below minimum_score but still worth logging (60-69 band)
    risk_per_trade_pct: float
    min_rr: float
    max_spread_pct: float           # spread as % of price — above this: NO_TRADE (section 26)
    sessions: SessionRules
    min_confluence: int
    fvg_requires_confluence: bool   # FVG alone may never trigger a trade (section 3/7)
    news_block_minutes_before: int
    news_block_minutes_after: int
    max_symbol_exposure_pct: float  # section 17 — max % equity risked in this instrument bucket

    def __post_init__(self):
        total = self.score_weights.total()
        if abs(total - 100) > 0.01:
            raise ValueError(f"{self.name} score_weights must sum to 100, got {total}")


FOREX_SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF", "USDCAD", "NZDUSD")
GOLD_SYMBOLS  = ("XAUUSD", "GC=F", "XAU")

# Currencies tracked for correlation/exposure protection (section 17).
TRACKED_CURRENCIES = ("USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD", "XAU")

FOREX_PROFILE = TradingProfile(
    name="forex",
    symbols=FOREX_SYMBOLS,
    strategy_priority=("trend_pullback", "break_retest", "liquidity_grab", "fvg"),
    score_weights=ScoreWeights(
        htf_alignment=25, liquidity_event=15, structure_confirmation=15,
        displacement=8, fvg_confluence=7, session_quality=10,
        volatility_condition=5, risk_reward=10, macro_news_alignment=5,
    ),
    minimum_score=float(os.getenv("FOREX_MIN_SCORE", "75")),
    watchlist_score=float(os.getenv("FOREX_WATCHLIST_SCORE", "60")),
    risk_per_trade_pct=float(os.getenv("FOREX_RISK_PER_TRADE_PCT", "0.5")),
    min_rr=float(os.getenv("FOREX_MIN_RR", "2.0")),
    max_spread_pct=float(os.getenv("FOREX_MAX_SPREAD_PCT", "0.02")),
    sessions=SessionRules(
        allowed=("london", "london_ny_overlap", "new_york"),
        high_priority=("london", "london_ny_overlap", "new_york"),
    ),
    min_confluence=3,
    fvg_requires_confluence=True,
    news_block_minutes_before=int(os.getenv("FOREX_NEWS_BLOCK_MIN_BEFORE", "30")),
    news_block_minutes_after=int(os.getenv("FOREX_NEWS_BLOCK_MIN_AFTER", "30")),
    max_symbol_exposure_pct=float(os.getenv("FOREX_MAX_SYMBOL_EXPOSURE_PCT", "2.0")),
)

GOLD_PROFILE = TradingProfile(
    name="gold",
    symbols=GOLD_SYMBOLS,
    strategy_priority=("liquidity_grab", "fvg", "break_retest", "trend_pullback"),
    score_weights=ScoreWeights(
        htf_alignment=10, liquidity_event=25, structure_confirmation=18,
        displacement=15, fvg_confluence=12, session_quality=12,
        volatility_condition=3, risk_reward=3, macro_news_alignment=2,
    ),
    minimum_score=float(os.getenv("GOLD_MIN_SCORE", "78")),
    watchlist_score=float(os.getenv("GOLD_WATCHLIST_SCORE", "60")),
    risk_per_trade_pct=float(os.getenv("GOLD_RISK_PER_TRADE_PCT", "0.35")),
    min_rr=float(os.getenv("GOLD_MIN_RR", "2.0")),
    max_spread_pct=float(os.getenv("GOLD_MAX_SPREAD_PCT", "0.05")),
    sessions=SessionRules(
        allowed=("london", "new_york", "london_ny_overlap"),
        high_priority=("new_york", "london_ny_overlap"),
    ),
    min_confluence=3,
    fvg_requires_confluence=True,
    news_block_minutes_before=int(os.getenv("GOLD_NEWS_BLOCK_MIN_BEFORE", "45")),
    news_block_minutes_after=int(os.getenv("GOLD_NEWS_BLOCK_MIN_AFTER", "45")),
    max_symbol_exposure_pct=float(os.getenv("GOLD_MAX_SYMBOL_EXPOSURE_PCT", "1.5")),
)

PROFILES = {"forex": FOREX_PROFILE, "gold": GOLD_PROFILE}


def get_profile(symbol: str) -> TradingProfile:
    """Symbol -> TradingProfile. Defaults to forex for unrecognized symbols
    rather than raising, since the watchlist can be edited via /watchlist
    with tickers this module doesn't know about yet — but gold tickers are
    matched explicitly first so they never silently fall through to forex
    weights (the one thing the spec forbids outright)."""
    upper = symbol.upper()
    if any(g in upper for g in ("XAU", "GOLD", "GC=F")):
        return GOLD_PROFILE
    return FOREX_PROFILE


# Execution threshold bands (section 12).
SCORE_BANDS = (
    (0, 60, "NO_TRADE"),
    (60, 70, "WATCHLIST"),
    (70, 80, "VALID"),
    (80, 90, "STRONG"),
    (90, 101, "EXCEPTIONAL"),
)


def score_band(score: float) -> str:
    for lo, hi, label in SCORE_BANDS:
        if lo <= score < hi:
            return label
    return "EXCEPTIONAL" if score >= 90 else "NO_TRADE"


# ── Global safety switches (section 25) ────────────────────────────────────
LIVE_TRADING = os.getenv("LIVE_TRADING", "false").lower() == "true"
PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() == "true"
