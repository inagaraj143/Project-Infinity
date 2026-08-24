# Project Infinity — Functional & System Requirements Specification Document

> **Project Name**: Project Infinity
> **Type**: Institutional-Grade Indian Stock Market (NSE) Screener, Pattern Scanner & Analytics Dashboard
> **Version**: 3.0.0 (Revised — errors corrected, UI/UX redesigned)
> **Architecture**: Python / Streamlit / Upstox API v2 / Plotly / SciPy / Pandas

---

## 📋 Table of Contents
1. [System Overview & Purpose](#1-system-overview--purpose)
2. [Data Pipeline & Market Data Specifications](#2-data-pipeline--market-data-specifications)
3. [Functional Module Requirements](#3-functional-module-requirements)
   - [3.1 📊 Top Ranked Stocks (Golden Zone Engine)](#31--top-ranked-stocks-golden-zone-engine)
   - [3.2 🔍 Know the Stock (Deep Fundamentals & Price Inspector)](#32--know-the-stock-deep-fundamentals--price-inspector)
   - [3.3 📉 13-Step High-Probability Active Trendline Scanner](#33--13-step-high-probability-active-trendline-scanner)
   - [3.4 📐 Bullish Triangle Pattern Scanner](#34--bullish-triangle-pattern-scanner)
   - [3.5 🚀 Resistance Breakout Scanner (Strict 4-Condition Filter)](#35--resistance-breakout-scanner-strict-4-condition-filter)
   - [3.6 🕯️ 50% Candle Rule Scanner](#36--50-candle-rule-scanner)
   - [3.7 💥 Institutional Displacement Engine](#37--institutional-displacement-engine)
   - [3.8 🧪 Historical Backtester](#38--historical-backtester)
4. [User Interface & UX Design Requirements](#4-user-interface--ux-design-requirements)
5. [Technical & System Environment Requirements](#5-technical--system-environment-requirements)
6. [Security, Privacy & Deployment Requirements](#6-security-privacy--deployment-requirements)
7. [Error Handling & Resilience Requirements](#7-error-handling--resilience-requirements)
8. [Testing & Quality Assurance Requirements](#8-testing--quality-assurance-requirements)
9. [Changelog — v2.5.0 → v3.0.0](#9-changelog--v250--v300)

---

## 1. System Overview & Purpose

**Project Infinity** is an advanced algorithmic stock screener, technical pattern scanner, and fundamental analysis system tailored for National Stock Exchange of India (NSE) equities. The system empowers traders and analysts to scan universe selections (`Nifty 50`, `Nifty 500`, `All NSE Equities`) for high-probability setups, trendline bounces, triangle breakouts, resistance breaches, institutional displacement events, and candle midpoint predictions.

### Key Architectural Principles
* **Modular View Architecture (`views/`)**: Each strategy concept lives in a dedicated view module — `views/view_top_ranked.py`, `views/view_know_stock.py`, `views/view_trendlines.py`, `views/view_triangle.py`, `views/view_resistance_breakout.py`, `views/view_candle_50.py`, `views/view_displacement.py`, `views/view_backtester.py` — for clean code isolation and independent testability.
* **Smart Session In-Memory Caching Engine**: `_DAILY_OHLC_CACHE` (15-min TTL) and `_INTRADAY_OHLC_CACHE` (5-min TTL) in `scanner/upstox_api.py` eliminate duplicate API calls when viewing charts, switching sub-tabs, or changing dropdown selections.
* **Live Trading Session Integration**: `_ensure_today_candle` appends today's live/completed trading session candle so analysis reflects current market values post-close.
* **True Lifetime IPO ATH Tracking**: Full listing-history queries compute true lifetime All-Time Highs for legacy stocks (e.g. `SUZLON` ATH of ₹422.19 in Jan 2008).
* **Candlestick ↔ Line Chart Toggle**: Every chart view across all 8 modules provides an instant toggle between Candlestick and Line Chart rendering.
* **8 Core Modules**: The system ships 8 functional modules (§3.1–3.8), each reachable from sidebar navigation. *(Corrected from v2.5.0, which stated 7 in §4 while listing 8 in the Table of Contents.)*

---

## 2. Data Pipeline & Market Data Specifications

### 2.1 Primary & Fallback Feeds
* **Primary Data Feed — Market Data (OHLCV)**: **Upstox API v2**
  * Instrument Master: Mapped daily from Upstox's complete exchange instrument file (`NSE_EQ` ISIN mapping).
  * Historical Daily Endpoint: `https://api.upstox.com/v2/historical-candle/{instrument_key}/day/{to_date}/{from_date}`
  * Intraday 5-Min / 15-Min Endpoint: `https://api.upstox.com/v2/historical-candle/{instrument_key}/minute/{to_date}/{from_date}`
  * Lifetime ATH history is retrieved by paging the daily endpoint backward from listing date to today in `to_date`/`from_date` windows (Upstox has no `period='max'` shorthand — that was a leftover from an earlier yfinance-based prototype and has been removed from this spec).
* **Fallback Data Feed — Market Data**: `yfinance`, used **only** when the Upstox historical-candle endpoint returns an error, times out, or the instrument key cannot be resolved for a given symbol. Fallback candles are visually flagged in the UI (see §4.6) so users know they are viewing a secondary source, and are never silently blended with primary-feed candles in the same chart series.
* **Fundamentals Data Feed**: Upstox API v2 does not provide company fundamentals. The 10 fundamental indicators in §3.2 are sourced from a dedicated fundamentals provider (e.g. a licensed financial-data API or a scheduled scrape of NSE corporate-filings data), refreshed on a daily batch job and cached locally. The specific provider must be selected and contracted before §3.2 implementation begins; this document defines the data contract (§3.2) but not the vendor.
* **Live vs Historical Market Status Indicator**: Detects active NSE trading session hours (Monday–Friday, 09:15 AM–03:30 PM IST) and drives the single top-right status badge defined in §4.6 (previously described inconsistently as two separate badges — consolidated here).

### 2.2 Authentication & Token Lifecycle
* **OAuth 2.0 Login Flow**: Upstox API v2 requires an interactive OAuth login (API Key + API Secret + redirect) to mint a daily **Access Token**. The system provides a one-click "Connect to Upstox" flow in the sidebar that opens the Upstox login page and captures the redirect code.
* **Daily Expiry**: Upstox access tokens expire once per day (around 3:30 AM IST). The app detects a `401`/token-expired response from any endpoint, halts in-flight scans gracefully (partial results preserved, not discarded), and prompts the user to re-authenticate rather than failing silently.
* **Token Storage**: Access token is held in-memory for the Streamlit session and persisted to `Key.txt` (local) or `st.secrets` (cloud) per §6.1 — never written to logs or exposed in the UI outside the redacted API transparency tab (§3.2, §6.1).

### 2.3 Rate Limiting & Full-Universe Scan Handling
* **Batching**: Full-universe scans (`All NSE Equities`, ~2,000+ symbols) issue requests in batches sized to stay under Upstox's published per-second and per-minute rate limits, with exponential backoff on `429` responses.
* **Progress Feedback**: Long-running scans display a progress bar with symbols-scanned / symbols-remaining counts, since a full-universe scan against per-symbol endpoints will take materially longer than a Nifty 50/500 scan.
* **Cache-First Scanning**: Scans always check `_DAILY_OHLC_CACHE` / `_INTRADAY_OHLC_CACHE` before issuing a network call, so re-running a scan or switching between modules within the TTL window does not re-hit the API for symbols already fetched.

### 2.4 Data Integrity Rules
* **Unadjusted Exchange Prices**: Uses raw actual exchange-traded prices (no split/dividend adjustment) to preserve true historical ATH values and match official trading-terminal price charts.
* **1D Series Sanitization**: Multi-index columns are flattened and 1D Series types enforced in `clean_dataframe()` to eliminate pandas 2.0+ 2D-assignment errors.

---

## 3. Functional Module Requirements

### 3.1 📊 Top Ranked Stocks (Golden Zone Engine)
* **Fibonacci Golden Zone**: Scans for stocks pulling back into the **50.0% to 61.8%** Fibonacci retracement zone of recent swing moves.
* **Multi-Factor Scoring (0–100 Points)**: Trend alignment (30 pts), Golden Zone proximity (30 pts), Volume confirmation (15 pts), Candle patterns (15 pts), Sector strength (10 pts).
* **Composite Dependency**: This module reads confirmed signals from §3.3 (trend direction/strength) and §3.6 (candle pattern signal) where available, rather than recomputing trend/candle logic independently, to keep scoring consistent across modules.

### 3.2 🔍 Know the Stock (Deep Fundamentals & Price Inspector)
* **Real-time Price Metrics**: Current LTP, Day High, Day Low, 52-Week High, 52-Week Low.
* **True Lifetime ATH**: True All-Time High price since IPO listing and distance-to-ATH %.
* **10 Fundamental Indicators** (sourced per §2.1 fundamentals feed):
  1. Market Capitalization (₹ Cr)
  2. Price-to-Earnings Ratio (P/E)
  3. Earnings Per Share (EPS)
  4. Book Value (₹)
  5. Face Value (₹)
  6. Price/Earnings-to-Growth (PEG) Ratio
  7. Return on Equity (ROE %)
  8. Debt to Equity Ratio
  9. Promoter Holdings (%)
  10. Pledged Shares (%)
* **Industry / Sector Peer Comparison**: Side-by-side comparison against sector averages (P/E, ROE, Debt/Equity, Market Cap).
* **`📡 API Requests & Responses` Tab**: Displays exact HTTP Request URLs, Headers, Parameters, and JSON Response Payloads for transparency. **The `Authorization` header value (bearer access token) and any API Secret are redacted (masked as `Bearer ••••••••`) in this tab at all times** — see §6.1. This applies regardless of deployment mode, and is mandatory before the public-tunnel deployment option in §6.2 is enabled.

---

### 3.3 📉 13-Step High-Probability Active Trendline Scanner
Strict 13-step quantitative architecture:

| Step | Requirement Description |
| :--- | :--- |
| **Step 1: Data Collection** | Minimum 250 Daily bars & 60 15-minute bars. |
| **Step 2: Swing Extrema Detection** | Swing Highs & Swing Lows identified via `scipy.signal.argrelextrema(order=4)`. |
| **Step 3: Candidate Building** | **Uptrend**: Connects ≥2 ascending swing lows (slope > 0).<br>**Downtrend**: Connects ≥2 descending swing highs (slope < 0). |
| **Step 4: Validation** | No major close breaches before latest bar; distance between touches ≥4 candles; tolerance ≤1%. |
| **Step 5: Current Price Position** | **Uptrend**: Close price at or within 0% to +3% above support line.<br>**Downtrend**: Close price testing or within −3% to 0% below resistance line. |
| **Step 6: Daily Confirmation** | **Uptrend**: Higher Highs / Higher Lows & Close > EMA 50.<br>**Downtrend**: Lower Highs / Lower Lows & Close < EMA 50. |
| **Step 7: 15-Min Confirmation** | 15-minute intraday trend direction aligns with Daily trend direction. |
| **Step 8: RSI Confirmation** | **Uptrend**: 50 ≤ RSI ≤ 70, rising, not > 80.<br>**Downtrend**: 30 ≤ RSI ≤ 50, falling, not < 20. |
| **Step 9: MACD Confirmation** | **Uptrend**: MACD Line > Signal Line & Histogram > 0.<br>**Downtrend**: MACD Line < Signal Line & Histogram < 0. |
| **Step 10: Moving Average Confirmation** | **Uptrend**: Close > EMA 20 > EMA 50 > EMA 200, sloping upward.<br>**Downtrend**: Close < EMA 20 < EMA 50 < EMA 200, sloping downward. |
| **Step 11: Volume Confirmation** | Volume > 20-day Volume SMA OR Volume ≥ 1.5× previous candle volume. |
| **Step 12: Trendline Strength Score** | **Max Score = 100**: Valid Trendline (+20), 2 Touches (+15) or 3+ Touches (+25), Daily Confirmed (+15), 15-Min Confirmed (+10), RSI Confirmed (+10), MACD Confirmed (+10), EMA Alignment (+10), Volume Confirmed (+10). |
| **Step 13: Final Classification** | 90–100: Excellent Setup (Buy/Sell Candidate) · 80–89: High Probability Setup (Buy/Sell Candidate) · 70–79: Good Setup (Watchlist) · 60–69: Moderate Setup (Watchlist) · <60: Reject |

#### 13-Column Output Table Specification:
`Stock Name`, `Trend Direction`, `Daily Status`, `15-Min Status`, `Touches`, `Distance %`, `RSI Value`, `MACD Status`, `EMA Alignment`, `Volume Confirmation`, `Strength Score`, `Overall Signal`, `Trendline Price`.
*(Corrected count from v2.5.0, which labeled this a "12-Column" table while listing 13 columns.)*

---

### 3.4 📐 Bullish Triangle Pattern Scanner
* **Ascending Triangle (Bullish Continuation)**:
  * Upper Resistance Line: Flat horizontal ceiling (|slope| ≤ 0.035% per bar).
  * Lower Support Line: Ascending higher lows (slope > +0.015% per bar).
* **Symmetrical Triangle (Bullish Breakout)**:
  * Upper Resistance Line: Descending lower highs (slope < −0.015% per bar).
  * Lower Support Line: Ascending higher lows (slope > +0.015% per bar).
* **Bullish Momentum Qualification**: Close ≥ EMA 20 × 0.98 and EMA 20 ≥ EMA 50 × 0.97.

---

### 3.5 🚀 Resistance Breakout Scanner (Strict 4-Condition Filter)
1. **50-Day Ceiling Average Price**: `ceil(mean((Open+High+Close)/3 over last 50 days)) < Current Daily Close`.
2. **Short-Term Trend Momentum**: EMA 8 > EMA 13 (8-day EMA is stronger and rising faster than 13-day EMA). *(Corrected direction from v2.5.0's `EMA 13 < EMA 8`, which was mathematically the same inequality but stated backwards in prose — both forms are reconciled here as EMA 8 > EMA 13.)*
3. **Volume Surge**: Today's Volume ≥ 150% of Previous Day Volume (Volume Ratio ≥ 1.50×).
4. **Bullish Green Candle**: Daily Close > Daily Open.

---

### 3.6 🕯️ 50% Candle Rule Scanner
* **Concept**: Evaluates price action relative to the 50% midpoint level ((High + Low) / 2) of valid reference candles.
* **Backward Reference Search**: Filters out indecisive Dojis and Spinning Tops (body < 25% of candle range) to lock onto the most recent decisive directional candle.
* **Prediction Engine**: Predicts next candle direction (Bullish / Bearish / Undecided) with a 0–100 Confidence Score.

---

### 3.7 💥 Institutional Displacement Engine
*(New in v3.0.0 — this module was referenced in the v2.5.0 architecture and Table of Contents but had no functional specification. Defined here for the first time.)*

* **Concept**: Detects candles that show a sudden, high-conviction directional move consistent with large institutional order flow — a "displacement" — distinct from ordinary volatility.
* **Displacement Candle Criteria** (all must hold):
  1. **Range Expansion**: Candle's (High − Low) ≥ 2.0× the 20-day Average True Range (ATR).
  2. **Volume Surge**: Candle volume ≥ 3.0× the 20-day Volume SMA.
  3. **Directional Close**: Close in the top 25% of the candle's range (bullish displacement) or bottom 25% (bearish displacement) — i.e. a strong-bodied candle, not a long-wick reversal.
  4. **Gap Context (optional flag, not required)**: If the candle also gaps beyond the prior day's high/low, it is additionally tagged `Gap Displacement`.
* **Imbalance Zone**: The engine marks the price range left behind by the displacement candle (the gap between the prior candle's close and the displacement candle's open-side extreme) as an unfilled imbalance zone, plotted as a shaded band on the chart.
* **Signal Logic**: A displacement candle followed by ≥2 subsequent candles that hold above the imbalance zone (bullish) or below it (bearish) without a close back inside the zone is classified `Confirmed Displacement`. A close back inside the zone within 5 candles is classified `Failed Displacement`.
* **Output Table**: `Stock Name`, `Displacement Date`, `Direction`, `Range vs ATR`, `Volume vs SMA`, `Gap Displacement (Y/N)`, `Imbalance Zone (Low–High)`, `Status (Confirmed/Pending/Failed)`.

---

### 3.8 🧪 Historical Backtester
*(Expanded in v3.0.0 — v2.5.0 defined only a one-line description with no methodology.)*

* **Purpose**: Evaluates historical performance of any scanner strategy (§3.1, §3.3–§3.7) across a user-selectable historical date window and universe.
* **Entry Rule**: A position is opened on the next candle's open after a symbol first appears in a strategy's qualifying output (e.g. Strength Score ≥ 80 for §3.3, `Confirmed Displacement` for §3.7).
* **Exit Rules** (user-configurable, one active at a time per backtest run):
  * Fixed holding period (N candles), or
  * Stop-loss / target % (e.g. −5% / +10%), or
  * Signal invalidation (e.g. trendline breach, EMA cross-back).
* **Transaction Costs**: Backtest results are computed both gross and net of a configurable cost assumption (brokerage + STT + slippage, default 0.15% round-trip) so users can see the impact of costs on edge.
* **Reported Metrics**: Win Rate %, Profit Factor, Total Trades, Average Win %, Average Loss %, Max Drawdown %, Sharpe Ratio (annualized), Expectancy per trade.
* **Output**: Equity curve chart (cumulative return vs. buy-and-hold benchmark for the same universe/period) plus the full trade-by-trade log, exportable to CSV.

---

## 4. User Interface & UX Design Requirements

### 4.1 Design Philosophy
A dense, data-forward "trading terminal" aesthetic rather than a consumer dashboard: information density and scanability take priority over decorative whitespace, while still keeping a clear visual hierarchy so a 13-column scanner table doesn't read as a wall of numbers.

### 4.2 Color System (Dark Mode, Default & Only Theme)
| Token | Value | Usage |
| :--- | :--- | :--- |
| `--bg-app` | `#0e1117` | App background |
| `--bg-card` | `#1f2937` | Panels, tables, cards |
| `--bg-card-hover` | `#26303f` | Row/card hover state |
| `--accent-primary` | `#26a69a` | Primary actions, active nav item, positive accents |
| `--bullish` | `#00c853` | Bullish signals, green candles, "Buy" tags |
| `--bearish` | `#ff5252` | Bearish signals, red candles, "Sell" tags |
| `--warning` | `#ff9100` | Historical-data state, moderate-confidence tags |
| `--danger` | `#ff1744` | Errors, token expiry, destructive actions |
| `--text-primary` | `#e5e7eb` | Primary text |
| `--text-muted` | `#9ca3af` | Secondary labels, timestamps |

### 4.3 Navigation
* **Left Sidebar, Icon + Label**: All 8 modules listed with their emoji glyph (§3.1–3.8) plus a search/filter box at the top of the sidebar for jumping to a module by name once the list grows.
* **Active Module Highlight**: Current module's sidebar entry uses `--accent-primary` background at 15% opacity with a solid left border, rather than v2.5.0's plain radio-button list, so the active section is legible at a glance.
* **Universe Selector**: Persistent across all scanner modules (Nifty 50 / Nifty 500 / All NSE Equities), pinned at the top of the main content area rather than re-declared per module, so switching modules doesn't reset the user's chosen universe.

### 4.4 Scanner Table UX
* **Sticky Header + Sticky First Column**: For wide tables (e.g. the 13-column trendline output), the `Stock Name` column and header row stay pinned while scrolling.
* **Inline Signal Chips**: `Overall Signal`, `Trend Direction`, `Status` columns render as small colored pill/chip badges (bullish = green pill, bearish = red pill, watchlist = amber pill) instead of plain text, so the eye can scan a column of chips faster than a column of words.
* **Score Bars**: Any 0–100 score column (Strength Score, Confidence Score) renders a thin inline horizontal bar behind the number for instant visual ranking.
* **Row Click → Chart Drill-in**: Clicking any table row opens that symbol's chart (§4.5) in a side panel without navigating away from the scan results, so users can flip through candidates quickly.
* **Column Sort & Filter**: Every numeric/categorical column is sortable; a filter row beneath the header allows quick numeric range filters (e.g. Strength Score ≥ 80) and category filters (e.g. Direction = Bullish).

### 4.5 Charting
* **Dual-Subplot Plotly Layout**: Price action (upper, ~75% height) and Volume (lower, ~25% height), sharing a synchronized x-axis and crosshair.
* **Candlestick ↔ Line Toggle**: Instant radio toggle, retained per-module in session state so switching tabs doesn't reset it.
* **Overlay Layer Controls**: Trendlines, resistance ceilings/support bounds, EMA overlays (20/50/200), and — new in v3.0.0 — Institutional Displacement imbalance zones (§3.7) are each independently toggleable via a small legend-style control strip above the chart, rather than always-on, so charts don't become cluttered when multiple modules' overlays could apply to the same symbol.
* **Timeframe Switch**: Daily / 15-Min toggle above the chart, matching the dual-timeframe confirmation logic used in §3.3 Step 7.

### 4.6 Status & Data-Source Indicator
*(Consolidated from v2.5.0, which described two conflicting badges — a green/amber LIVE/HISTORICAL badge in §2.1 and a separate red "DATA SOURCE: API" badge in §4 — into one badge with corrected color logic.)*
* **Single Top-Right Status Badge**, always visible, two states:
  * 🟢 **LIVE MARKET** — background `--bullish` (`#00c853`): shown Mon–Fri, 09:15–15:30 IST, Upstox streaming live intraday data.
  * 🌙 **HISTORICAL DATA** — background `--warning` (`#ff9100`): shown outside market hours (after 15:30, before 09:15, or weekends), serving settled historical candles.
* **Fallback-Feed Indicator**: When any visible chart or table row is drawing from the yfinance fallback feed (§2.1) rather than Upstox, that row/chart carries a small `⚠ Fallback Source` tag — this is additive to the market-status badge, not a replacement for it.

### 4.7 Responsiveness
* **Primary Target**: Desktop/laptop widths (≥1280px), reflecting the trading-terminal use case.
* **Narrow Viewport Degradation**: Below 1024px, the sidebar collapses to an icon-only rail (expandable on tap), and wide scanner tables switch from a table layout to a stacked card-per-stock layout to remain usable on tablets.

### 4.8 Accessibility
* All signal chips and colored badges pair color with a text label or icon (never color alone) so the interface remains usable for color-vision-deficient users — relevant given the bullish/bearish green/red convention throughout.
* Minimum contrast ratio of 4.5:1 maintained between `--text-primary`/`--text-muted` and their background tokens.

---

## 5. Technical & System Environment Requirements

### 5.1 Operating System & Runtime
* **OS**: Windows / Linux / macOS
* **Python Runtime**: Python 3.10, 3.11, 3.12, or 3.13

### 5.2 Python Dependencies (`requirements.txt`)
```text
streamlit
pandas
numpy
yfinance
requests
plotly
scipy
tqdm
openpyxl
```
`yfinance` is retained here specifically as the documented fallback feed (§2.1) — not a leftover dependency. If the fallback feed is later removed by product decision, this entry should be removed in the same change.

---

## 6. Security, Privacy & Deployment Requirements

### 6.1 Credentials Security
* **`Key.txt` Protection**: Local Upstox API credentials (API Key, API Secret, Access Token) are stored in `Key.txt`.
* **`.gitignore` Enforced**: `Key.txt` and `.streamlit/secrets.toml` are strictly excluded from git tracking.
* **`st.secrets` Fallback**: Supports `st.secrets["UPSTOX_ACCESS_TOKEN"]` when hosted on cloud servers.
* **Token Redaction (new in v3.0.0)**: The §3.2 API Requests & Responses tab must redact the `Authorization` header and any secret/token value in both the request and response payload views, in every deployment mode. This is a hard prerequisite for enabling §6.2 option 2 (public tunnel sharing) — the app should refuse to start a public tunnel session if redaction is disabled.

### 6.2 Deployment Options
1. **Local Desktop Execution**: `run_dashboard.bat` (`streamlit run app.py`).
2. **Code-Free Public Web Sharing (`share_dashboard.bat`)**: `localtunnel` (`npx localtunnel --port 8501`) generates an instant `https://...loca.lt` URL for external users without exposing source code. Requires §6.1 token redaction to be active.
3. **24/7 Streamlit Community Cloud Hosting**: Connects to a GitHub private repository with automatic 24/7 hosting via `share.streamlit.io`.

---

## 7. Error Handling & Resilience Requirements
*(New in v3.0.0 — undefined in v2.5.0.)*

* **API Failures**: Any Upstox API call that fails (timeout, 5xx, malformed response) falls back to the yfinance feed per §2.1 for that symbol only; the scan continues rather than aborting, and failed symbols are listed in a collapsible "Skipped Symbols" panel with the failure reason.
* **Partial Data**: If a symbol has fewer than the minimum bar count required by a module (e.g. 250 daily bars for §3.3 Step 1), it is excluded from that scan's output with reason `Insufficient History`, not silently dropped or treated as a non-match.
* **Token Expiry Mid-Scan**: Per §2.2, an expired token pauses the scan, preserves already-fetched results, and surfaces a re-authenticate prompt rather than discarding progress.
* **Instrument Key Resolution Failures**: Symbols that can't be mapped to a valid Upstox `instrument_key` on a given day are logged and excluded, with a visible count ("14 symbols skipped — instrument mapping unavailable") rather than failing the whole scan.

---

## 8. Testing & Quality Assurance Requirements
*(New in v3.0.0 — undefined in v2.5.0.)*

* **Indicator Unit Tests**: EMA/RSI/MACD/ATR calculations validated against known reference values for a fixed sample dataset.
* **Scanner Logic Tests**: Each of the 13 trendline scanner steps (§3.3) independently testable against synthetic OHLCV fixtures engineered to trigger/not-trigger each condition.
* **Backtester Validation**: Backtest engine (§3.8) validated against a small hand-computed trade set to confirm win rate, profit factor, and drawdown formulas are correct before trusting it on live strategies.
* **Cache Correctness**: Tests confirming `_DAILY_OHLC_CACHE` / `_INTRADAY_OHLC_CACHE` TTL expiry and that cached data is never served stale across a market-status transition (e.g. LIVE → HISTORICAL at 15:30 IST).

---

## 9. Changelog — v2.5.0 → v3.0.0

* **Added** full specification for §3.7 Institutional Displacement Engine (previously referenced but undefined).
* **Added** §7 Error Handling & Resilience and §8 Testing & QA (previously absent).
* **Added** §2.2 Auth/Token Lifecycle and §2.3 Rate Limiting (previously unaddressed).
* **Added** named fundamentals-data-feed requirement in §2.1 and §3.2 (previously unsourced).
* **Added** token redaction requirement in §3.2 / §6.1 (previously a latent leak in the public-tunnel deployment path).
* **Expanded** §3.8 Historical Backtester from a single bullet to a full methodology (entry/exit rules, costs, metrics).
* **Fixed** module count mismatch — UI section now correctly states 8 modules (was "7" against a TOC of 8).
* **Fixed** §3.3 output table column count label (was "12-Column" while listing 13 columns).
* **Fixed** §3.5 Condition 2 wording ambiguity (EMA 8 > EMA 13, stated unambiguously).
* **Fixed** duplicate/conflicting status badge description — consolidated into one badge spec in §4.6, with the emoji/color mismatch (🔴 labeled as green) corrected.
* **Fixed** yfinance-specific parameters (`auto_adjust=False`, `period='max'`) that don't apply to the Upstox API — replaced with Upstox-native equivalents and yfinance's role formally scoped to fallback-only.
* **Replaced** §4 (previously "User Interface & Charting Requirements") with a full UI/UX design section (§4.1–4.8): color system, navigation redesign, scanner-table UX, chart overlay controls, consolidated status badge, responsiveness, and accessibility requirements.
