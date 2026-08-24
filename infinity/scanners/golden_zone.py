"""3.1 Top Ranked Stocks -- Golden Zone Engine.

Scores stocks pulling back into the 50.0%-61.8% Fibonacci retracement of the
most recent swing move, out of 100:

| Component            | Points |
|----------------------|--------|
| Trend alignment      | 30     |
| Golden Zone proximity| 30     |
| Volume confirmation  | 15     |
| Candle pattern       | 15     |
| Sector strength      | 10     |

**Composite dependency (review finding C5).** The spec says this module reads
confirmed signals from 3.3 and 3.6 "where available" without defining the
absent case. Here the runner passes them through ``ctx.upstream``; when a
dependency is missing the component falls back to computing its own value and
the row records which path was taken in ``Trend Source`` / ``Candle Source``,
so a score is never silently built on different inputs for different symbols.

Sector strength needs peers, so it is supplied by the runner via
``ctx.upstream`` metadata rather than recomputed per symbol.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from infinity.indicators import ema, is_bullish, sma, swing_highs, swing_lows
from infinity.scanners.base import (
    ScanContext,
    ScanRow,
    Signal,
    normalise_score,
    register,
    require_bars,
)

GOLDEN_LOW = 0.500
GOLDEN_HIGH = 0.618
SWING_WINDOW = 120
SWING_ORDER = 4


@dataclass
class GoldenZoneScanner:
    name: str = "golden_zone"
    title: str = "Top Ranked (Golden Zone)"
    section: str = "3.1"
    min_daily_bars: int = 200
    # Runs last: reads trendlines (3.3) and candle_50 (3.6) from upstream.
    depends_on: tuple[str, ...] = ("trendlines", "candle_50")

    def scan(self, ctx: ScanContext) -> ScanRow | None:
        df = ctx.daily.reset_index(drop=True)
        require_bars(df, self.min_daily_bars)

        swing = self._latest_swing(df)
        if swing is None:
            return ScanRow(ctx.symbol, Signal.NEUTRAL, 0.0, {"Zone": "no swing found"})

        swing_low, swing_high, direction = swing
        close = float(df["close"].iloc[-1])
        rng = swing_high - swing_low
        if rng <= 0:
            return ScanRow(ctx.symbol, Signal.NEUTRAL, 0.0, {"Zone": "degenerate swing"})

        # Retracement measured from the swing extreme the move ran to.
        retrace = (swing_high - close) / rng if direction == "Up" else (close - swing_low) / rng
        zone_lo = swing_high - GOLDEN_HIGH * rng
        zone_hi = swing_high - GOLDEN_LOW * rng
        if direction == "Down":
            zone_lo = swing_low + GOLDEN_LOW * rng
            zone_hi = swing_low + GOLDEN_HIGH * rng
        in_zone = GOLDEN_LOW <= retrace <= GOLDEN_HIGH

        trend_pts, trend_src = self._trend_component(ctx, df)
        zone_pts = self._zone_component(retrace)
        vol_pts = self._volume_component(df)
        candle_pts, candle_src = self._candle_component(ctx, df)
        sector_pts = self._sector_component(ctx)

        raw = trend_pts + zone_pts + vol_pts + candle_pts + sector_pts
        score = normalise_score(raw, 100)

        if in_zone and score >= 70:
            signal = Signal.BULLISH
        elif score >= 55:
            signal = Signal.WATCHLIST
        else:
            signal = Signal.REJECT

        return ScanRow(
            symbol=ctx.symbol,
            signal=signal,
            score=score,
            fields={
                "Close": round(close, 2),
                "Swing Low": round(swing_low, 2),
                "Swing High": round(swing_high, 2),
                "Retracement %": round(retrace * 100, 1),
                "In Golden Zone": "Yes" if in_zone else "No",
                "Zone Range": f"{min(zone_lo, zone_hi):.2f}-{max(zone_lo, zone_hi):.2f}",
                "Trend (30)": round(trend_pts, 1),
                "Zone (30)": round(zone_pts, 1),
                "Volume (15)": round(vol_pts, 1),
                "Candle (15)": round(candle_pts, 1),
                "Sector (10)": round(sector_pts, 1),
                "Trend Source": trend_src,
                "Candle Source": candle_src,
                "Industry": ctx.industry or "Unclassified",
            },
            overlays={"golden_zone": (min(zone_lo, zone_hi), max(zone_lo, zone_hi))},
        )

    # -- components --------------------------------------------------------

    @staticmethod
    def _latest_swing(df: pd.DataFrame) -> tuple[float, float, str] | None:
        window = df.tail(SWING_WINDOW).reset_index(drop=True)
        hi = swing_highs(window["high"], SWING_ORDER)
        lo = swing_lows(window["low"], SWING_ORDER)
        if len(hi) == 0 or len(lo) == 0:
            return None

        last_hi, last_lo = int(hi[-1]), int(lo[-1])
        high = float(window["high"].iloc[last_hi])
        low = float(window["low"].iloc[last_lo])
        # Whichever extreme came last tells us which way the leg ran.
        return (low, high, "Up" if last_hi > last_lo else "Down")

    def _trend_component(self, ctx: ScanContext, df: pd.DataFrame) -> tuple[float, str]:
        """30 pts. Prefers 3.3's verdict; falls back to an EMA stack."""
        upstream = ctx.upstream.get("trendlines")
        if upstream is not None and upstream.qualifies:
            return upstream.score * 0.30, "3.3"

        e20, e50 = ema(df["close"], 20).iloc[-1], ema(df["close"], 50).iloc[-1]
        if pd.isna(e20) or pd.isna(e50):
            return 0.0, "fallback"
        close = float(df["close"].iloc[-1])
        if close > float(e20) > float(e50):
            return 30.0, "fallback"
        if close > float(e50):
            return 18.0, "fallback"
        return 0.0, "fallback"

    @staticmethod
    def _zone_component(retrace: float) -> float:
        """30 pts, peaking at the 0.559 midpoint of the golden zone."""
        centre = (GOLDEN_LOW + GOLDEN_HIGH) / 2.0
        half = (GOLDEN_HIGH - GOLDEN_LOW) / 2.0
        distance = abs(retrace - centre)
        if distance <= half:
            return 30.0
        # Decays outside the zone, reaching zero one zone-width away.
        return max(0.0, 30.0 * (1.0 - (distance - half) / (half * 4.0)))

    @staticmethod
    def _volume_component(df: pd.DataFrame) -> float:
        """15 pts. A pullback on fading volume is the constructive case."""
        vsma = sma(df["volume"], 20).iloc[-1]
        if pd.isna(vsma) or float(vsma) <= 0:
            return 0.0
        ratio = float(df["volume"].iloc[-1]) / float(vsma)
        if ratio < 0.8:
            return 15.0
        if ratio < 1.2:
            return 10.0
        return 5.0

    def _candle_component(self, ctx: ScanContext, df: pd.DataFrame) -> tuple[float, str]:
        """15 pts. Prefers 3.6's confidence; falls back to a green-candle check."""
        upstream = ctx.upstream.get("candle_50")
        if upstream is not None and upstream.signal is Signal.BULLISH:
            return upstream.score * 0.15, "3.6"
        if upstream is not None:
            return 0.0, "3.6"
        return (15.0 if bool(is_bullish(df).iloc[-1]) else 0.0), "fallback"

    @staticmethod
    def _sector_component(ctx: ScanContext) -> float:
        """10 pts from the runner's precomputed sector ranking."""
        rank = ctx.upstream.get("_sector_rank")
        if rank is None or not isinstance(getattr(rank, "score", None), float):
            return 0.0
        return max(0.0, min(10.0, rank.score))


register(GoldenZoneScanner())
