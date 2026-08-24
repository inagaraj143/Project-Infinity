"""3.5 Resistance Breakout Scanner -- strict 4-condition filter.

All four conditions must hold:

1. ``ceil(mean(typical price over last 50 days)) < today's close``
2. EMA 8 > EMA 13
3. today's volume >= 1.50x yesterday's volume
4. close > open (green candle)

Two spec ambiguities are surfaced as options rather than silently decided
(review findings C3):

* The spec writes the 50-day average over ``(Open+High+Close)/3``, which omits
  the Low. The standard typical price is ``(H+L+C)/3``. ``typical_price_mode``
  defaults to the spec's literal wording so behaviour matches the document;
  set it to ``"hlc"`` for the conventional definition.
* ``ceil()`` quantises the threshold to whole rupees -- negligible on a
  Rs 3,000 stock, a hard filter on a Rs 14 one. ``use_ceil`` keeps the spec's
  literal behaviour and can be turned off.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from infinity.indicators import ema
from infinity.scanners.base import (
    ScanContext,
    ScanRow,
    Signal,
    normalise_score,
    pct,
    register,
    require_bars,
)

CEILING_WINDOW = 50
VOLUME_RATIO_MIN = 1.50


@dataclass
class ResistanceBreakoutScanner:
    name: str = "resistance_breakout"
    title: str = "Resistance Breakout"
    section: str = "3.5"
    min_daily_bars: int = CEILING_WINDOW + 15  # + EMA13 warm-up
    typical_price_mode: str = "ohc"  # "ohc" = spec literal; "hlc" = conventional
    use_ceil: bool = True

    def scan(self, ctx: ScanContext) -> ScanRow | None:
        df = ctx.daily
        require_bars(df, self.min_daily_bars)

        if self.typical_price_mode == "hlc":
            typical = (df["high"] + df["low"] + df["close"]) / 3.0
        else:
            typical = (df["open"] + df["high"] + df["close"]) / 3.0

        ceiling_raw = float(typical.tail(CEILING_WINDOW).mean())
        ceiling = math.ceil(ceiling_raw) if self.use_ceil else ceiling_raw

        ema8 = ema(df["close"], 8)
        ema13 = ema(df["close"], 13)

        last = df.iloc[-1]
        prev_vol = float(df["volume"].iloc[-2])
        close = float(last["close"])
        open_ = float(last["open"])
        vol = float(last["volume"])

        e8, e13 = float(ema8.iloc[-1]), float(ema13.iloc[-1])
        vol_ratio = vol / prev_vol if prev_vol else 0.0

        c1 = close > ceiling
        c2 = e8 > e13
        c3 = vol_ratio >= VOLUME_RATIO_MIN
        c4 = close > open_

        passed = [c1, c2, c3, c4]
        score = normalise_score(sum(passed), 4)
        # Strict filter: all four, or it is not a breakout.
        signal = Signal.BULLISH if all(passed) else Signal.REJECT

        return ScanRow(
            symbol=ctx.symbol,
            signal=signal,
            score=score,
            fields={
                "Close": round(close, 2),
                "50D Ceiling": round(ceiling, 2),
                "Above Ceiling %": round(pct(close - ceiling, ceiling), 2),
                "EMA 8": round(e8, 2),
                "EMA 13": round(e13, 2),
                "Volume Ratio": round(vol_ratio, 2),
                "Green Candle": "Yes" if c4 else "No",
                "Conditions Met": f"{sum(passed)}/4",
            },
            overlays={"resistance_level": ceiling},
        )


register(ResistanceBreakoutScanner())
