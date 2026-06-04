# Free Data Roadmap

Optedge should prefer sources that are free, stable, documented, and allowed for automated research use. This keeps the project useful for a retail user without forcing paid feeds too early.

## Added Now

### SEC EDGAR recent filings

Status: implemented in `scripts/sec_filings.py` and surfaced in ticker lookup.

Source:
- https://www.sec.gov/edgar/sec-api-documentation
- https://data.sec.gov/

Why it helps:
- Shows recent 8-K, 10-Q, 10-K, S-3, S-1, 424B, ownership, and insider forms during ticker lookup.
- Adds filing context without requiring an API key.
- Keeps SEC filings as context first, not an automatic trade signal.

Notes:
- Set `SEC_USER_AGENT` if you want a custom SEC-compliant user agent.
- SEC filings do not replace a fresh Optedge scan; if SEC filings are the only hit, lookup still recommends a focused scan.

## Safe Free Sources Already In The Project

- SEC EDGAR Form 4 insider activity and 13F filings.
- FINRA daily short-volume files.
- CFTC Commitments of Traders reports.
- EIA/WASDE commodity data.
- Reddit/social/retail attention sources.
- Free price/history paths through existing public providers.

## Good Future Adds

- More SEC XBRL `companyfacts` fields for balance-sheet deterioration, margins, dilution, and cash runway.
- SEC offering/dilution classifier for S-1, S-3, 424B2, and 424B5 filings.
- EDGAR RSS/latest filings monitor for watchlist tickers.
- Public economic calendar/event-risk layer using official government releases where available.
- Free source health registry that tracks hit rate, latency, and stale data per source.

## Avoid For Automation

### Cboe delayed quote table scraping

Source checked:
- https://www.cboe.com/delayed_quotes/api/quote_table

Reason:
- The page states that automated downloading/querying of delayed quote-table data is prohibited. Do not build new automated extraction against that quote-table page.

For options coverage, prefer broker-authorized data, user-enabled subscriptions, or sources whose terms explicitly allow programmatic use.
