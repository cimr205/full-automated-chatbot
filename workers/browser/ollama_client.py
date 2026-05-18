import logging
import os

import httpx

log = logging.getLogger(__name__)

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL = os.getenv("OLLAMA_MODEL", "llama3:8b")


class OllamaClient:
    def __init__(self):
        self.base_url = OLLAMA_URL
        self.model = DEFAULT_MODEL

    async def reason(self, prompt: str, model: str = None) -> str:
        target_model = model or self.model
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{self.base_url}/api/generate",
                    json={
                        "model": target_model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {"temperature": 0.1, "num_predict": 300},
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("response", "")
        except Exception as e:
            log.error("Ollama error: %s", e)
            return '{"type": "wait", "seconds": 3}'

    async def vision(self, prompt: str, image_path: str) -> str:
        import base64
        try:
            with open(image_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode()
            async with httpx.AsyncClient(timeout=90) as client:
                resp = await client.post(
                    f"{self.base_url}/api/generate",
                    json={
                        "model": os.getenv("OLLAMA_VISION_MODEL", "qwen2.5vl"),
                        "prompt": prompt,
                        "images": [img_b64],
                        "stream": False,
                        "options": {"temperature": 0.1, "num_predict": 300},
                    },
                )
                resp.raise_for_status()
                return resp.json().get("response", "")
        except Exception as e:
            log.error("Ollama vision error: %s", e)
            return '{"type": "need_human", "reason": "Vision model unavailable"}'

    async def is_available(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self.base_url}/api/tags")
                return resp.status_code == 200
        except Exception:
            return False
