"""
MT5 Bridge — Railway side.

Sends trade commands to the MT5 Worker running on the user's Windows PC,
and listens for execution results to update position tracking.
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime

import redis.asyncio as aioredis

log = logging.getLogger(__name__)


class MT5Bridge:
    def __init__(self, redis: aioredis.Redis):
        self._redis   = redis
        self._running = False
        self._pending: dict[str, asyncio.Future] = {}

    async def run(self):
        """Listen for MT5 result messages and resolve pending futures."""
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
                    data = json.loads(msg["data"])
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
                        # Store MT5 ticket in Redis so we can close later
                        if "ticket" in result:
                            await self._redis.hset(
                                "trading:mt5:tickets",
                                trade_id,
                                json.dumps(result),
                            )
                            log.info("MT5 order executed: trade=%s ticket=%s",
                                     trade_id, result["ticket"])
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

                    elif command in ("get_account_info", "get_symbol_info", "get_tick",
                                     "check_pending", "cancel_pending", "get_rates",
                                     "modify_trade", "get_open_positions",
                                     "get_position_history"):
                        trade_id = data.get("trade_id")
                        result   = data.get("result", {})
                        if trade_id and trade_id in self._pending:
                            self._pending.pop(trade_id).set_result(result)

                except Exception as e:
                    log.warning("MT5Bridge result parse error: %s", e)
        finally:
            await pubsub.unsubscribe("trading:mt5:results")
            await pubsub.aclose()

    def stop(self):
        self._running = False

    async def send_open(self, trade_id: str, symbol: str, direction: str,
                        stop_loss: float, take_profit: float,
                        volume: float = 0, order_type: str = "market",
                        limit_price: float = 0) -> dict:
        """Send open order to MT5 worker. order_type "market" or "limit"
        (pending order at limit_price — sits until filled or cancelled)."""
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
            "order_type":  order_type,
            "limit_price": limit_price,
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

    async def check_pending(self, ticket: int) -> dict:
        """Poll a pending limit order's status: pending / filled / cancelled."""
        req_id = f"chk_{uuid.uuid4().hex[:8]}"
        cmd = {"command": "check_pending", "trade_id": req_id, "ticket": ticket,
               "ts": datetime.utcnow().isoformat()}

        fut = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        await self._redis.publish("trading:mt5:commands", json.dumps(cmd))

        try:
            return await asyncio.wait_for(fut, timeout=10)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return {"error": "timeout"}

    async def cancel_pending(self, ticket: int) -> dict:
        """Cancel a pending limit order that hasn't filled yet."""
        req_id = f"cnl_{uuid.uuid4().hex[:8]}"
        cmd = {"command": "cancel_pending", "trade_id": req_id, "ticket": ticket,
               "ts": datetime.utcnow().isoformat()}

        fut = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        await self._redis.publish("trading:mt5:commands", json.dumps(cmd))

        try:
            return await asyncio.wait_for(fut, timeout=10)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return {"error": "timeout"}

    async def send_close(self, trade_id: str, symbol: str, direction: str,
                         volume: float = 0.01) -> dict:
        """Send close order to MT5 worker."""
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

    async def get_account_info(self) -> dict:
        """Fetch live balance/equity from MT5. Returns {} if worker offline."""
        req_id = f"acct_{uuid.uuid4().hex[:8]}"
        cmd = {"command": "get_account_info", "trade_id": req_id,
               "ts": datetime.utcnow().isoformat()}

        fut = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        await self._redis.publish("trading:mt5:commands", json.dumps(cmd))

        try:
            return await asyncio.wait_for(fut, timeout=10)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return {"error": "timeout — er MT5 Worker kørende på din PC?"}

    async def get_symbol_info(self, symbol: str) -> dict:
        """Fetch contract size / tick value / volume limits for a symbol."""
        req_id = f"sym_{uuid.uuid4().hex[:8]}"
        cmd = {"command": "get_symbol_info", "trade_id": req_id, "symbol": symbol,
               "ts": datetime.utcnow().isoformat()}

        fut = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        await self._redis.publish("trading:mt5:commands", json.dumps(cmd))

        try:
            return await asyncio.wait_for(fut, timeout=10)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return {"error": "timeout — er MT5 Worker kørende på din PC?"}

    async def modify_trade(self, trade_id: str, symbol: str, new_sl: float, new_tp: float) -> dict:
        """Update SL/TP on an already-open position (e.g. auto-move to breakeven)."""
        ticket_raw = await self._redis.hget("trading:mt5:tickets", trade_id)
        if not ticket_raw:
            return {"error": "Ingen MT5 ticket fundet for denne trade"}
        ticket_data = json.loads(ticket_raw)

        req_id = f"mod_{uuid.uuid4().hex[:8]}"
        cmd = {"command": "modify_trade", "trade_id": req_id, "symbol": symbol,
               "ticket": ticket_data.get("ticket"), "new_sl": new_sl, "new_tp": new_tp,
               "ts": datetime.utcnow().isoformat()}

        fut = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        await self._redis.publish("trading:mt5:commands", json.dumps(cmd))

        try:
            return await asyncio.wait_for(fut, timeout=15)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return {"error": "timeout"}

    async def get_open_positions(self) -> dict:
        """All positions currently open on MT5 — used to reconcile against Redis."""
        req_id = f"pos_{uuid.uuid4().hex[:8]}"
        cmd = {"command": "get_open_positions", "trade_id": req_id,
               "ts": datetime.utcnow().isoformat()}

        fut = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        await self._redis.publish("trading:mt5:commands", json.dumps(cmd))

        try:
            return await asyncio.wait_for(fut, timeout=15)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return {"error": "timeout"}

    async def get_position_history(self, ticket: int) -> dict:
        """Real closing price/profit for a ticket from MT5's own deal history —
        used to reconcile positions closed outside our own SL/TP logic."""
        req_id = f"hist_{uuid.uuid4().hex[:8]}"
        cmd = {"command": "get_position_history", "trade_id": req_id, "ticket": ticket,
               "ts": datetime.utcnow().isoformat()}

        fut = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        await self._redis.publish("trading:mt5:commands", json.dumps(cmd))

        try:
            return await asyncio.wait_for(fut, timeout=10)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return {"error": "timeout"}

    async def get_tick(self, symbol: str) -> dict:
        """Current bid/ask for a symbol — used to monitor open positions."""
        req_id = f"tck_{uuid.uuid4().hex[:8]}"
        cmd = {"command": "get_tick", "trade_id": req_id, "symbol": symbol,
               "ts": datetime.utcnow().isoformat()}

        fut = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        await self._redis.publish("trading:mt5:commands", json.dumps(cmd))

        try:
            return await asyncio.wait_for(fut, timeout=10)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return {"error": "timeout"}

    async def get_rates(self, symbol: str, timeframe: str = "1h", count: int = 300) -> dict:
        """Historical OHLCV candles straight from the broker via MT5 —
        used instead of Yahoo Finance, which blocks/rate-limits Railway's
        outbound IP and starved the scanner of data on every symbol."""
        req_id = f"rts_{uuid.uuid4().hex[:8]}"
        cmd = {"command": "get_rates", "trade_id": req_id, "symbol": symbol,
               "timeframe": timeframe, "count": count,
               "ts": datetime.utcnow().isoformat()}

        fut = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        await self._redis.publish("trading:mt5:commands", json.dumps(cmd))

        try:
            return await asyncio.wait_for(fut, timeout=30)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return {"error": "timeout — er MT5 Worker kørende på din PC?"}

    async def ping(self) -> bool:
        """Check if MT5 Worker is online."""
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
