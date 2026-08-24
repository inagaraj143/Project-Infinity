"""UpstoxProvider tests (ADR 0002).

Offline: every HTTP call is stubbed. The point is to lock in the endpoint
shapes and the no-auth behaviour that were measured against the live API, so a
regression here shows up as a test failure rather than a silent fallback to
yfinance.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from infinity.config import AppSettings, UpstoxCredentials
from infinity.data.instruments import Instrument, InstrumentMaster
from infinity.data.models import Interval, Source
from infinity.data.providers.base import ProviderError, SymbolNotFound
from infinity.data.providers.upstox_provider import MAX_INTRADAY_DAYS, UpstoxProvider
from infinity.market_clock import now_ist

CANDLES = [
    ["2026-08-21T00:00:00+05:30", 1314.0, 1316.0, 1308.0, 1316.0, 5434871, 0],
    ["2026-08-20T00:00:00+05:30", 1315.5, 1316.5, 1307.0, 1313.2, 5546227, 0],
]


class FakeResponse:
    def __init__(self, status: int = 200, payload: dict | None = None, text: str = ""):
        self.status_code = status
        self._payload = payload if payload is not None else {"data": {"candles": CANDLES}}
        self.text = text or "{}"

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self) -> dict:
        return self._payload


class Recorder:
    """Captures every request so URL shape and headers can be asserted."""

    def __init__(self, responses: list[FakeResponse] | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses or []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, dict(headers or {})))
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse()

    @property
    def urls(self) -> list[str]:
        return [u for u, _ in self.calls]

    @property
    def headers(self) -> list[dict]:
        return [h for _, h in self.calls]


def master() -> InstrumentMaster:
    inst = Instrument("RELIANCE", "NSE_EQ|INE002A01018", "INE002A01018", "Reliance")
    return InstrumentMaster({inst.symbol: inst}, {inst.isin: inst}, now_ist())


def provider(token: str | None = None, recorder: Recorder | None = None) -> UpstoxProvider:
    p = UpstoxProvider(settings=AppSettings(), master=master())
    p.creds = UpstoxCredentials(api_key="k", api_secret="s", access_token=token)
    p._session = recorder or Recorder()
    return p


class TestAvailability:
    def test_available_without_a_token(self) -> None:
        """ADR 0002: candle endpoints are unauthenticated."""
        p = provider(token=None)
        assert p.is_available()
        assert not p.has_token

    def test_available_with_a_token_too(self) -> None:
        assert provider(token="abc").is_available()

    def test_disabled_by_explicit_env_flag(self, monkeypatch) -> None:
        monkeypatch.setenv("INFINITY_DISABLE_UPSTOX", "1")
        p = UpstoxProvider(settings=AppSettings(), master=master())
        assert not p.is_available()

    def test_enabled_under_ci(self, monkeypatch) -> None:
        """Regression: this used to be disabled whenever CI=true."""
        monkeypatch.setenv("CI", "true")
        monkeypatch.delenv("INFINITY_DISABLE_UPSTOX", raising=False)
        p = UpstoxProvider(settings=AppSettings(), master=master())
        assert p.is_available()


class TestEndpointShapes:
    def test_daily_uses_v2_day_path(self) -> None:
        rec = Recorder()
        p = provider(recorder=rec)
        p.fetch_bars("RELIANCE", Interval.DAY, date(2026, 6, 1), date(2026, 8, 21))

        assert len(rec.urls) == 1
        assert rec.urls[0] == (
            "https://api.upstox.com/v2/historical-candle/"
            "NSE_EQ|INE002A01018/day/2026-08-21/2026-06-01"
        )

    def test_intraday_uses_v3_minutes_path(self) -> None:
        """v2 '/minute/' returns 400; only v3 '/minutes/{n}/' works."""
        rec = Recorder()
        p = provider(recorder=rec)
        p.fetch_bars("RELIANCE", Interval.MIN_15, date(2026, 8, 1), date(2026, 8, 21))

        assert "/v3/historical-candle/" in rec.urls[0]
        assert "/minutes/15/" in rec.urls[0]
        assert "/minute/" not in rec.urls[0].replace("/minutes/", "/")

    def test_intraday_also_fetches_the_current_session(self) -> None:
        """Spec 2.1: history stops at the last settled session."""
        rec = Recorder()
        p = provider(recorder=rec)
        p.fetch_bars("RELIANCE", Interval.MIN_15, date(2026, 8, 1), date(2026, 8, 21))

        assert len(rec.urls) == 2
        assert "/historical-candle/intraday/" in rec.urls[1]

    def test_current_session_endpoint(self) -> None:
        rec = Recorder()
        p = provider(recorder=rec)
        p.fetch_current_session("RELIANCE", Interval.MIN_15)

        assert rec.urls[0] == (
            "https://api.upstox.com/v3/historical-candle/intraday/"
            "NSE_EQ|INE002A01018/minutes/15"
        )

    def test_current_session_rejects_daily(self) -> None:
        with pytest.raises(ValueError, match="intraday interval"):
            provider().fetch_current_session("RELIANCE", Interval.DAY)

    def test_intraday_start_is_clamped_to_the_api_limit(self) -> None:
        """60 days returns UDAPI1148; 31 is the measured maximum."""
        rec = Recorder()
        p = provider(recorder=rec)
        end = date(2026, 8, 21)
        p.fetch_bars("RELIANCE", Interval.MIN_15, end - timedelta(days=365), end)

        earliest = (end - timedelta(days=MAX_INTRADAY_DAYS)).isoformat()
        assert earliest in rec.urls[0]

    def test_daily_range_is_not_clamped(self) -> None:
        rec = Recorder()
        p = provider(recorder=rec)
        p.fetch_bars("RELIANCE", Interval.DAY, date(2014, 1, 1), date(2026, 8, 21))
        assert "2014-01-01" in rec.urls[0]


class TestAuthHeaders:
    def test_no_authorization_header_without_a_token(self) -> None:
        rec = Recorder()
        provider(token=None, recorder=rec).fetch_bars(
            "RELIANCE", Interval.DAY, date(2026, 6, 1), date(2026, 8, 21)
        )
        assert "Authorization" not in rec.headers[0]

    def test_token_is_sent_when_present(self) -> None:
        rec = Recorder()
        provider(token="abc123", recorder=rec).fetch_bars(
            "RELIANCE", Interval.DAY, date(2026, 6, 1), date(2026, 8, 21)
        )
        assert rec.headers[0]["Authorization"] == "Bearer abc123"

    def test_expired_token_retries_unauthenticated(self) -> None:
        """A dead token must not fail a scan over a credential we don't need."""
        rec = Recorder([FakeResponse(status=401), FakeResponse()])
        df = provider(token="expired", recorder=rec).fetch_bars(
            "RELIANCE", Interval.DAY, date(2026, 6, 1), date(2026, 8, 21)
        )

        assert len(rec.calls) == 2
        assert "Authorization" in rec.headers[0]
        assert "Authorization" not in rec.headers[1], "retry must drop the header"
        assert len(df) == 2


class TestParsing:
    def test_maps_upstox_columns_and_sorts_ascending(self) -> None:
        df = provider().fetch_bars(
            "RELIANCE", Interval.DAY, date(2026, 6, 1), date(2026, 8, 21)
        )
        assert list(df.columns) == ["ts", "open", "high", "low", "close", "volume"]
        assert df["ts"].is_monotonic_increasing, "Upstox returns newest-first"
        assert df["close"].iloc[-1] == 1316.0
        assert df["volume"].iloc[-1] == 5434871

    def test_open_interest_column_is_dropped(self) -> None:
        df = provider().fetch_bars(
            "RELIANCE", Interval.DAY, date(2026, 6, 1), date(2026, 8, 21)
        )
        assert "open_interest" not in df.columns
        assert len(df.columns) == 6

    def test_empty_candles_yield_an_empty_frame(self) -> None:
        rec = Recorder([FakeResponse(payload={"data": {"candles": []}})])
        df = provider(recorder=rec).fetch_bars(
            "RELIANCE", Interval.DAY, date(2026, 6, 1), date(2026, 8, 21)
        )
        assert df.empty

    def test_source_is_upstox_not_fallback(self) -> None:
        assert UpstoxProvider.source is Source.UPSTOX
        assert not Source.UPSTOX.is_fallback


class TestErrors:
    def test_unmapped_symbol_raises_symbol_not_found(self) -> None:
        with pytest.raises(SymbolNotFound, match="instrument_key"):
            provider().fetch_bars("NOSUCH", Interval.DAY, date(2026, 6, 1), date(2026, 8, 21))

    def test_http_400_raises_provider_error(self) -> None:
        rec = Recorder(
            [FakeResponse(status=400, text='{"errors":[{"message":"Invalid date range"}]}')]
        )
        with pytest.raises(ProviderError, match="400"):
            provider(recorder=rec).fetch_bars(
                "RELIANCE", Interval.MIN_15, date(2026, 8, 1), date(2026, 8, 21)
            )

    def test_non_json_response_raises_provider_error(self) -> None:
        class BadJSON(FakeResponse):
            def json(self):
                raise ValueError("not json")

        rec = Recorder([BadJSON(text="<html>502</html>")])
        with pytest.raises(ProviderError, match="non-JSON"):
            provider(recorder=rec).fetch_bars(
                "RELIANCE", Interval.DAY, date(2026, 6, 1), date(2026, 8, 21)
            )


class TestResolverChain:
    def test_upstox_is_primary_and_yfinance_the_fallback(self) -> None:
        """Spec 2.1's intended ordering, restored by ADR 0002."""
        from infinity.data.resolver import build_default_resolver

        chain = [p.name for p in build_default_resolver().providers]
        assert chain == ["upstox", "yfinance"]

    def test_chain_is_yfinance_only_when_upstox_disabled(self) -> None:
        from infinity.data.resolver import build_default_resolver

        chain = [p.name for p in build_default_resolver(allow_upstox=False).providers]
        assert chain == ["yfinance"]
