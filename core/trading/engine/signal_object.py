from __future__ import annotations

"""
Standardized signal object (spec section 19) and no-trade result (section
20). Every strategy path in the engine funnels into one of these two
shapes so downstream consumers (Telegram formatting, trade journal,
backtester) have one contract to work against.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Signal:
    symbol: str
    profile: str                    # "forex" | "gold"
    direction: str                  # "LONG" | "SHORT"
    setup: list[str]                # e.g. ["liquidity_grab", "fvg", "structure_shift"]
    timeframe_bias: dict             # {"h4": "bullish", "h1": "bullish", "m15": "bullish"}
    entry: float
    stop_loss: float
    take_profit: float
    risk_reward: float
    confidence_score: float          # 0-100
    session: str
    reasoning: list[str]
    status: str = "VALID"            # "VALID" | "WATCHLIST"
    order_type: str = "market"       # "market" | "limit"
    limit_price: float | None = None
    take_profit_2: float | None = None
    take_profit_3: float | None = None
    partial_r: float | None = None
    regime: str = "UNCERTAIN"
    score_breakdown: dict = field(default_factory=dict)
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "profile": self.profile, "direction": self.direction,
            "setup": self.setup, "timeframe_bias": self.timeframe_bias,
            "entry": self.entry, "stop_loss": self.stop_loss, "take_profit": self.take_profit,
            "risk_reward": self.risk_reward, "confidence_score": self.confidence_score,
            "session": self.session, "reasoning": self.reasoning, "status": self.status,
            "order_type": self.order_type, "limit_price": self.limit_price,
            "take_profit_2": self.take_profit_2, "take_profit_3": self.take_profit_3,
            "partial_r": self.partial_r, "regime": self.regime,
            "score_breakdown": self.score_breakdown, "generated_at": self.generated_at,
        }


@dataclass
class NoTradeResult:
    symbol: str
    profile: str
    reasons: list[str]
    score: float = 0.0
    status: str = "NO_TRADE"
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "profile": self.profile, "status": self.status,
                "score": self.score, "reasons": self.reasons, "generated_at": self.generated_at}
