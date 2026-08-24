"""Scanner interface shared by all modules (spec 3).

A scanner is a pure function: bars in, an optional row out. It never fetches,
never touches the clock, and never writes files -- which is what makes each of
the 13 trendline steps independently testable against synthetic fixtures
(spec 8).

Ordering note (resolves review finding C5): the spec has 3.1 read confirmed
signals from 3.3 and 3.6. Rather than let modules call each other, the runner
executes them in dependency order and hands the results forward through
``ScanContext.upstream``, so there is one explicit DAG and no import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

import pandas as pd


class Signal(StrEnum):
    """Rendered as the spec 4.4 chips; colour is always paired with this text."""

    BULLISH = "Bullish"
    BEARISH = "Bearish"
    WATCHLIST = "Watchlist"
    NEUTRAL = "Neutral"
    REJECT = "Reject"

    @property
    def chip_kind(self) -> str:
        return {
            Signal.BULLISH: "bull",
            Signal.BEARISH: "bear",
            Signal.WATCHLIST: "watch",
            Signal.NEUTRAL: "muted",
            Signal.REJECT: "muted",
        }[self]

    @property
    def glyph(self) -> str:
        return {
            Signal.BULLISH: "▲",
            Signal.BEARISH: "▼",
            Signal.WATCHLIST: "◆",
            Signal.NEUTRAL: "●",
            Signal.REJECT: "○",
        }[self]


class InsufficientHistory(Exception):
    """Spec 7: too few bars -- excluded with a reason, not treated as a non-match."""

    def __init__(self, have: int, need: int) -> None:
        super().__init__(f"Insufficient History ({have} bars, need {need})")
        self.have = have
        self.need = need


@dataclass
class ScanContext:
    """Everything a scanner may read beyond its own bars."""

    symbol: str
    daily: pd.DataFrame
    intraday: pd.DataFrame | None = None
    industry: str = ""
    name: str = ""
    # Results from scanners that ran earlier in the DAG, keyed by scanner name.
    upstream: dict[str, ScanRow] = field(default_factory=dict)


@dataclass
class ScanRow:
    """One row of a scanner's output table."""

    symbol: str
    signal: Signal
    score: float = 0.0
    fields: dict[str, Any] = field(default_factory=dict)
    # Chart overlays this row implies (trendlines, zones); consumed by 4.5.
    overlays: dict[str, Any] = field(default_factory=dict)

    @property
    def qualifies(self) -> bool:
        return self.signal not in (Signal.REJECT, Signal.NEUTRAL)

    def to_dict(self) -> dict[str, Any]:
        return {"Symbol": self.symbol, "Signal": self.signal.value,
                "Score": round(self.score, 1), **self.fields}


class Scanner(Protocol):
    name: str
    title: str
    section: str
    min_daily_bars: int

    def scan(self, ctx: ScanContext) -> ScanRow | None: ...


REGISTRY: dict[str, Scanner] = {}


def register(scanner: Scanner) -> Scanner:
    """Register a scanner instance under its ``name``."""
    if scanner.name in REGISTRY:
        raise ValueError(f"duplicate scanner name: {scanner.name}")
    REGISTRY[scanner.name] = scanner
    return scanner


def get_scanner(name: str) -> Scanner:
    if name not in REGISTRY:
        raise KeyError(f"unknown scanner {name!r}; have {sorted(REGISTRY)}")
    return REGISTRY[name]


def require_bars(df: pd.DataFrame, minimum: int) -> None:
    have = 0 if df is None else len(df)
    if have < minimum:
        raise InsufficientHistory(have, minimum)


def pct(numerator: float, denominator: float) -> float:
    """Percentage, guarding the zero denominator."""
    return 0.0 if not denominator else 100.0 * numerator / denominator


def normalise_score(raw: float, max_possible: float) -> float:
    """Rescale a raw point total onto 0-100.

    Resolves review finding C1: spec 3.3 Step 12 lists components summing to
    110 with 3+ touches but 100 with 2, while Step 13 grades on a 0-100 scale.
    Without this, "90-100 Excellent" means different things depending on touch
    count. Every scanner reports a normalised score and also carries its raw
    total in ``fields`` so the arithmetic stays auditable.
    """
    if max_possible <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * raw / max_possible))


def slope_pct_per_bar(y0: float, y1: float, bars: int) -> float:
    """Line slope as % of the starting price per bar -- scale-free across stocks."""
    if bars <= 0 or y0 == 0:
        return 0.0
    return 100.0 * (y1 - y0) / (abs(y0) * bars)
