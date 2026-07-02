from __future__ import annotations

"""
Economic calendar filter — blocks trading 45 min before and after
any high-impact USD/Gold event (CPI, NFP, FOMC, PPI, Core PCE, etc.).

Uses the ForexFactory public JSON feed (no auth required).
Falls back to a safe "not blocked" if the feed is unreachable, so a
network hiccup never silently prevents all trading.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

CET = ZoneInfo("Europe/Paris")
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Gold-relevant high-impact event titles (partial match)
GOLD_EVENTS = {
    "non-farm", "nfp", "cpi", "consumer price",
    "fomc", "federal funds", "fed chair", "powell",
    "ppi", "producer price", "core pce", "pce",
    "gdp", "retail sales", "ism manufacturing",
    "initial jobless", "unemployment",
}

BLOCK_WINDOW_MIN = 45   # minutes each side of the event

_cache: dict = {"events": None, "fetched_at": None}
_CACHE_TTL = 3600   # re-fetch at most once per hour


async def is_blocked_by_news() -> tuple[bool, str]:
    """
    Returns (blocked, reason). Non-blocking: if the calendar can't be
    fetched, returns (False, "") so trading isn't prevented by a network error.
    """
    try:
        events = await _get_events()
        now    = datetime.now(timezone.utc)
        window = timedelta(minutes=BLOCK_WINDOW_MIN)

        for ev in events:
            ev_time = ev.get("_dt")
            if not ev_time:
                continue
            if abs((ev_time - now).total_seconds()) <= window.total_seconds():
                title = ev.get("title", "Ukendt event")
                delta_min = int((ev_time - now).total_seconds() / 60)
                direction = "om" if delta_min > 0 else "for"
                return True, (
                    f"Nyheds-filter: *{title}* {direction} {abs(delta_min)} min. "
                    f"Ingen trade 45 min ±. Prøver igen bagefter."
                )
    except Exception as e:
        log.warning("News filter error (failing open): %s", e)
    return False, ""


async def next_event_description() -> str:
    """Human-readable list of today's upcoming high-impact events for /market."""
    try:
        events = await _get_events()
        now = datetime.now(timezone.utc)
        upcoming = [
            ev for ev in events
            if ev.get("_dt") and ev["_dt"] > now
        ]
        if not upcoming:
            return "Ingen high-impact nyheder de næste 7 dage."
        lines = []
        for ev in upcoming[:5]:
            dt_cet = ev["_dt"].astimezone(CET)
            lines.append(
                f"  📅 {dt_cet.strftime('%a %d/%m %H:%M')} CET — "
                f"*{ev.get('title', '?')}*"
            )
        return "\n".join(lines)
    except Exception:
        return "Kalender ikke tilgængelig."


async def _get_events() -> list[dict]:
    now = datetime.now(timezone.utc)
    if (_cache["events"] is not None
            and _cache["fetched_at"]
            and (now - _cache["fetched_at"]).total_seconds() < _CACHE_TTL):
        return _cache["events"]

    async with httpx.AsyncClient(timeout=8) as client:
        resp = await client.get(FF_URL, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        raw = resp.json()

    events = []
    for item in raw:
        currency = (item.get("country") or item.get("currency") or "").upper()
        if currency != "USD":
            continue
        impact = (item.get("impact") or "").lower()
        if impact != "high":
            continue
        title_lower = (item.get("title") or "").lower()
        if not any(kw in title_lower for kw in GOLD_EVENTS):
            continue

        # Parse ISO date — FF uses America/New_York offset strings
        date_str = item.get("date") or ""
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            dt = dt.astimezone(timezone.utc)
        except Exception:
            continue
        item["_dt"] = dt
        events.append(item)

    _cache["events"]     = events
    _cache["fetched_at"] = now
    log.info("News filter: fetched %d high-impact USD events", len(events))
    return events
