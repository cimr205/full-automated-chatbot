import asyncio
import json
import logging
import os
import random
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import redis.asyncio as aioredis
from playwright.async_api import async_playwright, BrowserContext, Page

from core.queue.task_queue import TaskQueue
from core.supervisor.supervisor import HumanRequiredError
from core.memory.vault import SecureVault
from workers.browser.ollama_client import OllamaClient

log = logging.getLogger(__name__)

SCREENSHOT_DIR = Path("/tmp/screenshots")
BROWSER_DATA_DIR = Path(os.getenv("BROWSER_DATA_DIR", "/tmp/browser-data"))
HUMAN_REPLY_TIMEOUT = int(os.getenv("HUMAN_REPLY_TIMEOUT", "300"))
PROGRESS_INTERVAL = int(os.getenv("PROGRESS_INTERVAL", "120"))  # seconds
MAX_STEPS = 200  # hard safety cap

APPROVAL_ACTIONS = {"send", "post", "publish", "submit_outreach", "deploy"}


class BrowserWorker:
    def __init__(self, task: dict, queue: TaskQueue, redis: aioredis.Redis, memory=None):
        self.task = task
        self.queue = queue
        self.redis = redis
        self.memory = memory
        self.ollama = OllamaClient()
        self._vault = SecureVault(redis)
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._consecutive_waits = 0
        self._done = False
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    async def execute(self):
        task_id = self.task["task_id"]
        self._done = False
        log.info("[%s] Browser execution starting", task_id)
        checkpoint = self.task.get("_checkpoint")

        async with async_playwright() as pw:
            self._context = await pw.chromium.launch_persistent_context(
                str(BROWSER_DATA_DIR),
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-gpu",
                ],
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
                log.info("[%s] Restoring checkpoint: %s", task_id, checkpoint["url"])
                await self._safe_goto(checkpoint["url"])

            # Start background progress reporter (every PROGRESS_INTERVAL seconds)
            reporter = asyncio.create_task(self._progress_reporter())

            # Send "started" screenshot immediately
            await asyncio.sleep(1)
            shot = await self._screenshot(f"{task_id}_start")
            await self._publish_progress(shot, f"🚀 Agent startet\n{self.task['description'][:120]}", step=0)

            try:
                await self._run_ai_loop()
            finally:
                self._done = True
                reporter.cancel()
                try:
                    await reporter
                except asyncio.CancelledError:
                    pass

            await self._context.close()

    # ── Progress reporting ────────────────────────────────────────────────────

    async def _progress_reporter(self):
        task_id = self.task["task_id"]
        while not self._done:
            await asyncio.sleep(PROGRESS_INTERVAL)
            if self._done:
                break
            try:
                shot = await self._screenshot(
                    f"{task_id}_prog_{int(datetime.utcnow().timestamp())}"
                )
                recent = self.task.get("logs", [])[-6:]
                status = "\n".join(f"• {l}" for l in recent) or "Arbejder…"
                step_n = len([l for l in self.task.get("logs", [])
                               if any(k in l for k in ("→", "Click", "Type", "Extract"))])
                await self._publish_progress(shot, status, step=step_n)
            except Exception as e:
                log.warning("[%s] Progress reporter error: %s", task_id, e)

    async def _publish_progress(self, screenshot_path: str, status: str, step: int = 0):
        await self.redis.publish("browser:progress", json.dumps({
            "task_id": self.task["task_id"],
            "screenshot_path": screenshot_path,
            "url": self._page.url if self._page else "",
            "status": status,
            "step": step,
            "ts": datetime.utcnow().isoformat(),
        }))

    # ── Stop signal ──────────────────────────────────────────────────────────

    async def _should_stop(self) -> bool:
        try:
            return await self.redis.exists(f"task:stop:{self.task['task_id']}") > 0
        except Exception:
            return False

    # ── Main AI loop ──────────────────────────────────────────────────────────

    async def _run_ai_loop(self):
        task_id = self.task["task_id"]
        description = self.task["description"]

        past_experience = []
        if self.memory:
            try:
                past_experience = await self.memory.query_experience(description[:100])
            except Exception:
                pass

        for step in range(1, MAX_STEPS + 1):

            # Check stop signal every step
            if await self._should_stop():
                self._log("⛔ Stop signal — agent afslutter")
                await self.redis.delete(f"task:stop:{task_id}")
                shot = await self._screenshot(f"{task_id}_stopped")
                await self._publish_progress(shot, "⛔ Stoppet af bruger", step=step)
                return

            log.info("[%s] Step %d | %s", task_id, step, self._page.url)

            screenshot_path = await self._screenshot(f"{task_id}_s{step:04d}")
            page_text = await self._get_page_text()
            current_url = self._page.url

            await self._save_checkpoint(current_url, step, page_text)

            prompt = self._build_prompt(description, step, current_url, page_text, past_experience)
            ai_response = await self.ollama.reason(prompt)
            action = self._parse_action(ai_response)

            log.info("[%s] Action: %s", task_id, json.dumps(action)[:120])
            await self._execute_action(action, screenshot_path, step)

            if action.get("type") == "complete":
                self._log("✅ Task færdig")
                shot = await self._screenshot(f"{task_id}_done")
                await self._publish_progress(shot, "✅ Task markeret færdig af agent", step=step)
                return

            self._consecutive_waits = (
                self._consecutive_waits + 1 if action.get("type") == "wait" else 0
            )
            if self._consecutive_waits >= 5:
                raise HumanRequiredError("Agent fast i wait-loop — brug /reply for at guide den")

        self._log(f"⚠️ Nåede maks. {MAX_STEPS} trin")

    # ── Action execution ──────────────────────────────────────────────────────

    async def _execute_action(self, action: dict, screenshot_path: str, step: int):
        t = action.get("type", "wait")

        if t == "complete":
            return

        if t == "login":
            domain = action.get("domain") or self._domain_key()
            email, password = await self._get_saved_credentials(domain)
            if email and password:
                attempted = await self._auto_login(domain, screenshot_path)
                if attempted:
                    return
            # No saved creds — ask user
            await self._send_screenshot_notification(
                screenshot_path,
                f"Login påkrævet for '{domain}'. Svar med:\nemail: din@email.com\nkodeord: DitKodeord"
            )
            reply = await self._wait_for_human_reply()
            parsed = self._parse_credential_reply(reply)
            if parsed["email"] or parsed["password"]:
                await self._save_credentials(domain, parsed["email"], parsed["password"])
                await self._auto_login(domain, screenshot_path)
            else:
                await self._handle_human_reply(reply)
            return

        if t == "need_human":
            reason = action.get("reason", "Input påkrævet")
            domain = self._domain_key()
            # Auto-intercept login/credential requests
            lower_reason = reason.lower()
            if any(w in lower_reason for w in ("login", "log in", "password", "kodeord", "sign in", "credentials", "2fa", "captcha")):
                email, password = await self._get_saved_credentials(domain)
                if email and password and "2fa" not in lower_reason and "captcha" not in lower_reason:
                    attempted = await self._auto_login(domain, screenshot_path)
                    if attempted:
                        return
            await self._send_screenshot_notification(screenshot_path, reason)
            reply = await self._wait_for_human_reply()
            # If human replies with credentials, save them
            parsed = self._parse_credential_reply(reply)
            if parsed["email"] or parsed["password"]:
                await self._save_credentials(domain, parsed["email"], parsed["password"])
                await self._auto_login(domain, screenshot_path)
            else:
                await self._handle_human_reply(reply)
            return

        if t == "approve_required":
            desc = action.get("description", "Handling kræver godkendelse")
            await self._request_approval(screenshot_path, desc)
            reply = await self._wait_for_human_reply()
            if reply.lower().strip() not in ("yes", "ok", "approve", "y", "ja", "confirmed"):
                self._log(f"Godkendelse afvist: {desc}")
                return
            self._log(f"Godkendt: {desc}")
            return

        if t == "navigate":
            if url := action.get("url", ""):
                await self._safe_goto(url)

        elif t == "click":
            if selector := action.get("selector", ""):
                await self._safe_click(selector)
            elif text := action.get("text", ""):
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

    # ── Prompt ────────────────────────────────────────────────────────────────

    def _build_prompt(self, description: str, step: int, url: str,
                      page_text: str, experience: list) -> str:
        recent_logs = "\n".join(self.task.get("logs", [])[-8:])
        exp_text = ""
        if experience:
            exp_text = "PAST EXPERIENCE:\n" + "\n".join(
                f"- {e.get('text','')[:120]}" for e in experience[:3]
            )

        return f"""You are an autonomous browser agent. Execute the task step by step.

TASK: {description[:600]}
STEP: {step}
URL: {url}
PAGE (truncated): {page_text[:2000]}

RECENT ACTIONS:
{recent_logs}
{exp_text}

Reply with ONLY one JSON action:

{{"type": "navigate", "url": "https://..."}}
{{"type": "click", "selector": "CSS_SELECTOR"}}
{{"type": "click", "text": "VISIBLE_BUTTON_TEXT"}}
{{"type": "type", "selector": "CSS_SELECTOR", "value": "TEXT_TO_TYPE"}}
{{"type": "scroll", "direction": "down", "amount": 500}}
{{"type": "extract", "fields": ["field1", "field2"]}}
{{"type": "wait", "seconds": 2}}
{{"type": "login", "domain": "SITE_NAME"}}
{{"type": "need_human", "reason": "EXPLAIN_EXACTLY_WHAT_IS_NEEDED"}}
{{"type": "approve_required", "description": "DESCRIBE_IRREVERSIBLE_ACTION"}}
{{"type": "complete"}}

Rules:
- login: use when you see a login form — the system will auto-fill saved credentials or ask the user once and remember them forever
- need_human: captchas, 2FA codes, or anything that is NOT a standard email+password login
- approve_required: before sending emails, posting, paying
- complete: only when the task outcome is fully achieved
- Never guess or fabricate results
- Never repeat the same failed action
- Be systematic — one action per step

JSON:"""

    def _parse_action(self, response: str) -> dict:
        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(response[start:end])
        except Exception as e:
            log.warning("Parse failed: %s | %.80s", e, response)
        return {"type": "wait", "seconds": 3}

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _human_delay(self):
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
            self._log(f"Click: {text}")
        except Exception as e:
            log.warning("Click text '%s' failed: %s", text, e)

    async def _safe_type(self, selector: str, value: str):
        try:
            await self._page.fill(selector, value, timeout=5000)
            await asyncio.sleep(random.uniform(0.3, 0.8))
            self._log(f"Type → [{selector}]")
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
        await self.queue.save_checkpoint(self.task["task_id"], {
            "url": url, "step": step,
            "ts": datetime.utcnow().isoformat(),
            "page_snippet": page_text[:500],
        })

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
            "reason": f"GODKENDELSE PÅKRÆVET:\n{description}\n\nSvar: ja / nej",
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
                    raise HumanRequiredError("Timeout — ingen svar fra bruger")
                if message["type"] == "message":
                    data = json.loads(message["data"])
                    return data.get("message", "")
        finally:
            await pubsub.unsubscribe(f"human_reply:{task_id}")
            await pubsub.aclose()
        return ""

    def _parse_credential_reply(self, reply: str) -> dict:
        """
        Parse replies of the form:
          email: foo@bar.com
          kodeord: secret
        or plain "foo@bar.com Cimraansej1@" style.
        Returns {"email": ..., "password": ...} with empty strings when not found.
        """
        import re as _re
        email, password = "", ""
        lines = reply.strip().splitlines()
        for line in lines:
            low = line.lower()
            if _re.match(r'e?mail\s*:\s*', low):
                email = _re.sub(r'^e?mail\s*:\s*', '', line, flags=_re.IGNORECASE).strip()
            elif _re.match(r'(password|kodeord|adgangskode|pass)\s*:\s*', low):
                password = _re.sub(r'^(password|kodeord|adgangskode|pass)\s*:\s*', '', line, flags=_re.IGNORECASE).strip()
        # Fallback: single line with email + space + password
        if not email and not password and len(lines) == 1:
            parts = lines[0].split()
            for p in parts:
                if "@" in p and not email:
                    email = p
                elif not password and p != email:
                    password = p
        return {"email": email, "password": password}

    async def _handle_human_reply(self, reply: str):
        self._log(f"Human: {reply}")
        lower = reply.lower().strip()
        if lower.startswith("click "):
            await self._click_by_text(reply[6:].strip())
        elif lower.startswith("type ") and " into " in lower:
            parts = reply[5:].split(" into ", 1)
            await self._safe_type(parts[1].strip(), parts[0].strip())
        elif lower.startswith(("goto ", "navigate ")):
            await self._safe_goto(reply.split(" ", 1)[1].strip())
        elif lower.startswith("http"):
            await self._safe_goto(reply.strip())
        else:
            # Generic: type the reply into any focused input
            try:
                await self._page.keyboard.type(reply)
            except Exception:
                pass

    def _log(self, message: str):
        entry = f"[{datetime.utcnow().strftime('%H:%M:%S')}] {message}"
        logs = self.task.setdefault("logs", [])
        logs.append(entry)
        if len(logs) > 200:
            self.task["logs"] = logs[-200:]
        log.info("[%s] %s", self.task["task_id"], message)

    # ── Credential helpers ────────────────────────────────────────────────────

    def _domain_key(self, url: str = "") -> str:
        """Return a short domain slug, e.g. 'lovable' from 'https://lovable.dev/...'"""
        try:
            host = urlparse(url or (self._page.url if self._page else "")).hostname or ""
            parts = host.split(".")
            # Drop 'www' prefix, take the main name portion
            parts = [p for p in parts if p != "www"]
            return parts[-2] if len(parts) >= 2 else (parts[0] if parts else "site")
        except Exception:
            return "site"

    async def _get_saved_credentials(self, domain: str) -> tuple[str, str]:
        """Return (email, password) for domain from vault, or ("", "")."""
        try:
            email = await self._vault.get(f"{domain}_email") or ""
            password = await self._vault.get(f"{domain}_password") or ""
            return email, password
        except Exception:
            return "", ""

    async def _save_credentials(self, domain: str, email: str, password: str):
        try:
            if email:
                await self._vault.save(f"{domain}_email", email)
            if password:
                await self._vault.save(f"{domain}_password", password)
            self._log(f"🔐 Credentials gemt for '{domain}'")
        except Exception as e:
            log.warning("Could not save credentials: %s", e)

    async def _auto_login(self, domain: str, screenshot_path: str) -> bool:
        """Try to fill login form with saved credentials. Returns True if attempted."""
        email, password = await self._get_saved_credentials(domain)
        if not email or not password:
            return False

        self._log(f"🔑 Bruger gemte credentials for '{domain}'")

        # Common email/username selectors
        email_selectors = [
            'input[type="email"]',
            'input[name="email"]',
            'input[name="username"]',
            'input[id="email"]',
            'input[placeholder*="email" i]',
            'input[placeholder*="mail" i]',
        ]
        password_selectors = [
            'input[type="password"]',
            'input[name="password"]',
            'input[id="password"]',
        ]

        filled_email = False
        for sel in email_selectors:
            try:
                if await self._page.locator(sel).count() > 0:
                    await self._page.fill(sel, email, timeout=3000)
                    filled_email = True
                    break
            except Exception:
                continue

        filled_pass = False
        for sel in password_selectors:
            try:
                if await self._page.locator(sel).count() > 0:
                    await self._page.fill(sel, password, timeout=3000)
                    filled_pass = True
                    break
            except Exception:
                continue

        if filled_email or filled_pass:
            await asyncio.sleep(0.5)
            # Try to click submit
            submit_selectors = [
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("Log in")',
                'button:has-text("Sign in")',
                'button:has-text("Login")',
                'button:has-text("Continue")',
            ]
            for sel in submit_selectors:
                try:
                    if await self._page.locator(sel).count() > 0:
                        await self._page.locator(sel).first.click(timeout=3000)
                        break
                except Exception:
                    continue
            self._log(f"✅ Auto-login forsøgt for '{domain}'")
            return True
        return False
