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

from .signal_engine import score_signal
from .position_manager import PositionManager
from .mt5_bridge import MT5Bridge
from .risk_manager import RiskManager
from . import reporting

log = logging.getLogger(__name__)

MONITOR_INTERVAL   = int(os.getenv("MONITOR_INTERVAL", "600"))   # 10 min default
CONFIDENCE_THRESH  = float(os.getenv("SIGNAL_CONFIDENCE", "0.68"))
SIGNAL_COOLDOWN    = int(os.getenv("SIGNAL_COOLDOWN", "14400"))   # 4h per symbol
CONFIRM_BAND       = float(os.getenv("SIGNAL_CONFIRM_BAND", "0.07"))   # borderline zone above the floor
PENDING_KEY_PREFIX = "trading:pending:"
PENDING_TTL        = MONITOR_INTERVAL * 2   # expire if not reconfirmed within ~2 cycles

# ── Default watchlists ────────────────────────────────────────────────────────

DEFAULT_FOREX = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "GBPJPY=X",
    "AUDUSD=X", "USDCHF=X", "USDCAD=X", "EURGBP=X",
    "EURJPY=X", "NZDUSD=X", "AUDJPY=X", "NZDJPY=X",
    "EURAUD=X", "GBPAUD=X", "AUDNZD=X", "CHFJPY=X", "CADJPY=X",
    "XAUUSD=X",
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
    def __init__(self, redis: aioredis.Redis, db=None):
        self._redis    = redis
        self._running  = False
        self._last_signal: dict[str, dict] = {}
        self.positions = PositionManager(redis)
        self.mt5       = MT5Bridge(redis)
        self.risk      = RiskManager(redis, db=db)

    # ── Public API ────────────────────────────────────────────────────────────

    async def run(self):
        self._running = True
        log.info("MarketMonitor started (interval=%ds, confidence≥%.0f%%)",
                 MONITOR_INTERVAL, CONFIDENCE_THRESH * 100)
        asyncio.create_task(self.positions.run())
        asyncio.create_task(self.mt5.run())
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
        await self._refresh_account()
        try:
            await reporting.maybe_send_daily_report(self._redis, self.positions, self.risk)
        except Exception as e:
            log.warning("Daily report check failed: %s", e)

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
        ohlcv_1d = await self._fetch(symbol, "1d")

        signal = score_signal(ohlcv_1h, ohlcv_4h, ohlcv_1d)

        # Always journal the scan (even if signal is rejected)
        await self._journal(symbol, market, signal, published=False)

        direction  = signal["direction"]
        confidence = signal["confidence"]

        if direction == "neutral":
            return

        # Pre-trade checklist — all hard checks must pass
        if not signal.get("checklist_ok"):
            checklist = signal.get("checklist", {})
            failed = [k for k, v in checklist.items() if not v]
            log.info("[%s] Checklist failed: %s", symbol, failed)
            return

        if confidence < CONFIDENCE_THRESH:
            log.debug("[%s] Confidence too low: %.0f%%", symbol, confidence * 100)
            return

        # Risk gate — never open a new trade while paused or locked
        can_trade, lock_reason = await self.risk.check_can_trade()
        if not can_trade:
            log.info("[%s] Skipped — %s", symbol, lock_reason)
            return

        # Borderline confidence — don't act on a single read. Require the
        # *next* scan to reproduce the same direction + setup before entering.
        setup_type = signal.get("setup_type")
        if not await self._confirmed(symbol, direction, setup_type, confidence):
            log.info("[%s] Borderline signal (%.0f%%) — waiting for re-confirmation", symbol, confidence * 100)
            return

        # Check daily signal cap (max 5 signals per day across all symbols)
        if await self._daily_cap_reached():
            log.info("Daily signal cap reached — skipping %s", symbol)
            return

        # Cooldown: don't repeat same direction within SIGNAL_COOLDOWN seconds
        now = datetime.now(timezone.utc).timestamp()
        last = self._last_signal.get(symbol, {})
        if last.get("direction") == direction and now - last.get("ts", 0) < SIGNAL_COOLDOWN:
            log.debug("[%s] Cooldown active, skipping", symbol)
            return

        self._last_signal[symbol] = {"direction": direction, "ts": now}
        await self._publish(symbol, market, signal)

        # Log position in Redis (always)
        trade_id = await self.positions.open_trade(
            symbol=symbol, market=market,
            direction=direction,
            entry=signal["price"],
            stop_loss=signal["stop_loss"],
            take_profit=signal["take_profit"],
            partial_tp=signal.get("partial_tp", 0),
            signal_data=signal,
            source="auto",
        )

        # Execute in MT5 if worker is online (optional — works without it)
        try:
            mt5_online = await self.mt5.ping()
            if mt5_online:
                volume = await self._sized_volume(symbol, signal["price"], signal["stop_loss"])
                mt5_result = await self.mt5.send_open(
                    trade_id=trade_id,
                    symbol=symbol,
                    direction=direction,
                    stop_loss=signal["stop_loss"],
                    take_profit=signal["take_profit"],
                    volume=volume or 0,
                )
                if "error" in mt5_result:
                    log.warning("MT5 execution failed: %s", mt5_result["error"])
                else:
                    log.info("MT5 order placed: ticket=%s vol=%s", mt5_result.get("ticket"), volume)
        except Exception as e:
            log.warning("MT5 execution error (non-fatal): %s", e)

        await self._notify_trade_open(trade_id, symbol, signal)
        await self._journal(symbol, market, signal, published=True)
        await self._increment_daily_count()

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
        partial_tp = signal.get("partial_tp",  0)
        rr         = signal.get("rr_ratio",    0)
        reasons    = signal.get("reasons",     [])
        setups     = signal.get("setups",      [])
        setup_type = signal.get("setup_type")
        session    = signal.get("session",     {})
        confluence = signal.get("confluence",  0)
        checklist  = signal.get("checklist",   {})
        timeframes = signal.get("timeframes",  1)
        rsi_val    = signal.get("rsi",  50)
        vol_r      = signal.get("vol_ratio", 1.0)

        dir_emoji  = "📈 LONG / KØB" if direction == "long" else "📉 SHORT / SÆLG"
        market_tag = "💱 Forex" if market == "forex" else "📊 Aktier/Indeks"
        sess_name  = session.get("name", "Ukendt")
        prime_tag  = " ⭐ PRIME" if session.get("prime") else ""

        def fmt(v):
            if v == 0: return "0"
            return f"{v:,.5f}" if abs(v) < 10 else f"{v:,.2f}"

        cl_labels = {
            "1_trend_aligned":    "Trend alignment",
            "2_confluence_3plus": f"3+ confluence ({confluence} faktorer)",
            "3_sl_logical":       "Logisk stop loss",
            "4_rr_min_1_2":       f"R:R ≥ 1:2  (faktisk {rr:.1f}:1)",
            "5_risk_1_2_pct":     "Max 1-2% risiko",
            "6_not_chasing":      "Ikke chasing",
            "7_active_session":   f"Aktiv session ({sess_name}{prime_tag})",
            "8_setup_identified": f"Setup: {setup_type or 'ingen'}",
        }
        cl_lines = [
            f"  {'✅' if checklist.get(k) else '❌'} {label}"
            for k, label in cl_labels.items()
        ]

        tf_label = {1: "1 tf", 2: "2 tf", 3: "3 tf"}.get(timeframes, f"{timeframes} tf")
        message = (
            f"🚨 *HANDELSSIGNAL* — {market_tag}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"*{symbol}*  {dir_emoji}\n"
            f"Confidence: *{confidence:.0%}*  |  {tf_label}  |  {confluence} faktorer\n\n"
            f"💰 Entry:          `{fmt(price)}`\n"
            f"🛑 Stop Loss:    `{fmt(sl)}`  _(1.5x ATR)_\n"
            f"🎯 Target:         `{fmt(tp)}`  _(R:R = {rr:.1f}:1)_\n"
            f"💛 Delvis profit:  `{fmt(partial_tp)}`  _(ved 1.5R → flyt SL til BE)_\n\n"
            f"*Pre-trade checklist:*\n" + "\n".join(cl_lines) +
            (f"\n\n*Setups:*\n" + "\n".join(f"  🔷 {s}" for s in setups) if setups else "") +
            f"\n\n*Confluence:*\n" + "\n".join(f"  • {r}" for r in reasons[:5]) +
            f"\n\n_RSI: {rsi_val:.1f}  |  Vol: {vol_r:.1f}x  |  {sess_name}_"
        )

        payload = {
            "symbol":      symbol,      "market":     market,
            "direction":   direction,   "confidence": confidence,
            "price":       price,       "stop_loss":  sl,
            "take_profit": tp,          "partial_tp": partial_tp,
            "rr_ratio":    rr,          "setup_type": setup_type,
            "session":     sess_name,   "confluence": confluence,
            "checklist":   checklist,   "reasons":    reasons,
            "message":     message,     "ts":         datetime.utcnow().isoformat(),
        }

        await self._redis.publish("supervisor:notifications", json.dumps({
            "message": message, "parse_mode": "Markdown",
            "task_id": f"signal_{symbol.replace('/', '_').replace('=', '')}",
        }))

        key = "trading:signal_history"
        await self._redis.lpush(key, json.dumps(payload))
        await self._redis.ltrim(key, 0, 499)
        await self._redis.publish("ws:events", json.dumps({"type": "trading_signal", **payload}))

        log.info("Signal: %s %s %.0f%% R:R=%.1f session=%s setup=%s",
                 symbol, direction, confidence * 100, rr, sess_name, setup_type or "none")

    async def _notify_trade_open(self, trade_id: str, symbol: str, signal: dict):
        direction  = signal["direction"]
        price      = signal["price"]
        sl         = signal["stop_loss"]
        tp         = signal["take_profit"]
        partial_tp = signal.get("partial_tp", 0)
        rr         = signal.get("rr_ratio", 0)
        setup      = signal.get("setup_type", "Signal-baseret")
        session    = signal.get("session", {}).get("name", "")
        reasons    = signal.get("reasons", [])[:4]

        def fmt(v): return f"{v:,.5f}" if abs(v) < 10 else f"{v:,.2f}"

        dir_emoji = "📈 LONG" if direction == "long" else "📉 SHORT"
        msg = (
            f"✅ *TRADE ÅBNET AUTOMATISK*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"*{symbol}*  {dir_emoji}\n"
            f"Trade ID: `{trade_id}`\n\n"
            f"💰 Entry:          `{fmt(price)}`\n"
            f"🛑 Stop Loss:    `{fmt(sl)}`\n"
            f"🎯 Take Profit:  `{fmt(tp)}`\n"
            f"💛 Delvis ved:   `{fmt(partial_tp)}` _(→ flyt SL til BE)_\n"
            f"R:R = *{rr:.1f}:1*\n\n"
            f"*Grundlag:*\n"
            f"  📐 Setup: {setup}\n"
            f"  🕐 Session: {session}\n" +
            "\n".join(f"  • {r}" for r in reasons) +
            f"\n\n_Systemet overvåger positionen automatisk._\n"
            f"_Stop: /close {trade_id}_"
        )
        await self._redis.publish("supervisor:notifications", json.dumps({
            "message": msg, "parse_mode": "Markdown",
            "task_id": f"trade_open_{trade_id}",
        }))

    async def _journal(self, symbol: str, market: str, signal: dict, published: bool):
        try:
            entry = {
                "symbol":       symbol,      "market":      market,
                "direction":    signal.get("direction"),
                "confidence":   signal.get("confidence", 0),
                "setup_type":   signal.get("setup_type"),
                "session":      signal.get("session", {}).get("name"),
                "confluence":   signal.get("confluence", 0),
                "rr_ratio":     signal.get("rr_ratio", 0),
                "price":        signal.get("price", 0),
                "stop_loss":    signal.get("stop_loss", 0),
                "take_profit":  signal.get("take_profit", 0),
                "checklist_ok": signal.get("checklist_ok"),
                "published":    published,
                "ts":           datetime.utcnow().isoformat(),
                "date":         datetime.utcnow().strftime("%Y-%m-%d"),
            }
            await self._redis.lpush("trading:journal", json.dumps(entry))
            await self._redis.ltrim("trading:journal", 0, 999)
        except Exception as e:
            log.warning("Journal write error: %s", e)

    async def _daily_cap_reached(self, cap: int = 5) -> bool:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        count = await self._redis.get(f"trading:daily_signals:{today}")
        return int(count or 0) >= cap

    async def _increment_daily_count(self):
        today = datetime.utcnow().strftime("%Y-%m-%d")
        key   = f"trading:daily_signals:{today}"
        await self._redis.incr(key)
        await self._redis.expire(key, 86400 * 2)

    # ── Risk / confirmation helpers ──────────────────────────────────────────

    async def _refresh_account(self):
        """Pull live equity/balance from MT5 once per scan cycle (no-op if offline)."""
        try:
            if not await self.mt5.ping():
                return
            info = await self.mt5.get_account_info()
            if "error" in info:
                return
            await self.risk.refresh_equity(info["equity"], info["balance"])
        except Exception as e:
            log.warning("Account refresh failed: %s", e)

    async def _confirmed(self, symbol: str, direction: str, setup_type: str | None,
                         confidence: float) -> bool:
        """
        High-confidence signals (clear of the borderline band) execute immediately.
        Borderline signals need the *next* cycle to reproduce the same
        direction + setup before they're allowed to trade.
        """
        key = f"{PENDING_KEY_PREFIX}{symbol}"
        if confidence >= CONFIDENCE_THRESH + CONFIRM_BAND:
            await self._redis.delete(key)
            return True

        raw = await self._redis.get(key)
        pending = json.loads(raw) if raw else None
        if pending and pending.get("direction") == direction and pending.get("setup_type") == setup_type:
            await self._redis.delete(key)
            return True

        await self._redis.set(
            key,
            json.dumps({"direction": direction, "setup_type": setup_type}),
            ex=PENDING_TTL,
        )
        return False

    async def _sized_volume(self, symbol: str, entry: float, stop_loss: float) -> float | None:
        """Equity-based lot size using the broker's real contract/tick data. None = use bridge default."""
        try:
            status = await self.risk.status()
            equity = status.get("equity") or 0
            if not equity:
                return None
            symbol_info = await self.mt5.get_symbol_info(symbol)
            if "error" in symbol_info:
                return None
            risk_amount = self.risk.risk_amount(equity)
            sl_distance = abs(entry - stop_loss)
            return self.risk.compute_volume(risk_amount, sl_distance, symbol_info)
        except Exception as e:
            log.warning("[%s] sizing failed, falling back to default lot: %s", symbol, e)
            return None


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
