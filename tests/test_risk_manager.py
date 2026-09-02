import pytest

from core.trading.risk_manager import RiskManager
from fake_redis import FakeRedis


def test_risk_amount_capped_by_max_loss_per_trade():
    rm = RiskManager(FakeRedis())
    # RISK_PER_TRADE_PCT default 1.0%, MAX_LOSS_PER_TRADE default 100.
    assert rm.risk_amount(5_000) == 50.0          # 1% of equity, under the cap
    assert rm.risk_amount(50_000) == 100.0        # 1% would be 500 -- capped at 100


def test_compute_volume_sizes_to_risk_amount():
    symbol_info = {
        "trade_tick_value": 1.0, "trade_tick_size": 0.0001, "trade_contract_size": 100_000,
        "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01,
    }
    # risk 50 currency units, SL 20 pips away -> value per unit per lot = 10000
    # -> loss per lot = 0.0020 * 10000 = 20 -> volume = 50 / 20 = 2.5 lots
    volume = RiskManager.compute_volume(risk_amount=50, sl_distance=0.0020, symbol_info=symbol_info)
    assert volume == pytest.approx(2.5, abs=1e-6)


def test_compute_volume_capped_by_available_margin():
    symbol_info = {
        "trade_tick_value": 1.0, "trade_tick_size": 0.0001, "trade_contract_size": 100_000,
        "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01,
    }
    # Same risk-based calc as above would want 2.5 lots, but margin only
    # supports a much smaller position at 1:30 leverage on $1,000 free margin.
    volume = RiskManager.compute_volume(
        risk_amount=50, sl_distance=0.0020, symbol_info=symbol_info,
        entry_price=1.10, margin_free=1_000, leverage=30,
    )
    assert volume < 2.5
    # margin_per_lot = (100_000 * 1.10) / 30 = 3,666.67; 80% of $1,000 free
    # margin / margin_per_lot = 0.218 lots, rounded down to the 0.01 step.
    assert volume == pytest.approx(0.21, abs=0.01)


def test_compute_volume_never_below_minimum():
    symbol_info = {
        "trade_tick_value": 1.0, "trade_tick_size": 0.0001, "trade_contract_size": 100_000,
        "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01,
    }
    volume = RiskManager.compute_volume(risk_amount=0.0001, sl_distance=0.0020, symbol_info=symbol_info)
    assert volume == 0.01


async def test_refresh_equity_locks_trading_on_daily_loss_breach():
    redis = FakeRedis()
    rm = RiskManager(redis)

    await rm.refresh_equity(equity=10_000, balance=10_000, currency="USD")
    can_trade, _ = await rm.check_can_trade()
    assert can_trade is True

    # 4% intraday loss -- breaches the default 3% daily-loss limit.
    status = await rm.refresh_equity(equity=9_600, balance=9_600, currency="USD")
    assert status["locked"] is True

    can_trade, reason = await rm.check_can_trade()
    assert can_trade is False
    assert "Daglig tab-grænse" in reason or "Risk-lock" in reason


async def test_lock_persists_until_explicit_unlock():
    redis = FakeRedis()
    rm = RiskManager(redis)
    await rm.refresh_equity(equity=10_000, balance=10_000)
    await rm.refresh_equity(equity=9_000, balance=9_000)   # 10% drawdown -> breaches 8% max

    can_trade, _ = await rm.check_can_trade()
    assert can_trade is False

    # Equity recovering on its own must NOT silently clear the lock.
    await rm.refresh_equity(equity=10_500, balance=10_500)
    can_trade, _ = await rm.check_can_trade()
    assert can_trade is False

    was_locked = await rm.unlock()
    assert was_locked is True
    can_trade, _ = await rm.check_can_trade()
    assert can_trade is True
