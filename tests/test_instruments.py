"""Instrument master and universe tests -- offline, using fixture payloads."""

from __future__ import annotations

import gzip
import json

import pytest

from infinity.config import Universe
from infinity.data.instruments import (
    Instrument,
    InstrumentMaster,
    InstrumentMasterError,
    _is_equity_isin,
    download_master,
)
from infinity.market_clock import now_ist
from tests.conftest import ist

RAW_INSTRUMENTS = [
    {
        "instrument_key": "NSE_EQ|INE002A01018", "trading_symbol": "RELIANCE",
        "name": "RELIANCE INDUSTRIES LTD", "isin": "INE002A01018",
        "segment": "NSE_EQ", "instrument_type": "EQ", "lot_size": 1, "tick_size": 5.0,
    },
    {
        "instrument_key": "NSE_EQ|INE101A01026", "trading_symbol": "M&M",
        "name": "MAHINDRA & MAHINDRA LTD", "isin": "INE101A01026",
        "segment": "NSE_EQ", "instrument_type": "EQ", "lot_size": 1, "tick_size": 5.0,
    },
    {  # ETF unit -- INF ISIN, must be filtered out
        "instrument_key": "NSE_EQ|INF109K1A476", "trading_symbol": "SMALLIETF",
        "name": "ICICIPRAMC - SMALLIETF", "isin": "INF109K1A476",
        "segment": "NSE_EQ", "instrument_type": "EQ", "lot_size": 1, "tick_size": 1.0,
    },
    {  # wrong segment
        "instrument_key": "NSE_FO|X", "trading_symbol": "RELIANCE24000CE",
        "name": "RELIANCE CE", "isin": "", "segment": "NSE_FO",
        "instrument_type": "CE", "lot_size": 250, "tick_size": 5.0,
    },
    {  # index, not an equity
        "instrument_key": "NSE_INDEX|Nifty 50", "trading_symbol": "NIFTY 50",
        "name": "Nifty 50", "isin": "", "segment": "NSE_INDEX",
        "instrument_type": "INDEX", "lot_size": 1, "tick_size": 1.0,
    },
]

NIFTY_CSV = (
    "Company Name,Industry,Symbol,Series,ISIN Code\n"
    "Reliance Industries Ltd.,Oil Gas & Consumable Fuels,RELIANCE,EQ,INE002A01018\n"
    "Mahindra & Mahindra Ltd.,Automobile and Auto Components,M&M,EQ,INE101A01026\n"
    "Ghost Corp Ltd.,Unknown,GHOSTCO,EQ,INE999Z01011\n"
)


class FakeResponse:
    def __init__(self, content: bytes, status: int = 200):
        self.content = content
        self.status_code = status
        self.headers = {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def fake_master_download(monkeypatch):
    payload = gzip.compress(json.dumps(RAW_INSTRUMENTS).encode())
    monkeypatch.setattr(
        "infinity.data.instruments.requests.get",
        lambda *a, **k: FakeResponse(payload),
    )


class TestIsinFilter:
    def test_ine_is_equity_inf_is_a_fund(self) -> None:
        assert _is_equity_isin("INE002A01018")
        assert not _is_equity_isin("INF109K1A476")
        assert not _is_equity_isin("")


class TestDownloadMaster:
    def test_keeps_only_nse_eq_equities(self, fake_master_download) -> None:
        df = download_master()
        assert list(df["symbol"]) == ["M&M", "RELIANCE"]  # sorted; ETF/FO/index excluded

    def test_builds_the_instrument_key(self, fake_master_download) -> None:
        df = download_master()
        row = df[df["symbol"] == "RELIANCE"].iloc[0]
        assert row["instrument_key"] == "NSE_EQ|INE002A01018"
        assert row["isin"] == "INE002A01018"

    def test_network_failure_raises_a_typed_error(self, monkeypatch) -> None:
        import requests

        def boom(*a, **k):
            raise requests.RequestException("no route to host")

        monkeypatch.setattr("infinity.data.instruments.requests.get", boom)
        with pytest.raises(InstrumentMasterError, match="could not download"):
            download_master()

    def test_zero_equities_raises(self, monkeypatch) -> None:
        payload = gzip.compress(json.dumps([RAW_INSTRUMENTS[3]]).encode())
        monkeypatch.setattr(
            "infinity.data.instruments.requests.get", lambda *a, **k: FakeResponse(payload)
        )
        with pytest.raises(InstrumentMasterError, match="zero NSE_EQ"):
            download_master()


class TestInstrumentMaster:
    def build(self) -> InstrumentMaster:
        insts = [
            Instrument("RELIANCE", "NSE_EQ|INE002A01018", "INE002A01018", "Reliance"),
            Instrument("M&M", "NSE_EQ|INE101A01026", "INE101A01026", "M&M"),
        ]
        return InstrumentMaster(
            by_symbol={i.symbol: i for i in insts},
            by_isin={i.isin: i for i in insts},
            fetched_at=now_ist(),
        )

    def test_resolution_is_case_insensitive(self) -> None:
        m = self.build()
        assert m.instrument_key("reliance") == "NSE_EQ|INE002A01018"
        assert m.resolve(" M&M ") is not None

    def test_unknown_symbol_resolves_to_none(self) -> None:
        assert self.build().instrument_key("NOSUCH") is None

    def test_isin_lookup(self) -> None:
        assert self.build().resolve_isin("INE101A01026").symbol == "M&M"

    def test_staleness_is_per_calendar_day(self) -> None:
        fresh = self.build()
        assert not fresh.is_stale

        stale = InstrumentMaster(
            fresh.by_symbol, fresh.by_isin, fetched_at=ist("2020-01-01 08:00")
        )
        assert stale.is_stale


class TestUniverseBuild:
    @pytest.fixture(autouse=True)
    def _tmp_universe_dir(self, monkeypatch, tmp_path):
        from infinity import config
        from infinity.data import universe as uni

        d = tmp_path / "universes"
        d.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(config, "UNIVERSE_DIR", d, raising=False)
        monkeypatch.setattr(uni, "UNIVERSE_DIR", d, raising=False)
        monkeypatch.setattr(uni, "ensure_dirs", lambda: None)
        return d

    @pytest.fixture
    def fake_csv(self, monkeypatch):
        monkeypatch.setattr(
            "infinity.data.universe.requests.get",
            lambda *a, **k: FakeResponse(NIFTY_CSV.encode()),
        )

    def master(self) -> InstrumentMaster:
        insts = [
            Instrument("RELIANCE", "NSE_EQ|INE002A01018", "INE002A01018", "Reliance"),
            Instrument("M&M", "NSE_EQ|INE101A01026", "INE101A01026", "M&M"),
        ]
        return InstrumentMaster(
            {i.symbol: i for i in insts}, {i.isin: i for i in insts}, now_ist()
        )

    def test_attaches_instrument_keys_and_industry(self, fake_csv) -> None:
        from infinity.data.universe import build_universe

        ul = build_universe(Universe.NIFTY_50, master=self.master())

        assert len(ul) == 3
        rel = next(m for m in ul.members if m.symbol == "RELIANCE")
        assert rel.instrument_key == "NSE_EQ|INE002A01018"
        assert rel.industry == "Oil Gas & Consumable Fuels"

    def test_unresolvable_symbols_are_reported_not_dropped(self, fake_csv) -> None:
        """Spec 7: a visible count, never a silent drop."""
        from infinity.data.universe import build_universe

        ul = build_universe(Universe.NIFTY_50, master=self.master())

        assert ul.unresolved == ["GHOSTCO"]
        assert "GHOSTCO" in ul.symbols, "still listed, just without a key"
        assert next(m for m in ul.members if m.symbol == "GHOSTCO").instrument_key == ""

    def test_industry_buckets(self, fake_csv) -> None:
        from infinity.data.universe import build_universe

        ul = build_universe(Universe.NIFTY_50, master=self.master())
        assert set(ul.by_industry) == {
            "Oil Gas & Consumable Fuels", "Automobile and Auto Components", "Unknown"
        }

    def test_save_then_load_round_trips(self, fake_csv) -> None:
        from infinity.data.universe import build_universe, load_universe, save_universe

        original = build_universe(Universe.NIFTY_50, master=self.master())
        save_universe(original)
        back = load_universe(Universe.NIFTY_50)

        assert back is not None
        assert back.symbols == original.symbols
        assert back.unresolved == ["GHOSTCO"]
        assert back.members[0].industry == original.members[0].industry

    def test_load_rejects_an_older_schema(self, fake_csv) -> None:
        from infinity.data.universe import (
            build_universe,
            load_universe,
            save_universe,
            universe_path,
        )

        save_universe(build_universe(Universe.NIFTY_50, master=self.master()))
        p = universe_path(Universe.NIFTY_50)
        payload = json.loads(p.read_text(encoding="utf-8"))
        payload["schema"] = 1
        p.write_text(json.dumps(payload), encoding="utf-8")

        assert load_universe(Universe.NIFTY_50) is None

    def test_load_missing_returns_none(self) -> None:
        from infinity.data.universe import load_universe

        assert load_universe(Universe.NIFTY_500) is None

    def test_all_nse_is_derived_from_the_master(self) -> None:
        from infinity.data.universe import build_universe

        ul = build_universe(Universe.ALL_NSE, master=self.master())
        assert ul.symbols == ["M&M", "RELIANCE"]
        assert all(m.instrument_key for m in ul.members)

    def test_csv_missing_a_column_raises(self, monkeypatch) -> None:
        from infinity.data.universe import UniverseError, build_universe

        monkeypatch.setattr(
            "infinity.data.universe.requests.get",
            lambda *a, **k: FakeResponse(b"Symbol,Series\nRELIANCE,EQ\n"),
        )
        with pytest.raises(UniverseError, match="missing columns"):
            build_universe(Universe.NIFTY_50, master=self.master())


class TestShippedUniverses:
    def test_nifty50_file_is_present_and_verified(self) -> None:
        """The shipped nifty50.json must be the live-fetched one, not a guess."""
        from infinity.config import ROOT

        payload = json.loads(
            (ROOT / "data" / "universes" / "nifty50.json").read_text(encoding="utf-8")
        )
        assert payload["verified"] is True
        assert payload["count"] == 50
        assert payload["unresolved"] == []
        assert all(m["instrument_key"].startswith("NSE_EQ|") for m in payload["members"])
        assert all(m["industry"] for m in payload["members"])

    def test_nifty500_file_is_present_and_verified(self) -> None:
        from infinity.config import ROOT

        payload = json.loads(
            (ROOT / "data" / "universes" / "nifty500.json").read_text(encoding="utf-8")
        )
        assert payload["verified"] is True
        assert payload["count"] == 500
        assert payload["unresolved"] == []
