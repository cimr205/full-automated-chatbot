import os
import uuid
import json
import asyncio
import logging
import pathlib
from datetime import datetime
from typing import Optional

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

DASHBOARD_HTML = pathlib.Path(__file__).parent.parent / "dashboard" / "index.html"


class ConnectionManager:
    def __init__(self):
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self._connections:
            self._connections.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self._connections:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()

from core.queue.task_queue import TaskQueue
from core.supervisor.supervisor import Supervisor
from core.memory.db import Database
from core.memory.chromadb_client import ChromaMemory
from core.memory.memory_manager import MemoryManager
from core.goals.goal_engine import GoalEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Autonomous Execution System v2")

redis_client: aioredis.Redis = None
task_queue: TaskQueue = None
supervisor: Supervisor = None
db: Database = None
memory: MemoryManager = None
goal_engine: GoalEngine = None


@app.on_event("startup")
async def startup():
    global redis_client, task_queue, supervisor, db, memory, goal_engine

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    redis_client = aioredis.from_url(redis_url, decode_responses=True)

    task_queue = TaskQueue(redis_client)
    db = Database()
    await db.connect()

    chroma = ChromaMemory()
    memory = MemoryManager(db, chroma)
    goal_engine = GoalEngine(redis_client)

    supervisor = Supervisor(task_queue, db, redis_client, goal_engine, memory)
    asyncio.create_task(supervisor.run())
    log.info("API v2 started")


@app.on_event("shutdown")
async def shutdown():
    await redis_client.close()
    await db.disconnect()


# ── Task endpoints ────────────────────────────────────────────────────────────

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
    try:
        await redis_client.publish("ws:events", json.dumps({
            "type": "task_update", "task_id": task_id, "status": "queued"
        }))
    except Exception:
        pass
    return {"task_id": task_id, "status": "queued"}


@app.get("/tasks/{task_id}")
async def get_task(task_id: str):
    task = await task_queue.get_task_state(task_id)
    if not task:
        task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@app.post("/tasks/{task_id}/stop")
async def stop_task(task_id: str):
    await redis_client.set(f"task:stop:{task_id}", "1", ex=3600)
    return {"status": "stop_signal_sent", "task_id": task_id}


@app.get("/tasks")
async def list_tasks():
    tasks = await db.list_tasks(limit=50)
    if not tasks:
        # Redis fallback when Postgres is not connected
        try:
            keys = await redis_client.keys("task:state:*")
            for key in keys[:50]:
                raw = await redis_client.get(key)
                if raw:
                    try:
                        tasks.append(json.loads(raw))
                    except Exception:
                        pass
            tasks.sort(key=lambda t: t.get("created_at", ""), reverse=True)
        except Exception:
            pass
    return tasks


@app.post("/tasks/human-reply")
async def human_reply(reply: HumanReply):
    await redis_client.publish(
        f"human_reply:{reply.task_id}",
        json.dumps({"task_id": reply.task_id, "message": reply.message}),
    )
    return {"status": "delivered"}


# ── Goal endpoints ────────────────────────────────────────────────────────────

class GoalRequest(BaseModel):
    description: str
    priority: str = "normal"
    recurring: bool = False
    schedule: Optional[str] = None
    tags: list = []


@app.post("/goals")
async def create_goal(req: GoalRequest):
    goal = await goal_engine.create_goal(
        description=req.description,
        priority=req.priority,
        recurring=req.recurring,
        schedule=req.schedule,
        tags=req.tags,
    )
    return goal


@app.get("/goals")
async def list_goals(status: Optional[str] = None):
    return await goal_engine.list_goals(status=status)


@app.get("/goals/{goal_id}")
async def get_goal(goal_id: str):
    goal = await goal_engine.get_goal(goal_id)
    if not goal:
        raise HTTPException(404, "Goal not found")
    return goal


@app.post("/goals/{goal_id}/pause")
async def pause_goal(goal_id: str):
    await goal_engine.pause_goal(goal_id)
    return {"status": "paused"}


@app.post("/goals/{goal_id}/resume")
async def resume_goal(goal_id: str):
    await goal_engine.resume_goal(goal_id)
    return {"status": "active"}


@app.delete("/goals/{goal_id}")
async def cancel_goal(goal_id: str):
    await goal_engine.cancel_goal(goal_id)
    return {"status": "cancelled"}


# ── Memory endpoints ──────────────────────────────────────────────────────────

class MemoryEntry(BaseModel):
    key: str
    value: dict


class BusinessContext(BaseModel):
    company_name: str = ""
    industry: str = ""
    target_market: str = ""
    value_proposition: str = ""
    communication_style: str = "professional"
    extra: dict = {}


class LeadEntry(BaseModel):
    name: str
    company: str = ""
    role: str = ""
    email: str = ""
    linkedin: str = ""
    notes: str = ""


@app.post("/memory")
async def set_memory(entry: MemoryEntry):
    await memory.remember(entry.key, entry.value)
    return {"status": "saved"}


@app.get("/memory/{key}")
async def get_memory(key: str):
    value = await memory.recall(key)
    if value is None:
        raise HTTPException(404, "Key not found")
    return {"key": key, "value": value}


@app.post("/memory/business-context")
async def set_business_context(ctx: BusinessContext):
    await memory.set_business_context(ctx.dict())
    return {"status": "saved"}


@app.post("/leads")
async def save_lead(lead: LeadEntry):
    await memory.save_lead(lead.dict())
    return {"status": "saved"}


@app.get("/leads/search")
async def search_leads(q: str, n: int = 10):
    results = await memory.find_leads(q, n_results=n)
    return results


# ── Screenshots ───────────────────────────────────────────────────────────────

@app.get("/screenshots/{filename}")
async def get_screenshot(filename: str):
    path = f"/tmp/screenshots/{filename}"
    if not os.path.exists(path):
        raise HTTPException(404, "Screenshot not found")
    return FileResponse(path, media_type="image/png")


@app.get("/health")
async def health():
    queue_len = await task_queue.queue_length()
    active = await task_queue.peek_active()
    goals = await goal_engine.list_goals(status="active")
    return {
        "status": "ok",
        "ts": datetime.utcnow().isoformat(),
        "queue_length": queue_len,
        "active_task": active,
        "active_goals": len(goals),
    }


@app.get("/")
async def dashboard():
    if DASHBOARD_HTML.exists():
        return FileResponse(str(DASHBOARD_HTML), media_type="text/html")
    return {"service": "Autonomous Execution System v2", "dashboard": "not found"}


@app.websocket("/ws/dashboard")
async def ws_dashboard(ws: WebSocket):
    await manager.connect(ws)
    pubsub = redis_client.pubsub()
    await pubsub.subscribe("ws:events")

    async def _redis_listener():
        try:
            async for msg in pubsub.listen():
                if msg["type"] == "message":
                    try:
                        await ws.send_json(json.loads(msg["data"]))
                    except Exception:
                        break
        except Exception:
            pass

    listener = asyncio.create_task(_redis_listener())
    try:
        while True:
            try:
                h = await health()
                await ws.send_json({"type": "health", **h})
            except Exception:
                pass
            await asyncio.sleep(5)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        listener.cancel()
        await pubsub.unsubscribe("ws:events")
        await pubsub.aclose()
        manager.disconnect(ws)
