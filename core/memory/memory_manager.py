"""
4-Layer Memory System:
  1. Permanent — user preferences, goals, business context (PostgreSQL)
  2. Task      — active task state, checkpoints (Redis)
  3. Experience — learned workflows, failures, anti-patterns (ChromaDB)
  4. Relationship — leads, outreach, CRM records (PostgreSQL)
"""
import json
import logging
from datetime import datetime
from typing import Optional

from core.memory.db import Database
from core.memory.chromadb_client import ChromaMemory

log = logging.getLogger(__name__)


class MemoryManager:
    def __init__(self, db: Database, chroma: ChromaMemory):
        self.db = db
        self.chroma = chroma

    # ── Layer 1: Permanent ────────────────────────────────────────────────

    async def remember(self, key: str, value: dict):
        """Persist a permanent memory entry."""
        await self.db.set_memory(key, value)

    async def recall(self, key: str) -> Optional[dict]:
        return await self.db.get_memory(key)

    async def remember_preference(self, key: str, value):
        prefs = await self.recall("preferences") or {}
        prefs[key] = value
        await self.remember("preferences", prefs)

    async def get_preferences(self) -> dict:
        return await self.recall("preferences") or {}

    async def set_business_context(self, context: dict):
        await self.remember("business_context", context)

    async def get_business_context(self) -> dict:
        return await self.recall("business_context") or {}

    # ── Layer 3: Experience ───────────────────────────────────────────────

    async def record_success(self, workflow: str, steps: list, outcome: str):
        doc = {
            "type": "success",
            "workflow": workflow,
            "steps": steps,
            "outcome": outcome,
            "ts": datetime.utcnow().isoformat(),
        }
        await self.chroma.store(
            collection="experience",
            doc_id=f"success_{datetime.utcnow().timestamp():.0f}",
            text=f"SUCCESS: {workflow}\nOutcome: {outcome}\nSteps: {' → '.join(steps)}",
            metadata=doc,
        )

    async def record_failure(self, workflow: str, error: str, context: str = ""):
        doc = {
            "type": "failure",
            "workflow": workflow,
            "error": error,
            "context": context,
            "ts": datetime.utcnow().isoformat(),
        }
        await self.chroma.store(
            collection="experience",
            doc_id=f"failure_{datetime.utcnow().timestamp():.0f}",
            text=f"FAILURE: {workflow}\nError: {error}\nContext: {context}",
            metadata=doc,
        )

    async def query_experience(self, query: str, n_results: int = 3) -> list:
        return await self.chroma.query(collection="experience", query=query, n_results=n_results)

    # ── Layer 4: Relationship / CRM ───────────────────────────────────────

    async def save_lead(self, lead: dict):
        lead_id = lead.get("id") or f"lead_{lead.get('name','').replace(' ','_').lower()}"
        lead["updated_at"] = datetime.utcnow().isoformat()
        await self.db.set_memory(f"lead:{lead_id}", lead)
        await self.chroma.store(
            collection="relationships",
            doc_id=lead_id,
            text=f"{lead.get('name','')} {lead.get('company','')} {lead.get('role','')} {lead.get('notes','')}",
            metadata=lead,
        )

    async def find_leads(self, query: str, n_results: int = 10) -> list:
        return await self.chroma.query(collection="relationships", query=query, n_results=n_results)

    async def get_lead(self, lead_id: str) -> Optional[dict]:
        return await self.recall(f"lead:{lead_id}")

    async def update_outreach_status(self, lead_id: str, status: str, message: str = ""):
        lead = await self.get_lead(lead_id) or {}
        lead.setdefault("outreach_history", []).append({
            "status": status,
            "message": message,
            "ts": datetime.utcnow().isoformat(),
        })
        lead["outreach_status"] = status
        await self.save_lead(lead)

    async def get_pending_followups(self) -> list:
        return await self.chroma.query(
            collection="relationships",
            query="waiting for reply followup pending",
            n_results=20,
        )
