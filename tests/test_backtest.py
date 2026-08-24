"""Backtester tests, including a hand-computed trade set (spec 8)."""

from __future__ import annotations

import pandas as pd
import pytest

from infinity.backtest import (
    BacktestConfig,
    BacktestResult,
    ExitRule,
    Trade,
    back_adjust,
    build_equity_curve,
    detect_split_gaps,
    run_backtest,
)
from infinity.market_clock import IST


def frame(closes: list[float], highs=None, lows=None, opens=None) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame(
        {
            "ts": pd.date_range("2024-01-01", periods=n, freq="D", tz=IST),
            "open": opens if opens is not None else closes,
            "high": highs if highs is not None else [c * 1.01 for c in closes],
            "low": lows if lows is not None else [c * 0.99 for c in closes],
            "close": closes,
            "volume": [1000.0] * n,
        }
    )


def trade(symbol="A", entry=100.0, exit_=110.0, entry_date="2024-01-02",
          exit_date="2024-01-05", bars=3, reason="Target", score=100.0) -> Trade:
    return Trade(symbol, entry_date, entry, exit_date, exit_, bars, reason, score)


class TestTradeArithmetic:
    def test_gross_and_net(self) -> None:
        t = trade(entry=100.0, exit_=110.0)
        assert t.gross_pct == pytest.approx(10.0)
        assert t.net_pct(0.15) == pytest.approx(9.85)

    def test_losing_trade(self) -> None:
        t = trade(entry=100.0, exit_=95.0)
        assert t.gross_pct == pytest.approx(-5.0)
        assert t.net_pct(0.15) == pytest.approx(-5.15)

    def test_zero_entry_does_not_divide_by_zero(self) -> None:
        assert trade(entry=0.0).gross_pct == 0.0


class TestMetricsAgainstHandComputedTrades:
    """Spec 8: validate the formulas on a small set worked out by hand."""

    def result(self) -> BacktestResult:
        cfg = BacktestConfig(scanner="resistance_breakout", cost_pct=0.0)
        res = BacktestResult(config=cfg)
        # +10, -5, +10, -5, +20  ->  3 wins / 2 losses
        res.trades = [
            trade("A", 100.0, 110.0),
            trade("B", 100.0, 95.0),
            trade("C", 100.0, 110.0),
            trade("D", 100.0, 95.0),
            trade("E", 100.0, 120.0),
        ]
        return res

    def test_win_rate(self) -> None:
        assert self.result().metrics()["Win Rate %"] == pytest.approx(60.0)

    def test_profit_factor(self) -> None:
        # gross profit 10+10+20 = 40; gross loss 5+5 = 10; PF = 4.0
        assert self.result().metrics()["Profit Factor"] == pytest.approx(4.0)

    def test_average_win_and_loss(self) -> None:
        m = self.result().metrics()
        assert m["Avg Win %"] == pytest.approx(40.0 / 3, abs=0.01)
        assert m["Avg Loss %"] == pytest.approx(-5.0)

    def test_expectancy(self) -> None:
        # (10 - 5 + 10 - 5 + 20) / 5 = 6.0
        assert self.result().metrics()["Expectancy %"] == pytest.approx(6.0)

    def test_costs_reduce_every_trade(self) -> None:
        res = self.result()
        res.config.cost_pct = 1.0
        assert res.metrics()["Expectancy %"] == pytest.approx(5.0)

    def test_no_trades_reports_zero(self) -> None:
        res = BacktestResult(config=BacktestConfig(scanner="resistance_breakout"))
        assert res.metrics() == {"Total Trades": 0}


class TestEquityCurve:
    """Regression for the sequential-compounding bug."""

    def test_parallel_trades_are_not_compounded_sequentially(self) -> None:
        """Two simultaneous +10% trades give +10%, not +21%.

        Compounding them in sequence treats one capital stack as if it were
        recycled through both, which is how a positive expectancy turned into
        a -98% drawdown before this was fixed.
        """
        bars = {
            "A": frame([100.0, 105.0, 110.0]),
            "B": frame([100.0, 105.0, 110.0]),
        }
        trades = [
            trade("A", 100.0, 110.0, "2024-01-01", "2024-01-03", 2),
            trade("B", 100.0, 110.0, "2024-01-01", "2024-01-03", 2),
        ]
        curve = build_equity_curve(trades, bars, cost_pct=0.0)
        assert curve["equity"].iloc[-1] == pytest.approx(110.0, abs=0.5)

    def test_sequential_trades_do_compound(self) -> None:
        bars = {"A": frame([100.0, 110.0, 110.0, 121.0])}
        trades = [
            trade("A", 100.0, 110.0, "2024-01-01", "2024-01-02", 1),
            trade("A", 110.0, 121.0, "2024-01-03", "2024-01-04", 1),
        ]
        curve = build_equity_curve(trades, bars, cost_pct=0.0)
        assert curve["equity"].iloc[-1] == pytest.approx(121.0, abs=1.0)

    def test_empty_trades_gives_empty_curve(self) -> None:
        assert build_equity_curve([], {}, 0.15).empty

    def test_drawdown_is_negative_or_zero(self) -> None:
        res = BacktestResult(config=BacktestConfig(scanner="resistance_breakout"))
        res.equity_curve = pd.DataFrame(
            {"date": ["a", "b", "c"], "equity": [100.0, 120.0, 90.0]}
        )
        assert res.max_drawdown() == pytest.approx(-25.0)

    def test_sharpe_of_a_flat_curve_is_zero(self) -> None:
        res = BacktestResult(config=BacktestConfig(scanner="resistance_breakout"))
        res.equity_curve = pd.DataFrame({"date": ["a", "b"], "equity": [100.0, 100.0]})
        assert res.sharpe() == 0.0


class TestCorporateActions:
    """Review finding B2."""

    def test_detects_a_ten_for_one_split(self) -> None:
        closes = [1000.0] * 10 + [100.0] * 10  # -90% overnight
        assert 10 in detect_split_gaps(frame(closes))

    def test_ignores_ordinary_volatility(self) -> None:
        closes = [100.0, 103.0, 99.0, 105.0, 101.0]
        assert detect_split_gaps(frame(closes)) == []

    def test_back_adjust_makes_the_series_continuous(self) -> None:
        closes = [1000.0] * 5 + [100.0] * 5
        df = frame(closes)
        adjusted = back_adjust(df, detect_split_gaps(df))
        assert adjusted["close"].iloc[4] == pytest.approx(100.0)
        assert detect_split_gaps(adjusted) == []

    def test_exclude_policy_keeps_the_split_out_of_the_stats(self) -> None:
        bars = {"SPLITCO": frame([1000.0] * 300 + [100.0] * 60)}
        cfg = BacktestConfig(scanner="resistance_breakout", split_policy="exclude")
        res = run_backtest(cfg, bars)

        assert res.symbols_tested == 0
        assert len(res.excluded) == 1
        assert "corporate action" in res.excluded[0]["reason"]

    def test_ignore_policy_warns_loudly(self) -> None:
        cfg = BacktestConfig(scanner="resistance_breakout", split_policy="ignore")
        res = run_backtest(cfg, {})
        assert any("unadjusted" in w for w in res.warnings)


class TestExitRules:
    def rising(self) -> dict[str, pd.DataFrame]:
        closes = [100.0 + i * 0.9 for i in range(320)]
        return {"UP": frame(closes)}

    def test_fixed_bars_exit(self) -> None:
        cfg = BacktestConfig(
            scanner="resistance_breakout", exit_rule=ExitRule.FIXED_BARS,
            hold_bars=5, min_score=100.0,
        )
        res = run_backtest(cfg, self.rising())
        assert all(t.bars_held <= 5 for t in res.trades)
        assert all(t.exit_reason == "Fixed hold" for t in res.trades)

    def test_stop_target_exit_reasons_are_valid(self) -> None:
        cfg = BacktestConfig(
            scanner="resistance_breakout", exit_rule=ExitRule.STOP_TARGET,
            stop_pct=-5.0, target_pct=10.0, min_score=100.0,
        )
        res = run_backtest(cfg, self.rising())
        assert all(
            t.exit_reason in ("Stop loss", "Target", "Open at end") for t in res.trades
        )

    def test_no_pyramiding(self) -> None:
        """A new position must not open while one is already live."""
        cfg = BacktestConfig(
            scanner="resistance_breakout", exit_rule=ExitRule.FIXED_BARS,
            hold_bars=10, min_score=100.0,
        )
        res = run_backtest(cfg, self.rising())
        by_symbol: dict[str, list[Trade]] = {}
        for t in res.trades:
            by_symbol.setdefault(t.symbol, []).append(t)
        for trades in by_symbol.values():
            ordered = sorted(trades, key=lambda x: x.entry_date)
            for a, b in zip(ordered, ordered[1:], strict=False):
                assert b.entry_date > a.exit_date or b.entry_date >= a.exit_date


class TestRunBacktest:
    def test_always_warns_about_survivorship_bias(self) -> None:
        """Review finding C6 -- the engine cannot fix it, so it must say so."""
        res = run_backtest(BacktestConfig(scanner="resistance_breakout"), {})
        assert any("Survivorship bias" in w for w in res.warnings)

    def test_unknown_scanner_raises(self) -> None:
        with pytest.raises(KeyError, match="unknown scanner"):
            run_backtest(BacktestConfig(scanner="nope"), {})

    def test_benchmark_is_built_from_the_same_universe(self) -> None:
        bars = {"A": frame([100.0 + i for i in range(300)])}
        res = run_backtest(BacktestConfig(scanner="resistance_breakout"), bars)
        assert res.benchmark_curve is not None
        assert not res.benchmark_curve.empty
        assert res.benchmark_return() > 0
