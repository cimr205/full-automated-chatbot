"""Regression test for the Forex/Gold watchlist split: every symbol in the
default watchlist must resolve to its own profile, with gold never bleeding
into forex weights/thresholds or vice versa."""
from core.trading.market_monitor import DEFAULT_FOREX, MT5_SYMBOL_MAP
from core.trading.engine.config import get_profile


def test_default_watchlist_contains_real_forex_pairs_not_just_gold():
    forex_pairs = {"EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCHF=X", "USDCAD=X", "NZDUSD=X"}
    assert forex_pairs.issubset(set(DEFAULT_FOREX))


def test_gold_symbol_is_in_default_watchlist():
    assert "GC=F" in DEFAULT_FOREX


def test_every_default_symbol_has_an_mt5_mapping():
    for symbol in DEFAULT_FOREX:
        assert symbol in MT5_SYMBOL_MAP, f"{symbol} has no MT5_SYMBOL_MAP entry"


def test_forex_pairs_resolve_to_forex_profile_only():
    for symbol in DEFAULT_FOREX:
        if symbol == "GC=F":
            continue
        assert get_profile(symbol).name == "forex", f"{symbol} should be forex, not gold"


def test_gold_resolves_to_gold_profile_only():
    assert get_profile("GC=F").name == "gold"


def test_forex_and_gold_profiles_have_independent_weights():
    from core.trading.engine.config import FOREX_PROFILE, GOLD_PROFILE
    assert FOREX_PROFILE.score_weights != GOLD_PROFILE.score_weights
    assert FOREX_PROFILE.minimum_score != GOLD_PROFILE.minimum_score
    assert FOREX_PROFILE.sessions.allowed != GOLD_PROFILE.sessions.allowed or \
           FOREX_PROFILE.sessions.high_priority != GOLD_PROFILE.sessions.high_priority
