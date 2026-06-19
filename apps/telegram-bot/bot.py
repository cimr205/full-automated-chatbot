"""
Telegram Bot — Autonomous Execution System
Full-featured digital twin: AI chat, vault, brain, tasks, goals, screenshots.
"""
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import httpx
import redis.asyncio as aioredis
from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# Make sure monorepo root is importable (also set via PYTHONPATH=/app in Dockerfile)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.ai.chat import chat as ai_chat
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


async def api_post(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
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
        "*Trading*\n"
        "/market — markedsovervågning + statistik\n"
        "/trades — åbne trades\n"
        "/trade SYMBOL long/short ENTRY sl=X tp=Y — log manuel trade\n"
        "/close <id> <pris> — luk trade\n"
        "/why <id> — se begrundelse for en trade\n"
        "/watchlist — overvågede symboler\n"
        "/scan — scan markedet nu\n"
        "/broker [mt4|mt5|auto] — broker-forbindelse\n\n"
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


# ── Trading commands ───────────────────────────────────────────────────────────

async def cmd_trades(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    try:
        trades = await api_get("/trading/trades")
        if not trades:
            await update.message.reply_text("Ingen åbne trades.")
            return
        lines = [
            f"• `{t['trade_id'][:14]}` {t['symbol']} {t['direction'].upper()} "
            f"@ `{t['entry']}` SL:`{t['stop_loss']}` TP:`{t['take_profit']}`"
            for t in trades[:15]
        ]
        await update.message.reply_text("*Åbne trades:*\n" + "\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_trade(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    if len(ctx.args) < 4:
        await update.message.reply_text(
            "Usage: /trade SYMBOL long ENTRY sl=X tp=Y [forex|stocks]\n"
            "Eksempel: /trade EURUSD=X long 1.0850 sl=1.0800 tp=1.0950 forex"
        )
        return
    try:
        symbol, direction, entry_raw = ctx.args[0], ctx.args[1].lower(), ctx.args[2]
        sl = tp = None
        market = "forex"
        for arg in ctx.args[3:]:
            if arg.startswith("sl="):
                sl = float(arg[3:])
            elif arg.startswith("tp="):
                tp = float(arg[3:])
            elif arg in ("forex", "stocks"):
                market = arg
        if sl is None or tp is None:
            await update.message.reply_text("Mangler sl= og/eller tp=")
            return
        data = await api_post("/trading/trades", {
            "symbol": symbol, "market": market, "direction": direction,
            "entry": float(entry_raw), "stop_loss": sl, "take_profit": tp,
        })
        await update.message.reply_text(
            f"Trade logget.\nID: `{data['trade_id']}`",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: /close <trade_id> <exit_price>")
        return
    try:
        trade_id, exit_price = ctx.args[0], float(ctx.args[1])
        data = await api_post(f"/trading/trades/{trade_id}/close?exit_price={exit_price}", {})
        r = data.get("r_multiple", 0)
        await update.message.reply_text(
            f"{'✅' if r > 0 else '❌'} Trade lukket. R: `{r:+.2f}`",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_why(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /why <trade_id>")
        return
    try:
        trade = await api_get(f"/trading/trades/{ctx.args[0]}")
        reasoning = trade.get("reasoning", {})
        if not reasoning:
            await update.message.reply_text("Ingen begrundelse fundet for denne trade.")
            return
        cl = reasoning.get("checklist", {})
        checklist_lines = "\n".join(f"{'✅' if v else '❌'} {k.replace('_',' ')}" for k, v in cl.items())
        reasons = "\n".join(f"• {r}" for r in reasoning.get("reasons", [])[:8])
        await update.message.reply_text(
            f"*Begrundelse for {trade['symbol']}:*\n\n"
            f"Confidence: `{reasoning.get('confidence',0)*100:.0f}%`\n"
            f"Confluence: `{reasoning.get('confluence',0)}`\n"
            f"Session: {reasoning.get('session','?')}\n"
            f"Setups: {', '.join(reasoning.get('setups',[])) or '—'}\n\n"
            f"*Checklist:*\n{checklist_lines}\n\n"
            f"*Indikatorer:*\n{reasons}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_market(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    try:
        stats = await api_get("/trading/stats")
        broker = await api_get("/trading/broker")
        cfg = await api_get("/trading/config")
        mt5_dot = "🟢" if broker.get("mt5_online") else "🔴"
        mt4_dot = "🟢" if broker.get("mt4_online") else "🔴"
        await update.message.reply_text(
            f"*Markedsovervågning*\n"
            f"Status: {'🟢 Kører' if cfg.get('running') else '🔴 Stoppet'}\n"
            f"Forex: {len(cfg.get('forex',[]))} par · Stocks: {len(cfg.get('stocks',[]))} symboler\n\n"
            f"*Broker forbindelse*\n"
            f"{mt5_dot} MT5 · {mt4_dot} MT4\n"
            f"Aktiv: `{broker.get('preferred','auto')}`\n\n"
            f"*Statistik*\n"
            f"Trades: {stats.get('total',0)} · Win rate: `{stats.get('win_rate',0)}%`\n"
            f"Avg R: `{stats.get('avg_rr',0):+.2f}` · Total R: `{stats.get('total_r',0):+.2f}`",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_watchlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    try:
        cfg = await api_get("/trading/config")
        await update.message.reply_text(
            f"*Forex:*\n{', '.join(cfg.get('forex',[]))}\n\n"
            f"*Stocks/Indeks:*\n{', '.join(cfg.get('stocks',[]))}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    try:
        await api_post("/trading/scan-now", {})
        await update.message.reply_text("Scanner markedet nu …")
    except Exception as e:
        await update.message.reply_text(f"Fejl: {e}")


async def cmd_broker(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    if not ctx.args:
        try:
            s = await api_get("/trading/broker")
            await update.message.reply_text(
                f"MT5: {'🟢 online' if s.get('mt5_online') else '🔴 offline'}\n"
                f"MT4: {'🟢 online' if s.get('mt4_online') else '🔴 offline'}\n"
                f"Foretrukket: `{s.get('preferred','auto')}`\n\n"
                f"Skift med: /broker mt4 · /broker mt5 · /broker auto",
                parse_mode="Markdown",
            )
        except Exception as e:
            await update.message.reply_text(f"Fejl: {e}")
        return
    choice = ctx.args[0].lower()
    if choice not in ("mt4", "mt5", "auto"):
        await update.message.reply_text("Usage: /broker mt4|mt5|auto")
        return
    try:
        await api_post("/trading/broker", {"broker": choice})
        await update.message.reply_text(f"Foretrukken broker sat til: `{choice}`", parse_mode="Markdown")
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

    # Check if a browser task is waiting for a human reply
    pending_key = "bot:pending_task"
    pending_task_id = await redis_client.get(pending_key)
    if pending_task_id:
        await redis_client.delete(pending_key)
        await redis_client.publish(
            f"human_reply:{pending_task_id}",
            json.dumps({"task_id": pending_task_id, "message": text})
        )
        await update.message.reply_text(
            f"Svar sendt til opgave `{pending_task_id}`.",
            parse_mode="Markdown"
        )
        return

    # Load chat history from Redis
    history_key = f"chat_history:{chat_id}"
    try:
        raw = await redis_client.get(history_key)
        history = json.loads(raw) if raw else []
    except Exception:
        history = []

    # Call the full AI engine with brain context
    result = await ai_chat(text, history, brain=brain, vault=vault, max_tokens=2000)

    # Persist history (last 20 messages, 7-day TTL)
    try:
        await redis_client.setex(history_key, 86400 * 7, json.dumps(history[-20:]))
    except Exception:
        pass

    reply = result.get("reply", "Hvad kan jeg hjælpe med?")
    action = result.get("action")

    if action == "task":
        desc = result.get("description", text)
        try:
            data = await api_post("/tasks", {"description": desc})
            task_id = data["task_id"]
            await _ws_event("telegram_out", f"Task queued: {desc[:80]}", task_id)
            full_reply = (
                f"{reply}\n\n"
                f"ID: `{task_id}`\n"
                f"Følg med: /status {task_id} · Stop: /stop {task_id}"
            )
            await update.message.reply_text(full_reply, parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"{reply}\n\n(Fejl ved oprettelse: {e})")

    elif action == "goal":
        desc = result.get("description", text)
        recurring = result.get("recurring", False)
        try:
            data = await api_post("/goals", {"description": desc, "recurring": recurring})
            await _ws_event("telegram_out", f"Goal created: {desc[:80]}", data["goal_id"])
            full_reply = f"{reply}\n\nID: `{data['goal_id']}`"
            await update.message.reply_text(full_reply, parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"{reply}\n\n(Fejl: {e})")

    else:
        await _ws_event("telegram_out", f"Bot: {reply[:200]}")
        # Telegram Markdown is strict — send as plain text if it might have issues
        try:
            await update.message.reply_text(reply, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(reply)


# ── Notification listener ─────────────────────────────────────────────────────

async def listen_notifications(bot):
    pubsub = redis_client.pubsub()
    await pubsub.subscribe("supervisor:notifications", "browser:screenshots", "browser:progress")
    log.info("Notification listener started")

    async for message in pubsub.listen():
        if message["type"] != "message":
            continue
        try:
            data = json.loads(message["data"])
            channel = message["channel"]

            if channel == "supervisor:notifications":
                msg_text = data["message"]
                task_id  = data.get("task_id", "?")
                parse_mode = data.get("parse_mode", "Markdown")
                # Trading/broker messages are already fully formatted — send as-is
                if task_id.startswith(("trading", "trd_", "mt4_bridge", "mt5_bridge")):
                    text = msg_text
                else:
                    text = f"[SYS] `{task_id}`\n{msg_text}"
                await bot.send_message(chat_id=ALLOWED_CHAT_ID, text=text, parse_mode=parse_mode)

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
        ("task", cmd_task), ("status", cmd_status),
        ("tasks", cmd_tasks), ("reply", cmd_reply), ("stop", cmd_stop),
        ("goal", cmd_goal), ("goal_recurring", cmd_goal_recurring),
        ("goals", cmd_goals), ("pause", cmd_pause), ("resume", cmd_resume),
        ("cancel", cmd_cancel), ("health", cmd_health),
        ("vault", cmd_vault),
        ("remember", cmd_remember), ("recall", cmd_recall),
        ("brain", cmd_brain), ("forget", cmd_forget),
        ("lead", cmd_lead), ("findleads", cmd_findleads),
        ("trades", cmd_trades), ("trade", cmd_trade), ("close", cmd_close),
        ("why", cmd_why), ("market", cmd_market), ("watchlist", cmd_watchlist),
        ("scan", cmd_scan), ("broker", cmd_broker),
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
