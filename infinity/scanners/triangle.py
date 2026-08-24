"""3.4 Bullish Triangle Pattern Scanner.

Fits a resistance line through recent swing highs and a support line through
recent swing lows, then classifies:

* **Ascending**  -- flat resistance (|slope| <= 0.035%/bar) over rising support
  (slope > +0.015%/bar).
* **Symmetrical** -- falling resistance (< -0.015%/bar) over rising support.

Both require the bullish momentum qualifier: ``close >= EMA20 * 0.98`` and
``EMA20 >= EMA50 * 0.97``.

Slopes are expressed as a percentage of price per bar so the thresholds mean
the same thing on a Rs 50 stock and a Rs 5,000 one.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from infinity.indicators import ema, swing_highs, swing_lows
from infinity.scanners.base import (
    ScanContext,
    ScanRow,
    Signal,
    normalise_score,
    pct,
    register,
    require_bars,
)

FLAT_MAX = 0.035  # %/bar, ascending-triangle ceiling
RISING_MIN = 0.015  # %/bar, support must rise by at least this
FALLING_MAX = -0.015  # %/bar, symmetrical resistance must fall by at least this
WINDOW = 60
SWING_ORDER = 3


@dataclass
class _Line:
    slope_pct: float  # % of price per bar
    intercept: float
    slope_abs: float  # price units per bar
    touches: int

    def value_at(self, x: float) -> float:
        return self.slope_abs * x + self.intercept


def _fit(x: np.ndarray, y: np.ndarray) -> _Line | None:
    """Least-squares line, with slope normalised to %/bar."""
    if len(x) < 2:
        return None
    slope_abs, intercept = np.polyfit(x, y, 1)
    base = float(np.mean(y))
    if base == 0:
        return None
    return _Line(
        slope_pct=100.0 * float(slope_abs) / base,
        intercept=float(intercept),
        slope_abs=float(slope_abs),
        touches=len(x),
    )


@dataclass
class TriangleScanner:
    name: str = "triangle"
    title: str = "Bullish Triangle"
    section: str = "3.4"
    min_daily_bars: int = 80

    def scan(self, ctx: ScanContext) -> ScanRow | None:
        df = ctx.daily
        require_bars(df, self.min_daily_bars)

        window = df.tail(WINDOW).reset_index(drop=True)
        hi_idx = swing_highs(window["high"], SWING_ORDER)
        lo_idx = swing_lows(window["low"], SWING_ORDER)

        if len(hi_idx) < 2 or len(lo_idx) < 2:
            return ScanRow(ctx.symbol, Signal.REJECT, 0.0,
                           {"Pattern": "None", "Reason": "too few swings"})

        resistance = _fit(hi_idx.astype(float), window["high"].to_numpy()[hi_idx])
        support = _fit(lo_idx.astype(float), window["low"].to_numpy()[lo_idx])
        if resistance is None or support is None:
            return ScanRow(ctx.symbol, Signal.REJECT, 0.0, {"Pattern": "None"})

        pattern = self._classify(resistance, support)

        ema20 = ema(df["close"], 20).iloc[-1]
        ema50 = ema(df["close"], 50).iloc[-1]
        close = float(df["close"].iloc[-1])
        momentum_ok = bool(
            pd.notna(ema20) and pd.notna(ema50)
            and close >= float(ema20) * 0.98
            and float(ema20) >= float(ema50) * 0.97
        )

        last_x = float(len(window) - 1)
        res_now = resistance.value_at(last_x)
        sup_now = support.value_at(last_x)
        # A triangle is only meaningful while the lines are still converging.
        converged = pct(res_now - sup_now, close)

        qualifies = pattern != "None" and momentum_ok and converged > 0
        score = self._score(pattern, resistance, support, momentum_ok, converged)

        return ScanRow(
            symbol=ctx.symbol,
            signal=Signal.BULLISH if qualifies and score >= 60 else (
                Signal.WATCHLIST if qualifies else Signal.REJECT
            ),
            score=score,
            fields={
                "Pattern": pattern,
                "Close": round(close, 2),
                "Resistance": round(res_now, 2),
                "Support": round(sup_now, 2),
                "Apex Gap %": round(converged, 2),
                "Res Slope %/bar": round(resistance.slope_pct, 4),
                "Sup Slope %/bar": round(support.slope_pct, 4),
                "Res Touches": resistance.touches,
                "Sup Touches": support.touches,
                "Momentum OK": "Yes" if momentum_ok else "No",
            },
            overlays={
                "resistance_line": (resistance.slope_abs, resistance.intercept),
                "support_line": (support.slope_abs, support.intercept),
                "window": WINDOW,
            },
        )

    @staticmethod
    def _classify(resistance: _Line, support: _Line) -> str:
        rising_support = support.slope_pct > RISING_MIN
        if not rising_support:
            return "None"
        if abs(resistance.slope_pct) <= FLAT_MAX:
            return "Ascending"
        if resistance.slope_pct < FALLING_MAX:
            return "Symmetrical"
        return "None"

    @staticmethod
    def _score(
        pattern: str, resistance: _Line, support: _Line, momentum: bool, gap: float
    ) -> float:
        if pattern == "None":
            return 0.0
        raw = 30.0  # a valid pattern
        raw += 15.0 if pattern == "Ascending" else 10.0
        raw += min(resistance.touches + support.touches, 8) * 2.5  # up to 20
        raw += 20.0 if momentum else 0.0
        # Tighter apex = closer to resolution; 1% gap or less scores full marks.
        raw += max(0.0, 1.0 - min(gap, 10.0) / 10.0) * 15.0
        return normalise_score(raw, 100)


register(TriangleScanner())
