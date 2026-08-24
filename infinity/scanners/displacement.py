"""3.7 Institutional Displacement Engine.

Detects candles showing sudden high-conviction directional movement:

1. Range expansion  -- (High - Low) >= 2.0x the 20-day ATR
2. Volume surge     -- volume >= 3.0x the 20-day volume SMA
3. Directional close -- close in the top 25% (bullish) or bottom 25% (bearish)
4. Gap context      -- optional flag, not required

**Imbalance zone.** The spec describes the zone as the gap between the prior
candle's close and the displacement candle's open-side extreme, which emits a
zone even when no gap exists (review finding C4). This implements the standard
three-candle fair-value gap instead -- bullish: ``candle[i-1].high ->
candle[i+1].low``, bearish: ``candle[i+1].high -> candle[i-1].low`` -- and only
reports a zone when one genuinely exists. ``zone_mode="spec"`` restores the
literal two-candle wording.

Status follows the spec: >= 2 subsequent candles holding outside the zone is
Confirmed; a close back inside within 5 candles is Failed; otherwise Pending.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from infinity.indicators import atr, sma
from infinity.scanners.base import (
    ScanContext,
    ScanRow,
    Signal,
    normalise_score,
    register,
    require_bars,
)

RANGE_ATR_MULT = 2.0
VOLUME_SMA_MULT = 3.0
CLOSE_QUARTILE = 0.25
CONFIRM_BARS = 2
FAIL_WINDOW = 5
SEARCH_WINDOW = 40


@dataclass
class DisplacementScanner:
    name: str = "displacement"
    title: str = "Institutional Displacement"
    section: str = "3.7"
    min_daily_bars: int = 60
    zone_mode: str = "fvg"  # "fvg" = standard 3-candle; "spec" = literal wording

    def scan(self, ctx: ScanContext) -> ScanRow | None:
        df = ctx.daily.reset_index(drop=True)
        require_bars(df, self.min_daily_bars)

        atr20 = atr(df["high"], df["low"], df["close"], 20)
        vol20 = sma(df["volume"], 20)
        rng = df["high"] - df["low"]
        # np.nan, not pd.NA: pd.NA upcasts the series to object dtype and the
        # subsequent float() conversion then raises on every symbol.
        span = rng.replace(0.0, np.nan)
        close_pos = ((df["close"] - df["low"]) / span).astype(float).fillna(0.5)

        found = self._latest_displacement(df, atr20, vol20, rng, close_pos)
        if found is None:
            return ScanRow(ctx.symbol, Signal.NEUTRAL, 0.0, {"Displacement": "None"})

        pos, direction = found
        bar = df.iloc[pos]
        range_mult = float(rng.iloc[pos] / atr20.iloc[pos])
        vol_mult = float(df["volume"].iloc[pos] / vol20.iloc[pos])
        gapped = self._is_gap(df, pos, direction)

        zone = self._imbalance_zone(df, pos, direction)
        status = self._status(df, pos, zone, direction)

        score = self._score(range_mult, vol_mult, gapped, status)
        signal = {
            "Confirmed": Signal.BULLISH if direction == "Bullish" else Signal.BEARISH,
            "Pending": Signal.WATCHLIST,
            "Failed": Signal.REJECT,
        }[status]

        return ScanRow(
            symbol=ctx.symbol,
            signal=signal,
            score=score,
            fields={
                "Displacement Date": str(bar["ts"].date()) if "ts" in bar else "",
                "Direction": direction,
                "Range vs ATR": round(range_mult, 2),
                "Volume vs SMA": round(vol_mult, 2),
                "Gap Displacement": "Y" if gapped else "N",
                "Imbalance Zone": (
                    f"{zone[0]:.2f}-{zone[1]:.2f}" if zone else "none"
                ),
                "Status": status,
                "Bars Since": len(df) - 1 - pos,
            },
            overlays={"imbalance_zone": zone, "displacement_index": pos},
        )

    # -- detection ---------------------------------------------------------

    def _latest_displacement(
        self,
        df: pd.DataFrame,
        atr20: pd.Series,
        vol20: pd.Series,
        rng: pd.Series,
        close_pos: pd.Series,
    ) -> tuple[int, str] | None:
        start = len(df) - 1
        stop = max(20, len(df) - SEARCH_WINDOW)
        for pos in range(start, stop - 1, -1):
            a, v = atr20.iloc[pos], vol20.iloc[pos]
            if pd.isna(a) or pd.isna(v) or a <= 0 or v <= 0:
                continue
            if float(rng.iloc[pos]) < RANGE_ATR_MULT * float(a):
                continue
            if float(df["volume"].iloc[pos]) < VOLUME_SMA_MULT * float(v):
                continue

            cp = float(close_pos.iloc[pos])
            if cp >= 1.0 - CLOSE_QUARTILE:
                return pos, "Bullish"
            if cp <= CLOSE_QUARTILE:
                return pos, "Bearish"
        return None

    @staticmethod
    def _is_gap(df: pd.DataFrame, pos: int, direction: str) -> bool:
        if pos == 0:
            return False
        prev = df.iloc[pos - 1]
        cur = df.iloc[pos]
        if direction == "Bullish":
            return float(cur["low"]) > float(prev["high"])
        return float(cur["high"]) < float(prev["low"])

    def _imbalance_zone(
        self, df: pd.DataFrame, pos: int, direction: str
    ) -> tuple[float, float] | None:
        if self.zone_mode == "spec":
            if pos == 0:
                return None
            prev_close = float(df["close"].iloc[pos - 1])
            cur = df.iloc[pos]
            edge = float(cur["low"]) if direction == "Bullish" else float(cur["high"])
            lo, hi = sorted((prev_close, edge))
            return (lo, hi) if hi > lo else None

        # Standard three-candle fair-value gap.
        if pos == 0 or pos + 1 >= len(df):
            return None
        before, after = df.iloc[pos - 1], df.iloc[pos + 1]
        if direction == "Bullish":
            lo, hi = float(before["high"]), float(after["low"])
        else:
            lo, hi = float(after["high"]), float(before["low"])
        return (lo, hi) if hi > lo else None

    @staticmethod
    def _status(
        df: pd.DataFrame, pos: int, zone: tuple[float, float] | None, direction: str
    ) -> str:
        after = df.iloc[pos + 1 :]
        if zone is None or after.empty:
            return "Pending"

        lo, hi = zone
        for n, (_, bar) in enumerate(after.iterrows(), start=1):
            close = float(bar["close"])
            back_inside = lo <= close <= hi if direction == "Bullish" else lo <= close <= hi
            if back_inside and n <= FAIL_WINDOW:
                return "Failed"
            # A close through the far side of the zone also invalidates it.
            if direction == "Bullish" and close < lo and n <= FAIL_WINDOW:
                return "Failed"
            if direction == "Bearish" and close > hi and n <= FAIL_WINDOW:
                return "Failed"

        return "Confirmed" if len(after) >= CONFIRM_BARS else "Pending"

    @staticmethod
    def _score(range_mult: float, vol_mult: float, gapped: bool, status: str) -> float:
        raw = min(range_mult / 4.0, 1.0) * 35.0
        raw += min(vol_mult / 6.0, 1.0) * 35.0
        raw += 10.0 if gapped else 0.0
        raw += {"Confirmed": 20.0, "Pending": 10.0, "Failed": 0.0}[status]
        return normalise_score(raw, 100)


register(DisplacementScanner())
