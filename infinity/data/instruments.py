"""NSE_EQ instrument master (spec 2.1).

Source: ``https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz``
-- a public, unauthenticated file. That matters: it means CI can refresh the
mapping without an Upstox token, same reasoning as ADR 0001.

The master maps a trading symbol to an Upstox ``instrument_key`` of the form
``NSE_EQ|<ISIN>``, and is cached to parquet so a refresh happens at most once
per calendar day.

ETF and mutual-fund units are excluded. Indian ISINs encode this directly:
equity shares are ``INE...`` while fund units are ``INF...``, so an ETF like
SMALLIETF (INF109K1A476) is filtered out while MARUTI (INE585B01010) stays.
"""

from __future__ import annotations

import gzip
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

from infinity.config import INSTRUMENT_DIR, ensure_dirs
from infinity.market_clock import now_ist, to_ist

log = logging.getLogger(__name__)

UPSTOX_NSE_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
USER_AGENT = "Mozilla/5.0 (compatible; ProjectInfinity/0.1)"

_MASTER_PARQUET = INSTRUMENT_DIR / "nse_eq.parquet"
_MASTER_META = INSTRUMENT_DIR / "nse_eq.meta.json"

_COLUMNS = ["symbol", "instrument_key", "isin", "name", "lot_size", "tick_size"]


class InstrumentMasterError(RuntimeError):
    """Master could not be downloaded or parsed."""


@dataclass(frozen=True)
class Instrument:
    symbol: str
    instrument_key: str
    isin: str
    name: str
    lot_size: int = 1
    tick_size: float = 0.05


@dataclass(frozen=True)
class InstrumentMaster:
    by_symbol: dict[str, Instrument]
    by_isin: dict[str, Instrument]
    fetched_at: datetime

    def __len__(self) -> int:
        return len(self.by_symbol)

    def resolve(self, symbol: str) -> Instrument | None:
        return self.by_symbol.get(symbol.upper().strip())

    def resolve_isin(self, isin: str) -> Instrument | None:
        return self.by_isin.get(isin.upper().strip())

    def instrument_key(self, symbol: str) -> str | None:
        inst = self.resolve(symbol)
        return inst.instrument_key if inst else None

    @property
    def is_stale(self) -> bool:
        """Refresh at most once per calendar day (spec 2.1: 'mapped daily')."""
        return self.fetched_at.date() < now_ist().date()

    @property
    def symbols(self) -> list[str]:
        return sorted(self.by_symbol)


def _is_equity_isin(isin: str) -> bool:
    """INE = equity shares; INF = mutual-fund/ETF units."""
    return isin.upper().startswith("INE")


def download_master(timeout: float = 60.0) -> pd.DataFrame:
    """Fetch and parse the Upstox NSE instrument file into a tidy frame."""
    try:
        resp = requests.get(UPSTOX_NSE_URL, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        resp.raise_for_status()
        records = json.loads(gzip.decompress(resp.content))
    except requests.RequestException as exc:
        raise InstrumentMasterError(f"could not download instrument master: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise InstrumentMasterError(f"could not parse instrument master: {exc}") from exc

    rows = [
        {
            "symbol": str(r["trading_symbol"]).upper().strip(),
            "instrument_key": r["instrument_key"],
            "isin": str(r.get("isin", "")).upper().strip(),
            "name": str(r.get("name", "")).strip(),
            "lot_size": int(r.get("lot_size") or 1),
            "tick_size": float(r.get("tick_size") or 0.05),
        }
        for r in records
        if r.get("segment") == "NSE_EQ"
        and r.get("instrument_type") == "EQ"
        and r.get("trading_symbol")
        and _is_equity_isin(str(r.get("isin", "")))
    ]
    if not rows:
        raise InstrumentMasterError("instrument master parsed to zero NSE_EQ equities")

    df = pd.DataFrame(rows, columns=_COLUMNS)
    return df.drop_duplicates(subset=["symbol"], keep="first").sort_values("symbol")


def _to_master(df: pd.DataFrame, fetched_at: datetime) -> InstrumentMaster:
    by_symbol: dict[str, Instrument] = {}
    by_isin: dict[str, Instrument] = {}
    for rec in df.to_dict("records"):
        inst = Instrument(
            symbol=rec["symbol"],
            instrument_key=rec["instrument_key"],
            isin=rec["isin"],
            name=rec["name"],
            lot_size=int(rec["lot_size"]),
            tick_size=float(rec["tick_size"]),
        )
        by_symbol[inst.symbol] = inst
        if inst.isin:
            by_isin[inst.isin] = inst
    return InstrumentMaster(by_symbol, by_isin, fetched_at)


def save_master(df: pd.DataFrame, fetched_at: datetime | None = None) -> Path:
    ensure_dirs()
    stamp = fetched_at or now_ist()
    df.to_parquet(_MASTER_PARQUET, index=False)
    _MASTER_META.write_text(
        json.dumps({"fetched_at": stamp.isoformat(), "count": len(df)}), encoding="utf-8"
    )
    return _MASTER_PARQUET


def load_cached_master() -> InstrumentMaster | None:
    """Load the parquet cache, or None if absent/unreadable."""
    if not (_MASTER_PARQUET.exists() and _MASTER_META.exists()):
        return None
    try:
        meta = json.loads(_MASTER_META.read_text(encoding="utf-8"))
        df = pd.read_parquet(_MASTER_PARQUET)
        return _to_master(df, to_ist(datetime.fromisoformat(meta["fetched_at"])))
    except Exception as exc:  # parquet/arrow raise a wide variety of errors
        log.warning("could not read cached instrument master (%s); will refetch", exc)
        return None


def get_master(force_refresh: bool = False, allow_stale: bool = True) -> InstrumentMaster:
    """Return the master, refreshing from Upstox at most once per day.

    If the network fails but a cached copy exists, the stale copy is served with
    a warning -- a day-old ISIN mapping is far better than failing an entire
    scan, since NSE ISINs almost never change.
    """
    cached = None if force_refresh else load_cached_master()
    if cached is not None and not cached.is_stale:
        return cached

    try:
        df = download_master()
    except InstrumentMasterError as exc:
        if cached is not None and allow_stale:
            log.warning("instrument refresh failed (%s); using cache from %s",
                        exc, cached.fetched_at)
            return cached
        raise

    stamp = now_ist()
    save_master(df, stamp)
    log.info("instrument master refreshed: %d NSE equities", len(df))
    return _to_master(df, stamp)
