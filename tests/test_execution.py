import time

from core.trading.engine import execution
from fake_redis import FakeRedis


async def test_duplicate_order_blocked_within_window():
    redis = FakeRedis()
    ok1, _ = await execution.check_duplicate_order(redis, "EURUSD", "long", dedupe_window_sec=60)
    assert ok1 is True
    ok2, reason = await execution.check_duplicate_order(redis, "EURUSD", "long", dedupe_window_sec=60)
    assert ok2 is False
    assert "Duplikat" in reason


async def test_duplicate_order_allows_different_direction():
    redis = FakeRedis()
    ok1, _ = await execution.check_duplicate_order(redis, "EURUSD", "long")
    ok2, _ = await execution.check_duplicate_order(redis, "EURUSD", "short")
    assert ok1 is True and ok2 is True


async def test_duplicate_order_allows_different_symbol():
    redis = FakeRedis()
    ok1, _ = await execution.check_duplicate_order(redis, "EURUSD", "long")
    ok2, _ = await execution.check_duplicate_order(redis, "GBPUSD", "long")
    assert ok1 is True and ok2 is True


def test_spread_filter_blocks_wide_spread():
    tick = {"bid": 100.0, "ask": 100.5}
    ok, reason = execution.check_spread(tick, max_spread_pct=0.1)
    assert ok is False
    assert "Spread" in reason


def test_spread_filter_allows_tight_spread():
    tick = {"bid": 100.0, "ask": 100.01}
    ok, _ = execution.check_spread(tick, max_spread_pct=0.1)
    assert ok is True


def test_spread_filter_rejects_missing_tick_data():
    ok, reason = execution.check_spread({}, max_spread_pct=0.1)
    assert ok is False


def test_stale_price_detected():
    tick = {"bid": 100, "ask": 100.1, "time": time.time() - 300}
    ok, reason = execution.check_stale_price(tick, max_age_sec=120)
    assert ok is False
    assert "gammel" in reason


def test_fresh_price_passes():
    tick = {"bid": 100, "ask": 100.1, "time": time.time()}
    ok, _ = execution.check_stale_price(tick, max_age_sec=120)
    assert ok is True


def test_slippage_within_tolerance_passes():
    ok, _ = execution.check_slippage(intended_price=100.0, fill_price=100.05, max_pct=0.15)
    assert ok is True


def test_slippage_beyond_tolerance_blocked():
    ok, reason = execution.check_slippage(intended_price=100.0, fill_price=101.0, max_pct=0.15)
    assert ok is False
    assert "Slippage" in reason


def test_live_trading_gate_defaults_false():
    ok, reason = execution.live_trading_gate()
    assert ok is False
    assert "LIVE_TRADING" in reason


async def test_kill_switch_trip_and_reset():
    redis = FakeRedis()
    ks = execution.KillSwitch(redis)
    assert await ks.is_active() is False
    await ks.trip("manual emergency stop")
    assert await ks.is_active() is True
    await ks.reset()
    assert await ks.is_active() is False


def test_get_execution_defaults_to_paper_when_live_trading_disabled():
    redis = FakeRedis()
    ex = execution.get_execution(mt5_bridge=object(), redis=redis)
    assert isinstance(ex, execution.PaperExecution)
