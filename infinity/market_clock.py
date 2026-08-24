"""NSE market session clock (spec 2.1, 4.6).

Every timestamp in this module is timezone-aware in Asia/Kolkata. Streamlit
Community Cloud and GitHub Actions both run in UTC, so naive datetimes are a
correctness bug, not a style issue -- there are none here.

The function that matters most is :func:`last_session_close`. It defines the
freshness boundary for the on-disk snapshot cache: a snapshot is fresh iff it
was captured at or after the most recently completed session close. That single
predicate covers nights, weekends, holidays and the 15:30 LIVE -> CLOSED flip.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

_HOLIDAY_FILE = Path(__file__).resolve().parent.parent / "data" / "nse_holidays.json"

KNOWN_LIMITATIONS = (
    "Muhurat (Diwali evening) special sessions are not modelled; the clock "
    "reports CLOSED during them.",
    "Pre-open (09:00-09:15) is reported as CLOSED, matching spec 2.1 which "
    "defines the session as 09:15-15:30.",
)


class SessionState(StrEnum):
    """Drives the spec 4.6 status badge and the resolver's fetch path."""

    LIVE = "LIVE"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class Holiday:
    day: date
    name: str
    confidence: str  # "fixed" | "estimated"


@dataclass(frozen=True)
class HolidayCalendar:
    holidays: dict[date, Holiday]
    verified: bool
    source: str

    def __contains__(self, d: date) -> bool:
        return d in self.holidays

    def name_for(self, d: date) -> str | None:
        h = self.holidays.get(d)
        return h.name if h else None

    @property
    def estimated_count(self) -> int:
        return sum(1 for h in self.holidays.values() if h.confidence == "estimated")


@lru_cache(maxsize=1)
def load_calendar(path: Path | None = None) -> HolidayCalendar:
    """Load and cache the NSE holiday calendar.

    A missing or unparseable file degrades to an empty (weekend-only) calendar
    rather than crashing -- but logs at ERROR, because the snapshot cache will
    behave incorrectly on holidays until it is fixed.
    """
    p = path or _HOLIDAY_FILE
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error(
            "Could not load NSE holiday calendar from %s (%s). Falling back to a "
            "weekend-only calendar -- the app will incorrectly report LIVE on "
            "trading holidays.",
            p,
            exc,
        )
        return HolidayCalendar(holidays={}, verified=False, source="missing")

    holidays: dict[date, Holiday] = {}
    for _year, entries in raw.get("years", {}).items():
        for e in entries:
            d = date.fromisoformat(e["date"])
            holidays[d] = Holiday(d, e.get("name", "Holiday"), e.get("confidence", "estimated"))

    cal = HolidayCalendar(
        holidays=holidays,
        verified=bool(raw.get("verified", False)),
        source=raw.get("source", "unknown"),
    )

    if not cal.verified:
        log.warning(
            "NSE holiday calendar is UNVERIFIED (%d entries, %d estimated). Confirm "
            "against the official NSE circular and set verified=true in %s. Until "
            "then, snapshot freshness may be wrong around holidays.",
            len(cal.holidays),
            cal.estimated_count,
            p.name,
        )
    return cal


def now_ist() -> datetime:
    """Current wall-clock time in IST. The only entry point for 'now'."""
    return datetime.now(IST)


def to_ist(ts: datetime) -> datetime:
    """Normalise any datetime to IST. Naive input is rejected, never assumed."""
    if ts.tzinfo is None:
        raise ValueError(
            f"Naive datetime {ts!r} rejected. Attach a timezone -- assuming local "
            "time is how UTC servers silently break market-hours logic."
        )
    return ts.astimezone(IST)


def is_trading_day(d: date) -> bool:
    """True for a weekday that is not an NSE holiday."""
    return d.weekday() < 5 and d not in load_calendar()


def session_state(ts: datetime | None = None) -> SessionState:
    """LIVE during Mon-Fri 09:15-15:30 IST on a non-holiday, else CLOSED."""
    t = to_ist(ts) if ts else now_ist()
    if is_trading_day(t.date()) and MARKET_OPEN <= t.time() <= MARKET_CLOSE:
        return SessionState.LIVE
    return SessionState.CLOSED


def is_market_open(ts: datetime | None = None) -> bool:
    return session_state(ts) is SessionState.LIVE


def previous_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def last_session_close(ts: datetime | None = None) -> datetime:
    """Timestamp of the most recently *completed* 15:30 IST session close.

    This is the snapshot-cache freshness boundary. Note the strict ``>``: at
    exactly 15:30 the session is still LIVE and today's close has not settled,
    so the boundary is still the previous session's.
    """
    t = to_ist(ts) if ts else now_ist()
    d = t.date()
    if is_trading_day(d) and t.time() > MARKET_CLOSE:
        return datetime.combine(d, MARKET_CLOSE, tzinfo=IST)
    return datetime.combine(previous_trading_day(d), MARKET_CLOSE, tzinfo=IST)


def current_session_open(ts: datetime | None = None) -> datetime | None:
    """Open timestamp of the in-progress session, or None when closed."""
    t = to_ist(ts) if ts else now_ist()
    if session_state(t) is not SessionState.LIVE:
        return None
    return datetime.combine(t.date(), MARKET_OPEN, tzinfo=IST)


def next_session_open(ts: datetime | None = None) -> datetime:
    """Open timestamp of the next session (today's, if it has not started)."""
    t = to_ist(ts) if ts else now_ist()
    d = t.date()
    if is_trading_day(d) and t.time() < MARKET_OPEN:
        return datetime.combine(d, MARKET_OPEN, tzinfo=IST)
    return datetime.combine(next_trading_day(d), MARKET_OPEN, tzinfo=IST)


@dataclass(frozen=True)
class MarketStatus:
    """Everything the 4.6 badge and the resolver need, computed once."""

    state: SessionState
    as_of: datetime
    last_close: datetime
    next_open: datetime
    holiday_name: str | None
    calendar_verified: bool

    @property
    def is_live(self) -> bool:
        return self.state is SessionState.LIVE

    @property
    def badge_label(self) -> str:
        return "LIVE MARKET" if self.is_live else "HISTORICAL DATA"

    @property
    def badge_icon(self) -> str:
        return "\U0001f7e2" if self.is_live else "\U0001f319"

    @property
    def detail(self) -> str:
        if self.is_live:
            return f"Session closes {MARKET_CLOSE.strftime('%H:%M')} IST"
        if self.holiday_name:
            return f"NSE holiday: {self.holiday_name}"
        return f"Settled to {self.last_close:%d %b %Y %H:%M} IST"


def market_status(ts: datetime | None = None) -> MarketStatus:
    t = to_ist(ts) if ts else now_ist()
    cal = load_calendar()
    return MarketStatus(
        state=session_state(t),
        as_of=t,
        last_close=last_session_close(t),
        next_open=next_session_open(t),
        holiday_name=cal.name_for(t.date()) if t.weekday() < 5 else None,
        calendar_verified=cal.verified,
    )
