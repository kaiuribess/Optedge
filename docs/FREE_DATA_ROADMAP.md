# Free Data Roadmap

Optedge should prefer sources that are free, stable, documented, and allowed for automated research use. This keeps the project useful for a retail user without forcing paid feeds too early.

## Added Now

### Keyless FRED macro/rates/credit fallback

Status: implemented in `engines/fred_public.py` and used by macro, credit-spread, and yield-curve engines when `FRED_API_KEY` is not configured or the keyed API path fails.

Source:
- https://fred.stlouisfed.org/graph/fredgraph.csv

Why it helps:
- Keeps CPI, unemployment, Fed funds, Treasury curve, and HY/IG credit spread context available without another account.
- Improves futures, shares, value, and risk-regime scoring because macro context no longer disappears when a FRED key is missing.
- Uses FRED as context and factor evidence, not as a standalone trade trigger.

### Official Treasury yield-curve fallback

Status: implemented in `engines/yield_curve_pca.py` and used when the FRED-backed curve panel is too thin.

Source:
- https://home.treasury.gov/treasury-daily-interest-rate-xml-feed

Why it helps:
- Adds a second official no-key source for the daily Treasury par yield curve.
- Keeps rates/curve context available for financials, REITs, bonds, futures, value, and swing-climate scoring.
- Uses end-of-day official curve data as context, not as an intraday quote feed.

### SEC EDGAR recent filings

Status: implemented in `scripts/sec_filings.py` and surfaced in ticker lookup.

Source:
- https://www.sec.gov/edgar/sec-api-documentation
- https://data.sec.gov/

Why it helps:
- Shows recent 8-K, 10-Q, 10-K, S-3, S-1, 424B, ownership, and insider forms during ticker lookup.
- Adds SEC companyfacts balance-sheet and income/cash-flow context during ticker lookup.
- Surfaces cash, debt, leverage, margin, and official filing-derived risk flags.
- Adds filing context without requiring an API key.
- Keeps SEC filings as context first, not an automatic trade signal.

Notes:
- Set `SEC_USER_AGENT` if you want a custom SEC-compliant user agent.
- SEC filings do not replace a fresh Optedge scan; if SEC filings are the only hit, lookup still recommends a focused scan.
- SEC companyfacts are standardized XBRL facts, so some metrics can be missing or use company-specific reporting choices.

### Cboe put/call market statistics

Status: implemented in `scripts/local_cockpit.py` and surfaced in Market Pulse as options sentiment context.

Source:
- https://www.cboe.com/markets/us/options/market-statistics/daily/
- https://cdn.cboe.com/resources/options/volume_and_call_put_ratios/totalpc.csv
- https://cdn.cboe.com/resources/options/volume_and_call_put_ratios/equitypc.csv
- https://cdn.cboe.com/resources/options/volume_and_call_put_ratios/indexpcarchive.csv

Why it helps:
- Adds market-wide total, equity, and index put/call context without an API key.
- Helps identify defensive hedging, call-demand/complacency, and balanced options sentiment.
- Uses Cboe market statistics as delayed/informational context, not as an execution quote.

### Nasdaq public stock screener

Status: implemented in `engines/nasdaq_screener.py` and surfaced in the local cockpit Swing Scout.

Source:
- https://www.nasdaq.com/market-activity/stocks/screener
- https://api.nasdaq.com/api/screener/stocks

Why it helps:
- Adds a no-key small-cap mover radar for names outside the latest Optedge scan files.
- Captures price, percent change, volume, market cap, sector, and industry context.
- Enriches fresh mover rows with FINRA short-volume pressure when the no-key RegSHO file is reachable.
- Keeps these rows as review candidates only; they still need a focused Optedge scan and live quote check before any manual paper/live decision.

## Safe Free Sources Already In The Project

- SEC EDGAR Form 4 insider activity and 13F filings.
- Keyless FRED CSV macro/rates/credit series.
- Official Treasury XML yield-curve feed.
- Cboe market-wide put/call market statistics.
- Nasdaq public stock screener for delayed small-cap mover discovery.
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
