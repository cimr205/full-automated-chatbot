import json

import pytest
import fakeredis.aioredis as fakeredis

from core.trading.market_monitor import MarketMonitor, DATA_FAILURE_ALERT_AFTER


@pytest.fixture
async def redis():
    r = fakeredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
def monitor(redis):
    return MarketMonitor(redis)


def test_default_watchlist_is_xauusd_only():
    from core.trading.market_monitor import DEFAULT_FOREX, DEFAULT_STOCKS
    assert DEFAULT_FOREX == ["XAUUSD=X"]
    assert DEFAULT_STOCKS == []


@pytest.mark.asyncio
async def test_watchlist_falls_back_to_defaults_when_unset(monitor):
    wl = await monitor.watchlist()
    assert wl == {"forex": ["XAUUSD=X"], "stocks": []}


@pytest.mark.asyncio
async def test_watchlist_uses_redis_override_when_present(monitor, redis):
    await redis.set("trading:watchlist:forex", json.dumps(["EURUSD=X"]))
    wl = await monitor.watchlist()
    assert wl["forex"] == ["EURUSD=X"]


@pytest.mark.asyncio
async def test_daily_cap_not_reached_initially(monitor):
    assert await monitor._daily_cap_reached() is False


@pytest.mark.asyncio
async def test_daily_cap_reached_after_max_signals(monitor):
    from core.trading.market_monitor import MAX_DAILY_SIGNALS
    for _ in range(MAX_DAILY_SIGNALS):
        await monitor._increment_daily_count()
    assert await monitor._daily_cap_reached() is True


@pytest.mark.asyncio
async def test_journal_writes_entry(monitor, redis):
    await monitor._journal("XAUUSD=X", "forex", None, published=False, reject_reason="ingen data")
    raw = await redis.lrange("trading:journal", 0, 0)
    entry = json.loads(raw[0])
    assert entry["symbol"] == "XAUUSD=X"
    assert entry["reject_reason"] == "ingen data"
    assert entry["published"] is False


def test_reject_reason_reports_session_first():
    signal = {
        "checklist": {
            "active_session": False, "trend_aligned": False,
            "confluence_3plus": False, "rr_min_1_2": False,
        },
        "confluence": 0, "rr_ratio": 0, "confidence": 0,
    }
    assert "session" in MarketMonitor._reject_reason(signal)


def test_reject_reason_reports_confluence_when_session_and_trend_ok():
    signal = {
        "checklist": {
            "active_session": True, "trend_aligned": True,
            "confluence_3plus": False, "rr_min_1_2": True,
        },
        "confluence": 1, "rr_ratio": 3.0, "confidence": 0.6,
    }
    reason = MarketMonitor._reject_reason(signal)
    assert "confluence-faktorer" in reason


@pytest.mark.asyncio
async def test_analyze_no_data_journals_and_tracks_failure(monitor, redis, monkeypatch):
    async def fake_fetch(symbol, interval="1h", range_="1mo"):
        return None

    monkeypatch.setattr("core.trading.market_monitor.fetch_ohlcv", fake_fetch)

    for _ in range(DATA_FAILURE_ALERT_AFTER):
        await monitor._analyze("XAUUSD=X", "forex")

    assert monitor._data_fail_count["XAUUSD=X"] == DATA_FAILURE_ALERT_AFTER

    journal = await redis.lrange("trading:journal", 0, -1)
    assert len(journal) == DATA_FAILURE_ALERT_AFTER
    assert all(json.loads(e)["reject_reason"] == "ingen data" for e in journal)


@pytest.mark.asyncio
async def test_analyze_alerts_on_persistent_data_failure(monitor, redis, monkeypatch):
    async def fake_fetch(symbol, interval="1h", range_="1mo"):
        return None

    monkeypatch.setattr("core.trading.market_monitor.fetch_ohlcv", fake_fetch)

    pubsub = redis.pubsub()
    await pubsub.subscribe("supervisor:notifications")
    await pubsub.get_message(timeout=1)  # subscribe confirmation

    for _ in range(DATA_FAILURE_ALERT_AFTER):
        await monitor._analyze("XAUUSD=X", "forex")

    msg = await pubsub.get_message(timeout=1)
    assert msg is not None
    payload = json.loads(msg["data"])
    assert "Datafejl" in payload["message"]
    assert "XAUUSD=X" in payload["message"]
    await pubsub.aclose()


@pytest.mark.asyncio
async def test_analyze_resets_failure_count_on_successful_fetch(monitor, monkeypatch):
    calls = {"n": 0}

    async def flaky_fetch(symbol, interval="1h", range_="1mo"):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        # second call succeeds with a flat/low-quality series so no trade fires
        closes = [100.0] * 60
        return {"open": closes, "high": [c + 0.1 for c in closes],
                "low": [c - 0.1 for c in closes], "close": closes,
                "volume": [10.0] * 60}

    monkeypatch.setattr("core.trading.market_monitor.fetch_ohlcv", flaky_fetch)
    await monitor._analyze("XAUUSD=X", "forex")
    assert monitor._data_fail_count["XAUUSD=X"] == 1

    await monitor._analyze("XAUUSD=X", "forex")
    assert monitor._data_fail_count["XAUUSD=X"] == 0
