"""
Daily/weekly performance reports — sent to Telegram via the same
supervisor:notifications Redis channel used everywhere else.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

import redis.asyncio as aioredis

from .position_manager import PositionManager
from .risk_manager import RiskManager

log = logging.getLogger(__name__)

LAST_REPORT_DATE_KEY = "trading:report:last_date"


async def maybe_send_daily_report(redis: aioredis.Redis, positions: PositionManager, risk: RiskManager):
    """Call once per scan cycle. Sends yesterday's report exactly once when the UTC date rolls over."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    last_date = await redis.get(LAST_REPORT_DATE_KEY)
    if last_date == today:
        return
    if last_date:   # skip on the very first run ever — nothing to report yet
        await send_report(redis, positions, risk, period="daily", since_date=last_date)
    await redis.set(LAST_REPORT_DATE_KEY, today)


async def send_report(redis: aioredis.Redis, positions: PositionManager, risk: RiskManager,
                      period: str = "daily", since_date: str | None = None) -> str:
    trades = await positions.history(200)

    if period == "weekly":
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        trades = [t for t in trades if (t.get("closed_at") or "") >= cutoff]
    else:
        target_date = since_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        trades = [t for t in trades if (t.get("closed_at") or "")[:10] == target_date]

    wins   = [t for t in trades if t.get("pnl_r", 0) > 0]
    losses = [t for t in trades if t.get("pnl_r", 0) <= 0]
    total_r  = round(sum(t.get("pnl_r", 0) for t in trades), 2)
    win_rate = (len(wins) / len(trades) * 100) if trades else 0

    status = await risk.status()
    label  = "Dagsrapport" if period == "daily" else "Ugentlig rapport"

    if status.get("locked"):
        risk_line = f"🔒 LÅST — {status['locked']}"
    elif status.get("paused"):
        risk_line = "⏸️ Pause (manuel)"
    else:
        risk_line = "✅ Aktiv"

    msg = (
        f"📊 *{label}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Trades lukket: *{len(trades)}*  (vundet {len(wins)} / tabt {len(losses)})\n"
        f"Win rate: *{win_rate:.0f}%*\n"
        f"Samlet resultat: *{total_r:+.2f}R*\n\n"
        f"💰 Equity: `{status['equity']:.2f}`\n"
        f"📉 Dagens start-equity: `{status['day_start_equity']:.2f}`\n"
        f"📈 Peak equity: `{status['peak_equity']:.2f}`\n"
        f"Status: {risk_line}\n"
    )

    await redis.publish("supervisor:notifications", json.dumps({
        "message": msg, "parse_mode": "Markdown", "task_id": f"report_{period}",
    }))
    log.info("Sent %s report: %d trades, %.2fR", period, len(trades), total_r)
    return msg
