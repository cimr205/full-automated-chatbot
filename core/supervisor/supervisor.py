"""
Supervisor — deterministic orchestrator.

Phase 2: drives a goal execution loop in addition to one-off tasks.
- Pulls from goal engine when task queue is empty
- Decomposes goals into tasks via planner
- Handles retries, crashes, memory recording
- Notifies Telegram via Redis pub/sub
"""
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
POLL_INTERVAL = 3
GOAL_IDLE_SLEEP = 30


class HumanRequiredError(Exception):
    pass


class Supervisor:
    def __init__(self, queue: TaskQueue, db: Database, redis: aioredis.Redis,
                 goal_engine=None, memory=None):
        self.queue = queue
        self.db = db
        self.redis = redis
        self.goal_engine = goal_engine
        self.memory = memory
        self._current_task: Optional[dict] = None
        self._worker_task: Optional[asyncio.Task] = None

    async def run(self):
        log.info("Supervisor v2 started")
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
                self._worker_task = None
                return
            self._worker_task = None

        task = await self.queue.dequeue()

        if not task and self.goal_engine:
            task = await self._task_from_goal()

        if not task:
            return

        log.info("Executing task %s", task["task_id"])
        self._current_task = task
        self._worker_task = asyncio.create_task(self._run_task(task))

    async def _task_from_goal(self) -> Optional[dict]:
        goal = await self.goal_engine.next_executable_goal()
        if not goal:
            return None

        from workers.browser.ollama_client import OllamaClient
        from core.planning.planner import Planner

        ollama = OllamaClient()
        planner = Planner(ollama)

        context = ""
        if self.memory:
            biz = await self.memory.get_business_context()
            context = json.dumps(biz)[:500]

        plan = await planner.plan(goal["description"], context)
        task_desc = planner.decompose_to_task_description(plan, goal["description"])

        import uuid
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        task = {
            "task_id": task_id,
            "goal_id": goal["goal_id"],
            "description": task_desc,
            "plan": plan,
            "priority": goal["priority"],
            "status": "queued",
            "created_at": datetime.utcnow().isoformat(),
            "retry_count": 0,
            "logs": [],
            "checkpoints": [],
        }
        await self.queue.enqueue(task)
        await self.goal_engine.mark_goal_ran(goal["goal_id"], task_id)
        await self.db.save_task(task)
        log.info("Goal %s → task %s", goal["goal_id"], task_id)
        return task

    async def _run_task(self, task: dict):
        from workers.browser.browser_worker import BrowserWorker

        task["status"] = "running"
        task["started_at"] = datetime.utcnow().isoformat()
        await self._update(task)

        worker = BrowserWorker(task, self.queue, self.redis, self.memory)
        try:
            await worker.execute()
            task["status"] = "completed"
            task["completed_at"] = datetime.utcnow().isoformat()
            await self._update(task)
            await self.queue.complete_task(task["task_id"])

            if self.memory:
                logs = task.get("logs", [])
                await self.memory.record_success(
                    workflow=task.get("description", "")[:80],
                    steps=[l for l in logs[-10:]],
                    outcome="completed",
                )

            await self._notify(task["task_id"], f"Completed: {task['description'][:60]}")

        except HumanRequiredError as e:
            task["status"] = "waiting_for_human"
            task["blocker"] = str(e)
            await self._update(task)
            await self._notify(
                task["task_id"],
                f"Human required:\n{e}\n\nTask: {task['description'][:60]}\n\nReply: /reply {task['task_id']} <instruction>",
            )

        except Exception as e:
            log.error("Task %s failed: %s", task["task_id"], e)
            task["retry_count"] = task.get("retry_count", 0) + 1

            if self.memory:
                await self.memory.record_failure(
                    workflow=task.get("description", "")[:80],
                    error=str(e),
                )

            if task["retry_count"] <= MAX_RETRIES:
                task["status"] = "retrying"
                await self._update(task)
                await self.queue.enqueue(task)
                log.info("Re-queued %s (attempt %d)", task["task_id"], task["retry_count"])
            else:
                task["status"] = "failed"
                task["error"] = str(e)
                await self._update(task)
                await self._notify(
                    task["task_id"],
                    f"FAILED after {MAX_RETRIES} retries:\n{e}\n\nTask: {task['description'][:60]}",
                )

    async def _handle_worker_crash(self, exc: Exception):
        if not self._current_task:
            return
        task = self._current_task
        log.error("Worker crashed for %s: %s", task["task_id"], exc)
        checkpoint = await self.queue.load_checkpoint(task["task_id"])
        if checkpoint:
            task["_checkpoint"] = checkpoint
            log.info("Checkpoint restored for %s", task["task_id"])
        task["retry_count"] = task.get("retry_count", 0) + 1
        if task["retry_count"] <= MAX_RETRIES:
            task["status"] = "retrying"
            await self._update(task)
            await self.queue.enqueue(task)
        else:
            task["status"] = "failed"
            task["error"] = f"Worker crashed: {exc}"
            await self._update(task)
            await self._notify(task["task_id"], f"Worker crash — task failed: {task['description'][:50]}")

    async def _update(self, task: dict):
        await self.queue.update_task_state(task)
        await self.db.save_task(task)
        try:
            await self.redis.publish("ws:events", json.dumps({
                "type": "task_update",
                "task_id": task["task_id"],
                "status": task["status"],
            }))
        except Exception:
            pass

    async def _notify(self, task_id: str, message: str):
        await self.redis.publish("supervisor:notifications", json.dumps({
            "task_id": task_id,
            "message": message,
            "ts": datetime.utcnow().isoformat(),
        }))
