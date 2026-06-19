"""
Tracks open/closed trades in Redis and watches SL/TP on a background loop.
Sends Telegram notifications through the existing `supervisor:notifications`
pubsub channel (the Telegram bot already listens to it).
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime

import redis.asyncio as aioredis

log = logging.getLogger(__name__)

POSITIONS_KEY = "trading:positions"
HISTORY_KEY   = "trading:trade_history"
CHECK_INTERVAL = 60


class PositionManager:
    def __init__(self, redis: aioredis.Redis):
        self._redis = redis
        self._running = False

    # ── Open / close ─────────────────────────────────────────────────────────

    async def open_trade(self, symbol: str, market: str, direction: str,
                         entry: float, sl: float, tp: float,
                         partial_tp: float | None = None,
                         signal_data: dict | None = None,
                         source: str = "auto") -> str:
        trade_id = f"trd_{uuid.uuid4().hex[:10]}"
        trade = {
            "trade_id":   trade_id,
            "symbol":     symbol,
            "market":     market,
            "direction":  direction,
            "entry":      entry,
            "stop_loss":  sl,
            "take_profit": tp,
            "partial_tp": partial_tp,
            "status":     "open",
            "source":     source,
            "opened_at":  datetime.utcnow().isoformat(),
            "reasoning":  signal_data or {},
        }
        await self._redis.hset(POSITIONS_KEY, trade_id, json.dumps(trade))
        log.info("Trade opened: %s %s %s @ %s", trade_id, symbol, direction, entry)
        return trade_id

    async def close_trade(self, trade_id: str, exit_price: float, reason: str = "manual") -> dict:
        raw = await self._redis.hget(POSITIONS_KEY, trade_id)
        if not raw:
            return {"error": "Trade ikke fundet"}
        trade = json.loads(raw)
        if trade["status"] != "open":
            return {"error": "Trade er allerede lukket"}

        entry, sl = trade["entry"], trade["stop_loss"]
        risk = abs(entry - sl) or 1e-9
        direction = trade["direction"]
        pnl_price = (exit_price - entry) if direction == "long" else (entry - exit_price)
        r_multiple = round(pnl_price / risk, 2)

        trade.update({
            "status":     "closed",
            "exit_price": exit_price,
            "closed_at":  datetime.utcnow().isoformat(),
            "close_reason": reason,
            "r_multiple": r_multiple,
        })
        await self._redis.hdel(POSITIONS_KEY, trade_id)
        await self._redis.lpush(HISTORY_KEY, json.dumps(trade))
        await self._redis.ltrim(HISTORY_KEY, 0, 999)

        await self._notify(
            f"{'✅' if r_multiple > 0 else '❌'} *{trade['symbol']}* lukket ({reason})\n"
            f"R: `{r_multiple:+.2f}R` · Exit: `{exit_price}`"
        )
        return trade

    async def list_open(self) -> list[dict]:
        raw = await self._redis.hgetall(POSITIONS_KEY)
        return sorted(
            (json.loads(v) for v in raw.values()),
            key=lambda t: t.get("opened_at", ""), reverse=True,
        )

    async def get_trade(self, trade_id: str) -> dict | None:
        raw = await self._redis.hget(POSITIONS_KEY, trade_id)
        if raw:
            return json.loads(raw)
        for t in await self.history(limit=500):
            if t["trade_id"] == trade_id:
                return t
        return None

    async def history(self, limit: int = 50) -> list[dict]:
        raw = await self._redis.lrange(HISTORY_KEY, 0, limit - 1)
        return [json.loads(r) for r in raw]

    async def stats(self) -> dict:
        trades = await self.history(limit=500)
        if not trades:
            return {"total": 0, "win_rate": 0, "avg_rr": 0, "total_r": 0}
        wins = [t for t in trades if t.get("r_multiple", 0) > 0]
        total_r = sum(t.get("r_multiple", 0) for t in trades)
        return {
            "total":    len(trades),
            "wins":     len(wins),
            "win_rate": round(len(wins) / len(trades) * 100, 1),
            "avg_rr":   round(total_r / len(trades), 2),
            "total_r":  round(total_r, 2),
        }

    # ── Background SL/TP watcher ─────────────────────────────────────────────

    async def run(self):
        self._running = True
        while self._running:
            try:
                await self._check_positions()
            except Exception as e:
                log.error("Position check error: %s", e)
            await asyncio.sleep(CHECK_INTERVAL)

    def stop(self):
        self._running = False

    async def _check_positions(self):
        from .market_data import get_last_price  # local import avoids circular import

        open_trades = await self.list_open()
        for trade in open_trades:
            try:
                price = await get_last_price(trade["symbol"])
                if price is None:
                    continue
                direction = trade["direction"]
                sl, tp = trade["stop_loss"], trade["take_profit"]
                partial = trade.get("partial_tp")

                hit_sl = (price <= sl) if direction == "long" else (price >= sl)
                hit_tp = (price >= tp) if direction == "long" else (price <= tp)

                if hit_sl:
                    await self.close_trade(trade["trade_id"], price, reason="stop_loss")
                elif hit_tp:
                    await self.close_trade(trade["trade_id"], price, reason="take_profit")
                elif partial and not trade.get("partial_hit"):
                    hit_partial = (price >= partial) if direction == "long" else (price <= partial)
                    if hit_partial:
                        trade["partial_hit"] = True
                        await self._redis.hset(POSITIONS_KEY, trade["trade_id"], json.dumps(trade))
                        await self._notify(
                            f"📈 *{trade['symbol']}* nåede partial profit (1.5R)\n"
                            f"Overvej at sikre noget af gevinsten."
                        )
            except Exception as e:
                log.warning("Position check failed for %s: %s", trade.get("trade_id"), e)

    async def _notify(self, message: str):
        await self._redis.publish("supervisor:notifications", json.dumps({
            "message":    message,
            "parse_mode": "Markdown",
            "task_id":    "trading",
        }))
