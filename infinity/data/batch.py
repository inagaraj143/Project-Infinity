"""Batched universe fetching (spec 2.3 rate limits, spec 7 resilience).

Three guarantees this module exists to provide:

1. **Rate limiting** -- a shared token bucket gates every worker, so raising
   ``workers`` never raises the request rate past ``rate_per_sec``.
2. **Partial results survive** -- an expired token (spec 2.2) halts the batch
   and returns what was already fetched, rather than discarding the run.
3. **Skips are visible** -- every failure is reported with a reason for the
   "Skipped Symbols" panel, never silently dropped.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from infinity.data.models import Interval
from infinity.data.providers.base import TokenExpired
from infinity.data.resolver import FetchOutcome, Resolution, Resolver

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Skipped:
    symbol: str
    reason: str


@dataclass
class BatchProgress:
    total: int
    completed: int = 0
    succeeded: int = 0
    failed: int = 0
    from_cache: int = 0
    current: str = ""

    @property
    def remaining(self) -> int:
        return self.total - self.completed

    @property
    def fraction(self) -> float:
        return self.completed / self.total if self.total else 1.0

    def __str__(self) -> str:
        return (
            f"{self.completed}/{self.total} scanned | {self.remaining} remaining "
            f"| {self.succeeded} ok | {self.failed} skipped"
        )


@dataclass
class BatchResult:
    resolutions: dict[str, Resolution] = field(default_factory=dict)
    skipped: list[Skipped] = field(default_factory=list)
    halted: bool = False
    halt_reason: str | None = None
    elapsed_sec: float = 0.0
    requested: int = 0

    @property
    def succeeded(self) -> int:
        return len(self.resolutions)

    @property
    def failure_pct(self) -> float:
        return 100.0 * len(self.skipped) / self.requested if self.requested else 0.0

    @property
    def network_calls(self) -> int:
        return sum(1 for r in self.resolutions.values() if r.outcome is FetchOutcome.NETWORK)

    @property
    def cache_hits(self) -> int:
        return sum(
            1 for r in self.resolutions.values()
            if r.outcome in (FetchOutcome.SNAPSHOT, FetchOutcome.MEMORY)
        )

    @property
    def fallback_symbols(self) -> list[str]:
        """Symbols served by the fallback feed -- drives the spec 4.6 tag."""
        return sorted(s for s, r in self.resolutions.items() if r.is_fallback)


class RateLimiter:
    """Token bucket shared across worker threads.

    Enforces an average of ``rate_per_sec`` with a small burst allowance, so a
    full-universe scan stays under the published per-second limit no matter how
    many workers are running.
    """

    def __init__(self, rate_per_sec: float, burst: int = 1) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be positive")
        self.rate = rate_per_sec
        self.capacity = max(1, burst)
        self._tokens = float(self.capacity)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._updated) * self.rate
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
            time.sleep(min(wait, 1.0))


def fetch_many(
    resolver: Resolver,
    symbols: Sequence[str],
    interval: Interval = Interval.DAY,
    lookback_days: int = 365 * 12,
    workers: int = 4,
    rate_per_sec: float = 8.0,
    on_progress: Callable[[BatchProgress], None] | None = None,
) -> BatchResult:
    """Fetch a universe, honouring the rate limit and preserving partial results."""
    symbols = [s.upper().strip() for s in symbols]
    result = BatchResult(requested=len(symbols))
    if not symbols:
        return result

    limiter = RateLimiter(rate_per_sec, burst=max(1, workers))
    progress = BatchProgress(total=len(symbols))
    halt = threading.Event()
    lock = threading.Lock()
    started = time.monotonic()

    def work(symbol: str) -> tuple[str, Resolution | None, str | None]:
        if halt.is_set():
            return symbol, None, "halted before fetch"
        # Only a network call consumes a token; cached reads are free, which is
        # what makes a re-run of a cached universe near-instant.
        if not resolver.has_fresh(symbol, interval):
            limiter.acquire()
        try:
            return symbol, resolver.resolve(symbol, interval, lookback_days), None
        except TokenExpired as exc:
            halt.set()
            return symbol, None, f"token expired: {exc}"
        except Exception as exc:  # a scan must never die on one bad symbol
            log.debug("unexpected error for %s: %s", symbol, exc)
            return symbol, None, f"{type(exc).__name__}: {exc}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, s) for s in symbols]
        for fut in concurrent.futures.as_completed(futures):
            symbol, res, err = fut.result()
            with lock:
                progress.completed += 1
                progress.current = symbol

                if res is not None and res.ok and len(res.df):
                    result.resolutions[symbol] = res
                    progress.succeeded += 1
                    if res.outcome in (FetchOutcome.SNAPSHOT, FetchOutcome.MEMORY):
                        progress.from_cache += 1
                else:
                    reason = err or (res.error if res else None) or "empty series"
                    result.skipped.append(Skipped(symbol, reason))
                    progress.failed += 1

                    if err and "token expired" in err:
                        result.halted = True
                        result.halt_reason = err

                if on_progress:
                    on_progress(progress)

    result.elapsed_sec = time.monotonic() - started

    if result.halted:
        # Spec 2.2/7: keep what we have and tell the caller to re-authenticate.
        log.warning(
            "batch halted on token expiry -- %d results preserved, %d not attempted",
            result.succeeded, len(result.skipped),
        )

    return result


def chunked(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    """Split a universe into batches (spec 2.3)."""
    if size <= 0:
        raise ValueError("size must be positive")
    for i in range(0, len(items), size):
        yield items[i : i + size]
