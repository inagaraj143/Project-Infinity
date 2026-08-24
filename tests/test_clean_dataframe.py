"""clean_dataframe tests (spec 2.4 -- 1D series sanitisation)."""

from __future__ import annotations

import pandas as pd
import pytest

from infinity.data.models import Interval
from infinity.data.providers.base import ProviderError, clean_dataframe
from infinity.market_clock import IST


def test_flattens_yfinance_multiindex_to_field_names() -> None:
    """Regression: the flattener used to pick the ticker level, not the field.

    yfinance returns columns like ('Open', 'RELIANCE.NS'); taking the wrong
    level yields six identically-named 'reliance.ns' columns and every symbol
    fails with 'missing columns'.
    """
    idx = pd.date_range("2026-08-17", periods=3, freq="D", tz="UTC")
    cols = pd.MultiIndex.from_product(
        [["Open", "High", "Low", "Close", "Volume"], ["RELIANCE.NS"]],
        names=["Price", "Ticker"],
    )
    raw = pd.DataFrame(
        [[100, 105, 99, 104, 1000], [101, 106, 100, 105, 2000], [102, 107, 101, 106, 3000]],
        index=idx,
        columns=cols,
    )

    out = clean_dataframe(raw.reset_index(), Interval.DAY)

    assert list(out.columns) == ["ts", "open", "high", "low", "close", "volume"]
    assert len(out) == 3
    assert out["close"].iloc[-1] == 106


def test_flattens_reversed_multiindex_order() -> None:
    """(ticker, field) ordering must resolve to the field just the same."""
    idx = pd.date_range("2026-08-17", periods=2, freq="D", tz="UTC")
    cols = pd.MultiIndex.from_product([["TCS.NS"], ["Open", "High", "Low", "Close", "Volume"]])
    raw = pd.DataFrame([[1, 2, 0.5, 1.5, 10], [2, 3, 1.5, 2.5, 20]], index=idx, columns=cols)

    out = clean_dataframe(raw.reset_index(), Interval.DAY)
    assert list(out.columns) == ["ts", "open", "high", "low", "close", "volume"]


def test_converts_utc_to_ist() -> None:
    raw = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-08-17 04:00:00"], utc=True),
            "Open": [100.0], "High": [101.0], "Low": [99.0],
            "Close": [100.5], "Volume": [500],
        }
    )
    out = clean_dataframe(raw, Interval.MIN_15)
    assert str(out["ts"].dt.tz) == str(IST)
    assert out["ts"].iloc[0].hour == 9  # 04:00 UTC -> 09:30 IST


def test_daily_timestamps_are_normalised_to_midnight() -> None:
    raw = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-08-17 10:30:00"], utc=True),
            "Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [1],
        }
    )
    out = clean_dataframe(raw, Interval.DAY)
    assert (out["ts"].iloc[0].hour, out["ts"].iloc[0].minute) == (0, 0)


def test_sorts_and_deduplicates_on_timestamp() -> None:
    raw = pd.DataFrame(
        {
            "Date": pd.to_datetime(
                ["2026-08-19", "2026-08-17", "2026-08-18", "2026-08-18"], utc=True
            ),
            "Open": [3.0, 1.0, 2.0, 2.5],
            "High": [3.0, 1.0, 2.0, 2.5],
            "Low": [3.0, 1.0, 2.0, 2.5],
            "Close": [3.0, 1.0, 2.0, 2.5],
            "Volume": [30, 10, 20, 25],
        }
    )
    out = clean_dataframe(raw, Interval.DAY)
    assert len(out) == 3, "duplicate timestamp must collapse"
    assert out["ts"].is_monotonic_increasing
    assert out["close"].iloc[1] == 2.5, "last write wins on a duplicate"


def test_rows_with_null_prices_are_dropped() -> None:
    raw = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-08-17", "2026-08-18"], utc=True),
            "Open": [1.0, None], "High": [1.0, None], "Low": [1.0, None],
            "Close": [1.0, None], "Volume": [10, 20],
        }
    )
    assert len(clean_dataframe(raw, Interval.DAY)) == 1


def test_missing_volume_becomes_zero_not_nan() -> None:
    raw = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-08-17"], utc=True),
            "Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [None],
        }
    )
    assert clean_dataframe(raw, Interval.DAY)["volume"].iloc[0] == 0


def test_empty_input_yields_an_empty_canonical_frame() -> None:
    out = clean_dataframe(pd.DataFrame(), Interval.DAY)
    assert out.empty
    assert list(out.columns) == ["ts", "open", "high", "low", "close", "volume"]


def test_missing_required_column_raises() -> None:
    raw = pd.DataFrame(
        {"Date": pd.to_datetime(["2026-08-17"], utc=True), "Open": [1.0], "Close": [1.0]}
    )
    with pytest.raises(ProviderError, match="missing columns"):
        clean_dataframe(raw, Interval.DAY)
