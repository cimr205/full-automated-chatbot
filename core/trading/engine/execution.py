from __future__ import annotations

"""
Broker-agnostic execution interface (spec section 27) plus the paper-
trading and live-safety layer (sections 24/25/26).

core.trading.mt5_bridge.MT5Bridge already speaks a broker-agnostic JSON
command/result protocol over Redis -- the MT5-specific code lives entirely
in mt5_agent/mt5_worker.py on the other end of that protocol. MT5Execution
below is a thin adapter renaming MT5Bridge's methods to this interface's
contract, so a future second broker only needs to implement these 7
methods without anything upstream (signal/scoring/risk) changing.
"""
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from .config import LIVE_TRADING

log = logging.getLogger(__name__)

STALE_PRICE_MAX_AGE_SEC = int(os.getenv("STALE_PRICE_MAX_AGE_SEC", "120"))
MAX_SLIPPAGE_PCT = float(os.getenv("MAX_SLIPPAGE_PCT", "0.15"))


class ExecutionInterface(ABC):
    """Every broker adapter (MT5, paper, future brokers) implements exactly
    this surface. Signal/risk/scoring code must depend only on this
    interface, never import MT5Bridge directly."""

    @abstractmethod
    async def get_market_data(self, symbol: str, timeframe: str, count: int) -> list: ...

    @abstractmethod
    async def get_account(self) -> dict: ...

    @abstractmethod
    async def place_order(self, trade_id: str, symbol: str, direction: str,
                           stop_loss: float, take_profit: float, volume: float,
                           order_type: str = "market", limit_price: float = 0) -> dict: ...

    @abstractmethod
    async def cancel_order(self, ticket) -> dict: ...

    @abstractmethod
    async def modify_stop(self, trade_id: str, symbol: str, new_sl: float, new_tp: float) -> dict: ...

    @abstractmethod
    async def close_position(self, trade_id: str, symbol: str, direction: str, volume: float) -> dict: ...

    @abstractmethod
    async def get_positions(self) -> dict: ...

    @abstractmethod
    async def get_tick(self, symbol: str) -> dict: ...

    @abstractmethod
    async def get_symbol_info(self, symbol: str) -> dict: ...


class MT5Execution(ExecutionInterface):
    """Adapts core.trading.mt5_bridge.MT5Bridge to ExecutionInterface."""

    def __init__(self, mt5_bridge):
        self._bridge = mt5_bridge

    async def get_market_data(self, symbol, timeframe, count):
        result = await self._bridge.get_rates(symbol, timeframe, count)
        return result.get("ohlcv") or []

    async def get_account(self) -> dict:
        return await self._bridge.get_account_info()

    async def place_order(self, trade_id, symbol, direction, stop_loss, take_profit,
                           volume, order_type="market", limit_price=0) -> dict:
        return await self._bridge.send_open(trade_id, symbol, direction, stop_loss,
                                             take_profit, volume, order_type, limit_price)

    async def cancel_order(self, ticket) -> dict:
        return await self._bridge.cancel_pending(ticket)

    async def modify_stop(self, trade_id, symbol, new_sl, new_tp) -> dict:
        return await self._bridge.modify_trade(trade_id, symbol, new_sl, new_tp)

    async def close_position(self, trade_id, symbol, direction, volume) -> dict:
        return await self._bridge.send_close(trade_id, symbol, direction, volume)

    async def get_positions(self) -> dict:
        return await self._bridge.get_open_positions()

    async def get_tick(self, symbol: str) -> dict:
        return await self._bridge.get_tick(symbol)

    async def get_symbol_info(self, symbol: str) -> dict:
        return await self._bridge.get_symbol_info(symbol)


class PaperExecution(ExecutionInterface):
    """Simulated broker for paper trading (spec section 24). Market-data
    reads are delegated to a REAL data source (typically an MT5Execution,
    since paper trading must use real prices) -- only order placement is
    simulated, with fills/PnL tracked in a separate Redis namespace from
    the live account so the two can never cross-contaminate. Uses the
    exact same signal logic, risk engine, SL/TP and execution rules as
    live mode (spec requirement) -- this class is the ONLY thing that
    changes when switching modes."""

    def __init__(self, market_data_source: ExecutionInterface, redis, starting_equity: float = 10_000.0):
        self._data = market_data_source
        self._redis = redis
        self._starting_equity = starting_equity
        self._key = "trading:paper:positions"
        self._equity_key = "trading:paper:equity"

    async def get_market_data(self, symbol, timeframe, count):
        return await self._data.get_market_data(symbol, timeframe, count)

    async def get_tick(self, symbol):
        return await self._data.get_tick(symbol)

    async def get_symbol_info(self, symbol):
        return await self._data.get_symbol_info(symbol)

    async def get_account(self) -> dict:
        raw = await self._redis.get(self._equity_key)
        equity = float(raw) if raw else self._starting_equity
        return {"equity": equity, "balance": equity, "currency": "PAPER"}

    async def place_order(self, trade_id, symbol, direction, stop_loss, take_profit,
                           volume, order_type="market", limit_price=0) -> dict:
        tick = await self.get_tick(symbol)
        fill_price = limit_price if order_type == "limit" else (tick.get("ask") or tick.get("bid") or 0)
        position = {
            "trade_id": trade_id, "symbol": symbol, "direction": direction,
            "entry": fill_price, "stop_loss": stop_loss, "take_profit": take_profit,
            "volume": volume, "order_type": order_type, "status": "open",
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }
        await self._redis.hset(self._key, trade_id, json.dumps(position))
        log.info("[PAPER] Opened %s %s @ %s", symbol, direction, fill_price)
        return {"ticket": trade_id, "price": fill_price, "paper": True}

    async def cancel_order(self, ticket) -> dict:
        await self._redis.hdel(self._key, str(ticket))
        return {"cancelled": True, "paper": True}

    async def modify_stop(self, trade_id, symbol, new_sl, new_tp) -> dict:
        raw = await self._redis.hget(self._key, trade_id)
        if not raw:
            return {"error": "position not found", "paper": True}
        pos = json.loads(raw)
        pos["stop_loss"], pos["take_profit"] = new_sl, new_tp
        await self._redis.hset(self._key, trade_id, json.dumps(pos))
        return {"modified": True, "paper": True}

    async def close_position(self, trade_id, symbol, direction, volume) -> dict:
        raw = await self._redis.hget(self._key, trade_id)
        if not raw:
            return {"error": "position not found", "paper": True}
        pos = json.loads(raw)
        tick = await self.get_tick(symbol)
        exit_price = (tick.get("bid") if direction == "long" else tick.get("ask")) or pos["entry"]
        pnl = ((exit_price - pos["entry"]) if direction == "long" else (pos["entry"] - exit_price)) * volume
        equity = float(await self._redis.get(self._equity_key) or self._starting_equity)
        await self._redis.set(self._equity_key, equity + pnl)
        await self._redis.hdel(self._key, trade_id)
        log.info("[PAPER] Closed %s @ %s, pnl=%.2f", symbol, exit_price, pnl)
        return {"price": exit_price, "pnl": pnl, "paper": True}

    async def get_positions(self) -> dict:
        raw = await self._redis.hgetall(self._key)
        return {"positions": [json.loads(v) for v in raw.values()]}


# ── Live safety (spec section 25) ──────────────────────────────────────────

class KillSwitch:
    """Redis-backed global kill switch. Separate from risk_manager's
    drawdown lock: this is a manual/emergency override that must be
    checked before EVERY place_order call regardless of what upstream
    signal/risk logic concluded."""
    KEY = "trading:kill_switch"

    def __init__(self, redis):
        self._redis = redis

    async def is_active(self) -> bool:
        return bool(await self._redis.get(self.KEY))

    async def trip(self, reason: str):
        await self._redis.set(self.KEY, reason)
        log.critical("KILL SWITCH TRIPPED: %s", reason)

    async def reset(self):
        await self._redis.delete(self.KEY)


def check_spread(tick: dict, max_spread_pct: float) -> tuple[bool, str | None]:
    bid, ask = tick.get("bid"), tick.get("ask")
    if not bid or not ask or bid <= 0:
        return False, "Ugyldig tick-data (bid/ask mangler)"
    spread_pct = (ask - bid) / bid * 100
    if spread_pct > max_spread_pct:
        return False, f"Spread {spread_pct:.3f}% over max {max_spread_pct:.3f}%"
    return True, None


def check_stale_price(tick: dict, max_age_sec: int = STALE_PRICE_MAX_AGE_SEC) -> tuple[bool, str | None]:
    ts = tick.get("time") or tick.get("ts")
    if not ts:
        return False, "Tick uden tidsstempel — kan ikke bekræfte friskhed"
    age = time.time() - (ts / 1000 if ts > 1e12 else ts)
    if age > max_age_sec:
        return False, f"Pris er {age:.0f}s gammel (max {max_age_sec}s) — muligvis stale feed"
    return True, None


def check_slippage(intended_price: float, fill_price: float,
                    max_pct: float = MAX_SLIPPAGE_PCT) -> tuple[bool, str | None]:
    if intended_price <= 0:
        return True, None
    slip_pct = abs(fill_price - intended_price) / intended_price * 100
    if slip_pct > max_pct:
        return False, f"Slippage {slip_pct:.3f}% over max {max_pct:.3f}%"
    return True, None


async def check_duplicate_order(redis, symbol: str, direction: str,
                                 dedupe_window_sec: int = 60) -> tuple[bool, str | None]:
    """Blocks firing two orders for the same symbol+direction within a
    short window -- e.g. a signal repeated across two overlapping scan
    cycles, or a retry after a slow/ambiguous broker response, must not
    silently double a position. Uses SET NX so the check-and-mark is a
    single atomic Redis operation, not a race-prone read-then-write."""
    key = f"trading:exec:recent_order:{symbol}:{direction}"
    was_set = await redis.set(key, "1", nx=True, ex=dedupe_window_sec)
    if not was_set:
        return False, f"Duplikat-beskyttelse: {symbol} {direction} order sendt for < {dedupe_window_sec}s siden"
    return True, None


def live_trading_gate() -> tuple[bool, str | None]:
    """Section 25: LIVE_TRADING must be explicitly 'true'; default false.
    Gates real order placement only -- paper trading runs regardless."""
    if not LIVE_TRADING:
        return False, ("LIVE_TRADING=false — kun paper trading er aktiv. "
                        "Sæt LIVE_TRADING=true eksplicit for at handle med rigtige penge.")
    return True, None


def get_execution(mt5_bridge, redis, force_paper: bool = False) -> ExecutionInterface:
    """Factory: returns the correct ExecutionInterface for the current mode.
    'Botten skal hellere misse en trade end sende en ugyldig ordre' —
    defaults to paper unless LIVE_TRADING=true is set explicitly."""
    mt5_exec = MT5Execution(mt5_bridge)
    if force_paper or not LIVE_TRADING:
        return PaperExecution(mt5_exec, redis)
    return mt5_exec
