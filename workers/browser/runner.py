"""
Standalone browser worker process.
Runs on VPS alongside Ollama. Polls Redis for tasks and executes them.
"""
import asyncio
import json
import logging
import os

import redis.asyncio as aioredis

from core.queue.task_queue import TaskQueue
from core.memory.db import Database
from core.memory.chromadb_client import ChromaMemory
from core.memory.memory_manager import MemoryManager
from core.supervisor.supervisor import Supervisor, HumanRequiredError
from core.goals.goal_engine import GoalEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


async def main():
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    redis_client = aioredis.from_url(redis_url, decode_responses=True)

    db = Database()
    await db.connect()

    chroma = ChromaMemory()
    memory = MemoryManager(db, chroma)
    goal_engine = GoalEngine(redis_client)
    task_queue = TaskQueue(redis_client)

    supervisor = Supervisor(task_queue, db, redis_client, goal_engine, memory)
    log.info("Browser worker runner started — waiting for tasks")
    await supervisor.run()


if __name__ == "__main__":
    asyncio.run(main())
