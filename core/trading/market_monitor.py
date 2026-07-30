from __future__ import annotations

"""
Market Monitor — continuously scans Forex and Stock/Index symbols,
generates high-confidence signals, and pushes Telegram alerts via Redis.
"""
import asyncio
import base64
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone, time as dtime

import redis.asyncio as aioredis

from .signal_engine import score_signal
from .position_manager import PositionManager
from .mt5_bridge import MT5Bridge
from .risk_manager import RiskManager
from . import reporting
from . import chart
from . import learning
from . import asian_range as _asian_range
from . import news_filter

log = logging.getLogger(__name__)

MONITOR_INTERVAL   = int(os.getenv("MONITOR_INTERVAL", "900"))    # 15 min — matches 15m timeframe
CONFIDENCE_THRESH  = float(os.getenv("SIGNAL_CONFIDENCE", "0.72")) # raised: we only want the best
SIGNAL_COOLDOWN    = int(os.getenv("SIGNAL_COOLDOWN", "86400"))    # 24h — one trade per day
CONFIRM_BAND       = float(os.getenv("SIGNAL_CONFIRM_BAND", "0.07"))
FIXED_LOT_SIZE     = float(os.getenv("TRADE_FIXED_LOT_SIZE", "0"))  # >0 = skip equity-based sizing, always use this lot
PENDING_KEY_PREFIX = "trading:pending:"
TUNED_KEY_PREFIX   = "trading:tuned_params:"   # written by core/trading/nightly_tune.py
PENDING_TTL        = MONITOR_INTERVAL * 2
DAILY_TRADE_CAP    = int(os.getenv("DAILY_TRADE_CAP", "1"))   # max qualifying trades per day

# ── Default watchlists ────────────────────────────────────────────────────────

# XAUUSD only — one market, master it.
# GC=F is the Yahoo Finance ticker for Gold futures (closest to XAUUSD spot).
# MT5 uses XAUUSD — see MT5_SYMBOL_MAP below.
DEFAULT_FOREX  = ["GC=F"]
DEFAULT_STOCKS = []

# Map Yahoo tickers to MT5 symbol names
MT5_SYMBOL_MAP = {
    "GC=F": "XAUUSD",
}

# Per-symbol parameter overrides derived from backtesting + known pair characteristics.
# Global defaults (from 2026-06-24 56-combo sweep across 5 symbols):
#   SL=1.0x ATR, TP=3.0x ATR, conf=0.68 → ~48% win rate, +0.93R/trade avg
#
# Pattern: tighter SL beats the global default on volatile/mean-reverting assets
# (GC=F sweep confirmed 0.75x → +0.80R/trade); trending pairs benefit from wider TP.
# GBPJPY requires higher confluence (4 vs 3) — "widow maker" pair, too many fakeouts
# at the standard 3-factor bar. Nothing here overrides risk management.
SYMBOL_OVERRIDES = {
    # Gold — actual 192-combo sweep: SL=0.75x beats 1.0x by +0.13R/trade
    # (35.9% win rate, +0.80R/trade), tested against the TP=3.0x that was
    # the global default at sweep time. atr_tp_mult pinned here explicitly:
    # the 2026-06-26 change that raised the *global* ATR_TP_MULT default to
    # 5.0 (to compensate for a much tighter 0.2x global SL) was silently
    # inherited by GC=F too, since this entry never set its own TP — that
    # produced a live 0.75x/5.0x combo (6.67:1 R:R) that was never actually
    # backtested together and got 3 real trades stopped out on noise on
    # 2026-07-28. Pin it to the pairing that was actually validated.
    # min_confluence_short=4: bearish setups noticeably underperform bullish
    # ones live (fvg 43% vs 60%, break_retest 31% vs 40%, pullback 4% vs 4%) —
    # consistent with gold trending up overall, making shorts the counter-trend
    # trade. Raise the bar for shorts specifically rather than gold as a whole.
    "GC=F":    {"atr_sl_mult": 0.75, "atr_tp_mult": 3.0, "min_confluence_short": 4},
    # GBP pairs — higher volatility, SMC setups hit TP faster → tighter SL, smaller TP
    "GBPUSD=X": {"atr_sl_mult": 0.85, "atr_tp_mult": 2.5},
    "GBPJPY=X": {"atr_sl_mult": 1.25, "atr_tp_mult": 3.5, "min_confluence": 4},
    "GBPAUD=X": {"atr_sl_mult": 0.85, "atr_tp_mult": 2.5},
    # JPY trending pairs — cleaner trends, let TP run further
    "USDJPY=X": {"atr_tp_mult": 3.5},
    "EURJPY=X": {"atr_tp_mult": 3.5},
    "AUDJPY=X": {"atr_tp_mult": 3.5},
    "CADJPY=X": {"atr_tp_mult": 3.5},
    # NZD pairs — lower liquidity, tighter SL to compensate for wider spreads
    "NZDUSD=X": {"atr_sl_mult": 0.85},
    "NZDJPY=X": {"atr_sl_mult": 0.85, "atr_tp_mult": 3.5},
    # Silver — same logic as gold (high intraday volatility, mean-reverting)
    "SI=F":     {"atr_sl_mult": 0.75},
}

# Candle counts requested from MT5 per timeframe (4h is resampled from 1h,
# not fetched directly — MT5's own H4 timeframe starts its bars at different
# clock boundaries than a clean 4x-H1 grouping).
_RATES_COUNT = {
    "15m": 500,
    "1h":  1000,
    "1d":  500,
}


class MarketMonitor:
    def __init__(self, redis: aioredis.Redis, db=None):
        self._redis    = redis
        # True from construction, not just once run() starts the periodic loop:
        # _scan_all()'s per-symbol loop checks this to bail out early on stop(),
        # but processes that intentionally never call run() (telegram-bot, which
        # only does on-demand /scan without duplicating the periodic loop — see
        # apps/telegram-bot/bot.py) still need on-demand scans to actually
        # iterate symbols instead of exiting on the very first one.
        self._running  = True
        self._last_signal: dict[str, dict] = {}
        self._last_snapshot: dict[str, dict] = {}   # for the per-cycle watchlist chart
        self._mt5_offline_since: float | None = None   # timestamp when MT5 first went offline
        self.positions = PositionManager(redis)
        self.mt5       = MT5Bridge(redis)
        self.positions._mt5 = self.mt5   # inject for auto-breakeven
        self.risk      = RiskManager(redis, db=db)

    # ── Public API ────────────────────────────────────────────────────────────

    async def run(self):
        self._running = True
        log.info("MarketMonitor started (interval=%ds, confidence≥%.0f%%)",
                 MONITOR_INTERVAL, CONFIDENCE_THRESH * 100)
        await learning.seed_setup_priors(self._redis)
        asyncio.create_task(self.positions.run())
        asyncio.create_task(self.mt5.run())
        asyncio.create_task(self._pending_orders_loop())
        # Give MT5Bridge a moment to connect before reconciling
        await asyncio.sleep(5)
        asyncio.create_task(self._reconcile_positions())
        while self._running:
            try:
                await self._scan_all()
            except Exception as e:
                log.error("Monitor scan error: %s", e)
                await self._notify(f"🔴 *Scan-loop fejl*: {e}")
            await asyncio.sleep(MONITOR_INTERVAL)

    def stop(self):
        self._running = False

    # ── Pending limit orders ─────────────────────────────────────────────────

    async def _pending_orders_loop(self):
        """Polls MT5 every 60s for pending limit orders that have filled or
        been cancelled. No auto-expiry — they sit until one of those happens
        or you /cancel them."""
        while self._running:
            try:
                await self._check_pending_orders()
            except Exception as e:
                log.warning("Pending-orders check error: %s", e)
            await asyncio.sleep(60)

    async def _check_pending_orders(self):
        pending = await self.positions.list_pending()
        if not pending:
            return
        for trade in pending:
            trade_id = trade["trade_id"]
            ticket_raw = await self._redis.hget("trading:mt5:tickets", trade_id)
            if not ticket_raw:
                continue
            ticket = json.loads(ticket_raw).get("ticket")
            if not ticket:
                continue
            result = await self.mt5.check_pending(ticket)
            status = result.get("status")
            if status == "filled":
                await self.positions.mark_filled(trade_id, result.get("price", trade["entry"]))
            elif status == "cancelled":
                await self.positions.cancel_pending(trade_id, reason="annulleret/udløbet i MT5")

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
                await self._notify_block(symbol, f"🔴 Fejl under scan: {e}")
            await asyncio.sleep(2)   # gentle rate limit
        # Watchlist chart used to auto-send every cycle here — removed,
        # it's a non-trade notification and contributed to message overload.
        # Available on demand via /chart instead (see _send_watchlist_chart).

    async def _analyze(self, symbol: str, market: str):
        """Acquire a distributed per-symbol lock before doing any real analysis.
        Without this, two overlapping calls (e.g. rapid repeated /scan presses,
        or the periodic loop and an on-demand /scan landing close together --
        possibly from different processes entirely, telegram-bot vs the api
        service) can each independently pass cooldown/daily-cap/etc. before
        either has updated that shared state, and both end up opening real
        duplicate positions on the same signal. A plain in-process asyncio.Lock
        wouldn't catch the cross-process case; this uses Redis so it's shared
        by whichever process gets there first. 30s TTL as a crash safety net
        so a dead process can never leave this stuck locked forever."""
        lock_key = f"trading:analyze_lock:{symbol}"
        got_lock = await self._redis.set(lock_key, "1", nx=True, ex=30)
        if not got_lock:
            log.info("[%s] Analyze already running elsewhere, skipping", symbol)
            return
        try:
            await self._analyze_locked(symbol, market)
        finally:
            await self._redis.delete(lock_key)

    async def _analyze_locked(self, symbol: str, market: str):
        # Fetch all timeframes: 15m (entry), 1h (structure), 4h + daily (bias)
        ohlcv_15m = await self._fetch(symbol, "15m")
        ohlcv_1h  = await self._fetch(symbol, "1h")
        if not ohlcv_1h or len(ohlcv_1h) < 50:
            log.debug("[%s] Not enough 1h data", symbol)
            await self._notify_block(symbol, "Kan ikke hente nok 1h-data fra MT5 lige nu.")
            return

        ohlcv_4h = _resample_4h(ohlcv_1h)
        ohlcv_1d = await self._fetch(symbol, "1d")

        # Nightly-tuned params (from core/trading/nightly_tune.py) take precedence
        # over the static SYMBOL_OVERRIDES table when present — they're refreshed
        # against this account's own recent XAUUSD data, SYMBOL_OVERRIDES is a
        # one-off 2026-06-24 sweep across a 5-symbol basket.
        overrides = {**SYMBOL_OVERRIDES.get(symbol, {}), **await self._tuned_overrides(symbol)}
        signal = score_signal(
            ohlcv_1h, ohlcv_4h, ohlcv_1d,
            ohlcv_15m=ohlcv_15m,
            min_confluence=overrides.get("min_confluence"),
            min_confluence_short=overrides.get("min_confluence_short"),
            atr_sl_mult=overrides.get("atr_sl_mult"),
            atr_tp_mult=overrides.get("atr_tp_mult"),
            min_rr=overrides.get("min_rr"),
        )

        # Annotate signal with MT5 symbol name (may differ from Yahoo ticker)
        mt5_sym = MT5_SYMBOL_MAP.get(symbol, symbol)
        signal["mt5_symbol"] = mt5_sym

        # Always journal the scan (even if signal is rejected)
        await self._journal(symbol, market, signal, published=False)

        # Snapshot for the per-cycle watchlist overview chart (all symbols, not just signals)
        self._last_snapshot[symbol] = {
            "closes":       [c[4] for c in ohlcv_1h[-50:]],
            "direction":    signal.get("direction", "neutral"),
            "confidence":   signal.get("confidence", 0),
            "rsi":          signal.get("rsi", 50),
            "checklist_ok": signal.get("checklist_ok", False),
        }

        direction  = signal["direction"]
        confidence = signal["confidence"]

        if direction == "neutral":
            reason = "; ".join(signal.get("reasons") or []) or "Ingen klar retning lige nu."
            await self._notify_block(symbol, f"Neutral — {reason}")
            return

        # Pre-trade checklist — all hard checks must pass
        if not signal.get("checklist_ok"):
            checklist = signal.get("checklist", {})
            failed = [k for k, v in checklist.items() if not v]
            log.info("[%s] Checklist failed: %s", symbol, failed)
            await self._notify_block(symbol, f"Signal fundet ({direction}, {confidence:.0%}), men checklist fejlede: {', '.join(failed)}")
            return

        conf_thresh = overrides.get("confidence_thresh", CONFIDENCE_THRESH)
        if confidence < conf_thresh:
            log.debug("[%s] Confidence too low: %.0f%% (need %.0f%%)", symbol, confidence * 100, conf_thresh * 100)
            await self._notify_block(symbol, f"Signal fundet ({direction}, {confidence:.0%}), men under confidence-grænsen ({conf_thresh:.0%}).")
            return

        # News filter — no trading 45 min around high-impact USD/Gold events
        news_blocked, news_reason = await news_filter.is_blocked_by_news()
        if news_blocked:
            log.info("[%s] News filter: %s", symbol, news_reason)
            await self._notify(f"📰 {news_reason}")
            return

        # Risk gate — never open a new trade while paused or locked
        can_trade, lock_reason = await self.risk.check_can_trade()
        if not can_trade:
            log.info("[%s] Skipped — %s", symbol, lock_reason)
            await self._notify_block(symbol, f"Signal fundet ({direction}, {confidence:.0%}), men risk-gate: {lock_reason}")
            return

        # Learning gate — setups with a clearly bad real track record are blocked
        # Checks per-symbol first (more specific), then global
        setup_type = signal.get("setup_type")
        if await learning.is_blocked(self._redis, setup_type, symbol=symbol):
            log.info("[%s] Setup '%s' is blocked by learning — skipping", symbol, setup_type)
            await self._notify_block(symbol, f"Signal fundet ({direction}, {confidence:.0%}), men setup '{setup_type}' er blokeret af learning (dårlig historisk win rate).")
            return

        # Borderline confidence — don't act on a single read. Require the
        # *next* scan to reproduce the same direction + setup before entering.
        if not await self._confirmed(symbol, direction, setup_type, confidence):
            log.info("[%s] Borderline signal (%.0f%%) — waiting for re-confirmation", symbol, confidence * 100)
            await self._notify_block(symbol, f"Borderline signal ({direction}, {confidence:.0%}) — venter på bekræftelse næste scan.")
            return

        # Daily cap: 1 perfect trade per day — no second-guessing once we're in
        if await self._daily_cap_reached():
            log.info("Daily cap reached (1 trade/day) — skipping %s", symbol)
            await self._notify_block(symbol, f"Signal fundet ({direction}, {confidence:.0%}), men dagens handel er allerede brugt (max {DAILY_TRADE_CAP}/dag).")
            return

        # Cooldown: don't repeat same direction within SIGNAL_COOLDOWN seconds
        now = datetime.now(timezone.utc).timestamp()
        last = self._last_signal.get(symbol, {})
        if last.get("direction") == direction and now - last.get("ts", 0) < SIGNAL_COOLDOWN:
            log.debug("[%s] Cooldown active, skipping", symbol)
            await self._notify_block(symbol, f"Signal fundet ({direction}, {confidence:.0%}), men cooldown aktiv efter sidste {direction}-signal.")
            return

        self._last_signal[symbol] = {"direction": direction, "ts": now}
        await self._publish(symbol, market, signal)

        order_type  = signal.get("order_type", "market")
        limit_price = signal.get("limit_price") or 0

        # MT5 execution happens FIRST. The local trade record and the
        # "trade opened" Telegram message only get created on confirmed
        # success — never claim a trade exists when MT5 never placed it.
        mt5_online = await self.mt5.ping()
        if not mt5_online:
            await self._handle_mt5_offline_signal(symbol, signal)
            return

        # MT5 came back online — clear the offline tracker and notify once
        if self._mt5_offline_since is not None:
            offline_mins = int((time.time() - self._mt5_offline_since) / 60)
            self._mt5_offline_since = None
            # Clear per-threshold alert keys so escalation resets on next outage
            for threshold in [10, 30, 60, 120]:
                await self._redis.delete(f"trading:mt5:offline_alert_{threshold}")
            await self._notify(
                f"✅ *MT5 Worker online igen*\n"
                f"Var offline i ca. {offline_mins} minutter. Handel genoptaget."
            )

        # Validate SL/TP are on the correct sides before sending anything to MT5
        entry = signal["price"]
        sl    = signal["stop_loss"]
        tp    = signal["take_profit"]
        if direction == "long" and not (sl < entry < tp):
            log.error("[%s] Invalid levels for LONG: entry=%s sl=%s tp=%s — skipping", symbol, entry, sl, tp)
            await self._notify_block(symbol, f"⚠️ Ugyldige SL/TP-niveauer for LONG (entry={entry}, sl={sl}, tp={tp}) — signal droppet, ikke sendt til MT5.")
            return
        if direction == "short" and not (tp < entry < sl):
            log.error("[%s] Invalid levels for SHORT: entry=%s sl=%s tp=%s — skipping", symbol, entry, sl, tp)
            await self._notify_block(symbol, f"⚠️ Ugyldige SL/TP-niveauer for SHORT (entry={entry}, sl={sl}, tp={tp}) — signal droppet, ikke sendt til MT5.")
            return
        if entry <= 0 or sl <= 0 or tp <= 0:
            log.error("[%s] Zero price level detected — skipping", symbol)
            await self._notify_block(symbol, "⚠️ Pris/SL/TP på 0 registreret — signal droppet, ikke sendt til MT5.")
            return

        trade_id  = f"trade_{uuid.uuid4().hex[:8]}"
        mt5_sym   = signal.get("mt5_symbol", symbol)   # XAUUSD in MT5, GC=F on Yahoo
        try:
            volume = await self._sized_volume(mt5_sym, signal["price"], signal["stop_loss"])
            if volume is None:
                log.warning("[%s] Volume sizing unavailable — using default lot", mt5_sym)
            mt5_result = await self.mt5.send_open(
                trade_id=trade_id,
                symbol=mt5_sym,
                direction=direction,
                stop_loss=signal["stop_loss"],
                take_profit=signal["take_profit"],
                volume=volume or 0,
                order_type=order_type,
                limit_price=limit_price,
            )
        except Exception as e:
            mt5_result = {"error": str(e)}

        if "error" in mt5_result:
            log.warning("MT5 execution failed: %s", mt5_result["error"])
            await self._notify_mt5_not_executed(
                symbol, signal, f"MT5 afviste ordren: {mt5_result['error']}"
            )
            await self._journal(symbol, market, signal, published=False)
            return

        log.info("MT5 order placed: ticket=%s vol=%s type=%s",
                 mt5_result.get("ticket"), volume, order_type)

        # Only now — confirmed real execution — record it and notify.
        await self.positions.open_trade(
            symbol=symbol, market=market,
            direction=direction,
            entry=signal["price"],
            stop_loss=signal["stop_loss"],
            take_profit=signal["take_profit"],
            partial_tp=signal.get("partial_tp", 0),
            size=volume or 0,
            signal_data=signal,
            source="auto",
            order_type=order_type,
            trade_id=trade_id,
        )
        await self._notify_trade_open(trade_id, symbol, signal)
        await self._journal(symbol, market, signal, published=True)
        await self._increment_daily_count()

    # ── Data fetching ─────────────────────────────────────────────────────────

    async def _fetch(self, symbol: str, timeframe: str) -> list | None:
        """Historical OHLCV straight from the broker via the MT5 Worker —
        Yahoo Finance blocked/rate-limited Railway's outbound IP (confirmed
        via 404s on every request), which silently starved every symbol of
        data and meant the scanner never found a signal to act on."""
        count = _RATES_COUNT.get(timeframe, 500)
        try:
            result = await self.mt5.get_rates(symbol, timeframe, count)
            if "error" in result:
                log.warning("[%s] MT5 get_rates %s error: %s", symbol, timeframe, result["error"])
                return None
            ohlcv = result.get("ohlcv")
            return ohlcv if ohlcv and len(ohlcv) >= 10 else None
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
        setup_label = signal.get("setup_label") or setup_type
        session    = signal.get("session",     {})
        confluence = signal.get("confluence",  0)
        checklist  = signal.get("checklist",   {})
        timeframes = signal.get("timeframes",  1)
        rsi_val           = signal.get("rsi",  50)
        vol_r             = signal.get("vol_ratio", 1.0)
        confluence_boost  = signal.get("confluence_boost", 0.0)
        confluence_labels = signal.get("confluence_labels", [])

        dir_emoji  = "📈 LONG / KØB" if direction == "long" else "📉 SHORT / SÆLG"
        sess_name  = session.get("name", "Ukendt")
        prime_tag  = " ⭐ PRIME" if session.get("prime") else ""
        mt5_sym    = signal.get("mt5_symbol", symbol)

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
            "8_setup_identified": f"Setup: {setup_label or 'ingen'}",
        }
        cl_lines = [
            f"  {'✅' if checklist.get(k) else '❌'} {label}"
            for k, label in cl_labels.items()
        ]

        # Gold confluence breakdown — shows exactly WHY we're taking this trade
        gold_conf_lines = "\n".join(f"  {lbl}" for lbl in confluence_labels) if confluence_labels else "  • Basis-indikatorer"
        conf_boost_str  = f" _(+{confluence_boost:.0%} boost fra {len(confluence_labels)}/7 faktorer)_" if confluence_boost > 0 else ""

        tf_label = {1: "1 tf", 2: "2 tf", 3: "3 tf"}.get(timeframes, f"{timeframes} tf")
        message = (
            f"🚨 *GULD SIGNAL* — XAUUSD\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"*{mt5_sym}*  {dir_emoji}\n"
            f"Confidence: *{confidence:.0%}*{conf_boost_str}\n"
            f"{tf_label}  |  {confluence} faktorer\n\n"
            f"💰 Entry:          `{fmt(price)}`\n"
            f"🛑 Stop Loss:    `{fmt(sl)}`\n"
            f"🎯 Target:         `{fmt(tp)}`  _(R:R = {rr:.1f}:1)_\n"
            f"💛 Delvis profit:  `{fmt(partial_tp)}`  _(→ flyt SL til BE)_\n\n"
            f"*🔍 Bekræftede faktorer ({len(confluence_labels)}/7):*\n{gold_conf_lines}\n\n"
            f"*Pre-trade checklist:*\n" + "\n".join(cl_lines) +
            (f"\n\n*Setup:*\n" + "\n".join(f"  🔷 {s}" for s in setups) if setups else "") +
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
        setup      = signal.get("setup_label") or signal.get("setup_type", "Signal-baseret")
        session    = signal.get("session", {}).get("name", "")
        reasons    = signal.get("reasons", [])[:4]

        def fmt(v): return f"{v:,.5f}" if abs(v) < 10 else f"{v:,.2f}"

        order_type = signal.get("order_type", "market")
        dir_emoji  = "📈 LONG" if direction == "long" else "📉 SHORT"
        if order_type == "limit":
            header     = "⏳ *LIMIT-ORDER LAGT*"
            entry_line = f"💰 Limit-niveau:   `{fmt(price)}` _(afventer udfyldelse)_\n"
            footer     = f"\n\n_Udfyldes automatisk hvis prisen rammer niveauet. Ingen udløb._\n_Annuller: /cancel {trade_id}_"
        else:
            header     = "✅ *TRADE ÅBNET AUTOMATISK*"
            entry_line = f"💰 Entry:          `{fmt(price)}`\n"
            footer     = f"\n\n_Systemet overvåger positionen automatisk._\n_Stop: /close {trade_id}_"

        msg = (
            f"{header}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"*{symbol}*  {dir_emoji}\n"
            f"Trade ID: `{trade_id}`\n\n"
            f"{entry_line}"
            f"🛑 Stop Loss:    `{fmt(sl)}`\n"
            f"🎯 Take Profit:  `{fmt(tp)}`\n"
            f"💛 Delvis ved:   `{fmt(partial_tp)}` _(→ flyt SL til BE)_\n"
            f"R:R = *{rr:.1f}:1*\n\n"
            f"*Grundlag:*\n"
            f"  📐 Setup: {setup}\n"
            f"  🕐 Session: {session}\n" +
            "\n".join(f"  • {r}" for r in reasons) +
            footer
        )
        await self._redis.publish("supervisor:notifications", json.dumps({
            "message": msg, "parse_mode": "Markdown",
            "task_id": f"trade_open_{trade_id}",
        }))

    async def _notify_mt5_not_executed(self, symbol: str, signal: dict, reason: str):
        """A signal qualified but no real broker order was placed — say so
        explicitly. Never let the bot claim a trade exists when it doesn't."""
        direction = signal.get("direction", "?").upper()
        msg = (
            f"⚠️ *SIGNAL FUNDET — IKKE UDFØRT*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"*{symbol}*  {direction}\n"
            f"{reason}\n\n"
            f"_Ingen trade er åbnet på din konto. Botten fortsætter med at scanne._"
        )
        await self._redis.publish("supervisor:notifications", json.dumps({
            "message": msg, "parse_mode": "Markdown",
            "task_id": f"mt5_not_executed_{symbol}",
        }))

    async def _handle_mt5_offline_signal(self, symbol: str, signal: dict):
        """Send escalating alerts when MT5 is offline so the user doesn't miss it."""
        now = time.time()
        if self._mt5_offline_since is None:
            self._mt5_offline_since = now
            # First detection — immediate alert
            await self._notify_mt5_not_executed(
                symbol, signal,
                "MT5 Worker er offline — ingen rigtig trade åbnet."
            )
            return

        offline_mins = (now - self._mt5_offline_since) / 60
        # Escalating alerts at 10, 30, 60, 120 minute marks (each fires once)
        for threshold in [10, 30, 60, 120]:
            if offline_mins >= threshold:
                alert_key = f"trading:mt5:offline_alert_{threshold}"
                if not await self._redis.get(alert_key):
                    await self._redis.set(alert_key, "1", ex=3600 * 6)
                    await self._notify(
                        f"⚠️ *MT5 Worker stadig offline*\n"
                        f"Det er nu *{int(offline_mins)} minutter* siden forbindelsen gik tabt.\n"
                        f"Handler udføres ikke. Tjek mt5_worker.py på din PC/VPS."
                    )

    async def _reconcile_positions(self):
        """Sync Redis position records with what MT5 actually has open.

        Runs once at startup (after MT5Bridge has had time to connect).
        Detects ghost positions (traded on MT5, missing from Redis after a crash)
        and stale records (in Redis but no longer on MT5 — manually closed).
        """
        try:
            if not await self.mt5.ping():
                log.info("Reconcile skipped — MT5 offline at startup")
                return

            mt5_data = await self.mt5.get_open_positions()
            if "error" in mt5_data:
                log.warning("Reconcile: could not get MT5 positions: %s", mt5_data["error"])
                return

            mt5_by_ticket = {p["ticket"]: p for p in mt5_data.get("positions", [])}
            redis_positions = await self.positions.list_open()

            # Build ticket → trade from Redis
            redis_by_ticket: dict[int, dict] = {}
            for trade in redis_positions:
                trade_id  = trade["trade_id"]
                ticket_raw = await self._redis.hget("trading:mt5:tickets", trade_id)
                if ticket_raw:
                    ticket = json.loads(ticket_raw).get("ticket")
                    if ticket:
                        redis_by_ticket[ticket] = trade

            discrepancies = 0

            # 1. Redis records that no longer exist on MT5 → mark closed.
            # NOTE: previously tried self.mt5.get_position_history(ticket) here
            # to pull the real close price/P&L (2026-07-29, commit 649e3bc),
            # but on this account's mt5linux/RPyC bridge, mt5.history_deals_get()
            # doesn't error — it hangs indefinitely, wedging the whole worker
            # process (confirmed live 2026-07-30: no command of any kind, not
            # even ping, got a response for 2+ minutes until mt5-agent was
            # restarted). That's worse than an inaccurate placeholder — a
            # hung worker means NO position monitoring at all, on whatever
            # else happens to be open at the time. Reverted to the placeholder
            # until a bridge that actually supports deal history is in place;
            # every closed_externally trade's true P&L still needs a manual
            # check in the MT5 terminal, same as before.
            for ticket, trade in redis_by_ticket.items():
                if ticket not in mt5_by_ticket:
                    discrepancies += 1
                    log.warning("Reconcile: trade %s (ticket %s) not on MT5 — marking closed",
                                trade["trade_id"], ticket)
                    closed = await self.positions.close_trade(
                        trade["trade_id"], trade["entry"], reason="closed_externally"
                    )
                    if closed:
                        await self._notify(
                            f"⚠️ *Position afstemning*\n"
                            f"`{trade['symbol']}` {trade['direction'].upper()} "
                            f"(ticket {ticket}) eksisterer ikke på MT5 — "
                            f"markeret som lukket. Tjek terminalen for den reelle lukkepris og P&L."
                        )

            # 2. MT5 positions we have no Redis record for → re-register
            redis_tickets = set(redis_by_ticket.keys())
            for ticket, pos in mt5_by_ticket.items():
                if ticket not in redis_tickets:
                    discrepancies += 1
                    log.warning("Reconcile: MT5 ticket %s (%s) not in Redis — re-registering",
                                ticket, pos["symbol"])
                    recovery_id = f"recovered_{ticket}"
                    sl_guess = pos["sl"] or pos["price_open"] * 0.995
                    tp_guess = pos["tp"] or pos["price_open"] * 1.01
                    await self.positions.open_trade(
                        symbol=pos["symbol"], market="forex",
                        direction=pos["direction"],
                        entry=pos["price_open"],
                        stop_loss=sl_guess,
                        take_profit=tp_guess,
                        source="recovered",
                        trade_id=recovery_id,
                    )
                    await self._redis.hset(
                        "trading:mt5:tickets", recovery_id,
                        json.dumps({"ticket": ticket})
                    )
                    await self._notify(
                        f"⚠️ *Position genfundet*\n"
                        f"MT5 ticket {ticket} ({pos['symbol']} {pos['direction'].upper()}) "
                        f"manglede i systemet — genregistreret som `{recovery_id}`.\n"
                        f"Opdater SL/TP manuelt med /close hvis nødvendigt."
                    )

            if discrepancies:
                log.info("Reconcile: %d uoverensstemmelser rettet", discrepancies)
            else:
                log.info("Reconcile: ingen uoverensstemmelser fundet (%d positioner OK)",
                         len(redis_by_ticket))

        except Exception as e:
            log.error("Position reconciliation fejlede: %s", e)

    async def _notify(self, message: str):
        await self._redis.publish("supervisor:notifications", json.dumps({
            "message": message, "parse_mode": "Markdown", "task_id": "market_monitor",
        }))

    async def _notify_block(self, symbol: str, reason: str):
        """Tell the user why a scan didn't lead to a trade. Debounced per
        symbol so an unchanged reason (still locked, still low confidence,
        still neutral) sends once, not every 15-min cycle — but any change
        in reason, or a fresh problem, always gets a fresh message."""
        key  = f"trading:last_block_reason:{symbol}"
        last = await self._redis.get(key)
        if last == reason:
            return
        await self._redis.set(key, reason, ex=86400)
        await self._notify(f"⏸️ *{symbol}*: {reason}")

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
                "checklist":    signal.get("checklist"),
                "reasons":      signal.get("reasons"),
                "tf_directions": signal.get("tf_directions"),
                "published":    published,
                "ts":           datetime.utcnow().isoformat(),
                "date":         datetime.utcnow().strftime("%Y-%m-%d"),
            }
            await self._redis.lpush("trading:journal", json.dumps(entry))
            await self._redis.ltrim("trading:journal", 0, 999)
        except Exception as e:
            log.warning("Journal write error: %s", e)

    async def _daily_cap_reached(self) -> bool:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        count = await self._redis.get(f"trading:daily_signals:{today}")
        return int(count or 0) >= DAILY_TRADE_CAP

    async def _increment_daily_count(self):
        today = datetime.utcnow().strftime("%Y-%m-%d")
        key   = f"trading:daily_signals:{today}"
        await self._redis.incr(key)
        await self._redis.expire(key, 86400 * 2)

    # ── Watchlist overview chart ─────────────────────────────────────────────

    async def _send_watchlist_chart(self):
        """Sends one PNG overview of the whole watchlist's latest reads, once per scan cycle."""
        if not self._last_snapshot:
            return
        try:
            loop = asyncio.get_event_loop()
            png_bytes = await loop.run_in_executor(
                None, chart.render_watchlist_overview, dict(self._last_snapshot)
            )
            if not png_bytes:
                return
            await self._redis.publish("trading:charts", json.dumps({
                "image_b64": base64.b64encode(png_bytes).decode("ascii"),
                "caption": f"📊 Watchlist-overblik — {datetime.now(timezone.utc).strftime('%H:%M UTC')}",
            }))
        except Exception as e:
            log.warning("Watchlist chart render/send failed: %s", e)

    # ── Risk / confirmation helpers ──────────────────────────────────────────

    async def _refresh_account(self):
        """Pull live equity/balance from MT5 once per scan cycle (no-op if offline)."""
        try:
            if not await self.mt5.ping():
                return
            info = await self.mt5.get_account_info()
            if "error" in info:
                return
            await self.risk.refresh_equity(info["equity"], info["balance"], info.get("currency", ""))
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

    async def _tuned_overrides(self, symbol: str) -> dict:
        """Latest nightly parameter-sweep result for this symbol, if any (see
        core/trading/nightly_tune.py). Empty dict if never tuned yet."""
        try:
            raw = await self._redis.hgetall(f"{TUNED_KEY_PREFIX}{symbol}")
        except Exception as e:
            log.warning("[%s] tuned-params lookup failed: %s", symbol, e)
            return {}
        if not raw:
            return {}
        out = {}
        for k in ("min_confluence", "atr_sl_mult", "atr_tp_mult", "min_rr", "confidence_thresh"):
            if k in raw:
                out[k] = float(raw[k])
        if "min_confluence" in out:
            out["min_confluence"] = int(out["min_confluence"])
        return out

    async def _sized_volume(self, symbol: str, entry: float, stop_loss: float) -> float | None:
        """Equity-based lot size using the broker's real contract/tick data. None = use bridge default."""
        if FIXED_LOT_SIZE:
            return FIXED_LOT_SIZE
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

            # Fresh margin/leverage (not the cached risk status) so sizing never
            # exceeds what the account can actually afford to open — risk_amount
            # alone only bounds the loss IF stopped out, not whether the position
            # is marginable at all (bites hard on low-leverage accounts, e.g.
            # 1:10 on metals, where a risk-sized lot can be un-affordable).
            margin_free = 0.0
            leverage = 0.0
            try:
                account_info = await self.mt5.get_account_info()
                if "error" not in account_info:
                    margin_free = float(account_info.get("margin_free") or 0)
                    leverage = float(account_info.get("leverage") or 0)
            except Exception as e:
                log.warning("[%s] margin lookup failed, sizing without margin cap: %s", symbol, e)

            return self.risk.compute_volume(
                risk_amount, sl_distance, symbol_info,
                entry_price=entry, margin_free=margin_free, leverage=leverage,
            )
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
