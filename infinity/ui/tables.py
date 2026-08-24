"""Scanner table rendering (spec 4.4).

Native Streamlit throughout -- no streamlit-aggrid. That buys ~85% of 4.4
(sticky header, pinned first column, sortable columns, inline score bars,
row-click drill-in) with none of the deploy fragility. The remaining 15%
(HTML chips in cells, an in-header filter row) is approximated with a glyph
prefix and a control strip above the table.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from infinity.scanners.base import Signal

_SIGNAL_GLYPH = {
    "Bullish": "▲ Bullish",
    "Bearish": "▼ Bearish",
    "Watchlist": "◆ Watchlist",
    "Neutral": "● Neutral",
    "Reject": "○ Reject",
}


def decorate_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Prefix signal text with a glyph (spec 4.8: never colour alone)."""
    out = df.copy()
    for col in out.columns:
        if col in ("Signal", "Overall Signal", "Direction", "Trend Direction", "Status"):
            out[col] = out[col].map(lambda v: _SIGNAL_GLYPH.get(str(v), str(v)))
    return out


def column_config(df: pd.DataFrame) -> dict[str, Any]:
    """Score columns become inline bars; the symbol column pins."""
    cfg: dict[str, Any] = {}
    for col in df.columns:
        low = col.lower()
        if col == "Symbol":
            cfg[col] = st.column_config.TextColumn("Symbol", pinned=True, width="small")
        elif "score" in low or "confidence" in low:
            if pd.api.types.is_numeric_dtype(df[col]):
                cfg[col] = st.column_config.ProgressColumn(
                    col, min_value=0, max_value=100, format="%.0f"
                )
        elif pd.api.types.is_numeric_dtype(df[col]):
            cfg[col] = st.column_config.NumberColumn(col, format="%.2f")
    return cfg


def filter_strip(df: pd.DataFrame, key: str) -> pd.DataFrame:
    """Score threshold + signal filter (spec 4.4 filter row, as a strip)."""
    if df.empty:
        return df

    c1, c2, c3 = st.columns([2, 2, 1])
    out = df

    score_cols = [c for c in df.columns if "score" in c.lower() or "confidence" in c.lower()]
    if score_cols:
        col = score_cols[0]
        with c1:
            threshold = st.slider(
                f"Min {col}", 0, 100, 0, step=5, key=f"{key}_score"
            )
        out = out[out[col] >= threshold]

    signal_cols = [c for c in df.columns if c in ("Signal", "Overall Signal")]
    if signal_cols:
        col = signal_cols[0]
        options = sorted(df[col].astype(str).unique())
        with c2:
            chosen = st.multiselect(col, options, default=options, key=f"{key}_sig")
        if chosen:
            out = out[out[col].astype(str).isin(chosen)]

    with c3:
        st.metric("Matches", len(out))
    return out


def scanner_table(
    records: list[dict],
    key: str,
    height: int = 460,
) -> str | None:
    """Render a scan table. Returns the symbol of the selected row, if any."""
    if not records:
        st.info("No symbols matched this scan.", icon="🔍")
        return None

    df = pd.DataFrame(records)
    df = filter_strip(df, key)
    if df.empty:
        st.warning("No rows pass the current filters.", icon="🔍")
        return None

    shown = decorate_signals(df)
    event = st.dataframe(
        shown,
        width="stretch",
        height=height,
        hide_index=True,
        column_config=column_config(df),
        on_select="rerun",
        selection_mode="single-row",
        key=f"{key}_table",
    )

    rows = event.selection.rows if event and event.selection else []
    if rows:
        return str(df.iloc[rows[0]]["Symbol"])
    return None


def signal_summary(records: list[dict]) -> None:
    """Counts per signal, above the table."""
    if not records:
        return
    counts: dict[str, int] = {}
    for r in records:
        val = str(r.get("Signal") or r.get("Overall Signal") or "")
        if val:
            counts[val] = counts.get(val, 0) + 1
    if not counts:
        return

    cols = st.columns(len(counts) + 1)
    cols[0].metric("Total", len(records))
    for col, (name, n) in zip(cols[1:], sorted(counts.items()), strict=False):
        col.metric(_SIGNAL_GLYPH.get(name, name), n)


def skipped_panel(skipped: list[dict], label: str = "Skipped symbols") -> None:
    """Spec 7: skipped symbols visible with their reason, never silently dropped."""
    if not skipped:
        return
    with st.expander(f"{label} ({len(skipped)})"):
        st.dataframe(skipped, hide_index=True, width="stretch")


def signal_from_text(value: str) -> Signal:
    try:
        return Signal(value)
    except ValueError:
        return Signal.NEUTRAL
