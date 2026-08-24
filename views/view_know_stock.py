"""3.2 Know the Stock -- deep fundamentals and price inspector."""

from __future__ import annotations

import streamlit as st

from infinity.config import load_upstox_credentials, redact
from infinity.data.models import Interval
from infinity.fundamentals import fetch_fundamentals, peer_comparison, true_lifetime_ath
from infinity.ui.charts import price_chart
from views.common import cached_universe, chart_controls, get_resolver, shell, universe_picker


@st.cache_data(ttl=3600, show_spinner="Fetching fundamentals...")
def _fundamentals(symbol: str):
    return fetch_fundamentals(symbol)


def render() -> None:
    shell("3.2  Know the Stock")
    st.subheader("3.2 Know the Stock")
    st.caption("Deep fundamentals, true lifetime ATH, and sector peer comparison.")

    universe = universe_picker()
    ul = cached_universe(universe.value)
    if not ul.symbols:
        st.warning("No universe loaded. Run `py scripts/refresh_reference.py`.")
        return

    symbol = st.selectbox("Symbol", ul.symbols, key="know_symbol")
    member = next((m for m in ul.members if m.symbol == symbol), None)
    st.divider()

    res = get_resolver().resolve(symbol, Interval.DAY, 365 * 25)
    if not res.ok or res.df.empty:
        st.error(f"No price data for {symbol}: {res.error}")
        return
    bars = res.df

    fun = _fundamentals(symbol)

    st.markdown(f"### {fun.name or member.name if member else symbol}")
    st.caption(
        f"{member.industry if member else fun.sector} · {member.instrument_key if member else ''}"
    )
    if res.is_fallback:
        st.caption("⚠ Fallback Source — served by the yfinance feed, not Upstox.")

    # -- price metrics -----------------------------------------------------
    ath, ath_date = true_lifetime_ath(bars)
    close = float(bars["close"].iloc[-1])

    c = st.columns(6)
    c[0].metric("LTP", f"{close:,.2f}")
    c[1].metric("Day High", f"{float(bars['high'].iloc[-1]):,.2f}")
    c[2].metric("Day Low", f"{float(bars['low'].iloc[-1]):,.2f}")
    c[3].metric("52W High", f"{float(bars['high'].tail(252).max()):,.2f}")
    c[4].metric("52W Low", f"{float(bars['low'].tail(252).min()):,.2f}")
    if ath:
        c[5].metric("Lifetime ATH", f"{ath:,.2f}", f"{(close - ath) / ath * 100:+.1f}%")

    if ath:
        st.caption(
            f"True lifetime ATH {ath:,.2f} on {ath_date}, from "
            f"{len(bars):,} bars back to {bars['ts'].iloc[0]:%d %b %Y}. "
            "Prices are unadjusted (spec 2.4), so this matches a trading terminal."
        )

    st.divider()

    tab_f, tab_p, tab_c, tab_a = st.tabs(
        ["📊 Fundamentals", "🏭 Peer Comparison", "📈 Chart", "📡 API"]
    )

    with tab_f:
        st.caption(f"{fun.available_count} of 10 spec indicators available.")
        rows = [
            {
                "Indicator": m.label,
                "Value": m.display(),
                "Source": m.provenance.value,
            }
            for m in fun.metrics.values()
        ]
        st.dataframe(rows, hide_index=True, width="stretch")

        if caveats := fun.caveats():
            with st.expander(f"Data caveats ({len(caveats)})"):
                for cav in caveats:
                    st.markdown(f"- {cav}")

    with tab_p:
        peers = [
            m.symbol for m in ul.members
            if member and m.industry == member.industry and m.symbol != symbol
        ][:6]
        if not peers:
            st.info("No sector peers in this universe.")
        else:
            st.caption(f"Compared against {len(peers)} peers in {member.industry}.")
            peer_data = [_fundamentals(p) for p in peers]
            st.dataframe(
                peer_comparison(fun, peer_data), hide_index=True, width="stretch"
            )

    with tab_c:
        style, emas = chart_controls("know_stock")
        st.plotly_chart(
            price_chart(bars.tail(500), symbol, style=style, show_emas=emas),
            width="stretch",
        )

    with tab_a:
        # Spec 3.2 / 6.1: secrets redacted here in every deployment mode.
        creds = load_upstox_credentials()
        st.caption("Credentials are redacted in this tab in every deployment mode.")
        st.json(
            {
                "request": {
                    "url": (
                        "https://api.upstox.com/v2/historical-candle/"
                        f"{member.instrument_key if member else '?'}/day/"
                        "{to_date}/{from_date}"
                    ),
                    "headers": {
                        "Authorization": f"Bearer {redact(creds.access_token)}",
                        "Accept": "application/json",
                    },
                },
                "resolved": {
                    "source": res.source.value,
                    "outcome": res.outcome.value,
                    "captured_at": res.captured_at.isoformat(),
                    "bars": len(bars),
                },
                "credentials": creds.redacted(),
            }
        )
