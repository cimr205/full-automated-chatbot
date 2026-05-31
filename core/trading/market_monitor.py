"""
Market Monitor — continuously scans Forex and Stock/Index symbols,
generates high-confidence signals, and pushes Telegram alerts via Redis.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone, time as dtime

import httpx
import redis.asyncio as aioredis

from .signal_engine import score_signal, MIN_CONFIDENCE

log = logging.getLogger(__name__)

MONITOR_INTERVAL   = int(os.getenv("MONITOR_INTERVAL", "600"))   # 10 min default
CONFIDENCE_THRESH  = float(os.getenv("SIGNAL_CONFIDENCE", "0.68"))
SIGNAL_COOLDOWN    = int(os.getenv("SIGNAL_COOLDOWN", "14400"))   # 4h per symbol

# ── Default watchlists ────────────────────────────────────────────────────────

DEFAULT_FOREX = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "GBPJPY=X",
    "AUDUSD=X", "USDCHF=X", "USDCAD=X", "EURGBP=X",
]
DEFAULT_STOCKS = [
    "SPY", "QQQ", "NVDA", "AAPL", "MSFT",
    "^GSPC", "^NDX", "^DJI",
]

# Yahoo Finance interval mapping: (1h, 4h, 1d)
_YF_INTERVAL = {
    "1h": "1h",
    "4h": "1h",   # YF has no 4h; we resample 1h → 4h
    "1d": "1d",
}
_YF_PERIOD = {
    "1h": "60d",
    "1d": "2y",
}


class MarketMonitor:
    def __init__(self, redis: aioredis.Redis):
        self._redis  = redis
        self._running = False
        self._last_signal: dict[str, dict] = {}   # symbol → {direction, ts}

    # ── Public API ────────────────────────────────────────────────────────────

    async def run(self):
        self._running = True
        log.info("MarketMonitor started (interval=%ds, confidence≥%.0f%%)",
                 MONITOR_INTERVAL, CONFIDENCE_THRESH * 100)
        while self._running:
            try:
                await self._scan_all()
            except Exception as e:
                log.error("Monitor scan error: %s", e)
            await asyncio.sleep(MONITOR_INTERVAL)

    def stop(self):
        self._running = False

    # ── Scan loop ─────────────────────────────────────────────────────────────

    async def _scan_all(self):
        forex_raw  = await self._redis.get("trading:watchlist:forex")
        stocks_raw = await self._redis.get("trading:watchlist:stocks")

        forex_list  = forex_raw.split(",")  if forex_raw  else DEFAULT_FOREX
        stocks_list = stocks_raw.split(",") if stocks_raw else DEFAULT_STOCKS

        all_symbols = [(s.strip(), "forex")  for s in forex_list  if s.strip()] + \
                      [(s.strip(), "stock")  for s in stocks_list if s.strip()]

        for symbol, market in all_symbols:
            if not self._running:
                break
            try:
                await self._analyze(symbol, market)
            except Exception as e:
                log.warning("[%s] analyze error: %s", symbol, e)
            await asyncio.sleep(2)   # gentle rate limit

    async def _analyze(self, symbol: str, market: str):
        # Fetch 1h data (used as base + resampled to 4h)
        ohlcv_1h = await self._fetch(symbol, "1h")
        if not ohlcv_1h or len(ohlcv_1h) < 50:
            log.debug("[%s] Not enough 1h data", symbol)
            return

        ohlcv_4h = _resample_4h(ohlcv_1h)

        # Daily data for long-term trend
        ohlcv_1d = await self._fetch(symbol, "1d")

        signal = score_signal(ohlcv_1h, ohlcv_4h, ohlcv_1d)
        direction  = signal["direction"]
        confidence = signal["confidence"]

        if direction == "neutral" or confidence < CONFIDENCE_THRESH:
            log.debug("[%s] Weak signal: %s %.0f%%", symbol, direction, confidence * 100)
            return

        # Cooldown: don't repeat the same direction within SIGNAL_COOLDOWN seconds
        now = datetime.now(timezone.utc).timestamp()
        last = self._last_signal.get(symbol, {})
        if last.get("direction") == direction and now - last.get("ts", 0) < SIGNAL_COOLDOWN:
            log.debug("[%s] Cooldown active, skipping", symbol)
            return

        self._last_signal[symbol] = {"direction": direction, "ts": now}
        await self._publish(symbol, market, signal)

    # ── Data fetching ─────────────────────────────────────────────────────────

    async def _fetch(self, symbol: str, timeframe: str) -> list | None:
        period   = _YF_PERIOD.get(timeframe, "60d")
        interval = _YF_INTERVAL.get(timeframe, "1h")
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            f"?interval={interval}&range={period}&includePrePost=false"
        )
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept":     "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=20, headers=headers) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()

            result = data.get("chart", {}).get("result", [])
            if not result:
                return None
            r         = result[0]
            timestamps = r.get("timestamp", [])
            q          = r.get("indicators", {}).get("quote", [{}])[0]
            opens      = q.get("open",   [])
            highs      = q.get("high",   [])
            lows       = q.get("low",    [])
            closes     = q.get("close",  [])
            volumes    = q.get("volume", [])

            ohlcv = []
            for i, ts in enumerate(timestamps):
                try:
                    c = closes[i]
                    if c is None:
                        continue
                    ohlcv.append([
                        ts * 1000,
                        opens[i]   or c,
                        highs[i]   or c,
                        lows[i]    or c,
                        c,
                        volumes[i] or 0,
                    ])
                except (IndexError, TypeError):
                    continue
            return ohlcv if len(ohlcv) >= 10 else None
        except Exception as e:
            log.warning("[%s] fetch %s error: %s", symbol, timeframe, e)
            return None

    # ── Publish signal ────────────────────────────────────────────────────────

    async def _publish(self, symbol: str, market: str, signal: dict):
        direction  = signal["direction"]
        confidence = signal["confidence"]
        price      = signal["price"]
        sl         = signal.get("stop_loss",   0)
        tp         = signal.get("take_profit", 0)
        reasons    = signal.get("reasons", [])
        timeframes = signal.get("timeframes", 1)
        rsi_val    = signal.get("rsi", 50)
        vol_r      = signal.get("vol_ratio", 1.0)

        dir_emoji  = "📈 LONG / KØB" if direction == "long" else "📉 SHORT / SÆLG"
        market_tag = "💱 Forex" if market == "forex" else "📊 Aktier/Indeks"

        # Format prices sensibly
        def fmt(v): return f"{v:,.5f}" if v < 10 else f"{v:,.2f}"

        tf_label = {1: "1 tidsramme", 2: "2 tidsrammer", 3: "3 tidsrammer"}.get(timeframes, str(timeframes))

        message = (
            f"🚨 *HANDELSSIGNAL* — {market_tag}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"*{symbol}*   {dir_emoji}\n"
            f"Confidence: *{confidence:.0%}*  ({tf_label} bekræftet)\n\n"
            f"💰 Pris:         `{fmt(price)}`\n"
            f"🛑 Stop Loss:  `{fmt(sl)}`\n"
            f"🎯 Take Profit: `{fmt(tp)}`\n\n"
            f"📊 *Indikatorer:*\n"
            f"  RSI: {rsi_val:.1f}  |  Volumen: {vol_r:.1f}x\n\n"
            f"*Bekræftelser ({len(reasons)}):*\n"
            + "\n".join(f"  ✅ {r}" for r in reasons[:6]) +
            f"\n\n⚠️ _Svar 'ja' på Telegram for at bekræfte denne trade._"
        )

        payload = {
            "symbol":     symbol,
            "market":     market,
            "direction":  direction,
            "confidence": confidence,
            "price":      price,
            "stop_loss":  sl,
            "take_profit": tp,
            "reasons":    reasons,
            "message":    message,
            "ts":         datetime.utcnow().isoformat(),
        }

        # Push to Telegram via supervisor notifications channel
        await self._redis.publish("supervisor:notifications", json.dumps({
            "message":  message,
            "task_id":  f"signal_{symbol.replace('/', '_').replace('=', '')}",
            "parse_mode": "Markdown",
        }))

        # Store in signal history (last 200)
        key = "trading:signal_history"
        await self._redis.lpush(key, json.dumps(payload))
        await self._redis.ltrim(key, 0, 199)

        # Publish on trading channel (for dashboard WebSocket)
        await self._redis.publish("ws:events", json.dumps({
            "type": "trading_signal", **payload,
        }))

        log.info("Signal: %s %s %.0f%% @%s SL=%s TP=%s",
                 symbol, direction, confidence * 100, fmt(price), fmt(sl), fmt(tp))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resample_4h(ohlcv_1h: list) -> list:
    """Group 1h candles into 4h candles."""
    if not ohlcv_1h:
        return []
    result = []
    i = 0
    while i + 3 < len(ohlcv_1h):
        chunk = ohlcv_1h[i:i + 4]
        ts     = chunk[0][0]
        open_  = chunk[0][1]
        high   = max(c[2] for c in chunk)
        low    = min(c[3] for c in chunk)
        close  = chunk[-1][4]
        volume = sum(c[5] for c in chunk)
        result.append([ts, open_, high, low, close, volume])
        i += 4
    return result
