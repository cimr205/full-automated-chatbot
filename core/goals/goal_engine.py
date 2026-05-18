"""
Goal Engine — manages long-term persistent objectives.
Goals survive restarts and drive the supervisor's execution loop.
"""
import json
import logging
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

import redis.asyncio as aioredis

log = logging.getLogger(__name__)

GOALS_KEY = "goals:all"
GOAL_PREFIX = "goal:"


class GoalStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class GoalPriority(int, Enum):
    CRITICAL = 1
    HIGH = 2
    NORMAL = 3
    LOW = 4
    MAINTENANCE = 5


PRIORITY_LABELS = {
    "critical": GoalPriority.CRITICAL,
    "high": GoalPriority.HIGH,
    "normal": GoalPriority.NORMAL,
    "low": GoalPriority.LOW,
    "maintenance": GoalPriority.MAINTENANCE,
}


class GoalEngine:
    def __init__(self, redis: aioredis.Redis):
        self.redis = redis

    async def create_goal(
        self,
        description: str,
        priority: str = "normal",
        recurring: bool = False,
        schedule: Optional[str] = None,
        tags: list = None,
    ) -> dict:
        goal_id = f"goal_{uuid.uuid4().hex[:8]}"
        goal = {
            "goal_id": goal_id,
            "description": description,
            "status": GoalStatus.ACTIVE,
            "priority": PRIORITY_LABELS.get(priority, GoalPriority.NORMAL),
            "recurring": recurring,
            "schedule": schedule,
            "tags": tags or [],
            "created_at": datetime.utcnow().isoformat(),
            "last_run": None,
            "run_count": 0,
            "task_ids": [],
            "notes": [],
        }
        await self.redis.set(f"{GOAL_PREFIX}{goal_id}", json.dumps(goal))
        await self.redis.zadd(GOALS_KEY, {goal_id: goal["priority"]})
        log.info("Goal created: %s — %s", goal_id, description[:60])
        return goal

    async def get_goal(self, goal_id: str) -> Optional[dict]:
        raw = await self.redis.get(f"{GOAL_PREFIX}{goal_id}")
        if raw:
            return json.loads(raw)
        return None

    async def update_goal(self, goal: dict):
        await self.redis.set(f"{GOAL_PREFIX}{goal['goal_id']}", json.dumps(goal))

    async def list_goals(self, status: Optional[str] = None) -> list:
        goal_ids = await self.redis.zrange(GOALS_KEY, 0, -1)
        goals = []
        for gid in goal_ids:
            goal = await self.get_goal(gid)
            if goal:
                if status is None or goal["status"] == status:
                    goals.append(goal)
        return goals

    async def next_executable_goal(self) -> Optional[dict]:
        """Return highest-priority active goal ready to run."""
        goals = await self.list_goals(status=GoalStatus.ACTIVE)
        if not goals:
            return None
        goals.sort(key=lambda g: g["priority"])
        return goals[0]

    async def mark_goal_ran(self, goal_id: str, task_id: str):
        goal = await self.get_goal(goal_id)
        if not goal:
            return
        goal["last_run"] = datetime.utcnow().isoformat()
        goal["run_count"] += 1
        goal["task_ids"].append(task_id)
        if not goal["recurring"]:
            goal["status"] = GoalStatus.COMPLETED
        await self.update_goal(goal)

    async def pause_goal(self, goal_id: str):
        goal = await self.get_goal(goal_id)
        if goal:
            goal["status"] = GoalStatus.PAUSED
            await self.update_goal(goal)

    async def resume_goal(self, goal_id: str):
        goal = await self.get_goal(goal_id)
        if goal:
            goal["status"] = GoalStatus.ACTIVE
            await self.update_goal(goal)

    async def cancel_goal(self, goal_id: str):
        goal = await self.get_goal(goal_id)
        if goal:
            goal["status"] = GoalStatus.CANCELLED
            await self.update_goal(goal)
            await self.redis.zrem(GOALS_KEY, goal_id)

    async def add_note(self, goal_id: str, note: str):
        goal = await self.get_goal(goal_id)
        if goal:
            goal["notes"].append({"ts": datetime.utcnow().isoformat(), "note": note})
            goal["notes"] = goal["notes"][-20:]
            await self.update_goal(goal)
