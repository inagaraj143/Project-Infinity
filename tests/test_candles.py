"""ensure_today_candle tests (spec 2.1 live-session integration)."""

from __future__ import annotations

import pandas as pd

from infinity.data.candles import (
    aggregate_to_daily_bar,
    ensure_today_candle,
    has_min_bars,
    latest_bar,
)
from infinity.market_clock import IST
from tests.conftest import ist


def daily_frame(dates: list[str], close_base: float = 100.0) -> pd.DataFrame:
    ts = pd.to_datetime(dates).tz_localize(IST)
    n = len(dates)
    return pd.DataFrame(
        {
            "ts": ts,
            "open": [close_base + i for i in range(n)],
            "high": [close_base + 5 + i for i in range(n)],
            "low": [close_base - 5 + i for i in range(n)],
            "close": [close_base + 2 + i for i in range(n)],
            "volume": [1000 * (i + 1) for i in range(n)],
        }
    )


def intraday_frame(day: str, bars: list[tuple[float, float, float, float, int]]) -> pd.DataFrame:
    ts = pd.date_range(f"{day} 09:15", periods=len(bars), freq="15min", tz=IST)
    return pd.DataFrame(
        {
            "ts": ts,
            "open": [b[0] for b in bars],
            "high": [b[1] for b in bars],
            "low": [b[2] for b in bars],
            "close": [b[3] for b in bars],
            "volume": [b[4] for b in bars],
        }
    )


class TestAggregateToDailyBar:
    def test_ohlcv_aggregation_is_correct(self) -> None:
        intra = intraday_frame(
            "2026-08-21",
            [(100, 104, 99, 103, 500), (103, 110, 102, 108, 700), (108, 109, 105, 106, 300)],
        )
        bar = aggregate_to_daily_bar(intra, pd.Timestamp("2026-08-21", tz=IST))

        assert len(bar) == 1
        row = bar.iloc[0]
        assert row["open"] == 100, "open is the first bar's open"
        assert row["high"] == 110, "high is the max"
        assert row["low"] == 99, "low is the min"
        assert row["close"] == 106, "close is the last bar's close"
        assert row["volume"] == 1500, "volume is the sum"

    def test_other_days_are_ignored(self) -> None:
        intra = pd.concat(
            [
                intraday_frame("2026-08-20", [(1, 1, 1, 1, 10)]),
                intraday_frame("2026-08-21", [(50, 60, 40, 55, 999)]),
            ]
        )
        bar = aggregate_to_daily_bar(intra, pd.Timestamp("2026-08-21", tz=IST))
        assert bar.iloc[0]["close"] == 55 and bar.iloc[0]["volume"] == 999

    def test_empty_input_yields_empty(self) -> None:
        assert aggregate_to_daily_bar(pd.DataFrame(), pd.Timestamp("2026-08-21", tz=IST)).empty

    def test_no_bars_for_that_day_yields_empty(self) -> None:
        intra = intraday_frame("2026-08-20", [(1, 1, 1, 1, 10)])
        assert aggregate_to_daily_bar(intra, pd.Timestamp("2026-08-21", tz=IST)).empty


class TestEnsureTodayCandle:
    def test_appends_todays_bar_when_missing(self) -> None:
        daily = daily_frame(["2026-08-19", "2026-08-20"])
        intra = intraday_frame("2026-08-21", [(200, 210, 195, 205, 5000)])

        out = ensure_today_candle(daily, intra, as_of=ist("2026-08-21 12:00"))

        assert len(out) == 3
        assert out["ts"].iloc[-1].date().isoformat() == "2026-08-21"
        assert out["close"].iloc[-1] == 205
        assert out["ts"].is_monotonic_increasing

    def test_replaces_a_stale_bar_for_today(self) -> None:
        """An unsettled daily bar must be superseded by fresher intraday data."""
        daily = daily_frame(["2026-08-20", "2026-08-21"])
        intra = intraday_frame("2026-08-21", [(200, 210, 195, 207, 5000)])

        out = ensure_today_candle(daily, intra, as_of=ist("2026-08-21 14:00"))

        assert len(out) == 2, "must replace, not duplicate"
        assert out["close"].iloc[-1] == 207

    def test_no_op_on_a_non_trading_day(self) -> None:
        daily = daily_frame(["2026-08-20", "2026-08-21"])
        intra = intraday_frame("2026-08-22", [(1, 1, 1, 1, 1)])
        out = ensure_today_candle(daily, intra, as_of=ist("2026-08-22 12:00"))
        assert out.equals(daily)

    def test_no_op_on_a_holiday(self) -> None:
        daily = daily_frame(["2026-08-20", "2026-08-21"])
        intra = intraday_frame("2026-08-24", [(1, 1, 1, 1, 1)])
        out = ensure_today_candle(daily, intra, as_of=ist("2026-08-24 12:00"))
        assert out.equals(daily)

    def test_no_op_without_intraday_data(self) -> None:
        daily = daily_frame(["2026-08-20", "2026-08-21"])
        assert ensure_today_candle(daily, None, as_of=ist("2026-08-21 12:00")).equals(daily)

    def test_no_op_when_intraday_has_no_bars_for_today(self) -> None:
        daily = daily_frame(["2026-08-20"])
        intra = intraday_frame("2026-08-20", [(1, 1, 1, 1, 1)])
        out = ensure_today_candle(daily, intra, as_of=ist("2026-08-21 12:00"))
        assert out.equals(daily)

    def test_empty_daily_frame_is_returned_unchanged(self) -> None:
        empty = pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
        assert ensure_today_candle(empty, None, as_of=ist("2026-08-21 12:00")).empty


class TestHelpers:
    def test_latest_bar_returns_the_last_chronologically(self) -> None:
        df = daily_frame(["2026-08-21", "2026-08-19", "2026-08-20"])
        assert latest_bar(df)["ts"].date().isoformat() == "2026-08-21"

    def test_latest_bar_of_empty_is_none(self) -> None:
        assert latest_bar(pd.DataFrame()) is None

    def test_has_min_bars(self) -> None:
        df = daily_frame(["2026-08-19", "2026-08-20", "2026-08-21"])
        assert has_min_bars(df, 3)
        assert not has_min_bars(df, 4)
        assert not has_min_bars(None, 1)
