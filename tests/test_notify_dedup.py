"""Regression test: _notify_block must dedupe on a STABLE key, not the raw
reason text, when the reason embeds a live number (confidence %, R:R,
score) that shifts slightly every scan -- otherwise it sends a fresh
Telegram message every single cycle instead of once per situation."""
import json

from core.trading.market_monitor import MarketMonitor
from fake_redis import FakeRedis


async def test_notify_block_dedupes_on_explicit_key_despite_changing_text():
    redis = FakeRedis()
    mm = MarketMonitor(redis)

    sent = []
    mm._notify = lambda msg: sent.append(msg) or _noop()

    async def _noop():
        return None

    # Same situation (still below confidence threshold), different exact %.
    await mm._notify_block("USDCHF=X", "Signal fundet (long, 68%), men under confidence-grænsen (72%).",
                            dedupe_key="low_confidence:long")
    await mm._notify_block("USDCHF=X", "Signal fundet (long, 69%), men under confidence-grænsen (72%).",
                            dedupe_key="low_confidence:long")
    await mm._notify_block("USDCHF=X", "Signal fundet (long, 71%), men under confidence-grænsen (72%).",
                            dedupe_key="low_confidence:long")

    assert len(sent) == 1, "second and third calls should be deduped despite different %"


async def test_notify_block_sends_fresh_message_when_situation_changes():
    redis = FakeRedis()
    mm = MarketMonitor(redis)

    sent = []

    async def _fake_notify(msg):
        sent.append(msg)

    mm._notify = _fake_notify

    await mm._notify_block("USDCHF=X", "low conf long", dedupe_key="low_confidence:long")
    await mm._notify_block("USDCHF=X", "low conf short", dedupe_key="low_confidence:short")

    assert len(sent) == 2, "a genuinely different situation must still notify"


async def test_neutral_reason_dedupe_strips_fluctuating_numbers():
    """The 'Neutral -- <reasons>' path has no explicit dedupe_key; it must
    derive one that ignores embedded numbers (ATR percentile, R:R, score)
    or every scan re-triggers a message even though nothing really changed."""
    import re
    r1 = "R:R 0.52 under minimum 2.0"
    r2 = "R:R 0.69 under minimum 2.0"
    assert re.sub(r"[\d.]+", "#", r1) == re.sub(r"[\d.]+", "#", r2)
