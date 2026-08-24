"""On-disk snapshot schema.

Bars are stored columnar -- ``columns`` plus an array-of-arrays ``rows`` --
rather than an array of objects. For 3,000 daily bars that is roughly 40%
smaller than repeating the key names on every row, which matters because these
files are committed to git by the EOD workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

import pandas as pd

from infinity.config import SNAPSHOT_SCHEMA
from infinity.market_clock import IST, to_ist

OHLCV_COLUMNS = ["ts", "open", "high", "low", "close", "volume"]


class Interval(StrEnum):
    DAY = "day"
    MIN_15 = "15minute"
    MIN_5 = "5minute"

    @property
    def is_intraday(self) -> bool:
        return self is not Interval.DAY


class Source(StrEnum):
    UPSTOX = "upstox"
    YFINANCE = "yfinance"
    SNAPSHOT = "snapshot"

    @property
    def is_fallback(self) -> bool:
        """Drives the spec 4.6 'Fallback Source' tag."""
        return self is Source.YFINANCE


class SchemaMismatch(ValueError):
    """Raised when a snapshot file predates the current SNAPSHOT_SCHEMA."""


@dataclass(frozen=True)
class Snapshot:
    """One symbol's bars plus the provenance needed to judge freshness."""

    symbol: str
    interval: Interval
    source: Source
    captured_at: datetime
    session_close: datetime
    df: pd.DataFrame
    instrument_key: str | None = None
    schema: int = SNAPSHOT_SCHEMA

    def is_fresh(self, last_close: datetime) -> bool:
        """Fresh iff captured at or after the most recent completed close.

        This is the whole cache policy. A snapshot taken at 15:52 on Friday
        stays fresh all weekend and through a Monday holiday, and goes stale
        the moment the next session actually closes.
        """
        return self.captured_at >= last_close

    @property
    def bar_count(self) -> int:
        return len(self.df)

    def to_payload(self) -> dict[str, Any]:
        out = self.df.copy()
        ts = out["ts"]
        # Daily bars serialise as plain dates; intraday keeps the IST offset.
        out["ts"] = (
            ts.dt.strftime("%Y-%m-%d")
            if self.interval is Interval.DAY
            else ts.dt.strftime("%Y-%m-%dT%H:%M:%S%z")
        )
        # NSE tick size is 0.05, so anything past 2dp is float32 noise from the
        # feed (e.g. 2596.300048828125). Rounding is both more correct and
        # roughly 40% smaller on disk, which matters for files kept in git.
        for col in ("open", "high", "low", "close"):
            out[col] = out[col].round(2)
        out["volume"] = out["volume"].fillna(0).astype("int64")
        return {
            "schema": self.schema,
            "symbol": self.symbol,
            "instrument_key": self.instrument_key,
            "interval": self.interval.value,
            "source": self.source.value,
            "captured_at": self.captured_at.isoformat(),
            "session_close": self.session_close.isoformat(),
            "bar_count": len(out),
            "columns": OHLCV_COLUMNS,
            "rows": out[OHLCV_COLUMNS].to_numpy().tolist(),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Snapshot:
        schema = int(payload.get("schema", 0))
        if schema != SNAPSHOT_SCHEMA:
            raise SchemaMismatch(
                f"snapshot schema {schema} != expected {SNAPSHOT_SCHEMA}; refetch required"
            )

        cols = payload.get("columns", OHLCV_COLUMNS)
        df = pd.DataFrame(payload.get("rows", []), columns=cols)
        interval = Interval(payload["interval"])

        if df.empty:
            df = pd.DataFrame(columns=OHLCV_COLUMNS)
            df["ts"] = pd.to_datetime(df["ts"], utc=True)
        else:
            parsed = pd.to_datetime(df["ts"], format="ISO8601", utc=True)
            df["ts"] = parsed.dt.tz_convert(IST)
            for c in ("open", "high", "low", "close", "volume"):
                df[c] = pd.to_numeric(df[c], errors="coerce")

        return cls(
            symbol=payload["symbol"],
            interval=interval,
            source=Source(payload["source"]),
            captured_at=to_ist(datetime.fromisoformat(payload["captured_at"])),
            session_close=to_ist(datetime.fromisoformat(payload["session_close"])),
            df=df,
            instrument_key=payload.get("instrument_key"),
            schema=schema,
        )
