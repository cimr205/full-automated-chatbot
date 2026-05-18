"""
Browser Worker — executes tasks via persistent Playwright session.

Phase 2 additions:
- Anti-detection timing (realistic delays)
- Memory-aware context (experience retrieval)
- Plan-aware step execution
- Approval gating before irreversible actions
"""
import asyncio
import json
import logging
import os
import random
from datetime import datetime
from pathlib import Path
from typing import Optional

import redis.asyncio as aioredis
from playwright.async_api import async_playwright, BrowserContext, Page

from core.queue.task_queue import TaskQueue
from core.supervisor.supervisor import HumanRequiredError
from workers.browser.ollama_client import OllamaClient

log = logging.getLogger(__name__)

SCREENSHOT_DIR = Path("/tmp/screenshots")
BROWSER_DATA_DIR = Path(os.getenv("BROWSER_DATA_DIR", "/tmp/browser-data"))
HUMAN_REPLY_TIMEOUT = int(os.getenv("HUMAN_REPLY_TIMEOUT", "300"))
MAX_TASK_STEPS = int(os.getenv("MAX_TASK_STEPS", "25"))

APPROVAL_ACTIONS = {"send", "post", "publish", "submit_outreach", "deploy"}


class BrowserWorker:
    def __init__(self, task: dict, queue: TaskQueue, redis: aioredis.Redis, memory=None):
        self.task = task
        self.queue = queue
        self.redis = redis
        self.memory = memory
        self.ollama = OllamaClient()
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._consecutive_waits = 0
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    async def execute(self):
        task_id = self.task["task_id"]
        log.info("[%s] Browser execution starting", task_id)
        checkpoint = self.task.get("_checkpoint")

        async with async_playwright() as pw:
            self._context = await pw.chromium.launch_persistent_context(
                str(BROWSER_DATA_DIR),
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            await self._context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )

            self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()

            if checkpoint and checkpoint.get("url"):
                log.info("[%s] Restoring from checkpoint: %s", task_id, checkpoint["url"])
                await self._safe_goto(checkpoint["url"])

            await self._run_ai_loop()
            await self._context.close()

    async def _run_ai_loop(self):
        task_id = self.task["task_id"]
        description = self.task["description"]

        past_experience = []
        if self.memory:
            past_experience = await self.memory.query_experience(description[:100])

        for step in range(MAX_TASK_STEPS):
            log.info("[%s] Step %d/%d | URL: %s", task_id, step + 1, MAX_TASK_STEPS, self._page.url)

            screenshot_path = await self._screenshot(f"{task_id}_step{step:02d}")
            page_text = await self._get_page_text()
            current_url = self._page.url

            await self._save_checkpoint(current_url, step, page_text)

            prompt = self._build_prompt(description, step, current_url, page_text, past_experience)
            ai_response = await self.ollama.reason(prompt)
            action = self._parse_action(ai_response)

            log.info("[%s] Action: %s", task_id, json.dumps(action)[:120])

            await self._execute_action(action, screenshot_path, step)

            if action.get("type") == "complete":
                self._log("Task marked complete by AI")
                return

            self._consecutive_waits = (self._consecutive_waits + 1) if action.get("type") == "wait" else 0
            if self._consecutive_waits >= 5:
                raise HumanRequiredError("AI stuck in wait loop — manual intervention needed")

        log.warning("[%s] Reached max steps", task_id)

    async def _execute_action(self, action: dict, screenshot_path: str, step: int):
        t = action.get("type", "wait")

        if t == "complete":
            return

        if t == "need_human":
            reason = action.get("reason", "Human input required")
            await self._send_screenshot_notification(screenshot_path, reason)
            reply = await self._wait_for_human_reply()
            await self._handle_human_reply(reply)
            return

        if t == "approve_required":
            action_desc = action.get("description", "Action requires approval")
            await self._request_approval(screenshot_path, action_desc)
            reply = await self._wait_for_human_reply()
            if reply.lower().strip() not in ("yes", "ok", "approve", "y", "confirmed"):
                self._log(f"Approval denied: {action_desc}")
                return
            self._log(f"Approved: {action_desc}")
            return

        if t == "navigate":
            url = action.get("url", "")
            if url:
                await self._safe_goto(url)

        elif t == "click":
            selector = action.get("selector", "")
            text = action.get("text", "")
            if selector:
                await self._safe_click(selector)
            elif text:
                await self._click_by_text(text)

        elif t == "type":
            selector = action.get("selector", "")
            value = action.get("value", "")
            if selector and value:
                await self._safe_type(selector, value)

        elif t == "scroll":
            direction = action.get("direction", "down")
            amount = action.get("amount", 500) * (-1 if direction == "up" else 1)
            await self._page.evaluate(f"window.scrollBy(0, {amount})")

        elif t == "extract":
            data = await self._extract_data(action.get("fields", []))
            self._log(f"Extracted: {json.dumps(data)[:200]}")
            self.task.setdefault("extracted_data", []).append(data)

        elif t == "wait":
            await asyncio.sleep(action.get("seconds", 2))

        await self._human_delay()

    def _build_prompt(self, description: str, step: int, url: str, page_text: str, experience: list) -> str:
        recent_logs = "\n".join(self.task.get("logs", [])[-6:])
        exp_text = ""
        if experience:
            exp_text = "PAST EXPERIENCE:\n" + "\n".join(
                f"- {e.get('text','')[:120]}" for e in experience[:2]
            )

        return f"""You are a browser automation AI. Execute the task reliably and safely.

TASK: {description[:600]}
STEP: {step + 1}/{MAX_TASK_STEPS}
URL: {url}
PAGE (truncated): {page_text[:1800]}

RECENT ACTIONS:
{recent_logs}
{exp_text}

Respond with ONLY one JSON action:

{{"type": "navigate", "url": "https://..."}}
{{"type": "click", "selector": "CSS_SELECTOR"}}
{{"type": "click", "text": "VISIBLE_BUTTON_TEXT"}}
{{"type": "type", "selector": "CSS_SELECTOR", "value": "TEXT"}}
{{"type": "scroll", "direction": "down", "amount": 500}}
{{"type": "extract", "fields": ["name", "email", "company"]}}
{{"type": "wait", "seconds": 2}}
{{"type": "need_human", "reason": "EXPLAIN_WHAT_IS_NEEDED"}}
{{"type": "approve_required", "description": "WHAT_YOU_ARE_ABOUT_TO_DO"}}
{{"type": "complete"}}

Rules:
- Use need_human for logins, captchas, 2FA, or anything blocking
- Use approve_required before sending messages, posting, or any irreversible action
- Use complete only when the task outcome is fully verified
- Never fabricate success
- Prefer conservative actions
- Avoid repeating the same action twice in a row

JSON:"""

    def _parse_action(self, response: str) -> dict:
        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(response[start:end])
        except Exception as e:
            log.warning("Action parse failed: %s | raw: %.100s", e, response)
        return {"type": "wait", "seconds": 3}

    async def _human_delay(self):
        """Realistic timing to avoid bot detection."""
        await asyncio.sleep(random.uniform(0.8, 2.5))

    async def _screenshot(self, name: str) -> str:
        path = str(SCREENSHOT_DIR / f"{name}.png")
        try:
            await self._page.screenshot(path=path, full_page=False)
        except Exception as e:
            log.warning("Screenshot failed: %s", e)
        return path

    async def _get_page_text(self) -> str:
        try:
            return await self._page.evaluate("document.body.innerText")
        except Exception:
            return ""

    async def _safe_goto(self, url: str):
        try:
            await self._page.goto(url, timeout=30000, wait_until="domcontentloaded")
            self._log(f"→ {url}")
        except Exception as e:
            log.warning("Navigate failed %s: %s", url, e)

    async def _safe_click(self, selector: str):
        try:
            await self._page.click(selector, timeout=5000)
            self._log(f"Click [{selector}]")
        except Exception as e:
            log.warning("Click %s failed: %s", selector, e)

    async def _click_by_text(self, text: str):
        try:
            await self._page.get_by_text(text, exact=False).first.click(timeout=5000)
            self._log(f"Click text: {text}")
        except Exception as e:
            log.warning("Click text '%s' failed: %s", text, e)

    async def _safe_type(self, selector: str, value: str):
        try:
            await self._page.fill(selector, value, timeout=5000)
            await asyncio.sleep(random.uniform(0.3, 0.8))
            self._log(f"Type into [{selector}]")
        except Exception as e:
            log.warning("Type %s failed: %s", selector, e)

    async def _extract_data(self, fields: list) -> dict:
        data = {}
        for field in fields:
            try:
                val = await self._page.evaluate(
                    f"document.querySelector('[data-field=\"{field}\"], [name=\"{field}\"]')?.innerText"
                )
                if val:
                    data[field] = val
            except Exception:
                pass
        return data

    async def _save_checkpoint(self, url: str, step: int, page_text: str):
        cp = {
            "url": url,
            "step": step,
            "ts": datetime.utcnow().isoformat(),
            "page_snippet": page_text[:500],
        }
        await self.queue.save_checkpoint(self.task["task_id"], cp)

    async def _send_screenshot_notification(self, screenshot_path: str, reason: str):
        await self.redis.publish("browser:screenshots", json.dumps({
            "task_id": self.task["task_id"],
            "screenshot_path": screenshot_path,
            "reason": reason,
            "ts": datetime.utcnow().isoformat(),
        }))

    async def _request_approval(self, screenshot_path: str, description: str):
        await self.redis.publish("browser:screenshots", json.dumps({
            "task_id": self.task["task_id"],
            "screenshot_path": screenshot_path,
            "reason": f"APPROVAL REQUIRED: {description}\n\nReply yes/no",
            "ts": datetime.utcnow().isoformat(),
        }))

    async def _wait_for_human_reply(self) -> str:
        task_id = self.task["task_id"]
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(f"human_reply:{task_id}")
        try:
            deadline = asyncio.get_event_loop().time() + HUMAN_REPLY_TIMEOUT
            async for message in pubsub.listen():
                if asyncio.get_event_loop().time() > deadline:
                    raise HumanRequiredError("Timed out waiting for human reply")
                if message["type"] == "message":
                    data = json.loads(message["data"])
                    return data.get("message", "")
        finally:
            await pubsub.unsubscribe(f"human_reply:{task_id}")
            await pubsub.close()
        return ""

    async def _handle_human_reply(self, reply: str):
        self._log(f"Human: {reply}")
        lower = reply.lower().strip()
        if lower.startswith("click "):
            await self._click_by_text(reply[6:].strip())
        elif lower.startswith("type ") and " into " in lower:
            parts = reply[5:].split(" into ", 1)
            await self._safe_type(parts[1].strip(), parts[0].strip())
        elif lower.startswith("goto ") or lower.startswith("navigate "):
            await self._safe_goto(reply.split(" ", 1)[1].strip())
        elif lower.startswith("http"):
            await self._safe_goto(reply.strip())

    def _log(self, message: str):
        entry = f"[{datetime.utcnow().strftime('%H:%M:%S')}] {message}"
        logs = self.task.setdefault("logs", [])
        logs.append(entry)
        if len(logs) > 100:
            self.task["logs"] = logs[-100:]
        log.info("[%s] %s", self.task["task_id"], message)
