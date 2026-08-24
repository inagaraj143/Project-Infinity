"""yfinance provider -- NSE equities via the ``.NS`` Yahoo suffix.

Deliberately promoted from "fallback only" (spec 2.1) to the *primary* feed for
the unattended EOD batch job. Reason: Upstox mints an access token through an
interactive OAuth redirect and expires it daily around 03:30 IST, so no cron
can hold one. yfinance needs no auth and works in GitHub Actions.

Upstox stays primary for live intraday, where the user is present to log in.
See docs/adr/0001-feed-strategy.md.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd

from infinity.data.models import Interval, Source
from infinity.data.providers.base import ProviderError, SymbolNotFound, clean_dataframe

log = logging.getLogger(__name__)

# Yahoo caps intraday history: 60d for sub-hourly intervals.
_YF_INTERVAL = {Interval.DAY: "1d", Interval.MIN_15: "15m", Interval.MIN_5: "5m"}
_YF_MAX_INTRADAY_DAYS = 59


class YFinanceProvider:
    name = "yfinance"
    source = Source.YFINANCE

    def __init__(self, suffix: str = ".NS") -> None:
        self.suffix = suffix

    def is_available(self) -> bool:
        try:
            import yfinance  # noqa: F401
        except ModuleNotFoundError:
            return False
        return True

    def ticker_for(self, symbol: str) -> str:
        s = symbol.upper().strip()
        return s if s.endswith(self.suffix) else f"{s}{self.suffix}"

    def fetch_bars(
        self,
        symbol: str,
        interval: Interval,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        try:
            import yfinance as yf
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise ProviderError("yfinance is not installed") from exc

        if interval.is_intraday:
            earliest = date.today() - timedelta(days=_YF_MAX_INTRADAY_DAYS)
            if start < earliest:
                log.debug("clamping %s intraday start %s -> %s", symbol, start, earliest)
                start = earliest

        ticker = self.ticker_for(symbol)
        try:
            raw = yf.download(
                tickers=ticker,
                start=start.isoformat(),
                # yfinance treats `end` as exclusive.
                end=(end + timedelta(days=1)).isoformat(),
                interval=_YF_INTERVAL[interval],
                # Spec 2.4 wants raw exchange prices so ATH matches the terminal.
                auto_adjust=False,
                actions=False,
                progress=False,
                threads=False,
            )
        except Exception as exc:  # yfinance raises a wide variety of network errors
            raise ProviderError(f"yfinance download failed for {ticker}: {exc}") from exc

        if raw is None or raw.empty:
            raise SymbolNotFound(f"yfinance returned no bars for {ticker}")

        return clean_dataframe(raw.reset_index(), interval)
