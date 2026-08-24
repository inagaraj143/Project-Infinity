from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from infinity.market_clock import IST  # noqa: E402


def ist(text: str) -> datetime:
    """Build an IST datetime from 'YYYY-MM-DD HH:MM'."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=IST)


@pytest.fixture
def calendar_2026(tmp_path: Path) -> Path:
    """A small, *verified* calendar so tests never depend on the shipped file."""
    payload = {
        "verified": True,
        "source": "test fixture",
        "years": {
            "2026": [
                {"date": "2026-01-26", "name": "Republic Day", "confidence": "fixed"},
                {"date": "2026-08-24", "name": "Test Monday Holiday", "confidence": "fixed"},
                {"date": "2026-12-25", "name": "Christmas", "confidence": "fixed"},
            ]
        },
    }
    p = tmp_path / "nse_holidays.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _use_test_calendar(monkeypatch, calendar_2026: Path):
    """Point market_clock at the fixture calendar and reset its lru_cache."""
    from infinity import market_clock

    market_clock.load_calendar.cache_clear()
    monkeypatch.setattr(market_clock, "_HOLIDAY_FILE", calendar_2026)
    yield
    market_clock.load_calendar.cache_clear()
