<!-- Purpose: Plan improvements to free market-data coverage. -->

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

### SEC fails-to-deliver data

Status: implemented in `engines/sec_ftd.py` and used as a small non-option context factor for shares and futures ETF proxies.

Source:
- https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data

Why it helps:
- Adds official no-key settlement-fail context for small-cap and high-short-pressure swing candidates.
- Captures ticker, settlement date, aggregate fail quantity, issuer description, and prior closing price from the SEC pipe-delimited files.
- Helps separate ordinary short-volume noise from names with delayed settlement pressure.
- Keeps the factor conservative and contextual; it is not a standalone entry trigger.

Notes:
- SEC publishes these files with a delay: first-half monthly data near month-end and second-half data around the middle of the next month.
- SEC explicitly notes FTD values are aggregate balances as of a settlement date, not daily fails.
- Fails-to-deliver can happen for multiple reasons and are not proof of abusive short selling or naked shorting.

### FINRA official equity short interest

Status: implemented in `engines/short_interest.py` as an official no-key fallback and amplifier for short/squeeze context.

Source:
- https://www.finra.org/finra-data/browse-catalog/equity-short-interest/files
- https://cdn.finra.org/equity/otcmarket/biweekly/shrtYYYYMMDD.csv

Why it helps:
- Adds official twice-monthly current short position, prior short position, average daily volume, days-to-cover, and settlement date.
- Improves small-cap and squeeze-candidate coverage when yfinance-style short-float fields are missing.
- Keeps short interest separate from daily short volume, which is noisier and not the same thing as open short interest.
- Uses delayed regulatory context as a risk/pressure factor, not as a standalone entry trigger.

Notes:
- FINRA files are delayed and published around short-interest settlement/dissemination cycles.
- The file does not include shares-float, so Optedge uses days-to-cover conservatively when float percentage is unavailable.

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

### Cboe option symbol activity

Status: implemented in `engines/cboe_symbol_data.py` and surfaced in the Robinhood agentic queue as public activity context for candidate options.

Source:
- https://www.cboe.com/us/options/market_statistics/symbol_data/
- https://www.cboe.com/us/options/market_statistics/symbol_data/csv/?mkt=cone
- https://www.cboe.com/us/options/market_statistics/symbol_data/csv/?mkt=opt
- https://www.cboe.com/us/options/market_statistics/symbol_data/csv/?mkt=ctwo
- https://www.cboe.com/us/options/market_statistics/symbol_data/csv/?mkt=exo

Why it helps:
- Adds no-key public contract activity from Cboe, BZX Options, C2 Options, and EDGX Options.
- Gives the Robinhood/Codex review packet a quick "public activity seen / not seen" sanity check before any option is considered.
- Aggregates exact-contract volume, matched/routed activity, and top-of-book context where available.
- Keeps the check conservative: no activity match is not an automatic rejection, and a match is not approval to trade.

Notes:
- This is Cboe venue activity, not consolidated OPRA and not a live execution quote.
- Robinhood must still verify the exact contract, current bid/ask, liquidity, buying power, and news before any order.

### Nasdaq public stock screener

Status: implemented in `engines/nasdaq_screener.py` and surfaced in the local cockpit Swing Scout.

Source:
- https://www.nasdaq.com/market-activity/stocks/screener
- https://api.nasdaq.com/api/screener/stocks

Why it helps:
- Adds a no-key small-cap mover radar for names outside the latest Optedge scan files.
- Captures price, percent change, volume, market cap, sector, and industry context.
- Enriches fresh mover rows with FINRA short-volume pressure when the no-key RegSHO file is reachable.
- Surfaces reviewable mover rows in Swing Scout, Today Review, Command Center, and the Action Queue.
- Keeps these rows as review candidates only; they still need a focused Optedge scan and live quote check before any manual paper/live decision.

### Nasdaq Trader trade halt RSS

Status: implemented in `engines/trading_halts.py` and surfaced in the local cockpit Action Queue for watchlist/open-position symbols.

Source:
- https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts
- https://www.nasdaqtrader.com/Trader.aspx?id=TradeHaltRSS

Why it helps:
- Adds an official no-key halt/pause risk layer for speculative small-cap swing candidates.
- Flags watchlist or open-position symbols that are currently halted or recently resumed before the user reviews new swing actions.
- Keeps halt rows as risk context only; a halt is not a standalone entry or exit signal.
- Uses a one-minute cache by default to respect Nasdaq Trader's RSS refresh-frequency guideline.

### Nasdaq Trader Reg SHO threshold list

Status: implemented in `engines/regsho_threshold.py` and surfaced in the local cockpit Action Queue for watchlist/open-position symbols.

Source:
- https://www.nasdaqtrader.com/trader.aspx?id=regshothreshold
- https://www.nasdaqtrader.com/Trader.aspx?id=RegShoDefs

Why it helps:
- Adds official no-key settlement/mandatory close-out context for small-cap and high-short-pressure names.
- Flags saved or open symbols that appear on Nasdaq's current Reg SHO or Rule 3210 threshold list.
- Uses the official pipe-delimited download file rather than scraping display text.
- Keeps threshold-list inclusion as risk context only; it is not proof of a squeeze, issuer weakness, or trade edge.

### Nasdaq Trader short-sale circuit breaker list

Status: implemented in `engines/short_sale_circuit.py` and surfaced in the local cockpit Action Queue for watchlist/open-position symbols.

Source:
- https://www.nasdaqtrader.com/trader.aspx?id=shortsalecircuitbreaker
- https://www.nasdaqtrader.com/Trader.aspx?id=SSCircuitBreakerdefs

Why it helps:
- Adds official no-key SEC Rule 201 short-sale restriction context for names that dropped enough to trigger the price test.
- Flags saved or open symbols under SSR before the user reviews new small-cap swing actions.
- Uses the official CSV download file rather than scraping display text.
- Keeps SSR as downside-stress context only; it is not a squeeze signal or entry trigger by itself.

## Safe Free Sources Already In The Project

- SEC EDGAR Form 4 insider activity and 13F filings.
- SEC fails-to-deliver settlement-fail context.
- Keyless FRED CSV macro/rates/credit series.
- Official Treasury XML yield-curve feed.
- Cboe market-wide put/call market statistics.
- Nasdaq public stock screener for delayed small-cap mover discovery.
- Nasdaq Trader trade halt RSS for halt/pause risk context.
- Nasdaq Trader Reg SHO threshold list for settlement/close-out risk context.
- Nasdaq Trader short-sale circuit breaker list for SSR/downside-stress context.
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
