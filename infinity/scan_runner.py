"""Scan runner -- executes scanners over a universe in dependency order.

This is where review finding C5 is actually resolved. Scanners never import or
call each other; the runner topologically orders them by ``depends_on`` and
threads earlier results into ``ScanContext.upstream``. One explicit DAG, no
import cycles, and a scanner can always tell whether a dependency was present.

Sector strength (spec 3.1's 10 points) needs peers, so it is computed once per
universe from median member performance and injected per symbol.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from infinity.data.models import Interval
from infinity.data.resolver import Resolver
from infinity.data.universe import UniverseList
from infinity.scanners.base import (
    REGISTRY,
    InsufficientHistory,
    ScanContext,
    Scanner,
    ScanRow,
    Signal,
)

log = logging.getLogger(__name__)

SECTOR_LOOKBACK = 20


@dataclass
class ScanOutcome:
    scanner: str
    rows: list[ScanRow] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)

    @property
    def qualifying(self) -> list[ScanRow]:
        return [r for r in self.rows if r.qualifies]

    def to_records(self, only_qualifying: bool = True) -> list[dict]:
        rows = self.qualifying if only_qualifying else self.rows
        return [r.to_dict() for r in sorted(rows, key=lambda r: -r.score)]


def order_scanners(names: list[str]) -> list[Scanner]:
    """Topologically order by ``depends_on``; dependencies not requested are ignored."""
    wanted = {n: REGISTRY[n] for n in names}
    ordered: list[Scanner] = []
    placed: set[str] = set()

    # Depth is bounded by the number of scanners, so a simple fixpoint is fine.
    for _ in range(len(wanted) + 1):
        for name, scanner in wanted.items():
            if name in placed:
                continue
            deps = [d for d in getattr(scanner, "depends_on", ()) if d in wanted]
            if all(d in placed for d in deps):
                ordered.append(scanner)
                placed.add(name)
        if len(placed) == len(wanted):
            break

    missing = set(wanted) - placed
    if missing:
        raise ValueError(f"dependency cycle among scanners: {sorted(missing)}")
    return ordered


def sector_scores(
    universe: UniverseList, bars: dict[str, pd.DataFrame], lookback: int = SECTOR_LOOKBACK
) -> dict[str, float]:
    """0-10 per industry, ranked by median member return over ``lookback`` bars."""
    per_industry: dict[str, list[float]] = {}
    for member in universe.members:
        df = bars.get(member.symbol)
        if df is None or len(df) < lookback + 1:
            continue
        closes = df["close"]
        ret = (float(closes.iloc[-1]) / float(closes.iloc[-lookback - 1]) - 1.0) * 100.0
        per_industry.setdefault(member.industry or "Unclassified", []).append(ret)

    if not per_industry:
        return {}

    medians = {k: float(pd.Series(v).median()) for k, v in per_industry.items()}
    ranked = sorted(medians.items(), key=lambda kv: kv[1], reverse=True)
    n = len(ranked)
    if n == 1:
        return {ranked[0][0]: 10.0}
    return {name: 10.0 * (n - 1 - i) / (n - 1) for i, (name, _) in enumerate(ranked)}


def run_scanners(
    scanner_names: list[str],
    universe: UniverseList,
    bars: dict[str, pd.DataFrame],
    intraday: dict[str, pd.DataFrame] | None = None,
) -> dict[str, ScanOutcome]:
    """Run scanners across a universe, threading upstream results forward."""
    intraday = intraday or {}
    scanners = order_scanners(scanner_names)
    sectors = sector_scores(universe, bars)
    outcomes = {s.name: ScanOutcome(s.name) for s in scanners}

    by_symbol = {m.symbol: m for m in universe.members}

    for symbol, df in bars.items():
        member = by_symbol.get(symbol)
        upstream: dict[str, ScanRow] = {}

        industry = member.industry if member else ""
        if industry in sectors:
            upstream["_sector_rank"] = ScanRow(symbol, Signal.NEUTRAL, sectors[industry])

        for scanner in scanners:
            ctx = ScanContext(
                symbol=symbol,
                daily=df,
                intraday=intraday.get(symbol),
                industry=industry,
                name=member.name if member else "",
                upstream=upstream,
            )
            try:
                row = scanner.scan(ctx)
            except InsufficientHistory as exc:
                # Spec 7: excluded with a reason, never a silent non-match.
                outcomes[scanner.name].skipped.append(
                    {"symbol": symbol, "reason": str(exc)}
                )
                continue
            except Exception as exc:  # one bad symbol must not kill the scan
                log.warning("%s failed on %s: %s", scanner.name, symbol, exc)
                outcomes[scanner.name].skipped.append(
                    {"symbol": symbol, "reason": f"{type(exc).__name__}: {exc}"}
                )
                continue

            if row is not None:
                outcomes[scanner.name].rows.append(row)
                upstream[scanner.name] = row

    return outcomes


def load_bars(
    resolver: Resolver, symbols: list[str], lookback_days: int = 365 * 12
) -> dict[str, pd.DataFrame]:
    """Read daily bars for a symbol list, skipping anything unavailable."""
    out: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        res = resolver.resolve(symbol, Interval.DAY, lookback_days)
        if res.ok and len(res.df):
            out[symbol] = res.df
    return out


def load_intraday(
    resolver: Resolver,
    symbols: list[str],
    interval: Interval = Interval.MIN_15,
    lookback_days: int = 31,
) -> dict[str, pd.DataFrame]:
    """Read intraday bars, for spec 3.3 Step 7's dual-timeframe confirmation.

    Upstox caps an intraday request at 31 days, which at 15 minutes is ~600
    bars -- past the 200 Step 7 requires. Symbols that fail are simply absent,
    and Step 7 then reports 'No Data' rather than failing the symbol.
    """
    out: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        try:
            res = resolver.resolve(symbol, interval, lookback_days)
        except Exception as exc:
            log.debug("intraday unavailable for %s: %s", symbol, exc)
            continue
        if res.ok and len(res.df):
            out[symbol] = res.df
    return out
