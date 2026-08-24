"""Central configuration and path resolution.

Secrets resolve in this order, first hit wins:
  1. ``st.secrets``      -- Streamlit Community Cloud
  2. environment / .env  -- GitHub Actions, local CLI
  3. ``Key.txt``         -- local desktop convenience (spec 6.1)

All three are gitignored. ``Key.txt`` is supported because the spec asks for
it, but env vars are preferred everywhere else.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = ROOT / "data"
OHLC_DIR = DATA_DIR / "ohlc"
DAILY_DIR = OHLC_DIR / "daily"
INTRADAY_DIR = OHLC_DIR / "intraday"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
INSTRUMENT_DIR = DATA_DIR / "instruments"
UNIVERSE_DIR = DATA_DIR / "universes"

KEY_FILE = ROOT / "Key.txt"

# In-memory TTLs (spec 2.3). Only consulted while the market is LIVE; when it is
# CLOSED the resolver reads the on-disk snapshot and never starts a TTL clock.
DAILY_CACHE_TTL_SEC = 15 * 60
INTRADAY_CACHE_TTL_SEC = 5 * 60

# Snapshot payload format version. Bump on any breaking change to the on-disk
# layout so stale files are re-fetched instead of mis-parsed.
SNAPSHOT_SCHEMA = 1


class Universe(StrEnum):
    NIFTY_50 = "nifty50"
    NIFTY_500 = "nifty500"
    ALL_NSE = "all_nse"

    @property
    def label(self) -> str:
        return {"nifty50": "Nifty 50", "nifty500": "Nifty 500", "all_nse": "All NSE Equities"}[
            self.value
        ]

    @property
    def hosted_safe(self) -> bool:
        """ALL_NSE is local-only: ~360 MB of JSON will not live in a git repo."""
        return self is not Universe.ALL_NSE


def ensure_dirs() -> None:
    for d in (DAILY_DIR, INTRADAY_DIR, SNAPSHOT_DIR, INSTRUMENT_DIR, UNIVERSE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _from_streamlit_secrets(key: str) -> str | None:
    try:
        import streamlit as st
    except ModuleNotFoundError:
        return None
    try:
        return st.secrets[key]  # type: ignore[no-any-return]
    except Exception:
        # No secrets.toml, or key absent. Both are normal outside cloud.
        return None


def _from_key_file(key: str) -> str | None:
    """Parse ``KEY=value`` lines out of Key.txt (spec 6.1)."""
    if not KEY_FILE.exists():
        return None
    try:
        for line in KEY_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip().upper() == key.upper():
                return v.strip().strip("\"'")
    except OSError as exc:
        log.warning("Could not read %s: %s", KEY_FILE.name, exc)
    return None


def get_secret(key: str, default: str | None = None) -> str | None:
    for source in (_from_streamlit_secrets, lambda k: os.environ.get(k), _from_key_file):
        val = source(key)
        if val:
            return val
    return default


def redact(value: str | None, keep: int = 0) -> str:
    """Mask a secret for display (spec 3.2 / 6.1 token redaction)."""
    if not value:
        return "<not set>"
    tail = value[-keep:] if keep and len(value) > keep else ""
    return "•" * 8 + (f"...{tail}" if tail else "")


@dataclass(frozen=True)
class UpstoxCredentials:
    api_key: str | None = None
    api_secret: str | None = None
    access_token: str | None = None
    redirect_uri: str = "http://localhost:8501"

    @property
    def is_authenticated(self) -> bool:
        return bool(self.access_token)

    def redacted(self) -> dict[str, str]:
        return {
            "api_key": redact(self.api_key, keep=4),
            "api_secret": redact(self.api_secret),
            "access_token": redact(self.access_token),
            "redirect_uri": self.redirect_uri,
        }


def load_upstox_credentials() -> UpstoxCredentials:
    return UpstoxCredentials(
        api_key=get_secret("UPSTOX_API_KEY"),
        api_secret=get_secret("UPSTOX_API_SECRET"),
        access_token=get_secret("UPSTOX_ACCESS_TOKEN"),
        redirect_uri=get_secret("UPSTOX_REDIRECT_URI") or "http://localhost:8501",
    )


@dataclass
class AppSettings:
    """Runtime switches. ``CI=true`` is set by GitHub Actions."""

    is_ci: bool = field(default_factory=lambda: os.environ.get("CI", "").lower() == "true")
    default_universe: Universe = Universe.NIFTY_50
    # Upstox candle endpoints need no authentication (verified 2026-08-24, see
    # ADR 0002), so the provider is enabled everywhere including CI. Set
    # INFINITY_DISABLE_UPSTOX=1 to force the yfinance path.
    allow_upstox: bool = field(
        default_factory=lambda: os.environ.get("INFINITY_DISABLE_UPSTOX", "") not in
        ("1", "true", "True")
    )
    request_timeout_sec: float = 15.0
    max_retries: int = 3
