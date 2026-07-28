"""
Risk Manager — the single gatekeeper for "can we open a trade right now".

Tracks account equity (via MT5Bridge), enforces a daily-loss limit and a
total-drawdown limit that stay comfortably inside typical prop-firm rules
(5% daily / 10% total), and sizes trades as a % of equity instead of a
fixed lot. A breach is sticky: trading stays locked until the next broker
day AND an explicit /unlock_risk confirmation — it never clears itself
mid-incident.
"""
import json
import logging
import os
from datetime import datetime, timezone

import redis.asyncio as aioredis

log = logging.getLogger(__name__)

RISK_PER_TRADE_PCT        = float(os.getenv("RISK_PER_TRADE_PCT", "1.0"))
# 3% default matches Equity Edge Instant/Pro Edge's daily loss limit (3% of starting
# balance/equity) — tighter than the old 4% generic prop-firm default.
MAX_DAILY_LOSS_PCT        = float(os.getenv("RISK_MAX_DAILY_LOSS_PCT", "3.0"))
MAX_TOTAL_DRAWDOWN_PCT    = float(os.getenv("RISK_MAX_TOTAL_DRAWDOWN_PCT", "8.0"))
# Equity Edge "15% consistency score": no single trading day's profit may exceed this
# share of total net profit across the trailing payout cycle (14 calendar days) — else
# payout eligibility is at risk. Not a loss-protection rule, a payout-eligibility one.
CONSISTENCY_MAX_PCT       = float(os.getenv("RISK_CONSISTENCY_MAX_PCT", "15.0"))
CONSISTENCY_CYCLE_DAYS    = int(os.getenv("RISK_CONSISTENCY_CYCLE_DAYS", "14"))
# On a brand-new account (or right after a reset_baseline swap), day 1's profit is
# mathematically 100% of "all profit ever" simply because there's no prior history
# to divide by -- the guard would trip on literally any winning first day. Require
# at least this many PRIOR days of recorded history before enforcing the cap at all.
CONSISTENCY_MIN_HISTORY_DAYS = int(os.getenv("RISK_CONSISTENCY_MIN_HISTORY_DAYS", "3"))
# Hard ceiling in account currency — no single trade's stop-loss may ever risk more than
# this, regardless of what RISK_PER_TRADE_PCT computes. Assumes the MT5 account currency
# matches this value's currency (DKK by default) — check /risk, which shows the account's
# actual currency, and adjust this env var if the account isn't in DKK.
MAX_LOSS_PER_TRADE        = float(os.getenv("RISK_MAX_LOSS_PER_TRADE", "100"))

DAY_START_KEY       = "trading:risk:day_start_equity"
DAY_DATE_KEY        = "trading:risk:day_date"
PEAK_KEY            = "trading:risk:peak_equity"
LAST_KEY            = "trading:risk:last_equity"
LOCK_KEY            = "trading:risk:locked"
PAUSED_KEY          = "trading:paused"
CURRENCY_KEY        = "trading:risk:currency"
DAILY_PNL_KEY        = "trading:risk:daily_pnl"          # hash: {date: realized/floating P&L that day}
CONSISTENCY_PAUSE_KEY = "trading:risk:consistency_pause"  # auto-clears next broker day, unlike LOCK_KEY


class RiskManager:
    def __init__(self, redis: aioredis.Redis, db=None):
        self._redis = redis
        self._db    = db   # optional core.memory.db.Database — for equity curve persistence

    # ── Equity tracking ───────────────────────────────────────────────────────

    async def refresh_equity(self, equity: float, balance: float, currency: str = "") -> dict:
        """Call once per scan cycle with fresh account info from MT5Bridge."""
        if currency:
            await self._redis.set(CURRENCY_KEY, currency)

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        stored_date = await self._redis.get(DAY_DATE_KEY)

        if stored_date != today:
            # New broker day — archive yesterday's realized P&L for the consistency-score
            # check, reset the daily baseline, and clear any consistency pause (it's a
            # same-day-only guard, unlike the sticky drawdown LOCK_KEY which needs /unlock_risk).
            if stored_date:
                prev_start = float(await self._redis.get(DAY_START_KEY) or 0)
                prev_last  = float(await self._redis.get(LAST_KEY) or prev_start)
                if prev_start:
                    await self._redis.hset(DAILY_PNL_KEY, stored_date, prev_last - prev_start)
            await self._redis.set(DAY_DATE_KEY, today)
            await self._redis.set(DAY_START_KEY, equity)
            await self._redis.delete(CONSISTENCY_PAUSE_KEY)

        peak_raw = await self._redis.get(PEAK_KEY)
        peak = max(float(peak_raw), equity) if peak_raw else equity
        await self._redis.set(PEAK_KEY, peak)
        await self._redis.set(LAST_KEY, equity)

        day_start = float(await self._redis.get(DAY_START_KEY) or equity)
        daily_loss_pct = max(0.0, (day_start - equity) / day_start * 100) if day_start else 0.0
        drawdown_pct   = max(0.0, (peak - equity) / peak * 100) if peak else 0.0

        breach_reason = None
        if daily_loss_pct >= MAX_DAILY_LOSS_PCT:
            breach_reason = f"Daglig tab-grænse ramt: -{daily_loss_pct:.1f}% (max {MAX_DAILY_LOSS_PCT:.1f}%)"
        elif drawdown_pct >= MAX_TOTAL_DRAWDOWN_PCT:
            breach_reason = f"Max drawdown ramt: -{drawdown_pct:.1f}% fra peak (max {MAX_TOTAL_DRAWDOWN_PCT:.1f}%)"

        if breach_reason and not await self._redis.get(LOCK_KEY):
            await self._lock(breach_reason)

        # Consistency-score guard (payout eligibility, not loss protection): if today's
        # profit alone would already exceed CONSISTENCY_MAX_PCT of the trailing cycle's
        # total net profit, pause new trades for the rest of today so the imbalance
        # doesn't get worse. Clears automatically at the next broker day above.
        today_pnl = equity - day_start
        if today_pnl > 0 and not await self._redis.get(CONSISTENCY_PAUSE_KEY):
            cycle_total, cycle_days = await self._consistency_cycle_total(today, today_pnl)
            prior_days = len(cycle_days) - 1   # cycle_days always includes today itself
            if (prior_days >= CONSISTENCY_MIN_HISTORY_DAYS
                    and cycle_total > 0 and (today_pnl / cycle_total * 100) >= CONSISTENCY_MAX_PCT):
                await self._redis.set(CONSISTENCY_PAUSE_KEY, "1")
                await self._notify(
                    f"⏸️ *Consistency-guard aktiveret*\n"
                    f"Dagens profit (${today_pnl:.2f}) ville udgøre "
                    f"{today_pnl / cycle_total * 100:.0f}% af {CONSISTENCY_CYCLE_DAYS}-dages "
                    f"nettoprofit (max {CONSISTENCY_MAX_PCT:.0f}% — Equity Edge consistency score).\n"
                    f"Ingen nye trades resten af dagen. Genoptages automatisk i morgen."
                )

        if self._db:
            try:
                await self._db.save_equity_snapshot(equity, balance)
            except Exception as e:
                log.warning("Equity snapshot persist failed: %s", e)

        return {
            "equity": equity, "balance": balance,
            "day_start_equity": day_start, "peak_equity": peak,
            "daily_loss_pct": round(daily_loss_pct, 2),
            "drawdown_pct": round(drawdown_pct, 2),
            "max_daily_loss_pct": MAX_DAILY_LOSS_PCT,
            "max_drawdown_pct": MAX_TOTAL_DRAWDOWN_PCT,
            "max_loss_per_trade": MAX_LOSS_PER_TRADE,
            "currency": await self._redis.get(CURRENCY_KEY) or "?",
            "locked": bool(await self._redis.get(LOCK_KEY)),
            "paused": bool(await self._redis.get(PAUSED_KEY)),
            "consistency_paused": bool(await self._redis.get(CONSISTENCY_PAUSE_KEY)),
        }

    async def _consistency_cycle_total(self, today: str, today_pnl: float) -> tuple[float, list]:
        """Net profit across the trailing CONSISTENCY_CYCLE_DAYS calendar days, including
        today's running (unrealized-included) P&L. Returns (total, per-day list)."""
        history = await self._redis.hgetall(DAILY_PNL_KEY)
        today_dt = datetime.strptime(today, "%Y-%m-%d")
        days = [today_pnl]
        for date_str, pnl_str in history.items():
            try:
                d = datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                continue
            if 0 <= (today_dt - d).days < CONSISTENCY_CYCLE_DAYS:
                days.append(float(pnl_str))
        return sum(days), days

    # ── Gatekeeper ────────────────────────────────────────────────────────────

    async def check_can_trade(self) -> tuple[bool, str | None]:
        if await self._redis.get(PAUSED_KEY):
            return False, "Trading er manuelt sat på pause (/resume for at genoptage)"
        lock_reason = await self._redis.get(LOCK_KEY)
        if lock_reason:
            return False, f"Risk-lock aktiv: {lock_reason}"
        if await self._redis.get(CONSISTENCY_PAUSE_KEY):
            return False, "Consistency-guard aktiv (dagens profit-andel nået) — genoptages i morgen"
        return True, None

    async def status(self) -> dict:
        return {
            "equity":           float(await self._redis.get(LAST_KEY) or 0),
            "day_start_equity": float(await self._redis.get(DAY_START_KEY) or 0),
            "peak_equity":      float(await self._redis.get(PEAK_KEY) or 0),
            "locked":           await self._redis.get(LOCK_KEY),
            "paused":           bool(await self._redis.get(PAUSED_KEY)),
            "consistency_paused": bool(await self._redis.get(CONSISTENCY_PAUSE_KEY)),
            "max_daily_loss_pct":   MAX_DAILY_LOSS_PCT,
            "max_drawdown_pct":     MAX_TOTAL_DRAWDOWN_PCT,
            "consistency_max_pct":  CONSISTENCY_MAX_PCT,
            "risk_per_trade_pct":   RISK_PER_TRADE_PCT,
            "max_loss_per_trade":   MAX_LOSS_PER_TRADE,
            "currency":             await self._redis.get(CURRENCY_KEY) or "?",
        }

    async def unlock(self) -> bool:
        was_locked = bool(await self._redis.get(LOCK_KEY))
        was_consistency_paused = bool(await self._redis.get(CONSISTENCY_PAUSE_KEY))
        await self._redis.delete(LOCK_KEY)
        await self._redis.delete(CONSISTENCY_PAUSE_KEY)
        if was_locked or was_consistency_paused:
            await self._notify("🔓 *Risk-lock fjernet manuelt* — trading genoptaget.")
        return was_locked or was_consistency_paused

    async def reset_baseline(self) -> dict:
        """Re-anchor day_start/peak equity to the current live reading, AND wipe
        the consistency-score daily-P&L history — for deliberate account swaps,
        where all of that is from a different account entirely and would
        otherwise register as fake profit/loss (or fake consistency-history
        "days") against the new one. Does not touch the drawdown/daily-loss
        lock itself (that's what unlock() does)."""
        equity = float(await self._redis.get(LAST_KEY) or 0)
        if not equity:
            return {"reset": False, "reason": "no equity reading yet"}
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        await self._redis.set(DAY_START_KEY, equity)
        await self._redis.set(PEAK_KEY, equity)
        await self._redis.set(DAY_DATE_KEY, today)
        await self._redis.delete(CONSISTENCY_PAUSE_KEY)
        await self._redis.delete(DAILY_PNL_KEY)
        await self._notify(
            f"🔄 *Risk-baseline nulstillet* til nuværende equity (${equity:.2f}) — "
            f"bruges til dagligt tab / drawdown / consistency-score fremover. "
            f"Gammel consistency-historik ryddet (anden konto)."
        )
        return {"reset": True, "new_baseline": equity}

    async def _lock(self, reason: str):
        await self._redis.set(LOCK_KEY, reason)
        await self._notify(
            f"🔒 *RISK-LOCK AKTIVERET*\n{reason}\n\n"
            f"Ingen nye trades åbnes før du sender /unlock_risk efter at have "
            f"gennemgået kontoen."
        )

    # ── Position sizing ───────────────────────────────────────────────────────

    def risk_amount(self, equity: float) -> float:
        """
        Amount of account currency to risk on a single trade — the smaller of the
        %-of-equity calc and the hard MAX_LOSS_PER_TRADE ceiling, so a single stop-loss
        can never lose more than that fixed amount no matter how equity grows.
        """
        pct_based = equity * (RISK_PER_TRADE_PCT / 100)
        return min(pct_based, MAX_LOSS_PER_TRADE)

    @staticmethod
    def compute_volume(risk_amount: float, sl_distance: float, symbol_info: dict,
                      entry_price: float = 0, margin_free: float = 0, leverage: float = 0) -> float:
        """
        Lot size so that hitting the stop loses ~risk_amount, using the
        broker's own tick value/size for this symbol (correct for both
        forex pairs and XAUUSD, whose contract size differs a lot).

        Also capped by actually available margin when entry_price/margin_free/
        leverage are supplied — risk_amount alone only controls the dollar
        loss IF the stop is hit, it says nothing about whether the account
        can even afford to open that many lots in the first place. On a
        low-leverage account (e.g. 1:10 on metals) the risk-based size can
        come out well beyond what's marginable, which the broker then
        rejects outright (MT5 error 10019 "no money") instead of opening
        a smaller position.
        """
        tick_value     = symbol_info.get("trade_tick_value") or 0
        tick_size      = symbol_info.get("trade_tick_size") or 0
        contract_size  = symbol_info.get("trade_contract_size") or 0
        vol_min        = symbol_info.get("volume_min", 0.01)
        vol_max        = symbol_info.get("volume_max", 100.0)
        vol_step       = symbol_info.get("volume_step", 0.01)

        if not tick_value or not tick_size or sl_distance <= 0:
            return vol_min

        value_per_unit_per_lot = tick_value / tick_size  # account currency per 1.0 price unit per 1 lot
        loss_per_lot = sl_distance * value_per_unit_per_lot
        if loss_per_lot <= 0:
            return vol_min

        volume = risk_amount / loss_per_lot

        if entry_price > 0 and margin_free > 0 and leverage > 0 and contract_size > 0:
            margin_per_lot = (contract_size * entry_price) / leverage
            if margin_per_lot > 0:
                # only ever use a portion of free margin for one new position —
                # leaves room for spread/slippage vs. this estimate and for
                # other positions/safety buffer
                max_volume_by_margin = (margin_free * 0.8) / margin_per_lot
                volume = min(volume, max_volume_by_margin)

        volume = max(vol_min, min(vol_max, volume))
        # round down to nearest step so we never risk more than intended
        steps = int(volume / vol_step)
        volume = round(steps * vol_step, 8)
        return max(volume, vol_min)

    # ── Notifications ─────────────────────────────────────────────────────────

    async def _notify(self, message: str):
        await self._redis.publish("supervisor:notifications", json.dumps({
            "message": message, "parse_mode": "Markdown", "task_id": "risk_manager",
        }))
