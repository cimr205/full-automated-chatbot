"""Regression tests for the paper/live execution switch: LIVE_TRADING must
default to false, MarketMonitor must wire PaperExecution by default, and
paper trades must be tagged so PositionManager never sends a real broker
order for them."""
import json

from core.trading.market_monitor import MarketMonitor
from core.trading.position_manager import PositionManager
from core.trading.engine import execution
from core.trading.engine.config import LIVE_TRADING
from fake_redis import FakeRedis


def test_live_trading_defaults_to_false():
    assert LIVE_TRADING is False


def test_market_monitor_defaults_to_paper_execution():
    mm = MarketMonitor(FakeRedis())
    assert mm._paper_mode is True
    assert isinstance(mm.execution, execution.PaperExecution)


def test_market_monitor_positions_still_get_real_market_data_bridge():
    """Paper mode simulates ORDERS, not market data -- price feeds must
    still come from the real MT5Bridge."""
    mm = MarketMonitor(FakeRedis())
    assert mm.positions._mt5 is mm.mt5


async def test_open_trade_stores_paper_flag():
    pm = PositionManager(FakeRedis())
    trade_id = await pm.open_trade(
        symbol="EURUSD", market="forex", direction="long",
        entry=1.1000, stop_loss=1.0950, take_profit=1.1100, paper=True,
    )
    trade = await pm.get_trade(trade_id)
    assert trade["paper"] is True


async def test_open_trade_defaults_to_not_paper():
    pm = PositionManager(FakeRedis())
    trade_id = await pm.open_trade(
        symbol="EURUSD", market="forex", direction="long",
        entry=1.1000, stop_loss=1.0950, take_profit=1.1100,
    )
    trade = await pm.get_trade(trade_id)
    assert trade["paper"] is False


async def test_paper_partial_close_does_not_touch_mt5():
    """A paper trade hitting its 1.5R partial level must simulate the
    partial close locally -- it must NOT call the (unset/None) MT5 bridge,
    since there's no real broker position behind a paper trade."""
    redis = FakeRedis()
    pm = PositionManager(redis)
    pm._mt5 = None   # simulate MT5 bridge being unavailable, as it would be for a pure paper run
    trade_id = await pm.open_trade(
        symbol="EURUSD", market="forex", direction="long",
        entry=1.1000, stop_loss=1.0950, take_profit=1.1150,
        partial_tp=1.1075, size=1.0, paper=True,
    )
    trade = await pm.get_trade(trade_id)
    # Price at the partial level -- should simulate a partial close without raising.
    await pm._evaluate_position(trade, price=1.1076)
    updated = await pm.get_trade(trade_id)
    assert updated["partial_taken"] is True
    assert updated["partial_closed_volume"] is not None
