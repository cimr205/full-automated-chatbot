from __future__ import annotations

"""
Asian Range Sweep detection — the single most reliable gold (XAUUSD) intraday pattern.

Playbook:
  1. Asian session (00:00-08:00 CET) builds a range: a clear high and low.
  2. London Open (08:00-10:00 CET) sweeps ONE extreme — takes out stop orders
     sitting just below the Asian low (for a bullish trade) or above the high.
  3. Price reverses sharply back inside the range: this is the entry signal.
  4. SL: just below the sweep wick (tight — the sweep was the real bottom).
  5. TP: opposite side of the Asian range + 50% extension.

Why it works:
  Banks and institutions need to fill large orders. They engineer a liquidity
  grab (a fake move to sweep retail stops) before the real daily move begins.
  Gold does this almost every trading day during the London open.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

CET = ZoneInfo("Europe/Paris")

ASIAN_START_H  = 0    # 00:00 CET
ASIAN_END_H    = 8    # 08:00 CET (exclusive)
LONDON_START_H = 8    # London Open
LONDON_END_H   = 12   # Prime London session ends

# Minimum range size to be worth trading (filters tight/choppy overnight sessions)
MIN_RANGE_PIPS = 5.0   # gold pip ≈ $0.10 → 5 pips = $0.50 minimum range


def detect(ohlcv_15m: list, at: datetime | None = None) -> dict | None:
    """
    Given a list of 15m OHLCV candles [[ts_ms, o, h, l, c, v], ...],
    detect if today's Asian Range has been swept and reversed.

    Returns a setup dict on match, or None if no setup.
    """
    if not ohlcv_15m or len(ohlcv_15m) < 10:
        return None

    now_cet = (at or datetime.now(timezone.utc)).astimezone(CET)
    today   = now_cet.date()

    # Split candles into today's Asian vs London session
    asian_candles  = []
    london_candles = []
    for c in ohlcv_15m:
        dt_cet  = datetime.fromtimestamp(c[0] / 1000, tz=CET)
        if dt_cet.date() != today:
            continue
        h = dt_cet.hour
        if ASIAN_START_H <= h < ASIAN_END_H:
            asian_candles.append(c)
        elif LONDON_START_H <= h < LONDON_END_H:
            london_candles.append(c)

    if len(asian_candles) < 4:   # need at least 1h of Asian data
        return None
    if not london_candles:
        return None

    asian_high = max(c[2] for c in asian_candles)
    asian_low  = min(c[3] for c in asian_candles)
    range_size = asian_high - asian_low

    if range_size < MIN_RANGE_PIPS * 0.1:   # gold: $0.10 per pip
        return None

    london_low  = min(c[3] for c in london_candles)
    london_high = max(c[2] for c in london_candles)
    current_close = london_candles[-1][4]

    # ── Bullish setup: London swept below Asian low → price returned inside ──
    swept_low = london_low < asian_low
    if swept_low and current_close > asian_low:
        tp = asian_high + range_size * 0.5   # 1.5× range target above range high
        sl = london_low - range_size * 0.10  # tight: 10% of range below the sweep wick
        if tp <= current_close or sl >= current_close:
            return None
        rr = (tp - current_close) / max(current_close - sl, 1e-6)
        if rr < 2.0:
            return None
        return {
            "type":       "bullish_asian_sweep",
            "label":      f"Asian Range Sweep — Long (brudt {asian_low:.2f}, vendt)",
            "direction":  "long",
            "sl_level":   round(sl, 3),
            "tp_level":   round(tp, 3),
            "asian_high": asian_high,
            "asian_low":  asian_low,
            "sweep_low":  london_low,
            "range_size": range_size,
            "rr":         round(rr, 1),
        }

    # ── Bearish setup: London swept above Asian high → price returned inside ──
    swept_high = london_high > asian_high
    if swept_high and current_close < asian_high:
        tp = asian_low - range_size * 0.5    # 1.5× range target below range low
        sl = london_high + range_size * 0.10
        if tp >= current_close or sl <= current_close:
            return None
        rr = (current_close - tp) / max(sl - current_close, 1e-6)
        if rr < 2.0:
            return None
        return {
            "type":       "bearish_asian_sweep",
            "label":      f"Asian Range Sweep — Short (brudt {asian_high:.2f}, vendt)",
            "direction":  "short",
            "sl_level":   round(sl, 3),
            "tp_level":   round(tp, 3),
            "asian_high": asian_high,
            "asian_low":  asian_low,
            "sweep_high": london_high,
            "range_size": range_size,
            "rr":         round(rr, 1),
        }

    return None


def asian_range_summary(ohlcv_15m: list, at: datetime | None = None) -> dict:
    """Returns Asian range stats for display — used by /market command."""
    if not ohlcv_15m:
        return {}

    now_cet = (at or datetime.now(timezone.utc)).astimezone(CET)
    today   = now_cet.date()

    asian_candles = [
        c for c in ohlcv_15m
        if (dt := datetime.fromtimestamp(c[0] / 1000, tz=CET)).date() == today
        and ASIAN_START_H <= dt.hour < ASIAN_END_H
    ]
    if not asian_candles:
        return {}

    high = max(c[2] for c in asian_candles)
    low  = min(c[3] for c in asian_candles)
    return {
        "asian_high": high,
        "asian_low":  low,
        "range_size": round(high - low, 2),
        "candles":    len(asian_candles),
    }
