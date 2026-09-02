from __future__ import annotations

"""
Structured decision logging (spec section 30). Every setup detection,
rejection, entry, and score breakdown is logged in one consistent shape so
it can be replayed for debugging or later used as training data for the
ML layer (section 32/31).
"""
import json
import logging
from datetime import datetime, timezone

log = logging.getLogger("trading.decisions")


def log_decision(event: str, symbol: str, profile: str, payload: dict) -> dict:
    """event: 'setup_detected' | 'no_trade' | 'trade_entered' | 'sl_chosen'
    | 'tp_chosen' | 'score_breakdown'. Returns the record so callers can
    also persist it (see persist_decision) rather than only logging it."""
    record = {
        "event": event, "symbol": symbol, "profile": profile,
        "ts": datetime.now(timezone.utc).isoformat(), **payload,
    }
    log.info(json.dumps(record, default=str))
    return record


async def persist_decision(redis, record: dict, key: str = "trading:decision_log", max_len: int = 5000) -> None:
    """Redis-backed ring buffer of recent decisions, for /why-style
    introspection without grepping application logs."""
    await redis.lpush(key, json.dumps(record, default=str))
    await redis.ltrim(key, 0, max_len - 1)
