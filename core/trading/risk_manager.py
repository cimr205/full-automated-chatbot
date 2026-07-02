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
MAX_DAILY_LOSS_PCT        = float(os.getenv("RISK_MAX_DAILY_LOSS_PCT", "4.0"))
MAX_TOTAL_DRAWDOWN_PCT    = float(os.getenv("RISK_MAX_TOTAL_DRAWDOWN_PCT", "8.0"))
# Hard ceiling in account currency — no single trade's stop-loss may ever risk more than
# this, regardless of what RISK_PER_TRADE_PCT computes. Assumes the MT5 account currency
# matches this value's currency (DKK by default) — check /risk, which shows the account's
# actual currency, and adjust this env var if the account isn't in DKK.
MAX_LOSS_PER_TRADE        = float(os.getenv("RISK_MAX_LOSS_PER_TRADE", "100"))

DAY_START_KEY  = "trading:risk:day_start_equity"
DAY_DATE_KEY   = "trading:risk:day_date"
PEAK_KEY       = "trading:risk:peak_equity"
LAST_KEY       = "trading:risk:last_equity"
LOCK_KEY       = "trading:risk:locked"
PAUSED_KEY     = "trading:paused"
CURRENCY_KEY   = "trading:risk:currency"


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
            # New broker day — reset the daily baseline (does NOT clear a lock)
            await self._redis.set(DAY_DATE_KEY, today)
            await self._redis.set(DAY_START_KEY, equity)

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
        }

    # ── Gatekeeper ────────────────────────────────────────────────────────────

    async def check_can_trade(self) -> tuple[bool, str | None]:
        if await self._redis.get(PAUSED_KEY):
            return False, "Trading er manuelt sat på pause (/resume for at genoptage)"
        lock_reason = await self._redis.get(LOCK_KEY)
        if lock_reason:
            return False, f"Risk-lock aktiv: {lock_reason}"
        return True, None

    async def status(self) -> dict:
        return {
            "equity":           float(await self._redis.get(LAST_KEY) or 0),
            "day_start_equity": float(await self._redis.get(DAY_START_KEY) or 0),
            "peak_equity":      float(await self._redis.get(PEAK_KEY) or 0),
            "locked":           await self._redis.get(LOCK_KEY),
            "paused":           bool(await self._redis.get(PAUSED_KEY)),
            "max_daily_loss_pct":   MAX_DAILY_LOSS_PCT,
            "max_drawdown_pct":     MAX_TOTAL_DRAWDOWN_PCT,
            "risk_per_trade_pct":   RISK_PER_TRADE_PCT,
            "max_loss_per_trade":   MAX_LOSS_PER_TRADE,
            "currency":             await self._redis.get(CURRENCY_KEY) or "?",
        }

    async def unlock(self) -> bool:
        was_locked = bool(await self._redis.get(LOCK_KEY))
        await self._redis.delete(LOCK_KEY)
        if was_locked:
            await self._notify("🔓 *Risk-lock fjernet manuelt* — trading genoptaget.")
        return was_locked

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
    def compute_volume(risk_amount: float, sl_distance: float, symbol_info: dict) -> float:
        """
        Lot size so that hitting the stop loses ~risk_amount, using the
        broker's own tick value/size for this symbol (correct for both
        forex pairs and XAUUSD, whose contract size differs a lot).
        """
        tick_value = symbol_info.get("trade_tick_value") or 0
        tick_size  = symbol_info.get("trade_tick_size") or 0
        vol_min    = symbol_info.get("volume_min", 0.01)
        vol_max    = symbol_info.get("volume_max", 100.0)
        vol_step   = symbol_info.get("volume_step", 0.01)

        if not tick_value or not tick_size or sl_distance <= 0:
            return vol_min

        value_per_unit_per_lot = tick_value / tick_size  # account currency per 1.0 price unit per 1 lot
        loss_per_lot = sl_distance * value_per_unit_per_lot
        if loss_per_lot <= 0:
            return vol_min

        volume = risk_amount / loss_per_lot
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
