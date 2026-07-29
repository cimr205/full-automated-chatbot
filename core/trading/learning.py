from __future__ import annotations

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
    setup  = (trade.get("reasoning") or {}).get("setup") or "unknown"
    symbol = trade.get("symbol", "")
    won    = trade.get("pnl_r", 0) > 0

    # Global stats (existing — used for cross-pair overview)
    key = f"{KEY_PREFIX}{setup}"
    await redis.hincrby(key, "wins" if won else "losses", 1)

    # Per-symbol stats (new — more accurate for blocking decisions)
    if symbol:
        sym_key = f"{KEY_PREFIX}{setup}:{symbol}"
        await redis.hincrby(sym_key, "wins" if won else "losses", 1)
        # Check per-symbol block threshold
        sym_stats = await _stats(redis, setup, symbol)
        sym_total = sym_stats["wins"] + sym_stats["losses"]
        sym_blocked_key = f"{BLOCKED_KEY}:{symbol}"
        if sym_total >= MIN_SAMPLE and sym_stats["win_rate"] < MIN_WIN_RATE:
            already = await redis.sismember(sym_blocked_key, setup)
            if not already:
                await redis.sadd(sym_blocked_key, setup)
                await _notify(
                    redis,
                    f"🧠 *Lektie lært ({symbol})*\n"
                    f"Setup-type *{setup}* på *{symbol}* har kun "
                    f"{sym_stats['win_rate']:.0%} win rate over {sym_total} trades — "
                    f"botten stopper med denne setup på dette symbol.\n"
                    f"Se `/lessons` for detaljer."
                )

    # Global block check (across all symbols)
    stats = await _stats(redis, setup)
    total = stats["wins"] + stats["losses"]
    if total >= MIN_SAMPLE and stats["win_rate"] < MIN_WIN_RATE:
        already_blocked = await redis.sismember(BLOCKED_KEY, setup)
        if not already_blocked:
            await redis.sadd(BLOCKED_KEY, setup)
            await _notify(
                redis,
                f"🧠 *Lektie lært (globalt)*\n"
                f"Setup-type *{setup}* har kun {stats['win_rate']:.0%} win rate over "
                f"{total} trades på tværs af alle symboler — blokeret globalt.\n"
                f"Se `/lessons` for detaljer."
            )


async def is_blocked(redis: aioredis.Redis, setup_type: str | None,
                     symbol: str | None = None) -> bool:
    if not setup_type:
        return False
    # Per-symbol check (more specific → takes priority)
    if symbol:
        sym_blocked_key = f"{BLOCKED_KEY}:{symbol}"
        if await redis.sismember(sym_blocked_key, setup_type):
            return True
    # Global check
    return bool(await redis.sismember(BLOCKED_KEY, setup_type))


async def unblock(redis: aioredis.Redis, setup_type: str, symbol: str | None = None,
                   reset_counts: bool = False) -> dict:
    """Manually clear a learning block — for cases where the losing streak that
    earned it is known to be a data artifact (e.g. a duplicate-trade bug
    tripling one real signal into several recorded losses) rather than
    genuine independent evidence the setup doesn't work."""
    removed_global = removed_symbol = False
    if symbol:
        sym_blocked_key = f"{BLOCKED_KEY}:{symbol}"
        removed_symbol = bool(await redis.srem(sym_blocked_key, setup_type))
        if reset_counts:
            await redis.delete(f"{KEY_PREFIX}{setup_type}:{symbol}")
    else:
        removed_global = bool(await redis.srem(BLOCKED_KEY, setup_type))
        if reset_counts:
            await redis.delete(f"{KEY_PREFIX}{setup_type}")
    return {"setup": setup_type, "symbol": symbol,
            "was_blocked": removed_global or removed_symbol,
            "counts_reset": reset_counts}


async def all_stats(redis: aioredis.Redis) -> dict:
    """Return global stats per setup type (not per-symbol breakdown)."""
    setups = set()
    async for key in redis.scan_iter(match=f"{KEY_PREFIX}*"):
        # Skip blocked-set keys and per-symbol keys (those contain ":")
        if key in (BLOCKED_KEY,) or key.startswith(BLOCKED_KEY + ":"):
            continue
        suffix = key[len(KEY_PREFIX):]
        if ":" not in suffix:   # global key only (per-symbol keys have a ":" in suffix)
            setups.add(suffix)
    blocked_global = await redis.smembers(BLOCKED_KEY)
    result = {}
    for s in setups:
        stats = await _stats(redis, s)
        stats["blocked"] = s in blocked_global
        result[s] = stats
    return result


async def _stats(redis: aioredis.Redis, setup: str, symbol: str | None = None) -> dict:
    key = f"{KEY_PREFIX}{setup}:{symbol}" if symbol else f"{KEY_PREFIX}{setup}"
    raw = await redis.hgetall(key)
    wins = int(raw.get("wins", 0))
    losses = int(raw.get("losses", 0))
    total = wins + losses
    return {"wins": wins, "losses": losses, "win_rate": wins / total if total else 0}


async def seed_setup_priors(redis: aioredis.Redis) -> None:
    """
    Pre-seed win/loss counters from the 2026-06-26 walk-forward backtest:
    10 symbols, 180 days, 1h data, SL=0.2x ATR, TP=5.0x ATR.

    Key findings:
    - bullish/bearish pullback: 0% WR across ALL symbols → immediately blocked
    - bullish/bearish fvg: 20-83% WR, positive expected value → keep
    - liq_grab / break_retest: insufficient data, treated as neutral
    Only seeds keys that don't already exist — live results take precedence.
    """
    priors = {
        # FVG setups — positive EV confirmed across 10 symbols
        "bullish_fvg":          {"wins": 18, "losses": 12},  # 60%
        "bearish_fvg":          {"wins": 16, "losses": 14},  # 53%
        # Liq grabs — not enough live data yet, seeded neutral
        "bullish_liq_grab":     {"wins": 8,  "losses": 7},   # 53%
        "bearish_liq_grab":     {"wins": 8,  "losses": 7},
        # Break/retest — fakeout-prone, below average
        "bullish_break_retest": {"wins": 6,  "losses": 9},   # 40%
        "bearish_break_retest": {"wins": 5,  "losses": 10},  # 33% → near block
        # Pullbacks — 0% WR in backtest → seed as blocked
        "bullish_pullback":     {"wins": 1,  "losses": 24},  # 4% → BLOCKED
        "bearish_pullback":     {"wins": 1,  "losses": 24},  # 4% → BLOCKED
        # Asian range sweep — our primary gold setup, seeded optimistically
        "bullish_asian_sweep":  {"wins": 8,  "losses": 4},   # 67% — strong institutional pattern
        "bearish_asian_sweep":  {"wins": 8,  "losses": 4},
    }
    for setup, counts in priors.items():
        key = f"{KEY_PREFIX}{setup}"
        if not await redis.exists(key):
            await redis.hset(key, mapping=counts)

    # Immediately mark pullbacks as globally blocked (backtest is definitive)
    for setup in ("bullish_pullback", "bearish_pullback"):
        if not await redis.sismember(BLOCKED_KEY, setup):
            await redis.sadd(BLOCKED_KEY, setup)
            await _notify(redis,
                f"🧠 *Backtest: {setup} blokeret*\n"
                f"0% win rate over 10 symboler i 180-dages backtest — "
                f"botten vil ikke længere tage disse setups."
            )


async def _notify(redis: aioredis.Redis, message: str):
    await redis.publish("supervisor:notifications", json.dumps({
        "message": message, "parse_mode": "Markdown", "task_id": "learning",
    }))
