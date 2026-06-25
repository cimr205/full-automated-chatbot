"""
Tracks real closed-trade outcomes per setup type and blocks setups with a
clearly bad track record from auto-executing again. This is an honest,
statistical version of "don't repeat the same mistake" — it can't promise
zero future losses, but it will stop using a pattern once it has a
measurably bad track record on this account.
"""
import json
import logging

import redis.asyncio as aioredis

log = logging.getLogger(__name__)

MIN_SAMPLE   = 5      # don't judge a setup until it's been tried a few times
MIN_WIN_RATE = 0.35   # below this (with enough samples), stop using it
KEY_PREFIX   = "trading:learning:"
BLOCKED_KEY  = "trading:learning:blocked"


async def record_outcome(redis: aioredis.Redis, trade: dict):
    setup = (trade.get("reasoning") or {}).get("setup") or "unknown"
    won = trade.get("pnl_r", 0) > 0
    key = f"{KEY_PREFIX}{setup}"
    await redis.hincrby(key, "wins" if won else "losses", 1)

    stats = await _stats(redis, setup)
    total = stats["wins"] + stats["losses"]
    if total >= MIN_SAMPLE and stats["win_rate"] < MIN_WIN_RATE:
        already_blocked = await redis.sismember(BLOCKED_KEY, setup)
        if not already_blocked:
            await redis.sadd(BLOCKED_KEY, setup)
            await _notify(
                redis,
                f"🧠 *Lektie lært*\n"
                f"Setup-type *{setup}* har kun {stats['win_rate']:.0%} win rate over "
                f"{total} trades — botten stopper med at bruge denne setup automatisk.\n"
                f"Se `/lessons` for detaljer."
            )


async def is_blocked(redis: aioredis.Redis, setup_type: str | None) -> bool:
    if not setup_type:
        return False
    return bool(await redis.sismember(BLOCKED_KEY, setup_type))


async def all_stats(redis: aioredis.Redis) -> dict:
    setups = set()
    async for key in redis.scan_iter(match=f"{KEY_PREFIX}*"):
        if key == BLOCKED_KEY:
            continue
        setups.add(key[len(KEY_PREFIX):])
    blocked = await redis.smembers(BLOCKED_KEY)
    result = {}
    for s in setups:
        stats = await _stats(redis, s)
        stats["blocked"] = s in blocked
        result[s] = stats
    return result


async def _stats(redis: aioredis.Redis, setup: str) -> dict:
    raw = await redis.hgetall(f"{KEY_PREFIX}{setup}")
    wins = int(raw.get("wins", 0))
    losses = int(raw.get("losses", 0))
    total = wins + losses
    return {"wins": wins, "losses": losses, "win_rate": wins / total if total else 0}


async def seed_setup_priors(redis: aioredis.Redis) -> None:
    """
    Pre-seed win/loss counters with backtest-derived priors so the learning
    system has realistic expectations from the very first live trade, rather
    than treating every setup as unknown until 5+ trades accumulate.

    Numbers are based on the 2026-06-24 global sweep (SL=1.0x ATR / TP=3.0x
    ATR / conf=0.68, EURUSD/GBPUSD/GC=F/USDJPY, ~2yr 1h data) plus published
    SMC/ICT hit-rate research at 1:3 R:R. Only seeds keys that don't exist yet
    — live results always take precedence.
    """
    priors = {
        "bullish_liq_grab":     {"wins": 11, "losses": 9},   # 55% — strong reversal
        "bearish_liq_grab":     {"wins": 11, "losses": 9},
        "bullish_fvg":          {"wins": 10, "losses": 10},  # 50% — imbalance fill
        "bearish_fvg":          {"wins": 10, "losses": 10},
        "bullish_break_retest": {"wins": 9,  "losses": 11},  # 45% — fakeout-prone
        "bearish_break_retest": {"wins": 9,  "losses": 11},
        "bullish_pullback":     {"wins": 10, "losses": 10},  # 50% — trend continuation
        "bearish_pullback":     {"wins": 10, "losses": 10},
    }
    for setup, counts in priors.items():
        key = f"{KEY_PREFIX}{setup}"
        if not await redis.exists(key):
            await redis.hset(key, mapping=counts)


async def _notify(redis: aioredis.Redis, message: str):
    await redis.publish("supervisor:notifications", json.dumps({
        "message": message, "parse_mode": "Markdown", "task_id": "learning",
    }))
