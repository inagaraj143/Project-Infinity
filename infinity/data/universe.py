"""Universe (index constituent) resolution -- spec 2.3 / 4.3.

Nifty 50 / 100 / 500 constituents come from NSE's public archive CSVs, which
need no auth and carry an ``Industry`` column. That industry label is what
feeds the sector-strength component of the spec 3.1 scoring model, so it is
persisted with the universe rather than sourced separately.

``all_nse`` is derived from the instrument master instead of a CSV, and stays
local-only: ~2,600 symbols of bar history is far too large to keep in git.
"""

from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

from infinity.config import UNIVERSE_DIR, Universe, ensure_dirs
from infinity.data.instruments import InstrumentMaster, get_master
from infinity.market_clock import now_ist

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; ProjectInfinity/0.1)"

NSE_INDEX_CSV = {
    Universe.NIFTY_50: "https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
    Universe.NIFTY_500: "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
}

UNIVERSE_SCHEMA = 2


class UniverseError(RuntimeError):
    """Universe could not be resolved."""


@dataclass(frozen=True)
class Member:
    symbol: str
    name: str = ""
    industry: str = ""
    isin: str = ""
    instrument_key: str = ""


@dataclass(frozen=True)
class UniverseList:
    universe: Universe
    members: list[Member]
    built_at: datetime
    verified: bool = False
    unresolved: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.members)

    @property
    def symbols(self) -> list[str]:
        return [m.symbol for m in self.members]

    @property
    def by_industry(self) -> dict[str, list[str]]:
        """Sector buckets for the spec 3.1 sector-strength score."""
        out: dict[str, list[str]] = {}
        for m in self.members:
            out.setdefault(m.industry or "Unclassified", []).append(m.symbol)
        return out


def universe_path(universe: Universe) -> Path:
    return UNIVERSE_DIR / f"{universe.value}.json"


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def fetch_index_csv(universe: Universe, timeout: float = 30.0) -> pd.DataFrame:
    url = NSE_INDEX_CSV.get(universe)
    if not url:
        raise UniverseError(f"{universe.value} has no NSE constituent CSV")
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        resp.raise_for_status()
        df = pd.read_csv(io.BytesIO(resp.content))
    except requests.RequestException as exc:
        raise UniverseError(f"could not download {universe.value} constituents: {exc}") from exc
    except ValueError as exc:
        raise UniverseError(f"could not parse {universe.value} CSV: {exc}") from exc

    required = {"Symbol", "Company Name", "Industry", "ISIN Code"}
    if missing := required - set(df.columns):
        raise UniverseError(f"{universe.value} CSV missing columns {sorted(missing)}")
    return df


def build_universe(
    universe: Universe,
    master: InstrumentMaster | None = None,
) -> UniverseList:
    """Resolve a universe to members with instrument keys attached."""
    master = master or get_master()

    if universe is Universe.ALL_NSE:
        members = [
            Member(
                symbol=i.symbol, name=i.name, industry="",
                isin=i.isin, instrument_key=i.instrument_key,
            )
            for i in master.by_symbol.values()
        ]
        return UniverseList(
            universe=universe,
            members=sorted(members, key=lambda m: m.symbol),
            built_at=now_ist(),
            verified=True,  # derived directly from the master, not a snapshot list
        )

    df = fetch_index_csv(universe)
    members: list[Member] = []
    unresolved: list[str] = []

    for rec in df.to_dict("records"):
        symbol = str(rec["Symbol"]).upper().strip()
        isin = str(rec["ISIN Code"]).upper().strip()

        # Prefer ISIN: symbols get renamed, ISINs effectively do not.
        inst = master.resolve_isin(isin) or master.resolve(symbol)
        if inst is None:
            unresolved.append(symbol)

        members.append(
            Member(
                symbol=symbol,
                name=str(rec["Company Name"]).strip(),
                industry=str(rec["Industry"]).strip(),
                isin=isin,
                instrument_key=inst.instrument_key if inst else "",
            )
        )

    if unresolved:
        # Spec 7: visible count, never a silent drop.
        log.warning(
            "%d/%d %s symbols have no Upstox instrument_key: %s",
            len(unresolved), len(members), universe.value, ", ".join(unresolved[:10]),
        )

    return UniverseList(
        universe=universe,
        members=sorted(members, key=lambda m: m.symbol),
        built_at=now_ist(),
        verified=True,  # fetched live from NSE, not a hand-maintained list
        unresolved=unresolved,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_universe(ul: UniverseList) -> Path:
    ensure_dirs()
    path = universe_path(ul.universe)
    payload = {
        "schema": UNIVERSE_SCHEMA,
        "index": ul.universe.label,
        "verified": ul.verified,
        "source": (
            "instrument master"
            if ul.universe is Universe.ALL_NSE
            else NSE_INDEX_CSV[ul.universe]
        ),
        "built_at": ul.built_at.isoformat(),
        "count": len(ul.members),
        "unresolved": ul.unresolved,
        "members": [
            {
                "symbol": m.symbol,
                "name": m.name,
                "industry": m.industry,
                "isin": m.isin,
                "instrument_key": m.instrument_key,
            }
            for m in ul.members
        ],
        # Kept for the simple `payload["symbols"]` readers.
        "symbols": ul.symbols,
    }
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return path


def load_universe(universe: Universe) -> UniverseList | None:
    """Load a saved universe. Returns None if absent or written by an older schema."""
    path = universe_path(universe)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read %s (%s)", path.name, exc)
        return None

    if int(payload.get("schema", 0)) != UNIVERSE_SCHEMA:
        log.info("%s is schema %s, expected %s; rebuild required",
                 path.name, payload.get("schema"), UNIVERSE_SCHEMA)
        return None

    members = [
        Member(
            symbol=m["symbol"],
            name=m.get("name", ""),
            industry=m.get("industry", ""),
            isin=m.get("isin", ""),
            instrument_key=m.get("instrument_key", ""),
        )
        for m in payload.get("members", [])
    ]
    return UniverseList(
        universe=universe,
        members=members,
        built_at=datetime.fromisoformat(payload["built_at"]),
        verified=bool(payload.get("verified", False)),
        unresolved=list(payload.get("unresolved", [])),
    )


def get_universe(universe: Universe, refresh: bool = False) -> UniverseList:
    """Load the saved universe, rebuilding from NSE when asked or absent."""
    if not refresh and (cached := load_universe(universe)) is not None:
        return cached
    ul = build_universe(universe)
    save_universe(ul)
    return ul
