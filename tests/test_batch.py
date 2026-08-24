"""Batch fetch tests: rate limiting, partial-result preservation, skip reporting."""

from __future__ import annotations

import threading
import time
from datetime import date

import pandas as pd
import pytest

from infinity.data.batch import BatchProgress, RateLimiter, chunked, fetch_many
from infinity.data.models import Interval, Source
from infinity.data.providers.base import ProviderError, TokenExpired
from infinity.data.resolver import Resolver
from infinity.market_clock import IST
from tests.conftest import ist


@pytest.fixture(autouse=True)
def _tmp_data_dirs(monkeypatch, tmp_path):
    from infinity import config
    from infinity.data import snapshot_store as ss

    daily = tmp_path / "ohlc" / "daily"
    intraday = tmp_path / "ohlc" / "intraday"
    snaps = tmp_path / "snapshots"
    for d in (daily, intraday, snaps):
        d.mkdir(parents=True, exist_ok=True)
    for mod in (config, ss):
        monkeypatch.setattr(mod, "DAILY_DIR", daily, raising=False)
        monkeypatch.setattr(mod, "INTRADAY_DIR", intraday, raising=False)
        monkeypatch.setattr(mod, "SNAPSHOT_DIR", snaps, raising=False)
    monkeypatch.setattr(ss, "ensure_dirs", lambda: None)
    return tmp_path


class ScriptedProvider:
    """Fails for symbols listed in ``fail_for``; succeeds otherwise."""

    name = "scripted"
    source = Source.YFINANCE

    def __init__(self, fail_for: dict[str, Exception] | None = None):
        self.fail_for = fail_for or {}
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def is_available(self) -> bool:
        return True

    def fetch_bars(self, symbol, interval, start: date, end: date) -> pd.DataFrame:
        with self._lock:
            self.calls.append(symbol)
        if symbol in self.fail_for:
            raise self.fail_for[symbol]
        ts = pd.date_range("2026-08-17", periods=3, freq="D", tz=IST)
        return pd.DataFrame(
            {
                "ts": ts,
                "open": [1.0, 2.0, 3.0], "high": [1.0, 2.0, 3.0],
                "low": [1.0, 2.0, 3.0], "close": [1.0, 2.0, 3.0],
                "volume": [10, 20, 30],
            }
        )


class TestFetchMany:
    def test_all_symbols_succeed(self) -> None:
        p = ScriptedProvider()
        out = fetch_many(Resolver([p]), ["A", "B", "C"], workers=2, rate_per_sec=100)

        assert out.succeeded == 3
        assert out.skipped == []
        assert out.failure_pct == 0.0
        assert out.network_calls == 3
        assert sorted(out.resolutions) == ["A", "B", "C"]

    def test_failures_are_reported_with_reasons(self) -> None:
        p = ScriptedProvider({"B": ProviderError("delisted")})
        out = fetch_many(Resolver([p]), ["A", "B", "C"], workers=2, rate_per_sec=100)

        assert out.succeeded == 2
        assert len(out.skipped) == 1
        assert out.skipped[0].symbol == "B"
        assert "delisted" in out.skipped[0].reason
        assert out.failure_pct == pytest.approx(100 / 3)

    def test_token_expiry_halts_but_preserves_partial_results(self) -> None:
        """Spec 2.2/7: an expired token must not discard work already done."""
        p = ScriptedProvider({"C": TokenExpired("token expired at 03:30")})
        out = fetch_many(
            Resolver([p]), ["A", "B", "C", "D", "E"], workers=1, rate_per_sec=100
        )

        assert out.halted
        assert out.halt_reason is not None and "token expired" in out.halt_reason
        assert out.succeeded >= 2, "results fetched before the halt must survive"
        assert "A" in out.resolutions and "B" in out.resolutions

    def test_empty_symbol_list_is_a_no_op(self) -> None:
        p = ScriptedProvider()
        out = fetch_many(Resolver([p]), [], workers=2)
        assert out.succeeded == 0 and out.requested == 0 and out.failure_pct == 0.0

    def test_symbols_are_upper_cased(self) -> None:
        p = ScriptedProvider()
        out = fetch_many(Resolver([p]), ["reliance"], workers=1, rate_per_sec=100)
        assert "RELIANCE" in out.resolutions

    def test_progress_callback_reaches_completion(self) -> None:
        seen: list[BatchProgress] = []
        p = ScriptedProvider({"B": ProviderError("x")})
        fetch_many(
            Resolver([p]), ["A", "B", "C"], workers=1, rate_per_sec=100,
            on_progress=lambda pr: seen.append(
                BatchProgress(pr.total, pr.completed, pr.succeeded, pr.failed)
            ),
        )
        assert len(seen) == 3
        assert seen[-1].completed == 3
        assert seen[-1].succeeded == 2 and seen[-1].failed == 1
        assert seen[-1].remaining == 0

    def test_cached_symbols_do_not_hit_the_provider(self) -> None:
        p = ScriptedProvider()
        r = Resolver([p])
        symbols = ["A", "B"]

        fetch_many(r, symbols, workers=1, rate_per_sec=100)
        assert len(p.calls) == 2

        out = fetch_many(Resolver([p]), symbols, workers=1, rate_per_sec=100)
        assert len(p.calls) == 2, "second run must be served from snapshots"
        assert out.cache_hits == 2 and out.network_calls == 0

    def test_fallback_symbols_are_listed(self) -> None:
        p = ScriptedProvider()
        out = fetch_many(Resolver([p]), ["A", "B"], workers=1, rate_per_sec=100)
        assert out.fallback_symbols == ["A", "B"]  # ScriptedProvider is yfinance


class TestRateLimiter:
    def test_enforces_the_average_rate(self) -> None:
        limiter = RateLimiter(rate_per_sec=20, burst=1)
        started = time.monotonic()
        for _ in range(6):
            limiter.acquire()
        # 6 tokens at 20/s with burst 1 -> >= 5 intervals of 50 ms.
        assert time.monotonic() - started >= 0.2

    def test_burst_is_allowed_immediately(self) -> None:
        limiter = RateLimiter(rate_per_sec=1, burst=5)
        started = time.monotonic()
        for _ in range(5):
            limiter.acquire()
        assert time.monotonic() - started < 0.5

    def test_rejects_a_non_positive_rate(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            RateLimiter(0)

    def test_is_thread_safe(self) -> None:
        limiter = RateLimiter(rate_per_sec=500, burst=1)
        count = {"n": 0}
        lock = threading.Lock()

        def worker():
            for _ in range(20):
                limiter.acquire()
                with lock:
                    count["n"] += 1

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert count["n"] == 80


class TestChunked:
    def test_splits_evenly_and_keeps_the_remainder(self) -> None:
        assert [list(c) for c in chunked(list("abcde"), 2)] == [
            ["a", "b"], ["c", "d"], ["e"]
        ]

    def test_size_larger_than_input_yields_one_chunk(self) -> None:
        assert [list(c) for c in chunked(["a"], 10)] == [["a"]]

    def test_rejects_a_non_positive_size(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            list(chunked(["a"], 0))


class TestResolverHasFresh:
    def test_reflects_snapshot_freshness_when_closed(self) -> None:
        p = ScriptedProvider()
        r = Resolver([p])
        saturday = ist("2026-08-22 11:00")

        assert not r.has_fresh("A", Interval.DAY, saturday)
        r.resolve("A", Interval.DAY, 30, as_of=saturday)
        assert r.has_fresh("A", Interval.DAY, saturday)
