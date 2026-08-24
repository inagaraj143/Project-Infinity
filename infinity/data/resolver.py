"""Session-aware data resolver -- the LIVE / CLOSED fetch policy.

    LIVE  (Mon-Fri 09:15-15:30 IST, non-holiday)
        memory cache (TTL) -> provider -> write snapshot

    CLOSED (nights, weekends, holidays)
        fresh snapshot?  -> serve it, zero network calls
        stale / absent?  -> ONE fetch -> write snapshot -> serve
                            (in the hosted build this branch should never run;
                             the EOD workflow pre-populates the snapshots)

"Fresh" means ``captured_at >= last_session_close()``. Wall-clock TTLs are not
used on the closed path -- settled candles do not change, so a Friday-evening
snapshot is still correct on Sunday night and a TTL would only cause pointless
refetching.
"""

from __future__ import annotations

import logging
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum

import pandas as pd

from infinity.config import DAILY_CACHE_TTL_SEC, INTRADAY_CACHE_TTL_SEC
from infinity.data.models import Interval, Snapshot, Source
from infinity.data.providers.base import DataProvider, ProviderError, TokenExpired
from infinity.data.snapshot_store import read_if_fresh, write_bars
from infinity.market_clock import SessionState, last_session_close, now_ist, session_state

log = logging.getLogger(__name__)

DEFAULT_DAILY_LOOKBACK_DAYS = 365 * 12  # deep enough for a true lifetime ATH
DEFAULT_INTRADAY_LOOKBACK_DAYS = 30


class FetchOutcome(StrEnum):
    """How a resolve() call was satisfied -- surfaced in the UI and CI logs."""

    MEMORY = "memory"
    SNAPSHOT = "snapshot"
    NETWORK = "network"
    FAILED = "failed"


@dataclass(frozen=True)
class Resolution:
    """A resolved series plus the provenance the UI needs (spec 4.6)."""

    symbol: str
    interval: Interval
    df: pd.DataFrame
    source: Source
    outcome: FetchOutcome
    captured_at: datetime
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is not FetchOutcome.FAILED

    @property
    def is_fallback(self) -> bool:
        return self.source.is_fallback


@dataclass
class _Entry:
    snapshot: Snapshot
    stored_at: float


@dataclass
class MemoryCache:
    """Process-local TTL cache (spec 2.3 _DAILY_OHLC_CACHE / _INTRADAY_OHLC_CACHE).

    Consulted only while the market is LIVE. Thread-safe because Streamlit
    serves each browser session on its own thread.
    """

    _store: dict[tuple[str, str], _Entry] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @staticmethod
    def _ttl(interval: Interval) -> int:
        return INTRADAY_CACHE_TTL_SEC if interval.is_intraday else DAILY_CACHE_TTL_SEC

    def get(self, symbol: str, interval: Interval) -> Snapshot | None:
        key = (symbol.upper(), interval.value)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if _time.monotonic() - entry.stored_at > self._ttl(interval):
                del self._store[key]
                return None
            return entry.snapshot

    def put(self, snapshot: Snapshot) -> None:
        key = (snapshot.symbol.upper(), snapshot.interval.value)
        with self._lock:
            self._store[key] = _Entry(snapshot, _time.monotonic())

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


class Resolver:
    """Applies the session policy over an ordered provider chain."""

    def __init__(
        self,
        providers: list[DataProvider],
        cache: MemoryCache | None = None,
    ) -> None:
        if not providers:
            raise ValueError("Resolver needs at least one provider")
        self.providers = providers
        self.cache = cache or MemoryCache()

    # -- public API --------------------------------------------------------

    def daily(self, symbol: str, lookback_days: int = DEFAULT_DAILY_LOOKBACK_DAYS) -> Resolution:
        return self.resolve(symbol, Interval.DAY, lookback_days)

    def intraday(
        self,
        symbol: str,
        interval: Interval = Interval.MIN_15,
        lookback_days: int = DEFAULT_INTRADAY_LOOKBACK_DAYS,
    ) -> Resolution:
        return self.resolve(symbol, interval, lookback_days)

    def has_fresh(
        self, symbol: str, interval: Interval, as_of: datetime | None = None
    ) -> bool:
        """True when resolve() can be satisfied without a network call.

        Lets a batch skip the rate limiter for cached symbols, which is what
        makes re-running a scan within the same session near-instant.
        """
        symbol = symbol.upper().strip()
        now = as_of or now_ist()
        if session_state(now) is SessionState.LIVE:
            return self.cache.get(symbol, interval) is not None
        return read_if_fresh(symbol, interval, now) is not None

    def resolve(
        self,
        symbol: str,
        interval: Interval,
        lookback_days: int,
        as_of: datetime | None = None,
    ) -> Resolution:
        symbol = symbol.upper().strip()
        now = as_of or now_ist()
        live = session_state(now) is SessionState.LIVE

        if live:
            cached = self.cache.get(symbol, interval)
            if cached is not None:
                return self._resolution(cached, FetchOutcome.MEMORY)
        else:
            snap = read_if_fresh(symbol, interval, now)
            if snap is not None:
                self.cache.put(snap)
                return self._resolution(snap, FetchOutcome.SNAPSHOT)

        return self._fetch_and_store(symbol, interval, lookback_days, now)

    # -- internals ---------------------------------------------------------

    def _fetch_and_store(
        self,
        symbol: str,
        interval: Interval,
        lookback_days: int,
        now: datetime,
    ) -> Resolution:
        start = (now - timedelta(days=lookback_days)).date()
        end: date = now.date()
        errors: list[str] = []

        for provider in self.providers:
            if not provider.is_available():
                continue
            try:
                df = provider.fetch_bars(symbol, interval, start, end)
            except TokenExpired:
                # Spec 2.2/7: never silently downgrade an auth failure -- the
                # caller must halt the scan and re-authenticate.
                raise
            except ProviderError as exc:
                log.debug("%s failed for %s: %s", provider.name, symbol, exc)
                errors.append(f"{provider.name}: {exc}")
                continue

            snap = write_bars(symbol, df, interval, provider.source, captured_at=now)
            self.cache.put(snap)
            return self._resolution(snap, FetchOutcome.NETWORK)

        # Every provider failed. A stale snapshot beats nothing, but it is
        # labelled SNAPSHOT with the error attached so the UI can flag it.
        from infinity.data.snapshot_store import read_bars

        stale = read_bars(symbol, interval)
        reason = "; ".join(errors) or "no provider available"
        if stale is not None:
            log.warning("serving STALE snapshot for %s (%s)", symbol, reason)
            return Resolution(
                symbol=symbol,
                interval=interval,
                df=stale.df,
                source=stale.source,
                outcome=FetchOutcome.SNAPSHOT,
                captured_at=stale.captured_at,
                error=f"stale snapshot; {reason}",
            )

        return Resolution(
            symbol=symbol,
            interval=interval,
            df=pd.DataFrame(),
            source=Source.SNAPSHOT,
            outcome=FetchOutcome.FAILED,
            captured_at=now,
            error=reason,
        )

    @staticmethod
    def _resolution(snap: Snapshot, outcome: FetchOutcome) -> Resolution:
        return Resolution(
            symbol=snap.symbol,
            interval=snap.interval,
            df=snap.df,
            source=snap.source,
            outcome=outcome,
            captured_at=snap.captured_at,
        )


def build_default_resolver(allow_upstox: bool = True) -> Resolver:
    """Provider chain: Upstox primary, yfinance fallback (spec 2.1).

    Upstox candle endpoints are unauthenticated (ADR 0002), so this ordering
    holds everywhere -- local, hosted and CI -- which is what the spec asked
    for in the first place. yfinance remains the documented fallback.
    """
    from infinity.data.providers.yfinance_provider import YFinanceProvider

    providers: list[DataProvider] = []
    if allow_upstox:
        try:
            from infinity.data.providers.upstox_provider import UpstoxProvider

            up = UpstoxProvider()
            if up.is_available():
                providers.append(up)
        except ModuleNotFoundError:
            pass
    providers.append(YFinanceProvider())
    return Resolver(providers)


__all__ = [
    "FetchOutcome",
    "MemoryCache",
    "Resolution",
    "Resolver",
    "build_default_resolver",
    "last_session_close",
]
