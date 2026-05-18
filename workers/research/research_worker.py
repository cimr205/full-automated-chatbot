"""
Research Worker — extracts structured lead/company data from web pages.
Stores results in CRM memory layer.
"""
import json
import logging
from datetime import datetime
from typing import Optional

from workers.browser.ollama_client import OllamaClient

log = logging.getLogger(__name__)

EXTRACT_PROMPT = """Extract structured information from this web page content.

TARGET: {target}
PAGE URL: {url}
CONTENT: {content}

Respond with ONLY valid JSON. Extract as many fields as available:
{{
  "name": "",
  "company": "",
  "role": "",
  "email": "",
  "linkedin": "",
  "website": "",
  "location": "",
  "industry": "",
  "company_size": "",
  "recent_activity": "",
  "notes": "",
  "lead_score": 1-10
}}

JSON:"""


class ResearchWorker:
    def __init__(self, memory=None):
        self.memory = memory
        self.ollama = OllamaClient()

    async def extract_lead(self, url: str, page_content: str, target: str = "founder") -> Optional[dict]:
        prompt = EXTRACT_PROMPT.format(
            target=target,
            url=url,
            content=page_content[:3000],
        )
        response = await self.ollama.reason(prompt)
        lead = self._parse_lead(response)
        if lead and self.memory:
            lead["source_url"] = url
            lead["extracted_at"] = datetime.utcnow().isoformat()
            await self.memory.save_lead(lead)
            log.info("Lead saved: %s @ %s", lead.get("name"), lead.get("company"))
        return lead

    def _parse_lead(self, response: str) -> Optional[dict]:
        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(response[start:end])
        except Exception as e:
            log.warning("Lead parse failed: %s", e)
        return None

    async def score_lead(self, lead: dict, criteria: str) -> int:
        """Ask Ollama to score a lead against criteria. Returns 1-10."""
        prompt = (
            f"Score this lead from 1-10 against the criteria.\n"
            f"Lead: {json.dumps(lead)[:400]}\n"
            f"Criteria: {criteria}\n"
            f"Respond with ONLY a number 1-10:"
        )
        response = await self.ollama.reason(prompt)
        try:
            score = int(response.strip().split()[0])
            return max(1, min(10, score))
        except Exception:
            return 5
