"""Refresh reference data: the NSE_EQ instrument master and index universes.

Both sources are public and unauthenticated, so this runs fine in CI.

    py scripts/refresh_reference.py
    py scripts/refresh_reference.py --universe nifty500 --universe nifty50
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from infinity.config import Universe  # noqa: E402
from infinity.data.instruments import InstrumentMasterError, get_master  # noqa: E402
from infinity.data.universe import UniverseError, build_universe, save_universe  # noqa: E402

log = logging.getLogger("refresh_reference")


def main() -> int:
    ap = argparse.ArgumentParser(description="Refresh instrument master and universes.")
    ap.add_argument(
        "--universe", action="append", choices=[u.value for u in Universe],
        help="repeatable; defaults to nifty50 + nifty500",
    )
    ap.add_argument("--force", action="store_true", help="refetch even if cached today")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    try:
        master = get_master(force_refresh=args.force)
    except InstrumentMasterError as exc:
        log.error("instrument master unavailable: %s", exc)
        return 1
    log.info("instrument master: %d NSE equities (fetched %s)", len(master), master.fetched_at)

    targets = [Universe(u) for u in (args.universe or ["nifty50", "nifty500"])]
    failed = 0

    for u in targets:
        if u is Universe.ALL_NSE:
            log.info("skipping %s: derived from the master at run time, not persisted", u.label)
            continue
        try:
            ul = build_universe(u, master=master)
        except UniverseError as exc:
            log.error("%s failed: %s", u.label, exc)
            failed += 1
            continue

        path = save_universe(ul)
        keyed = sum(1 for m in ul.members if m.instrument_key)
        log.info(
            "%s: %d members, %d keyed, %d unresolved, %d industries -> %s",
            u.label, len(ul), keyed, len(ul.unresolved), len(ul.by_industry), path.name,
        )
        if ul.unresolved:
            log.warning("  unresolved: %s", ", ".join(ul.unresolved[:15]))

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
