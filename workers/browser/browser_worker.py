import asyncio
import json
import logging
import os
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


class BrowserWorker:
    def __init__(self, task: dict, queue: TaskQueue, redis: aioredis.Redis):
        self.task = task
        self.queue = queue
        self.redis = redis
        self.ollama = OllamaClient()
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    async def execute(self):
        task_id = self.task["task_id"]
        log.info("[%s] Starting browser execution", task_id)

        checkpoint = self.task.get("_checkpoint")

        async with async_playwright() as pw:
            self._context = await pw.chromium.launch_persistent_context(
                str(BROWSER_DATA_DIR),
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()

            if checkpoint and checkpoint.get("url"):
                log.info("[%s] Restoring checkpoint URL: %s", task_id, checkpoint["url"])
                await self._page.goto(checkpoint["url"], timeout=30000)

            await self._run_ai_loop()
            await self._context.close()

    async def _run_ai_loop(self):
        task_id = self.task["task_id"]
        description = self.task["description"]
        max_steps = int(os.getenv("MAX_TASK_STEPS", "20"))

        for step in range(max_steps):
            log.info("[%s] Step %d/%d", task_id, step + 1, max_steps)

            screenshot_path = await self._screenshot(f"{task_id}_step{step}")
            page_text = await self._get_page_text()
            current_url = self._page.url

            await self._save_checkpoint(current_url, step, page_text)

            prompt = self._build_prompt(description, step, current_url, page_text)
            ai_response = await self.ollama.reason(prompt)
            log.info("[%s] AI response: %s", task_id, ai_response[:200])

            action = self._parse_action(ai_response)

            if action.get("type") == "complete":
                log.info("[%s] AI signals task complete", task_id)
                self._log(f"Task completed at step {step + 1}")
                return

            if action.get("type") == "need_human":
                reason = action.get("reason", "Human input required")
                await self._send_screenshot_notification(screenshot_path, reason)
                reply = await self._wait_for_human_reply()
                await self._handle_human_reply(reply)
                continue

            if action.get("type") == "navigate":
                url = action.get("url", "")
                if url:
                    await self._safe_goto(url)

            elif action.get("type") == "click":
                selector = action.get("selector", "")
                text = action.get("text", "")
                if selector:
                    await self._safe_click(selector)
                elif text:
                    await self._click_by_text(text)

            elif action.get("type") == "type":
                selector = action.get("selector", "")
                value = action.get("value", "")
                if selector and value:
                    await self._safe_type(selector, value)

            elif action.get("type") == "scroll":
                await self._page.evaluate("window.scrollBy(0, 500)")

            elif action.get("type") == "wait":
                await asyncio.sleep(action.get("seconds", 2))

            await asyncio.sleep(1)

        log.warning("[%s] Reached max steps without completion", task_id)

    def _build_prompt(self, description: str, step: int, url: str, page_text: str) -> str:
        history = "\n".join(self.task.get("logs", [])[-5:])
        return f"""You are a browser automation AI. Execute tasks reliably.

TASK: {description}
STEP: {step + 1}
CURRENT URL: {url}
PAGE CONTENT (truncated): {page_text[:2000]}
RECENT ACTIONS:
{history}

Respond with ONLY valid JSON. Choose one action:

{{"type": "navigate", "url": "https://..."}}
{{"type": "click", "selector": "CSS_SELECTOR"}}
{{"type": "click", "text": "VISIBLE_TEXT"}}
{{"type": "type", "selector": "CSS_SELECTOR", "value": "TEXT_TO_TYPE"}}
{{"type": "scroll"}}
{{"type": "wait", "seconds": 2}}
{{"type": "need_human", "reason": "EXPLANATION"}}
{{"type": "complete"}}

Rules:
- Use need_human if login/captcha/approval is required
- Use complete when the task is fully done
- Be precise with selectors
- If blocked 3 times, use need_human

JSON:"""

    def _parse_action(self, response: str) -> dict:
        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(response[start:end])
        except Exception as e:
            log.warning("Failed to parse AI action: %s | raw: %s", e, response[:100])
        return {"type": "wait", "seconds": 2}

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
            self._log(f"Navigated to {url}")
        except Exception as e:
            log.warning("Navigate failed: %s", e)

    async def _safe_click(self, selector: str):
        try:
            await self._page.click(selector, timeout=5000)
            self._log(f"Clicked {selector}")
        except Exception as e:
            log.warning("Click failed for %s: %s", selector, e)

    async def _click_by_text(self, text: str):
        try:
            await self._page.get_by_text(text, exact=False).first.click(timeout=5000)
            self._log(f"Clicked text: {text}")
        except Exception as e:
            log.warning("Click by text failed '%s': %s", text, e)

    async def _safe_type(self, selector: str, value: str):
        try:
            await self._page.fill(selector, value, timeout=5000)
            self._log(f"Typed into {selector}")
        except Exception as e:
            log.warning("Type failed for %s: %s", selector, e)

    async def _save_checkpoint(self, url: str, step: int, page_text: str):
        checkpoint = {
            "url": url,
            "step": step,
            "ts": datetime.utcnow().isoformat(),
            "page_snippet": page_text[:500],
        }
        await self.queue.save_checkpoint(self.task["task_id"], checkpoint)

    async def _send_screenshot_notification(self, screenshot_path: str, reason: str):
        task_id = self.task["task_id"]
        await self.redis.publish("browser:screenshots", json.dumps({
            "task_id": task_id,
            "screenshot_path": screenshot_path,
            "reason": reason,
            "ts": datetime.utcnow().isoformat(),
        }))

    async def _wait_for_human_reply(self) -> str:
        task_id = self.task["task_id"]
        channel = f"human_reply:{task_id}"
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(channel)

        try:
            deadline = asyncio.get_event_loop().time() + HUMAN_REPLY_TIMEOUT
            async for message in pubsub.listen():
                if asyncio.get_event_loop().time() > deadline:
                    raise HumanRequiredError("Timed out waiting for human reply")
                if message["type"] == "message":
                    data = json.loads(message["data"])
                    return data.get("message", "")
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()

        return ""

    async def _handle_human_reply(self, reply: str):
        log.info("[%s] Human replied: %s", self.task["task_id"], reply)
        self._log(f"Human: {reply}")
        lower = reply.lower().strip()

        if lower.startswith("click "):
            text = reply[6:].strip()
            await self._click_by_text(text)
        elif lower.startswith("type "):
            parts = reply[5:].split(" into ", 1)
            if len(parts) == 2:
                await self._safe_type(parts[1].strip(), parts[0].strip())
            else:
                log.warning("Could not parse type instruction: %s", reply)
        elif lower.startswith("goto ") or lower.startswith("navigate "):
            url = reply.split(" ", 1)[1].strip()
            await self._safe_goto(url)
        elif lower.startswith("http"):
            await self._safe_goto(reply.strip())
        else:
            log.info("Unstructured human reply, continuing AI loop")

    def _log(self, message: str):
        entry = f"[{datetime.utcnow().strftime('%H:%M:%S')}] {message}"
        logs = self.task.setdefault("logs", [])
        logs.append(entry)
        if len(logs) > 100:
            self.task["logs"] = logs[-100:]
        log.info("[%s] %s", self.task["task_id"], message)
