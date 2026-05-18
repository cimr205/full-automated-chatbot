"""
Planning Engine — decomposes goals into ordered executable subtasks.
Uses Ollama to reason about the goal and produce a structured execution plan.
"""
import json
import logging
from typing import Optional

log = logging.getLogger(__name__)


PLAN_PROMPT = """You are an execution planning AI. Break down a goal into ordered subtasks.

GOAL: {goal}
CONTEXT: {context}

Produce a JSON execution plan. Each step must be a discrete, executable browser or research action.

Rules:
- Maximum 10 steps
- Each step must be independently executable
- Identify which steps require human approval (auth, sending messages, publishing)
- Keep steps concrete, not vague
- For outreach tasks, always include: research → draft → approve → send → monitor

Respond with ONLY valid JSON:
{{
  "estimated_steps": <int>,
  "requires_human_approval": <bool>,
  "approval_steps": [<step indices requiring approval>],
  "steps": [
    {{
      "index": 0,
      "action": "navigate|click|type|research|extract|draft|approve|send|monitor|complete",
      "description": "...",
      "requires_approval": false,
      "dependencies": []
    }}
  ]
}}

JSON:"""


class Planner:
    def __init__(self, ollama_client):
        self.ollama = ollama_client

    async def plan(self, goal_description: str, context: str = "") -> dict:
        prompt = PLAN_PROMPT.format(goal=goal_description, context=context[:800])
        response = await self.ollama.reason(prompt, model=None)

        plan = self._parse_plan(response)
        if not plan:
            plan = self._fallback_plan(goal_description)

        log.info("Plan generated: %d steps, approval_needed=%s",
                 len(plan.get("steps", [])), plan.get("requires_human_approval"))
        return plan

    def _parse_plan(self, response: str) -> Optional[dict]:
        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(response[start:end])
        except Exception as e:
            log.warning("Plan parse failed: %s", e)
        return None

    def _fallback_plan(self, goal: str) -> dict:
        return {
            "estimated_steps": 3,
            "requires_human_approval": False,
            "approval_steps": [],
            "steps": [
                {"index": 0, "action": "research", "description": f"Research: {goal}", "requires_approval": False, "dependencies": []},
                {"index": 1, "action": "complete", "description": "Mark complete", "requires_approval": False, "dependencies": [0]},
            ],
        }

    def decompose_to_task_description(self, plan: dict, goal: str) -> str:
        steps = plan.get("steps", [])
        step_lines = "\n".join(
            f"  {i+1}. [{s['action'].upper()}] {s['description']}"
            + (" [NEEDS APPROVAL]" if s.get("requires_approval") else "")
            for i, s in enumerate(steps)
        )
        return f"{goal}\n\nExecution plan:\n{step_lines}"
