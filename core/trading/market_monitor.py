"""
Market Monitor — the autonomous loop.

Scans the forex + stocks/indices watchlist on a fixed interval, scores
every symbol against the rulebook in signal_engine.py, and for any signal
that clears every hard rule (trend alignment, 3+ confluence, 1:2+ R:R,
active session): logs a position, sends a Telegram alert, and — if a
broker worker (MT4 or MT5) is online — executes the trade automatically.
"""
import asyncio
import json
import logging
from datetime import datetime, date

import redis.asyncio as aioredis

from .market_data import fetch_ohlcv, resample_4h
from .signal_engine import score_signal
from .position_manager import PositionManager
from .broker_bridge import BrokerBridge

log = logging.getLogger(__name__)

DEFAULT_FOREX  = ["XAUUSD=X"]
DEFAULT_STOCKS = []

MONITOR_INTERVAL  = 600     # 10 minutes between full scans
CONFIDENCE_THRESH = 0.68    # only publish/execute above this
SIGNAL_COOLDOWN   = 14400   # 4 hours — don't re-signal the same symbol+direction
MAX_DAILY_SIGNALS = 5
DATA_FAILURE_ALERT_AFTER = 3  # consecutive failed scans before we tell the user


class MarketMonitor:
    def __init__(self, redis: aioredis.Redis):
        self._redis    = redis
        self._running  = False
        self._last_signal: dict[str, dict] = {}
        self._data_fail_count: dict[str, int] = {}
        self.positions = PositionManager(redis)
        self.broker    = BrokerBridge(redis)

    # ── Public API ────────────────────────────────────────────────────────────

    async def run(self):
        self._running = True
        log.info("MarketMonitor started (interval=%ds, confidence>=%.0f%%)",
                 MONITOR_INTERVAL, CONFIDENCE_THRESH * 100)
        asyncio.create_task(self.positions.run())
        asyncio.create_task(self.broker.run())
        while self._running:
            try:
                await self._scan_all()
            except Exception as e:
                log.error("Scan error: %s", e)
            await asyncio.sleep(MONITOR_INTERVAL)

    def stop(self):
        self._running = False
        self.positions.stop()

    async def watchlist(self) -> dict:
        forex  = await self._redis.get("trading:watchlist:forex")
        stocks = await self._redis.get("trading:watchlist:stocks")
        return {
            "forex":  json.loads(forex) if forex else DEFAULT_FOREX,
            "stocks": json.loads(stocks) if stocks else DEFAULT_STOCKS,
        }

    async def _scan_all(self):
        wl = await self.watchlist()
        for symbol in wl["forex"]:
            await self._analyze(symbol, "forex")
        for symbol in wl["stocks"]:
            await self._analyze(symbol, "stocks")

    async def _analyze(self, symbol: str, market: str):
        ohlcv_1h = await fetch_ohlcv(symbol, interval="1h", range_="1mo")
        if not ohlcv_1h:
            await self._journal(symbol, market, None, published=False, reject_reason="ingen data")
            await self._track_data_failure(symbol)
            return
        self._data_fail_count[symbol] = 0

        ohlcv_4h = resample_4h(ohlcv_1h)
        ohlcv_1d = await fetch_ohlcv(symbol, interval="1d", range_="6mo")

        signal = score_signal(ohlcv_1h, ohlcv_4h, ohlcv_1d)

        if signal["confidence"] < CONFIDENCE_THRESH or not signal["checklist_ok"]:
            await self._journal(symbol, market, signal, published=False,
                                reject_reason=self._reject_reason(signal))
            return

        direction = signal["direction"]
        now = datetime.utcnow().timestamp()
        last = self._last_signal.get(symbol)
        if last and last["direction"] == direction and now - last["ts"] < SIGNAL_COOLDOWN:
            await self._journal(symbol, market, signal, published=False, reject_reason="cooldown")
            return

        if await self._daily_cap_reached():
            await self._journal(symbol, market, signal, published=False, reject_reason="dagligt max nået")
            return

        self._last_signal[symbol] = {"direction": direction, "ts": now}
        await self._publish(symbol, market, signal)

        trade_id = await self.positions.open_trade(
            symbol=symbol, market=market,
            direction=direction,
            entry=signal["price"],
            sl=signal["stop_loss"],
            tp=signal["take_profit"],
            partial_tp=signal["partial_tp"],
            signal_data=signal,
            source="auto",
        )

        try:
            broker_symbol = symbol  # workers apply their own broker-specific symbol mapping
            result = await self.broker.send_open(
                trade_id=trade_id,
                symbol=broker_symbol,
                direction=direction,
                stop_loss=signal["stop_loss"],
                take_profit=signal["take_profit"],
            )
            if "error" in result:
                log.warning("Broker execution failed: %s", result["error"])
            else:
                log.info("Order placed via %s: ticket=%s", result.get("broker"), result.get("ticket"))
        except Exception as e:
            log.warning("Broker execution error (non-fatal): %s", e)

        await self._notify_trade_open(trade_id, symbol, signal)
        await self._journal(symbol, market, signal, published=True)
        await self._increment_daily_count()

    async def _track_data_failure(self, symbol: str):
        """Alert the user if a symbol can't be fetched repeatedly, instead of
        silently never trading it (e.g. data source blocking/rate-limiting)."""
        count = self._data_fail_count.get(symbol, 0) + 1
        self._data_fail_count[symbol] = count
        if count == DATA_FAILURE_ALERT_AFTER:
            await self._redis.publish("supervisor:notifications", json.dumps({
                "message": (
                    f"⚠️ *Datafejl: {symbol}*\n"
                    f"Kunne ikke hente kursdata {count} scans i træk. "
                    f"Boten kan ikke analysere eller handle {symbol} før dette løses.\n"
                    f"Tjek evt. om datakilden blokerer serverens IP."
                ),
                "parse_mode": "Markdown",
                "task_id": "trading",
            }))

    @staticmethod
    def _reject_reason(signal: dict) -> str:
        cl = signal["checklist"]
        if not cl["active_session"]:
            return "uden for handelssession"
        if not cl["trend_aligned"]:
            return "timeframes ikke aligned"
        if not cl["confluence_3plus"]:
            return f"kun {signal['confluence']} confluence-faktorer"
        if not cl["rr_min_1_2"]:
            return f"R:R kun {signal['rr_ratio']}"
        return f"confidence {signal['confidence']*100:.0f}% under tærskel"

    # ── Notifications ────────────────────────────────────────────────────────

    async def _publish(self, symbol: str, market: str, signal: dict):
        cl = signal["checklist"]
        checklist_lines = "\n".join(
            f"{'✅' if v else '❌'} {k.replace('_', ' ')}" for k, v in cl.items()
        )
        msg = (
            f"📈 *SIGNAL: {symbol}* ({market})\n\n"
            f"Retning: *{signal['direction'].upper()}*\n"
            f"Confidence: `{signal['confidence']*100:.0f}%`\n"
            f"Confluence: `{signal['confluence']}` faktorer\n"
            f"Session: {signal['session']}\n"
            f"Setups: {', '.join(signal['setups']) or '—'}\n\n"
            f"Entry: `{signal['price']}`\n"
            f"Stop Loss: `{signal['stop_loss']}`\n"
            f"Take Profit: `{signal['take_profit']}`\n"
            f"Partial (1.5R): `{signal['partial_tp']}`\n"
            f"R:R = `1:{signal['rr_ratio']}`\n\n"
            f"*Checklist:*\n{checklist_lines}\n\n"
            f"*Begrundelse:*\n" + "\n".join(f"• {r}" for r in signal["reasons"][:6])
        )
        await self._redis.publish("supervisor:notifications", json.dumps({
            "message":    msg,
            "parse_mode": "Markdown",
            "task_id":    "trading_signal",
        }))
        await self._redis.publish("ws:events", json.dumps({
            "type": "trading_signal", "symbol": symbol, "direction": signal["direction"],
            "confidence": signal["confidence"], "ts": datetime.utcnow().isoformat(),
        }))

    async def _notify_trade_open(self, trade_id: str, symbol: str, signal: dict):
        msg = (
            f"🤖 *Auto-trade åbnet: {symbol}*\n"
            f"ID: `{trade_id}`\n"
            f"Retning: {signal['direction'].upper()} @ `{signal['price']}`\n"
            f"SL: `{signal['stop_loss']}` · TP: `{signal['take_profit']}`\n\n"
            f"_Se hvorfor: /why {trade_id}_"
        )
        await self._redis.publish("supervisor:notifications", json.dumps({
            "message": msg, "parse_mode": "Markdown", "task_id": trade_id,
        }))

    # ── Journal + daily cap ──────────────────────────────────────────────────

    async def _journal(self, symbol: str, market: str, signal: dict | None,
                       published: bool, reject_reason: str = ""):
        entry = {
            "symbol": symbol, "market": market, "published": published,
            "reject_reason": reject_reason, "ts": datetime.utcnow().isoformat(),
            "confidence": signal["confidence"] if signal else 0,
            "direction": signal["direction"] if signal else None,
        }
        await self._redis.lpush("trading:journal", json.dumps(entry))
        await self._redis.ltrim("trading:journal", 0, 499)

    async def _daily_cap_reached(self) -> bool:
        key = f"trading:daily_count:{date.today().isoformat()}"
        count = await self._redis.get(key)
        return int(count or 0) >= MAX_DAILY_SIGNALS

    async def _increment_daily_count(self):
        key = f"trading:daily_count:{date.today().isoformat()}"
        await self._redis.incr(key)
        await self._redis.expire(key, 90000)
