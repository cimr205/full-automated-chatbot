from core.trading.engine import risk_exposure as rx


def test_parse_currency_pair_forex():
    assert rx.parse_currency_pair("EURUSD") == ("EUR", "USD")
    assert rx.parse_currency_pair("USDJPY") == ("USD", "JPY")


def test_parse_currency_pair_gold():
    assert rx.parse_currency_pair("XAUUSD") == ("XAU", "USD")
    assert rx.parse_currency_pair("GC=F") == ("XAU", "USD")


def test_parse_currency_pair_unknown_returns_none():
    assert rx.parse_currency_pair("BTCUSD") is None


def test_stacked_usd_short_exposure_blocked():
    # LONG EURUSD, SHORT USDCHF, LONG GBPUSD are all, in practice, short USD.
    open_positions = [
        {"symbol": "EURUSD", "direction": "long", "status": "open"},
        {"symbol": "USDCHF", "direction": "short", "status": "open"},
        {"symbol": "GBPUSD", "direction": "long", "status": "open"},
    ]
    ok, reason = rx.check_correlation(open_positions, "AUDUSD", "long", max_exposure_units=3.0)
    assert ok is False
    assert "USD" in reason


def test_uncorrelated_trade_allowed():
    open_positions = [{"symbol": "EURUSD", "direction": "long", "status": "open"}]
    ok, _ = rx.check_correlation(open_positions, "USDJPY", "short", max_exposure_units=3.0)
    assert ok is True


def test_closed_positions_excluded_from_exposure():
    open_positions = [{"symbol": "EURUSD", "direction": "long", "status": "closed"}]
    exposure = rx.currency_exposure(open_positions)
    assert exposure["EUR"] == 0.0
    assert exposure["USD"] == 0.0


def test_max_simultaneous_positions_enforced():
    open_positions = [{"symbol": f"SYM{i}", "direction": "long", "status": "open"} for i in range(5)]
    ok, reason = rx.check_position_limits(open_positions, "EURUSD", max_simultaneous=5)
    assert ok is False
    assert "samtidige" in reason


def test_max_per_symbol_enforced():
    open_positions = [{"symbol": "EURUSD", "direction": "long", "status": "open"}]
    ok, reason = rx.check_position_limits(open_positions, "EURUSD", max_simultaneous=10, max_per_symbol=1)
    assert ok is False


def test_max_xau_exposure_blocks_third_gold_position():
    open_positions = [
        {"symbol": "XAUUSD", "direction": "long", "status": "open"},
        {"symbol": "GC=F", "direction": "long", "status": "open"},
    ]
    ok, reason = rx.check_xau_exposure(open_positions, adding=True, max_positions=2)
    assert ok is False
    assert "XAU" in reason


def test_xau_exposure_allows_first_position():
    ok, _ = rx.check_xau_exposure([], adding=True, max_positions=2)
    assert ok is True
