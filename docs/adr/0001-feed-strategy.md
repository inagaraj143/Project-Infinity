# ADR 0001 — Split the market-data feeds by session, not by fallback

**Status:** Accepted · **Date:** 2026-08-23 · **Supersedes:** spec §2.1 feed priority

## Context

The spec (§2.1) makes Upstox API v2 the primary feed and yfinance a
failure-only fallback. §6.2.3 simultaneously wants 24/7 hosting on Streamlit
Community Cloud, fed by a scheduled job.

These two requirements cannot both hold:

- Upstox v2 mints an access token through an **interactive OAuth redirect**.
- That token **expires daily**, around 03:30 IST (spec §2.2).
- A GitHub Actions cron has no browser and no human at 03:30 IST.
- Upstox publishes no machine-to-machine credential flow.

So an unattended job can never hold a valid Upstox token. Storing one in
GitHub Secrets does not help — it is dead within 24 hours.

## Decision

Select the feed by **session state**, not by failure:

| Context | Feed | Auth |
|---|---|---|
| EOD batch (GitHub Actions) | yfinance (`SYMBOL.NS`) | none |
| Live intraday, user present | Upstox v2 | user's own daily OAuth |
| Upstox error mid-session | yfinance | none |

`AppSettings.allow_upstox` is False whenever `CI=true`, so the batch job is
structurally incapable of trying Upstox.

## Consequences

**Good**
- 24/7 hosting works with zero manual intervention and zero cost.
- No long-lived broker credential is stored in any CI system.
- The resolver already treats providers as an ordered chain, so this is
  configuration rather than new code.

**Bad**
- EOD data comes from an unofficial Yahoo endpoint. It can change shape without
  notice — we hit exactly this during Phase 0, where yfinance returned
  MultiIndex `(field, ticker)` columns. Mitigated by `clean_dataframe()` and a
  regression test, but the risk is ongoing.
- yfinance is scraping, not a licensed feed. Fine for private use; review
  before making the app broadly public (spec §6.2.2).
- Upstox and Yahoo can disagree on adjustments and volume. `Source` is recorded
  on every snapshot and surfaced as the §4.6 "Fallback Source" tag, and series
  from different sources are never blended in one chart.

## Alternatives rejected

- **Automate Upstox login with TOTP/Selenium in CI** — brittle, likely breaches
  Upstox's terms, and puts full broker credentials in CI.
- **Refresh the token manually each morning** — defeats "24/7 unattended".
- **Drop the hosted build, run local only** — rejected; hosting is a stated
  requirement.
