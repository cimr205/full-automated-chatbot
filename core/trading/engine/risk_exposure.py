from __future__ import annotations

"""
Currency exposure / correlation protection (spec section 17). Three "safe
looking" separate Forex positions -- LONG EURUSD, SHORT USDCHF, LONG
GBPUSD -- can in practice all be the same underlying bet: short USD. This
tracks net directional exposure per tracked currency across all open
positions and blocks a new trade that would push any single currency's
exposure past a configured limit.

Exposure is measured in risk-units (one unit per open trade), not notional
dollar value: risk_manager.RiskManager.compute_volume already sizes every
trade to risk approximately the same % of equity, so counting trades is a
reasonable proxy for stacked directional risk without needing live tick
values here.

This module is additive to risk_manager.RiskManager (daily-loss/drawdown/
consistency remain its sole responsibility) -- it only adds the
correlation/position-count checks that didn't exist before.
"""
from .config import TRACKED_CURRENCIES, GOLD_SYMBOLS

_GOLD_ALIASES = {s.upper() for s in GOLD_SYMBOLS}


def parse_currency_pair(symbol: str) -> tuple[str, str] | None:
    original = symbol.upper()
    if original in _GOLD_ALIASES or "XAU" in original:
        return "XAU", "USD"
    s = original.replace("=X", "").replace("=F", "")
    if s in _GOLD_ALIASES or "XAU" in s:
        return "XAU", "USD"
    if len(s) == 6:
        base, quote = s[:3], s[3:]
        if base in TRACKED_CURRENCIES and quote in TRACKED_CURRENCIES:
            return base, quote
    return None


def currency_exposure(open_positions: list[dict]) -> dict[str, float]:
    """Net exposure per currency in risk-units: +1 per open trade net-long
    that currency, -1 per trade net-short it. A currency can appear on the
    base side of one pair and the quote side of another simultaneously --
    that's exactly the stacking this exists to catch."""
    exposure = {c: 0.0 for c in TRACKED_CURRENCIES}
    for pos in open_positions:
        if pos.get("status") not in ("open", "pending"):
            continue
        pair = parse_currency_pair(pos.get("symbol", ""))
        if not pair:
            continue
        base, quote = pair
        sign = 1.0 if pos.get("direction") == "long" else -1.0
        exposure[base] = exposure.get(base, 0.0) + sign
        exposure[quote] = exposure.get(quote, 0.0) - sign
    return exposure


def check_correlation(open_positions: list[dict], new_symbol: str, new_direction: str,
                       max_exposure_units: float = 3.0) -> tuple[bool, str | None]:
    """Would adding this trade push either leg's currency past
    max_exposure_units net risk-units? Returns (allowed, reason)."""
    pair = parse_currency_pair(new_symbol)
    if not pair:
        return True, None
    base, quote = pair
    sign = 1.0 if new_direction == "long" else -1.0

    exposure = currency_exposure(open_positions)
    projected_base = exposure.get(base, 0.0) + sign
    projected_quote = exposure.get(quote, 0.0) - sign

    if abs(projected_base) > max_exposure_units:
        return False, (f"Ny trade ville øge {base}-eksponering til {projected_base:+.1f} "
                        f"risk-enheder (max {max_exposure_units})")
    if abs(projected_quote) > max_exposure_units:
        return False, (f"Ny trade ville øge {quote}-eksponering til {projected_quote:+.1f} "
                        f"risk-enheder (max {max_exposure_units})")
    return True, None


def check_position_limits(open_positions: list[dict], new_symbol: str,
                           max_simultaneous: int = 5, max_per_symbol: int = 1) -> tuple[bool, str | None]:
    open_now = [p for p in open_positions if p.get("status") in ("open", "pending")]
    if len(open_now) >= max_simultaneous:
        return False, f"Max samtidige positioner nået ({max_simultaneous})"
    same_symbol = [p for p in open_now if p.get("symbol") == new_symbol]
    if len(same_symbol) >= max_per_symbol:
        return False, f"Allerede {len(same_symbol)} åben(e) position(er) i {new_symbol} (max {max_per_symbol})"
    return True, None


def check_xau_exposure(open_positions: list[dict], adding: bool,
                        max_positions: float = 2.0) -> tuple[bool, str | None]:
    """Gold-specific guard (spec section 14) -- separate from the general
    per-currency check because gold's volatility means even a small number
    of stacked gold positions carries outsized risk relative to the same
    count of forex positions."""
    gold_open = [p for p in open_positions
                 if p.get("status") in ("open", "pending") and p.get("symbol", "").upper() in _GOLD_ALIASES]
    count = len(gold_open) + (1 if adding else 0)
    if count > max_positions:
        return False, f"Max XAU-eksponering nået ({len(gold_open)} åbne, max {max_positions})"
    return True, None
