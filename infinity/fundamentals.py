"""3.2 Fundamentals feed.

Revises review finding B3. The original estimate was that only six of the ten
spec indicators were obtainable for free; a live probe showed **eight** are:

| # | Spec indicator      | Status                                            |
|---|---------------------|---------------------------------------------------|
| 1 | Market Cap          | available                                         |
| 2 | P/E                 | available                                         |
| 3 | EPS                 | available                                         |
| 4 | Book Value          | available                                         |
| 5 | Face Value          | **unavailable** -- not published by the feed      |
| 6 | PEG                 | available                                         |
| 7 | ROE                 | available; derived from EPS/BookValue when absent |
| 8 | Debt/Equity         | available                                         |
| 9 | Promoter Holding %  | **proxy** -- Yahoo "insiders", not the NSE filing |
|10 | Pledged Shares %    | **unavailable** -- NSE XBRL filings only          |

Every value carries its own provenance so the UI can distinguish a reported
figure from a derived one from a proxy. Nothing is silently invented: an
unavailable indicator renders as "not available", never as zero.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from infinity.market_clock import now_ist

log = logging.getLogger(__name__)


class Provenance(StrEnum):
    REPORTED = "reported"      # straight from the feed
    DERIVED = "derived"        # computed from other reported figures
    PROXY = "proxy"            # a related but not identical measure
    UNAVAILABLE = "unavailable"

    @property
    def needs_caveat(self) -> bool:
        return self is not Provenance.REPORTED


@dataclass(frozen=True)
class Metric:
    label: str
    value: float | None
    unit: str = ""
    provenance: Provenance = Provenance.REPORTED
    note: str = ""

    @property
    def available(self) -> bool:
        return self.value is not None

    def display(self) -> str:
        if self.value is None:
            return "not available"
        if self.unit == "Rs Cr":
            return f"Rs {self.value:,.0f} Cr"
        if self.unit == "%":
            return f"{self.value:.2f}%"
        if self.unit == "Rs":
            return f"Rs {self.value:,.2f}"
        return f"{self.value:,.2f}"


@dataclass
class Fundamentals:
    symbol: str
    name: str = ""
    sector: str = ""
    industry: str = ""
    fetched_at: datetime = field(default_factory=now_ist)
    metrics: dict[str, Metric] = field(default_factory=dict)
    price: dict[str, float | None] = field(default_factory=dict)

    @property
    def available_count(self) -> int:
        return sum(1 for m in self.metrics.values() if m.available)

    def caveats(self) -> list[str]:
        return [
            f"{m.label}: {m.note}"
            for m in self.metrics.values()
            if m.note and m.provenance.needs_caveat
        ]


def _num(info: dict[str, Any], key: str) -> float | None:
    val = info.get(key)
    if val is None:
        return None
    try:
        out = float(val)
    except (TypeError, ValueError):
        return None
    # Yahoo returns 0 for "unknown" on several of these; treat as missing
    # rather than reporting a company with a P/E of exactly zero.
    return out if out != 0 else None


def fetch_fundamentals(symbol: str, suffix: str = ".NS") -> Fundamentals:
    """Fetch the 3.2 indicator set for one symbol."""
    import yfinance as yf

    ticker = symbol if symbol.endswith(suffix) else f"{symbol}{suffix}"
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as exc:
        log.warning("fundamentals unavailable for %s: %s", ticker, exc)
        info = {}

    out = Fundamentals(
        symbol=symbol.upper(),
        name=str(info.get("longName") or info.get("shortName") or ""),
        sector=str(info.get("sector") or ""),
        industry=str(info.get("industry") or ""),
    )

    market_cap = _num(info, "marketCap")
    eps = _num(info, "trailingEps")
    book = _num(info, "bookValue")
    roe = _num(info, "returnOnEquity")
    promoter = _num(info, "heldPercentInsiders")

    # ROE is frequently absent; EPS/BookValue is the standard reconstruction.
    if roe is not None:
        roe_metric = Metric("Return on Equity", roe * 100.0, "%", Provenance.REPORTED)
    elif eps is not None and book:
        roe_metric = Metric(
            "Return on Equity", 100.0 * eps / book, "%", Provenance.DERIVED,
            "computed as EPS / Book Value; not the reported figure",
        )
    else:
        roe_metric = Metric("Return on Equity", None, "%", Provenance.UNAVAILABLE)

    out.metrics = {
        "market_cap": Metric(
            "Market Capitalisation",
            market_cap / 1e7 if market_cap else None,  # rupees -> crore
            "Rs Cr",
        ),
        "pe": Metric("Price-to-Earnings", _num(info, "trailingPE")),
        "eps": Metric("Earnings Per Share", eps, "Rs"),
        "book_value": Metric("Book Value", book, "Rs"),
        "face_value": Metric(
            "Face Value", None, "Rs", Provenance.UNAVAILABLE,
            "not published by the market-data feed; sourced from NSE filings only",
        ),
        "peg": Metric("PEG Ratio", _num(info, "pegRatio")),
        "roe": roe_metric,
        "debt_equity": Metric("Debt to Equity", _num(info, "debtToEquity")),
        "promoter_holding": Metric(
            "Promoter Holding",
            promoter * 100.0 if promoter is not None else None,
            "%",
            Provenance.PROXY if promoter is not None else Provenance.UNAVAILABLE,
            "Yahoo 'held by insiders', which approximates but does not equal the "
            "NSE promoter-holding filing",
        ),
        "pledged_shares": Metric(
            "Pledged Shares", None, "%", Provenance.UNAVAILABLE,
            "available only in quarterly NSE shareholding-pattern XBRL filings",
        ),
    }

    out.price = {
        "ltp": _num(info, "currentPrice"),
        "day_high": _num(info, "dayHigh"),
        "day_low": _num(info, "dayLow"),
        "week52_high": _num(info, "fiftyTwoWeekHigh"),
        "week52_low": _num(info, "fiftyTwoWeekLow"),
    }
    return out


def peer_comparison(
    target: Fundamentals, peers: list[Fundamentals], keys: tuple[str, ...] = (
        "pe", "roe", "debt_equity", "market_cap",
    )
) -> list[dict[str, Any]]:
    """Side-by-side against the sector median (spec 3.2 peer comparison)."""
    rows = []
    for key in keys:
        mine = target.metrics.get(key)
        values = [
            p.metrics[key].value
            for p in peers
            if key in p.metrics and p.metrics[key].available
        ]
        median = None
        if values:
            ordered = sorted(values)
            mid = len(ordered) // 2
            median = (
                ordered[mid]
                if len(ordered) % 2
                else (ordered[mid - 1] + ordered[mid]) / 2.0
            )

        rows.append(
            {
                "Metric": mine.label if mine else key,
                target.symbol: mine.display() if mine else "not available",
                "Sector Median": (
                    Metric(mine.label, median, mine.unit).display()
                    if mine and median is not None else "not available"
                ),
                "Peers": len(values),
            }
        )
    return rows


def true_lifetime_ath(daily_bars) -> tuple[float | None, str]:
    """True all-time high across the full available history (spec 3.2)."""
    if daily_bars is None or len(daily_bars) == 0:
        return None, ""
    pos = daily_bars["high"].idxmax()
    row = daily_bars.loc[pos]
    date = str(row["ts"].date()) if "ts" in row else ""
    return float(row["high"]), date
