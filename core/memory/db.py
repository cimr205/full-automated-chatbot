import json
import logging
import os
from datetime import datetime
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    description TEXT,
    status TEXT,
    priority INT DEFAULT 5,
    retry_count INT DEFAULT 0,
    created_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    data JSONB,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS task_logs (
    id SERIAL PRIMARY KEY,
    task_id TEXT REFERENCES tasks(task_id),
    level TEXT,
    message TEXT,
    ts TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS memory (
    id SERIAL PRIMARY KEY,
    key TEXT UNIQUE,
    value JSONB,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
"""


class Database:
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None
        self.dsn = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/executor")

    async def connect(self):
        try:
            self.pool = await asyncpg.create_pool(self.dsn, min_size=2, max_size=10)
            async with self.pool.acquire() as conn:
                await conn.execute(SCHEMA)
            log.info("Database connected")
        except Exception as e:
            log.warning("Database unavailable: %s — running without persistence", e)

    async def disconnect(self):
        if self.pool:
            await self.pool.close()

    async def save_task(self, task: dict):
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO tasks (task_id, description, status, priority, retry_count,
                                       created_at, started_at, completed_at, data, updated_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,NOW())
                    ON CONFLICT (task_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        retry_count = EXCLUDED.retry_count,
                        started_at = EXCLUDED.started_at,
                        completed_at = EXCLUDED.completed_at,
                        data = EXCLUDED.data,
                        updated_at = NOW()
                    """,
                    task["task_id"],
                    task.get("description", ""),
                    task.get("status", "queued"),
                    task.get("priority", 5),
                    task.get("retry_count", 0),
                    task.get("created_at"),
                    task.get("started_at"),
                    task.get("completed_at"),
                    json.dumps(task),
                )
        except Exception as e:
            log.error("DB save_task error: %s", e)

    async def get_task(self, task_id: str) -> Optional[dict]:
        if not self.pool:
            return None
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("SELECT data FROM tasks WHERE task_id=$1", task_id)
                if row:
                    return json.loads(row["data"])
        except Exception as e:
            log.error("DB get_task error: %s", e)
        return None

    async def list_tasks(self, limit: int = 50) -> list:
        if not self.pool:
            return []
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT data FROM tasks ORDER BY updated_at DESC LIMIT $1", limit
                )
                return [json.loads(r["data"]) for r in rows]
        except Exception as e:
            log.error("DB list_tasks error: %s", e)
        return []

    async def log_task(self, task_id: str, level: str, message: str):
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO task_logs (task_id, level, message) VALUES ($1,$2,$3)",
                    task_id, level, message,
                )
        except Exception as e:
            log.error("DB log_task error: %s", e)

    async def set_memory(self, key: str, value: dict):
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO memory (key, value, updated_at)
                    VALUES ($1,$2,NOW())
                    ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
                    """,
                    key, json.dumps(value),
                )
        except Exception as e:
            log.error("DB set_memory error: %s", e)

    async def get_memory(self, key: str) -> Optional[dict]:
        if not self.pool:
            return None
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("SELECT value FROM memory WHERE key=$1", key)
                if row:
                    return json.loads(row["value"])
        except Exception as e:
            log.error("DB get_memory error: %s", e)
        return None
