"""Market clock tests -- the freshness boundary the whole cache rests on."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from infinity.market_clock import (
    SessionState,
    is_trading_day,
    last_session_close,
    market_status,
    next_session_open,
    previous_trading_day,
    session_state,
    to_ist,
)
from tests.conftest import ist

# 2026-08-19 Wed, 08-20 Thu, 08-21 Fri, 08-22 Sat, 08-23 Sun,
# 08-24 Mon (fixture holiday), 08-25 Tue.


class TestSessionState:
    @pytest.mark.parametrize(
        "when,expected",
        [
            ("2026-08-21 09:14", SessionState.CLOSED),  # one minute pre-open
            ("2026-08-21 09:15", SessionState.LIVE),  # open boundary, inclusive
            ("2026-08-21 12:00", SessionState.LIVE),
            ("2026-08-21 15:30", SessionState.LIVE),  # close boundary, inclusive
            ("2026-08-21 15:31", SessionState.CLOSED),
            ("2026-08-22 12:00", SessionState.CLOSED),  # Saturday
            ("2026-08-23 12:00", SessionState.CLOSED),  # Sunday
            ("2026-08-24 12:00", SessionState.CLOSED),  # Monday holiday
            ("2026-08-25 12:00", SessionState.LIVE),  # Tuesday
        ],
    )
    def test_boundaries(self, when: str, expected: SessionState) -> None:
        assert session_state(ist(when)) is expected

    def test_holiday_is_not_a_trading_day(self) -> None:
        assert not is_trading_day(date(2026, 8, 24))
        assert is_trading_day(date(2026, 8, 25))

    def test_weekend_is_not_a_trading_day(self) -> None:
        assert not is_trading_day(date(2026, 8, 22))
        assert not is_trading_day(date(2026, 8, 23))


class TestTimezoneDiscipline:
    def test_naive_datetime_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="Naive datetime"):
            to_ist(datetime(2026, 8, 21, 12, 0))

    def test_utc_input_converts_correctly(self) -> None:
        # 10:00 UTC == 15:30 IST -> still LIVE on the inclusive close boundary.
        utc = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
        assert session_state(utc) is SessionState.LIVE

    def test_utc_server_midnight_is_closed(self) -> None:
        # A UTC-hosted app at 00:30 UTC Saturday is 06:00 IST Saturday.
        utc = datetime(2026, 8, 22, 0, 30, tzinfo=UTC)
        assert session_state(utc) is SessionState.CLOSED


class TestLastSessionClose:
    """The predicate that decides whether a snapshot is reused or refetched."""

    def test_during_session_points_at_previous_close(self) -> None:
        # Mid-session Friday: today's close has not settled yet.
        assert last_session_close(ist("2026-08-21 12:00")) == ist("2026-08-20 15:30")

    def test_at_exactly_1530_still_previous(self) -> None:
        # Strict '>' -- at 15:30 the session is still live.
        assert last_session_close(ist("2026-08-21 15:30")) == ist("2026-08-20 15:30")

    def test_one_minute_after_close_rolls_forward(self) -> None:
        assert last_session_close(ist("2026-08-21 15:31")) == ist("2026-08-21 15:30")

    @pytest.mark.parametrize(
        "when",
        [
            "2026-08-21 18:00",  # Friday evening
            "2026-08-22 10:00",  # Saturday
            "2026-08-23 23:59",  # Sunday
            "2026-08-24 12:00",  # Monday holiday
            "2026-08-25 09:00",  # Tuesday pre-open
        ],
    )
    def test_stays_pinned_across_the_long_weekend(self, when: str) -> None:
        """A Friday-evening snapshot must stay fresh until Tuesday's close.

        This is the scenario the whole design exists for: one fetch on Friday,
        then four days of zero API calls.
        """
        assert last_session_close(ist(when)) == ist("2026-08-21 15:30")

    def test_advances_after_the_next_real_close(self) -> None:
        assert last_session_close(ist("2026-08-25 16:00")) == ist("2026-08-25 15:30")

    def test_before_open_uses_previous_trading_day(self) -> None:
        assert last_session_close(ist("2026-08-21 08:00")) == ist("2026-08-20 15:30")


class TestNextSessionOpen:
    def test_before_open_returns_today(self) -> None:
        assert next_session_open(ist("2026-08-21 07:00")) == ist("2026-08-21 09:15")

    def test_after_close_skips_weekend_and_holiday(self) -> None:
        assert next_session_open(ist("2026-08-21 16:00")) == ist("2026-08-25 09:15")

    def test_previous_trading_day_skips_holiday(self) -> None:
        assert previous_trading_day(date(2026, 8, 25)) == date(2026, 8, 21)


class TestMarketStatus:
    def test_live_badge(self) -> None:
        s = market_status(ist("2026-08-21 11:00"))
        assert s.is_live
        assert s.badge_label == "LIVE MARKET"
        assert s.badge_icon == "\U0001f7e2"

    def test_closed_badge(self) -> None:
        s = market_status(ist("2026-08-22 11:00"))
        assert not s.is_live
        assert s.badge_label == "HISTORICAL DATA"
        assert s.badge_icon == "\U0001f319"

    def test_holiday_name_surfaces_in_detail(self) -> None:
        s = market_status(ist("2026-08-24 11:00"))
        assert s.holiday_name == "Test Monday Holiday"
        assert "Test Monday Holiday" in s.detail

    def test_fixture_calendar_reports_verified(self) -> None:
        assert market_status(ist("2026-08-21 11:00")).calendar_verified is True


class TestShippedCalendar:
    def test_shipped_calendar_parses(self, monkeypatch) -> None:
        """The real data/nse_holidays.json must load and flag itself unverified."""
        from infinity import market_clock

        market_clock.load_calendar.cache_clear()
        monkeypatch.setattr(
            market_clock,
            "_HOLIDAY_FILE",
            market_clock.Path(__file__).resolve().parent.parent / "data" / "nse_holidays.json",
        )
        cal = market_clock.load_calendar()
        market_clock.load_calendar.cache_clear()

        assert len(cal.holidays) >= 10
        assert date(2026, 1, 26) in cal
        # Guards the honesty flag: flipping this to True is a deliberate act
        # that must follow checking the official NSE circular.
        assert cal.verified is False, "set verified=true only after checking NSE circular"

    def test_missing_calendar_degrades_to_weekend_only(self, monkeypatch, tmp_path) -> None:
        from infinity import market_clock

        market_clock.load_calendar.cache_clear()
        monkeypatch.setattr(market_clock, "_HOLIDAY_FILE", tmp_path / "does_not_exist.json")
        cal = market_clock.load_calendar()
        market_clock.load_calendar.cache_clear()

        assert cal.holidays == {}
        assert cal.source == "missing"
