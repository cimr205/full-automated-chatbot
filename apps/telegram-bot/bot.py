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
app: Application = None


async def auth_check(update: Update) -> bool:
    if ALLOWED_CHAT_ID and update.effective_chat.id != ALLOWED_CHAT_ID:
        await update.message.reply_text("Unauthorized.")
        return False
    return True


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update):
        return
    await update.message.reply_text(
        "Autonomous Execution System online.\n\n"
        "Commands:\n"
        "/task <description> — queue a new task\n"
        "/status [task_id] — check task status\n"
        "/tasks — list recent tasks\n"
        "/reply <task_id> <message> — reply to waiting task\n"
        "/cancel <task_id> — cancel a task\n"
        "/help — show this message"
    )


async def cmd_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update):
        return
    description = " ".join(ctx.args)
    if not description:
        await update.message.reply_text("Usage: /task <description>")
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{API_URL}/tasks", json={"description": description})
            resp.raise_for_status()
            data = resp.json()
        await update.message.reply_text(
            f"Task queued.\nID: `{data['task_id']}`\nStatus: {data['status']}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"Error creating task: {e}")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /status <task_id>")
        return
    task_id = ctx.args[0]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{API_URL}/tasks/{task_id}")
            resp.raise_for_status()
            task = resp.json()
        status = task.get("status", "unknown")
        desc = task.get("description", "")[:60]
        retries = task.get("retry_count", 0)
        logs = task.get("logs", [])[-3:]
        log_text = "\n".join(logs) if logs else "No logs yet"
        msg = (
            f"*{task_id}*\n"
            f"Status: `{status}`\n"
            f"Task: {desc}\n"
            f"Retries: {retries}\n"
            f"Recent:\n```\n{log_text}\n```"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update):
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{API_URL}/tasks")
            resp.raise_for_status()
            tasks = resp.json()
        if not tasks:
            await update.message.reply_text("No tasks found.")
            return
        lines = []
        for t in tasks[:10]:
            lines.append(f"• `{t['task_id']}` [{t.get('status','?')}] {t.get('description','')[:40]}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update):
        return
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: /reply <task_id> <message>")
        return
    task_id = ctx.args[0]
    message = " ".join(ctx.args[1:])
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{API_URL}/tasks/human-reply",
                json={"task_id": task_id, "message": message},
            )
            resp.raise_for_status()
        await update.message.reply_text(f"Reply sent to `{task_id}`.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update):
        return
    text = update.message.text.strip()
    if not text.startswith("/"):
        await update.message.reply_text(
            "Use /task <description> to queue a task, or /help for commands."
        )


async def listen_notifications(bot):
    global redis_client
    pubsub = redis_client.pubsub()
    await pubsub.subscribe("supervisor:notifications", "browser:screenshots")
    log.info("Listening for notifications")
    async for message in pubsub.listen():
        if message["type"] != "message":
            continue
        try:
            data = json.loads(message["data"])
            channel = message["channel"]

            if channel == "supervisor:notifications":
                text = (
                    f"[SYSTEM] `{data['task_id']}`\n{data['message']}"
                )
                await bot.send_message(
                    chat_id=ALLOWED_CHAT_ID,
                    text=text,
                    parse_mode="Markdown",
                )

            elif channel == "browser:screenshots":
                task_id = data["task_id"]
                reason = data.get("reason", "Human input required")
                screenshot_path = data.get("screenshot_path", "")
                caption = (
                    f"[BLOCKED] `{task_id}`\n"
                    f"{reason}\n\n"
                    f"Reply with:\n`/reply {task_id} <instruction>`"
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


async def main():
    global redis_client, app
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("task", cmd_task))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("reply", cmd_reply))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    asyncio.create_task(listen_notifications(app.bot))

    log.info("Telegram bot started")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
