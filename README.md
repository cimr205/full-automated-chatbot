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

## Trading (MT5 + risk protection)

The system can also auto-scan Forex/XAUUSD and execute trades via a Windows
MT5 terminal, with account-level risk protection so it never blows past a
daily-loss or max-drawdown limit.

**Setup — three ways to run the MT5 side, pick one:**
1. **Railway (recommended, zero-touch)** — runs MT5 under Wine as a 4th Railway
   service in the same project as your API/bot/Redis, with auto-login (no VPS,
   no Windows PC, no manual steps after the one-time setup). See
   `mt5_agent/RAILWAY_MT5_OPSAETNING.md`.
2. **Your own Windows PC/VPS** — run `mt5_agent/mt5_worker.py` (via `START.bat`)
   with MT5 logged in. Must be **redeployed** (re-copied) whenever it changes —
   it now also reports live account equity/balance and per-symbol contract data
   used for position sizing.
3. **Free Linux VPS (Oracle Cloud Always Free)** — see `mt5_agent/OPSAET_GRATIS_VPS.md`
   if you'd rather not add a 4th Railway service.
2. Set the risk env vars in `.env` (see `.env.example`): `RISK_PER_TRADE_PCT`,
   `RISK_MAX_DAILY_LOSS_PCT`, `RISK_MAX_TOTAL_DRAWDOWN_PCT`, `SIGNAL_CONFIRM_BAND`.

**How it stays safe:**
- Position size is computed from real account equity and the broker's own contract/tick
  data for each symbol (correct for both forex and XAUUSD, whose contract sizes differ a lot).
- Borderline-confidence signals are **not** traded on the first read — they must reproduce
  the same direction + setup on the next scan before the bot enters.
- If daily loss or total drawdown exceeds the configured limit, trading locks immediately
  and stays locked — including across restarts — until you review the account and send `/unlock_risk`.

**Telegram commands:**

| Command | Description |
|---------|-------------|
| `/scan` | Trigger an immediate market scan |
| `/chart` | Send the watchlist overview chart now (no longer sent automatically) |
| `/trades` | List open trades + stats |
| `/trading_pause` / `/trading_resume` | Pause/resume new auto-trades (open positions still monitored) |
| `/risk` | Show equity, daily/total drawdown, and lock status |
| `/unlock_risk` | Clear a tripped risk-lock after reviewing the account |
| `/report daily` / `/report weekly` | Send a performance report now (also sent automatically once per day) |
| `/lessons` | Win rate per setup type, and which ones the bot has stopped using after a bad track record |
| `/backtest <symbol>` | Measure the real historical win rate for a symbol (e.g. `/backtest EURUSD=X`) against ~2 years of data |
| `/seed_learning` | Pre-load `/lessons` with backtested history across the whole watchlist, instead of waiting for 5+ live trades per setup |
| `/watchlist` | View/edit the Forex + Stocks watchlists |

**Backtested baseline (as of this writing, EURUSD/Gold, ~2yr 1h data):** ~32-33% win rate,
roughly breakeven-to-slightly-negative expectancy at the current 1:2 R:R. Per-setup it's
uneven — e.g. `bullish_pullback` was the strongest setup on EURUSD (56% win rate) but one of
the weakest on Gold (22%), and vice versa for `bullish_fvg`. This is exactly why the
per-setup learning/blocking exists — run `/backtest` yourself before trusting any of this
on a live account, numbers drift as markets change.

**Known limitation:** `/lessons` blocking is currently global per setup type, not
per-symbol — a setup performing well on one instrument and badly on another will be
judged on its combined track record. Worth revisiting if you trade a wide watchlist.

**On "learning from mistakes":** there's no magic 98%-win-rate AI here — that's not a real
thing any honest system can promise. What *is* real: every closed trade is tagged by setup
type, and once a setup type has lost money over enough trades (`core/trading/learning.py`,
5+ trades and <35% win rate by default), it's automatically blocked from auto-executing
again until you review it.

**Telegram notifications are trade-event-only, by design:** a signal found → trade opened →
SL/TP/partial hit → trade closed. No per-minute "still open" digest and no automatic
watchlist chart — both existed earlier and were removed after producing hundreds of
messages overnight for a single open trade. Use `/trades` or `/chart` on demand instead.

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
