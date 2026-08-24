"""Resolver policy tests: which path serves the data, and how many network hits."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from infinity.data.models import Interval, Source
from infinity.data.providers.base import ProviderError, TokenExpired
from infinity.data.resolver import FetchOutcome, MemoryCache, Resolver
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


class FakeProvider:
    """Counts calls so tests can assert the cache actually prevents network hits."""

    def __init__(self, name="fake", source=Source.YFINANCE, fail=None, available=True):
        self.name = name
        self.source = source
        self.calls = 0
        self._fail = fail
        self._available = available

    def is_available(self) -> bool:
        return self._available

    def fetch_bars(self, symbol: str, interval: Interval, start: date, end: date) -> pd.DataFrame:
        self.calls += 1
        if self._fail:
            raise self._fail
        ts = pd.date_range("2026-08-17", periods=3, freq="D", tz=IST)
        return pd.DataFrame(
            {
                "ts": ts,
                "open": [100.0, 101.0, 102.0],
                "high": [105.0, 106.0, 107.0],
                "low": [99.0, 100.0, 101.0],
                "close": [104.0, 105.0, 106.0],
                "volume": [1000, 2000, 3000],
            }
        )


class TestClosedMarketPath:
    """Market closed: fetch once, then serve JSON forever."""

    def test_first_call_fetches_then_subsequent_calls_do_not(self) -> None:
        p = FakeProvider()
        r = Resolver([p])
        saturday = ist("2026-08-22 11:00")

        first = r.resolve("RELIANCE", Interval.DAY, 30, as_of=saturday)
        assert first.outcome is FetchOutcome.NETWORK
        assert p.calls == 1

        # A brand-new Resolver (cold memory cache) must still hit only the JSON.
        for _ in range(3):
            again = Resolver([p]).resolve("RELIANCE", Interval.DAY, 30, as_of=saturday)
            assert again.outcome is FetchOutcome.SNAPSHOT
        assert p.calls == 1, "closed-market reads must not touch the network again"

    def test_snapshot_survives_the_whole_long_weekend(self) -> None:
        p = FakeProvider()
        r = Resolver([p])
        r.resolve("TCS", Interval.DAY, 30, as_of=ist("2026-08-21 15:45"))
        assert p.calls == 1

        weekend = ("2026-08-21 20:00", "2026-08-22 09:00",
                   "2026-08-23 22:00", "2026-08-24 13:00")
        for when in weekend:
            out = Resolver([p]).resolve("TCS", Interval.DAY, 30, as_of=ist(when))
            assert out.outcome is FetchOutcome.SNAPSHOT, when
        assert p.calls == 1

    def test_refetches_once_the_next_session_closes(self) -> None:
        p = FakeProvider()
        Resolver([p]).resolve("TCS", Interval.DAY, 30, as_of=ist("2026-08-21 15:45"))
        assert p.calls == 1

        Resolver([p]).resolve("TCS", Interval.DAY, 30, as_of=ist("2026-08-25 16:00"))
        assert p.calls == 2, "a new settled close must invalidate the snapshot"


class TestLiveMarketPath:
    def test_memory_cache_absorbs_repeat_calls(self) -> None:
        p = FakeProvider()
        r = Resolver([p])
        live = ist("2026-08-21 11:00")

        assert r.resolve("INFY", Interval.DAY, 30, as_of=live).outcome is FetchOutcome.NETWORK
        assert r.resolve("INFY", Interval.DAY, 30, as_of=live).outcome is FetchOutcome.MEMORY
        assert p.calls == 1

    def test_live_ignores_a_stale_snapshot(self) -> None:
        """Yesterday's JSON must never be served as today's live series."""
        from infinity.data.snapshot_store import write_bars

        p = FakeProvider()
        write_bars(
            "INFY", FakeProvider().fetch_bars("INFY", Interval.DAY, date.today(), date.today()),
            Interval.DAY, Source.YFINANCE, captured_at=ist("2026-08-20 16:00"),
        )
        out = Resolver([p]).resolve("INFY", Interval.DAY, 30, as_of=ist("2026-08-21 11:00"))
        assert out.outcome is FetchOutcome.NETWORK
        assert p.calls == 1

    def test_live_fetch_still_warms_the_json_store(self) -> None:
        from infinity.data.snapshot_store import read_bars

        p = FakeProvider()
        Resolver([p]).resolve("WIPRO", Interval.DAY, 30, as_of=ist("2026-08-21 11:00"))
        assert read_bars("WIPRO", Interval.DAY) is not None


class TestProviderChain:
    def test_falls_through_to_the_next_provider(self) -> None:
        bad = FakeProvider("bad", Source.UPSTOX, fail=ProviderError("boom"))
        good = FakeProvider("good", Source.YFINANCE)
        out = Resolver([bad, good]).resolve("X", Interval.DAY, 30, as_of=ist("2026-08-22 11:00"))

        assert out.outcome is FetchOutcome.NETWORK
        assert out.source is Source.YFINANCE
        assert out.is_fallback, "fallback source must be flagged for the 4.6 UI tag"
        assert bad.calls == 1 and good.calls == 1

    def test_unavailable_provider_is_skipped_without_calling(self) -> None:
        off = FakeProvider("off", available=False)
        on = FakeProvider("on")
        Resolver([off, on]).resolve("X", Interval.DAY, 30, as_of=ist("2026-08-22 11:00"))
        assert off.calls == 0 and on.calls == 1

    def test_token_expiry_propagates_instead_of_falling_back(self) -> None:
        """Spec 2.2: an expired token must halt the scan, not silently degrade."""
        bad = FakeProvider("upstox", Source.UPSTOX, fail=TokenExpired("expired"))
        good = FakeProvider("yf", Source.YFINANCE)

        with pytest.raises(TokenExpired):
            Resolver([bad, good]).resolve("X", Interval.DAY, 30, as_of=ist("2026-08-21 11:00"))
        assert good.calls == 0

    def test_total_failure_returns_failed_not_an_exception(self) -> None:
        bad = FakeProvider("bad", fail=ProviderError("down"))
        out = Resolver([bad]).resolve("X", Interval.DAY, 30, as_of=ist("2026-08-22 11:00"))

        assert out.outcome is FetchOutcome.FAILED
        assert not out.ok
        assert out.df.empty
        assert "down" in (out.error or "")

    def test_stale_snapshot_beats_nothing_when_providers_are_down(self) -> None:
        good = FakeProvider("good")
        Resolver([good]).resolve("X", Interval.DAY, 30, as_of=ist("2026-08-20 16:00"))

        bad = FakeProvider("bad", fail=ProviderError("network down"))
        out = Resolver([bad]).resolve("X", Interval.DAY, 30, as_of=ist("2026-08-25 16:00"))

        assert out.outcome is FetchOutcome.SNAPSHOT
        assert out.error is not None and "stale" in out.error
        assert not out.df.empty

    def test_empty_provider_list_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one provider"):
            Resolver([])


class TestMemoryCacheTTL:
    def test_entry_expires_after_its_ttl(self, monkeypatch) -> None:
        import infinity.data.resolver as res

        clock = {"t": 1000.0}
        monkeypatch.setattr(res._time, "monotonic", lambda: clock["t"])

        cache = MemoryCache()
        p = FakeProvider()
        r = Resolver([p], cache=cache)
        live = ist("2026-08-21 11:00")

        r.resolve("Z", Interval.DAY, 30, as_of=live)
        assert p.calls == 1

        clock["t"] += 60  # inside the 15-minute daily TTL
        assert r.resolve("Z", Interval.DAY, 30, as_of=live).outcome is FetchOutcome.MEMORY

        clock["t"] += 16 * 60  # past it
        r.resolve("Z", Interval.DAY, 30, as_of=live)
        assert p.calls == 2

    def test_intraday_uses_the_shorter_ttl(self, monkeypatch) -> None:
        import infinity.data.resolver as res

        clock = {"t": 1000.0}
        monkeypatch.setattr(res._time, "monotonic", lambda: clock["t"])

        p = FakeProvider()
        r = Resolver([p], cache=MemoryCache())
        live = ist("2026-08-21 11:00")

        r.resolve("Z", Interval.MIN_15, 5, as_of=live)
        clock["t"] += 6 * 60  # past the 5-minute intraday TTL
        r.resolve("Z", Interval.MIN_15, 5, as_of=live)
        assert p.calls == 2
