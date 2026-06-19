"""
BrokerBridge — routes trade execution to whichever broker terminal
(MetaTrader 4 or MetaTrader 5) is actually online, so the rest of the
system never needs to care which one the user is running.

Preference order:
  1. Redis key "trading:active_broker" if set to "mt4" or "mt5"
  2. Whichever bridge responds to ping() (MT5 checked first)
"""
import asyncio
import logging

import redis.asyncio as aioredis

from .mt5_bridge import MT5Bridge
from .mt4_bridge import MT4Bridge

log = logging.getLogger(__name__)

ACTIVE_BROKER_KEY = "trading:active_broker"


class BrokerBridge:
    def __init__(self, redis: aioredis.Redis):
        self._redis = redis
        self.mt5 = MT5Bridge(redis)
        self.mt4 = MT4Bridge(redis)

    async def run(self):
        asyncio.create_task(self.mt5.run())
        asyncio.create_task(self.mt4.run())
        log.info("BrokerBridge started (MT5 + MT4 listeners)")

    async def status(self) -> dict:
        mt5_online, mt4_online = await asyncio.gather(self.mt5.ping(), self.mt4.ping())
        preferred = await self._redis.get(ACTIVE_BROKER_KEY)
        return {
            "mt5_online": mt5_online,
            "mt4_online": mt4_online,
            "preferred":  preferred or "auto",
        }

    async def _select(self) -> MT5Bridge | MT4Bridge | None:
        preferred = await self._redis.get(ACTIVE_BROKER_KEY)
        if preferred == "mt5" and await self.mt5.ping():
            return self.mt5
        if preferred == "mt4" and await self.mt4.ping():
            return self.mt4
        if await self.mt5.ping():
            return self.mt5
        if await self.mt4.ping():
            return self.mt4
        return None

    async def send_open(self, trade_id: str, symbol: str, direction: str,
                        stop_loss: float, take_profit: float, volume: float = 0) -> dict:
        broker = await self._select()
        if broker is None:
            return {"error": "Ingen MT4/MT5 Worker online"}
        result = await broker.send_open(trade_id, symbol, direction, stop_loss, take_profit, volume)
        result["broker"] = broker.name
        return result

    async def send_close(self, trade_id: str, symbol: str, direction: str, volume: float = 0.01) -> dict:
        # Whichever bridge holds the ticket for this trade_id must close it.
        if await self._redis.hget("trading:mt5:tickets", trade_id):
            return await self.mt5.send_close(trade_id, symbol, direction, volume)
        if await self._redis.hget("trading:mt4:tickets", trade_id):
            return await self.mt4.send_close(trade_id, symbol, direction, volume)
        return {"error": "Ingen ticket fundet for denne trade på MT4 eller MT5"}

    async def set_preference(self, broker: str):
        if broker not in ("mt4", "mt5", "auto"):
            raise ValueError("broker must be 'mt4', 'mt5' or 'auto'")
        if broker == "auto":
            await self._redis.delete(ACTIVE_BROKER_KEY)
        else:
            await self._redis.set(ACTIVE_BROKER_KEY, broker)
