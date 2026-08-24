"""Atomic on-disk snapshot store (the JSON cache).

Writes go to a temp file in the destination directory and are then renamed over
the target. ``os.replace`` is atomic on both NTFS and POSIX, so a crash or a
concurrent reader never observes a half-written snapshot -- which would
otherwise surface as a JSONDecodeError that looks like data corruption.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd

from infinity.config import DAILY_DIR, INTRADAY_DIR, SNAPSHOT_DIR, ensure_dirs
from infinity.data.models import Interval, SchemaMismatch, Snapshot, Source
from infinity.market_clock import last_session_close, now_ist

log = logging.getLogger(__name__)

_SAFE = str.maketrans({"/": "_", "\\": "_", ":": "_", "*": "_", "?": "_", '"': "_"})


def _safe_name(symbol: str) -> str:
    return symbol.upper().translate(_SAFE)


def bars_path(symbol: str, interval: Interval) -> Path:
    root = DAILY_DIR if interval is Interval.DAY else INTRADAY_DIR
    suffix = "" if interval is Interval.DAY else f".{interval.value}"
    return root / f"{_safe_name(symbol)}{suffix}.json"


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"), default=str)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_bars(
    symbol: str,
    df: pd.DataFrame,
    interval: Interval,
    source: Source,
    instrument_key: str | None = None,
    captured_at: datetime | None = None,
) -> Snapshot:
    ensure_dirs()
    captured = captured_at or now_ist()
    snap = Snapshot(
        symbol=symbol.upper(),
        interval=interval,
        source=source,
        captured_at=captured,
        session_close=last_session_close(captured),
        df=df,
        instrument_key=instrument_key,
    )
    _atomic_write_json(bars_path(symbol, interval), snap.to_payload())
    log.debug("snapshot written: %s %s (%d bars)", symbol, interval.value, len(df))
    return snap


def read_bars(symbol: str, interval: Interval) -> Snapshot | None:
    """Return the stored snapshot, or None if absent/unreadable/outdated schema.

    Never raises on a bad file: a corrupt snapshot should trigger a refetch, not
    take down a 500-symbol scan.
    """
    path = bars_path(symbol, interval)
    if not path.exists():
        return None
    try:
        return Snapshot.from_payload(json.loads(path.read_text(encoding="utf-8")))
    except SchemaMismatch as exc:
        log.info("Discarding %s: %s", path.name, exc)
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        log.warning("Unreadable snapshot %s (%s); will refetch", path.name, exc)
    return None


def read_if_fresh(
    symbol: str, interval: Interval, as_of: datetime | None = None
) -> Snapshot | None:
    """Read a snapshot only if it postdates the last completed session close."""
    snap = read_bars(symbol, interval)
    if snap is None:
        return None
    if not snap.is_fresh(last_session_close(as_of)):
        log.debug("snapshot stale: %s captured %s", symbol, snap.captured_at)
        return None
    return snap


def has_fresh(symbol: str, interval: Interval, as_of: datetime | None = None) -> bool:
    return read_if_fresh(symbol, interval, as_of) is not None


# --------------------------------------------------------------------------
# Scan-result snapshots -- the small, UI-facing JSON the EOD workflow commits.
# --------------------------------------------------------------------------


def scan_path(module: str, session_date: str) -> Path:
    return SNAPSHOT_DIR / session_date / f"{module}.json"


def write_scan(
    module: str,
    rows: list[dict],
    universe: str,
    meta: dict | None = None,
    captured_at: datetime | None = None,
) -> Path:
    """Persist one module's scan output for the hosted app to read."""
    ensure_dirs()
    captured = captured_at or now_ist()
    close = last_session_close(captured)
    payload = {
        "schema": 1,
        "module": module,
        "universe": universe,
        "captured_at": captured.isoformat(),
        "session_close": close.isoformat(),
        "row_count": len(rows),
        "meta": meta or {},
        "rows": rows,
    }
    path = scan_path(module, close.date().isoformat())
    _atomic_write_json(path, payload)
    _atomic_write_json(
        SNAPSHOT_DIR / "latest.json",
        {"session_date": close.date().isoformat(), "updated_at": captured.isoformat()},
    )
    return path


def read_scan(module: str, session_date: str | None = None) -> dict | None:
    """Read a module's scan output; defaults to whatever ``latest.json`` points at."""
    if session_date is None:
        pointer = SNAPSHOT_DIR / "latest.json"
        if not pointer.exists():
            return None
        try:
            session_date = json.loads(pointer.read_text(encoding="utf-8"))["session_date"]
        except (OSError, json.JSONDecodeError, KeyError):
            return None

    path = scan_path(module, str(session_date))
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Unreadable scan snapshot %s (%s)", path.name, exc)
        return None
