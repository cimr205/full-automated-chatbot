# Autonomous Execution System — MVP Phase 1

A persistent, Telegram-controlled browser automation system with AI reasoning, task queuing, crash recovery, and human-in-the-loop authentication.

## Architecture

```
Telegram Bot
    ↓
FastAPI Backend
    ↓
Task Queue (Redis)
    ↓
Supervisor (deterministic orchestrator)
    ↓
Browser Worker (Playwright + persistent Chromium)
    ↓
Ollama (local AI reasoning)
    ↓
Memory (PostgreSQL + ChromaDB)
```

## Stack

| Component | Technology |
|-----------|-----------|
| Telegram bot | python-telegram-bot |
| API | FastAPI |
| Queue | Redis (pub/sub + lists) |
| Database | PostgreSQL |
| Semantic memory | ChromaDB |
| Browser | Playwright + persistent Chromium |
| AI reasoning | Ollama (llama3:8b) |
| Vision | Ollama (qwen2.5vl) |

## Quick Start

### 1. Prerequisites

- Docker + Docker Compose
- Ollama running on a VPS or locally
- Telegram bot token (from @BotFather)
- Your Telegram chat ID

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your values
```

Required variables:
```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
OLLAMA_URL=http://YOUR_VPS_IP:11434
```

### 3. Pull Ollama models (on your VPS)

```bash
ollama pull llama3:8b
ollama pull qwen2.5vl
```

### 4. Start the system

```bash
cd infrastructure
docker-compose --env-file ../.env up -d
```

### 5. Use via Telegram

```
/task Go to google.com and search for Danish SaaS founders
/tasks
/status task_abc123
/reply task_abc123 Click continue with Google
```

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/task <desc>` | Queue a new task |
| `/tasks` | List recent tasks |
| `/status <id>` | Check task status |
| `/reply <id> <msg>` | Send instruction to waiting task |
| `/help` | Show commands |

## Human-in-the-Loop Flow

When the browser hits a login screen or captcha:

1. System captures a screenshot
2. Screenshot sent to Telegram with explanation
3. Task pauses — `waiting_for_human`
4. You reply: `/reply task_id Click continue with Google`
5. System executes and continues immediately

## Task States

| State | Meaning |
|-------|---------|
| `queued` | Waiting to run |
| `running` | Executing |
| `waiting_for_human` | Needs your input |
| `retrying` | Failed, attempting retry |
| `completed` | Done |
| `failed` | Failed after max retries |

## Folder Structure

```
apps/
  api/           FastAPI backend
  telegram-bot/  Telegram control layer
core/
  queue/         Redis task queue
  memory/        PostgreSQL persistence
  supervisor/    Deterministic task orchestrator
  checkpoints/   File-based checkpoint fallback
workers/
  browser/       Playwright browser worker + Ollama client
models/
  prompts/       AI prompt templates
infrastructure/
  docker-compose.yml
  railway.json
```

## Recovery

- Browser sessions persist across restarts via Chromium persistent context
- Checkpoints saved after every step (URL + page state)
- Supervisor retries failed tasks up to 3 times
- On worker crash: last checkpoint restored, task re-queued automatically

## Deployment

**Railway** (API + Telegram bot + Redis + Postgres):
Set env vars in Railway dashboard, connect this repo, deploy.

**VPS** (Ollama + browser workers):
```bash
ollama serve &
docker-compose up browser -d
```
