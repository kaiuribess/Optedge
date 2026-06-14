# Optedge - Multi-Asset Market Research Cockpit

![CI](https://github.com/kaiuribess/Optedge/actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-research-orange)

Optedge is a local research cockpit for options, shares, futures, and value ideas. It combines options-chain analytics, market data, retail attention, news, fundamentals, filings, macro context, sizing rules, lifecycle tracking, validation reports, and guardrails into one local dashboard.

Optedge is built for research and decision support. It does not place trades, store broker credentials, or promise future returns. Broker connectivity, when enabled, is read-only market data.

## What Optedge Is

- A local research cockpit that runs on your machine.
- A multi-asset signal ranking system for options, shares, futures, and value ideas.
- A dashboard and validation layer for reviewing recommendations, open positions, closed outcomes, and model evidence.
- A lifecycle tracker that stores open and closed recommendations locally.
- A research tool with archive/reset support and safety guardrails.

## What Optedge Is Not

- Not financial advice.
- Not a profit guarantee.
- Not an autonomous execution engine.
- Not a replacement for human review.
- Not production-ready for live capital without strong validation evidence.

## Core Features

- Options mispricing, chain analytics, surface checks, and contract ranking.
- Share and value ranking using the full non-option factor stack.
- Futures ranking using futures trend, macro, risk, volatility, and cross-asset context.
- Multi-factor fusion across sentiment, news, fundamentals, filings, macro, technicals, market structure, and retail attention.
- EV, slippage, fractional Kelly sizing, bankroll caps, and sector concentration controls.
- Multi-asset lifecycle tracking for open and closed recommendations.
- Dynamic exit pressure reviewed every scan.
- Conservative self-learning exit policy that stays inactive until enough evidence exists.
- Validation report, factor IC summary, equity curve, and position aging output.
- Interactive local HTML dashboard.
- Free local cockpit server with symbol lookup across latest scan artifacts.
- Safe archive/reset tool for generated research artifacts.
- Research guardrails for sample size, drawdown, spreads, stale models, and data health.

## Signal Coverage

Optedge is intentionally broad. Each scan can combine many independent signals, but the goal is not to trust every factor blindly. The goal is to make the evidence visible, size ideas conservatively, and validate which signals are actually helping.

| Area | Coverage |
|---|---|
| Options pricing, surface, and flow | Black-Scholes, CRR binomial, Bjerksund-Stensland, CBOE theoretical price, ensemble weights, IV rank, IV premium, skew, surface anomalies, DTE, delta, open interest, bid/ask spread, unusual options activity, put/call ratios, and contract-level call/put ranking |
| Sentiment, social, and retail attention | WSB, r/options, StockTwits-style social signals, ApeWisdom/Twitter-style attention, FinBERT, VADER, keyword/degen-aware scoring, Google Trends, and attention momentum |
| News, earnings, and catalysts | Recent headlines, headline sentiment, news momentum, earnings calendar, days-to-earnings, whisper signals, IV-crush risk, FDA/biotech catalysts, and event proximity |
| Fundamentals and value | Market cap, valuation, quality, P/E, FCF yield, earnings yield, EV/EBITDA, SEC companyfacts balance-sheet context, margin/ROIC proxies, deep value buckets, Graham-style score, and Magic Formula-style quality/value composite |
| Insider, filings, and Congress | SEC Form 4 parsing, recent SEC filing lookup, insider buys/sells, officer/director weighting, Finnhub MSPR aggregate insider sentiment, Form 144 planned sales, buybacks, 13F context, cluster-buy detection, and STOCK Act disclosures |
| Macro, rates, credit, and volatility | VIX, SPY momentum, Treasury yields, curve slope, CPI, unemployment, Fed funds, HY/IG credit spreads, keyless FRED CSV fallback, and volatility regime context |
| Futures, commodities, and crypto | Equity index, rates, energy, metals, agriculture, crypto futures, trend/range/volatility features, CFTC CoT, EIA energy data, USDA WASDE, and Hyperliquid-style crypto context |
| Market structure and technicals | Dark-pool/FINRA short-volume proxy, short interest, squeeze setups, sector ETF flows, trend, momentum, RSI, MACD, relative strength, 52-week range position, and volatility regime |
| Risk, portfolio, and telemetry | Sector concentration, portfolio Greeks, drawdown breaker, research guard report, engine health, empty-engine diagnostics, and engine latency telemetry |

## Multi-Asset Trade Lifecycle

Every scan can add new qualified recommendations, reprice existing open recommendations, review exits, and update local open/closed state files.

### Options

- Uses the full research stack plus option-chain data and option-specific pricing math.
- Prices, ranks, sizes, tracks, reprices, and closes recommendations.
- Applies hard exits for stop, target, and expiry.
- Runs dynamic exit review every scan after hard risk exits.

### Shares

- Uses the full non-option research stack for equity ideas.
- Tracks equity entries, current prices, suggested sizing, stops, targets, and dynamic exit pressure.
- Does not depend on option-chain fields.

### Futures

- Uses the full non-option research stack plus futures, macro, trend, volatility, and risk context.
- Uses ATR-like stop/target logic, point-value risk sizing, and micro futures preference when available.
- Reviews futures score reversals, volatility changes, macro context, and reprice failures every scan.

Shares and futures do not use option-specific fields such as strike, expiry, DTE, IV, delta, Black-Scholes, CRR, BJS, CBOE theoretical price, or option mispricing. All assets remain research recommendations only. Optedge does not submit orders.

## How A Run Works

1. Builds a universe from configured option/share lists, prior tracked names, and WSB trending discovery.
2. Filters the universe so slower engines focus on liquid, relevant, or attention-heavy names.
3. Runs live-data engines concurrently across options, news, filings, fundamentals, sentiment, macro, futures, technicals, and market-structure signals.
4. Prices option contracts with multiple models and logs model predictions for later scoring.
5. Scores social and headline text with local sentiment models when available.
6. Updates forward-test evidence and model weights when enough recent data exists.
7. Fuses factor scores into ranked calls, puts, share ideas, value plays, and futures setups.
8. Applies slippage, spread checks, fractional Kelly sizing, bankroll caps, sector caps, earnings-risk adjustments, and guardrails.
9. Adds qualified recommendations to local open-position files.
10. Reprices open options, shares, and futures.
11. Applies hard exits, dynamic exit review, and conservative learned exit policy when evidence thresholds are met.
12. Writes the dashboard, watchlist, signal logs, lifecycle state, telemetry, and validation outputs.

## Validation Status

Generate the validation report with:

```bash
python run.py --validation-report
```

Outputs:

- `data/validation_report.html`
- `data/validation_summary.json`
- `data/equity_curve.png`
- `data/factor_ic_summary.json`
- `data/position_aging_summary.json`

Validation uses `current_model` scope by default so current-era results are separated from stale older results. Open positions are counted from the current open-position state files, while closed-position metrics only become meaningful after enough recommendations close.

Early reports are expected to show small-sample warnings. Learned exits remain inactive until minimum evidence thresholds are met. Negative or uncorrelated forward results should be treated seriously; this project is a research system, not proof of alpha.

Use all-time validation only when you intentionally want older history included:

```bash
python run.py --validation-all-time
```

See [docs/VALIDATION.md](docs/VALIDATION.md) for details.

## Install

Windows:

```powershell
install.bat
```

Manual setup:

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python setup_check.py
```

Python `3.11` or `3.12` is recommended.

Optional live/broker option-chain source:

```powershell
$env:OPTEDGE_TRADIER_TOKEN="your-production-tradier-token"
```

If no Tradier token is set, Optedge stays on the free chain stack: CBOE delayed quotes, NASDAQ chains, bounded Yahoo options JSON, then yfinance fallback.

## Run

Single scan:

```bash
python run.py
```

Aggressive research mode with a custom bankroll:

```bash
python run.py --aggressive --bankroll 25000
```

Loop every 30 minutes:

```bash
python run.py --aggressive --bankroll 25000 --loop 30
```

Loop every 30 minutes without opening a browser:

```bash
python run.py --aggressive --bankroll 25000 --loop 30 --no-open
```

Loop mode sleeps after each run completes. It is not exact wall-clock scheduling.

Faster loop when SEC insider parsing is slow:

```bash
python run.py --aggressive --bankroll 25000 --loop 30 --fast-insider
```

Turbo loop using RAM cache, batched GPU FinBERT when CUDA is available, and faster insider parsing:

```bash
python run.py --aggressive --bankroll 25000 --loop 30 --turbo --no-open
```

`--turbo` does not place trades. It keeps the normal engine stack, enables the in-process RAM cache, raises FinBERT batch size, and switches insider parsing to the faster count-only mode.

Forward test logged signals:

```bash
python run.py --forward
```

Historical factor IC backtest:

```bash
python run.py --backtest
```

Heston pricing stability check:

```bash
python run.py --heston-stability
```

Instant local lookup from the latest scan artifacts:

```bash
python run.py --lookup NVDA
```

## Dashboard

Each scan writes a local dashboard to `data/dashboard_*.html` and opens it in the browser by default unless `--no-open` is used.

The dashboard includes:

- Macro regime and run statistics.
- Live analytics and open-position P&L charts.
- Factor IC and open-position aging charts.
- Calls, puts, shares, value, and futures cards.
- Search, sort, ready/watch filters, asset filters, compact mode, and expandable sections.
- Engine telemetry, rolling engine health, and empty-engine diagnostics.
- TradingView watchlist export.

Generated dashboards are ignored by Git so local research output stays private.

## Local Cockpit

Run a small local browser cockpit without paid services or extra dashboard hosting:

```bash
python run.py --cockpit
```

The cockpit opens at `http://127.0.0.1:8765` by default and reads local files from `data/`. It gives you:

- Instant symbol lookup across latest option, share, value, futures, and open-position artifacts.
- Focused scan launcher: type a ticker, company name, or option idea and click **Run focused scan**.
- Full/quick focused-scan modes, optional bankroll override, and aggressive sizing toggle.
- Open option/share/futures counts.
- Quick links to the latest dashboard, validation report, validation JSON, equity curve, and external paper-order export.
- A browser UI that does not rerun engines until you choose to run a new scan.

Use another port or keep it from opening a browser:

```bash
python run.py --cockpit --port 8777 --no-open
```

For a ticker that is not in the latest artifacts, run a focused scan first:

```bash
python run.py --universe NVDA --no-open
```

The cockpit run button does this for you in the background. Company-name resolution uses a free Yahoo search endpoint where available, so `Nvidia` can resolve to `NVDA`; direct tickers always work. Option-style requests such as `AAPL 20260618 C 200` are stored with the focused scan job so the cockpit remembers the contract you wanted checked while the scanner researches the underlying. Completed jobs link to their own generated dashboard and expose a log tail for review.

## Archive / Reset

`archive.py` is a safe reset button for generated run data. It moves files into `archive/run_YYYYMMDD_HHMMSS/`, preserves subfolder structure, and does not delete source code.

Archive generated run data:

```bash
python archive.py
```

Preview first:

```bash
python archive.py --dry-run
```

Archive/reset while keeping learned adaptive files:

```bash
python archive.py --keep-learned
```

Default archive mode moves learned/adaptive files too, which is useful for a fully clean experiment reset. `--keep-learned` preserves:

- `data/model_weights.json`
- `data/exit_policy.json`
- `data/exit_policy_history.jsonl`
- `data/exit_reviews.jsonl`

Archive/reset does not move source code, docs, tests, config, requirements, or GitHub workflow files.

## Validation Report

The validation report is the main proof layer for the research loop. It reports:

- Total signals.
- Closed versus open positions by asset.
- Win rate, average return, median return, profit factor, and max drawdown.
- Calls versus puts performance.
- DTE, spread, and confidence bucket performance.
- Factor IC and open-position age buckets.
- Dynamic exit actions and learned exit policy status.
- Performance after estimated slippage.
- SPY and QQQ benchmark comparison when market data is reachable.
- Random baseline comparison.
- Sample-size warnings.

See [docs/VALIDATION.md](docs/VALIDATION.md) for details.

## Research Guardrails

Optedge includes a research safety layer in `risk/research_guard.py`.

It warns or blocks trust when:

- The closed-signal sample is under 500.
- Max drawdown is worse than `-20%`.
- Spread bucket performance is negative.
- An option recommendation has a spread above `15%`.
- Win rate is below a simple breakeven threshold.
- Model updates appear stale.
- Key data sources return no data.
- Key engines have weak rolling health.

Research Guard is supposed to be conservative. A warning is not cosmetic; it means the output needs more evidence or more human skepticism. If validation is weak, negative, sparse, or uncorrelated, Optedge should not be treated as reliable.

## Data Sources

Optedge uses free or locally configured sources where possible, including:

- Options chains and price history.
- Cboe daily market statistics and total/equity/index put-call ratio CSVs for delayed options sentiment context.
- FRED public graph CSV macro stress context for credit, rates, labor, inflation, growth, and liquidity.
- Nasdaq Trader symbol directory for broader official ticker/ETF search and universe hygiene.
- Reddit and retail-attention feeds.
- SEC EDGAR filings.
- News and earnings feeds.
- Macro, rates, credit, energy, agriculture, volatility, and futures context.
- Optional FinBERT sentiment scoring when the local environment supports it.
- Optional Tradier production token for broker/live option chains; free CBOE/NASDAQ/yfinance fallbacks remain the default.

Some sources may rate-limit, return partial data, or require keys in your local `keys.py`. Private keys are ignored by Git.

See [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).

## Project Layout

```text
optedge/
  cli.py
  orchestrator.py
  engine_registry.py
  modes/

engines/       factor and data engines
fusion/        ranking and attribution
backtest/      sizing, lifecycle tracking, exits, forward tests, calibration
dashboard/     interactive HTML dashboard
reports/       validation report generation
risk/          research guardrails
docs/          architecture, validation, risk, limitations
tests/         direct-run test files
```

`run.py` is intentionally tiny and delegates to `optedge.cli`.

## Tests

The CI workflow runs direct test files so import behavior matches simple local commands. Current direct-run tests:

```bash
python tests/test_pricing.py
python tests/test_research_guard.py
python tests/test_archive.py
python tests/test_exit_rules.py
python tests/test_exit_learning.py
python tests/test_futures_sizing.py
python tests/test_option_positions.py
python tests/test_share_positions.py
python tests/test_futures_positions.py
python tests/test_validation_report.py
python tests/test_external_paper_track.py
python tests/test_robinhood_agentic_queue.py
python tests/test_symbol_resolver.py
python tests/test_research_jobs.py
python tests/test_lookup_symbol.py
python tests/test_sec_companyfacts.py
python tests/test_news.py
python tests/test_local_cockpit.py
python tests/test_fred_public.py
python tests/test_treasury_yield_curve.py
python tests/test_data_provider_stooq.py
python tests/test_performance_cache.py
python tests/test_finbert_batching.py
python tests/test_chain_provider_tradier.py
```

If `pytest` and `ruff` are installed:

```bash
pytest
ruff check . --select E9,F63,F7,F82
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Validation](docs/VALIDATION.md)
- [Data Sources](docs/DATA_SOURCES.md)
- [Free Data Roadmap](docs/FREE_DATA_ROADMAP.md)
- [Risk Model](docs/RISK_MODEL.md)
- [Factor Library](docs/FACTOR_LIBRARY.md)
- [Third-Party Forward Testing](docs/THIRD_PARTY_FORWARD_TESTING.md)
- [Limitations](docs/LIMITATIONS.md)

## Limitations

Optedge is a research and decision-support tool, not financial advice and not an autonomous trading system.

Signals require human review. Performance depends on data quality, fills, spreads, slippage, liquidity, regime changes, news shocks, earnings gaps, and sample size.

See [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## License

MIT. See [LICENSE](LICENSE).
