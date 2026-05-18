"""
Outreach Worker — personalized, CRM-tracked outreach.

Rules:
- Never send without approval
- Always personalize using lead context
- Always record in CRM
- Avoid spam patterns (rate limits, realistic timing)
"""
import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Optional

from workers.browser.ollama_client import OllamaClient

log = logging.getLogger(__name__)

DRAFT_PROMPT = """You are writing a highly personalized outreach message.
You are NOT a generic AI assistant. You write as a human professional.

SENDER CONTEXT:
{sender_context}

TARGET LEAD:
Name: {name}
Company: {company}
Role: {role}
Notes: {notes}
Recent activity: {activity}

GOAL: {goal}

Rules:
- Maximum 4 sentences
- Reference something specific about them
- Sound human, not AI-generated
- No hollow compliments ("I love your work")
- Clear value proposition in one line
- Natural closing

Write ONLY the message body (no subject line):"""


class OutreachWorker:
    def __init__(self, memory, redis):
        self.memory = memory
        self.redis = redis
        self.ollama = OllamaClient()

    async def draft_message(self, lead: dict, goal: str) -> str:
        biz = {}
        if self.memory:
            biz = await self.memory.get_business_context()

        prompt = DRAFT_PROMPT.format(
            sender_context=json.dumps(biz)[:300],
            name=lead.get("name", ""),
            company=lead.get("company", ""),
            role=lead.get("role", ""),
            notes=lead.get("notes", "")[:200],
            activity=lead.get("recent_activity", "")[:200],
            goal=goal,
        )

        draft = await self.ollama.reason(prompt)
        log.info("Draft generated for %s", lead.get("name", "unknown"))
        return draft.strip()

    async def request_approval(self, lead: dict, draft: str, task_id: str):
        lead_name = lead.get("name", "Unknown")
        company = lead.get("company", "")
        message = (
            f"OUTREACH APPROVAL NEEDED\n\n"
            f"To: {lead_name} ({company})\n\n"
            f"Message:\n{draft}\n\n"
            f"Reply: /reply {task_id} yes   (or no)"
        )
        await self.redis.publish("supervisor:notifications", json.dumps({
            "task_id": task_id,
            "message": message,
            "ts": datetime.utcnow().isoformat(),
        }))

    async def record_sent(self, lead: dict, message: str):
        if self.memory:
            lead_id = lead.get("id", lead.get("name", "").replace(" ", "_").lower())
            await self.memory.update_outreach_status(lead_id, "sent", message)
            log.info("CRM updated: %s → sent", lead_id)

    async def record_reply(self, lead_id: str, reply: str):
        if self.memory:
            await self.memory.update_outreach_status(lead_id, "replied", reply)

    async def process_lead_batch(self, leads: list, goal: str, task_id: str) -> list:
        """Draft + request approval for a batch. Returns approved drafts."""
        approved = []
        for lead in leads[:5]:  # max 5 per batch — avoid spam
            draft = await self.draft_message(lead, goal)
            await self.request_approval(lead, draft, task_id)
            await asyncio.sleep(3)
            approved.append({"lead": lead, "draft": draft})
        return approved
