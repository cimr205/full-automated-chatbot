"""
MT5 Worker — runs alongside MetaTrader 5.

Two backends, picked via MT5_BACKEND in .env:
  "native" (default) — Windows PC/VPS with the MetaTrader5 pip package.
            Just double-click START.bat — it handles everything automatically.
  "linux"            — a free Linux box (e.g. Oracle Cloud Always Free) running
            MT5 under Wine + the mt5linux bridge. See OPSAET_GRATIS_VPS.md.
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

MT5_BACKEND = os.environ.get("MT5_BACKEND", "native").lower()   # "native" | "linux"

if MT5_BACKEND == "linux":
    # Gratis VPS-spor: MT5 kører under Wine, vi taler med det via mt5linux's
    # RPyC-bro i stedet for at importere MetaTrader5 nativt (kræver Windows).
    try:
        from mt5linux import MetaTrader5 as _MT5Linux
        mt5 = _MT5Linux(
            host=os.environ.get("MT5_LINUX_HOST", "localhost"),
            port=int(os.environ.get("MT5_LINUX_PORT", "18812")),
        )
        MT5_AVAILABLE = True
        log.info("mt5linux-bro fundet ✓ (host=%s)", os.environ.get("MT5_LINUX_HOST", "localhost"))
    except Exception as e:
        MT5_AVAILABLE = False
        log.warning("mt5linux-bro ikke tilgængelig (%s) — se OPSAET_GRATIS_VPS.md", e)
else:
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
    "AUDJPY=X": "AUDJPY",
    "NZDJPY=X": "NZDJPY",
    "EURAUD=X": "EURAUD",
    "GBPAUD=X": "GBPAUD",
    "AUDNZD=X": "AUDNZD",
    "CHFJPY=X": "CHFJPY",
    "CADJPY=X": "CADJPY",
    "GC=F": "XAUUSD",   # Yahoo gold futures ticker → broker's gold spot symbol
    "SI=F": "XAGUSD",   # Yahoo silver futures ticker → broker's silver spot symbol
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
    return SYMBOL_MAP.get(yf_symbol, yf_symbol.replace("=X", "").replace("^", ""))


# ── Auto-login (used on the zero-touch Railway/linux backend) ────────────────
MT5_LOGIN         = os.environ.get("MT5_LOGIN")
MT5_PASSWORD      = os.environ.get("MT5_PASSWORD")
MT5_SERVER        = os.environ.get("MT5_SERVER")
MT5_TERMINAL_PATH = os.environ.get("MT5_TERMINAL_PATH")  # e.g. C:\\Program Files\\MetaTrader 5\\terminal64.exe


def _initialize() -> bool:
    """
    initialize() also (re)launches and logs into the terminal if it isn't
    already running — used so the Railway container can come up cold with
    no manual login step. Native Windows setups with an already-open,
    logged-in terminal keep working unchanged (no creds configured → bare call).
    """
    if MT5_LOGIN and MT5_PASSWORD and MT5_SERVER:
        kwargs = {"login": int(MT5_LOGIN), "password": MT5_PASSWORD, "server": MT5_SERVER}
        if MT5_TERMINAL_PATH:
            kwargs["path"] = MT5_TERMINAL_PATH
        return mt5.initialize(**kwargs)
    return mt5.initialize()


# ── MT5 execution ─────────────────────────────────────────────────────────────

def _pick_filling_mode(info) -> int:
    """
    Brokers vary in which order-filling mode they accept per symbol — hardcoding
    IOC causes silent 'Unsupported filling mode' rejections on brokers that only
    support FOK (or only RETURN, for exchange-traded instruments). Pick the first
    mode this symbol's filling_mode bitmask actually advertises support for.
    """
    mode = getattr(info, "filling_mode", 0)
    if mode & mt5.SYMBOL_FILLING_IOC:
        return mt5.ORDER_FILLING_IOC
    if mode & mt5.SYMBOL_FILLING_FOK:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


def mt5_open_trade(symbol: str, direction: str, volume: float,
                   sl: float, tp: float, comment: str = "",
                   order_type: str = "market", limit_price: float = 0) -> dict:
    """order_type: "market" (immediate) or "limit" (pending order at limit_price,
    sits until filled or manually cancelled)."""
    if not MT5_AVAILABLE:
        return {"error": "MetaTrader5 ikke installeret"}
    if not _initialize():
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

    if order_type == "limit" and limit_price:
        action   = mt5.TRADE_ACTION_PENDING
        mt5_type = mt5.ORDER_TYPE_BUY_LIMIT if direction == "long" else mt5.ORDER_TYPE_SELL_LIMIT
        price    = limit_price
    else:
        action   = mt5.TRADE_ACTION_DEAL
        mt5_type = mt5.ORDER_TYPE_BUY if direction == "long" else mt5.ORDER_TYPE_SELL
        price    = tick.ask if direction == "long" else tick.bid

    request = {
        "action":       action,
        "symbol":       symbol,
        "volume":       volume,
        "type":         mt5_type,
        "price":        price,
        "sl":           sl,
        "tp":           tp,
        "magic":        MT5_MAGIC,
        "comment":      comment[:31],
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": _pick_filling_mode(info),
    }

    result = mt5.order_send(request)
    mt5.shutdown()

    if result is None:
        return {"error": "order_send returnerede None"}
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return {"error": f"MT5 fejl {result.retcode}: {result.comment}"}

    return {
        "ticket":     result.order,
        "price":      result.price,
        "volume":     result.volume,
        "symbol":     symbol,
        "direction":  direction,
        "order_type": order_type,
        "pending":    order_type == "limit",
    }


def mt5_check_pending(order_ticket: int) -> dict:
    """Status of a pending limit order: still pending, filled (now a position), or cancelled."""
    if not MT5_AVAILABLE:
        return {"error": "MetaTrader5 ikke installeret"}
    if not _initialize():
        return {"error": f"MT5 initialize fejlede: {mt5.last_error()}"}

    still_pending = mt5.orders_get(ticket=order_ticket)
    if still_pending:
        mt5.shutdown()
        return {"status": "pending"}

    hist = mt5.history_orders_get(ticket=order_ticket)
    mt5.shutdown()
    if not hist:
        return {"status": "unknown"}

    order = hist[0]
    if order.state == mt5.ORDER_STATE_FILLED:
        return {
            "status":       "filled",
            "position_id":  order.position_id,
            "price":        order.price_open,
        }
    if order.state in (mt5.ORDER_STATE_CANCELED, mt5.ORDER_STATE_EXPIRED, mt5.ORDER_STATE_REJECTED):
        return {"status": "cancelled"}
    return {"status": "unknown"}


def mt5_cancel_pending(order_ticket: int) -> dict:
    if not MT5_AVAILABLE:
        return {"error": "MetaTrader5 ikke installeret"}
    if not _initialize():
        return {"error": f"MT5 initialize fejlede: {mt5.last_error()}"}

    request = {"action": mt5.TRADE_ACTION_REMOVE, "order": order_ticket}
    result = mt5.order_send(request)
    mt5.shutdown()

    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        err = result.comment if result else "None"
        return {"error": f"Annullering fejlede: {err}"}
    return {"cancelled": True, "ticket": order_ticket}


def mt5_get_account_info() -> dict:
    if not MT5_AVAILABLE:
        return {"error": "MetaTrader5 ikke installeret"}
    if not _initialize():
        return {"error": f"MT5 initialize fejlede: {mt5.last_error()}"}

    info = mt5.account_info()
    mt5.shutdown()
    if info is None:
        return {"error": f"account_info fejlede: {mt5.last_error()}"}

    return {
        "balance":  info.balance,
        "equity":   info.equity,
        "margin":   info.margin,
        "currency": info.currency,
    }


def mt5_get_symbol_info(symbol: str) -> dict:
    if not MT5_AVAILABLE:
        return {"error": "MetaTrader5 ikke installeret"}
    if not _initialize():
        return {"error": f"MT5 initialize fejlede: {mt5.last_error()}"}

    info = mt5.symbol_info(symbol)
    if info is None:
        mt5.shutdown()
        return {"error": f"Symbol {symbol} ikke fundet"}
    if not info.visible:
        mt5.symbol_select(symbol, True)
        info = mt5.symbol_info(symbol)
    mt5.shutdown()

    return {
        "symbol":              symbol,
        "trade_contract_size": info.trade_contract_size,
        "trade_tick_value":    info.trade_tick_value,
        "trade_tick_size":     info.trade_tick_size,
        "volume_min":          info.volume_min,
        "volume_max":          info.volume_max,
        "volume_step":         info.volume_step,
        "digits":              info.digits,
    }


def mt5_close_trade(ticket: int, symbol: str, direction: str, volume: float) -> dict:
    if not MT5_AVAILABLE:
        return {"error": "MetaTrader5 ikke installeret"}
    if not _initialize():
        return {"error": f"MT5 initialize fejlede: {mt5.last_error()}"}

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        mt5.shutdown()
        return {"error": f"Ingen tick data for {symbol}"}

    info = mt5.symbol_info(symbol)

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
        "type_filling": _pick_filling_mode(info) if info else mt5.ORDER_FILLING_IOC,
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
        order_type  = cmd.get("order_type", "market")
        limit_price = float(cmd.get("limit_price", 0) or 0)
        result  = mt5_open_trade(symbol, direction, volume, sl, tp,
                                 comment=f"auto_{trade_id[:8]}",
                                 order_type=order_type, limit_price=limit_price)
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
            log.info("Åbnet: ticket=%s @ %s (%s)", result.get("ticket"), result.get("price"), order_type)

    elif command == "check_pending" and ticket:
        result  = mt5_check_pending(int(ticket))
        payload = {
            "trade_id": trade_id,
            "command":  "check_pending",
            "result":   result,
            "ts":       datetime.utcnow().isoformat(),
        }

    elif command == "cancel_pending" and ticket:
        result  = mt5_cancel_pending(int(ticket))
        payload = {
            "trade_id": trade_id,
            "command":  "cancel_pending",
            "result":   result,
            "ts":       datetime.utcnow().isoformat(),
        }
        if result.get("cancelled"):
            log.info("Pending order annulleret: ticket=%s", ticket)
        else:
            log.error("Annullering fejlede: %s", result.get("error"))

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

    elif command == "get_account_info":
        result  = mt5_get_account_info()
        payload = {
            "trade_id": trade_id,
            "command":  "get_account_info",
            "result":   result,
            "ts":       datetime.utcnow().isoformat(),
        }
        if "error" in result:
            log.error("account_info fejlede: %s", result["error"])

    elif command == "get_symbol_info":
        result  = mt5_get_symbol_info(symbol)
        payload = {
            "trade_id": trade_id,
            "command":  "get_symbol_info",
            "symbol":   symbol,
            "result":   result,
            "ts":       datetime.utcnow().isoformat(),
        }
        if "error" in result:
            log.error("symbol_info fejlede for %s: %s", symbol, result["error"])

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
    """Main loop with auto-reconnect."""
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
