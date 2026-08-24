"""Indicator tests against hand-computed reference values (spec 8)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from infinity.indicators import (
    add_indicators,
    atr,
    body_ratio,
    close_position_in_range,
    ema,
    higher_highs_higher_lows,
    lower_highs_lower_lows,
    macd,
    rsi,
    sma,
    swing_highs,
    swing_lows,
    true_range,
    wilder_smooth,
)


class TestEMA:
    def test_matches_hand_computed_recursion(self) -> None:
        """alpha = 2/(3+1) = 0.5; seeded on the SMA of the first 3 values."""
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        out = ema(s, 3)

        assert out.iloc[:2].isna().all(), "warm-up must be NaN, not truncated"
        assert out.iloc[2] == pytest.approx(2.0)  # seed = mean(1,2,3)
        assert out.iloc[3] == pytest.approx(2.0 + 0.5 * (4.0 - 2.0))  # 3.0
        assert out.iloc[4] == pytest.approx(3.0 + 0.5 * (5.0 - 3.0))  # 4.0

    def test_constant_series_converges_to_the_constant(self) -> None:
        out = ema(pd.Series([7.0] * 50), 10)
        assert out.iloc[-1] == pytest.approx(7.0)

    def test_rejects_a_non_positive_period(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            ema(pd.Series([1.0]), 0)


class TestSMA:
    def test_simple_mean_over_the_window(self) -> None:
        out = sma(pd.Series([1.0, 2.0, 3.0, 4.0]), 2)
        assert out.iloc[0] != out.iloc[0] or pd.isna(out.iloc[0])
        assert out.iloc[1] == pytest.approx(1.5)
        assert out.iloc[3] == pytest.approx(3.5)


class TestWilderSmoothing:
    def test_alpha_is_one_over_n(self) -> None:
        s = pd.Series([10.0, 20.0, 30.0, 40.0])
        out = wilder_smooth(s, 2)
        # seed = mean(10,20) = 15; then 15 + 0.5*(30-15) = 22.5; 22.5+0.5*(40-22.5)=31.25
        assert out.iloc[1] == pytest.approx(15.0)
        assert out.iloc[2] == pytest.approx(22.5)
        assert out.iloc[3] == pytest.approx(31.25)


class TestRSI:
    def test_all_gains_gives_100(self) -> None:
        s = pd.Series(np.arange(1.0, 30.0))
        assert rsi(s, 14).iloc[-1] == pytest.approx(100.0)

    def test_all_losses_gives_0(self) -> None:
        s = pd.Series(np.arange(30.0, 1.0, -1.0))
        assert rsi(s, 14).iloc[-1] == pytest.approx(0.0)

    def test_alternating_equal_moves_sits_near_50(self) -> None:
        """Equal gains and losses hover around 50.

        Not exactly 50: Wilder smoothing keeps oscillating, so the final value
        leans whichever way the last bar moved. The band just has to be tight
        enough to catch a genuinely broken RSI.
        """
        s = pd.Series([100.0 + (1.0 if i % 2 else -1.0) for i in range(60)])
        assert rsi(s, 14).iloc[-1] == pytest.approx(50.0, abs=5.0)

    def test_stays_within_bounds(self) -> None:
        rng = np.random.default_rng(42)
        s = pd.Series(100 + rng.standard_normal(200).cumsum())
        out = rsi(s, 14).dropna()
        assert out.between(0, 100).all()

    def test_warm_up_is_nan(self) -> None:
        s = pd.Series(np.arange(1.0, 30.0))
        assert rsi(s, 14).iloc[:13].isna().all()


class TestMACD:
    def test_line_equals_fast_minus_slow_ema(self) -> None:
        rng = np.random.default_rng(7)
        s = pd.Series(100 + rng.standard_normal(200).cumsum())
        out = macd(s)
        expected = ema(s, 12) - ema(s, 26)
        pd.testing.assert_series_equal(
            out["macd"].dropna(), expected.dropna(), check_names=False
        )

    def test_histogram_is_line_minus_signal(self) -> None:
        rng = np.random.default_rng(7)
        s = pd.Series(100 + rng.standard_normal(200).cumsum())
        out = macd(s).dropna()
        assert np.allclose(out["hist"], out["macd"] - out["signal"])

    def test_rejects_fast_not_shorter_than_slow(self) -> None:
        with pytest.raises(ValueError, match="shorter"):
            macd(pd.Series([1.0]), fast=26, slow=12)


class TestATR:
    def test_true_range_picks_the_largest_of_the_three(self) -> None:
        df = pd.DataFrame(
            {"high": [10.0, 12.0], "low": [8.0, 11.0], "close": [9.0, 11.5]}
        )
        tr = true_range(df["high"], df["low"], df["close"])
        assert tr.iloc[0] == pytest.approx(2.0)  # no prev close -> H-L
        # H-L = 1.0; |H-Cprev| = |12-9| = 3.0; |L-Cprev| = |11-9| = 2.0
        assert tr.iloc[1] == pytest.approx(3.0)

    def test_constant_range_gives_that_range(self) -> None:
        n = 40
        df = pd.DataFrame(
            {"high": [11.0] * n, "low": [9.0] * n, "close": [10.0] * n}
        )
        assert atr(df["high"], df["low"], df["close"], 14).iloc[-1] == pytest.approx(2.0)


class TestSwings:
    def test_finds_a_clear_peak(self) -> None:
        vals = [1, 2, 3, 4, 5, 10, 5, 4, 3, 2, 1]
        idx = swing_highs(pd.Series(vals, dtype=float), order=4)
        assert 5 in idx

    def test_finds_a_clear_trough(self) -> None:
        vals = [10, 9, 8, 7, 6, 1, 6, 7, 8, 9, 10]
        idx = swing_lows(pd.Series(vals, dtype=float), order=4)
        assert 5 in idx

    def test_series_too_short_returns_empty(self) -> None:
        assert len(swing_highs(pd.Series([1.0, 2.0, 3.0]), order=4)) == 0


class TestTrendStructure:
    def ascending(self) -> pd.DataFrame:
        # Two rising swings inside the lookback window.
        highs = [10, 11, 12, 11, 10, 11, 13, 14, 15, 14, 13, 14, 16, 17, 18, 17, 16, 17, 19, 20]
        lows = [h - 3 for h in highs]
        return pd.DataFrame({"high": list(map(float, highs)), "low": list(map(float, lows))})

    def test_detects_higher_highs_and_higher_lows(self) -> None:
        assert higher_highs_higher_lows(self.ascending(), lookback=20, order=2)

    def descending(self) -> pd.DataFrame:
        """Mirror of ``ascending`` about a constant, so the swing structure is
        genuinely descending rather than a reversed series (reversing a zigzag
        does not reliably invert which points argrelextrema picks)."""
        highs = [10, 11, 12, 11, 10, 11, 13, 14, 15, 14, 13, 14, 16, 17, 18, 17, 16, 17, 19, 20]
        mirrored = [40 - h for h in highs]
        return pd.DataFrame(
            {"high": [float(m) for m in mirrored], "low": [float(m - 3) for m in mirrored]}
        )

    def test_descending_is_not_ascending(self) -> None:
        df = self.descending()
        assert not higher_highs_higher_lows(df, lookback=20, order=2)
        assert lower_highs_lower_lows(df, lookback=20, order=2)

    def test_ascending_is_not_descending(self) -> None:
        assert not lower_highs_lower_lows(self.ascending(), lookback=20, order=2)

    def test_too_short_returns_false(self) -> None:
        df = pd.DataFrame({"high": [1.0, 2.0], "low": [0.5, 1.5]})
        assert not higher_highs_higher_lows(df, order=4)


class TestCandleAnatomy:
    def test_body_ratio(self) -> None:
        df = pd.DataFrame({"open": [10.0], "high": [14.0], "low": [8.0], "close": [12.0]})
        assert body_ratio(df).iloc[0] == pytest.approx(2.0 / 6.0)

    def test_doji_has_near_zero_body_ratio(self) -> None:
        df = pd.DataFrame({"open": [10.0], "high": [12.0], "low": [8.0], "close": [10.0]})
        assert body_ratio(df).iloc[0] == pytest.approx(0.0)

    def test_zero_range_does_not_divide_by_zero(self) -> None:
        df = pd.DataFrame({"open": [10.0], "high": [10.0], "low": [10.0], "close": [10.0]})
        assert body_ratio(df).iloc[0] == 0.0
        assert close_position_in_range(df).iloc[0] == 0.5

    def test_close_at_high_is_one(self) -> None:
        df = pd.DataFrame({"open": [9.0], "high": [12.0], "low": [8.0], "close": [12.0]})
        assert close_position_in_range(df).iloc[0] == pytest.approx(1.0)


class TestAddIndicators:
    def test_attaches_the_expected_columns(self) -> None:
        rng = np.random.default_rng(3)
        n = 260
        close = 100 + rng.standard_normal(n).cumsum()
        df = pd.DataFrame(
            {
                "ts": pd.date_range("2025-01-01", periods=n, freq="D"),
                "open": close, "high": close + 1, "low": close - 1,
                "close": close, "volume": rng.integers(1000, 5000, n).astype(float),
            }
        )
        out = add_indicators(df)
        for col in ("ema8", "ema13", "ema20", "ema50", "ema200", "rsi",
                    "macd", "macd_signal", "macd_hist", "atr", "vol_sma"):
            assert col in out.columns, col
        assert out["ema200"].notna().sum() > 0
        assert len(out) == len(df), "must not drop rows"
