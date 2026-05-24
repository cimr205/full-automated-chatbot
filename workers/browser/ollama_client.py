"""
AI Client — supports Groq (default) and Ollama (optional VPS).

Priority:
1. Groq API if GROQ_API_KEY is set (recommended for Railway)
2. Ollama if OLLAMA_URL is reachable (for VPS setup)
"""
import logging
import os

import httpx

log = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama3-8b-8192")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

OLLAMA_URL = os.getenv("OLLAMA_URL", "")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3:8b")


class OllamaClient:
    """Unified AI client — uses Groq or Ollama depending on env config."""

    async def reason(self, prompt: str, model: str = None) -> str:
        if GROQ_API_KEY:
            return await self._groq(prompt, model or GROQ_MODEL)
        if OLLAMA_URL:
            return await self._ollama(prompt, model or OLLAMA_MODEL)
        log.error("No AI provider configured — set GROQ_API_KEY or OLLAMA_URL")
        return '{"type": "wait", "seconds": 3}'

    async def vision(self, prompt: str, image_path: str) -> str:
        # Vision falls back to text reasoning with page content description
        return await self.reason(prompt)

    async def is_available(self) -> bool:
        if GROQ_API_KEY:
            return True
        if OLLAMA_URL:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.get(f"{OLLAMA_URL}/api/tags")
                    return resp.status_code == 200
            except Exception:
                return False
        return False

    async def _groq(self, prompt: str, model: str) -> str:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    GROQ_URL,
                    headers={
                        "Authorization": f"Bearer {GROQ_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1,
                        "max_tokens": 300,
                    },
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            log.error("Groq error: %s", e)
            return '{"type": "wait", "seconds": 3}'

    async def _ollama(self, prompt: str, model: str) -> str:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{OLLAMA_URL}/api/generate",
                    json={
                        "model": model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {"temperature": 0.1, "num_predict": 300},
                    },
                )
                resp.raise_for_status()
                return resp.json().get("response", "")
        except Exception as e:
            log.error("Ollama error: %s", e)
            return '{"type": "wait", "seconds": 3}'
