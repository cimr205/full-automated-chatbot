from __future__ import annotations

"""
Timezone-aware session engine (spec section 10).

Session windows are anchored to each market's own local exchange timezone
via zoneinfo and compared in UTC -- NOT to a fixed CET offset. The old
signal_engine.session_info() used fixed CET hour arithmetic, which drifts
by up to an hour during the ~2-3 weeks each year where EU and US
daylight-saving transitions don't land on the same date. Japan observes
no DST, so Asia/Tokyo is a stable, DST-free proxy for the Asian session.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TOKYO    = ZoneInfo("Asia/Tokyo")
LONDON   = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")

# Local-exchange-time session windows: (start_hour inclusive, end_hour exclusive).
ASIAN_LOCAL       = (0, 9)   # Tokyo 00:00-09:00 JST
LONDON_LOCAL      = (8, 17)  # London 08:00-17:00 local
LONDON_OPEN_LOCAL = (8, 9)   # first hour = highest-quality liquidity
NY_LOCAL          = (8, 17)  # New York 08:00-17:00 local
NY_PRIME_LOCAL    = (8, 11)  # NY open + early volatility window


def _in_local_window(at: datetime, tz: ZoneInfo, window: tuple[int, int]) -> bool:
    local = at.astimezone(tz)
    lo, hi = window
    return lo <= local.hour < hi


@dataclass(frozen=True)
class SessionInfo:
    active: tuple[str, ...]   # e.g. ("london",) or ("london", "new_york") during overlap
    name: str                 # human label, Danish (matches existing bot copy)
    tradeable: bool
    prime: bool                # highest-quality window for at least one active session
    overlap: bool

    def to_dict(self) -> dict:
        return {"active": list(self.active), "name": self.name,
                "tradeable": self.tradeable, "prime": self.prime, "overlap": self.overlap}


def current_session(at: datetime | None = None) -> SessionInfo:
    moment = at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    asian       = _in_local_window(moment, TOKYO, ASIAN_LOCAL)
    london      = _in_local_window(moment, LONDON, LONDON_LOCAL)
    london_open = _in_local_window(moment, LONDON, LONDON_OPEN_LOCAL)
    ny          = _in_local_window(moment, NEW_YORK, NY_LOCAL)
    ny_prime    = _in_local_window(moment, NEW_YORK, NY_PRIME_LOCAL)

    active  = tuple(n for n, on in (("asian", asian), ("london", london), ("new_york", ny)) if on)
    overlap = london and ny

    if overlap:
        name, prime = "London/NY Overlap", True
    elif london_open:
        name, prime = "London Open", True
    elif ny_prime:
        name, prime = "NY Prime", True
    elif london:
        name, prime = "London Session", False
    elif ny:
        name, prime = "NY Session", False
    elif asian:
        name, prime = "Asian Session", False
    else:
        name, prime = f"Lukket ({moment.astimezone(timezone.utc).hour:02d}:00 UTC)", False

    tradeable = bool(london or ny)   # Asian-only hours are not tradeable, matches prior behaviour
    return SessionInfo(active=active, name=name, tradeable=tradeable, prime=prime, overlap=bool(overlap))


def session_allowed(session: SessionInfo, profile) -> bool:
    """profile is a config.TradingProfile; checks its `sessions.allowed` list."""
    if session.overlap and "london_ny_overlap" in profile.sessions.allowed:
        return True
    return any(s in profile.sessions.allowed for s in session.active)


def session_is_high_priority(session: SessionInfo, profile) -> bool:
    if session.overlap and "london_ny_overlap" in profile.sessions.high_priority:
        return True
    return any(s in profile.sessions.high_priority for s in session.active)


def asian_range_window(ohlcv_intraday: list, at: datetime | None = None) -> dict:
    """Today's Asian-session high/low from intraday (e.g. 15m) candles, using
    the same Tokyo-anchored window as current_session(). Distinct from
    core.trading.asian_range.detect(), which additionally checks for a
    London-session sweep+reversal of this range -- this only returns the
    raw range."""
    moment = at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    today_tokyo = moment.astimezone(TOKYO).date()

    candles = []
    for c in ohlcv_intraday:
        dt = datetime.fromtimestamp(c[0] / 1000, tz=TOKYO)
        if dt.date() == today_tokyo and ASIAN_LOCAL[0] <= dt.hour < ASIAN_LOCAL[1]:
            candles.append(c)
    if not candles:
        return {}
    return {
        "high": max(c[2] for c in candles), "low": min(c[3] for c in candles),
        "range": max(c[2] for c in candles) - min(c[3] for c in candles),
        "candles": len(candles),
    }
