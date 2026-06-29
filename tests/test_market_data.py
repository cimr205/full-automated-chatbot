import pytest

from core.trading import market_data as md


@pytest.mark.asyncio
async def test_fetch_ohlcv_returns_data_on_first_success(monkeypatch):
    calls = []

    async def fake_once(symbol, interval, range_):
        calls.append(symbol)
        return {"open": [1.0] * 12, "high": [1.0] * 12, "low": [1.0] * 12,
                "close": [1.0] * 12, "volume": [1.0] * 12}

    monkeypatch.setattr(md, "_fetch_once", fake_once)
    result = await md.fetch_ohlcv("EURUSD=X")
    assert result is not None
    assert calls == ["EURUSD=X"]


@pytest.mark.asyncio
async def test_fetch_ohlcv_retries_on_exception_then_succeeds(monkeypatch):
    attempts = {"n": 0}

    async def flaky(symbol, interval, range_):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RuntimeError("503")
        return {"open": [1.0] * 12, "high": [1.0] * 12, "low": [1.0] * 12,
                "close": [1.0] * 12, "volume": [1.0] * 12}

    monkeypatch.setattr(md, "_fetch_once", flaky)
    monkeypatch.setattr(md.asyncio, "sleep", lambda *_: _instant_sleep())
    result = await md.fetch_ohlcv("EURUSD=X")
    assert result is not None
    assert attempts["n"] == 2


async def _instant_sleep():
    return None


@pytest.mark.asyncio
async def test_fetch_ohlcv_falls_back_to_alternate_symbol_for_gold(monkeypatch):
    calls = []

    async def fake_once(symbol, interval, range_):
        calls.append(symbol)
        if symbol == "XAUUSD=X":
            return None  # primary always fails
        return {"open": [1.0] * 12, "high": [1.0] * 12, "low": [1.0] * 12,
                "close": [1.0] * 12, "volume": [1.0] * 12}

    monkeypatch.setattr(md, "_fetch_once", fake_once)
    monkeypatch.setattr(md.asyncio, "sleep", lambda *_: _instant_sleep())
    result = await md.fetch_ohlcv("XAUUSD=X")
    assert result is not None
    assert "GC=F" in calls
    assert calls[0] == "XAUUSD=X"  # primary tried first


@pytest.mark.asyncio
async def test_fetch_ohlcv_returns_none_when_all_attempts_fail(monkeypatch):
    async def always_fail(symbol, interval, range_):
        raise RuntimeError("boom")

    monkeypatch.setattr(md, "_fetch_once", always_fail)
    monkeypatch.setattr(md.asyncio, "sleep", lambda *_: _instant_sleep())
    result = await md.fetch_ohlcv("XAUUSD=X")
    assert result is None


def test_resample_4h_groups_correctly():
    ohlcv_1h = {
        "open":  list(range(8)),
        "high":  [v + 1 for v in range(8)],
        "low":   [v - 1 for v in range(8)],
        "close": list(range(8)),
        "volume": [10] * 8,
    }
    out = md.resample_4h(ohlcv_1h)
    assert out is not None
    assert len(out["close"]) == 2
    assert out["high"][0] == max(range(0, 4)) + 1
    assert out["volume"][0] == 40


def test_resample_4h_returns_none_for_insufficient_data():
    assert md.resample_4h({"open": [], "high": [], "low": [], "close": [], "volume": []}) is None
