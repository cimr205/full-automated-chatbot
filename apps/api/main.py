import os
import uuid
import json
import asyncio
import logging
from datetime import datetime
from typing import Optional

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core.queue.task_queue import TaskQueue
from core.supervisor.supervisor import Supervisor
from core.memory.db import Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Autonomous Execution System")

redis_client: aioredis.Redis = None
task_queue: TaskQueue = None
supervisor: Supervisor = None
db: Database = None


@app.on_event("startup")
async def startup():
    global redis_client, task_queue, supervisor, db
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    redis_client = aioredis.from_url(redis_url, decode_responses=True)
    task_queue = TaskQueue(redis_client)
    db = Database()
    await db.connect()
    supervisor = Supervisor(task_queue, db, redis_client)
    asyncio.create_task(supervisor.run())
    log.info("API started")


@app.on_event("shutdown")
async def shutdown():
    await redis_client.close()
    await db.disconnect()


class TaskRequest(BaseModel):
    description: str
    priority: int = 5


class HumanReply(BaseModel):
    task_id: str
    message: str


@app.post("/tasks")
async def create_task(req: TaskRequest):
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    task = {
        "task_id": task_id,
        "description": req.description,
        "priority": req.priority,
        "status": "queued",
        "created_at": datetime.utcnow().isoformat(),
        "retry_count": 0,
        "logs": [],
        "checkpoints": [],
    }
    await task_queue.enqueue(task)
    await db.save_task(task)
    return {"task_id": task_id, "status": "queued"}


@app.get("/tasks/{task_id}")
async def get_task(task_id: str):
    task = await task_queue.get_task_state(task_id)
    if not task:
        task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@app.get("/tasks")
async def list_tasks():
    return await db.list_tasks(limit=50)


@app.post("/tasks/human-reply")
async def human_reply(reply: HumanReply):
    await redis_client.publish(
        f"human_reply:{reply.task_id}",
        json.dumps({"task_id": reply.task_id, "message": reply.message}),
    )
    return {"status": "delivered"}


@app.get("/screenshots/{filename}")
async def get_screenshot(filename: str):
    path = f"/tmp/screenshots/{filename}"
    if not os.path.exists(path):
        raise HTTPException(404, "Screenshot not found")
    return FileResponse(path, media_type="image/png")


@app.get("/health")
async def health():
    return {"status": "ok", "ts": datetime.utcnow().isoformat()}
