"""
Telegram Bot — Phase 2
Operational control layer for the autonomous execution system.
"""
import asyncio
import json
import logging
import os
from pathlib import Path

import httpx
import redis.asyncio as aioredis
from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
API_URL = os.getenv("API_URL", "http://api:8000")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")

redis_client: aioredis.Redis = None


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


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    await update.message.reply_text(
        "*Autonomous Execution System v2*\n\n"
        "*Tasks (one-off)*\n"
        "/task <desc> — queue task\n"
        "/status <id> — task status\n"
        "/tasks — list tasks\n"
        "/reply <id> <msg> — respond to waiting task\n\n"
        "*Goals (persistent)*\n"
        "/goal <desc> — create goal\n"
        "/goal_recurring <desc> — recurring goal\n"
        "/goals — list active goals\n"
        "/pause <goal_id> — pause goal\n"
        "/resume <goal_id> — resume goal\n"
        "/cancel <goal_id> — cancel goal\n\n"
        "*Memory*\n"
        "/context — show business context\n"
        "/setcontext <json> — set business context\n"
        "/lead <name|company|role> — save lead\n"
        "/findleads <query> — search CRM\n\n"
        "*System*\n"
        "/health — system status\n"
        "/help — this message",
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
        await update.message.reply_text(
            f"Task queued.\nID: `{data['task_id']}`", parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /status <task_id>")
        return
    try:
        task = await api_get(f"/tasks/{ctx.args[0]}")
        status = task.get("status", "?")
        logs = task.get("logs", [])[-4:]
        log_text = "\n".join(logs) or "No logs"
        await update.message.reply_text(
            f"*{ctx.args[0]}*\nStatus: `{status}`\n\n```\n{log_text}\n```",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    try:
        tasks = await api_get("/tasks")
        if not tasks:
            await update.message.reply_text("No tasks.")
            return
        lines = [
            f"• `{t['task_id']}` [{t.get('status','?')}] {t.get('description','')[:40]}"
            for t in tasks[:10]
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


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
        await update.message.reply_text(f"Reply sent to `{task_id}`.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


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
            f"Goal created.\nID: `{data['goal_id']}`\n{description[:60]}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


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
            f"Recurring goal created.\nID: `{data['goal_id']}`\n{description[:60]}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_goals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    try:
        goals = await api_get("/goals?status=active")
        if not goals:
            await update.message.reply_text("No active goals.")
            return
        lines = [
            f"• `{g['goal_id']}` [p{g.get('priority',3)}{'↺' if g.get('recurring') else ''}] {g.get('description','')[:50]}"
            for g in goals[:10]
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /pause <goal_id>")
        return
    try:
        await api_post(f"/goals/{ctx.args[0]}/pause", {})
        await update.message.reply_text(f"`{ctx.args[0]}` paused.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /resume <goal_id>")
        return
    try:
        await api_post(f"/goals/{ctx.args[0]}/resume", {})
        await update.message.reply_text(f"`{ctx.args[0]}` resumed.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_health(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    try:
        h = await api_get("/health")
        await update.message.reply_text(
            f"*System Status*\n"
            f"Queue: {h.get('queue_length', 0)} waiting\n"
            f"Active task: `{h.get('active_task') or 'none'}`\n"
            f"Active goals: {h.get('active_goals', 0)}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_context(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    try:
        data = await api_get("/memory/business_context")
        await update.message.reply_text(
            f"Business context:\n```\n{json.dumps(data.get('value', {}), indent=2)}\n```",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"No context set. Use /setcontext {{json}}")


async def cmd_setcontext(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    raw = " ".join(ctx.args)
    try:
        data = json.loads(raw)
        await api_post("/memory/business-context", data)
        await update.message.reply_text("Business context saved.")
    except json.JSONDecodeError:
        await update.message.reply_text("Invalid JSON. Example:\n`/setcontext {\"company_name\": \"Acme\", \"industry\": \"SaaS\"}`", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_lead(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /lead Name|Company|Role")
        return
    parts = " ".join(ctx.args).split("|")
    lead = {
        "name": parts[0].strip() if len(parts) > 0 else "",
        "company": parts[1].strip() if len(parts) > 1 else "",
        "role": parts[2].strip() if len(parts) > 2 else "",
    }
    try:
        await api_post("/leads", lead)
        await update.message.reply_text(f"Lead saved: {lead['name']} @ {lead['company']}")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_findleads(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    query = " ".join(ctx.args)
    if not query:
        await update.message.reply_text("Usage: /findleads <search query>")
        return
    try:
        results = await api_get(f"/leads/search?q={query}&n=5")
        if not results:
            await update.message.reply_text("No leads found.")
            return
        lines = []
        for r in results:
            m = r.get("metadata", {})
            lines.append(f"• {m.get('name','?')} — {m.get('company','?')} ({m.get('role','?')})")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    text = update.message.text.strip()
    await update.message.chat.send_action("typing")

    # Ask AI to classify and respond
    ai_response = await _ai_chat(text)

    if ai_response.get("type") == "task":
        # Auto-create task from natural language
        desc = ai_response.get("description", text)
        try:
            data = await api_post("/tasks", {"description": desc})
            task_id = data["task_id"]
            reply = (
                f"{ai_response.get('reply', 'Opgave oprettet.')}\n\n"
                f"ID: `{task_id}`\n"
                f"Følg med: /status {task_id}"
            )
            await update.message.reply_text(reply, parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"Kunne ikke oprette opgave: {e}")

    elif ai_response.get("type") == "goal":
        desc = ai_response.get("description", text)
        recurring = ai_response.get("recurring", False)
        try:
            data = await api_post("/goals", {"description": desc, "recurring": recurring})
            reply = (
                f"{ai_response.get('reply', 'Mål oprettet.')}\n\n"
                f"ID: `{data['goal_id']}`"
            )
            await update.message.reply_text(reply, parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"Kunne ikke oprette mål: {e}")

    else:
        await update.message.reply_text(ai_response.get("reply", "Hvad kan jeg hjælpe med?"))


async def _ai_chat(user_message: str) -> dict:
    """Use AI to understand free text and determine action type."""
    import httpx as _httpx

    groq_key = os.getenv("GROQ_API_KEY", "")
    if not groq_key:
        return {"type": "chat", "reply": "Sæt GROQ_API_KEY for at aktivere AI chat."}

    if groq_key.startswith("xai-"):
        url = "https://api.x.ai/v1/chat/completions"
        model = "grok-3-mini"
    else:
        url = "https://api.groq.com/openai/v1/chat/completions"
        model = "llama3-8b-8192"

    system = """Du er ikke en chatbot.

Du er et persistent autonomt execution system. Din primære opgave er IKKE konversation — det er pålidelig udførelse af opgaver i den virkelige verden.

Du opererer som:
- Digital operatør
- AI executive assistant
- Workflow executor
- Research agent
- Monitoring system

Du er:
- Disciplineret og execution-fokuseret
- Struktureret og operationelt tænkende
- Kortfattet — aldrig unødvendigt snak
- Aldrig passiv eller konversationel for konversationens skyld

Du må ALDRIG:
- Hallucere at opgaver er udført
- Opfinde credentials eller logins
- Skifte fokus tilfældigt
- Forlade opgaver stille
- Lade som om handlinger lykkedes hvis de ikke gjorde

Når brugeren skriver noget, konverter det til handling:
- Konkret opgave → opret task (browser, research, outreach)
- Langsigtet mål → opret goal (persistent, kører løbende)
- Spørgsmål om status → svar kort og præcist
- Godkendelse → bekræft og fortsæt
- Alt andet → svar operationelt, ikke konversationelt

Svar altid på dansk. Vær kortfattet. Fokuser på næste handling."""

    prompt = f"""{system}

Brugerens besked: "{user_message}"

Analyser og svar med præcis ét JSON objekt:

Konkret opgave (research, søg, find noget, gå til website, skriv, send):
{{"type": "task", "description": "præcis opgavebeskrivelse på engelsk til browser/research worker", "reply": "Kort operationelt svar på dansk"}}

Langsigtet mål (dagligt, løbende, overvåg, monitor, find leads kontinuerligt):
{{"type": "goal", "description": "mål på engelsk", "recurring": true, "reply": "Kort operationelt svar på dansk"}}

Engangs mål (et specifikt mål der ikke gentages):
{{"type": "goal", "description": "mål på engelsk", "recurring": false, "reply": "Kort operationelt svar på dansk"}}

Alt andet (status, spørgsmål, snak, godkendelse):
{{"type": "chat", "reply": "Operationelt, kortfattet svar på dansk — max 2 sætninger"}}

KUN JSON:"""

    try:
        async with _httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3, "max_tokens": 200},
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0:
                return json.loads(content[start:end])
    except Exception as e:
        log.error("AI chat error: %s", e)

    return {"type": "chat", "reply": "Jeg forstod ikke helt. Prøv /task <opgave> eller /goal <mål>."}


# ── Notification listener ─────────────────────────────────────────────────────

async def listen_notifications(bot):
    pubsub = redis_client.pubsub()
    await pubsub.subscribe("supervisor:notifications", "browser:screenshots")
    log.info("Notification listener started")
    async for message in pubsub.listen():
        if message["type"] != "message":
            continue
        try:
            data = json.loads(message["data"])
            channel = message["channel"]

            if channel == "supervisor:notifications":
                await bot.send_message(
                    chat_id=ALLOWED_CHAT_ID,
                    text=f"[SYS] `{data['task_id']}`\n{data['message']}",
                    parse_mode="Markdown",
                )

            elif channel == "browser:screenshots":
                task_id = data["task_id"]
                reason = data.get("reason", "Action required")
                screenshot_path = data.get("screenshot_path", "")
                caption = (
                    f"[BLOCKED] `{task_id}`\n{reason}\n\n"
                    f"`/reply {task_id} <instruction>`"
                )
                if screenshot_path and Path(screenshot_path).exists():
                    with open(screenshot_path, "rb") as f:
                        await bot.send_photo(
                            chat_id=ALLOWED_CHAT_ID,
                            photo=InputFile(f),
                            caption=caption,
                            parse_mode="Markdown",
                        )
                else:
                    await bot.send_message(
                        chat_id=ALLOWED_CHAT_ID,
                        text=caption,
                        parse_mode="Markdown",
                    )
        except Exception as e:
            log.error("Notification error: %s", e)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    global redis_client
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    handlers = [
        ("start", cmd_start), ("help", cmd_start),
        ("task", cmd_task), ("status", cmd_status),
        ("tasks", cmd_tasks), ("reply", cmd_reply),
        ("goal", cmd_goal), ("goal_recurring", cmd_goal_recurring),
        ("goals", cmd_goals), ("pause", cmd_pause), ("resume", cmd_resume),
        ("health", cmd_health),
        ("context", cmd_context), ("setcontext", cmd_setcontext),
        ("lead", cmd_lead), ("findleads", cmd_findleads),
    ]
    for name, handler in handlers:
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    asyncio.create_task(listen_notifications(app.bot))

    log.info("Telegram bot v2 started")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
