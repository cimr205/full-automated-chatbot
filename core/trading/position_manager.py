from __future__ import annotations

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

from . import learning, retrospective
from .signal_engine import PARTIAL_R

log = logging.getLogger(__name__)

POSITIONS_KEY  = "trading:positions"          # Redis hash: trade_id → JSON
HISTORY_KEY    = "trading:trade_history"      # Redis list: closed trades
MONITOR_INTERVAL = 60                          # seconds between price checks
PARTIAL_CLOSE_FRACTION = 0.5    # fraction of the position actually banked at the 1.5R partial level
MIN_CLOSE_VOLUME       = 0.01   # broker's typical minimum lot — below this a partial split isn't possible


class PositionManager:
    def __init__(self, redis: aioredis.Redis):
        self._redis   = redis
        self._running = False
        self._mt5     = None   # injected by MarketMonitor after both are created

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
        order_type: str     = "market", # "market" | "limit"
        trade_id:   str     = None,     # supply when MT5 needs the same ID for correlation
        paper:      bool    = False,    # True = simulated fill (LIVE_TRADING=false) -- no real broker position exists
    ) -> str:
        trade_id = trade_id or f"trade_{uuid.uuid4().hex[:8]}"
        trade = {
            "trade_id":   trade_id,
            "symbol":     symbol,
            "market":     market,
            "direction":  direction,
            "paper":      paper,
            "entry":      entry,
            "stop_loss":  stop_loss,
            # Immutable copy of the opening stop — "stop_loss" above gets moved
            # to breakeven on partial-profit, and R-multiple math must always
            # be against the ORIGINAL risk distance, not whatever the live
            # protective stop currently sits at (see close_trade()).
            "initial_stop_loss": stop_loss,
            "take_profit": take_profit,
            "partial_tp": partial_tp,
            "size":       size,
            # "pending" = limit order placed but not yet filled — not monitored
            # for SL/TP until it actually opens. No auto-expiry; stays until
            # filled or manually cancelled via /cancel.
            "status":     "pending" if order_type == "limit" else "open",
            "order_type": order_type,
            "source":     source,
            "opened_at":  datetime.utcnow().isoformat(),
            "pnl_r":      0.0,
            "partial_taken": False,
            # Reasoning — persisted so user can ask "why did you enter?"
            "reasoning": {
                "setup":      signal_data.get("setup_type")  if signal_data else None,
                "setup_label": (signal_data.get("setup_label") or signal_data.get("setup_type"))
                               if signal_data else None,
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
        # Original risk distance, not the live stop (which partial-profit moves
        # to breakeven — using that here would make risk=0 and misreport every
        # trade that reached breakeven as a flat 0R regardless of where it
        # actually closed).
        sl    = trade.get("initial_stop_loss", trade["stop_loss"])
        risk  = abs(entry - sl)

        if trade["direction"] == "long":
            final_leg_r = (exit_price - entry) / risk if risk else 0
        else:
            final_leg_r = (entry - exit_price) / risk if risk else 0

        # If a real partial close already banked profit at the 1.5R level,
        # blend it with the remainder's final result, weighted by volume —
        # otherwise a runner that gives back to breakeven erases the real,
        # already-realized gain from the partial.
        partial_vol = trade.get("partial_closed_volume") or 0
        original_size = trade.get("size") or 0
        if partial_vol and original_size:
            partial_frac   = min(partial_vol / original_size, 1.0)
            remainder_frac = 1 - partial_frac
            pnl_r = partial_frac * trade["partial_closed_r"] + remainder_frac * final_leg_r
        else:
            pnl_r = final_leg_r

        closed_at = datetime.utcnow()
        opened_ts = trade.get("filled_at") or trade.get("opened_at")
        duration_min = None
        if opened_ts:
            try:
                duration_min = (closed_at - datetime.fromisoformat(opened_ts)).total_seconds() / 60
            except ValueError:
                pass

        trade.update({
            "status":      "closed",
            "exit_price":  exit_price,
            "exit_reason": reason,
            "closed_at":   closed_at.isoformat(),
            "pnl_r":       round(pnl_r, 2),
            "duration_min": round(duration_min, 1) if duration_min is not None else None,
        })

        await self._redis.hdel(POSITIONS_KEY, trade_id)
        await self._redis.lpush(HISTORY_KEY, json.dumps(trade))
        await self._redis.ltrim(HISTORY_KEY, 0, 999)
        log.info("Trade closed: %s %s @ %s  P&L: %.2fR  duration: %s min",
                  trade_id, reason, exit_price, pnl_r, duration_min)

        try:
            await self._check_compliance(trade, pnl_r, duration_min)
        except Exception as e:
            log.warning("Compliance check failed: %s", e)

        try:
            await learning.record_outcome(self._redis, trade)
        except Exception as e:
            log.warning("Learning record failed: %s", e)

        try:
            await retrospective.analyse_trade(self._redis, trade)
        except Exception as e:
            log.warning("Retrospective analyse failed: %s", e)

        return trade

    async def _check_compliance(self, trade: dict, pnl_r: float, duration_min: float | None):
        """Equity Edge payout-eligibility flags — these can't be prevented after the
        fact, only surfaced so they're not a surprise at payout time:
          - trades under 2 min: profit gets deducted at payout
          - largest loss exceeding largest win: a hard payout-eligibility rule
        Tracked in R-multiples (position sizing keeps risk ~constant, so R is a fair
        proxy for dollar comparison without needing a separate P&L reconstruction)."""
        if duration_min is not None and duration_min < 2 and pnl_r > 0:
            await self._notify(
                f"⚠️ *Trade under 2 min* ({duration_min:.1f} min, {trade['symbol']})\n"
                f"Profit fra denne trade bliver trukket fra ved payout (Equity Edge-regel)."
            )

        largest_win  = float(await self._redis.get("trading:compliance:largest_win_r") or 0)
        largest_loss = float(await self._redis.get("trading:compliance:largest_loss_r") or 0)
        if pnl_r > largest_win:
            await self._redis.set("trading:compliance:largest_win_r", pnl_r)
        elif pnl_r < 0 and abs(pnl_r) > largest_loss:
            await self._redis.set("trading:compliance:largest_loss_r", abs(pnl_r))
            if abs(pnl_r) > largest_win:
                await self._notify(
                    f"⚠️ *Største tab overstiger største gevinst*\n"
                    f"Tab: {abs(pnl_r):.2f}R vs. hidtidig største gevinst: {largest_win:.2f}R\n"
                    f"Bryder Equity Edge's regel om at største tab ikke må overstige største gevinst."
                )

    async def _notify(self, message: str):
        await self._redis.publish("supervisor:notifications", json.dumps({
            "message": message, "parse_mode": "Markdown", "task_id": "compliance",
        }))

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

    async def list_pending(self) -> list[dict]:
        return [t for t in await self.list_open() if t.get("status") == "pending"]

    async def mark_filled(self, trade_id: str, fill_price: float) -> dict | None:
        """A pending limit order got filled — it's now a real open position."""
        raw = await self._redis.hget(POSITIONS_KEY, trade_id)
        if not raw:
            return None
        trade = json.loads(raw)
        trade["status"]    = "open"
        trade["entry"]     = fill_price
        trade["filled_at"] = datetime.utcnow().isoformat()
        await self._redis.hset(POSITIONS_KEY, trade_id, json.dumps(trade))
        log.info("Pending order filled: %s @ %s", trade_id, fill_price)
        await self._notify_filled(trade)
        return trade

    async def cancel_pending(self, trade_id: str, reason: str = "cancelled") -> dict | None:
        """A pending limit order was cancelled/expired before filling — never became a real trade."""
        raw = await self._redis.hget(POSITIONS_KEY, trade_id)
        if not raw:
            return None
        trade = json.loads(raw)
        await self._redis.hdel(POSITIONS_KEY, trade_id)
        log.info("Pending order cancelled: %s (%s)", trade_id, reason)
        await self._notify_cancelled(trade, reason)
        return trade

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
        # Only SL/TP/partial-profit hits notify Telegram (real trade events).
        # A per-cycle "still open, current R" digest used to fire every ~60s
        # here too — removed after it produced hundreds of messages overnight
        # for a single trade left open. Use /trades on demand instead.
        trades = [t for t in await self.list_open() if t.get("status") == "open"]
        if not trades:
            return

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
        """Current price via MT5 (bid/ask midpoint) — replaces Yahoo Finance,
        which blocks/rate-limits Railway's outbound IP. The broker enforces
        the real SL/TP on its own regardless of this check; this just drives
        Telegram close-notifications, auto-breakeven, and learning."""
        if self._mt5 is None:
            return None
        try:
            tick = await self._mt5.get_tick(symbol)
            if "error" in tick:
                log.debug("[%s] MT5 tick fetch failed: %s", symbol, tick["error"])
                return None
            return (tick["bid"] + tick["ask"]) / 2
        except Exception as e:
            log.warning("[%s] MT5 price fetch failed: %s", symbol, e)
            return None

    async def _evaluate_position(self, trade: dict, price: float):
        trade_id  = trade["trade_id"]
        entry     = trade["entry"]
        sl        = trade["stop_loss"]
        tp        = trade["take_profit"]
        partial   = trade.get("partial_tp", 0)
        direction = trade["direction"]
        partial_taken = trade.get("partial_taken", False)

        # Calculate risk unit
        risk = abs(entry - sl)
        if risk == 0:
            return

        if direction == "long":
            sl_hit = price <= sl
            tp_hit = price >= tp
            partial_hit = partial and not partial_taken and price >= partial
        else:
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

        # Partial profit level hit. Two things happen, in order:
        # 1) Actually bank real profit: close half the position on MT5 at the
        #    current price. This used to ONLY move the stop to breakeven and
        #    leave 100% of the size riding toward the (often far, sometimes
        #    never-reached) full target — so a trade that went the right
        #    direction but never reached the full TP banked nothing and often
        #    ended up back at breakeven for a real 0R result. Closing half here
        #    means the 1.5R is real money regardless of what the runner does.
        # 2) Move SL to breakeven on the remainder, same as before.
        # Both are attempted independently — a partial-close failure (e.g. the
        # position is already at the broker's minimum lot and can't be split)
        # must not block the breakeven protection, and vice versa.
        if partial_hit:
            # Guard on partial_closed_volume specifically, not partial_taken —
            # partial_taken also covers the breakeven move below, and the two
            # can succeed/fail independently across retries. Using partial_taken
            # here would re-close another half of an already-halved position
            # if the close succeeded on one cycle but the breakeven move
            # failed and got retried on the next.
            if trade.get("partial_closed_volume") is None:
                partial_volume = 0.0
                close_result = {"error": "MT5 bridge ikke tilgængelig"}
                if trade.get("paper"):
                    # No real broker position exists for a paper trade -- simulate
                    # the partial close locally instead of calling MT5 at all.
                    partial_volume = round((trade.get("size") or 1.0) * PARTIAL_CLOSE_FRACTION, 2)
                    close_result = {"paper": True}
                elif self._mt5:
                    try:
                        ticket_raw = await self._redis.hget("trading:mt5:tickets", trade_id)
                        ticket = json.loads(ticket_raw).get("ticket") if ticket_raw else None
                        positions = await self._mt5.get_open_positions()
                        live_vol = next(
                            (p.get("volume") for p in positions.get("positions", [])
                             if p.get("ticket") == ticket),
                            None,
                        )
                        if live_vol:
                            partial_volume = round(live_vol * PARTIAL_CLOSE_FRACTION, 2)
                            if MIN_CLOSE_VOLUME <= partial_volume < live_vol:
                                close_result = await self._mt5.send_close(
                                    trade_id=trade_id, symbol=trade["symbol"],
                                    direction=direction, volume=partial_volume,
                                )
                            else:
                                close_result = {"error": f"partial volume {partial_volume} ikke splitbar (live={live_vol})"}
                    except Exception as e:
                        close_result = {"error": str(e)}

                if "error" not in close_result:
                    trade["partial_closed_volume"] = partial_volume
                    trade["partial_closed_r"] = PARTIAL_R
                    # Persist immediately — this must survive even if the
                    # breakeven move below fails, so a retry next cycle never
                    # re-closes another half of the already-halved position.
                    await self._redis.hset(POSITIONS_KEY, trade_id, json.dumps(trade))
                    await self._notify(
                        f"💰 *Delvis profit hjemtaget* — {trade['symbol']}\n"
                        f"Lukkede {partial_volume} lots @ 1.5R (`{price:,.2f}`), "
                        f"resten kører videre mod fuldt target."
                    )
                    log.info("[%s] Real partial close: %s lots @ %s", trade["symbol"], partial_volume, price)
                else:
                    log.info("[%s] Partial close skipped/failed (%s) — breakeven-only", trade["symbol"], close_result["error"])

            mod_result = {"error": "MT5 bridge ikke tilgængelig"}
            if trade.get("paper"):
                # Same reasoning as the partial close above -- nothing real to
                # modify on a broker, the breakeven move is purely local bookkeeping.
                mod_result = {"paper": True}
            elif self._mt5:
                try:
                    mod_result = await self._mt5.modify_trade(
                        trade_id=trade_id,
                        symbol=trade["symbol"],
                        new_sl=entry,
                        new_tp=trade["take_profit"],
                    )
                except Exception as e:
                    mod_result = {"error": str(e)}

            if "error" not in mod_result:
                trade["partial_taken"] = True
                trade["stop_loss"] = entry   # update local record to BE
                await self._redis.hset(POSITIONS_KEY, trade_id, json.dumps(trade))
                await self._notify_partial(trade, price)
                await self._redis.delete(f"trading:be_alert_sent:{trade_id}")
                log.info("[%s] SL moved to breakeven @ %s on MT5", trade["symbol"], entry)
            else:
                log.warning("[%s] Auto-BE move failed on MT5: %s", trade["symbol"], mod_result["error"])
                alert_key = f"trading:be_alert_sent:{trade_id}"
                if not await self._redis.get(alert_key):
                    await self._redis.set(alert_key, "1", ex=3600)
                    await self._notify(
                        f"⚠️ *Auto-breakeven fejlede* — {trade['symbol']} ramte 1.5R, men MT5 "
                        f"afviste SL-flytningen: {mod_result['error']}\n"
                        f"Den rigtige stop loss på kontoen er STADIG den oprindelige, ikke breakeven. "
                        f"Botten prøver automatisk igen hvert minut — tjek positionen manuelt hvis det "
                        f"bliver ved med at fejle."
                    )

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
            f"P&L:    `{pnl_r:+.2f}R`\n"
            f"Varighed: `{trade.get('duration_min', '?')} min`\n\n"
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

    async def _notify_filled(self, trade: dict):
        def fmt(v): return f"{v:,.5f}" if abs(v) < 10 else f"{v:,.2f}"
        msg = (
            f"✅ *LIMIT-ORDER UDFYLDT — {trade['symbol']}*\n"
            f"{trade['direction'].upper()} @ `{fmt(trade['entry'])}`\n"
            f"SL: `{fmt(trade['stop_loss'])}`  TP: `{fmt(trade['take_profit'])}`\n"
            f"Trade ID: `{trade['trade_id']}`"
        )
        await self._redis.publish("supervisor:notifications", json.dumps({
            "message": msg, "parse_mode": "Markdown",
            "task_id": f"filled_{trade['trade_id']}",
        }))

    async def _notify_cancelled(self, trade: dict, reason: str):
        def fmt(v): return f"{v:,.5f}" if abs(v) < 10 else f"{v:,.2f}"
        msg = (
            f"🚫 *Limit-order annulleret — {trade['symbol']}*\n"
            f"{trade['direction'].upper()} @ `{fmt(trade['entry'])}` ({reason})\n"
            f"Trade ID: `{trade['trade_id']}`"
        )
        await self._redis.publish("supervisor:notifications", json.dumps({
            "message": msg, "parse_mode": "Markdown",
            "task_id": f"cancelled_{trade['trade_id']}",
        }))
