"""Project Infinity -- Streamlit entry point.

All eight spec modules (3.1-3.8) reachable from the sidebar.

    streamlit run app.py
"""

from __future__ import annotations

import logging

import streamlit as st

from infinity import __version__
from infinity.config import AppSettings, Universe, load_upstox_credentials
from infinity.data.snapshot_store import read_scan
from infinity.market_clock import KNOWN_LIMITATIONS, market_status
from infinity.scanners import REGISTRY
from infinity.ui.theme import inject_css, status_bar
from views import view_backtester, view_know_stock
from views.common import scanner_page

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)

st.set_page_config(
    page_title="Project Infinity",
    page_icon="♾️",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------------
# Scanner pages (3.1, 3.3-3.7)
# ---------------------------------------------------------------------------

DESCRIPTIONS = {
    "golden_zone": (
        "Stocks pulling back into the 50.0%-61.8% Fibonacci retracement, scored on "
        "trend (30), zone proximity (30), volume (15), candle (15) and sector (10)."
    ),
    "trendlines": (
        "13-step filter for active trendlines: swing detection, validation, price "
        "position, dual-timeframe confirmation, RSI/MACD/EMA/volume, then scoring."
    ),
    "triangle": (
        "Ascending (flat ceiling over rising support) and symmetrical (converging) "
        "triangles, with a bullish momentum qualifier."
    ),
    "resistance_breakout": (
        "Strict 4-condition filter: close above the 50-day ceiling, EMA 8 > EMA 13, "
        "volume >= 1.5x prior day, and a green candle. All four must hold."
    ),
    "candle_50": (
        "Price against the 50% midpoint of the most recent decisive candle; dojis "
        "and spinning tops are skipped when choosing the reference."
    ),
    "displacement": (
        "Candles with range >= 2x ATR, volume >= 3x SMA and a close in the top or "
        "bottom quarter, plus the imbalance zone they leave behind."
    ),
}


def _make_page(name: str):
    def page() -> None:
        scanner_page(name, DESCRIPTIONS[name])

    page.__name__ = f"page_{name}"
    return page


# ---------------------------------------------------------------------------
# Overview pages
# ---------------------------------------------------------------------------


def page_overview() -> None:
    inject_css()
    status_bar(market_status())
    st.subheader("Modules")

    rows = []
    for _name, s in sorted(REGISTRY.items(), key=lambda kv: kv[1].section):
        rows.append(
            {
                "Section": s.section,
                "Module": s.title,
                # str, not int: this column also carries "-" for the two
                # non-scanner modules, and Arrow rejects a mixed-type column.
                "Min bars": str(s.min_daily_bars),
                "Depends on": ", ".join(getattr(s, "depends_on", ())) or "-",
            }
        )
    rows.append({"Section": "3.2", "Module": "Know the Stock", "Min bars": "-",
                 "Depends on": "-"})
    rows.append({"Section": "3.8", "Module": "Historical Backtester", "Min bars": "-",
                 "Depends on": "any scanner"})
    st.dataframe(
        sorted(rows, key=lambda r: r["Section"]), hide_index=True, width="stretch"
    )

    st.caption(
        f"Project Infinity v{__version__} — pick a module from the sidebar. "
        "Scans are cached for 15 minutes per universe."
    )

    with st.expander("Applied spec corrections"):
        st.markdown(
            """
- **C1** — 3.3 Step 12 sums to 110 with 3+ touches but 100 with 2, while Step 13
  grades 0-100. Scores are normalised by the maximum actually attainable, so the
  bands mean one thing regardless of touch count. `Raw Score` shows the arithmetic.
- **C2** — Step 1's "60 15-minute bars" is ~2.4 sessions, too short for Step 7.
  Raised to 200; below that Step 7 is *unavailable*, and its points leave the
  denominator rather than counting as a failure.
- **C3** — 3.5's `(O+H+C)/3` omits the Low and `ceil()` quantises to whole rupees.
  Both are configurable; the spec-literal behaviour is the default.
- **C4** — 3.7's two-candle imbalance emits a zone even with no gap. The standard
  three-candle fair-value gap is used instead.
- **C5** — 3.1's "where available" dependency is resolved by an explicit DAG; each
  row records whether a component came from 3.3/3.6 or a fallback.
- **B2** — the backtester detects corporate actions and excludes or adjusts them,
  because unadjusted prices turn a 1:10 split into a fabricated -90% trade.
- **C6** — survivorship bias is stated on every backtest run.
            """
        )


def page_data_health() -> None:
    inject_css()
    status = market_status()
    status_bar(status, "Data layer diagnostics")
    st.subheader("Session & cache state")

    creds = load_upstox_credentials()
    settings = AppSettings()

    c1, c2, c3 = st.columns(3)
    c1.metric("Session", status.state.value)
    c2.metric("Last settled close", f"{status.last_close:%d %b %H:%M}")
    c3.metric("Next open", f"{status.next_open:%d %b %H:%M}")

    st.caption(
        "Active policy: "
        + (
            "memory cache (TTL) → provider → write snapshot"
            if status.is_live
            else "fresh snapshot → serve with zero network calls"
        )
    )

    st.divider()
    st.subheader("Credentials")
    st.caption("Spec 3.2 / 6.1: secrets are redacted here in every deployment mode.")
    st.json(creds.redacted())
    if not creds.is_authenticated:
        st.info(
            "No Upstox access token. The resolver runs on yfinance — the intended "
            "state for the unattended EOD job, since Upstox tokens expire daily "
            "and need a browser to mint.",
            icon="ℹ️",
        )
    if settings.is_ci:
        st.caption("Running under CI: Upstox disabled, yfinance only.")

    st.divider()
    st.subheader("Reference data")

    from infinity.data.instruments import load_cached_master
    from infinity.data.universe import load_universe

    master = load_cached_master()
    if master is None:
        st.warning("No instrument master. Run `py scripts/refresh_reference.py`.", icon="⚠️")
    else:
        m1, m2 = st.columns(2)
        m1.metric("NSE equities mapped", f"{len(master):,}")
        m2.metric("Fetched", f"{master.fetched_at:%d %b %H:%M}")

    st.dataframe(
        [
            {
                "Universe": u.label,
                "Members": len(ul) if (ul := load_universe(u)) else 0,
                "Industries": len(ul.by_industry) if ul else 0,
                "Unresolved": len(ul.unresolved) if ul else 0,
                "Built": f"{ul.built_at:%d %b %H:%M}" if ul else "—",
            }
            for u in Universe
            if u is not Universe.ALL_NSE
        ],
        hide_index=True,
        width="stretch",
    )

    st.divider()
    st.subheader("Latest EOD snapshot")
    meta = read_scan("universe_meta")
    if meta is None:
        st.info(
            "No snapshot yet. Run `py scripts/build_snapshots.py --universe nifty500`.",
            icon="ℹ️",
        )
    else:
        info = meta.get("meta", {})
        s = st.columns(4)
        s[0].metric("Symbols", f"{info.get('succeeded', 0)}/{info.get('requested', 0)}")
        s[1].metric("Skipped", len(info.get("skipped", [])))
        s[2].metric("Network calls", info.get("network_calls", 0))
        s[3].metric("Elapsed", f"{info.get('elapsed_sec', 0):.0f}s")
        if skipped := info.get("skipped"):
            with st.expander(f"Skipped symbols ({len(skipped)})"):
                st.dataframe(skipped, hide_index=True, width="stretch")

    st.divider()
    with st.expander("Known clock limitations"):
        for line in KNOWN_LIMITATIONS:
            st.markdown(f"- {line}")


# ---------------------------------------------------------------------------
# Navigation (spec 4.3)
# ---------------------------------------------------------------------------

# url_path is set explicitly on every page. Streamlit otherwise infers it from
# the callable's name, and both view modules expose a function called `render`,
# which collides.
nav = st.navigation(
    {
        "Overview": [
            st.Page(
                page_overview, title="Modules", icon="🧭",
                url_path="modules", default=True,
            ),
            st.Page(
                page_data_health, title="Data health", icon="🩺",
                url_path="data-health",
            ),
        ],
        "Scanners": [
            st.Page(
                _make_page("golden_zone"), title="Top Ranked", icon="📊",
                url_path="top-ranked",
            ),
            st.Page(
                view_know_stock.render, title="Know the Stock", icon="🔍",
                url_path="know-the-stock",
            ),
            st.Page(
                _make_page("trendlines"), title="Trendlines", icon="📉",
                url_path="trendlines",
            ),
            st.Page(
                _make_page("triangle"), title="Triangle", icon="📐",
                url_path="triangle",
            ),
            st.Page(
                _make_page("resistance_breakout"), title="Breakout", icon="🚀",
                url_path="breakout",
            ),
            st.Page(
                _make_page("candle_50"), title="50% Candle", icon="🕯️",
                url_path="candle-50",
            ),
            st.Page(
                _make_page("displacement"), title="Displacement", icon="💥",
                url_path="displacement",
            ),
        ],
        "Analysis": [
            st.Page(
                view_backtester.render, title="Backtester", icon="🧪",
                url_path="backtester",
            ),
        ],
    }
)
nav.run()
