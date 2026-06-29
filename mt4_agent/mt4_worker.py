"""
MT4 Worker — runs on your Windows PC alongside MetaTrader 4.

MT4 has no official Python package, so this worker talks to the
MT4_Bridge.mq4 Expert Advisor through plain text files instead:
it writes a `<trade_id>.cmd` file, the EA picks it up, executes the
trade, and writes back a `<trade_id>.result` file.

Just double-click START.bat — it handles everything automatically.
"""
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import redis.asyncio as aioredis
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("mt4_worker.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

REDIS_URL      = os.environ.get("REDIS_URL", "")
MT4_FILES_PATH = os.environ.get("MT4_FILES_PATH", "")
DEFAULT_VOLUME = float(os.environ.get("MT4_LOT_SIZE", "0.01"))
MT4_MAGIC      = int(os.environ.get("MT4_MAGIC", "777000"))

if not REDIS_URL or not MT4_FILES_PATH:
    print("\n" + "="*60)
    print("FEJL: REDIS_URL eller MT4_FILES_PATH mangler i .env filen!")
    print("Kør START.bat igen for at sætte dem op.")
    print("="*60 + "\n")
    sys.exit(1)

BRIDGE_DIR     = Path(MT4_FILES_PATH) / "mt4_bridge"
COMMANDS_DIR   = BRIDGE_DIR / "commands"
RESULTS_DIR    = BRIDGE_DIR / "results"
HEARTBEAT_FILE = BRIDGE_DIR / "heartbeat.txt"
COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


SYMBOL_MAP = {
    "EURUSD=X": "EURUSD",
    "GBPUSD=X": "GBPUSD",
    "USDJPY=X": "USDJPY",
    "GBPJPY=X": "GBPJPY",
    "AUDUSD=X": "AUDUSD",
    "USDCHF=X": "USDCHF",
    "USDCAD=X": "USDCAD",
    "EURGBP=X": "EURGBP",
    "EURJPY=X": "EURJPY",
    "NZDUSD=X": "NZDUSD",
    "XAUUSD=X": "XAUUSD",
    "GC=F":     "XAUUSD",
    "SPY":   "US500",
    "QQQ":   "US100",
    "^GSPC": "US500",
    "^NDX":  "US100",
    "^DJI":  "US30",
    "NVDA":  "NVDA",
    "AAPL":  "AAPL",
    "MSFT":  "MSFT",
}

def to_mt4_symbol(yf_symbol: str) -> str:
    mapped = SYMBOL_MAP.get(yf_symbol, yf_symbol.replace("=X", "").replace("^", ""))
    if mapped == "XAUUSD":
        return os.getenv("GOLD_SYMBOL", "XAUUSD")
    return mapped


def ea_alive(max_age: float = 5.0) -> bool:
    """The EA writes a heartbeat file every CheckIntervalMs — fresh file = EA attached & running."""
    try:
        age = time.time() - HEARTBEAT_FILE.stat().st_mtime
        return age < max_age
    except FileNotFoundError:
        return False


def write_cmd_file(trade_id: str, fields: dict):
    # Write to a temp file then rename atomically — the EA polls this folder
    # every CheckIntervalMs and must never see a partially-written command.
    path = COMMANDS_DIR / f"{trade_id}.cmd"
    tmp_path = COMMANDS_DIR / f"{trade_id}.cmd.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        for k, v in fields.items():
            f.write(f"{k}={v}\n")
    tmp_path.replace(path)


def read_result_file(path: Path) -> dict:
    out = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k] = v
    return out


async def wait_for_result(trade_id: str, timeout: float = 20.0) -> dict | None:
    result_path = RESULTS_DIR / f"{trade_id}.result"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if result_path.exists():
            await asyncio.sleep(0.1)  # let the EA finish writing before we read
            try:
                data = read_result_file(result_path)
                result_path.unlink(missing_ok=True)
                return data
            except Exception:
                pass
        await asyncio.sleep(0.3)
    return None


# ── Redis command handler ─────────────────────────────────────────────────────

async def handle_command(cmd: dict, redis: aioredis.Redis):
    command   = cmd.get("command")
    trade_id  = cmd.get("trade_id") or f"ping_{uuid.uuid4().hex[:8]}"
    symbol    = to_mt4_symbol(cmd.get("symbol", ""))
    direction = cmd.get("direction", "long")
    volume    = float(cmd.get("volume") or DEFAULT_VOLUME)
    sl        = float(cmd.get("stop_loss", 0))
    tp        = float(cmd.get("take_profit", 0))
    ticket    = cmd.get("ticket")

    log.info("Kommando: %s %s %s vol=%s", command, symbol, direction, volume)

    if command == "ping":
        alive = ea_alive()
        await redis.publish("trading:mt4:results", json.dumps({
            "command": "pong", "ts": datetime.utcnow().isoformat(), "mt4_available": alive,
        }))
        log.info("Ping -> pong (ea_alive=%s)", alive)
        return

    if not ea_alive():
        log.warning("Expert Advisor svarer ikke (intet heartbeat) — er den tilknyttet et chart?")

    if command == "open":
        write_cmd_file(trade_id, {
            "command": "open", "trade_id": trade_id, "symbol": symbol,
            "direction": direction, "volume": volume, "sl": sl, "tp": tp,
            "magic": MT4_MAGIC,
        })
        raw = await wait_for_result(trade_id)
        if raw is None:
            result = {"error": "timeout — er Expert Advisor (MT4_Bridge.mq4) tilknyttet et chart?"}
        elif raw.get("error"):
            result = {"error": raw["error"]}
        else:
            result = {
                "ticket":    int(raw["ticket"]),
                "price":     float(raw["price"]),
                "volume":    float(raw.get("volume", volume)),
                "symbol":    symbol,
                "direction": direction,
            }
        if "error" in result:
            log.error("Åbn fejlede: %s", result["error"])
        else:
            log.info("Åbnet: ticket=%s @ %s", result["ticket"], result["price"])
        payload = {"trade_id": trade_id, "command": "open", "symbol": symbol,
                  "result": result, "ts": datetime.utcnow().isoformat()}

    elif command == "close" and ticket:
        write_cmd_file(trade_id, {
            "command": "close", "trade_id": trade_id, "symbol": symbol,
            "direction": direction, "volume": volume, "ticket": ticket,
        })
        raw = await wait_for_result(trade_id)
        if raw is None:
            result = {"error": "timeout"}
        elif raw.get("error"):
            result = {"error": raw["error"]}
        else:
            result = {"closed": True, "price": float(raw["price"]), "ticket": int(raw["ticket"])}
        if result.get("closed"):
            log.info("Lukket: ticket=%s @ %s", ticket, result.get("price"))
        else:
            log.error("Luk fejlede: %s", result.get("error"))
        payload = {"trade_id": trade_id, "command": "close", "symbol": symbol,
                  "result": result, "ts": datetime.utcnow().isoformat()}

    else:
        return

    await redis.publish("trading:mt4:results", json.dumps(payload))


async def connect_and_listen():
    log.info("Forbinder til Redis …")
    redis = aioredis.from_url(REDIS_URL, decode_responses=True,
                              socket_connect_timeout=10,
                              socket_keepalive=True)

    await redis.ping()
    log.info("Redis forbundet ✓")

    await redis.publish("trading:mt4:results", json.dumps({
        "command":       "startup",
        "mt4_available": ea_alive(),
        "ts":            datetime.utcnow().isoformat(),
    }))
    log.info("Startup besked sendt — bot vil modtage '✅ MT4 Worker tilkoblet' på Telegram")
    if not ea_alive():
        log.warning("Heartbeat ikke fundet endnu — husk at tilknytte MT4_Bridge til et chart i MetaTrader 4!")

    pubsub = redis.pubsub()
    await pubsub.subscribe("trading:mt4:commands")
    log.info("Lytter på handelskommandoer … (lad dette vindue være åbent)")

    async for message in pubsub.listen():
        if message["type"] != "message":
            continue
        try:
            cmd = json.loads(message["data"])
            asyncio.create_task(handle_command(cmd, redis))
        except Exception as e:
            log.error("Kommando fejl: %s", e)


async def main():
    print("\n" + "="*60)
    print("  MT4 Worker — Automatisk handelsudførelse")
    print("="*60)
    print("  Lad dette vindue være åbent mens du trader.")
    print("  Husk: MT4_Bridge.mq4 skal være tilknyttet et chart i MT4.")
    print("="*60 + "\n")

    retry_delay = 5
    while True:
        try:
            await connect_and_listen()
        except KeyboardInterrupt:
            log.info("Stopper MT4 Worker …")
            break
        except Exception as e:
            log.error("Forbindelsesfejl: %s — prøver igen om %ds …", e, retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)
        else:
            retry_delay = 5


if __name__ == "__main__":
    asyncio.run(main())
