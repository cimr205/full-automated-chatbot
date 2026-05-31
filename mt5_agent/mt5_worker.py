"""
MT5 Worker — runs on your Windows PC alongside MetaTrader 5.

Listens to Railway's Redis for trade commands, executes them in MT5,
and reports results back so Railway can update position tracking.

Setup:
  pip install MetaTrader5 redis python-dotenv
  copy .env.example to .env and fill in REDIS_URL
  python mt5_worker.py
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

REDIS_URL       = os.environ.get("REDIS_URL", "redis://localhost:6379")
DEFAULT_VOLUME  = float(os.environ.get("MT5_LOT_SIZE", "0.01"))
MT5_MAGIC       = int(os.environ.get("MT5_MAGIC", "777000"))

# MT5 must be imported on Windows only
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    log.warning("MetaTrader5 package not installed. Run: pip install MetaTrader5")


# ── Symbol name mapping ───────────────────────────────────────────────────────
# Yahoo Finance → MT5 symbol names (customize for your broker)
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
    # Indices (names vary by broker — adjust for yours)
    "SPY":    "US500",     # or "SP500", "US500m", etc.
    "QQQ":    "US100",     # or "NAS100"
    "^GSPC":  "US500",
    "^NDX":   "US100",
    "^DJI":   "US30",
    "NVDA":   "NVDA",      # only if broker offers stock CFDs
    "AAPL":   "AAPL",
}

def to_mt5_symbol(yf_symbol: str) -> str:
    return SYMBOL_MAP.get(yf_symbol, yf_symbol.replace("=X", "").replace("^", ""))


# ── MT5 execution ─────────────────────────────────────────────────────────────

def mt5_open_trade(symbol: str, direction: str, volume: float,
                   sl: float, tp: float, comment: str = "") -> dict:
    if not MT5_AVAILABLE:
        return {"error": "MetaTrader5 not installed"}
    if not mt5.initialize():
        return {"error": f"MT5 initialize failed: {mt5.last_error()}"}

    info = mt5.symbol_info(symbol)
    if info is None:
        mt5.shutdown()
        return {"error": f"Symbol {symbol} not found. Check SYMBOL_MAP in mt5_worker.py"}
    if not info.visible:
        mt5.symbol_select(symbol, True)

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        mt5.shutdown()
        return {"error": f"No tick data for {symbol}"}

    order_type = mt5.ORDER_TYPE_BUY if direction == "long" else mt5.ORDER_TYPE_SELL
    price      = tick.ask if direction == "long" else tick.bid

    request = {
        "action":      mt5.TRADE_ACTION_DEAL,
        "symbol":      symbol,
        "volume":      volume,
        "type":        order_type,
        "price":       price,
        "sl":          sl,
        "tp":          tp,
        "magic":       MT5_MAGIC,
        "comment":     comment[:31],
        "type_time":   mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    mt5.shutdown()

    if result is None:
        return {"error": "order_send returned None"}
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return {"error": f"MT5 error {result.retcode}: {result.comment}"}

    return {
        "ticket":     result.order,
        "price":      result.price,
        "volume":     result.volume,
        "symbol":     symbol,
        "direction":  direction,
    }


def mt5_close_trade(ticket: int, symbol: str, direction: str, volume: float) -> dict:
    if not MT5_AVAILABLE:
        return {"error": "MetaTrader5 not installed"}
    if not mt5.initialize():
        return {"error": f"MT5 initialize failed: {mt5.last_error()}"}

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        mt5.shutdown()
        return {"error": f"No tick data for {symbol}"}

    close_type = mt5.ORDER_TYPE_SELL if direction == "long" else mt5.ORDER_TYPE_BUY
    close_price = tick.bid if direction == "long" else tick.ask

    request = {
        "action":   mt5.TRADE_ACTION_DEAL,
        "position": ticket,
        "symbol":   symbol,
        "volume":   volume,
        "type":     close_type,
        "price":    close_price,
        "magic":    MT5_MAGIC,
        "comment":  "auto_close",
        "type_time":   mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    mt5.shutdown()

    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        err = result.comment if result else "None"
        return {"error": f"Close failed: {err}"}

    return {"closed": True, "price": result.price, "ticket": ticket}


def mt5_get_position(ticket: int) -> dict | None:
    if not MT5_AVAILABLE or not mt5.initialize():
        return None
    positions = mt5.positions_get(ticket=ticket)
    mt5.shutdown()
    if not positions:
        return None
    p = positions[0]
    return {
        "ticket":     p.ticket,
        "symbol":     p.symbol,
        "volume":     p.volume,
        "price_open": p.price_open,
        "price_current": p.price_current,
        "profit":     p.profit,
        "sl":         p.sl,
        "tp":         p.tp,
    }


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

    log.info("Command: %s %s %s vol=%s", command, symbol, direction, volume)

    if command == "open":
        result = mt5_open_trade(symbol, direction, volume, sl, tp,
                                comment=f"auto_{trade_id[:8]}")
        payload = {
            "trade_id": trade_id,
            "command":  "open",
            "symbol":   symbol,
            "result":   result,
            "ts":       datetime.utcnow().isoformat(),
        }
        if "error" in result:
            log.error("Open failed: %s", result["error"])
        else:
            log.info("Opened: ticket=%s @ %s", result.get("ticket"), result.get("price"))

    elif command == "close" and ticket:
        result = mt5_close_trade(int(ticket), symbol, direction, volume)
        payload = {
            "trade_id": trade_id,
            "command":  "close",
            "symbol":   symbol,
            "result":   result,
            "ts":       datetime.utcnow().isoformat(),
        }

    elif command == "ping":
        payload = {"command": "pong", "ts": datetime.utcnow().isoformat(),
                   "mt5_available": MT5_AVAILABLE}
    else:
        return

    await redis.publish("trading:mt5:results", json.dumps(payload))


async def main():
    log.info("MT5 Worker starting — connecting to Redis: %s", REDIS_URL[:30] + "…")
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)

    # Send startup ping
    await redis.publish("trading:mt5:results", json.dumps({
        "command": "startup",
        "mt5_available": MT5_AVAILABLE,
        "ts": datetime.utcnow().isoformat(),
    }))

    pubsub = redis.pubsub()
    await pubsub.subscribe("trading:mt5:commands")
    log.info("Listening on trading:mt5:commands …")

    async for message in pubsub.listen():
        if message["type"] != "message":
            continue
        try:
            cmd = json.loads(message["data"])
            await handle_command(cmd, redis)
        except Exception as e:
            log.error("Command handler error: %s", e)


if __name__ == "__main__":
    asyncio.run(main())
