"""
MT5 Bridge — Railway side.

Sends trade commands to the MT5 Worker running on the user's Windows PC
(via the official MetaTrader5 Python package), and listens for execution
results to update position tracking.
"""
import asyncio
import json
import logging
from datetime import datetime

import redis.asyncio as aioredis

log = logging.getLogger(__name__)


class MT5Bridge:
    name = "mt5"

    def __init__(self, redis: aioredis.Redis):
        self._redis   = redis
        self._running = False
        self._pending: dict[str, asyncio.Future] = {}

    async def run(self):
        self._running = True
        pubsub = self._redis.pubsub()
        await pubsub.subscribe("trading:mt5:results")
        log.info("MT5Bridge listening for results")
        try:
            async for msg in pubsub.listen():
                if not self._running:
                    break
                if msg["type"] != "message":
                    continue
                try:
                    await self._handle_result(json.loads(msg["data"]))
                except Exception as e:
                    log.warning("MT5Bridge result parse error: %s", e)
        finally:
            await pubsub.unsubscribe("trading:mt5:results")
            await pubsub.aclose()

    async def _handle_result(self, data: dict):
        command = data.get("command")

        if command == "startup":
            available = data.get("mt5_available")
            log.info("MT5 Worker online — mt5_available=%s", available)
            await self._redis.set("trading:mt5:online", "1", ex=120)
            await self._notify(
                f"{'✅' if available else '⚠️'} MT5 Worker tilkoblet"
                + ("" if available else " — MetaTrader5 package ikke installeret på din PC")
            )

        elif command == "pong":
            await self._redis.set("trading:mt5:online", "1", ex=120)

        elif command == "open":
            trade_id = data.get("trade_id")
            result   = data.get("result", {})
            if trade_id and trade_id in self._pending:
                self._pending.pop(trade_id).set_result(result)
            if "ticket" in result:
                await self._redis.hset("trading:mt5:tickets", trade_id, json.dumps(result))
                log.info("MT5 order executed: trade=%s ticket=%s", trade_id, result["ticket"])
            else:
                log.error("MT5 open failed for %s: %s", trade_id, result.get("error"))
                await self._notify(f"❌ MT5 order fejlede: {result.get('error')}")

        elif command == "close":
            trade_id = data.get("trade_id")
            result   = data.get("result", {})
            if trade_id and trade_id in self._pending:
                self._pending.pop(trade_id).set_result(result)
            if result.get("closed"):
                log.info("MT5 position closed: trade=%s", trade_id)
            else:
                log.error("MT5 close failed: %s", result.get("error"))

    def stop(self):
        self._running = False

    async def send_open(self, trade_id: str, symbol: str, direction: str,
                        stop_loss: float, take_profit: float,
                        volume: float = 0) -> dict:
        if not volume:
            raw = await self._redis.get("trading:lot_size")
            volume = float(raw) if raw else 0.01

        cmd = {
            "command":     "open",
            "trade_id":    trade_id,
            "symbol":      symbol,
            "direction":   direction,
            "volume":      volume,
            "stop_loss":   stop_loss,
            "take_profit": take_profit,
            "ts":          datetime.utcnow().isoformat(),
        }

        fut = asyncio.get_event_loop().create_future()
        self._pending[trade_id] = fut
        await self._redis.publish("trading:mt5:commands", json.dumps(cmd))

        try:
            return await asyncio.wait_for(fut, timeout=15)
        except asyncio.TimeoutError:
            self._pending.pop(trade_id, None)
            log.warning("MT5 open timeout for %s — is mt5_worker.py running?", trade_id)
            return {"error": "timeout — er MT5 Worker kørende på din PC?"}

    async def send_close(self, trade_id: str, symbol: str, direction: str,
                         volume: float = 0.01) -> dict:
        ticket_raw = await self._redis.hget("trading:mt5:tickets", trade_id)
        if not ticket_raw:
            return {"error": "Ingen MT5 ticket fundet for denne trade"}

        ticket_data = json.loads(ticket_raw)
        cmd = {
            "command":   "close",
            "trade_id":  trade_id,
            "symbol":    symbol,
            "direction": direction,
            "volume":    volume,
            "ticket":    ticket_data.get("ticket"),
            "ts":        datetime.utcnow().isoformat(),
        }

        fut = asyncio.get_event_loop().create_future()
        self._pending[trade_id] = fut
        await self._redis.publish("trading:mt5:commands", json.dumps(cmd))

        try:
            return await asyncio.wait_for(fut, timeout=15)
        except asyncio.TimeoutError:
            self._pending.pop(trade_id, None)
            return {"error": "timeout"}

    async def ping(self) -> bool:
        online = await self._redis.get("trading:mt5:online")
        if online:
            return True
        await self._redis.publish("trading:mt5:commands", json.dumps({"command": "ping"}))
        await asyncio.sleep(3)
        return bool(await self._redis.get("trading:mt5:online"))

    async def _notify(self, message: str):
        await self._redis.publish("supervisor:notifications", json.dumps({
            "message":    message,
            "parse_mode": "Markdown",
            "task_id":    "mt5_bridge",
        }))
