import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Optional

import redis.asyncio as aioredis

from core.queue.task_queue import TaskQueue
from core.memory.db import Database

log = logging.getLogger(__name__)

MAX_RETRIES = 3
POLL_INTERVAL = 2


class Supervisor:
    """
    Deterministic task orchestrator. No AI here — just reliable state management.
    Starts workers, monitors execution, handles retries and crash recovery.
    """

    def __init__(self, queue: TaskQueue, db: Database, redis: aioredis.Redis):
        self.queue = queue
        self.db = db
        self.redis = redis
        self._current_task: Optional[dict] = None
        self._worker_task: Optional[asyncio.Task] = None

    async def run(self):
        log.info("Supervisor started")
        while True:
            try:
                await self._tick()
            except Exception as e:
                log.error("Supervisor tick error: %s", e)
            await asyncio.sleep(POLL_INTERVAL)

    async def _tick(self):
        if self._worker_task and not self._worker_task.done():
            return

        if self._worker_task and self._worker_task.done():
            exc = self._worker_task.exception()
            if exc:
                await self._handle_worker_crash(exc)
                return

        task = await self.queue.dequeue()
        if not task:
            return

        log.info("Starting task %s", task["task_id"])
        self._current_task = task
        self._worker_task = asyncio.create_task(self._run_task(task))

    async def _run_task(self, task: dict):
        from workers.browser.browser_worker import BrowserWorker

        task["status"] = "running"
        task["started_at"] = datetime.utcnow().isoformat()
        await self._update(task)

        worker = BrowserWorker(task, self.queue, self.redis)
        try:
            await worker.execute()
            task["status"] = "completed"
            task["completed_at"] = datetime.utcnow().isoformat()
            await self._update(task)
            await self.queue.complete_task(task["task_id"])
            await self._notify(task["task_id"], f"Task completed: {task['description'][:60]}")
        except HumanRequiredError as e:
            task["status"] = "waiting_for_human"
            task["blocker"] = str(e)
            await self._update(task)
            await self._notify(task["task_id"], f"Human required: {e}\nTask: {task['description'][:60]}")
        except Exception as e:
            log.error("Task %s failed: %s", task["task_id"], e)
            task["retry_count"] = task.get("retry_count", 0) + 1
            if task["retry_count"] <= MAX_RETRIES:
                task["status"] = "retrying"
                await self._update(task)
                await self.queue.enqueue(task)
                log.info("Re-queued %s (attempt %d)", task["task_id"], task["retry_count"])
            else:
                task["status"] = "failed"
                task["error"] = str(e)
                await self._update(task)
                await self._notify(task["task_id"], f"Task FAILED after {MAX_RETRIES} retries: {e}")

    async def _handle_worker_crash(self, exc: Exception):
        if not self._current_task:
            return
        task = self._current_task
        log.error("Worker crashed for task %s: %s", task["task_id"], exc)
        checkpoint = await self.queue.load_checkpoint(task["task_id"])
        if checkpoint:
            log.info("Restoring from checkpoint for task %s", task["task_id"])
            task["_checkpoint"] = checkpoint
        task["retry_count"] = task.get("retry_count", 0) + 1
        if task["retry_count"] <= MAX_RETRIES:
            task["status"] = "retrying"
            await self._update(task)
            await self.queue.enqueue(task)
        else:
            task["status"] = "failed"
            task["error"] = f"Worker crashed: {exc}"
            await self._update(task)

    async def _update(self, task: dict):
        await self.queue.update_task_state(task)
        await self.db.save_task(task)

    async def _notify(self, task_id: str, message: str):
        await self.redis.publish("supervisor:notifications", json.dumps({
            "task_id": task_id,
            "message": message,
            "ts": datetime.utcnow().isoformat(),
        }))


class HumanRequiredError(Exception):
    pass
