"""3.8 Historical Backtester.

Replays a scanner across a historical window and reports trade statistics.

**Corporate actions (review finding B2).** Spec 2.4 mandates unadjusted prices
so the ATH display matches a trading terminal. Returns computed on unadjusted
prices are wrong across a split: a 1:10 split reads as a -90% trade. The
backtester therefore detects overnight gaps consistent with a split or bonus
and, depending on ``split_policy``:

* ``"exclude"`` (default) -- drop the affected symbol from the run and report
  it, so no fabricated loss ever enters the statistics;
* ``"adjust"``  -- back-adjust prices by the detected ratio;
* ``"ignore"``  -- spec-literal behaviour, retained for comparison.

**Survivorship bias (review finding C6).** The universe is today's index
membership, so a backtest over a past window silently excludes companies that
were removed. ``BacktestResult.warnings`` always states this; it is a property
of the available data, not something the engine can correct.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np
import pandas as pd

from infinity.scanners.base import REGISTRY, InsufficientHistory, ScanContext, Signal

log = logging.getLogger(__name__)

TRADING_DAYS = 252
SPLIT_GAP_THRESHOLD = 0.35  # |overnight move| beyond this looks like an action


class ExitRule(StrEnum):
    FIXED_BARS = "fixed_bars"
    STOP_TARGET = "stop_target"
    SIGNAL_INVALIDATION = "signal_invalidation"


@dataclass
class BacktestConfig:
    scanner: str
    exit_rule: ExitRule = ExitRule.STOP_TARGET
    hold_bars: int = 10
    stop_pct: float = -5.0
    target_pct: float = 10.0
    min_score: float = 80.0
    cost_pct: float = 0.15  # round-trip brokerage + STT + slippage
    start: str | None = None
    end: str | None = None
    warmup_bars: int = 250
    step: int = 1  # evaluate every Nth bar
    split_policy: str = "exclude"  # exclude | adjust | ignore


@dataclass
class Trade:
    symbol: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    bars_held: int
    exit_reason: str
    score: float

    @property
    def gross_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        return 100.0 * (self.exit_price - self.entry_price) / self.entry_price

    def net_pct(self, cost_pct: float) -> float:
        return self.gross_pct - cost_pct

    def to_dict(self, cost_pct: float) -> dict:
        return {
            "Symbol": self.symbol,
            "Entry Date": self.entry_date,
            "Entry": round(self.entry_price, 2),
            "Exit Date": self.exit_date,
            "Exit": round(self.exit_price, 2),
            "Bars": self.bars_held,
            "Gross %": round(self.gross_pct, 2),
            "Net %": round(self.net_pct(cost_pct), 2),
            "Exit Reason": self.exit_reason,
            "Score": round(self.score, 1),
        }


@dataclass
class BacktestResult:
    config: BacktestConfig
    trades: list[Trade] = field(default_factory=list)
    equity_curve: pd.DataFrame | None = None
    benchmark_curve: pd.DataFrame | None = None
    warnings: list[str] = field(default_factory=list)
    excluded: list[dict[str, str]] = field(default_factory=list)
    symbols_tested: int = 0

    # -- metrics -----------------------------------------------------------

    def metrics(self) -> dict[str, float | int]:
        cost = self.config.cost_pct
        nets = [t.net_pct(cost) for t in self.trades]
        gross = [t.gross_pct for t in self.trades]
        if not nets:
            return {"Total Trades": 0}

        wins = [r for r in nets if r > 0]
        losses = [r for r in nets if r <= 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))

        return {
            "Total Trades": len(nets),
            "Win Rate %": round(100.0 * len(wins) / len(nets), 2),
            "Profit Factor": round(gross_profit / gross_loss, 2) if gross_loss else float("inf"),
            "Avg Win %": round(sum(wins) / len(wins), 2) if wins else 0.0,
            "Avg Loss %": round(sum(losses) / len(losses), 2) if losses else 0.0,
            "Expectancy %": round(sum(nets) / len(nets), 2),
            "Avg Gross %": round(sum(gross) / len(gross), 2),
            # Portfolio return from the equity curve. Summing 660 individual
            # trade percentages is not a return anyone could have earned --
            # the trades overlap and share capital.
            "Portfolio Return %": round(self.portfolio_return(), 2),
            "Max Drawdown %": round(self.max_drawdown(), 2),
            "Sharpe (ann.)": round(self.sharpe(), 2),
        }

    def portfolio_return(self) -> float:
        if self.equity_curve is None or self.equity_curve.empty:
            return 0.0
        return float(self.equity_curve["equity"].iloc[-1]) - 100.0

    def benchmark_return(self) -> float:
        if self.benchmark_curve is None or self.benchmark_curve.empty:
            return 0.0
        return float(self.benchmark_curve["equity"].iloc[-1]) - 100.0

    def max_drawdown(self) -> float:
        if self.equity_curve is None or self.equity_curve.empty:
            return 0.0
        equity = self.equity_curve["equity"]
        peak = equity.cummax()
        return float(((equity - peak) / peak * 100.0).min())

    def sharpe(self, risk_free_annual: float = 0.0) -> float:
        """Annualised Sharpe on the daily equity curve.

        Computed on daily equity returns rather than per-trade returns, since
        trades overlap and have unequal holding periods -- a per-trade Sharpe
        would not annualise meaningfully.
        """
        if self.equity_curve is None or len(self.equity_curve) < 2:
            return 0.0
        rets = self.equity_curve["equity"].pct_change().dropna()
        std = rets.std()
        # A single return gives std = NaN under ddof=1, and NaN == 0 is False,
        # so the zero check alone would let NaN through into the UI.
        if rets.empty or pd.isna(std) or std == 0:
            return 0.0
        excess = rets - (risk_free_annual / 100.0) / TRADING_DAYS
        return float(excess.mean() / std * math.sqrt(TRADING_DAYS))


# ---------------------------------------------------------------------------
# Corporate-action handling (B2)
# ---------------------------------------------------------------------------


def detect_split_gaps(df: pd.DataFrame, threshold: float = SPLIT_GAP_THRESHOLD) -> list[int]:
    """Positions where an overnight move looks like a split or bonus issue."""
    closes = df["close"].to_numpy(dtype=float)
    if len(closes) < 2:
        return []
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = closes[1:] / np.where(closes[:-1] == 0, np.nan, closes[:-1])
    moves = np.abs(ratio - 1.0)
    return [int(i + 1) for i in np.where(moves > threshold)[0]]


def back_adjust(df: pd.DataFrame, positions: list[int]) -> pd.DataFrame:
    """Scale pre-action prices so the series is continuous for return maths."""
    out = df.copy()
    closes = out["close"].to_numpy(dtype=float)
    for pos in sorted(positions, reverse=True):
        if pos == 0 or closes[pos - 1] == 0:
            continue
        factor = closes[pos] / closes[pos - 1]
        for col in ("open", "high", "low", "close"):
            out.iloc[:pos, out.columns.get_loc(col)] *= factor
    return out


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def _prepare(df: pd.DataFrame, config: BacktestConfig) -> tuple[pd.DataFrame | None, str | None]:
    gaps = detect_split_gaps(df)
    if not gaps:
        return df, None
    if config.split_policy == "ignore":
        return df, None
    if config.split_policy == "adjust":
        return back_adjust(df, gaps), None
    return None, f"corporate action suspected at {len(gaps)} bar(s); excluded"


def backtest_symbol(
    symbol: str,
    df: pd.DataFrame,
    config: BacktestConfig,
    industry: str = "",
) -> list[Trade]:
    """Replay one symbol, opening at the next bar's open after a signal."""
    scanner = REGISTRY[config.scanner]
    df = df.reset_index(drop=True)
    trades: list[Trade] = []

    start = max(config.warmup_bars, getattr(scanner, "min_daily_bars", 250))
    open_until = -1  # no pyramiding: ignore signals while a position is live

    for i in range(start, len(df) - 1, config.step):
        if i <= open_until:
            continue

        window = df.iloc[: i + 1]
        if config.start and str(window["ts"].iloc[-1].date()) < config.start:
            continue
        if config.end and str(window["ts"].iloc[-1].date()) > config.end:
            break

        try:
            row = scanner.scan(ScanContext(symbol=symbol, daily=window, industry=industry))
        except (InsufficientHistory, Exception):  # noqa: B014 - never abort a replay
            continue
        if row is None or not row.qualifies or row.score < config.min_score:
            continue
        if row.signal is not Signal.BULLISH:
            continue  # long-only

        entry_pos = i + 1
        entry = float(df["open"].iloc[entry_pos])
        if entry <= 0:
            continue

        exit_pos, exit_price, reason = _find_exit(df, entry_pos, entry, config)
        trades.append(
            Trade(
                symbol=symbol,
                entry_date=str(df["ts"].iloc[entry_pos].date()),
                entry_price=entry,
                exit_date=str(df["ts"].iloc[exit_pos].date()),
                exit_price=exit_price,
                bars_held=exit_pos - entry_pos,
                exit_reason=reason,
                score=row.score,
            )
        )
        open_until = exit_pos

    return trades


def _find_exit(
    df: pd.DataFrame, entry_pos: int, entry: float, config: BacktestConfig
) -> tuple[int, float, str]:
    last = len(df) - 1

    if config.exit_rule is ExitRule.FIXED_BARS:
        pos = min(entry_pos + config.hold_bars, last)
        return pos, float(df["close"].iloc[pos]), "Fixed hold"

    if config.exit_rule is ExitRule.STOP_TARGET:
        stop = entry * (1.0 + config.stop_pct / 100.0)
        target = entry * (1.0 + config.target_pct / 100.0)
        for pos in range(entry_pos, last + 1):
            low = float(df["low"].iloc[pos])
            high = float(df["high"].iloc[pos])
            # Stop checked first: the pessimistic assumption when a single bar
            # spans both levels and intrabar order is unknown.
            if low <= stop:
                return pos, stop, "Stop loss"
            if high >= target:
                return pos, target, "Target"
        return last, float(df["close"].iloc[last]), "Open at end"

    # Signal invalidation: exit on a close back below the 20-bar EMA.
    from infinity.indicators import ema

    e20 = ema(df["close"], 20)
    for pos in range(entry_pos + 1, last + 1):
        val = e20.iloc[pos]
        if pd.notna(val) and float(df["close"].iloc[pos]) < float(val):
            return pos, float(df["close"].iloc[pos]), "Signal invalidated"
    return last, float(df["close"].iloc[last]), "Open at end"


def run_backtest(
    config: BacktestConfig,
    bars: dict[str, pd.DataFrame],
    industries: dict[str, str] | None = None,
) -> BacktestResult:
    """Run a backtest across a universe of bars."""
    if config.scanner not in REGISTRY:
        raise KeyError(f"unknown scanner {config.scanner!r}")

    industries = industries or {}
    result = BacktestResult(config=config)
    result.warnings.append(
        "Survivorship bias: the universe is today's index membership, so "
        "companies removed during the window are absent and results are "
        "optimistic (review finding C6)."
    )
    if config.split_policy == "ignore":
        result.warnings.append(
            "split_policy='ignore' uses unadjusted prices; a split will appear "
            "as a large fabricated loss (review finding B2)."
        )

    all_trades: list[Trade] = []
    for symbol, df in bars.items():
        prepared, problem = _prepare(df, config)
        if prepared is None:
            result.excluded.append({"symbol": symbol, "reason": problem or "excluded"})
            continue
        result.symbols_tested += 1
        all_trades.extend(
            backtest_symbol(symbol, prepared, config, industries.get(symbol, ""))
        )

    result.trades = sorted(all_trades, key=lambda t: t.entry_date)
    result.equity_curve = build_equity_curve(result.trades, bars, config.cost_pct)
    result.benchmark_curve = build_benchmark(bars, config)
    return result


def build_equity_curve(
    trades: list[Trade], bars: dict[str, pd.DataFrame], cost_pct: float
) -> pd.DataFrame:
    """Daily mark-to-market portfolio equity, equal-weighted across open positions.

    Compounding each trade's return in sequence would be wrong: these are
    *parallel* positions across many symbols, not one capital stack recycled
    through 660 consecutive bets. Sequential compounding at a 35% win rate
    manufactures near-total ruin out of a positive expectancy -- it reports
    "+64% net" beside an equity curve ending at 38.

    Each day the portfolio return is the mean daily return of whatever
    positions are open, so drawdown and Sharpe describe a portfolio an actual
    trader could have held.
    """
    if not trades:
        return pd.DataFrame(columns=["date", "equity"])

    closes: dict[str, pd.Series] = {}
    for symbol, df in bars.items():
        s = df.set_index(df["ts"].dt.strftime("%Y-%m-%d"))["close"].astype(float)
        closes[symbol] = s[~s.index.duplicated(keep="last")]

    dates = sorted({d for s in closes.values() for d in s.index})
    first, last = min(t.entry_date for t in trades), max(t.exit_date for t in trades)
    dates = [d for d in dates if first <= d <= last]
    if not dates:
        return pd.DataFrame(columns=["date", "equity"])

    pos_of = {d: i for i, d in enumerate(dates)}
    # daily_returns[date] collects each open position's return for that day.
    daily: dict[str, list[float]] = {d: [] for d in dates}

    for t in trades:
        if t.entry_date not in pos_of or t.exit_date not in pos_of:
            continue
        series = closes.get(t.symbol)
        if series is None:
            continue

        span = dates[pos_of[t.entry_date] : pos_of[t.exit_date] + 1]
        prev = t.entry_price
        for i, d in enumerate(span):
            if d not in series.index:
                continue
            price = t.exit_price if i == len(span) - 1 else float(series.loc[d])
            if prev > 0:
                ret = price / prev - 1.0
                # Charge the full round-trip cost on the closing day.
                if i == len(span) - 1:
                    ret -= cost_pct / 100.0
                daily[d].append(ret)
            prev = price

    rows, equity = [], 100.0
    for d in dates:
        day = daily[d]
        if day:
            equity *= 1.0 + sum(day) / len(day)
        rows.append({"date": d, "equity": equity})
    return pd.DataFrame(rows)


def build_benchmark(bars: dict[str, pd.DataFrame], config: BacktestConfig) -> pd.DataFrame:
    """Equal-weight buy-and-hold across the same universe and window."""
    series = []
    for df in bars.values():
        d = df.copy()
        if config.start:
            d = d[d["ts"].dt.strftime("%Y-%m-%d") >= config.start]
        if config.end:
            d = d[d["ts"].dt.strftime("%Y-%m-%d") <= config.end]
        if len(d) < 2:
            continue
        base = float(d["close"].iloc[0])
        if base <= 0:
            continue
        series.append(
            pd.Series(
                (d["close"] / base * 100.0).to_numpy(),
                index=d["ts"].dt.strftime("%Y-%m-%d").to_numpy(),
            )
        )

    if not series:
        return pd.DataFrame(columns=["date", "equity"])

    frame = pd.concat(series, axis=1).sort_index()
    mean = frame.mean(axis=1)
    return pd.DataFrame({"date": mean.index, "equity": mean.to_numpy()})
