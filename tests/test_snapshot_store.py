"""Snapshot store round-trip and freshness tests (spec 8: cache correctness)."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from infinity.data.models import Interval, Snapshot, Source
from infinity.market_clock import IST
from tests.conftest import ist


@pytest.fixture(autouse=True)
def _tmp_data_dirs(monkeypatch, tmp_path):
    """Redirect every store path into tmp_path so tests never touch data/."""
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


def make_df(n: int = 5, intraday: bool = False) -> pd.DataFrame:
    freq = "15min" if intraday else "D"
    start = "2026-08-17 09:15" if intraday else "2026-08-17"
    ts = pd.date_range(start=start, periods=n, freq=freq, tz=IST)
    return pd.DataFrame(
        {
            "ts": ts,
            "open": [100.0 + i for i in range(n)],
            "high": [105.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [104.0 + i for i in range(n)],
            "volume": [1_000 * (i + 1) for i in range(n)],
        }
    )


class TestRoundTrip:
    def test_daily_round_trip_preserves_values(self) -> None:
        from infinity.data.snapshot_store import read_bars, write_bars

        df = make_df()
        write_bars(
            "RELIANCE", df, Interval.DAY, Source.YFINANCE,
            captured_at=ist("2026-08-21 16:00"),
        )
        back = read_bars("RELIANCE", Interval.DAY)

        assert back is not None
        assert back.symbol == "RELIANCE"
        assert back.source is Source.YFINANCE
        assert back.bar_count == 5
        pd.testing.assert_series_equal(
            back.df["close"].reset_index(drop=True),
            df["close"].reset_index(drop=True),
            check_names=False,
        )

    def test_intraday_round_trip_preserves_time_of_day(self) -> None:
        from infinity.data.snapshot_store import read_bars, write_bars

        df = make_df(4, intraday=True)
        write_bars("TCS", df, Interval.MIN_15, Source.UPSTOX, captured_at=ist("2026-08-21 16:00"))
        back = read_bars("TCS", Interval.MIN_15)

        assert back is not None
        first = back.df["ts"].iloc[0]
        assert (first.hour, first.minute) == (9, 15)
        assert str(first.tzinfo) == str(IST)

    def test_payload_is_columnar(self) -> None:
        from infinity.data.snapshot_store import bars_path, write_bars

        write_bars("INFY", make_df(3), Interval.DAY, Source.YFINANCE)
        payload = json.loads(bars_path("INFY", Interval.DAY).read_text(encoding="utf-8"))

        assert payload["columns"] == ["ts", "open", "high", "low", "close", "volume"]
        assert isinstance(payload["rows"][0], list), "rows must be arrays, not objects"
        assert payload["bar_count"] == 3

    def test_symbol_with_slash_does_not_escape_the_directory(self) -> None:
        from infinity.data.snapshot_store import bars_path, read_bars, write_bars

        write_bars("M&M/A", make_df(2), Interval.DAY, Source.YFINANCE)
        assert "/" not in bars_path("M&M/A", Interval.DAY).name
        assert read_bars("M&M/A", Interval.DAY) is not None


class TestFreshness:
    """captured_at >= last_session_close is the entire cache policy."""

    def test_snapshot_taken_after_close_is_fresh_all_weekend(self) -> None:
        from infinity.data.snapshot_store import read_if_fresh, write_bars

        write_bars(
            "RELIANCE", make_df(), Interval.DAY, Source.YFINANCE,
            captured_at=ist("2026-08-21 15:45"),  # Friday, just after close
        )
        weekend = ("2026-08-21 20:00", "2026-08-22 10:00",
                   "2026-08-23 23:00", "2026-08-24 12:00")
        for when in weekend:
            assert read_if_fresh("RELIANCE", Interval.DAY, ist(when)) is not None, when

    def test_goes_stale_once_the_next_session_closes(self) -> None:
        from infinity.data.snapshot_store import read_if_fresh, write_bars

        write_bars(
            "RELIANCE", make_df(), Interval.DAY, Source.YFINANCE,
            captured_at=ist("2026-08-21 15:45"),
        )
        # Tuesday 2026-08-25 is the next trading day after the Monday holiday.
        assert read_if_fresh("RELIANCE", Interval.DAY, ist("2026-08-25 15:29")) is not None
        assert read_if_fresh("RELIANCE", Interval.DAY, ist("2026-08-25 16:00")) is None

    def test_snapshot_captured_mid_session_is_stale_after_that_close(self) -> None:
        """A 12:00 intraday capture must not be reused as the settled EOD series."""
        from infinity.data.snapshot_store import read_if_fresh, write_bars

        write_bars(
            "TCS", make_df(), Interval.DAY, Source.YFINANCE,
            captured_at=ist("2026-08-21 12:00"),
        )
        assert read_if_fresh("TCS", Interval.DAY, ist("2026-08-21 12:05")) is not None
        assert read_if_fresh("TCS", Interval.DAY, ist("2026-08-21 15:31")) is None

    def test_missing_symbol_returns_none(self) -> None:
        from infinity.data.snapshot_store import read_if_fresh

        assert read_if_fresh("NOSUCH", Interval.DAY, ist("2026-08-21 16:00")) is None


class TestResilience:
    def test_corrupt_file_returns_none_instead_of_raising(self, _tmp_data_dirs) -> None:
        from infinity.data.snapshot_store import bars_path, read_bars, write_bars

        write_bars("BADCO", make_df(2), Interval.DAY, Source.YFINANCE)
        bars_path("BADCO", Interval.DAY).write_text("{not json", encoding="utf-8")
        assert read_bars("BADCO", Interval.DAY) is None

    def test_old_schema_is_discarded(self) -> None:
        from infinity.data.snapshot_store import bars_path, read_bars, write_bars

        write_bars("OLDCO", make_df(2), Interval.DAY, Source.YFINANCE)
        p = bars_path("OLDCO", Interval.DAY)
        payload = json.loads(p.read_text(encoding="utf-8"))
        payload["schema"] = 0
        p.write_text(json.dumps(payload), encoding="utf-8")

        assert read_bars("OLDCO", Interval.DAY) is None

    def test_no_temp_files_are_left_behind(self, _tmp_data_dirs) -> None:
        from infinity.data.snapshot_store import bars_path, write_bars

        write_bars("CLEANCO", make_df(2), Interval.DAY, Source.YFINANCE)
        leftovers = list(bars_path("CLEANCO", Interval.DAY).parent.glob("*.tmp"))
        assert leftovers == []


class TestScanSnapshots:
    def test_write_then_read_latest(self) -> None:
        from infinity.data.snapshot_store import read_scan, write_scan

        rows = [{"symbol": "RELIANCE", "score": 91}, {"symbol": "TCS", "score": 84}]
        write_scan("trendlines", rows, universe="nifty50", captured_at=ist("2026-08-21 15:45"))

        got = read_scan("trendlines")
        assert got is not None
        assert got["row_count"] == 2
        assert got["universe"] == "nifty50"
        assert got["rows"][0]["symbol"] == "RELIANCE"

    def test_read_missing_module_returns_none(self) -> None:
        from infinity.data.snapshot_store import read_scan, write_scan

        write_scan("trendlines", [], universe="nifty50", captured_at=ist("2026-08-21 15:45"))
        assert read_scan("displacement") is None


class TestSnapshotModel:
    def test_is_fresh_boundary_is_inclusive(self) -> None:
        close = ist("2026-08-21 15:30")
        snap = Snapshot(
            symbol="X",
            interval=Interval.DAY,
            source=Source.YFINANCE,
            captured_at=close,
            session_close=close,
            df=make_df(1),
        )
        assert snap.is_fresh(close)
        assert not snap.is_fresh(ist("2026-08-25 15:30"))

    def test_yfinance_is_flagged_as_fallback(self) -> None:
        assert Source.YFINANCE.is_fallback
        assert not Source.UPSTOX.is_fallback
