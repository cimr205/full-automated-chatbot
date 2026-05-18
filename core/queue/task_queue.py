import json
import logging
from typing import Optional

import redis.asyncio as aioredis

log = logging.getLogger(__name__)

QUEUE_KEY = "task:queue"
STATE_PREFIX = "task:state:"
ACTIVE_KEY = "task:active"


class TaskQueue:
    def __init__(self, redis: aioredis.Redis):
        self.redis = redis

    async def enqueue(self, task: dict):
        task_id = task["task_id"]
        await self.redis.set(f"{STATE_PREFIX}{task_id}", json.dumps(task))
        await self.redis.lpush(QUEUE_KEY, task_id)
        log.info("Enqueued %s", task_id)

    async def dequeue(self) -> Optional[dict]:
        task_id = await self.redis.rpop(QUEUE_KEY)
        if not task_id:
            return None
        raw = await self.redis.get(f"{STATE_PREFIX}{task_id}")
        if not raw:
            return None
        task = json.loads(raw)
        await self.redis.set(ACTIVE_KEY, task_id)
        return task

    async def peek_active(self) -> Optional[str]:
        return await self.redis.get(ACTIVE_KEY)

    async def update_task_state(self, task: dict):
        task_id = task["task_id"]
        await self.redis.set(f"{STATE_PREFIX}{task_id}", json.dumps(task))

    async def get_task_state(self, task_id: str) -> Optional[dict]:
        raw = await self.redis.get(f"{STATE_PREFIX}{task_id}")
        if not raw:
            return None
        return json.loads(raw)

    async def complete_task(self, task_id: str):
        current = await self.redis.get(ACTIVE_KEY)
        if current == task_id:
            await self.redis.delete(ACTIVE_KEY)

    async def queue_length(self) -> int:
        return await self.redis.llen(QUEUE_KEY)

    async def save_checkpoint(self, task_id: str, checkpoint: dict):
        key = f"task:checkpoint:{task_id}"
        await self.redis.set(key, json.dumps(checkpoint))

    async def load_checkpoint(self, task_id: str) -> Optional[dict]:
        key = f"task:checkpoint:{task_id}"
        raw = await self.redis.get(key)
        if not raw:
            return None
        return json.loads(raw)
