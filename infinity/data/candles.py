"""Candle assembly helpers (spec 2.1 live-session integration).

Historical-candle endpoints generally publish a day's bar only once the session
has settled, so during and shortly after a session the daily series is missing
today. ``ensure_today_candle`` folds today's intraday bars into a single daily
bar and appends it, which is what lets analysis reflect current values
post-close.

These are pure functions over dataframes: no network, no clock reads beyond an
injectable ``as_of``, so they are directly testable.
"""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd

from infinity.data.models import OHLCV_COLUMNS
from infinity.market_clock import is_trading_day, now_ist, to_ist

log = logging.getLogger(__name__)


def aggregate_to_daily_bar(intraday: pd.DataFrame, session_date: pd.Timestamp) -> pd.DataFrame:
    """Collapse one session's intraday bars into a single daily OHLCV row."""
    if intraday is None or intraday.empty:
        return pd.DataFrame(columns=OHLCV_COLUMNS)

    day = intraday[intraday["ts"].dt.normalize() == session_date.normalize()]
    if day.empty:
        return pd.DataFrame(columns=OHLCV_COLUMNS)

    day = day.sort_values("ts")
    return pd.DataFrame(
        [
            {
                "ts": session_date.normalize(),
                "open": float(day["open"].iloc[0]),
                "high": float(day["high"].max()),
                "low": float(day["low"].min()),
                "close": float(day["close"].iloc[-1]),
                "volume": float(day["volume"].sum()),
            }
        ]
    )


def ensure_today_candle(
    daily: pd.DataFrame,
    intraday: pd.DataFrame | None = None,
    as_of: datetime | None = None,
) -> pd.DataFrame:
    """Append (or refresh) today's daily bar from intraday data.

    No-ops when today is not a trading day, when no intraday bars for today are
    available, or when the daily series already carries a bar for today that
    intraday cannot improve on.
    """
    if daily is None or daily.empty:
        return daily if daily is not None else pd.DataFrame(columns=OHLCV_COLUMNS)

    now = to_ist(as_of) if as_of else now_ist()
    today = pd.Timestamp(now.date(), tz=now.tzinfo)

    if not is_trading_day(now.date()):
        return daily

    synthetic = aggregate_to_daily_bar(intraday, today) if intraday is not None else None
    if synthetic is None or synthetic.empty:
        return daily

    out = daily[daily["ts"].dt.normalize() != today.normalize()]
    out = pd.concat([out, synthetic], ignore_index=True)
    out = out.sort_values("ts").reset_index(drop=True)
    log.debug("today's candle folded in at %s", today.date())
    return out


def latest_bar(df: pd.DataFrame) -> pd.Series | None:
    """Most recent bar, or None for an empty frame."""
    if df is None or df.empty:
        return None
    return df.sort_values("ts").iloc[-1]


def has_min_bars(df: pd.DataFrame, minimum: int) -> bool:
    """Spec 7: a short series is excluded as 'Insufficient History', not a non-match."""
    return df is not None and len(df) >= minimum
