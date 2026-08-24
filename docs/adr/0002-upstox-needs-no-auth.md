# ADR 0002 — Upstox candle endpoints need no authentication

**Status:** Accepted · **Date:** 2026-08-24 · **Amends:** [ADR 0001](0001-feed-strategy.md)

## Context

ADR 0001 moved the EOD batch job to yfinance on the reasoning that Upstox
requires a daily OAuth token, which no unattended cron can hold. That premise
was **wrong for market data**, and it was never tested — it was inferred from
the spec's §2.2 description of the token lifecycle.

Testing an expired token against the live API on 2026-08-24 produced a result
that only makes sense one way:

| Endpoint | Auth sent | Result |
|---|---|---|
| `/v2/user/profile` | expired token | **401** |
| `/v2/historical-candle/{key}/day/{to}/{from}` | expired token | **200 + data** |
| same | *no header at all* | **200 + data** |
| same | `Bearer totally-invalid-token` | **200 + data** |
| `/v3/historical-candle/{key}/minutes/15/{to}/{from}` | no header | **200 + data** |
| `/v3/historical-candle/intraday/{key}/minutes/15` | no header | **200 + today's live session** |

The candle endpoints do not authenticate. Only account endpoints do.

## Decision

Upstox becomes the primary feed **everywhere** — local, hosted, and CI — with
yfinance as the fallback. This is what spec §2.1 asked for originally.

```
UpstoxProvider  ->  YFinanceProvider
```

`AppSettings.allow_upstox` no longer keys off `CI`; it defaults to True and is
disabled only by an explicit `INFINITY_DISABLE_UPSTOX=1`.

An access token is still sent when one is configured, because account endpoints
need it. If Upstox rejects it with 401, the provider retries once *without* the
header rather than failing a scan over a credential it does not need.

## Consequences

**Good**

- The official exchange-grade source is now primary, as the spec intended.
  No more relying on unofficial Yahoo scraping for the numbers that matter.
- **Spec §3.3 Step 7 works for the first time.** Intraday history was
  previously unavailable, so every symbol scored `15-Min Status: No Data`. On a
  live Nifty 50 run it now reads 19 Confirmed / 26 Not Confirmed, and the
  scoring denominator moves from 90 to 100.
- **Spec §2.1's live-session integration works.** The intraday endpoint serves
  the current session, so `ensure_today_candle` has a real source.
- ADR 0001's §6.2.2 concern is unchanged and its yfinance fallback still stands.

**Bad / constraints**

- **Intraday requests cap at 31 calendar days.** Measured: 31 days is accepted
  for 1/5/15-minute intervals, 60 returns `UDAPI1148 Invalid date range`. At
  15 minutes that is ~600 bars, past the 200 Step 7 needs, so a single window
  suffices — but deeper intraday history would require paging.
- **v2 has no working minute path.** `/v2/historical-candle/{key}/minute/...`
  returns 400; intraday is v3-only (`/minutes/{n}/`).
- Upstox is one request per symbol, where yfinance can batch. Roughly 0.8 s per
  symbol serially; the existing `RateLimiter` and worker pool keep a 500-symbol
  scan near the yfinance timing.
- This behaviour is **undocumented and could change without notice**. The
  yfinance fallback is what makes that survivable, and is now load-bearing
  rather than decorative.

## Lesson

ADR 0001 reasoned from the specification instead of from the API. The spec
described the token lifecycle accurately and I extrapolated an access-control
rule it never actually stated. One `curl` would have caught it before it shaped
the architecture.
