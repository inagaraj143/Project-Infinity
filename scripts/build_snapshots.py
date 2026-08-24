"""EOD snapshot builder -- the job GitHub Actions runs after market close.

Fetches settled daily bars for a universe and writes them to the JSON store, so
the hosted Streamlit app makes zero API calls while the market is closed.

    py scripts/build_snapshots.py --universe nifty50
    py scripts/build_snapshots.py --universe nifty500 --workers 8
    py scripts/build_snapshots.py --universe nifty50 --limit 5 -v   # quick check

Exits non-zero if the failure rate exceeds --max-failure-pct, so a broken feed
fails the workflow loudly instead of silently committing a half-empty dataset.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from infinity.config import Universe, ensure_dirs  # noqa: E402
from infinity.data.batch import BatchProgress, fetch_many  # noqa: E402
from infinity.data.models import Interval  # noqa: E402
from infinity.data.resolver import build_default_resolver  # noqa: E402
from infinity.data.snapshot_store import write_scan  # noqa: E402
from infinity.data.universe import UniverseError, get_universe  # noqa: E402
from infinity.market_clock import last_session_close, market_status, now_ist  # noqa: E402

log = logging.getLogger("build_snapshots")


def main() -> int:
    ap = argparse.ArgumentParser(description="Build EOD JSON snapshots for a universe.")
    ap.add_argument("--universe", default="nifty50", choices=[u.value for u in Universe])
    ap.add_argument("--lookback-days", type=int, default=365 * 12)
    ap.add_argument("--workers", type=int, default=4, help="parallel fetches")
    ap.add_argument("--rate", type=float, default=8.0, help="max requests/second")
    ap.add_argument("--max-failure-pct", type=float, default=10.0)
    ap.add_argument("--limit", type=int, default=0, help="debug: cap symbol count")
    ap.add_argument("--refresh-universe", action="store_true", help="refetch constituents")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    ensure_dirs()

    universe = Universe(args.universe)
    try:
        ul = get_universe(universe, refresh=args.refresh_universe)
    except UniverseError as exc:
        log.error("could not resolve %s: %s", universe.label, exc)
        return 1

    symbols = ul.symbols[: args.limit] if args.limit else ul.symbols
    log.info("%s: %d symbols, %d industries", universe.label, len(symbols), len(ul.by_industry))

    status = market_status()
    if status.is_live:
        log.warning(
            "Market is LIVE. Snapshots built now capture unsettled intraday values "
            "and will be treated as stale after %s.", status.last_close,
        )
    if not status.calendar_verified:
        log.warning("NSE holiday calendar is unverified -- see data/nse_holidays.json")

    # Upstox primary, yfinance fallback -- candle endpoints need no auth
    # (ADR 0002), so this chain is identical in CI and locally.
    resolver = build_default_resolver()
    log.info("provider chain: %s", [p.name for p in resolver.providers])

    last_logged = 0

    def on_progress(p: BatchProgress) -> None:
        nonlocal last_logged
        step = max(1, p.total // 20)
        if p.completed - last_logged >= step or p.completed == p.total:
            last_logged = p.completed
            log.info("%s", p)

    result = fetch_many(
        resolver,
        symbols,
        interval=Interval.DAY,
        lookback_days=args.lookback_days,
        workers=args.workers,
        rate_per_sec=args.rate,
        on_progress=on_progress,
    )

    for s in result.skipped[:20]:
        log.warning("SKIPPED %-14s %s", s.symbol, s.reason)
    if len(result.skipped) > 20:
        log.warning("... and %d more skipped", len(result.skipped) - 20)

    # Spec 7: skipped symbols are reported with a reason, never silently dropped.
    write_scan(
        "universe_meta",
        rows=[
            {
                "symbol": m.symbol,
                "name": m.name,
                "industry": m.industry,
                "instrument_key": m.instrument_key,
                "bars": len(result.resolutions[m.symbol].df),
                "source": result.resolutions[m.symbol].source.value,
            }
            for m in ul.members
            if m.symbol in result.resolutions
        ],
        universe=universe.value,
        meta={
            "requested": result.requested,
            "succeeded": result.succeeded,
            "skipped": [{"symbol": s.symbol, "reason": s.reason} for s in result.skipped],
            "failure_pct": round(result.failure_pct, 2),
            "network_calls": result.network_calls,
            "cache_hits": result.cache_hits,
            "fallback_symbols": result.fallback_symbols,
            "halted": result.halted,
            "halt_reason": result.halt_reason,
            "elapsed_sec": round(result.elapsed_sec, 1),
            "session_close": last_session_close().isoformat(),
            "built_at": now_ist().isoformat(),
        },
    )

    log.info(
        "done: %d/%d ok, %d skipped (%.1f%%) in %.1fs | %d network, %d cached",
        result.succeeded, result.requested, len(result.skipped),
        result.failure_pct, result.elapsed_sec, result.network_calls, result.cache_hits,
    )

    if result.halted:
        log.error("HALTED: %s - partial results preserved", result.halt_reason)
        return 2
    if result.failure_pct > args.max_failure_pct:
        log.error(
            "failure rate %.1f%% exceeds --max-failure-pct %.1f%%",
            result.failure_pct, args.max_failure_pct,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
