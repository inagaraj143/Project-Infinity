# Spec review — requirement_claude.md v3.0.0

Reviewed 2026-08-23. Tracks findings against
[requirement_claude.md](../requirement_claude.md). Status reflects Phase 2 (all
eight modules built).

## Blockers

| # | Finding | Status |
|---|---|---|
| **B1** | **Upstox OAuth kills 24/7 hosting.** | ✅ **Resolved — and the premise was wrong.** [ADR 0002](adr/0002-upstox-needs-no-auth.md): Upstox candle endpoints need **no authentication at all** (verified against the live API). Upstox is now primary everywhere including CI, with yfinance as the fallback — which is what §2.1 asked for originally. ADR 0001 reasoned from the spec instead of testing the API. |
| **B2** | **§2.4 (unadjusted prices) contradicts §3.8 (backtester).** | ✅ Resolved — the backtester detects split-sized overnight gaps and, per `split_policy`, excludes the symbol (default), back-adjusts, or reproduces the spec-literal behaviour with a loud warning. A live Nifty 50 run excluded ADANIENT and TMPV on real corporate actions. |
| **B3** | **§3.2's 10 fundamentals have no viable free source.** | ✅ **Resolved, and the original estimate was too pessimistic.** A live probe found **8 of 10** obtainable: PEG *is* published, and `heldPercentInsiders` is a usable promoter-holding proxy. Only **Face Value** and **Pledged Shares** are genuinely unavailable. Every value carries a `Provenance` tag (reported / derived / proxy / unavailable) so a derived ROE is never passed off as a reported one. |

## Correctness bugs

| # | Finding | Status |
|---|---|---|
| **C1** | **§3.3 Step 12 sums to 110, not 100.** | ✅ Resolved — `normalise_score(raw, max_attainable)`. The bands now mean one thing at any touch count; `Raw Score` keeps the arithmetic auditable. |
| **C2** | **§3.3 Step 1 wants only 60 fifteen-minute bars.** | ✅ Resolved — raised to 200, and Step 7 now **actually runs**: ADR 0002 unlocked the intraday feed. Live Nifty 50: 19 Confirmed / 26 Not Confirmed, replacing 45 × "No Data". The denominator moves 90 → 100 when intraday is present. |
| **C3** | **§3.5 Condition 1** drops the Low and `ceil()` quantises to ₹1. | ✅ Surfaced as `typical_price_mode` and `use_ceil`, defaulting to the spec-literal behaviour. A test shows `ceil()` masking the OHC/HLC difference entirely on a real fixture. **Still needs your decision on which is intended.** |
| **C4** | **§3.7 imbalance zone isn't a real imbalance.** | ✅ Resolved — standard 3-candle fair-value gap by default (`zone_mode="fvg"`), with the literal wording available as `zone_mode="spec"`. |
| **C5** | **§3.1's cross-module dependency is an ordering trap.** | ✅ Resolved — `scan_runner` topologically orders scanners by `depends_on` and threads results through `ScanContext.upstream`. Scanners never import each other, and every row records `Trend Source` / `Candle Source` so a fallback is visible rather than silent. |
| **C6** | **§3.8 has no point-in-time universe.** | ⚠️ Cannot be fixed with the available data — NSE publishes no historical constituent series for free. Every backtest now states the bias in `BacktestResult.warnings`. |

## Gaps

| # | Finding | Status |
|---|---|---|
| **G1** | No NSE holiday calendar — §2.1/§4.6 check only Mon–Fri 09:15–15:30. | ⚠️ Partly resolved — `data/nse_holidays.json` + `market_clock`. **10/16 dates for 2026 are still estimated**; app warns until `verified: true`. This is now the single highest-value remaining fix: unlike the universe lists, NSE publishes no machine-readable holiday feed, so it must be pasted in by hand. |
| **G2** | No timezone discipline. Streamlit Cloud, Actions and Vercel all run UTC. | ✅ Resolved — everything is `ZoneInfo("Asia/Kolkata")`; `to_ist()` rejects naive datetimes. |
| **G3** | No on-disk cache schema. §2.3 defines in-memory caching only. | ✅ Resolved — versioned columnar JSON, atomic writes, `SNAPSHOT_SCHEMA`. |
| **G4** | §5.2 has no version pins; missing `pyarrow`, `pytest`, `tzdata`. | ✅ Resolved — ranges in `requirements.txt`; run `pip freeze > requirements.lock.txt` before deploying. |
| **G5** | §6.2.2 localtunnel is unauthenticated — token redaction in the UI doesn't stop a stranger driving your logged-in session. | ⏳ Open. Recommend a password gate, or drop deployment option 2. |
| **G6** | §4.4 is ~85% native Streamlit. | ✅ Built — pinned Symbol column, sortable headers, `ProgressColumn` score bars, `on_select` row-click drill-in. Chips are glyph+text (§4.8 compliant) and the filter row is a control strip above the table. `streamlit-aggrid` still deliberately avoided. |
| **G7** | §4.7 sub-1024px card layout means a second render path for every table. | ⏳ Recommend cutting from v1 — desktop trading terminal. |
| **G8** | yfinance is unofficial Yahoo scraping — gray area for a public app. | ⏳ Note in ADR 0001. Fine privately. |

## Found during Phase 0 implementation

| Finding | Status |
|---|---|
| yfinance returns MultiIndex `(field, ticker)` columns; a naive flattener picks the ticker level and every symbol fails with "missing columns". | ✅ Fixed in `_pick_field_level()`, with a regression test in `tests/test_clean_dataframe.py`. |
| Feed prices carry float32 noise (`2596.300048828125`) — NSE tick size is 0.05. | ✅ Rounded to 2dp on write: 32% smaller on disk and strictly more correct. |
| Bulk OHLC for Nifty 500 × 3000 bars projects to **~74 MB** of JSON. | ✅ `data/ohlc/` is gitignored so it never enters history, and the EOD workflow now restores it from an `actions/cache` keyed per universe, so CI tops up rather than refetching 12 years daily. |

## Found during Phase 1 implementation

| Finding | Status |
|---|---|
| **The hand-written `nifty50.json` was wrong.** It was missing ETERNAL, INDIGO, JIOFIN and MAXHEALTH — four current constituents — and carried four that had been removed. | ✅ Replaced by a live fetch from NSE's archive CSV. This is exactly why it shipped flagged `verified: false`. |
| **The Upstox instrument master needs no auth.** `assets.upstox.com/.../NSE.json.gz` is a public file, so CI can refresh the ISIN mapping without a token — the same property that makes ADR 0001 work. | ✅ `infinity/data/instruments.py`. |
| **The NSE constituent CSVs carry an `Industry` column.** That is a free source for the sector-strength component of §3.1 scoring, which the spec left unsourced. | ✅ Persisted per member; `UniverseList.by_industry` exposes the buckets. Nifty 500 resolves to 20 industries. |
| **`instrument_type == "EQ"` includes ETF units** (e.g. SMALLIETF), which are not stocks and should not be pattern-scanned. Indian ISINs distinguish them: `INE…` is equity, `INF…` is a fund unit. | ✅ Filtered on ISIN prefix — 2,643 raw NSE_EQ rows reduce to 2,295 actual equities. |
| Raising worker count would have raised the request rate past any published limit. | ✅ `RateLimiter` token bucket is shared across workers, so `--rate` caps throughput independently of `--workers`. |

## Found during Phase 2 implementation

| Finding | Status |
|---|---|
| **§3.3 Step 5 was not gating anything.** The step was computed and stored but never required, so any *validated* line scored even when price was nowhere near it. **376 of 485 symbols (77%) "qualified"** on a live Nifty 500 run. | ✅ Step 5 now filters. Qualifying dropped to 138 (28%), which is what a high-probability filter should look like. |
| **The backtest equity curve compounded parallel trades sequentially.** 660 trades across 48 symbols were compounded as one capital stack recycled through 660 consecutive bets, turning a positive expectancy into a fabricated –98% drawdown — it reported "+64% net" beside an equity curve ending at 38. | ✅ Rewritten as a daily mark-to-market portfolio, equal-weighted across concurrently open positions. Same run now reads +13.6% vs +28.9% buy-and-hold. |
| `"Net Return %"` summed 660 individual trade percentages — not a return anyone could have earned. | ✅ Replaced with `Portfolio Return %` taken from the equity curve. |
| Sharpe returned `nan` on a flat curve: a single return gives `std()` = NaN under ddof=1, and `NaN == 0` is False so the guard missed it. | ✅ Explicit `pd.isna` guard. |
| `pd.NA` in the displacement scanner upcast a float series to object dtype; the later `float()` raised on **all 500 symbols**. | ✅ `np.nan` instead, with a regression test. |
| **EMA seeded on `x[0]`, not the SMA.** pandas' `ewm(adjust=False)` starts its recursion at the first value; charting packages seed on the SMA of the first window. §2.4 explicitly wants terminal-matching numbers. | ✅ `_seeded_ewm` plants the SMA seed and lets pandas' C loop run, so it stays vectorised. |

## Found during the Upstox integration

| Finding | Status |
|---|---|
| **Upstox candle endpoints are unauthenticated.** `/user/profile` returns 401 with an expired token, but `/v2/historical-candle/...`, `/v3/historical-candle/.../minutes/{n}/...` and `/v3/historical-candle/intraday/...` all return 200 with **no header at all**. | ✅ [ADR 0002](adr/0002-upstox-needs-no-auth.md). Provider chain flipped to Upstox → yfinance everywhere. |
| **Intraday history caps at 31 calendar days.** 60 days returns `UDAPI1148 Invalid date range` for 1/5/15-minute intervals; 31 is accepted. At 15 min that is ~600 bars. | ✅ Clamped in the provider, with a test. Deeper intraday history would need paging. |
| **v2 has no working minute path.** `/v2/historical-candle/{key}/minute/{to}/{from}` returns 400. | ✅ Intraday is v3-only (`/minutes/{n}/`). |
| **The current-session endpoint serves today's live candles**, so §2.1's live-session integration has a real source. | ✅ `fetch_current_session()`; intraday fetches append it automatically. |
| The supplied access token was already **20 days expired** (issued 2026-08-03, expired 2026-08-04 03:30 IST). | ℹ️ Irrelevant for scanning. The provider now retries unauthenticated on a 401 rather than failing the scan. |

## Spec changes applied in code

- Feed priority: **Upstox primary, yfinance fallback** everywhere (ADR 0002, restoring §2.1's intent).
- `Universe.ALL_NSE` marked local-only; hosted builds cap at Nifty 500.
- §3.2 ships **8 of 10** fundamentals, each provenance-tagged (B3).
- §3.3 Step 5 is a filter; Step 12 scores are normalised (C1, C2).
- §3.7 uses the standard 3-candle fair-value gap (C4).
