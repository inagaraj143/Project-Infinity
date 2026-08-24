"""3.3 High-Probability Active Trendline Scanner -- the 13-step architecture.

Each step is a separate method so it can be tested independently against
synthetic fixtures (spec 8).

| Step | What it does                                        |
|------|-----------------------------------------------------|
| 1    | Data sufficiency                                    |
| 2    | Swing extrema (argrelextrema, order=4)              |
| 3    | Candidate trendlines from >= 2 swings               |
| 4    | Validation: no prior close breach, spacing, tolerance |
| 5    | Current price position vs the line                  |
| 6    | Daily confirmation (structure + EMA 50)             |
| 7    | 15-minute confirmation                              |
| 8    | RSI band                                            |
| 9    | MACD alignment                                      |
| 10   | EMA stack alignment                                 |
| 11   | Volume confirmation                                 |
| 12   | Strength score                                      |
| 13   | Classification                                      |

**Two spec corrections applied** (see docs/review-v3.md):

* *C1* -- Step 12's components sum to 110 with 3+ touches but 100 with 2, while
  Step 13 grades 0-100. The raw total is normalised by the maximum actually
  attainable, so the Step 13 bands mean one thing regardless of touch count.
  ``Raw Score`` is reported alongside so the arithmetic stays auditable.
* *C2* -- Step 1's "60 15-minute bars" is ~2.4 sessions, too short for the
  trend reading Step 7 wants. The requirement is raised to
  ``min_intraday_bars`` (default 200); when fewer are available Step 7 is
  scored as unconfirmed rather than failing the symbol.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from infinity.indicators import (
    ema,
    higher_highs_higher_lows,
    lower_highs_lower_lows,
    macd,
    rsi,
    sma,
    swing_highs,
    swing_lows,
)
from infinity.scanners.base import (
    ScanContext,
    ScanRow,
    Signal,
    normalise_score,
    pct,
    register,
    require_bars,
)

SWING_ORDER = 4
MIN_TOUCH_SPACING = 4  # candles between touches (Step 4)
TOUCH_TOLERANCE_PCT = 1.0  # Step 4
PROXIMITY_PCT = 3.0  # Step 5
FIT_WINDOW = 180


@dataclass
class Trendline:
    slope: float  # price per bar
    intercept: float
    touches: int
    direction: str  # "Uptrend" | "Downtrend"
    start_pos: int

    def value_at(self, x: float) -> float:
        return self.slope * x + self.intercept


@dataclass
class StepResults:
    """Per-step outcomes, so a failing symbol can say which step it failed."""

    swings: int = 0
    line: Trendline | None = None
    validated: bool = False
    in_position: bool = False
    daily: bool = False
    intraday: bool = False
    intraday_available: bool = True
    rsi_ok: bool = False
    macd_ok: bool = False
    ema_ok: bool = False
    volume_ok: bool = False
    values: dict = field(default_factory=dict)


@dataclass
class TrendlineScanner:
    name: str = "trendlines"
    title: str = "13-Step Trendline"
    section: str = "3.3"
    min_daily_bars: int = 250
    min_intraday_bars: int = 200  # C2: raised from the spec's 60

    # -- orchestration -----------------------------------------------------

    def scan(self, ctx: ScanContext) -> ScanRow | None:
        df = ctx.daily.reset_index(drop=True)
        self.step1_data_check(df)

        st = StepResults()
        best: tuple[float, Trendline, StepResults] | None = None

        for direction in ("Uptrend", "Downtrend"):
            candidate = self.step3_build_candidate(df, direction)
            if candidate is None:
                continue

            trial = StepResults(line=candidate, swings=candidate.touches)
            trial.validated = self.step4_validate(df, candidate)
            if not trial.validated:
                continue

            # Step 5 is a filter, not a score component -- it is absent from
            # Step 12's point list. The line has to be *active*: price sitting
            # at or within 3% of it right now. Without this gate any validated
            # line scores, and roughly three quarters of the universe
            # "qualifies", which defeats the point of the module.
            trial.in_position = self.step5_price_position(df, candidate, direction)
            if not trial.in_position:
                continue

            trial.daily = self.step6_daily_confirmation(df, direction)
            trial.intraday, trial.intraday_available = self.step7_intraday_confirmation(
                ctx.intraday, direction
            )
            trial.rsi_ok = self.step8_rsi(df, direction)
            trial.macd_ok = self.step9_macd(df, direction)
            trial.ema_ok = self.step10_ema_alignment(df, direction)
            trial.volume_ok = self.step11_volume(df)

            raw, maximum = self.step12_score(trial)
            score = normalise_score(raw, maximum)
            trial.values = {"raw": raw, "max": maximum, "score": score}

            if best is None or score > best[0]:
                best = (score, candidate, trial)

        if best is None:
            return ScanRow(
                symbol=ctx.symbol,
                signal=Signal.REJECT,
                score=0.0,
                fields={"Trend Direction": "None", "Overall Signal": "Reject"},
            )

        score, line, st = best
        classification = self.step13_classify(score)
        return self._row(ctx, df, line, st, score, classification)

    # -- steps -------------------------------------------------------------

    def step1_data_check(self, df: pd.DataFrame) -> None:
        require_bars(df, self.min_daily_bars)

    def step2_swings(self, df: pd.DataFrame, direction: str) -> np.ndarray:
        window = df.tail(FIT_WINDOW)
        offset = len(df) - len(window)
        idx = (
            swing_lows(window["low"], SWING_ORDER)
            if direction == "Uptrend"
            else swing_highs(window["high"], SWING_ORDER)
        )
        return idx + offset

    def step3_build_candidate(self, df: pd.DataFrame, direction: str) -> Trendline | None:
        """Connect the two most recent qualifying swings, sloping the right way."""
        idx = self.step2_swings(df, direction)
        if len(idx) < 2:
            return None

        col = "low" if direction == "Uptrend" else "high"
        vals = df[col].to_numpy()

        # Walk back from the most recent swing for a partner far enough away.
        last = int(idx[-1])
        for prev in reversed(idx[:-1]):
            prev = int(prev)
            if last - prev < MIN_TOUCH_SPACING:
                continue
            slope = (vals[last] - vals[prev]) / (last - prev)
            if direction == "Uptrend" and slope <= 0:
                continue
            if direction == "Downtrend" and slope >= 0:
                continue

            intercept = vals[prev] - slope * prev
            line = Trendline(slope, intercept, 2, direction, prev)
            line.touches = self._count_touches(df, line, idx, col)
            return line
        return None

    @staticmethod
    def _count_touches(
        df: pd.DataFrame, line: Trendline, idx: np.ndarray, col: str
    ) -> int:
        vals = df[col].to_numpy()
        n = 0
        for i in idx:
            i = int(i)
            if i < line.start_pos:
                continue
            expected = line.value_at(i)
            if expected and abs(pct(vals[i] - expected, expected)) <= TOUCH_TOLERANCE_PCT:
                n += 1
        return max(n, 2)

    def step4_validate(self, df: pd.DataFrame, line: Trendline) -> bool:
        """No major close breach between the first touch and the latest bar."""
        closes = df["close"].to_numpy()
        for i in range(line.start_pos, len(df) - 1):
            expected = line.value_at(i)
            if expected <= 0:
                continue
            deviation = pct(closes[i] - expected, expected)
            if line.direction == "Uptrend" and deviation < -TOUCH_TOLERANCE_PCT:
                return False
            if line.direction == "Downtrend" and deviation > TOUCH_TOLERANCE_PCT:
                return False
        return True

    def step5_price_position(self, df: pd.DataFrame, line: Trendline, direction: str) -> bool:
        expected = line.value_at(len(df) - 1)
        if expected <= 0:
            return False
        distance = pct(float(df["close"].iloc[-1]) - expected, expected)
        if direction == "Uptrend":
            return 0.0 <= distance <= PROXIMITY_PCT
        return -PROXIMITY_PCT <= distance <= 0.0

    def step6_daily_confirmation(self, df: pd.DataFrame, direction: str) -> bool:
        e50 = ema(df["close"], 50).iloc[-1]
        if pd.isna(e50):
            return False
        close = float(df["close"].iloc[-1])
        if direction == "Uptrend":
            return higher_highs_higher_lows(df, 40, SWING_ORDER) and close > float(e50)
        return lower_highs_lower_lows(df, 40, SWING_ORDER) and close < float(e50)

    def step7_intraday_confirmation(
        self, intraday: pd.DataFrame | None, direction: str
    ) -> tuple[bool, bool]:
        """Returns (confirmed, data_available). C2: too few bars is 'unavailable'."""
        if intraday is None or len(intraday) < self.min_intraday_bars:
            return False, False

        e20 = ema(intraday["close"], 20).iloc[-1]
        e50 = ema(intraday["close"], 50).iloc[-1]
        if pd.isna(e20) or pd.isna(e50):
            return False, False

        close = float(intraday["close"].iloc[-1])
        if direction == "Uptrend":
            return bool(close > float(e20) > float(e50)), True
        return bool(close < float(e20) < float(e50)), True

    def step8_rsi(self, df: pd.DataFrame, direction: str) -> bool:
        series = rsi(df["close"], 14)
        val, prev = series.iloc[-1], series.iloc[-2]
        if pd.isna(val) or pd.isna(prev):
            return False
        val, prev = float(val), float(prev)
        if direction == "Uptrend":
            return 50.0 <= val <= 70.0 and val > prev and val <= 80.0
        return 30.0 <= val <= 50.0 and val < prev and val >= 20.0

    def step9_macd(self, df: pd.DataFrame, direction: str) -> bool:
        m = macd(df["close"]).iloc[-1]
        if m.isna().any():
            return False
        if direction == "Uptrend":
            return bool(m["macd"] > m["signal"] and m["hist"] > 0)
        return bool(m["macd"] < m["signal"] and m["hist"] < 0)

    def step10_ema_alignment(self, df: pd.DataFrame, direction: str) -> bool:
        e20 = ema(df["close"], 20)
        e50 = ema(df["close"], 50)
        e200 = ema(df["close"], 200)
        if any(pd.isna(s.iloc[-1]) for s in (e20, e50, e200)):
            return False

        close = float(df["close"].iloc[-1])
        a, b, c = float(e20.iloc[-1]), float(e50.iloc[-1]), float(e200.iloc[-1])
        # "Sloping" is checked over 5 bars to avoid single-bar noise.
        rising = a > float(e20.iloc[-6]) if len(e20) > 6 else True

        if direction == "Uptrend":
            return close > a > b > c and rising
        return close < a < b < c and not rising

    def step11_volume(self, df: pd.DataFrame) -> bool:
        vol = float(df["volume"].iloc[-1])
        prev = float(df["volume"].iloc[-2])
        vsma = sma(df["volume"], 20).iloc[-1]
        above_sma = bool(pd.notna(vsma) and vol > float(vsma))
        return above_sma or (prev > 0 and vol >= 1.5 * prev)

    def step12_score(self, st: StepResults) -> tuple[float, float]:
        """Raw total and the maximum attainable, per C1."""
        raw = 0.0
        raw += 20.0 if st.validated else 0.0
        raw += 25.0 if st.line and st.line.touches >= 3 else 15.0
        raw += 15.0 if st.daily else 0.0
        raw += 10.0 if st.intraday else 0.0
        raw += 10.0 if st.rsi_ok else 0.0
        raw += 10.0 if st.macd_ok else 0.0
        raw += 10.0 if st.ema_ok else 0.0
        raw += 10.0 if st.volume_ok else 0.0

        maximum = 20.0 + (25.0 if st.line and st.line.touches >= 3 else 15.0) + 65.0
        # No intraday feed means 10 points are unreachable; exclude them from the
        # denominator so those symbols are not silently penalised.
        if not st.intraday_available:
            maximum -= 10.0
        return raw, maximum

    def step13_classify(self, score: float) -> str:
        if score >= 90:
            return "Excellent Setup"
        if score >= 80:
            return "High Probability Setup"
        if score >= 70:
            return "Good Setup"
        if score >= 60:
            return "Moderate Setup"
        return "Reject"

    # -- output ------------------------------------------------------------

    def _row(
        self,
        ctx: ScanContext,
        df: pd.DataFrame,
        line: Trendline,
        st: StepResults,
        score: float,
        classification: str,
    ) -> ScanRow:
        expected = line.value_at(len(df) - 1)
        close = float(df["close"].iloc[-1])
        rsi_val = rsi(df["close"], 14).iloc[-1]

        if classification == "Reject":
            signal = Signal.REJECT
        elif score >= 80:
            signal = Signal.BULLISH if line.direction == "Uptrend" else Signal.BEARISH
        else:
            signal = Signal.WATCHLIST

        return ScanRow(
            symbol=ctx.symbol,
            signal=signal,
            score=score,
            fields={
                "Trend Direction": line.direction,
                "Daily Status": "Confirmed" if st.daily else "Not Confirmed",
                "15-Min Status": (
                    "Confirmed" if st.intraday
                    else ("Not Confirmed" if st.intraday_available else "No Data")
                ),
                "Touches": line.touches,
                "Distance %": round(pct(close - expected, expected), 2),
                "RSI Value": round(float(rsi_val), 1) if pd.notna(rsi_val) else None,
                "MACD Status": "Aligned" if st.macd_ok else "Not Aligned",
                "EMA Alignment": "Aligned" if st.ema_ok else "Not Aligned",
                "Volume Confirmation": "Yes" if st.volume_ok else "No",
                "Strength Score": round(score, 1),
                "Overall Signal": classification,
                "Trendline Price": round(expected, 2),
                "Raw Score": f"{st.values.get('raw', 0):.0f}/{st.values.get('max', 0):.0f}",
            },
            overlays={"trendline": (line.slope, line.intercept), "from": line.start_pos},
        )


register(TrendlineScanner())
