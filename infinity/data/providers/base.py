"""Provider interface plus the shared dataframe sanitiser (spec 2.4)."""

from __future__ import annotations

import logging
from datetime import date
from typing import Protocol, runtime_checkable

import pandas as pd

from infinity.data.models import OHLCV_COLUMNS, Interval, Source
from infinity.market_clock import IST

log = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """Provider could not serve the request; caller may fall back."""


class TokenExpired(ProviderError):
    """Auth rejected (HTTP 401). Spec 2.2: pause the scan, keep partial results."""


class SymbolNotFound(ProviderError):
    """Symbol could not be mapped to an instrument (spec 7)."""


@runtime_checkable
class DataProvider(Protocol):
    """Every provider returns the same tidy frame, so callers never branch on source."""

    name: str
    source: Source

    def is_available(self) -> bool: ...

    def fetch_bars(
        self,
        symbol: str,
        interval: Interval,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """Return columns ``ts, open, high, low, close, volume``.

        ``ts`` is tz-aware IST, ascending, deduplicated. Raises ProviderError on
        failure -- an empty frame means "no bars in range", not "it broke".
        """
        ...


_FIELD_NAMES = {
    "ts", "date", "datetime", "timestamp", "index",
    "open", "high", "low", "close", "volume",
    "adj close", "adj_close", "adjclose",
}


def _pick_field_level(col: tuple) -> str:
    """Choose the field-name level out of a MultiIndex column tuple."""
    parts = [str(lvl).strip() for lvl in col if lvl not in (None, "")]
    if not parts:
        return ""
    return next((p for p in parts if p.lower() in _FIELD_NAMES), parts[0])


def clean_dataframe(df: pd.DataFrame, interval: Interval) -> pd.DataFrame:
    """Normalise any provider's output into the canonical frame (spec 2.4).

    Flattens the MultiIndex columns yfinance returns for multi-symbol downloads
    and forces 1-D Series, which is what the spec's "pandas 2.0+ 2D-assignment
    errors" note is about.
    """
    if df is None or len(df) == 0:
        empty = pd.DataFrame(columns=OHLCV_COLUMNS)
        empty["ts"] = pd.to_datetime(empty["ts"], utc=True)
        return empty

    out = df.copy()

    if isinstance(out.columns, pd.MultiIndex):
        # yfinance yields (field, ticker) -- e.g. ('Open', 'RELIANCE.NS') -- but
        # the order is not guaranteed across versions or call shapes. Pick the
        # level that actually names a field; fall back to the first level.
        out.columns = [_pick_field_level(col) for col in out.columns]

    out.columns = [str(c).strip().lower().replace(" ", "_") for c in out.columns]

    if "ts" not in out.columns:
        for cand in ("date", "datetime", "timestamp", "index"):
            if cand in out.columns:
                out = out.rename(columns={cand: "ts"})
                break

    missing = [c for c in OHLCV_COLUMNS if c not in out.columns]
    if missing:
        raise ProviderError(f"provider frame missing columns {missing}; got {list(out.columns)}")

    out = out.loc[:, OHLCV_COLUMNS]

    # Force 1-D: a duplicated column name yields a DataFrame slice, not a Series.
    for col in OHLCV_COLUMNS:
        val = out[col]
        if isinstance(val, pd.DataFrame):
            out[col] = val.iloc[:, 0]

    ts = pd.to_datetime(out["ts"], utc=True, errors="coerce")
    out["ts"] = ts.dt.tz_convert(IST)

    for col in ("open", "high", "low", "close", "volume"):
        out[col] = pd.to_numeric(out[col], errors="coerce")

    out = (
        out.dropna(subset=["ts", "open", "high", "low", "close"])
        .drop_duplicates(subset=["ts"], keep="last")
        .sort_values("ts")
        .reset_index(drop=True)
    )
    out["volume"] = out["volume"].fillna(0)

    if interval is Interval.DAY:
        out["ts"] = out["ts"].dt.normalize()

    return out
