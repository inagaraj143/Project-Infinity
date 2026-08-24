"""3.6 50% Candle Rule Scanner.

Evaluates price against the 50% midpoint ``(High + Low) / 2`` of the most
recent *decisive* candle. Dojis and spinning tops (body < 25% of range) are
skipped by walking backwards until a decisive candle is found, so the reference
level comes from a candle that actually expressed direction.

Prediction: price holding above a bullish reference candle's midpoint favours
continuation; losing it favours reversal. Confidence combines how decisive the
reference candle was, how far price sits from the midpoint, and volume support.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from infinity.indicators import body_ratio, sma
from infinity.scanners.base import (
    ScanContext,
    ScanRow,
    Signal,
    normalise_score,
    pct,
    register,
    require_bars,
)

DECISIVE_BODY_MIN = 0.25  # body >= 25% of range
MAX_LOOKBACK = 20


@dataclass
class Candle50Scanner:
    name: str = "candle_50"
    title: str = "50% Candle Rule"
    section: str = "3.6"
    min_daily_bars: int = 30

    def scan(self, ctx: ScanContext) -> ScanRow | None:
        df = ctx.daily
        require_bars(df, self.min_daily_bars)

        ratios = body_ratio(df)
        ref_pos = self._find_reference(df, ratios)
        if ref_pos is None:
            return ScanRow(
                symbol=ctx.symbol,
                signal=Signal.NEUTRAL,
                score=0.0,
                fields={"Reference Candle": "none within lookback"},
            )

        ref = df.iloc[ref_pos]
        midpoint = (float(ref["high"]) + float(ref["low"])) / 2.0
        ref_bullish = float(ref["close"]) > float(ref["open"])

        last = df.iloc[-1]
        close = float(last["close"])
        distance = pct(close - midpoint, midpoint)
        above = close > midpoint

        # Continuation when price holds the decisive candle's own direction.
        if ref_bullish:
            direction = Signal.BULLISH if above else Signal.BEARISH
        else:
            direction = Signal.BEARISH if not above else Signal.BULLISH

        vol_sma = sma(df["volume"], 20).iloc[-1]
        vol_ok = bool(pd.notna(vol_sma) and float(last["volume"]) > float(vol_sma))

        confidence = self._confidence(
            body=float(ratios.iloc[ref_pos]),
            distance_pct=abs(distance),
            volume_ok=vol_ok,
            bars_since=len(df) - 1 - ref_pos,
        )

        # A coin-flip prediction is reported as Undecided, per the spec.
        signal = direction if confidence >= 50 else Signal.NEUTRAL

        return ScanRow(
            symbol=ctx.symbol,
            signal=signal,
            score=confidence,
            fields={
                "Close": round(close, 2),
                "Reference Date": str(ref["ts"].date()) if "ts" in ref else "",
                "Reference Type": "Bullish" if ref_bullish else "Bearish",
                "Midpoint (50%)": round(midpoint, 2),
                "Distance %": round(distance, 2),
                "Position": "Above" if above else "Below",
                "Bars Since Ref": len(df) - 1 - ref_pos,
                "Volume Support": "Yes" if vol_ok else "No",
                "Prediction": direction.value if confidence >= 50 else "Undecided",
                "Confidence": round(confidence, 1),
            },
            overlays={"midpoint_level": midpoint},
        )

    @staticmethod
    def _find_reference(df: pd.DataFrame, ratios: pd.Series) -> int | None:
        """Walk backwards from the last closed candle to the newest decisive one."""
        start = len(df) - 2  # skip the current candle; it is the one being predicted
        for pos in range(start, max(-1, start - MAX_LOOKBACK), -1):
            if pos < 0:
                break
            if float(ratios.iloc[pos]) >= DECISIVE_BODY_MIN:
                return pos
        return None

    @staticmethod
    def _confidence(body: float, distance_pct: float, volume_ok: bool, bars_since: int) -> float:
        """0-100. Decisiveness 40, distance 30, volume 15, recency 15."""
        decisive = min(body / 0.60, 1.0) * 40.0
        # Clear separation from the midpoint is more convincing than hugging it.
        separation = min(distance_pct / 3.0, 1.0) * 30.0
        volume = 15.0 if volume_ok else 0.0
        recency = max(0.0, 1.0 - bars_since / 10.0) * 15.0
        return normalise_score(decisive + separation + volume + recency, 100)


register(Candle50Scanner())
