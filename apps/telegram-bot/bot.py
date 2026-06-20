"""
Telegram Bot — Autonomous Execution System
Full-featured digital twin: AI chat, vault, brain, tasks, goals, screenshots.
"""
import asyncio
import base64
import json
import logging
import os
import re
import sys
from datetime import datetime
from io import BytesIO
from pathlib import Path

import httpx
import redis.asyncio as aioredis
from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# Make sure monorepo root is importable (also set via PYTHONPATH=/app in Dockerfile)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.memory.brain import Brain
from core.memory.vault import SecureVault

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
API_URL = os.getenv("API_URL", "http://api:8000")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")

redis_client: aioredis.Redis = None
vault: SecureVault = None
brain: Brain = None


async def _ws_event(event_type: str, text: str, task_id: str = None):
    try:
        payload = {"type": event_type, "text": text[:300], "ts": datetime.utcnow().isoformat()}
        if task_id:
            payload["task_id"] = task_id
        await redis_client.publish("ws:events", json.dumps(payload))
    except Exception:
        pass


async def auth(update: Update) -> bool:
    if ALLOWED_CHAT_ID and update.effective_chat.id != ALLOWED_CHAT_ID:
        await update.message.reply_text("Unauthorized.")
        return False
    return True


async def api_get(path: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{API_URL}{path}")
        resp.raise_for_status()
        return resp.json()


async def api_post(path: str, body: dict, timeout: int = 10) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{API_URL}{path}", json=body)
        resp.raise_for_status()
        return resp.json()


async def _send_photo_or_text(bot, chat_id: int, photo_path: str, caption: str):
    """Send photo if it exists, else fall back to text."""
    if photo_path and Path(photo_path).exists():
        try:
            with open(photo_path, "rb") as f:
                await bot.send_photo(chat_id=chat_id, photo=InputFile(f),
                                     caption=caption[:1024], parse_mode="Markdown")
            return
        except Exception as e:
            log.warning("Photo send failed: %s", e)
    await bot.send_message(chat_id=chat_id, text=caption, parse_mode="Markdown")


# ── Core commands ──────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    await update.message.reply_text(
        "*Autonomous Execution System*\n\n"
        "*Bare skriv til mig — AI forstår naturligt sprog.*\n\n"
        "*📊 Trading*\n"
        "/trades — åbne trades + statistik\n"
        "/trade SYM long ENTRY sl=X tp=Y — log trade manuelt\n"
        "/close <id> [pris] — luk trade\n"
        "/why <id> — grundlag for trade\n"
        "/market — seneste signaler\n"
        "/watchlist — vis/ændr overvågningsliste\n"
        "/scan — scan markedet NU\n\n"
        "*Opgaver & Mål*\n"
        "/task <desc> — kø en-gangs opgave\n"
        "/goal <desc> — opret vedvarende mål\n"
        "/goal_recurring <desc> — gentagende mål\n"
        "/tasks — vis aktive opgaver\n"
        "/goals — vis aktive mål\n"
        "/status <id> — opgavestatus\n"
        "/stop <id> — stop kørende opgave\n"
        "/pause <id> / /resume <id> / /cancel <id>\n\n"
        "*Vault (krypteret)*\n"
        "/vault save <navn> <værdi> — gem credentials\n"
        "/vault get <navn> — hent (slettes om 30s)\n"
        "/vault list — vis nøgler\n"
        "/vault del <navn> — slet\n\n"
        "*Hukommelse*\n"
        "/remember <nøgle>=<værdi> — husk noget\n"
        "/recall <nøgle> — husk\n"
        "/brain — vis alt jeg ved om dig\n"
        "/forget <nøgle> — glem\n\n"
        "*System*\n"
        "/health — systemstatus\n"
        "/help — denne besked",
        parse_mode="Markdown",
    )


async def cmd_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    description = " ".join(ctx.args)
    if not description:
        await update.message.reply_text("Usage: /task <description>")
        return
    try:
        data = await api_post("/tasks", {"description": description})
        await _ws_event("telegram_out", f"Task queued: {description[:80]}", data["task_id"])
        await update.message.reply_text(
            f"Task i kø.\nID: `{data['task_id']}`\nFølg med: /status {data['task_id']}",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /status <task_id>")
        return
    try:
        task = await api_get(f"/tasks/{ctx.args[0]}")
        logs = task.get("logs", [])[-5:]
        log_text = "\n".join(logs) or "Ingen logs"
        await update.message.reply_text(
            f"*{ctx.args[0]}*\nStatus: `{task.get('status', '?')}`\n\n```\n{log_text}\n```",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    try:
        tasks = await api_get("/tasks")
        if not tasks:
            await update.message.reply_text("Ingen opgaver.")
            return
        lines = [
            f"• `{t['task_id']}` [{t.get('status','?')}] {t.get('description','')[:40]}"
            for t in tasks[:10]
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /stop <task_id>")
        return
    task_id = ctx.args[0]
    try:
        await api_post(f"/tasks/{task_id}/stop", {})
        await redis_client.set(f"task:stop:{task_id}", "1", ex=3600)
        await update.message.reply_text(f"Stop-signal sendt til `{task_id}`.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: /reply <task_id> <message>")
        return
    task_id = ctx.args[0]
    message = " ".join(ctx.args[1:])
    try:
        await api_post("/tasks/human-reply", {"task_id": task_id, "message": message})
        await update.message.reply_text(f"Svar sendt til `{task_id}`.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_goal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    description = " ".join(ctx.args)
    if not description:
        await update.message.reply_text("Usage: /goal <description>")
        return
    try:
        data = await api_post("/goals", {"description": description, "priority": "normal", "recurring": False})
        await update.message.reply_text(
            f"Mål oprettet.\nID: `{data['goal_id']}`\n{description[:60]}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_goal_recurring(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    description = " ".join(ctx.args)
    if not description:
        await update.message.reply_text("Usage: /goal_recurring <description>")
        return
    try:
        data = await api_post("/goals", {"description": description, "priority": "normal", "recurring": True})
        await update.message.reply_text(
            f"Gentagende mål oprettet.\nID: `{data['goal_id']}`\n{description[:60]}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_goals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    try:
        goals = await api_get("/goals?status=active")
        if not goals:
            await update.message.reply_text("Ingen aktive mål.")
            return
        lines = [
            f"• `{g['goal_id']}` [{'↺' if g.get('recurring') else '→'}] {g.get('description','')[:50]}"
            for g in goals[:10]
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /pause <goal_id>")
        return
    try:
        await api_post(f"/goals/{ctx.args[0]}/pause", {})
        await update.message.reply_text(f"`{ctx.args[0]}` sat på pause.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /resume <goal_id>")
        return
    try:
        await api_post(f"/goals/{ctx.args[0]}/resume", {})
        await update.message.reply_text(f"`{ctx.args[0]}` genoptaget.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /cancel <goal_id>")
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.delete(f"{API_URL}/goals/{ctx.args[0]}")
            resp.raise_for_status()
        await update.message.reply_text(f"`{ctx.args[0]}` annulleret.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_health(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    try:
        h = await api_get("/health")
        await update.message.reply_text(
            f"*Systemstatus*\n"
            f"Kø: {h.get('queue_length', 0)} ventende\n"
            f"Aktiv opgave: `{h.get('active_task') or 'ingen'}`\n"
            f"Aktive mål: {h.get('active_goals', 0)}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


# ── Vault commands ─────────────────────────────────────────────────────────────

async def cmd_vault(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    if not ctx.args:
        await update.message.reply_text(
            "*Vault kommandoer:*\n"
            "/vault save <navn> <værdi>\n"
            "/vault get <navn>\n"
            "/vault list\n"
            "/vault del <navn>",
            parse_mode="Markdown"
        )
        return

    sub = ctx.args[0].lower()

    if sub == "save" and len(ctx.args) >= 3:
        name = ctx.args[1]
        value = " ".join(ctx.args[2:])
        await vault.save(name, value)
        await update.message.reply_text(f"Gemt i vault: `{name}`", parse_mode="Markdown")

    elif sub == "get" and len(ctx.args) >= 2:
        name = ctx.args[1]
        value = await vault.get(name)
        if value is None:
            await update.message.reply_text(f"Ingen vault-nøgle: `{name}`", parse_mode="Markdown")
            return
        msg = await update.message.reply_text(
            f"*{name}:*\n`{value}`\n\n_(slettes om 30 sekunder)_",
            parse_mode="Markdown"
        )
        await asyncio.sleep(30)
        try:
            await msg.delete()
        except Exception:
            pass

    elif sub == "list":
        keys = await vault.list_keys()
        if not keys:
            await update.message.reply_text("Vault er tom.")
        else:
            await update.message.reply_text("*Vault nøgler:*\n" + "\n".join(f"• `{k}`" for k in keys),
                                             parse_mode="Markdown")

    elif sub == "del" and len(ctx.args) >= 2:
        name = ctx.args[1]
        await vault.delete(name)
        await update.message.reply_text(f"Slettet fra vault: `{name}`", parse_mode="Markdown")

    else:
        await update.message.reply_text("Ukendt vault-kommando. Brug /vault for hjælp.")


# ── Brain commands ─────────────────────────────────────────────────────────────

async def cmd_remember(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    raw = " ".join(ctx.args)
    if "=" not in raw:
        await update.message.reply_text("Usage: /remember nøgle=værdi")
        return
    key, _, value = raw.partition("=")
    await brain.remember(key.strip(), value.strip())
    await update.message.reply_text(f"Husket: `{key.strip()}` = {value.strip()}", parse_mode="Markdown")


async def cmd_recall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /recall <nøgle>")
        return
    key = " ".join(ctx.args)
    value = await brain.recall(key)
    if value is None:
        await update.message.reply_text(f"Ingen hukommelse om `{key}`", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"`{key}` = {value}", parse_mode="Markdown")


async def cmd_brain(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    facts = await brain.all_facts()
    if not facts:
        await update.message.reply_text("Ingen facts endnu. Brug /remember nøgle=værdi")
        return
    lines = [f"• *{k}*: {v}" for k, v in facts.items()]
    await update.message.reply_text("*Alt jeg ved om dig:*\n" + "\n".join(lines), parse_mode="Markdown")


async def cmd_forget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /forget <nøgle>")
        return
    key = " ".join(ctx.args)
    await redis_client.hdel("brain:facts", key)
    await update.message.reply_text(f"Glemt: `{key}`", parse_mode="Markdown")


# ── Trading commands ──────────────────────────────────────────────────────────

async def cmd_market(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    try:
        signals = await api_get("/trading/signals?limit=10")
        if not signals:
            await update.message.reply_text("Ingen signaler endnu. Monitoren scanner hvert 10. minut.")
            return
        lines = []
        for s in signals[:8]:
            d = "📈" if s.get("direction") == "long" else "📉"
            conf = f"{s.get('confidence', 0):.0%}"
            sym = s.get("symbol", "?")
            price = s.get("price", 0)
            ts = s.get("ts", "")[:16].replace("T", " ")
            lines.append(f"{d} *{sym}* {conf} @ {price:.4g} _{ts}_")
        await update.message.reply_text(
            "*Seneste handelssignaler:*\n\n" + "\n".join(lines),
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_watchlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    # /watchlist → vis
    # /watchlist forex EURUSD=X,GBPUSD=X
    # /watchlist stocks SPY,QQQ,NVDA
    if not ctx.args:
        try:
            cfg = await api_get("/trading/config")
            forex  = ", ".join(cfg.get("forex", []))
            stocks = ", ".join(cfg.get("stocks", []))
            conf   = cfg.get("confidence", 0.68)
            await update.message.reply_text(
                f"*Overvågningsliste*\n\n"
                f"💱 *Forex:*\n{forex}\n\n"
                f"📊 *Aktier/Indeks:*\n{stocks}\n\n"
                f"Confidence threshold: {conf:.0%}\n\n"
                f"Brug `/watchlist forex EUR/USD=X,GBP/USD=X` for at ændre.",
                parse_mode="Markdown"
            )
        except Exception as e:
            await update.message.reply_text(f"Fejl: {e}")
        return

    market_type = ctx.args[0].lower()
    if market_type not in ("forex", "stocks") or len(ctx.args) < 2:
        await update.message.reply_text("Usage: /watchlist forex EURUSD=X,GBPUSD=X\neller: /watchlist stocks SPY,QQQ")
        return
    symbols = ctx.args[1].split(",")
    try:
        body = {market_type: [s.strip() for s in symbols]}
        await api_post("/trading/config", body)
        await update.message.reply_text(
            f"✅ {market_type.capitalize()} watchlist opdateret:\n" + ", ".join(symbols),
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    try:
        await api_post("/trading/scan-now", {})
        await update.message.reply_text(
            "🔍 Scanner markedet nu... Du får besked hvis der er et signal.",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_trading_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Pause the auto-trading scanner (it keeps monitoring open positions)."""
    if not await auth(update):
        return
    try:
        await api_post("/trading/pause", {})
        await update.message.reply_text(
            "⏸️ Auto-trading sat på pause. Åbne positioner overvåges stadig. /trading_resume for at genoptage.",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_trading_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Resume the auto-trading scanner."""
    if not await auth(update):
        return
    try:
        await api_post("/trading/resume", {})
        await update.message.reply_text("▶️ Auto-trading genoptaget.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_risk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show current risk status: equity, daily/total drawdown, lock state."""
    if not await auth(update):
        return
    try:
        s = await api_get("/trading/risk")
        if s.get("locked"):
            status_line = f"🔒 LÅST — {s['locked']}"
        elif s.get("paused"):
            status_line = "⏸️ Pause (manuel)"
        else:
            status_line = "✅ Aktiv"
        msg = (
            f"📐 *Risk-status*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Status: {status_line}\n\n"
            f"💰 Equity: `{s.get('equity', 0):,.2f}`\n"
            f"📉 Dagens start: `{s.get('day_start_equity', 0):,.2f}`\n"
            f"📈 Peak: `{s.get('peak_equity', 0):,.2f}`\n\n"
            f"Max dagligt tab: *{s.get('max_daily_loss_pct', 0):.1f}%*\n"
            f"Max drawdown: *{s.get('max_drawdown_pct', 0):.1f}%*\n"
            f"Risiko/trade: *{s.get('risk_per_trade_pct', 0):.1f}%*"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_unlock_risk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Manually clear a tripped risk-lock after reviewing the account."""
    if not await auth(update):
        return
    try:
        res = await api_post("/trading/unlock-risk", {})
        if res.get("status") == "unlocked":
            await update.message.reply_text("🔓 Risk-lock fjernet. Trading genoptaget.", parse_mode="Markdown")
        else:
            await update.message.reply_text("Der var ingen aktiv risk-lock.")
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Send a performance report now: /report [daily|weekly]"""
    if not await auth(update):
        return
    period = (ctx.args[0] if ctx.args else "daily").lower()
    if period not in ("daily", "weekly"):
        await update.message.reply_text("Usage: /report daily|weekly")
        return
    try:
        await api_get(f"/trading/report?period={period}")
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_backtest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Measure the real historical win rate for a symbol: /backtest EURUSD=X"""
    if not await auth(update):
        return
    symbol = ctx.args[0] if ctx.args else "EURUSD=X"
    await update.message.reply_text(f"⏳ Backtester {symbol} mod ~2 års historik — kan tage et minut...")
    try:
        result = await api_get(f"/trading/backtest?symbol={symbol}")
        if "error" in result:
            await update.message.reply_text(f"Fejl: {result['error']}")
            return
        overall = result["overall"]
        lines = [
            f"📐 *Backtest — {result['symbol']}* ({result['candles']} candles)\n",
            f"Samlet: {overall['total']} trades, {overall['win_rate']:.0%} win rate, "
            f"{overall['total_r']:+.1f}R samlet, {overall['avg_r']:+.2f}R/trade\n",
        ]
        for setup, s in sorted(result["by_setup"].items(), key=lambda kv: -kv[1]["total"]):
            lines.append(f"  • *{setup}*: {s['win_rate']:.0%} win rate ({s['total']} trades)")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_seed_learning(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Pre-load the learning system with backtested history across the watchlist."""
    if not await auth(update):
        return
    await update.message.reply_text("⏳ Backtester hele watchlisten og forhåndsudfylder lærings-systemet...")
    try:
        res = await api_post("/trading/seed-learning", {}, timeout=120)
        n = len(res.get("results", {}))
        await update.message.reply_text(f"✅ Forhåndsudfyldt med historik fra {n} symboler. Se /lessons.")
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_lessons(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show win rate per setup type, and which ones the bot has stopped using."""
    if not await auth(update):
        return
    try:
        stats = await api_get("/trading/lessons")
        if not stats:
            await update.message.reply_text("Ingen lukkede trades endnu — ingen lektier at vise.")
            return
        lines = []
        for setup, s in sorted(stats.items(), key=lambda kv: kv[1]["win_rate"]):
            total = s["wins"] + s["losses"]
            tag = "🚫 BLOKERET" if s.get("blocked") else "✅"
            lines.append(f"{tag} *{setup}*: {s['win_rate']:.0%} win rate ({s['wins']}W/{s['losses']}L, {total} trades)")
        await update.message.reply_text(
            "🧠 *Lektier — win rate per setup-type*\n\n" + "\n".join(lines),
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_trades(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show all open trades."""
    if not await auth(update):
        return
    try:
        trades = await api_get("/trading/trades")
        if not trades:
            await update.message.reply_text("Ingen åbne trades. Systemet scanner markedet automatisk.")
            return
        lines = []
        for t in trades:
            def fmt(v): return f"{v:,.5f}" if abs(v or 0) < 10 else f"{v:,.2f}"
            d = "📈" if t.get("direction") == "long" else "📉"
            sym = t.get("symbol", "?")
            entry = fmt(t.get("entry", 0))
            sl = fmt(t.get("stop_loss", 0))
            tp = fmt(t.get("take_profit", 0))
            src = "🤖" if t.get("source") == "auto" else "👤"
            lines.append(
                f"{src}{d} *{sym}* `{t['trade_id']}`\n"
                f"  Entry: `{entry}` | SL: `{sl}` | TP: `{tp}`\n"
                f"  Åbnet: {t.get('opened_at','')[:16]}"
            )
        stats = await api_get("/trading/stats")
        stats_text = (
            f"\n\n📊 *Statistik ({stats.get('total',0)} lukkede trades):*\n"
            f"Win rate: {stats.get('win_rate',0):.0%}  |  "
            f"Gns R:R: {stats.get('avg_rr',0):.2f}R  |  "
            f"Total: {stats.get('total_r',0):+.2f}R"
        )
        await update.message.reply_text(
            "*Åbne trades:*\n\n" + "\n\n".join(lines) + stats_text,
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_trade(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Manually log a trade: /trade EURUSD=X long 1.08500 sl=1.08200 tp=1.09100 [forex|stocks]"""
    if not await auth(update):
        return
    if len(ctx.args) < 4:
        await update.message.reply_text(
            "Usage: `/trade SYMBOL long/short ENTRY sl=SL tp=TP [forex|stocks]`\n"
            "Eksempel: `/trade EURUSD=X long 1.08500 sl=1.08200 tp=1.09100`",
            parse_mode="Markdown"
        )
        return
    try:
        symbol    = ctx.args[0].upper()
        direction = ctx.args[1].lower()
        entry     = float(ctx.args[2])
        sl = tp = partial_tp = 0.0
        market = "forex"
        note = ""
        for arg in ctx.args[3:]:
            if arg.startswith("sl="):   sl = float(arg[3:])
            elif arg.startswith("tp="): tp = float(arg[3:])
            elif arg.startswith("pt="): partial_tp = float(arg[3:])
            elif arg in ("forex", "stocks", "crypto"): market = arg
            else: note += arg + " "
        if not sl or not tp:
            await update.message.reply_text("Angiv sl= og tp= værdier.")
            return
        data = await api_post("/trading/trades", {
            "symbol": symbol, "market": market, "direction": direction,
            "entry": entry, "stop_loss": sl, "take_profit": tp,
            "partial_tp": partial_tp, "note": note.strip(),
        })
        def fmt(v): return f"{v:,.5f}" if abs(v) < 10 else f"{v:,.2f}"
        rr = abs(tp - entry) / abs(sl - entry) if sl != entry else 0
        await update.message.reply_text(
            f"✅ *Trade logget*\n"
            f"ID: `{data['trade_id']}`\n"
            f"*{symbol}* {direction.upper()} @ `{fmt(entry)}`\n"
            f"SL: `{fmt(sl)}`  |  TP: `{fmt(tp)}`  |  R:R: {rr:.1f}:1\n\n"
            f"Systemet overvåger positionen og melder ud ved SL/TP.\n"
            f"_Luk manuelt: /close {data['trade_id']}_",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Close a trade: /close <trade_id> [exit_price]"""
    if not await auth(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /close <trade_id> [exit_price]")
        return
    trade_id = ctx.args[0]
    try:
        # Get current price if not specified
        if len(ctx.args) >= 2:
            exit_price = float(ctx.args[1])
        else:
            trade = await api_get(f"/trading/trades/{trade_id}")
            exit_price = trade.get("entry", 0)  # placeholder; position monitor will use live price

        data = await api_post(f"/trading/trades/{trade_id}/close?exit_price={exit_price}", {})
        pnl  = data.get("pnl_r", 0)
        emoji = "💚" if pnl > 0 else "❤️"
        await update.message.reply_text(
            f"{emoji} *Trade lukket*\n"
            f"`{trade_id}`  P&L: `{pnl:+.2f}R`",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_why(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Explain why a trade was entered: /why <trade_id>"""
    if not await auth(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /why <trade_id>")
        return
    try:
        trade = await api_get(f"/trading/trades/{ctx.args[0]}")
        r = trade.get("reasoning", {})
        indicators = r.get("indicators", [])
        checklist  = r.get("checklist", {})

        cl_text = "\n".join(
            f"  {'✅' if v else '❌'} {k.replace('_', ' ').title()}"
            for k, v in checklist.items()
        ) if checklist else "  (ingen checklist gemt)"

        ind_text = "\n".join(f"  • {i}" for i in indicators[:6]) if indicators else "  (ingen)"

        def fmt(v): return f"{v:,.5f}" if abs(v or 0) < 10 else f"{v:,.2f}"

        await update.message.reply_text(
            f"🔍 *Grundlag for {trade['symbol']} {trade['direction'].upper()}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📐 Setup: {r.get('setup_label') or r.get('setup') or 'Ukendt'}\n"
            f"🕐 Session: {r.get('session') or 'Ukendt'}\n"
            f"📊 Confidence: {(r.get('confidence') or 0):.0%}\n"
            f"🔗 Confluence: {r.get('confluence') or 0} faktorer\n"
            f"📈 Planlagt R:R: {(r.get('rr_planned') or 0):.1f}:1\n\n"
            f"*Pre-trade checklist:*\n{cl_text}\n\n"
            f"*Indicator confluence:*\n{ind_text}\n\n"
            f"Entry: `{fmt(trade['entry'])}`  "
            f"SL: `{fmt(trade['stop_loss'])}`  "
            f"TP: `{fmt(trade['take_profit'])}`",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


# ── CRM commands ───────────────────────────────────────────────────────────────

async def cmd_lead(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /lead Navn|Firma|Rolle")
        return
    parts = " ".join(ctx.args).split("|")
    lead = {
        "name": parts[0].strip() if len(parts) > 0 else "",
        "company": parts[1].strip() if len(parts) > 1 else "",
        "role": parts[2].strip() if len(parts) > 2 else "",
    }
    try:
        await api_post("/leads", lead)
        await update.message.reply_text(f"Lead gemt: {lead['name']} @ {lead['company']}")
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_findleads(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    query = " ".join(ctx.args)
    if not query:
        await update.message.reply_text("Usage: /findleads <søgeforespørgsel>")
        return
    try:
        results = await api_get(f"/leads/search?q={query}&n=5")
        if not results:
            await update.message.reply_text("Ingen leads fundet.")
            return
        lines = []
        for r in results:
            m = r.get("metadata", {})
            lines.append(f"• {m.get('name','?')} — {m.get('company','?')} ({m.get('role','?')})")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


# ── Free-text handler (full AI) ────────────────────────────────────────────────

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    await update.message.chat.send_action("typing")
    await _ws_event("telegram_in", f"User: {text}")

    # Route reply to waiting browser task if one is pending
    pending_task_id = await redis_client.get("bot:pending_task")
    if pending_task_id:
        await redis_client.delete("bot:pending_task")
        await redis_client.publish(
            f"human_reply:{pending_task_id}",
            json.dumps({"task_id": pending_task_id, "message": text})
        )
        await update.message.reply_text(
            f"Svar sendt til `{pending_task_id}`.", parse_mode="Markdown"
        )
        return

    # Delegate all AI processing to the API service (which has Ollama running)
    try:
        result = await api_post(
            "/chat",
            {"message": text, "session_id": str(chat_id)},
            timeout=200,
        )
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")
        return

    reply = result.get("reply", "Hvad kan jeg hjælpe med?")
    action = result.get("action")

    if action == "task":
        task_id = result.get("task_id")
        if task_id:
            await _ws_event("telegram_out", f"Task queued", task_id)
            reply = f"{reply}\n\nID: `{task_id}`\nFølg med: /status {task_id} · Stop: /stop {task_id}"
    elif action == "goal":
        goal_id = result.get("goal_id")
        if goal_id:
            await _ws_event("telegram_out", f"Goal created", goal_id)
            reply = f"{reply}\n\nID: `{goal_id}`"

    await _ws_event("telegram_out", f"Bot: {reply[:200]}")
    try:
        await update.message.reply_text(reply, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(reply)


# ── Notification listener ─────────────────────────────────────────────────────

async def listen_notifications(bot):
    pubsub = redis_client.pubsub()
    await pubsub.subscribe("supervisor:notifications", "browser:screenshots", "browser:progress",
                           "trading:charts")
    log.info("Notification listener started")

    async for message in pubsub.listen():
        if message["type"] != "message":
            continue
        try:
            data = json.loads(message["data"])
            channel = message["channel"]

            if channel == "supervisor:notifications":
                msg_text = data["message"]
                task_id = data.get("task_id", "?")
                parse_mode = data.get("parse_mode", "Markdown")
                # Trading signals come with pre-formatted Markdown — send directly
                if task_id.startswith("signal_"):
                    await bot.send_message(
                        chat_id=ALLOWED_CHAT_ID,
                        text=msg_text,
                        parse_mode=parse_mode,
                    )
                else:
                    await bot.send_message(
                        chat_id=ALLOWED_CHAT_ID,
                        text=f"[SYS] `{task_id}`\n{msg_text}",
                        parse_mode=parse_mode,
                    )

            elif channel == "browser:screenshots":
                task_id = data["task_id"]
                reason = data.get("reason", "Kræver handling")
                screenshot_path = data.get("screenshot_path", "")
                # Set pending task so user can just type the reply
                await redis_client.setex("bot:pending_task", 300, task_id)
                caption = (
                    f"[BLOKKERET] `{task_id}`\n{reason}\n\n"
                    f"_Skriv dit svar direkte — eller /reply {task_id} <besked>_"
                )
                await _send_photo_or_text(bot, ALLOWED_CHAT_ID, screenshot_path, caption)

            elif channel == "browser:progress":
                task_id = data.get("task_id", "?")
                step = data.get("step", "?")
                url = data.get("url", "")
                logs = data.get("recent_logs", [])
                screenshot_path = data.get("screenshot_path", "")
                log_lines = "\n".join(f"  {l}" for l in logs[-3:]) if logs else "  Ingen logs"
                caption = (
                    f"*[PROGRESS] `{task_id}`*\n"
                    f"Trin: {step} · {url[:50] if url else 'ingen URL'}\n\n"
                    f"```\n{log_lines}\n```\n\n"
                    f"_Stop med: /stop {task_id}_"
                )
                await _send_photo_or_text(bot, ALLOWED_CHAT_ID, screenshot_path, caption)

            elif channel == "trading:charts":
                image_b64 = data.get("image_b64", "")
                caption   = data.get("caption", "📊 Watchlist-overblik")
                if image_b64:
                    image_bytes = base64.b64decode(image_b64)
                    await bot.send_photo(
                        chat_id=ALLOWED_CHAT_ID,
                        photo=BytesIO(image_bytes),
                        caption=caption,
                    )

        except Exception as e:
            log.error("Notification error: %s", e)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    global redis_client, vault, brain

    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    vault = SecureVault(redis_client)
    brain = Brain(redis_client)

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    handlers = [
        ("start", cmd_start), ("help", cmd_start),
        ("trades", cmd_trades), ("trade", cmd_trade), ("close", cmd_close), ("why", cmd_why),
        ("market", cmd_market), ("watchlist", cmd_watchlist), ("scan", cmd_scan),
        ("trading_pause", cmd_trading_pause), ("trading_resume", cmd_trading_resume),
        ("risk", cmd_risk), ("unlock_risk", cmd_unlock_risk), ("report", cmd_report),
        ("lessons", cmd_lessons), ("backtest", cmd_backtest), ("seed_learning", cmd_seed_learning),
        ("task", cmd_task), ("status", cmd_status),
        ("tasks", cmd_tasks), ("reply", cmd_reply), ("stop", cmd_stop),
        ("goal", cmd_goal), ("goal_recurring", cmd_goal_recurring),
        ("goals", cmd_goals), ("pause", cmd_pause), ("resume", cmd_resume),
        ("cancel", cmd_cancel), ("health", cmd_health),
        ("vault", cmd_vault),
        ("remember", cmd_remember), ("recall", cmd_recall),
        ("brain", cmd_brain), ("forget", cmd_forget),
        ("lead", cmd_lead), ("findleads", cmd_findleads),
    ]
    for name, handler in handlers:
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    asyncio.create_task(listen_notifications(app.bot))

    log.info("Telegram bot started — full AI + vault + brain enabled")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
