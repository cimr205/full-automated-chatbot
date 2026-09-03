"""Regression test: backtest.WINDOW must produce enough H4 candles after
_resample_4h() for score_symbol() to clear its own >= 60 H4-candle gate.

Bug history: WINDOW was 200, _resample_4h groups 4 H1 candles into 1 H4
candle, so 200 H1 candles produced only 50 H4 candles -- every single
backtest window failed scoring.score_symbol()'s first gate
("Utilstrækkelig candle-historik") regardless of the actual market data,
making every backtest report 0 trades no matter what (live-reported:
"/backtest EURUSD=X" -> "0 trades, 0% win rate" over 5000 real candles).
"""
from core.trading.backtest import WINDOW
from core.trading.market_monitor import _resample_4h


def test_window_produces_enough_h4_candles_for_scoring_gate():
    MIN_H4_REQUIRED = 60   # core/trading/engine/scoring.py: len(ohlcv_htf) < 60 -> reject
    fake_h1 = [[i * 3_600_000, 1.0, 1.0, 1.0, 1.0, 1.0] for i in range(WINDOW)]
    h4 = _resample_4h(fake_h1)
    assert len(h4) >= MIN_H4_REQUIRED, (
        f"WINDOW={WINDOW} H1 candles only resample to {len(h4)} H4 candles, "
        f"below scoring's {MIN_H4_REQUIRED}-candle floor -- every backtest window "
        f"would be rejected before any real signal logic runs."
    )
