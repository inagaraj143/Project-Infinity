"""Shared view plumbing: data loading, caching, and the scanner page template."""

from __future__ import annotations

import streamlit as st

from infinity.config import Universe
from infinity.data.models import Interval
from infinity.data.resolver import Resolver, build_default_resolver
from infinity.data.universe import UniverseList, get_universe
from infinity.market_clock import market_status
from infinity.scan_runner import ScanOutcome, load_bars, load_intraday, run_scanners
from infinity.scanners import REGISTRY
from infinity.ui.charts import price_chart
from infinity.ui.tables import scanner_table, signal_summary, skipped_panel
from infinity.ui.theme import inject_css, status_bar


def shell(subtitle: str = "") -> None:
    inject_css()
    status_bar(market_status(), subtitle)


@st.cache_resource(show_spinner=False)
def get_resolver() -> Resolver:
    return build_default_resolver(allow_upstox=True)


@st.cache_data(ttl=900, show_spinner=False)
def cached_universe(universe_value: str) -> UniverseList:
    return get_universe(Universe(universe_value))


@st.cache_data(ttl=900, show_spinner="Loading bars...")
def cached_bars(universe_value: str, limit: int = 0) -> dict:
    ul = cached_universe(universe_value)
    symbols = ul.symbols[:limit] if limit else ul.symbols
    return load_bars(build_default_resolver(), symbols)


@st.cache_data(ttl=300, show_spinner="Loading 15-minute bars...")
def cached_intraday(universe_value: str, limit: int = 0) -> dict:
    """Spec 3.3 Step 7. Upstox serves these unauthenticated (ADR 0002)."""
    ul = cached_universe(universe_value)
    symbols = ul.symbols[:limit] if limit else ul.symbols
    return load_intraday(build_default_resolver(), symbols)


@st.cache_data(ttl=900, show_spinner="Running scan...")
def cached_scan(
    universe_value: str,
    scanner_names: tuple[str, ...],
    limit: int = 0,
    with_intraday: bool = False,
) -> dict:
    ul = cached_universe(universe_value)
    bars = cached_bars(universe_value, limit)
    intraday = cached_intraday(universe_value, limit) if with_intraday else None
    return run_scanners(list(scanner_names), ul, bars, intraday)


def universe_picker() -> Universe:
    """Spec 4.3: pinned at the top, persisted across modules."""
    options = [u for u in Universe]
    current = st.session_state.get("universe", Universe.NIFTY_50)
    return st.radio(
        "Universe",
        options,
        index=options.index(current),
        format_func=lambda u: u.label + ("" if u.hosted_safe else "  (local only)"),
        horizontal=True,
        key="universe",
    )


def chart_controls(key: str) -> tuple[str, tuple[int, ...]]:
    """Spec 4.5 control strip: style toggle plus opt-in EMA overlays."""
    c1, c2 = st.columns([1, 3])
    with c1:
        style = st.radio(
            "Chart", ("Candlestick", "Line"), horizontal=True, key=f"{key}_style"
        )
    with c2:
        emas = st.multiselect(
            "EMA overlays", (20, 50, 200), default=[], key=f"{key}_emas"
        )
    return style, tuple(emas)


def scanner_page(scanner_name: str, description: str, bars_limit: int = 0) -> None:
    """Standard page: universe picker, summary, table, drill-in chart."""
    scanner = REGISTRY[scanner_name]
    shell(f"{scanner.section}  {scanner.title}")

    st.subheader(f"{scanner.section} {scanner.title}")
    st.caption(description)

    universe = universe_picker()
    c1, c2 = st.columns([1, 1])
    with c1:
        show_all = st.toggle(
            "Show non-qualifying rows", value=False, key=f"{scanner_name}_all"
        )
    uses_intraday = scanner_name in ("trendlines", "golden_zone")
    with_intraday = False
    if uses_intraday:
        with c2:
            with_intraday = st.toggle(
                "15-min confirmation (Step 7)", value=False,
                key=f"{scanner_name}_intraday",
                help="Fetches 15-minute bars per symbol. Slower, but Step 7 is "
                     "otherwise scored as unavailable.",
            )
    st.divider()

    deps = tuple(getattr(scanner, "depends_on", ()))
    names = tuple(sorted({scanner_name, *deps}))

    try:
        outcomes = cached_scan(universe.value, names, bars_limit, with_intraday)
    except Exception as exc:
        st.error(f"Scan failed: {exc}")
        return

    outcome: ScanOutcome = outcomes[scanner_name]
    records = outcome.to_records(only_qualifying=not show_all)

    signal_summary(records)
    selected = scanner_table(records, key=scanner_name)
    skipped_panel(outcome.skipped)

    if selected:
        st.divider()
        st.subheader(f"{selected}")
        row = next((r for r in outcome.rows if r.symbol == selected), None)
        bars = cached_bars(universe.value, bars_limit).get(selected)
        if bars is None or bars.empty:
            st.warning("No bars available for this symbol.")
            return

        style, emas = chart_controls(f"{scanner_name}_{selected}")
        st.plotly_chart(
            price_chart(
                bars.tail(260), selected, style=style,
                overlays=row.overlays if row else {}, show_emas=emas,
            ),
            width="stretch",
        )


def intraday_note() -> None:
    st.caption(
        "15-minute confirmation (spec 3.3 Step 7) needs an intraday feed. Without "
        "one the step is scored as unavailable rather than failed, so those "
        "points are excluded from the denominator."
    )


__all__ = [
    "Interval",
    "cached_bars",
    "cached_scan",
    "cached_universe",
    "chart_controls",
    "get_resolver",
    "intraday_note",
    "scanner_page",
    "shell",
    "universe_picker",
]
