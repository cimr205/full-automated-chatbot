"""
Free market data via Yahoo Finance's public REST endpoint — no API key needed.
Used for both forex pairs (EURUSD=X style) and stocks/indices (SPY, ^GSPC, etc).
"""
import logging

import httpx

log = logging.getLogger(__name__)

YF_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"


async def fetch_ohlcv(symbol: str, interval: str = "1h", range_: str = "5d") -> dict | None:
    """Returns {open, high, low, close, volume} lists, oldest → newest, or None on failure."""
    url = f"{YF_BASE}/{symbol}"
    params = {"interval": interval, "range": range_}
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        result = data["chart"]["result"][0]
        quote = result["indicators"]["quote"][0]

        opens, highs, lows, closes, volumes = (
            quote.get("open", []), quote.get("high", []), quote.get("low", []),
            quote.get("close", []), quote.get("volume", []),
        )
        # Drop trailing/leading None candles (Yahoo sometimes pads incomplete bars)
        cleaned = [
            (o, h, l, c, v or 0)
            for o, h, l, c, v in zip(opens, highs, lows, closes, volumes)
            if None not in (o, h, l, c)
        ]
        if len(cleaned) < 10:
            return None
        o, h, l, c, v = map(list, zip(*cleaned))
        return {"open": o, "high": h, "low": l, "close": c, "volume": v}
    except Exception as e:
        log.warning("Market data fetch failed for %s: %s", symbol, e)
        return None


def resample_4h(ohlcv_1h: dict) -> dict | None:
    """Groups 1h candles into 4h candles (Yahoo has no native 4h interval)."""
    n = len(ohlcv_1h["close"])
    if n < 8:
        return None
    groups = n // 4
    if groups < 2:
        return None
    trim = n - groups * 4
    o, h, l, c, v = (ohlcv_1h[k][trim:] for k in ("open", "high", "low", "close", "volume"))

    out = {"open": [], "high": [], "low": [], "close": [], "volume": []}
    for i in range(groups):
        s = slice(i * 4, i * 4 + 4)
        out["open"].append(o[s][0])
        out["high"].append(max(h[s]))
        out["low"].append(min(l[s]))
        out["close"].append(c[s][-1])
        out["volume"].append(sum(v[s]))
    return out


async def get_last_price(symbol: str) -> float | None:
    data = await fetch_ohlcv(symbol, interval="1m", range_="1d")
    if not data or not data["close"]:
        data = await fetch_ohlcv(symbol, interval="1h", range_="1d")
    if not data or not data["close"]:
        return None
    return float(data["close"][-1])
