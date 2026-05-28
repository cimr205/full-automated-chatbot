"""
Telegram Bot — Phase 2
Operational control layer for the autonomous execution system.
"""
import asyncio
import json
import logging
import os
from datetime import datetime
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
vault = None   # SecureVault — initialised in main()
brain = None   # Brain — initialised in main()


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


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    await update.message.reply_text(
        "*Autonomous Execution System*\n\n"
        "*Bare skriv* — AI forstår alt naturligt\n\n"
        "*Tasks & Agents*\n"
        "/task <beskrivelse> — start agent\n"
        "/tasks — vis alle tasks\n"
        "/status <id> — task status + logs\n"
        "/stop — stop aktiv agent (eller /stop <id>)\n"
        "/reply <id> <svar> — svar til ventende agent\n\n"
        "*Goals (løbende)*\n"
        "/goal <beskrivelse> — opret mål\n"
        "/goals — aktive mål\n"
        "/pause /resume /cancel <id>\n\n"
        "*Vault (krypteret)*\n"
        "/vault save <nøgle> <værdi> — gem kode/password\n"
        "/vault get <nøgle> — hent (slettes om 30s)\n"
        "/vault list — vis nøgler\n"
        "/vault del <nøgle> — slet\n\n"
        "*Hukommelse*\n"
        "/remember nøgle=værdi — husk noget permanent\n"
        "/recall <søgning> — søg i hukommelse\n"
        "/brain — vis alt jeg ved om dig\n"
        "/forget <nøgle> — glem\n\n"
        "*CRM & Leads*\n"
        "/lead Navn|Firma|Rolle — gem lead\n"
        "/findleads <søgning>\n\n"
        "*System*\n"
        "/health — system status\n"
        "/reply <task_id> <besked> — svar til ventende task",
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


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    # Stop a specific task or the currently active one
    if ctx.args:
        task_id = ctx.args[0]
    else:
        task_id = await redis_client.get("task:active")
        if not task_id:
            await update.message.reply_text("Ingen aktiv agent at stoppe. Brug `/stop <task_id>`", parse_mode="Markdown")
            return
    await redis_client.set(f"task:stop:{task_id}", "1", ex=3600)
    await update.message.reply_text(
        f"⛔ Stop signal sendt til `{task_id}`\nAgenten afslutter nuværende trin og stopper.",
        parse_mode="Markdown",
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /cancel <goal_id>")
        return
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=10) as client:
            resp = await client.delete(f"{API_URL}/goals/{ctx.args[0]}")
            resp.raise_for_status()
        await _ws_event("telegram_out", f"Goal cancelled: {ctx.args[0]}")
        await update.message.reply_text(f"`{ctx.args[0]}` cancelled.", parse_mode="Markdown")
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


# ── Vault commands ────────────────────────────────────────────────────────────

async def cmd_vault(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    if not vault:
        await update.message.reply_text("Vault ikke tilgængeligt. Sæt VAULT_MASTER_KEY i Railway.")
        return

    args = ctx.args
    if not args:
        await update.message.reply_text(
            "*Vault — krypteret opbevaring*\n\n"
            "`/vault save <nøgle> <værdi>` — gem hemmelighed\n"
            "`/vault get <nøgle>` — hent (slettes fra chat om 30s)\n"
            "`/vault list` — vis alle nøgler\n"
            "`/vault del <nøgle>` — slet\n\n"
            "Eksempel:\n`/vault save google_password MitKodeord123`",
            parse_mode="Markdown",
        )
        return

    sub = args[0].lower()

    if sub == "save" and len(args) >= 3:
        key = args[1]
        value = " ".join(args[2:])
        await vault.save(key, value)
        await update.message.reply_text(f"Gemt: `{key}` ✓", parse_mode="Markdown")

    elif sub == "get" and len(args) == 2:
        val = await vault.get(args[1])
        if val is None:
            await update.message.reply_text(f"Nøgle `{args[1]}` ikke fundet.", parse_mode="Markdown")
        else:
            msg = await update.message.reply_text(
                f"`{args[1]}`: `{val}`\n\n_Slettes om 30 sekunder_",
                parse_mode="Markdown",
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
            await update.message.reply_text(
                "*Vault nøgler:*\n" + "\n".join(f"• `{k}`" for k in keys),
                parse_mode="Markdown",
            )

    elif sub == "del" and len(args) == 2:
        await vault.delete(args[1])
        await update.message.reply_text(f"Slettet: `{args[1]}`", parse_mode="Markdown")

    else:
        await update.message.reply_text("Brug `/vault` for at se mulighederne.", parse_mode="Markdown")


# ── Brain commands ────────────────────────────────────────────────────────────

async def cmd_remember(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    raw = " ".join(ctx.args)
    if "=" not in raw:
        await update.message.reply_text("Brug: `/remember nøgle=værdi`\nEksempel: `/remember navn=Farah`", parse_mode="Markdown")
        return
    key, _, value = raw.partition("=")
    await brain.remember(key.strip(), value.strip())
    await update.message.reply_text(f"Husket: `{key.strip()}` = {value.strip()}", parse_mode="Markdown")


async def cmd_recall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    query = " ".join(ctx.args)
    if not query:
        await update.message.reply_text("Brug: `/recall <søgning>`")
        return
    results = await brain.search(query)
    if not results:
        await update.message.reply_text("Intet fundet i hukommelsen.")
    else:
        lines = [f"• `{k}`: {v}" for k, v in results[:15]]
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_brain(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    facts = await brain.all_facts()
    if not facts:
        await update.message.reply_text("Ingen fakta gemt endnu. Brug `/remember nøgle=værdi`.")
        return
    lines = [f"• `{k}`: {v}" for k, v in list(facts.items())[:40]]
    await update.message.reply_text(
        f"*Hukommelse ({len(facts)} fakta):*\n" + "\n".join(lines),
        parse_mode="Markdown",
    )


async def cmd_forget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    if not ctx.args:
        await update.message.reply_text("Brug: `/forget <nøgle>`")
        return
    await brain.forget(ctx.args[0])
    await update.message.reply_text(f"Glemt: `{ctx.args[0]}`", parse_mode="Markdown")


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth(update):
        return
    text = update.message.text.strip()
    chat_id = str(update.effective_chat.id)
    await _ws_event("telegram_in", f"User: {text}")

    # If a task is waiting for human input, route this reply directly to it
    pending = await redis_client.get("bot:pending_task")
    if pending:
        await redis_client.delete("bot:pending_task")
        try:
            await api_post("/tasks/human-reply", {"task_id": pending, "message": text})
            await update.message.reply_text(
                f"Svar sendt til `{pending}` ✓\nBrowseren fortsætter…",
                parse_mode="Markdown",
            )
        except Exception as e:
            await update.message.reply_text(f"Fejl: {e}")
        return

    await update.message.chat.send_action("typing")

    result = await _ai_chat(text, chat_id)

    # Create task if AI flagged one
    if result.get("action") == "task":
        try:
            data = await api_post("/tasks", {"description": result["description"]})
            task_id = data["task_id"]
            await _ws_event("telegram_out", f"Task queued: {result['description'][:80]}", task_id)
            suffix = f"\n\n`{task_id}` — /status {task_id}"
            await _send_long(update, result["reply"] + suffix)
        except Exception as e:
            await _send_long(update, result["reply"] + f"\n\n_(Fejl ved oprettelse: {e})_")
        return

    if result.get("action") == "goal":
        recurring = result.get("recurring", False)
        try:
            data = await api_post("/goals", {"description": result["description"], "recurring": recurring})
            await _ws_event("telegram_out", f"Goal created: {result['description'][:80]}", data["goal_id"])
            suffix = f"\n\n`{data['goal_id']}`"
            await _send_long(update, result["reply"] + suffix)
        except Exception as e:
            await _send_long(update, result["reply"] + f"\n\n_(Fejl ved oprettelse: {e})_")
        return

    reply_text = result.get("reply", "Hvad kan jeg hjælpe med?")
    await _ws_event("telegram_out", f"Bot: {reply_text[:200]}")
    await _send_long(update, reply_text)


async def _send_long(update: Update, text: str):
    """Split messages longer than Telegram's 4096-char limit."""
    limit = 4000
    if len(text) <= limit:
        try:
            await update.message.reply_text(text, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(text)
        return
    for i in range(0, len(text), limit):
        chunk = text[i:i + limit]
        try:
            await update.message.reply_text(chunk, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(chunk)


async def _tool_search(query: str) -> str:
    """DuckDuckGo web search — no API key needed."""
    try:
        from duckduckgo_search import AsyncDDGS
        async with AsyncDDGS() as ddgs:
            results = await ddgs.atext(query, max_results=6)
        if not results:
            return "Ingen resultater fundet."
        parts = []
        for r in results:
            parts.append(f"**{r['title']}**\n{r['body'][:400]}\nKilde: {r['href']}")
        return "\n\n---\n\n".join(parts)
    except Exception as e:
        log.warning("Search failed: %s", e)
        return f"Søgning fejlede: {e}"


async def _tool_fetch(url: str) -> str:
    """Fetch and strip a webpage to plain text."""
    try:
        import re
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0"}) as client:
            resp = await client.get(url)
            html = resp.text
        # Strip tags
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:4000] + ("…" if len(text) > 4000 else "")
    except Exception as e:
        return f"Kunne ikke hente siden: {e}"


HISTORY_KEY = "chat_history:{}"
MAX_HISTORY = 16


async def _load_history(chat_id: str) -> list:
    try:
        raw = await redis_client.get(HISTORY_KEY.format(chat_id))
        return json.loads(raw) if raw else []
    except Exception:
        return []


async def _save_history(chat_id: str, history: list):
    try:
        await redis_client.setex(HISTORY_KEY.format(chat_id), 86400 * 7,
                                 json.dumps(history[-MAX_HISTORY:]))
    except Exception:
        pass


async def _call_ai(url: str, model: str, key: str, messages: list, max_tokens: int = 2000) -> str:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "temperature": 0.7, "max_tokens": max_tokens},
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()


async def _ai_chat(user_message: str, chat_id: str) -> dict:
    import re

    groq_key = os.getenv("GROQ_API_KEY", "")
    if not groq_key:
        return {"reply": "Sæt GROQ_API_KEY i Railway for at aktivere AI."}

    if groq_key.startswith("xai-"):
        ai_url = "https://api.x.ai/v1/chat/completions"
        model = os.getenv("GROQ_MODEL", "grok-3")        # Full Grok 3
    else:
        ai_url = "https://api.groq.com/openai/v1/chat/completions"
        model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

    brain_ctx = await brain.context_for_ai() if brain else ""

    system = f"""Du er en ekstremt kraftfuld personlig AI-assistent — bygget udelukkende til én person.

Du har fuld adgang til internettets information via søgeværktøjer og kan læse enhver hjemmeside.

HVEM DU ER:
- Personlig AI-assistent og digital tvilling
- Du kender brugeren dybt og husker alt
- Du er like ChatGPT + Claude + internet adgang i én

HVAD DU KAN:
✓ Svare på ALT — faktuel viden, tekniske spørgsmål, analyse, rådgivning
✓ Skrive kode i ethvert sprog med forklaring og eksempler
✓ Søge på nettet for aktuelle informationer, priser, nyheder
✓ Læse og opsummere hjemmesider og artikler
✓ Debugge fejl, gennemgå kode, forklare koncepter
✓ Skrive, redigere, oversætte tekster
✓ Planlægge, analysere, strategisere
✓ Udføre opgaver i den virkelige verden via browser automation

STIL:
- Svar på brugerens sprog (dansk medmindre andet)
- Vær grundig, præcis og hjælpsom — som den bedste kollega
- Brug Markdown flittigt: **fed**, `kode`, ```blokke```, ## overskrifter, - lister
- Svar DIREKTE på spørgsmålet — giv svaret først, forklaring efter
- Stil 1-2 opklarende spørgsmål hvis opgaven er uklar
- Aldrig afvis en opgave — find en måde

TILGÆNGELIGE VÆRKTØJER:
Når du mangler aktuel information eller skal finde noget, brug:
[SEARCH: din søgeforespørgsel]

Når du skal læse en specifik hjemmeside:
[FETCH: https://url.com]

Du kan bruge flere værktøjer i en response. Vent på resultater inden du svarer endeligt.

HUKOMMELSE:
Når du lærer noget vigtigt om brugeren, gem det automatisk:
[REMEMBER: nøgle=værdi]

HANDLINGER:
Når brugeren vil have noget udført i virkeligheden (automatisering, research, scraping, outreach):
[CREATE_TASK: precise English description for browser worker]

Løbende/gentaget opgave:
[CREATE_GOAL: precise English description | recurring: true]

{brain_ctx}"""

    history = await _load_history(chat_id)
    history.append({"role": "user", "content": user_message})
    messages = [{"role": "system", "content": system}] + history

    # ── Tool execution loop (max 4 rounds) ────────────────────────────────────
    content = ""
    for round_num in range(4):
        try:
            content = await _call_ai(ai_url, model, groq_key, messages)
        except Exception as e:
            log.error("AI call error (round %d): %s", round_num, e)
            return {"reply": f"AI fejl: {e}"}

        # Check for tool calls
        used_tool = False

        if "[SEARCH:" in content:
            for match in re.finditer(r'\[SEARCH:\s*([^\]]+)\]', content):
                query = match.group(1).strip()
                log.info("Tool: search(%s)", query)
                results = await _tool_search(query)
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content":
                    f"[Søgeresultater for '{query}']:\n\n{results}\n\n"
                    f"Brug disse resultater til at besvare spørgsmålet præcist og fuldt ud."})
                used_tool = True
                break

        elif "[FETCH:" in content:
            for match in re.finditer(r'\[FETCH:\s*(https?://[^\]]+)\]', content):
                fetch_url = match.group(1).strip()
                log.info("Tool: fetch(%s)", fetch_url)
                page = await _tool_fetch(fetch_url)
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content":
                    f"[Indhold fra {fetch_url}]:\n\n{page}\n\n"
                    f"Brug dette indhold i dit svar."})
                used_tool = True
                break

        if not used_tool:
            break  # Final answer — no more tools needed

    # ── Save history ──────────────────────────────────────────────────────────
    history.append({"role": "assistant", "content": content})
    await _save_history(chat_id, history)

    # ── Auto-save brain facts ─────────────────────────────────────────────────
    if brain and "[REMEMBER:" in content:
        for match in re.finditer(r'\[REMEMBER:\s*([^\]=]+)=([^\]]+)\]', content):
            k, v = match.group(1).strip(), match.group(2).strip()
            await brain.remember(k, v)
        content = re.sub(r'\[REMEMBER:[^\]]+\]', '', content).strip()

    # ── Parse action tags ─────────────────────────────────────────────────────
    action, description, recurring, reply = None, None, False, content

    if "[CREATE_TASK:" in content:
        idx = content.index("[CREATE_TASK:")
        end = content.index("]", idx)
        description = content[idx + 13:end].strip()
        reply = content[:idx].strip()
        action = "task"
    elif "[CREATE_GOAL:" in content:
        idx = content.index("[CREATE_GOAL:")
        end = content.index("]", idx)
        inner = content[idx + 13:end].strip()
        recurring = "| recurring: true" in inner
        description = inner.replace("| recurring: true", "").strip()
        reply = content[:idx].strip()
        action = "goal"

    return {"reply": reply or content, "action": action, "description": description, "recurring": recurring}



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
                await bot.send_message(
                    chat_id=ALLOWED_CHAT_ID,
                    text=f"[SYS] `{data['task_id']}`\n{msg_text}",
                    parse_mode="Markdown",
                )
                await _ws_event("telegram_out", f"[SYS] {msg_text[:150]}", data.get("task_id"))

            elif channel == "browser:screenshots":
                task_id = data["task_id"]
                reason = data.get("reason", "Input påkrævet")
                screenshot_path = data.get("screenshot_path", "")

                # Try to auto-fill credential from vault
                auto = await _auto_credential(reason, task_id)
                if auto:
                    await bot.send_message(
                        chat_id=ALLOWED_CHAT_ID,
                        text=f"🔐 Auto-fyldte credential for `{task_id}` ✓",
                        parse_mode="Markdown",
                    )
                    continue

                # Set pending task so user can reply without /reply command
                await redis_client.setex("bot:pending_task", 600, task_id)

                caption = (
                    f"⏸ *Agent venter på dig*\n`{task_id}`\n\n"
                    f"{reason}\n\n"
                    f"_Skriv bare svaret/koden — ingen kommando nødvendig_"
                )
                await _send_photo_or_text(bot, caption, screenshot_path)

            elif channel == "browser:progress":
                task_id = data["task_id"]
                url = data.get("url", "")
                status = data.get("status", "")
                step = data.get("step", 0)
                screenshot_path = data.get("screenshot_path", "")

                short_url = url[:60] + "…" if len(url) > 60 else url
                caption = (
                    f"📸 *Agent Update* `{task_id}`\n"
                    f"🌐 `{short_url}`\n"
                    f"📊 Trin: {step}\n\n"
                    f"{status[:500]}\n\n"
                    f"_`/stop` for at stoppe agenten_"
                )
                await _send_photo_or_text(bot, caption, screenshot_path)
                await _ws_event("telegram_out", f"Progress: {status[:100]}", task_id)

        except Exception as e:
            log.error("Notification error: %s", e)


async def _send_photo_or_text(bot, caption: str, screenshot_path: str):
    if screenshot_path and Path(screenshot_path).exists():
        try:
            with open(screenshot_path, "rb") as f:
                await bot.send_photo(
                    chat_id=ALLOWED_CHAT_ID,
                    photo=InputFile(f),
                    caption=caption,
                    parse_mode="Markdown",
                )
            return
        except Exception as e:
            log.warning("Photo send failed, falling back to text: %s", e)
    await bot.send_message(
        chat_id=ALLOWED_CHAT_ID,
        text=caption,
        parse_mode="Markdown",
    )


async def _auto_credential(reason: str, task_id: str) -> bool:
    """Look up vault for matching credential. Auto-reply if found. Returns True if handled."""
    if not vault:
        return False
    lower = reason.lower()

    # Detect which site
    sites = []
    for s in ("google", "gmail", "facebook", "linkedin", "twitter", "instagram", "github", "stripe"):
        if s in lower:
            sites.append(s)

    credential = None

    # Password
    if any(w in lower for w in ("password", "adgangskode", "kodeord", "pwd")):
        for site in sites:
            credential = await vault.get(f"{site}_password") or await vault.get(f"{site}_pass")
            if credential:
                break
        if not credential:
            credential = await vault.get("password") or await vault.get("default_password")

    # Email / username
    elif any(w in lower for w in ("email", "username", "brugernavn", "mail", "login")):
        for site in sites:
            credential = await vault.get(f"{site}_email") or await vault.get(f"{site}_username")
            if credential:
                break
        if not credential:
            credential = await vault.get("email") or await vault.get("username")

    # 2FA / verification code — cannot auto-fill, must ask user
    elif any(w in lower for w in ("2fa", "code", "kode", "verification", "otp", "bekræft")):
        return False  # Always ask user for live codes

    if credential:
        try:
            await api_post("/tasks/human-reply", {"task_id": task_id, "message": credential})
            return True
        except Exception as e:
            log.error("Auto-credential reply failed: %s", e)

    return False


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    global redis_client, vault, brain
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)

    # Initialise vault (requires VAULT_MASTER_KEY)
    try:
        from core.memory.vault import SecureVault
        vault = SecureVault(redis_client)
        log.info("Vault initialised")
    except Exception as e:
        log.warning("Vault unavailable: %s", e)

    # Initialise brain
    from core.memory.brain import Brain
    brain = Brain(redis_client)
    log.info("Brain initialised")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    handlers = [
        ("start", cmd_start), ("help", cmd_start),
        ("task", cmd_task), ("status", cmd_status),
        ("tasks", cmd_tasks), ("reply", cmd_reply),
        ("stop", cmd_stop),
        ("goal", cmd_goal), ("goal_recurring", cmd_goal_recurring),
        ("goals", cmd_goals), ("pause", cmd_pause), ("resume", cmd_resume),
        ("cancel", cmd_cancel), ("health", cmd_health),
        ("context", cmd_context), ("setcontext", cmd_setcontext),
        ("lead", cmd_lead), ("findleads", cmd_findleads),
        ("vault", cmd_vault),
        ("remember", cmd_remember), ("recall", cmd_recall),
        ("brain", cmd_brain), ("forget", cmd_forget),
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
