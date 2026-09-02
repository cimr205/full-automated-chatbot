from __future__ import annotations

"""
Liquidity Grab engine (spec sections 2 & 6): promotes a confirmed
liquidity sweep (liquidity.detect_sweep) into an actionable directional
setup once a structural reversal (BOS/CHOCH) in the sweep's implied
direction follows it. A sweep alone is not a trade signal -- it needs the
structure event to confirm the reversal actually happened.
"""
from . import liquidity, market_structure

Candle = list


def detect(ohlcv: list[Candle], ohlcv_1h: list[Candle], ohlcv_intraday: list[Candle]) -> dict | None:
    swings = market_structure.find_swings(ohlcv_1h)
    levels = liquidity.gather_levels(ohlcv_1h, ohlcv_intraday, swings)

    bullish_sweep = liquidity.find_best_sweep(ohlcv, levels["sell_side_levels"], "sell_side")
    bearish_sweep = liquidity.find_best_sweep(ohlcv, levels["buy_side_levels"], "buy_side")

    if bullish_sweep and (not bearish_sweep or bullish_sweep["quality"] >= bearish_sweep["quality"]):
        sweep, direction = bullish_sweep, "long"
    elif bearish_sweep:
        sweep, direction = bearish_sweep, "short"
    else:
        return None

    structure_event = market_structure.detect_bos_choch(ohlcv)
    structure_confirms = (structure_event.get("event") in ("BOS", "CHOCH")
                           and structure_event.get("direction") == direction)

    return {
        "type": "bullish_liq_grab" if direction == "long" else "bearish_liq_grab",
        "direction": direction,
        "label": f"Liquidity grab @ {sweep['level_name']} ({sweep['level']:.5g}) — kvalitet {sweep['quality']}",
        "sweep": sweep, "structure_confirms": structure_confirms, "structure_event": structure_event,
    }
