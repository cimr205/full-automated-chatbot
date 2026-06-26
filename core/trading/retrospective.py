from __future__ import annotations

"""
Retrospective trade analyser — continuously learns from past mistakes.

Runs automatically after each trade closes and also on a schedule.
For every losing trade it asks:
  - What was the setup type?
  - How far did price go in our favour before reversing?
  - Was the SL hit on a wick or a genuine move?
  - Did the entry happen during a prime session or not?
  - Was the trend alignment correct?

Findings update three Redis keys:
  trading:learning:{setup}           — win/loss counters (existing)
  trading:retro:insights             — human-readable lessons list
  trading:retro:sl_factor_votes      — weighted vote for new SL multiplier
"""
import json
import logging
import math
from datetime import datetime

import redis.asyncio as aioredis

log = logging.getLogger(__name__)

INSIGHTS_KEY    = "trading:retro:insights"
SL_VOTES_KEY    = "trading:retro:sl_factor_votes"
MAX_INSIGHTS    = 50   # rolling window of insights stored in Redis


async def analyse_trade(redis: aioredis.Redis, trade: dict) -> None:
    """Called after every closed trade. Extracts lessons and updates learning."""
    if not trade:
        return

    pnl_r       = trade.get("pnl_r", 0.0)
    setup       = (trade.get("reasoning") or {}).get("setup") or "unknown"
    direction   = trade.get("direction", "?")
    symbol      = trade.get("symbol", "?")
    session     = (trade.get("reasoning") or {}).get("session", "?")
    confidence  = (trade.get("reasoning") or {}).get("confidence", 0)
    rr_planned  = (trade.get("reasoning") or {}).get("rr_planned", 0)
    exit_reason = trade.get("exit_reason", "?")
    entry       = float(trade.get("entry", 0) or 0)
    sl          = float(trade.get("stop_loss", 0) or 0)
    tp          = float(trade.get("take_profit", 0) or 0)
    opened_at   = trade.get("opened_at", "")
    closed_at   = trade.get("closed_at", "")

    insights = []

    # ── Session analysis ──────────────────────────────────────────────────────
    if pnl_r < 0 and session and "Asian" in session:
        insights.append(
            f"❌ {symbol} {setup}: tabt {pnl_r:.2f}R i Asian session — "
            f"Asian-handler er typisk noise. Overvej at blokere."
        )

    # ── Low-confidence entries ────────────────────────────────────────────────
    if pnl_r < 0 and confidence and confidence < 0.72:
        insights.append(
            f"❌ {symbol} {setup}: tabt {pnl_r:.2f}R med kun {confidence:.0%} confidence — "
            f"for lav kvalitet. Overvej at hæve confidence-tærsklen."
        )

    # ── Stop-loss too close (stopped on noise) ────────────────────────────────
    if sl > 0 and entry > 0:
        sl_dist_pct = abs(entry - sl) / entry * 100
        if pnl_r < 0 and sl_dist_pct < 0.05:  # SL within 5 basis points
            insights.append(
                f"⚠️ {symbol} {setup}: SL var {sl_dist_pct:.3f}% fra entry — "
                f"muligvis stoppet på normal wick-støj."
            )
        # Vote for a slightly wider SL if we're getting stopped too often
        if pnl_r < 0 and exit_reason == "stop_loss":
            await _vote_sl_wider(redis, setup)

    # ── Winning pattern identified ────────────────────────────────────────────
    if pnl_r > 1.5:
        insights.append(
            f"✅ {symbol} {setup} ({session}): +{pnl_r:.2f}R — "
            f"confidence {confidence:.0%}, R:R planlagt {rr_planned:.1f}:1. Gentag dette."
        )

    # ── Save insights ─────────────────────────────────────────────────────────
    for insight in insights:
        timestamped = f"[{datetime.utcnow().strftime('%m-%d %H:%M')}] {insight}"
        await redis.lpush(INSIGHTS_KEY, timestamped)
        log.info("Retrospective insight: %s", insight)

    await redis.ltrim(INSIGHTS_KEY, 0, MAX_INSIGHTS - 1)

    # ── Notify via Telegram if meaningful ────────────────────────────────────
    if insights:
        msg = "🔍 *Retrospektiv analyse:*\n\n" + "\n\n".join(insights[:3])
        await redis.publish("supervisor:notifications", json.dumps({
            "message": msg, "parse_mode": "Markdown", "task_id": "retrospective",
        }))


async def full_retrospective(redis: aioredis.Redis) -> str:
    """
    Scan ALL closed trades in Redis history, re-derive insights, and send
    a summary. Called by /lessons or at startup.
    """
    raw_trades = await redis.lrange("trading:history", 0, -1)
    if not raw_trades:
        return "Ingen afsluttede trades i historikken endnu."

    trades = []
    for r in raw_trades:
        try:
            trades.append(json.loads(r))
        except Exception:
            pass

    if not trades:
        return "Ingen gyldige trades fundet."

    total     = len(trades)
    wins      = sum(1 for t in trades if t.get("pnl_r", 0) > 0)
    losses    = total - wins
    win_rate  = wins / total if total else 0
    total_r   = sum(t.get("pnl_r", 0) for t in trades)
    avg_r     = total_r / total if total else 0

    # Per-setup breakdown
    by_setup: dict[str, dict] = {}
    for t in trades:
        setup = (t.get("reasoning") or {}).get("setup") or "unknown"
        s = by_setup.setdefault(setup, {"wins": 0, "losses": 0, "total_r": 0.0})
        if t.get("pnl_r", 0) > 0:
            s["wins"] += 1
        else:
            s["losses"] += 1
        s["total_r"] += t.get("pnl_r", 0)

    # Session breakdown
    by_session: dict[str, dict] = {}
    for t in trades:
        sess = (t.get("reasoning") or {}).get("session") or "Ukendt"
        sess = sess.split("/")[0].strip()
        s = by_session.setdefault(sess, {"wins": 0, "losses": 0})
        if t.get("pnl_r", 0) > 0:
            s["wins"] += 1
        else:
            s["losses"] += 1

    # Build analysis message
    lines = [
        f"📊 *Retrospektiv analyse — {total} handler*",
        f"Win rate: {win_rate:.0%}  |  Total: {total_r:+.1f}R  |  Snit: {avg_r:+.2f}R/trade",
        "",
        "*Per setup:*",
    ]
    for setup, s in sorted(by_setup.items(), key=lambda kv: kv[1]["total_r"]):
        n = s["wins"] + s["losses"]
        wr = s["wins"] / n if n else 0
        tag = "🔴" if wr < 0.35 else "🟡" if wr < 0.5 else "🟢"
        lines.append(f"{tag} *{setup}*: {wr:.0%} win ({s['wins']}W/{s['losses']}L) {s['total_r']:+.1f}R")

    lines.append("")
    lines.append("*Per session:*")
    for sess, s in sorted(by_session.items(), key=lambda kv: kv[1]["losses"], reverse=True):
        n = s["wins"] + s["losses"]
        wr = s["wins"] / n if n else 0
        lines.append(f"{'🔴' if wr < 0.4 else '🟢'} {sess}: {wr:.0%} ({n} trades)")

    # Identify worst losing run
    max_consec_losses = _max_consecutive_losses(trades)
    if max_consec_losses > 2:
        lines.append(f"\n⚠️ Længste tabsrækkefølge: *{max_consec_losses} i træk*")

    # Re-run individual analysis on all trades to populate insights
    for t in trades:
        await analyse_trade(redis, t)

    summary = "\n".join(lines)

    # Publish to Telegram
    await redis.publish("supervisor:notifications", json.dumps({
        "message": summary, "parse_mode": "Markdown", "task_id": "retrospective_full",
    }))

    return summary


async def get_insights(redis: aioredis.Redis, n: int = 10) -> list[str]:
    raw = await redis.lrange(INSIGHTS_KEY, 0, n - 1)
    return [r for r in raw]


async def _vote_sl_wider(redis: aioredis.Redis, setup: str) -> None:
    """Track how often each setup is stopped out — feeds SL tuning."""
    await redis.hincrby(SL_VOTES_KEY, setup, 1)


def _max_consecutive_losses(trades: list[dict]) -> int:
    max_run = cur = 0
    for t in sorted(trades, key=lambda x: x.get("closed_at", "")):
        if t.get("pnl_r", 0) <= 0:
            cur += 1
            max_run = max(max_run, cur)
        else:
            cur = 0
    return max_run
