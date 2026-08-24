# Project Infinity

NSE screener, pattern scanner and analytics dashboard.
Spec: [requirement_claude.md](requirement_claude.md) · Review: [docs/review-v3.md](docs/review-v3.md)

**Status: all eight modules built** (spec §3.1–3.8), on a working data pipeline.
223 tests, ruff clean.

---

## Quick start

```bash
py -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt

.venv/Scripts/python.exe -m pytest -q          # 223 tests
.venv/Scripts/python.exe -m streamlit run app.py
```

Or just double-click `run_dashboard.bat`, which creates the venv on first run.

Refresh reference data and build snapshots:

```bash
py scripts/refresh_reference.py                       # instrument master + universes
py scripts/build_snapshots.py --universe nifty500     # EOD bars
py scripts/build_snapshots.py --universe nifty50 --limit 5 -v   # quick check
```

### Reference data

Both sources are public and need no auth, which is what lets CI refresh them:

| Data | Source |
|---|---|
| Instrument master (symbol → `NSE_EQ\|ISIN`) | `assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz` |
| Daily + intraday candles | `api.upstox.com/v2` and `/v3` — **no auth required** ([ADR 0002](docs/adr/0002-upstox-needs-no-auth.md)) |
| Nifty 50 / 500 constituents + **Industry** | `archives.nseindia.com/content/indices/ind_nifty*list.csv` |

2,295 NSE equities after filtering ETF/fund units — Indian ISINs encode this
(`INE…` = equity, `INF…` = fund unit). The `Industry` column feeds §3.1's
sector-strength score, which the spec left unsourced.

---

## How the data layer works

The rule that drives everything: **a snapshot is fresh iff
`captured_at >= last_session_close()`.**

```
                        ┌─ LIVE  (Mon–Fri 09:15–15:30 IST, non-holiday)
  market_clock ─────────┤     memory cache (TTL) → provider → write snapshot
  (Asia/Kolkata +       │
   NSE holidays)        └─ CLOSED (nights, weekends, holidays)
                              fresh snapshot? → serve it, zero network calls
                              stale/absent?   → ONE fetch → write → serve
```

That single predicate covers nights, weekends, holidays, and the 15:30
LIVE→CLOSED flip. No wall-clock TTL is used on the closed path — settled
candles do not change, so a Friday-evening snapshot is still correct on Sunday
night.

Measured on Nifty 50 (5 symbols, 273 bars each):

| Run | Path | Time |
|---|---|---|
| First | network | 2.5 s |
| Second | snapshot | 0.1 s, **0 API calls** |

### Hosting

```
GitHub (private repo)
  ├── .github/workflows/eod.yml   cron 10:15 UTC = 15:45 IST, Mon–Fri
  │      builds JSON snapshots, commits them
  └── Streamlit Community Cloud   auto-deploys from main, reads the JSON
```

Total cost ₹0. The hosted app makes **no** market-API calls while the market is
closed — the workflow has already written the snapshots. See
[docs/adr/0001-feed-strategy.md](docs/adr/0001-feed-strategy.md) for why the CI
job runs on yfinance rather than Upstox.

Two GitHub Actions facts worth remembering:
- Cron is UTC and can drift 5–20 minutes under load — hence 15:45 IST, not 15:31.
- Scheduled workflows are auto-disabled after **60 days of repo inactivity**.
  The daily snapshot commit keeps the cron alive.

---

## Layout

```
app.py                       Streamlit entry (st.navigation shell)
infinity/
  market_clock.py            IST clock, NSE holidays, session state   ★
  config.py                  paths, secrets resolution, Universe enum
  data/
    models.py                Snapshot schema (columnar JSON)
    snapshot_store.py        atomic read/write, freshness predicate   ★
    resolver.py              LIVE/CLOSED fetch policy                 ★
    batch.py                 rate-limited universe fetch, partial results
    instruments.py           NSE_EQ master, symbol → instrument_key
    universe.py              index constituents + industry buckets
    candles.py               ensure_today_candle, bar helpers
    providers/
      base.py                DataProvider protocol + clean_dataframe
      upstox_provider.py     PRIMARY — daily + intraday, no auth needed
      yfinance_provider.py   fallback feed
  indicators.py              EMA/SMA/RSI/MACD/ATR/swings (SMA-seeded)
  scanners/                  §3.1, 3.3-3.7 — one module each
  scan_runner.py             dependency-ordered execution + sector scores
  fundamentals.py            §3.2, 8/10 indicators, provenance-tagged
  backtest.py                §3.8, corporate actions + portfolio equity
  ui/theme.py                §4.2 tokens, status badge, chips
  ui/tables.py               §4.4 pinned cols, score bars, row-click
  ui/charts.py               §4.5 Plotly dual-subplot + overlays
scripts/
  refresh_reference.py       instrument master + universes
  build_snapshots.py         the EOD job
views/                       Streamlit pages (know-stock, backtester, common)
tests/                       223 tests
```

`data/ohlc/` is gitignored (bulk, regenerated). `data/snapshots/` **is** tracked
— that is what the hosted app reads.

---

## Configuration

Secrets resolve in order: `st.secrets` → environment → `Key.txt`.
All three are gitignored. Copy `.streamlit/secrets.toml.example` or
`Key.txt.example` to get started.

Nothing is required. Without an Upstox token the app runs on yfinance, which is
the intended state for the hosted build.

---

## Before you trust this in production

**`data/nse_holidays.json` — 10 of 16 dates for 2026 are almanac estimates.**
Confirm against the [official NSE circular][nse] and set `verified: true`. The
app shows a banner until you do. A wrong holiday silently corrupts snapshot
freshness: the app serves stale JSON for a whole session, or reports LIVE on a
closed day.

Unlike the universe lists — which are now fetched live from NSE on every
refresh — NSE publishes no machine-readable holiday feed, so this one has to be
pasted in by hand once a year.

[nse]: https://www.nseindia.com/resources/exchange-communication-holidays

---

## Modules

| § | Module | Notes |
|---|---|---|
| 3.1 | Top Ranked (Golden Zone) | 50–61.8% Fib retracement, 5-component score |
| 3.2 | Know the Stock | 8/10 fundamentals, lifetime ATH, peer comparison |
| 3.3 | 13-Step Trendline | each step independently testable |
| 3.4 | Bullish Triangle | ascending + symmetrical |
| 3.5 | Resistance Breakout | strict 4-condition filter |
| 3.6 | 50% Candle Rule | decisive-candle midpoint |
| 3.7 | Institutional Displacement | 3-candle fair-value gap |
| 3.8 | Historical Backtester | costs, corporate actions, benchmark |

Live Nifty 500 run: 500 symbols × 6 scanners in **11 seconds**.

## Known open items

Full list in [docs/review-v3.md](docs/review-v3.md). Still needing **your**
decision or action:

- **NSE holiday calendar** — 10/16 dates for 2026 are estimates. Highest-value
  remaining fix; see above.
- **C3** — §3.5 uses `(O+H+C)/3` (drops the Low) and `ceil()` to whole rupees.
  Both are implemented as configurable, defaulting to the spec's literal
  wording. Which did you intend?
- **C6** — backtests carry survivorship bias; NSE publishes no free historical
  constituent series, so every run states it rather than correcting it.
- **G5** — §6.2.2 localtunnel is unauthenticated. Add a password gate or drop it.
- **G7** — §4.7 sub-1024px card layout not built; recommend cutting for a
  desktop trading terminal.
