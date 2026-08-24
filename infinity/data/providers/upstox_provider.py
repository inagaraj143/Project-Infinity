"""Upstox API provider (spec 2.1, 2.2, 2.3).

**Historical candles need no authentication.** Verified against the live API on
2026-08-24: the historical and intraday candle endpoints return 200 with no
``Authorization`` header at all, and also with a deliberately invalid one, while
``/v2/user/profile`` correctly returns 401. This supersedes the original
reasoning in ADR 0001 -- see ADR 0002.

Endpoint map (all unauthenticated):

===================  ==========================================================
Daily history        ``/v2/historical-candle/{key}/day/{to}/{from}``
Intraday history     ``/v3/historical-candle/{key}/minutes/{n}/{to}/{from}``
Current session      ``/v3/historical-candle/intraday/{key}/minutes/{n}``
===================  ==========================================================

The v2 path for minute data (``/minute/{to}/{from}``) returns HTTP 400; only
the v3 ``/minutes/{n}/`` form works, so intraday is v3-only.

A token is still accepted and sent when present, because account endpoints
(profile, funds, live quotes) do require one. Nothing this module needs it for.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import date, timedelta

import pandas as pd
import requests

from infinity.config import AppSettings, load_upstox_credentials
from infinity.data.instruments import InstrumentMaster, get_master
from infinity.data.models import Interval, Source
from infinity.data.providers.base import (
    ProviderError,
    SymbolNotFound,
    TokenExpired,
    clean_dataframe,
)

log = logging.getLogger(__name__)

BASE_URL = "https://api.upstox.com"

# Minutes per interval for the v3 path segment.
_MINUTES = {Interval.MIN_15: 15, Interval.MIN_5: 5}

# Measured against the live API on 2026-08-24: 31 calendar days is accepted for
# every intraday interval, 60 returns UDAPI1148 "Invalid date range". At 15-min
# that is ~600 bars (24 sessions x 25 candles), comfortably past the 200 that
# spec 3.3 Step 7 needs, so a single window suffices and no paging is required.
MAX_INTRADAY_DAYS = 31


class UpstoxProvider:
    name = "upstox"
    source = Source.UPSTOX

    def __init__(
        self,
        settings: AppSettings | None = None,
        master: InstrumentMaster | None = None,
    ) -> None:
        self.settings = settings or AppSettings()
        self.creds = load_upstox_credentials()
        self._session = requests.Session()
        self._master = master

    def is_available(self) -> bool:
        """Available without a token: candle endpoints are unauthenticated."""
        return self.settings.allow_upstox

    @property
    def has_token(self) -> bool:
        return self.creds.is_authenticated

    # -- instrument mapping ------------------------------------------------

    @property
    def master(self) -> InstrumentMaster:
        """Lazily loaded so constructing the provider never hits the network."""
        if self._master is None:
            self._master = get_master()
        return self._master

    def instrument_key(self, symbol: str) -> str:
        key = self.master.instrument_key(symbol)
        if not key:
            # Spec 7: excluded with a reason and counted, never silently dropped.
            raise SymbolNotFound(f"no Upstox instrument_key for {symbol}")
        return key

    # -- fetching ----------------------------------------------------------

    def fetch_bars(
        self,
        symbol: str,
        interval: Interval,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        key = self.instrument_key(symbol)

        if interval is Interval.DAY:
            url = f"{BASE_URL}/v2/historical-candle/{key}/day/{end}/{start}"
        else:
            earliest = end - timedelta(days=MAX_INTRADAY_DAYS)
            if start < earliest:
                log.debug("clamping %s intraday start %s -> %s", symbol, start, earliest)
                start = earliest
            minutes = _MINUTES[interval]
            url = (
                f"{BASE_URL}/v3/historical-candle/{key}"
                f"/minutes/{minutes}/{end}/{start}"
            )

        candles = self._candles(self._get(url))

        # The historical endpoints stop at the last settled session, so append
        # the current session from the intraday endpoint (spec 2.1).
        if interval is not Interval.DAY:
            candles += self._candles(self._get(self._intraday_url(key, interval)))

        return self._to_frame(candles, interval)

    def fetch_current_session(self, symbol: str, interval: Interval) -> pd.DataFrame:
        """Today's candles only. Empty outside market hours."""
        if interval is Interval.DAY:
            raise ValueError("current-session fetch requires an intraday interval")
        key = self.instrument_key(symbol)
        return self._to_frame(
            self._candles(self._get(self._intraday_url(key, interval))), interval
        )

    @staticmethod
    def _intraday_url(key: str, interval: Interval) -> str:
        return f"{BASE_URL}/v3/historical-candle/intraday/{key}/minutes/{_MINUTES[interval]}"

    @staticmethod
    def _candles(payload: dict) -> list[list]:
        return list(payload.get("data", {}).get("candles", []) or [])

    @staticmethod
    def _to_frame(candles: list[list], interval: Interval) -> pd.DataFrame:
        if not candles:
            return clean_dataframe(pd.DataFrame(), interval)
        # Upstox: [timestamp, open, high, low, close, volume, open_interest]
        df = pd.DataFrame(
            [c[:6] for c in candles],
            columns=["ts", "open", "high", "low", "close", "volume"],
        )
        return clean_dataframe(df, interval)

    # -- transport ---------------------------------------------------------

    def _get(self, url: str) -> dict:
        """GET with exponential backoff on 429/5xx (spec 2.3)."""
        headers = {"Accept": "application/json"}
        if self.has_token:
            headers["Authorization"] = f"Bearer {self.creds.access_token}"

        last_exc: Exception | None = None

        for attempt in range(self.settings.max_retries):
            try:
                resp = self._session.get(
                    url, headers=headers, timeout=self.settings.request_timeout_sec
                )
            except requests.RequestException as exc:
                last_exc = exc
                self._sleep(attempt)
                continue

            if resp.status_code == 401:
                # Candle endpoints do not authenticate, so a 401 here means an
                # expired token was sent and rejected. Retry once without it
                # rather than failing a scan over a credential we do not need.
                if "Authorization" in headers:
                    log.warning(
                        "Upstox rejected the access token (expired?); retrying "
                        "unauthenticated -- candle endpoints do not require one"
                    )
                    headers.pop("Authorization")
                    continue
                raise TokenExpired("Upstox returned 401 without an Authorization header")

            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = ProviderError(f"HTTP {resp.status_code}")
                self._sleep(attempt)
                continue
            if not resp.ok:
                raise ProviderError(f"Upstox HTTP {resp.status_code}: {resp.text[:200]}")

            try:
                return resp.json()
            except ValueError as exc:
                raise ProviderError(f"Upstox returned non-JSON: {resp.text[:200]}") from exc

        raise ProviderError(
            f"Upstox request failed after {self.settings.max_retries} attempts: {last_exc}"
        )

    @staticmethod
    def _sleep(attempt: int) -> None:
        # Full jitter: avoids a synchronised retry storm across a 500-symbol scan.
        time.sleep(random.uniform(0, min(2**attempt, 8)))
