"""
MT5 Worker — runs on your Windows PC alongside MetaTrader 5.

Just double-click START.bat — it handles everything automatically.
"""
import asyncio
import json
import logging
import os
import sys
from datetime import datetime

import redis.asyncio as aioredis
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("mt5_worker.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

REDIS_URL      = os.environ.get("REDIS_URL", "")
DEFAULT_VOLUME = float(os.environ.get("MT5_LOT_SIZE", "0.01"))
MT5_MAGIC      = int(os.environ.get("MT5_MAGIC", "777000"))

if not REDIS_URL:
    print("\n" + "="*60)
    print("FEJL: REDIS_URL mangler i .env filen!")
    print("Åbn .env filen og indsæt din Railway Redis URL.")
    print("="*60 + "\n")
    sys.exit(1)

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
    log.info("MetaTrader5 pakke fundet ✓")
except ImportError:
    MT5_AVAILABLE = False
    log.warning("MetaTrader5 pakke ikke installeret — kør START.bat igen")


# ── Symbol name mapping ───────────────────────────────────────────────────────
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

def to_mt5_symbol(yf_symbol: str) -> str:
    mapped = SYMBOL_MAP.get(yf_symbol, yf_symbol.replace("=X", "").replace("^", ""))
    if mapped == "XAUUSD":
        return os.getenv("GOLD_SYMBOL", "XAUUSD")
    return mapped


# ── MT5 execution ─────────────────────────────────────────────────────────────

def mt5_open_trade(symbol: str, direction: str, volume: float,
                   sl: float, tp: float, comment: str = "") -> dict:
    if not MT5_AVAILABLE:
        return {"error": "MetaTrader5 ikke installeret"}
    if not mt5.initialize():
        return {"error": f"MT5 initialize fejlede: {mt5.last_error()}"}

    info = mt5.symbol_info(symbol)
    if info is None:
        mt5.shutdown()
        return {"error": f"Symbol {symbol} ikke fundet. Tjek SYMBOL_MAP i mt5_worker.py"}
    if not info.visible:
        mt5.symbol_select(symbol, True)

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        mt5.shutdown()
        return {"error": f"Ingen tick data for {symbol}"}

    order_type = mt5.ORDER_TYPE_BUY if direction == "long" else mt5.ORDER_TYPE_SELL
    price      = tick.ask if direction == "long" else tick.bid

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       volume,
        "type":         order_type,
        "price":        price,
        "sl":           sl,
        "tp":           tp,
        "magic":        MT5_MAGIC,
        "comment":      comment[:31],
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    mt5.shutdown()

    if result is None:
        return {"error": "order_send returnerede None"}
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return {"error": f"MT5 fejl {result.retcode}: {result.comment}"}

    return {
        "ticket":    result.order,
        "price":     result.price,
        "volume":    result.volume,
        "symbol":    symbol,
        "direction": direction,
    }


def mt5_close_trade(ticket: int, symbol: str, direction: str, volume: float) -> dict:
    if not MT5_AVAILABLE:
        return {"error": "MetaTrader5 ikke installeret"}
    if not mt5.initialize():
        return {"error": f"MT5 initialize fejlede: {mt5.last_error()}"}

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        mt5.shutdown()
        return {"error": f"Ingen tick data for {symbol}"}

    close_type  = mt5.ORDER_TYPE_SELL if direction == "long" else mt5.ORDER_TYPE_BUY
    close_price = tick.bid if direction == "long" else tick.ask

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "position":     ticket,
        "symbol":       symbol,
        "volume":       volume,
        "type":         close_type,
        "price":        close_price,
        "magic":        MT5_MAGIC,
        "comment":      "auto_close",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    mt5.shutdown()

    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        err = result.comment if result else "None"
        return {"error": f"Luk fejlede: {err}"}

    return {"closed": True, "price": result.price, "ticket": ticket}


# ── Redis command handler ─────────────────────────────────────────────────────

async def handle_command(cmd: dict, redis: aioredis.Redis):
    command   = cmd.get("command")
    trade_id  = cmd.get("trade_id", "?")
    symbol    = to_mt5_symbol(cmd.get("symbol", ""))
    direction = cmd.get("direction", "long")
    volume    = float(cmd.get("volume") or DEFAULT_VOLUME)
    sl        = float(cmd.get("stop_loss", 0))
    tp        = float(cmd.get("take_profit", 0))
    ticket    = cmd.get("ticket")

    log.info("Kommando: %s %s %s vol=%s", command, symbol, direction, volume)

    if command == "open":
        result  = mt5_open_trade(symbol, direction, volume, sl, tp,
                                 comment=f"auto_{trade_id[:8]}")
        payload = {
            "trade_id": trade_id,
            "command":  "open",
            "symbol":   symbol,
            "result":   result,
            "ts":       datetime.utcnow().isoformat(),
        }
        if "error" in result:
            log.error("Åbn fejlede: %s", result["error"])
        else:
            log.info("Åbnet: ticket=%s @ %s", result.get("ticket"), result.get("price"))

    elif command == "close" and ticket:
        result  = mt5_close_trade(int(ticket), symbol, direction, volume)
        payload = {
            "trade_id": trade_id,
            "command":  "close",
            "symbol":   symbol,
            "result":   result,
            "ts":       datetime.utcnow().isoformat(),
        }
        if result.get("closed"):
            log.info("Lukket: ticket=%s @ %s", ticket, result.get("price"))
        else:
            log.error("Luk fejlede: %s", result.get("error"))

    elif command == "ping":
        payload = {
            "command":       "pong",
            "ts":            datetime.utcnow().isoformat(),
            "mt5_available": MT5_AVAILABLE,
        }
        log.info("Ping → pong (mt5_available=%s)", MT5_AVAILABLE)

    else:
        return

    await redis.publish("trading:mt5:results", json.dumps(payload))


async def connect_and_listen():
    """Connect to Redis, announce startup, and listen for commands."""
    log.info("Forbinder til Redis …")
    redis = aioredis.from_url(REDIS_URL, decode_responses=True,
                              socket_connect_timeout=10,
                              socket_keepalive=True)

    await redis.ping()
    log.info("Redis forbundet ✓")

    await redis.publish("trading:mt5:results", json.dumps({
        "command":       "startup",
        "mt5_available": MT5_AVAILABLE,
        "ts":            datetime.utcnow().isoformat(),
    }))
    log.info("Startup besked sendt — bot vil modtage '✅ MT5 Worker tilkoblet' på Telegram")

    pubsub = redis.pubsub()
    await pubsub.subscribe("trading:mt5:commands")
    log.info("Lytter på handelskommandoer … (lad dette vindue være åbent)")

    async for message in pubsub.listen():
        if message["type"] != "message":
            continue
        try:
            cmd = json.loads(message["data"])
            await handle_command(cmd, redis)
        except Exception as e:
            log.error("Kommando fejl: %s", e)


async def main():
    print("\n" + "="*60)
    print("  MT5 Worker — Automatisk handelsudførelse")
    print("="*60)
    print("  Lad dette vindue være åbent mens du trader.")
    print("  Din bot sender dig en Telegram-besked når forbundet.")
    print("="*60 + "\n")

    retry_delay = 5
    while True:
        try:
            await connect_and_listen()
        except KeyboardInterrupt:
            log.info("Stopper MT5 Worker …")
            break
        except Exception as e:
            log.error("Forbindelsesfejl: %s — prøver igen om %ds …", e, retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)
        else:
            retry_delay = 5


if __name__ == "__main__":
    asyncio.run(main())
