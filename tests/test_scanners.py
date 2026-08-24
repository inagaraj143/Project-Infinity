"""Scanner tests against synthetic OHLCV fixtures (spec 8).

Fixtures are engineered so each condition can be switched on and off in
isolation, which is what the spec asks for on the 13 trendline steps.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from infinity.market_clock import IST
from infinity.scanners import REGISTRY
from infinity.scanners.base import (
    InsufficientHistory,
    ScanContext,
    Signal,
    normalise_score,
)
from infinity.scanners.candle_50 import Candle50Scanner
from infinity.scanners.displacement import DisplacementScanner
from infinity.scanners.golden_zone import GoldenZoneScanner
from infinity.scanners.resistance_breakout import ResistanceBreakoutScanner
from infinity.scanners.trendlines import TrendlineScanner
from infinity.scanners.triangle import TriangleScanner


def frame(
    closes: list[float],
    volumes: list[float] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    opens: list[float] | None = None,
) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame(
        {
            "ts": pd.date_range("2024-01-01", periods=n, freq="D", tz=IST),
            "open": opens if opens is not None else [c * 0.995 for c in closes],
            "high": highs if highs is not None else [c * 1.01 for c in closes],
            "low": lows if lows is not None else [c * 0.99 for c in closes],
            "close": closes,
            "volume": volumes if volumes is not None else [1000.0] * n,
        }
    )


def uptrend(n: int = 300, start: float = 100.0, step: float = 0.5) -> pd.DataFrame:
    """Rising series with real pullbacks, so swing lows actually exist.

    A monotonically increasing series has no local minima at all, so
    ``argrelextrema`` returns nothing and no trendline can be built from it.
    Each 20-bar cycle rallies for 14 bars then retraces for 6, ending on a
    pullback low so the support line is 'active' for Step 5.
    """
    closes, price = [], start
    for i in range(n):
        price += step * 1.5 if i % 20 < 14 else -step * 1.2
        closes.append(price)
    return frame(closes)


def ctx(df: pd.DataFrame, symbol: str = "TEST", **kw) -> ScanContext:
    return ScanContext(symbol=symbol, daily=df, **kw)


class TestRegistry:
    def test_all_six_scanners_registered(self) -> None:
        assert set(REGISTRY) == {
            "golden_zone", "trendlines", "triangle",
            "resistance_breakout", "candle_50", "displacement",
        }

    def test_sections_are_unique(self) -> None:
        sections = [s.section for s in REGISTRY.values()]
        assert len(sections) == len(set(sections))


class TestNormaliseScore:
    """Review finding C1."""

    def test_rescales_a_110_point_total_onto_100(self) -> None:
        assert normalise_score(110, 110) == pytest.approx(100.0)
        assert normalise_score(90, 110) == pytest.approx(81.8, abs=0.1)

    def test_two_touch_and_three_touch_maxima_both_reach_100(self) -> None:
        """The whole point: 'Excellent' must mean the same at 2 and 3+ touches."""
        assert normalise_score(100, 100) == pytest.approx(100.0)
        assert normalise_score(110, 110) == pytest.approx(100.0)

    def test_clamps_and_handles_zero_maximum(self) -> None:
        assert normalise_score(200, 100) == 100.0
        assert normalise_score(-5, 100) == 0.0
        assert normalise_score(10, 0) == 0.0


class TestInsufficientHistory:
    @pytest.mark.parametrize("name", sorted(REGISTRY))
    def test_short_series_raises_rather_than_returning_no_match(self, name: str) -> None:
        """Spec 7: excluded with a reason, not silently treated as a non-match."""
        scanner = REGISTRY[name]
        with pytest.raises(InsufficientHistory) as exc:
            scanner.scan(ctx(uptrend(20)))
        assert "Insufficient History" in str(exc.value)
        assert exc.value.need == scanner.min_daily_bars


class TestResistanceBreakout:
    """Spec 3.5 -- all four conditions must hold."""

    def passing(self) -> pd.DataFrame:
        closes = [100.0 + i * 0.8 for i in range(80)]
        vols = [1000.0] * 79 + [2000.0]  # 2.0x >= 1.50x
        df = frame(closes, vols)
        df.loc[df.index[-1], "open"] = closes[-1] - 5  # green
        return df

    def test_all_conditions_met_is_bullish(self) -> None:
        row = ResistanceBreakoutScanner().scan(ctx(self.passing()))
        assert row.signal is Signal.BULLISH
        assert row.score == 100.0
        assert row.fields["Conditions Met"] == "4/4"

    def test_red_candle_fails_condition_4(self) -> None:
        df = self.passing()
        df.loc[df.index[-1], "open"] = float(df["close"].iloc[-1]) + 5.0
        row = ResistanceBreakoutScanner().scan(ctx(df))
        assert row.signal is Signal.REJECT
        assert row.fields["Green Candle"] == "No"

    def test_flat_volume_fails_condition_3(self) -> None:
        closes = [100.0 + i * 0.8 for i in range(80)]
        df = frame(closes, [1000.0] * 80)
        df.loc[df.index[-1], "open"] = closes[-1] - 5
        row = ResistanceBreakoutScanner().scan(ctx(df))
        assert row.signal is Signal.REJECT
        assert row.fields["Volume Ratio"] == 1.0

    def test_downtrend_fails_ceiling_and_ema(self) -> None:
        closes = [200.0 - i * 0.8 for i in range(80)]
        row = ResistanceBreakoutScanner().scan(ctx(frame(closes)))
        assert row.signal is Signal.REJECT

    def test_hlc_mode_uses_the_low(self) -> None:
        """Compared with ceil() off: the C3 rounding can mask the difference."""
        df = self.passing()
        ohc = ResistanceBreakoutScanner(
            typical_price_mode="ohc", use_ceil=False
        ).scan(ctx(df))
        hlc = ResistanceBreakoutScanner(
            typical_price_mode="hlc", use_ceil=False
        ).scan(ctx(df))
        assert ohc.fields["50D Ceiling"] != hlc.fields["50D Ceiling"]

    def test_ceil_can_be_disabled(self) -> None:
        df = self.passing()
        with_ceil = ResistanceBreakoutScanner(use_ceil=True).scan(ctx(df))
        without = ResistanceBreakoutScanner(use_ceil=False).scan(ctx(df))
        assert with_ceil.fields["50D Ceiling"] >= without.fields["50D Ceiling"]


class TestCandle50:
    """Spec 3.6 -- reference candle selection and midpoint rule."""

    def test_skips_dojis_to_find_a_decisive_candle(self) -> None:
        n = 40
        closes = [100.0] * n
        opens = [100.0] * n
        highs = [102.0] * n
        lows = [98.0] * n
        # Decisive bullish candle at index 30; 31-38 are dojis.
        opens[30], closes[30], highs[30], lows[30] = 98.0, 104.0, 105.0, 97.0
        closes[-1] = 103.0

        row = Candle50Scanner().scan(
            ctx(frame(closes, highs=highs, lows=lows, opens=opens))
        )
        assert row.fields["Reference Type"] == "Bullish"
        assert row.fields["Midpoint (50%)"] == pytest.approx(101.0)

    def test_above_midpoint_of_a_bullish_reference_is_bullish(self) -> None:
        n = 40
        opens = [100.0] * n
        closes = [100.0] * n
        highs = [110.0] * n
        lows = [90.0] * n
        opens[-2], closes[-2] = 92.0, 108.0  # decisive bullish, midpoint 100
        closes[-1] = 106.0  # well above

        row = Candle50Scanner().scan(
            ctx(frame(closes, highs=highs, lows=lows, opens=opens))
        )
        assert row.fields["Position"] == "Above"
        assert row.signal is Signal.BULLISH

    def test_confidence_is_bounded(self) -> None:
        row = Candle50Scanner().scan(ctx(uptrend(60)))
        assert 0.0 <= row.score <= 100.0

    def test_no_decisive_candle_is_neutral(self) -> None:
        n = 40
        df = frame([100.0] * n, highs=[101.0] * n, lows=[99.0] * n, opens=[100.0] * n)
        row = Candle50Scanner().scan(ctx(df))
        assert row.signal is Signal.NEUTRAL


class TestTriangle:
    """Spec 3.4 -- ascending and symmetrical classification."""

    def test_ascending_triangle_is_detected(self) -> None:
        n = 100
        closes, highs, lows = [], [], []
        for i in range(n):
            ceiling = 120.0
            floor = 90.0 + i * 0.25  # rising support
            mid = (ceiling + floor) / 2
            wobble = 4.0 if i % 6 < 3 else -4.0
            closes.append(mid + wobble)
            highs.append(ceiling if i % 6 == 0 else mid + 5)
            lows.append(floor if i % 6 == 3 else mid - 5)

        row = TriangleScanner().scan(ctx(frame(closes, highs=highs, lows=lows)))
        assert row.fields["Pattern"] in ("Ascending", "Symmetrical")
        assert row.fields["Sup Slope %/bar"] > 0

    def test_falling_support_is_not_a_bullish_triangle(self) -> None:
        n = 100
        closes = [120.0 - i * 0.3 for i in range(n)]
        row = TriangleScanner().scan(ctx(frame(closes)))
        assert row.fields["Pattern"] == "None"
        assert row.signal is Signal.REJECT

    def test_score_is_bounded(self) -> None:
        row = TriangleScanner().scan(ctx(uptrend(120)))
        assert 0.0 <= row.score <= 100.0


class TestDisplacement:
    """Spec 3.7 -- range, volume, directional close, imbalance zone."""

    def with_displacement(self, pos: int = 70, n: int = 90) -> pd.DataFrame:
        closes = [100.0] * n
        highs = [101.0] * n
        lows = [99.0] * n
        opens = [100.0] * n
        vols = [1000.0] * n

        # A huge bullish bar: wide range, 6x volume, close at the top.
        opens[pos], lows[pos], highs[pos], closes[pos] = 100.0, 99.5, 120.0, 119.5
        vols[pos] = 6000.0
        # Subsequent bars hold well above the zone.
        for i in range(pos + 1, n):
            opens[i] = lows[i] = 118.0
            closes[i] = highs[i] = 122.0
        return frame(closes, vols, highs=highs, lows=lows, opens=opens)

    def test_detects_a_bullish_displacement(self) -> None:
        row = DisplacementScanner().scan(ctx(self.with_displacement()))
        assert row.fields["Direction"] == "Bullish"
        assert row.fields["Range vs ATR"] >= 2.0
        assert row.fields["Volume vs SMA"] >= 3.0

    def test_quiet_series_finds_nothing(self) -> None:
        n = 90
        rng = np.random.default_rng(1)
        closes = list(100 + rng.standard_normal(n) * 0.2)
        row = DisplacementScanner().scan(ctx(frame(closes)))
        assert row.fields["Displacement"] == "None"
        assert row.signal is Signal.NEUTRAL

    def test_no_na_type_crash_on_zero_range_bars(self) -> None:
        """Regression: pd.NA upcast the series to object and broke every symbol."""
        n = 90
        closes = [100.0] * n
        df = frame(closes, highs=[100.0] * n, lows=[100.0] * n, opens=[100.0] * n)
        row = DisplacementScanner().scan(ctx(df))
        assert row is not None

    def test_fvg_and_spec_zone_modes_differ(self) -> None:
        df = self.with_displacement()
        fvg = DisplacementScanner(zone_mode="fvg").scan(ctx(df))
        spec = DisplacementScanner(zone_mode="spec").scan(ctx(df))
        assert fvg.fields["Imbalance Zone"] != spec.fields["Imbalance Zone"]


class TestTrendlineSteps:
    """Spec 3.3 -- each step exercised independently."""

    def scanner(self) -> TrendlineScanner:
        return TrendlineScanner()

    def test_step1_rejects_short_history(self) -> None:
        with pytest.raises(InsufficientHistory):
            self.scanner().step1_data_check(uptrend(100))

    def test_step2_finds_swings(self) -> None:
        assert len(self.scanner().step2_swings(uptrend(300), "Uptrend")) > 0

    def test_step3_uptrend_candidate_has_positive_slope(self) -> None:
        line = self.scanner().step3_build_candidate(uptrend(300), "Uptrend")
        assert line is not None
        assert line.slope > 0
        assert line.touches >= 2

    def test_step3_rejects_wrong_direction(self) -> None:
        """A rising series must not yield a descending resistance line."""
        assert self.scanner().step3_build_candidate(uptrend(300), "Downtrend") is None

    def test_step5_far_above_the_line_is_out_of_position(self) -> None:
        s = self.scanner()
        df = uptrend(300)
        line = s.step3_build_candidate(df, "Uptrend")
        df.loc[df.index[-1], "close"] = float(df["close"].iloc[-1]) * 3.0
        assert not s.step5_price_position(df, line, "Uptrend")

    def test_step8_rsi_band(self) -> None:
        s = self.scanner()
        assert not s.step8_rsi(frame([100.0 - i for i in range(60)]), "Uptrend")

    def test_step11_volume_spike_confirms(self) -> None:
        closes = [100.0 + i for i in range(60)]
        vols = [1000.0] * 59 + [5000.0]
        assert self.scanner().step11_volume(frame(closes, vols))

    def test_step12_max_is_110_at_three_touches(self) -> None:
        """Documents review finding C1 directly."""
        from infinity.scanners.trendlines import StepResults, Trendline

        s = self.scanner()
        st = StepResults(
            line=Trendline(1.0, 0.0, touches=3, direction="Uptrend", start_pos=0),
            validated=True, daily=True, intraday=True, intraday_available=True,
            rsi_ok=True, macd_ok=True, ema_ok=True, volume_ok=True,
        )
        raw, maximum = s.step12_score(st)
        assert raw == 110.0
        assert maximum == 110.0
        assert normalise_score(raw, maximum) == 100.0

    def test_step12_max_is_100_at_two_touches(self) -> None:
        from infinity.scanners.trendlines import StepResults, Trendline

        s = self.scanner()
        st = StepResults(
            line=Trendline(1.0, 0.0, touches=2, direction="Uptrend", start_pos=0),
            validated=True, daily=True, intraday=True, intraday_available=True,
            rsi_ok=True, macd_ok=True, ema_ok=True, volume_ok=True,
        )
        raw, maximum = s.step12_score(st)
        assert raw == 100.0 and maximum == 100.0

    def test_step12_excludes_intraday_points_when_unavailable(self) -> None:
        """C2: a symbol must not be penalised for a feed we never fetched."""
        from infinity.scanners.trendlines import StepResults, Trendline

        s = self.scanner()
        st = StepResults(
            line=Trendline(1.0, 0.0, touches=2, direction="Uptrend", start_pos=0),
            validated=True, daily=True, intraday=False, intraday_available=False,
            rsi_ok=True, macd_ok=True, ema_ok=True, volume_ok=True,
        )
        raw, maximum = s.step12_score(st)
        assert maximum == 90.0
        assert normalise_score(raw, maximum) == 100.0

    def test_step7_reports_unavailable_for_short_intraday(self) -> None:
        confirmed, available = self.scanner().step7_intraday_confirmation(
            frame([100.0] * 50), "Uptrend"
        )
        assert not confirmed and not available

    @pytest.mark.parametrize(
        "score,expected",
        [
            (95, "Excellent Setup"),
            (85, "High Probability Setup"),
            (75, "Good Setup"),
            (65, "Moderate Setup"),
            (50, "Reject"),
        ],
    )
    def test_step13_classification_bands(self, score: float, expected: str) -> None:
        assert self.scanner().step13_classify(score) == expected

    def test_full_scan_emits_all_13_columns(self) -> None:
        row = self.scanner().scan(ctx(uptrend(300)))
        for col in (
            "Trend Direction", "Daily Status", "15-Min Status", "Touches",
            "Distance %", "RSI Value", "MACD Status", "EMA Alignment",
            "Volume Confirmation", "Strength Score", "Overall Signal",
            "Trendline Price",
        ):
            assert col in row.fields, col

    def test_price_far_from_any_line_is_rejected(self) -> None:
        """Step 5 is a filter: an inactive line must not qualify."""
        df = uptrend(300)
        df.loc[df.index[-1], "close"] = float(df["close"].iloc[-1]) * 5.0
        row = self.scanner().scan(ctx(df))
        assert row.signal is Signal.REJECT


class TestGoldenZone:
    """Spec 3.1 -- Fibonacci zone plus composite scoring."""

    def test_reports_component_breakdown(self) -> None:
        row = GoldenZoneScanner().scan(ctx(uptrend(250)))
        for key in ("Trend (30)", "Zone (30)", "Volume (15)",
                    "Candle (15)", "Sector (10)"):
            assert key in row.fields

    def test_zone_component_peaks_inside_the_golden_zone(self) -> None:
        s = GoldenZoneScanner()
        assert s._zone_component(0.559) == pytest.approx(30.0)
        assert s._zone_component(0.500) == pytest.approx(30.0)
        assert s._zone_component(0.618) == pytest.approx(30.0)
        assert s._zone_component(0.20) < 30.0
        assert s._zone_component(0.95) < 30.0

    def test_records_which_source_each_component_used(self) -> None:
        """Review finding C5: the fallback path must be visible, not silent."""
        row = GoldenZoneScanner().scan(ctx(uptrend(250)))
        assert row.fields["Trend Source"] == "fallback"
        assert row.fields["Candle Source"] == "fallback"

    def test_uses_upstream_when_present(self) -> None:
        upstream = {
            "trendlines": ScanRow_stub(Signal.BULLISH, 90.0),
            "candle_50": ScanRow_stub(Signal.BULLISH, 80.0),
        }
        row = GoldenZoneScanner().scan(ctx(uptrend(250), upstream=upstream))
        assert row.fields["Trend Source"] == "3.3"
        assert row.fields["Candle Source"] == "3.6"
        assert row.fields["Trend (30)"] == pytest.approx(27.0)  # 90 * 0.30
        assert row.fields["Candle (15)"] == pytest.approx(12.0)  # 80 * 0.15

    def test_sector_points_come_from_the_runner(self) -> None:
        row = GoldenZoneScanner().scan(
            ctx(uptrend(250), upstream={"_sector_rank": ScanRow_stub(Signal.NEUTRAL, 10.0)})
        )
        assert row.fields["Sector (10)"] == 10.0


def ScanRow_stub(signal: Signal, score: float):
    from infinity.scanners.base import ScanRow

    return ScanRow(symbol="X", signal=signal, score=score)
