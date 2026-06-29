import json

import pytest
import fakeredis.aioredis as fakeredis

from core.trading.broker_bridge import BrokerBridge, ACTIVE_BROKER_KEY


@pytest.fixture
async def redis():
    r = fakeredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
def bridge(redis):
    return BrokerBridge(redis)


async def mark_online(redis, broker: str):
    await redis.set(f"trading:{broker}:online", "1", ex=120)


@pytest.mark.asyncio
async def test_status_reports_both_offline_by_default(bridge):
    status = await bridge.status()
    assert status == {"mt5_online": False, "mt4_online": False, "preferred": "auto"}


@pytest.mark.asyncio
async def test_status_reflects_online_workers(bridge, redis):
    await mark_online(redis, "mt5")
    status = await bridge.status()
    assert status["mt5_online"] is True
    assert status["mt4_online"] is False


@pytest.mark.asyncio
async def test_select_prefers_mt5_when_both_online_and_no_preference(bridge, redis):
    await mark_online(redis, "mt5")
    await mark_online(redis, "mt4")
    selected = await bridge._select()
    assert selected.name == "mt5"


@pytest.mark.asyncio
async def test_select_falls_back_to_mt4_when_mt5_offline(bridge, redis):
    await mark_online(redis, "mt4")
    selected = await bridge._select()
    assert selected.name == "mt4"


@pytest.mark.asyncio
async def test_select_returns_none_when_both_offline(bridge):
    selected = await bridge._select()
    assert selected is None


@pytest.mark.asyncio
async def test_select_honors_explicit_preference(bridge, redis):
    await mark_online(redis, "mt5")
    await mark_online(redis, "mt4")
    await bridge.set_preference("mt4")
    selected = await bridge._select()
    assert selected.name == "mt4"


@pytest.mark.asyncio
async def test_select_falls_back_when_preferred_broker_is_offline(bridge, redis):
    # user prefers mt4 but only mt5 is actually online — must not return None
    await mark_online(redis, "mt5")
    await bridge.set_preference("mt4")
    selected = await bridge._select()
    assert selected.name == "mt5"


@pytest.mark.asyncio
async def test_set_preference_auto_clears_redis_key(bridge, redis):
    await bridge.set_preference("mt5")
    assert await redis.get(ACTIVE_BROKER_KEY) == "mt5"
    await bridge.set_preference("auto")
    assert await redis.get(ACTIVE_BROKER_KEY) is None


@pytest.mark.asyncio
async def test_set_preference_rejects_invalid_value(bridge):
    with pytest.raises(ValueError):
        await bridge.set_preference("bogus")


@pytest.mark.asyncio
async def test_send_open_returns_error_when_no_broker_online(bridge):
    result = await bridge.send_open(
        trade_id="trd_1", symbol="XAUUSD", direction="long",
        stop_loss=1990.0, take_profit=2030.0,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_send_close_routes_to_broker_holding_the_ticket(bridge, redis):
    await redis.hset("trading:mt4:tickets", "trd_1", json.dumps({"ticket": 555}))
    await mark_online(redis, "mt4")

    async def fake_close(trade_id, symbol, direction, volume):
        return {"closed": True, "ticket": 555}

    bridge.mt4.send_close = fake_close
    result = await bridge.send_close("trd_1", "XAUUSD", "long")
    assert result == {"closed": True, "ticket": 555}


@pytest.mark.asyncio
async def test_send_close_returns_error_when_no_ticket_found(bridge):
    result = await bridge.send_close("trd_unknown", "XAUUSD", "long")
    assert "error" in result
