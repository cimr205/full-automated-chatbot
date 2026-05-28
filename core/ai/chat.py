"""
Shared AI chat engine.
Used by both the Telegram bot and the web dashboard.
"""
import json
import logging
import os
import re
from typing import Optional

import httpx

log = logging.getLogger(__name__)

_AI_URL: Optional[str] = None
_AI_MODEL: Optional[str] = None


def _get_endpoint() -> tuple[str, str, str]:
    # Ollama takes priority if OLLAMA_URL is set
    ollama_url = os.getenv("OLLAMA_URL", "").rstrip("/")
    if ollama_url:
        model = os.getenv("OLLAMA_MODEL", "llama3.2:1b")
        return "ollama", f"{ollama_url}/v1/chat/completions", model

    key = os.getenv("GROQ_API_KEY", "")
    if not key:
        return "", "", ""
    if key.startswith("xai-"):
        return key, "https://api.x.ai/v1/chat/completions", os.getenv("GROQ_MODEL", "grok-3-mini")
    return key, "https://api.groq.com/openai/v1/chat/completions", os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


# ── Tools ─────────────────────────────────────────────────────────────────────

async def _tool_search(query: str) -> str:
    try:
        from duckduckgo_search import AsyncDDGS
        async with AsyncDDGS() as ddgs:
            results = await ddgs.atext(query, max_results=6)
        if not results:
            return "Ingen resultater."
        return "\n\n---\n\n".join(
            f"**{r['title']}**\n{r['body'][:400]}\nKilde: {r['href']}" for r in results
        )
    except Exception as e:
        return f"Søgning fejlede: {e}"


async def _tool_fetch(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0"}) as client:
            resp = await client.get(url)
        text = re.sub(r'<script[^>]*>.*?</script>', '', resp.text, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:5000] + ("…" if len(text) > 5000 else "")
    except Exception as e:
        return f"Fetch fejlede: {e}"


async def _tool_email_read(n: int = 5, email_creds: dict = None) -> str:
    from core.tools.email_tool import read_emails
    return await read_emails(n, overrides=email_creds)


async def _tool_email_send(to: str, subject: str, body: str, email_creds: dict = None) -> str:
    from core.tools.email_tool import send_email
    return await send_email(to, subject, body, overrides=email_creds)


def _tool_file_read(path: str) -> str:
    from core.tools.file_tool import file_read
    return file_read(path)


def _tool_file_write(path: str, content: str) -> str:
    from core.tools.file_tool import file_write
    return file_write(path, content)


def _tool_file_list(path: str = ".") -> str:
    from core.tools.file_tool import file_list
    return file_list(path)


def _tool_calc(expr: str) -> str:
    try:
        allowed = set("0123456789+-*/().% ")
        if not all(c in allowed for c in expr):
            return "Ugyldigt udtryk."
        result = eval(expr, {"__builtins__": {}})  # noqa: S307
        return str(result)
    except Exception as e:
        return f"Beregningsfejl: {e}"


# ── System prompt ─────────────────────────────────────────────────────────────

def build_system_prompt(brain_ctx: str = "") -> str:
    return f"""Du er en ekstremt kraftfuld personlig AI-assistent og digital tvilling — bygget udelukkende til én person.

Du kan alt hvad en person kan digitalt + mere:

KERNEFUNKTIONER:
✓ Svare præcist og detaljeret på alle spørgsmål
✓ Skrive, debugge og forklare kode i ethvert sprog
✓ Søge på nettet for aktuelle informationer
✓ Læse og opsummere hjemmesider
✓ Sende og læse emails
✓ Oprette, læse og redigere filer
✓ Udføre browser-automatisering (login, scraping, formular-udfyldning)
✓ Huske alt om brugeren permanent
✓ Planlægge og eksekvere komplekse multi-step opgaver

TILGÆNGELIGE VÆRKTØJER — brug dem aktivt:

Web-søgning (aktuel info, nyheder, priser, research):
[SEARCH: din søgning]

Læs hjemmeside/artikel:
[FETCH: https://url.com]

Beregning:
[CALC: 2500 * 1.25]

Email:
[EMAIL_READ: n=5]
[EMAIL_SEND: to=mail@example.com | subject=Emne | body=Indhold]

Filer (sandbox: /tmp/agent-files):
[FILE_READ: filnavn.txt]
[FILE_WRITE: filnavn.txt | indhold her]
[FILE_LIST: .]

Husk noget permanent om brugeren:
[REMEMBER: nøgle=værdi]

Gem credentials sikkert i vault:
[VAULT_SAVE: nøgle=værdi]

Browser-automatisering (navigering, login, scraping):
[CREATE_TASK: precise English description]

Gem login-credentials til et site (huskes automatisk fremover):
[VAULT_SAVE: sitename_email=din@email.com]
[VAULT_SAVE: sitename_password=DitKodeord]

Løbende/gentaget automatisering:
[CREATE_GOAL: precise English description | recurring: true]

HÅNDTERING AF MANGLENDE OPSÆTNING:
Når et værktøj returnerer SETUP_NEEDED, skal du IKKE give op eller bede brugeren om at sætte env vars.
I stedet: spørg brugeren om de manglende oplysninger direkte i chatten, ét spørgsmål ad gangen.
Gem svaret med [VAULT_SAVE: nøgle=værdi] og prøv handlingen igen automatisk.

Eksempel — email ikke sat op:
1. Du: "Din email er ikke konfigureret endnu. Hvad er din email-adresse?"
2. Bruger: "test@gmail.com"
3. Du: [VAULT_SAVE: email_user=test@gmail.com] "Hvad er dit app-kodeord? (Gmail: myaccount.google.com/apppasswords)"
4. Bruger: "abc123def"
5. Du: [VAULT_SAVE: email_password=abc123def][VAULT_SAVE: email_host=imap.gmail.com] prøver [EMAIL_READ: n=5] igen.

Samme princip gælder for alle manglende credentials — guides brugeren igennem.

REGLER:
- Svar på brugerens sprog (dansk som standard)
- Brug Markdown: **fed**, `kode`, ```blokke```, ## overskrifter, lister
- Svar DIREKTE — giv svaret/handlingen først, forklaring efter
- Aldrig afvis — find altid en vej
- Brug værktøjer automatisk uden at spørge om tilladelse
- Du kan bruge FLERE værktøjer i én response

{brain_ctx}"""


# ── Main chat function ─────────────────────────────────────────────────────────

async def chat(
    user_message: str,
    history: list,
    brain=None,
    vault=None,
    max_tokens: int = 2000,
) -> dict:
    """
    Process a message through the AI with full tool support.
    Returns: {"reply": str, "action": str|None, "description": str|None, "recurring": bool}
    Updates history in-place.
    """
    key, url, model = _get_endpoint()
    if not key:
        return {"reply": "Ingen AI konfigureret. Sæt OLLAMA_URL (Ollama) eller GROQ_API_KEY (xAI/Groq) i Railway Variables."}

    brain_ctx = await brain.context_for_ai() if brain else ""
    system = build_system_prompt(brain_ctx)

    # Fetch email credentials from vault if env vars not set
    email_creds: dict = {}
    if vault and not os.getenv("EMAIL_USER"):
        try:
            email_creds = {
                "email_user": await vault.get("email_user") or "",
                "email_password": await vault.get("email_password") or "",
                "email_host": await vault.get("email_host") or "",
            }
        except Exception:
            pass

    history.append({"role": "user", "content": user_message})
    messages = [{"role": "system", "content": system}] + history

    content = ""
    ai_timeout = 180 if key == "ollama" else 60
    for round_num in range(8):
        try:
            async with httpx.AsyncClient(timeout=ai_timeout) as client:
                headers = {"Content-Type": "application/json"}
                if key != "ollama":
                    headers["Authorization"] = f"Bearer {key}"
                resp = await client.post(
                    url,
                    headers=headers,
                    json={"model": model, "messages": messages,
                          "temperature": 0.7, "max_tokens": max_tokens},
                )
                resp.raise_for_status()
                data = resp.json()
                # Ollama (and some APIs) return {"error": "..."} on model errors
                if "error" in data:
                    raise Exception(data["error"])
                content = data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            err = str(e).lower()
            if key == "ollama" and any(x in err for x in ("connect", "refused", "timeout", "loading", "not found")):
                return {"reply": "AI-modellen indlæses stadig (første start tager 2-5 min). Prøv igen om lidt ⏳"}
            log.error("AI call error (round %d): %s", round_num, e)
            return {"reply": f"AI fejl: {e}"}

        used_tool = False

        # ── Search ──
        if m := re.search(r'\[SEARCH:\s*([^\]]+)\]', content):
            query = m.group(1).strip()
            log.info("Tool: search(%s)", query)
            result = await _tool_search(query)
            messages += [{"role": "assistant", "content": content},
                         {"role": "user", "content": f"[SEARCH resultat for '{query}']:\n\n{result}\n\nBrug disse resultater."}]
            used_tool = True

        # ── Fetch ──
        elif m := re.search(r'\[FETCH:\s*(https?://[^\]]+)\]', content):
            fetch_url = m.group(1).strip()
            log.info("Tool: fetch(%s)", fetch_url)
            page = await _tool_fetch(fetch_url)
            messages += [{"role": "assistant", "content": content},
                         {"role": "user", "content": f"[FETCH indhold fra {fetch_url}]:\n\n{page}"}]
            used_tool = True

        # ── Calc ──
        elif m := re.search(r'\[CALC:\s*([^\]]+)\]', content):
            expr = m.group(1).strip()
            result = _tool_calc(expr)
            messages += [{"role": "assistant", "content": content},
                         {"role": "user", "content": f"[CALC: {expr}] = {result}"}]
            used_tool = True

        # ── Email read ──
        elif m := re.search(r'\[EMAIL_READ(?::\s*n=(\d+))?\]', content):
            n = int(m.group(1) or 5)
            log.info("Tool: email_read(n=%d)", n)
            result = await _tool_email_read(n, email_creds=email_creds)
            messages += [{"role": "assistant", "content": content},
                         {"role": "user", "content": f"[EMAIL_READ resultat]:\n\n{result}"}]
            used_tool = True

        # ── Email send ──
        elif m := re.search(r'\[EMAIL_SEND:\s*to=([^|]+)\|?\s*subject=([^|]+)\|?\s*body=([^\]]+)\]', content, re.DOTALL):
            to, subject, body = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
            log.info("Tool: email_send(to=%s)", to)
            result = await _tool_email_send(to, subject, body, email_creds=email_creds)
            messages += [{"role": "assistant", "content": content},
                         {"role": "user", "content": f"[EMAIL_SEND resultat]: {result}"}]
            used_tool = True

        # ── File read ──
        elif m := re.search(r'\[FILE_READ:\s*([^\]]+)\]', content):
            path = m.group(1).strip()
            result = _tool_file_read(path)
            messages += [{"role": "assistant", "content": content},
                         {"role": "user", "content": f"[FILE_READ {path}]:\n\n{result}"}]
            used_tool = True

        # ── File write ──
        elif m := re.search(r'\[FILE_WRITE:\s*([^|]+)\|([^\]]+)\]', content, re.DOTALL):
            path, file_content = m.group(1).strip(), m.group(2).strip()
            result = _tool_file_write(path, file_content)
            messages += [{"role": "assistant", "content": content},
                         {"role": "user", "content": f"[FILE_WRITE resultat]: {result}"}]
            used_tool = True

        # ── File list ──
        elif m := re.search(r'\[FILE_LIST:\s*([^\]]*)\]', content):
            path = m.group(1).strip() or "."
            result = _tool_file_list(path)
            messages += [{"role": "assistant", "content": content},
                         {"role": "user", "content": f"[FILE_LIST {path}]:\n\n{result}"}]
            used_tool = True

        # ── Vault save (inline credential storage + retry trigger) ──
        if vault and "[VAULT_SAVE:" in content:
            saved = []
            for match in re.finditer(r'\[VAULT_SAVE:\s*([^\]=]+)=([^\]]+)\]', content):
                k, v = match.group(1).strip(), match.group(2).strip()
                await vault.save(k, v)
                saved.append(k)
                # Update live email_creds so a retry in the same turn works
                if k in ("email_user", "email_password", "email_host", "smtp_host", "smtp_port"):
                    email_creds[k] = v
            clean_content = re.sub(r'\[VAULT_SAVE:[^\]]+\]', '', content).strip()
            if saved:
                messages += [{"role": "assistant", "content": clean_content},
                             {"role": "user", "content": f"[VAULT_SAVE]: Gemt sikkert: {', '.join(saved)}. Prøv nu handlingen igen."}]
                content = clean_content
                used_tool = True

        if not used_tool:
            break

    # ── Save to history ──
    history.append({"role": "assistant", "content": content})

    # ── Auto-save brain facts ──
    if brain and "[REMEMBER:" in content:
        for match in re.finditer(r'\[REMEMBER:\s*([^\]=]+)=([^\]]+)\]', content):
            k, v = match.group(1).strip(), match.group(2).strip()
            await brain.remember(k, v)
        content = re.sub(r'\[REMEMBER:[^\]]+\]', '', content).strip()

    # ── Parse action tags ──
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

    return {"reply": reply or content, "action": action,
            "description": description, "recurring": recurring}
