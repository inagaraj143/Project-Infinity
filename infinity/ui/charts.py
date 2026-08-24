"""Plotly charting (spec 4.5).

Dual subplot -- price ~75% / volume ~25% -- on a shared x-axis with a unified
crosshair. Overlays (trendlines, zones, EMAs) are opt-in via the control strip
so a chart does not become unreadable when several modules apply to the same
symbol.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from infinity.indicators import ema
from infinity.ui.theme import (
    ACCENT_PRIMARY,
    BEARISH,
    BG_CARD,
    BULLISH,
    TEXT_MUTED,
    TEXT_PRIMARY,
    WARNING,
)

EMA_COLOURS = {20: "#42a5f5", 50: "#ab47bc", 200: "#ffa726"}


def price_chart(
    df: pd.DataFrame,
    symbol: str,
    style: str = "Candlestick",
    overlays: dict[str, Any] | None = None,
    show_emas: tuple[int, ...] = (),
    height: int = 620,
) -> go.Figure:
    overlays = overlays or {}

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[0.75, 0.25], vertical_spacing=0.03,
        subplot_titles=(None, None),
    )

    x = df["ts"]
    if style == "Line":
        fig.add_trace(
            go.Scatter(
                x=x, y=df["close"], mode="lines", name="Close",
                line={"color": ACCENT_PRIMARY, "width": 1.6},
            ),
            row=1, col=1,
        )
    else:
        fig.add_trace(
            go.Candlestick(
                x=x, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
                name=symbol,
                increasing_line_color=BULLISH, increasing_fillcolor=BULLISH,
                decreasing_line_color=BEARISH, decreasing_fillcolor=BEARISH,
            ),
            row=1, col=1,
        )

    for period in show_emas:
        series = ema(df["close"], period)
        fig.add_trace(
            go.Scatter(
                x=x, y=series, mode="lines", name=f"EMA {period}",
                line={"color": EMA_COLOURS.get(period, TEXT_MUTED), "width": 1.1},
            ),
            row=1, col=1,
        )

    _add_overlays(fig, df, overlays)

    colours = [
        BULLISH if c >= o else BEARISH
        for c, o in zip(df["close"], df["open"], strict=False)
    ]
    fig.add_trace(
        go.Bar(x=x, y=df["volume"], name="Volume", marker_color=colours, opacity=0.55),
        row=2, col=1,
    )

    fig.update_layout(
        height=height,
        margin={"l": 10, "r": 10, "t": 30, "b": 10},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor=BG_CARD,
        font={"color": TEXT_PRIMARY, "size": 11},
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        showlegend=True,
        legend={"orientation": "h", "y": 1.02, "x": 0},
        dragmode="pan",
    )
    grid = "rgba(255,255,255,.06)"
    fig.update_xaxes(showgrid=True, gridcolor=grid, showspikes=True,
                     spikemode="across", spikethickness=1, spikecolor=TEXT_MUTED)
    fig.update_yaxes(showgrid=True, gridcolor=grid)
    return fig


def _add_overlays(fig: go.Figure, df: pd.DataFrame, overlays: dict[str, Any]) -> None:
    x = df["ts"]
    n = len(df)

    if line := overlays.get("trendline"):
        slope, intercept = line
        start = int(overlays.get("from", 0))
        xs = list(range(start, n))
        fig.add_trace(
            go.Scatter(
                x=x.iloc[start:], y=[slope * i + intercept for i in xs],
                mode="lines", name="Trendline",
                line={"color": WARNING, "width": 2, "dash": "dot"},
            ),
            row=1, col=1,
        )

    window = int(overlays.get("window", 0) or 0)
    for key, label, colour in (
        ("resistance_line", "Resistance", BEARISH),
        ("support_line", "Support", BULLISH),
    ):
        if fit := overlays.get(key):
            slope, intercept = fit
            start = max(0, n - window) if window else 0
            xs = list(range(n - start))
            fig.add_trace(
                go.Scatter(
                    x=x.iloc[start:], y=[slope * i + intercept for i in xs],
                    mode="lines", name=label,
                    line={"color": colour, "width": 1.6, "dash": "dash"},
                ),
                row=1, col=1,
            )

    for key, label, colour in (
        ("imbalance_zone", "Imbalance Zone", "rgba(255,145,0,.18)"),
        ("golden_zone", "Golden Zone", "rgba(38,166,154,.18)"),
    ):
        zone = overlays.get(key)
        if zone and len(zone) == 2 and zone[0] is not None:
            fig.add_hrect(
                y0=min(zone), y1=max(zone), line_width=0, fillcolor=colour,
                annotation_text=label, annotation_position="top left",
                row=1, col=1,
            )

    for key, label, colour in (
        ("resistance_level", "50D Ceiling", BEARISH),
        ("midpoint_level", "50% Midpoint", ACCENT_PRIMARY),
    ):
        level = overlays.get(key)
        if level:
            fig.add_hline(
                y=float(level), line={"color": colour, "width": 1.2, "dash": "dot"},
                annotation_text=label, annotation_position="right",
                row=1, col=1,
            )


def equity_chart(
    equity: pd.DataFrame, benchmark: pd.DataFrame | None = None, height: int = 380
) -> go.Figure:
    """Backtest equity curve against buy-and-hold (spec 3.8)."""
    fig = go.Figure()
    if equity is not None and not equity.empty:
        fig.add_trace(
            go.Scatter(
                x=equity["date"], y=equity["equity"], mode="lines", name="Strategy",
                line={"color": ACCENT_PRIMARY, "width": 2},
            )
        )
    if benchmark is not None and not benchmark.empty:
        fig.add_trace(
            go.Scatter(
                x=benchmark["date"], y=benchmark["equity"], mode="lines",
                name="Buy & Hold",
                line={"color": TEXT_MUTED, "width": 1.4, "dash": "dash"},
            )
        )
    fig.add_hline(y=100, line={"color": TEXT_MUTED, "width": 0.8, "dash": "dot"})
    fig.update_layout(
        height=height,
        margin={"l": 10, "r": 10, "t": 20, "b": 10},
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor=BG_CARD,
        font={"color": TEXT_PRIMARY, "size": 11},
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.05, "x": 0},
        yaxis_title="Equity (start = 100)",
    )
    fig.update_xaxes(showgrid=True, gridcolor="rgba(255,255,255,.06)")
    fig.update_yaxes(showgrid=True, gridcolor="rgba(255,255,255,.06)")
    return fig
