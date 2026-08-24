"""Technical indicators (spec 8: validated against known reference values).

Conventions that matter for reproducing a trading terminal's numbers:

* **EMA** uses ``adjust=False`` -- the recursive form every charting package
  uses. ``adjust=True`` gives different early values and will not match.
* **RSI and ATR** use Wilder's smoothing (alpha = 1/n), not a simple mean.
  A plain rolling mean produces visibly different RSI and is the single most
  common way these get implemented wrong.
* Every function returns a 1-D Series aligned to the input index, with NaN for
  the warm-up window rather than a truncated series.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.signal import argrelextrema

# ---------------------------------------------------------------------------
# Moving averages
# ---------------------------------------------------------------------------


def _seeded_ewm(series: pd.Series, period: int, alpha: float) -> pd.Series:
    """Recursive smoothing seeded on the SMA of the first ``period`` values.

    pandas' ``ewm(adjust=False)`` starts its recursion at ``x[0]``. Charting
    packages (and Wilder's original definition) instead seed on the simple mean
    of the first window, which is what makes the output match a trading
    terminal. Values before the seed are NaN.

    Implemented by blanking the warm-up and planting the seed, so pandas' own
    C loop does the work -- a Python loop here would be the hot path across
    500 symbols x 3000 bars x 5 EMAs.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    vals = series.astype(float)
    if len(vals) < period:
        return pd.Series(np.nan, index=series.index, dtype=float)

    seeded = vals.copy()
    seeded.iloc[: period - 1] = np.nan
    seeded.iloc[period - 1] = vals.iloc[:period].mean()
    return seeded.ewm(alpha=alpha, adjust=False).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average, SMA-seeded (alpha = 2/(n+1))."""
    return _seeded_ewm(series, period, alpha=2.0 / (period + 1))


def sma(series: pd.Series, period: int) -> pd.Series:
    if period <= 0:
        raise ValueError("period must be positive")
    return series.rolling(window=period, min_periods=period).mean()


def wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing: recursive with alpha = 1/n, seeded on the SMA."""
    return _seeded_ewm(series, period, alpha=1.0 / period)


# ---------------------------------------------------------------------------
# Oscillators
# ---------------------------------------------------------------------------


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index, Wilder's method."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    avg_gain = wilder_smooth(gain, period)
    avg_loss = wilder_smooth(loss, period)

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # An all-gain window has zero average loss -> RSI is 100 by definition.
    return out.where(avg_loss != 0.0, 100.0).where(avg_gain.notna())


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """MACD line, signal line and histogram."""
    if fast >= slow:
        raise ValueError("fast period must be shorter than slow")
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame(
        {
            "macd": macd_line,
            "signal": signal_line,
            "hist": macd_line - signal_line,
        }
    )


# ---------------------------------------------------------------------------
# Volatility
# ---------------------------------------------------------------------------


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range, Wilder's method."""
    return wilder_smooth(true_range(high, low, close), period)


# ---------------------------------------------------------------------------
# Swing structure
# ---------------------------------------------------------------------------


def swing_highs(high: pd.Series, order: int = 4) -> np.ndarray:
    """Positional indices of local maxima (spec 3.3 Step 2)."""
    if len(high) < 2 * order + 1:
        return np.array([], dtype=int)
    return argrelextrema(high.to_numpy(), np.greater_equal, order=order)[0]


def swing_lows(low: pd.Series, order: int = 4) -> np.ndarray:
    """Positional indices of local minima (spec 3.3 Step 2)."""
    if len(low) < 2 * order + 1:
        return np.array([], dtype=int)
    return argrelextrema(low.to_numpy(), np.less_equal, order=order)[0]


def higher_highs_higher_lows(df: pd.DataFrame, lookback: int = 20, order: int = 4) -> bool:
    """True when the recent swing structure is ascending (spec 3.3 Step 6)."""
    window = df.tail(lookback)
    if len(window) < 2 * order + 1:
        return False
    hi = swing_highs(window["high"], order)
    lo = swing_lows(window["low"], order)
    if len(hi) < 2 or len(lo) < 2:
        return False
    highs = window["high"].to_numpy()[hi]
    lows = window["low"].to_numpy()[lo]
    return bool(highs[-1] > highs[-2] and lows[-1] > lows[-2])


def lower_highs_lower_lows(df: pd.DataFrame, lookback: int = 20, order: int = 4) -> bool:
    window = df.tail(lookback)
    if len(window) < 2 * order + 1:
        return False
    hi = swing_highs(window["high"], order)
    lo = swing_lows(window["low"], order)
    if len(hi) < 2 or len(lo) < 2:
        return False
    highs = window["high"].to_numpy()[hi]
    lows = window["low"].to_numpy()[lo]
    return bool(highs[-1] < highs[-2] and lows[-1] < lows[-2])


# ---------------------------------------------------------------------------
# Candle anatomy
# ---------------------------------------------------------------------------


def candle_body(df: pd.DataFrame) -> pd.Series:
    return (df["close"] - df["open"]).abs()


def candle_range(df: pd.DataFrame) -> pd.Series:
    return df["high"] - df["low"]


def body_ratio(df: pd.DataFrame) -> pd.Series:
    """Body as a fraction of range. Doji/spinning tops sit near 0."""
    rng = candle_range(df).replace(0.0, np.nan)
    return (candle_body(df) / rng).fillna(0.0)


def close_position_in_range(df: pd.DataFrame) -> pd.Series:
    """Where the close sits in the candle: 1.0 = at the high, 0.0 = at the low."""
    rng = candle_range(df).replace(0.0, np.nan)
    return ((df["close"] - df["low"]) / rng).fillna(0.5)


def is_bullish(df: pd.DataFrame) -> pd.Series:
    return df["close"] > df["open"]


# ---------------------------------------------------------------------------
# Convenience bundle
# ---------------------------------------------------------------------------


def add_indicators(
    df: pd.DataFrame,
    ema_periods: tuple[int, ...] = (8, 13, 20, 50, 200),
    rsi_period: int = 14,
    atr_period: int = 14,
    volume_sma: int = 20,
) -> pd.DataFrame:
    """Attach the standard indicator set used across the scanner modules."""
    out = df.copy()
    for p in ema_periods:
        out[f"ema{p}"] = ema(out["close"], p)

    out["rsi"] = rsi(out["close"], rsi_period)

    m = macd(out["close"])
    out["macd"] = m["macd"]
    out["macd_signal"] = m["signal"]
    out["macd_hist"] = m["hist"]

    out["atr"] = atr(out["high"], out["low"], out["close"], atr_period)
    out["vol_sma"] = sma(out["volume"], volume_sma)
    out["body_ratio"] = body_ratio(out)
    out["close_pos"] = close_position_in_range(out)
    return out
