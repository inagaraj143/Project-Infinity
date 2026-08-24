"""Design tokens and shared widgets (spec 4.2, 4.4, 4.6, 4.8).

Colour is never the only signal: every chip carries a glyph and a text label,
which is the spec 4.8 accessibility requirement.
"""

from __future__ import annotations

import streamlit as st

from infinity.market_clock import MarketStatus

# -- spec 4.2 colour system -------------------------------------------------
BG_APP = "#0e1117"
BG_CARD = "#1f2937"
BG_CARD_HOVER = "#26303f"
ACCENT_PRIMARY = "#26a69a"
BULLISH = "#00c853"
BEARISH = "#ff5252"
WARNING = "#ff9100"
DANGER = "#ff1744"
TEXT_PRIMARY = "#e5e7eb"
TEXT_MUTED = "#9ca3af"

_CSS = f"""
<style>
  :root {{
    --bg-app: {BG_APP};
    --bg-card: {BG_CARD};
    --bg-card-hover: {BG_CARD_HOVER};
    --accent-primary: {ACCENT_PRIMARY};
    --bullish: {BULLISH};
    --bearish: {BEARISH};
    --warning: {WARNING};
    --danger: {DANGER};
    --text-primary: {TEXT_PRIMARY};
    --text-muted: {TEXT_MUTED};
  }}

  /* 4.1: dense trading-terminal layout, not a consumer dashboard. */
  .block-container {{ padding-top: 2.2rem; padding-bottom: 2rem; max-width: 100%; }}

  .inf-statusbar {{
    display: flex; align-items: center; justify-content: space-between;
    gap: 1rem; padding: .55rem .9rem; margin-bottom: 1rem;
    background: var(--bg-card); border-radius: 8px;
    border: 1px solid rgba(255,255,255,.06);
  }}
  .inf-title {{ font-weight: 650; letter-spacing: .3px; color: var(--text-primary); }}
  .inf-sub {{ color: var(--text-muted); font-size: .82rem; }}

  .inf-badge {{
    display: inline-flex; align-items: center; gap: .4rem;
    padding: .28rem .7rem; border-radius: 999px;
    font-size: .78rem; font-weight: 700; letter-spacing: .4px;
    color: #06170f; white-space: nowrap;
  }}
  .inf-badge--live {{ background: var(--bullish); }}
  .inf-badge--hist {{ background: var(--warning); }}

  .inf-chip {{
    display: inline-flex; align-items: center; gap: .3rem;
    padding: .12rem .5rem; border-radius: 5px;
    font-size: .76rem; font-weight: 600;
  }}
  .inf-chip--bull   {{ background: rgba(0,200,83,.16);  color: var(--bullish); }}
  .inf-chip--bear   {{ background: rgba(255,82,82,.16); color: var(--bearish); }}
  .inf-chip--watch  {{ background: rgba(255,145,0,.16); color: var(--warning); }}
  .inf-chip--muted  {{ background: rgba(156,163,175,.14); color: var(--text-muted); }}

  section[data-testid="stSidebar"] {{ background: var(--bg-card); }}
</style>
"""


def inject_css() -> None:
    """Apply the token sheet once per page render."""
    st.markdown(_CSS, unsafe_allow_html=True)


def chip(label: str, kind: str = "muted", glyph: str = "") -> str:
    """Inline signal chip (spec 4.4). Always glyph + text, never colour alone."""
    prefix = f"{glyph} " if glyph else ""
    return f'<span class="inf-chip inf-chip--{kind}">{prefix}{label}</span>'


def status_bar(status: MarketStatus, subtitle: str = "") -> None:
    """Single top-right status badge (spec 4.6) plus the page title block."""
    cls = "inf-badge--live" if status.is_live else "inf-badge--hist"
    st.markdown(
        f"""
        <div class="inf-statusbar">
          <div>
            <div class="inf-title">Project Infinity</div>
            <div class="inf-sub">{subtitle or status.detail}</div>
          </div>
          <div style="text-align:right">
            <span class="inf-badge {cls}">{status.badge_icon} {status.badge_label}</span>
            <div class="inf-sub" style="margin-top:.25rem">
              {status.as_of:%d %b %Y · %H:%M} IST
            </div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if not status.calendar_verified:
        st.warning(
            "**NSE holiday calendar is unverified.** Some dates in "
            "`data/nse_holidays.json` are estimated. Confirm them against the "
            "official NSE circular and set `verified: true` — until then the "
            "LIVE/HISTORICAL badge and the snapshot cache can be wrong around "
            "holidays.",
            icon="⚠️",
        )
