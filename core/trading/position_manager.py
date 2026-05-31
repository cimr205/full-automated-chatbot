"""
Position Manager — tracks all open and closed trades in Redis.

Supports both manually logged trades (user already in a trade)
and auto-executed trades from the signal engine.
Monitors open positions every 60s and alerts on SL/TP hit.
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

import redis.asyncio as aioredis

log = logging.getLogger(__name__)

POSITIONS_KEY  = "trading:positions"          # Redis hash: trade_id → JSON
HISTORY_KEY    = "trading:trade_history"      # Redis list: closed trades
MONITOR_INTERVAL = 60                          # seconds between price checks


class PositionManager:
    def __init__(self, redis: aioredis.Redis):
        self._redis   = redis
        self._running = False

    # ── Public API ────────────────────────────────────────────────────────────

    async def open_trade(
        self,
        symbol:     str,
        market:     str,
        direction:  str,
        entry:      float,
        stop_loss:  float,
        take_profit: float,
        partial_tp: float   = 0,
        size:       float   = 0,        # 0 = not specified
        signal_data: dict   = None,     # full signal dict for reasoning
        source:     str     = "auto",   # "auto" | "manual"
    ) -> str:
        trade_id = f"trade_{uuid.uuid4().hex[:8]}"
        trade = {
            "trade_id":   trade_id,
            "symbol":     symbol,
            "market":     market,
            "direction":  direction,
            "entry":      entry,
            "stop_loss":  stop_loss,
            "take_profit": take_profit,
            "partial_tp": partial_tp,
            "size":       size,
            "status":     "open",
            "source":     source,
            "opened_at":  datetime.utcnow().isoformat(),
            "pnl_r":      0.0,
            "partial_taken": False,
            # Reasoning — persisted so user can ask "why did you enter?"
            "reasoning": {
                "setup":      signal_data.get("setup_type")  if signal_data else None,
                "session":    signal_data.get("session")     if signal_data else None,
                "confidence": signal_data.get("confidence")  if signal_data else None,
                "confluence": signal_data.get("confluence")  if signal_data else None,
                "checklist":  signal_data.get("checklist")   if signal_data else None,
                "indicators": signal_data.get("reasons", []) if signal_data else [],
                "rr_planned": signal_data.get("rr_ratio")    if signal_data else None,
            },
        }
        await self._redis.hset(POSITIONS_KEY, trade_id, json.dumps(trade))
        log.info("Trade opened: %s %s %s @ %s  SL=%s  TP=%s",
                 trade_id, symbol, direction, entry, stop_loss, take_profit)
        return trade_id

    async def close_trade(self, trade_id: str, exit_price: float, reason: str = "manual") -> dict | None:
        raw = await self._redis.hget(POSITIONS_KEY, trade_id)
        if not raw:
            return None
        trade = json.loads(raw)
        entry = trade["entry"]
        sl    = trade["stop_loss"]
        risk  = abs(entry - sl)

        if trade["direction"] == "long":
            pnl_r = (exit_price - entry) / risk if risk else 0
        else:
            pnl_r = (entry - exit_price) / risk if risk else 0

        trade.update({
            "status":     "closed",
            "exit_price": exit_price,
            "exit_reason": reason,
            "closed_at":  datetime.utcnow().isoformat(),
            "pnl_r":      round(pnl_r, 2),
        })

        await self._redis.hdel(POSITIONS_KEY, trade_id)
        await self._redis.lpush(HISTORY_KEY, json.dumps(trade))
        await self._redis.ltrim(HISTORY_KEY, 0, 999)
        log.info("Trade closed: %s %s @ %s  P&L: %.2fR", trade_id, reason, exit_price, pnl_r)
        return trade

    async def list_open(self) -> list[dict]:
        raw = await self._redis.hgetall(POSITIONS_KEY)
        trades = []
        for v in raw.values():
            try:
                trades.append(json.loads(v))
            except Exception:
                pass
        return sorted(trades, key=lambda t: t.get("opened_at", ""), reverse=True)

    async def get_trade(self, trade_id: str) -> dict | None:
        raw = await self._redis.hget(POSITIONS_KEY, trade_id)
        return json.loads(raw) if raw else None

    async def history(self, limit: int = 50) -> list[dict]:
        raw = await self._redis.lrange(HISTORY_KEY, 0, limit - 1)
        return [json.loads(r) for r in raw]

    async def stats(self) -> dict:
        trades = await self.history(200)
        if not trades:
            return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0, "avg_rr": 0, "total_r": 0}
        wins   = [t for t in trades if t.get("pnl_r", 0) > 0]
        losses = [t for t in trades if t.get("pnl_r", 0) <= 0]
        total_r = sum(t.get("pnl_r", 0) for t in trades)
        return {
            "total":    len(trades),
            "wins":     len(wins),
            "losses":   len(losses),
            "win_rate": len(wins) / len(trades) if trades else 0,
            "avg_rr":   total_r / len(trades) if trades else 0,
            "total_r":  round(total_r, 2),
        }

    # ── Position monitor ─────────────────────────────────────────────────────

    async def run(self):
        """Background loop: check open positions against latest prices."""
        self._running = True
        log.info("PositionManager monitor started")
        while self._running:
            try:
                await self._check_positions()
            except Exception as e:
                log.error("Position check error: %s", e)
            await asyncio.sleep(MONITOR_INTERVAL)

    def stop(self):
        self._running = False

    async def _check_positions(self):
        trades = await self.list_open()
        if not trades:
            return

        import httpx
        for trade in trades:
            symbol = trade["symbol"]
            try:
                price = await self._get_current_price(symbol)
                if not price:
                    continue
                await self._evaluate_position(trade, price)
            except Exception as e:
                log.warning("[%s] position check: %s", symbol, e)

    async def _get_current_price(self, symbol: str) -> float | None:
        import httpx
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            f"?interval=1m&range=5m&includePrePost=false"
        )
        try:
            async with httpx.AsyncClient(timeout=10,
                                          headers={"User-Agent": "Mozilla/5.0"}) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
            result  = data.get("chart", {}).get("result", [])
            if not result:
                return None
            closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
            closes = [c for c in closes if c is not None]
            return closes[-1] if closes else None
        except Exception:
            return None

    async def _evaluate_position(self, trade: dict, price: float):
        trade_id  = trade["trade_id"]
        entry     = trade["entry"]
        sl        = trade["stop_loss"]
        tp        = trade["take_profit"]
        partial   = trade.get("partial_tp", 0)
        direction = trade["direction"]
        symbol    = trade["symbol"]
        partial_taken = trade.get("partial_taken", False)

        # Calculate risk unit
        risk = abs(entry - sl)
        if risk == 0:
            return

        if direction == "long":
            current_r = (price - entry) / risk
            sl_hit = price <= sl
            tp_hit = price >= tp
            partial_hit = partial and not partial_taken and price >= partial
        else:
            current_r = (entry - price) / risk
            sl_hit = price >= sl
            tp_hit = price <= tp
            partial_hit = partial and not partial_taken and price <= partial

        # SL hit — close trade
        if sl_hit:
            closed = await self.close_trade(trade_id, price, "stop_loss")
            await self._notify_close(closed, "🛑 Stop Loss ramt")
            return

        # TP hit — close trade
        if tp_hit:
            closed = await self.close_trade(trade_id, price, "take_profit")
            await self._notify_close(closed, "🎯 Take Profit ramt")
            return

        # Partial profit level hit — notify (user moves SL to BE manually)
        if partial_hit:
            trade["partial_taken"] = True
            await self._redis.hset(POSITIONS_KEY, trade_id, json.dumps(trade))
            await self._notify_partial(trade, price)

    async def _notify_close(self, trade: dict, reason: str):
        if not trade:
            return
        pnl_r  = trade.get("pnl_r", 0)
        symbol = trade["symbol"]
        won    = pnl_r > 0
        emoji  = "💚" if won else "❤️"
        result = "WIN" if won else "LOSS"

        def fmt(v): return f"{v:,.5f}" if abs(v) < 10 else f"{v:,.2f}"

        msg = (
            f"{emoji} *TRADE LUKKET — {result}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"*{symbol}*  {trade['direction'].upper()}\n"
            f"{reason}\n\n"
            f"Entry:  `{fmt(trade['entry'])}`\n"
            f"Exit:   `{fmt(trade.get('exit_price', 0))}`\n"
            f"P&L:    `{pnl_r:+.2f}R`\n\n"
            f"_Åbnet: {trade.get('opened_at', '')[:16]}_\n"
            f"_Lukket: {trade.get('closed_at', '')[:16]}_"
        )
        await self._redis.publish("supervisor:notifications", json.dumps({
            "message":    msg,
            "parse_mode": "Markdown",
            "task_id":    f"trade_close_{trade['trade_id']}",
        }))

    async def _notify_partial(self, trade: dict, price: float):
        symbol = trade["symbol"]
        def fmt(v): return f"{v:,.5f}" if abs(v) < 10 else f"{v:,.2f}"
        msg = (
            f"💛 *DELVIS PROFIT — {symbol}*\n"
            f"Pris har ramt 1.5R-målet ved `{fmt(price)}`\n\n"
            f"✅ Tag halv position ud\n"
            f"✅ Flyt stop loss til break even (`{fmt(trade['entry'])}`)\n"
            f"🎯 Lad resten løbe til fuldt target: `{fmt(trade['take_profit'])}`"
        )
        await self._redis.publish("supervisor:notifications", json.dumps({
            "message":    msg,
            "parse_mode": "Markdown",
            "task_id":    f"partial_{trade['trade_id']}",
        }))
