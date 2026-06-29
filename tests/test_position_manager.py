import json

import pytest
import fakeredis.aioredis as fakeredis

from core.trading.position_manager import PositionManager


@pytest.fixture
async def redis():
    r = fakeredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
def pm(redis):
    return PositionManager(redis)


@pytest.mark.asyncio
async def test_open_trade_creates_open_position(pm, redis):
    trade_id = await pm.open_trade(
        symbol="XAUUSD=X", market="forex", direction="long",
        entry=2000.0, sl=1990.0, tp=2030.0,
    )
    assert trade_id.startswith("trd_")
    open_trades = await pm.list_open()
    assert len(open_trades) == 1
    assert open_trades[0]["trade_id"] == trade_id
    assert open_trades[0]["status"] == "open"


@pytest.mark.asyncio
async def test_close_trade_moves_to_history_and_computes_r(pm):
    trade_id = await pm.open_trade(
        symbol="XAUUSD=X", market="forex", direction="long",
        entry=2000.0, sl=1990.0, tp=2030.0,
    )
    result = await pm.close_trade(trade_id, exit_price=2020.0, reason="take_profit")
    assert result["r_multiple"] == pytest.approx(2.0)  # risked 10, made 20 -> 2R

    assert await pm.list_open() == []
    history = await pm.history()
    assert len(history) == 1
    assert history[0]["trade_id"] == trade_id
    assert history[0]["status"] == "closed"


@pytest.mark.asyncio
async def test_close_trade_short_direction_computes_r_correctly(pm):
    trade_id = await pm.open_trade(
        symbol="XAUUSD=X", market="forex", direction="short",
        entry=2000.0, sl=2010.0, tp=1970.0,
    )
    result = await pm.close_trade(trade_id, exit_price=1980.0, reason="manual")
    # risked 10 (entry-sl), gained 20 (entry-exit) -> +2R
    assert result["r_multiple"] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_close_unknown_trade_returns_error(pm):
    result = await pm.close_trade("trd_doesnotexist", exit_price=100.0)
    assert "error" in result


@pytest.mark.asyncio
async def test_close_already_closed_trade_returns_error(pm):
    trade_id = await pm.open_trade(
        symbol="XAUUSD=X", market="forex", direction="long",
        entry=2000.0, sl=1990.0, tp=2030.0,
    )
    await pm.close_trade(trade_id, exit_price=2030.0)
    result = await pm.close_trade(trade_id, exit_price=2030.0)
    assert "error" in result


@pytest.mark.asyncio
async def test_stats_reports_zero_for_no_trades(pm):
    stats = await pm.stats()
    assert stats == {"total": 0, "win_rate": 0, "avg_rr": 0, "total_r": 0}


@pytest.mark.asyncio
async def test_stats_computes_win_rate_correctly(pm):
    winner = await pm.open_trade(symbol="XAUUSD=X", market="forex", direction="long", entry=2000.0, sl=1990.0, tp=2030.0)
    loser = await pm.open_trade(symbol="XAUUSD=X", market="forex", direction="long", entry=2000.0, sl=1990.0, tp=2030.0)
    await pm.close_trade(winner, exit_price=2030.0, reason="take_profit")
    await pm.close_trade(loser, exit_price=1990.0, reason="stop_loss")

    stats = await pm.stats()
    assert stats["total"] == 2
    assert stats["wins"] == 1
    assert stats["win_rate"] == 50.0


@pytest.mark.asyncio
async def test_check_positions_closes_on_stop_loss_hit(pm, redis, monkeypatch):
    trade_id = await pm.open_trade(
        symbol="XAUUSD=X", market="forex", direction="long",
        entry=2000.0, sl=1990.0, tp=2030.0,
    )

    async def fake_price(symbol):
        return 1985.0  # below stop loss

    monkeypatch.setattr("core.trading.market_data.get_last_price", fake_price)
    await pm._check_positions()

    open_trades = await pm.list_open()
    assert open_trades == []
    history = await pm.history()
    assert history[0]["close_reason"] == "stop_loss"


@pytest.mark.asyncio
async def test_check_positions_closes_on_take_profit_hit(pm, monkeypatch):
    trade_id = await pm.open_trade(
        symbol="XAUUSD=X", market="forex", direction="short",
        entry=2000.0, sl=2010.0, tp=1970.0,
    )

    async def fake_price(symbol):
        return 1965.0  # at/below take profit for a short

    monkeypatch.setattr("core.trading.market_data.get_last_price", fake_price)
    await pm._check_positions()

    history = await pm.history()
    assert history[0]["close_reason"] == "take_profit"


@pytest.mark.asyncio
async def test_check_positions_marks_partial_tp_without_closing(pm, redis, monkeypatch):
    trade_id = await pm.open_trade(
        symbol="XAUUSD=X", market="forex", direction="long",
        entry=2000.0, sl=1990.0, tp=2030.0, partial_tp=2015.0,
    )

    async def fake_price(symbol):
        return 2016.0  # past partial, not past full TP

    monkeypatch.setattr("core.trading.market_data.get_last_price", fake_price)
    await pm._check_positions()

    open_trades = await pm.list_open()
    assert len(open_trades) == 1
    assert open_trades[0].get("partial_hit") is True
    assert open_trades[0]["status"] == "open"


@pytest.mark.asyncio
async def test_check_positions_skips_when_price_unavailable(pm, monkeypatch):
    await pm.open_trade(
        symbol="XAUUSD=X", market="forex", direction="long",
        entry=2000.0, sl=1990.0, tp=2030.0,
    )

    async def fake_price(symbol):
        return None

    monkeypatch.setattr("core.trading.market_data.get_last_price", fake_price)
    await pm._check_positions()  # must not raise

    open_trades = await pm.list_open()
    assert len(open_trades) == 1  # untouched
