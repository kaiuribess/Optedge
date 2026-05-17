# Optedge — Multi-Factor Options & Market Research Cockpit

![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-research-orange)

Optedge is a local research cockpit for options, shares, futures, and value ideas. It combines live market data, options-chain analytics, retail attention, fundamentals, filings, macro context, sizing rules, and validation reports into one dashboard.

It is built for research and decision support. It is not an autonomous trading system, and it does not place trades.

## What It Does

- Ranks long calls, long puts, share buys, value plays, and futures ideas.
- Scores signals across options mispricing, IV, skew, sentiment, fundamentals, insider activity, filings, news, earnings, macro, Congress, retail attention, technicals, futures, and market structure.
- Applies EV, estimated slippage, fractional Kelly, per-trade caps, sector caps, and exit triggers.
- Tracks open and closed recommendations locally.
- Generates an interactive HTML dashboard.
- Produces a formal validation report with win rate, returns, drawdown, profit factor, buckets, benchmarks, and sample-size warnings.

## Signal Coverage

Optedge is intentionally broad. Each run can combine dozens of independent signals into one research board:

| Area | What Optedge Looks At |
|---|---|
| Options pricing | Black-Scholes, CRR binomial, Bjerksund-Stensland, CBOE theoretical price, ensemble weights, fair value gaps, net edge after spread |
| Options surface | IV rank, IV premium, skew, surface anomalies, DTE, delta, open interest, bid/ask spread, liquidity filters |
| Options flow | Unusual options activity, put/call ratios, contract-level ranking, call/put separation |
| Sentiment | WSB, r/options, StockTwits-style social signals, ApeWisdom/Twitter-style attention, FinBERT, VADER, keyword/degen-aware text scoring |
| News | Recent headlines, 24-hour news count, headline sentiment, news momentum |
| Earnings | Earnings calendar, days-to-earnings, whisper signals, IV-crush risk |
| Fundamentals | Market cap, valuation, quality, P/E, FCF yield, earnings yield, EV/EBITDA, margin/ROIC proxies |
| Value investing | Deep value buckets, Graham-style score, Magic Formula-style quality/value composite |
| Insider activity | SEC Form 4 parsing, insider buys/sells, officer/director weighting, Finnhub MSPR aggregate insider sentiment |
| Filings and flows | Form 144 planned sales, buyback announcements, 13F-style institutional context, cluster-buy detection |
| Congress | STOCK Act transaction disclosures from House/Senate reports, net buying/selling by ticker |
| Macro | VIX, SPY momentum, Treasury yields, curve slope, CPI, unemployment, Fed funds, HY/IG credit spreads |
| Futures and commodities | Equity index, rates, energy, metals, agriculture, crypto futures, trend/range/volatility features |
| Public macro datasets | CFTC CoT, EIA energy data, USDA WASDE, FRED yield/credit data when configured |
| Market structure | Dark-pool/FINRA short-volume proxy, short interest, squeeze setups, sector ETF flows |
| Technicals | Trend, momentum, RSI, MACD, relative strength, 52-week range position, volatility regime |
| Special catalysts | FDA/biotech catalyst detection, earnings-window flags, event proximity |
| Crypto attention | Hyperliquid open-interest style signals for crypto-linked names and futures context |
| Portfolio context | Sector concentration, portfolio Greeks, drawdown breaker, engine latency telemetry |

The goal is not to blindly trust every factor. The goal is to make competing evidence visible, size ideas conservatively, and then validate which signals actually work.

## How A Run Works

Each scan follows the same research pipeline:

1. Builds a universe from configured option/share lists, prior tracked names, and WSB trending discovery.
2. Filters the universe so the slower engines focus on names with enough liquidity, attention, or prior relevance.
3. Runs live-data engines concurrently across options, news, filings, fundamentals, sentiment, macro, futures, technicals, and market-structure signals.
4. Prices option contracts with multiple models, compares them to market prices, and logs model predictions for later scoring.
5. Re-scores headlines and social text with local sentiment models when available.
6. Updates forward-test evidence and model weights from logged signals when enough recent data exists.
7. Fuses factor scores into ranked calls, puts, share ideas, value plays, and futures setups.
8. Applies slippage, spread checks, fractional Kelly sizing, bankroll caps, sector concentration caps, earnings-risk adjustments, and research guardrails.
9. Tracks open recommendations and closes them when exit or aging rules are reached.
10. Writes a dashboard, TradingView watchlist, signal logs, model telemetry, and optional validation report.

## Current Validation Status

Run:

```bash
python run.py --validation-report
```

This writes:

- `data/validation_report.html`
- `data/validation_summary.json`
- `data/equity_curve.png`
- `data/factor_ic_summary.json`
- `data/position_aging_summary.json`

Primary validation uses the current model era by default so older stale data is not treated as proof for the latest weights. Use `--validation-all-time` only when you intentionally want the full historical view.

| Metric | Value |
|---|---:|
| Closed signals | TBD |
| Win rate | TBD |
| Avg return | TBD |
| Max drawdown | TBD |
| Profit factor | TBD |
| SPY benchmark | TBD |
| QQQ benchmark | TBD |

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

Faster loop when SEC insider parsing is slow:

```bash
python run.py --aggressive --bankroll 25000 --loop 30 --fast-insider
```

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

## Dashboard

Each scan writes a local dashboard to `data/dashboard_*.html` and opens it in the browser by default.

The dashboard includes:

- Macro regime and run statistics.
- Live analytics and open-position P&L charts.
- Factor IC and open-position aging charts.
- Calls, puts, shares, value, and futures cards.
- Search, sort, ready/watch filters, asset filters, compact mode, and expandable sections.
- Engine telemetry, rolling engine health, and empty-engine diagnostics.
- TradingView watchlist export.

Generated dashboards are ignored by Git so local research output stays private.

## Archive / Reset

Archive generated run data without deleting anything:

```bash
python archive.py
```

Preview first:

```bash
python archive.py --dry-run
```

Keep learned/adaptive files while archiving normal run history:

```bash
python archive.py --keep-learned
```

## Validation Report

The validation report is the main proof layer for the research loop. It reports:

- Total signals.
- Closed versus open positions.
- Win rate, average return, median return, profit factor, and max drawdown.
- Calls versus puts performance.
- DTE, spread, and confidence bucket performance.
- Factor IC and open-position age buckets.
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

These guardrails are meant to slow down overconfidence, not replace human review.

## Data Sources

Optedge uses free or locally configured sources where possible, including:

- Options chains and price history.
- Reddit and retail-attention feeds.
- SEC EDGAR filings.
- News and earnings feeds.
- Macro, rates, credit, energy, agriculture, volatility, and futures context.
- Optional FinBERT sentiment scoring when the local environment supports it.

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
backtest/      sizing, tracking, forward tests, calibration
dashboard/     interactive HTML dashboard
reports/       validation report generation
risk/          research guardrails
docs/          architecture, validation, risk, limitations
tests/         pricing and guardrail tests
```

`run.py` is intentionally tiny and delegates to `optedge.cli`.

## Tests

Run the pricing test file directly:

```bash
python tests/test_pricing.py
```

Run the research guard tests:

```bash
python tests/test_research_guard.py
```

If `pytest` and `ruff` are installed:

```bash
pytest
ruff check .
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Validation](docs/VALIDATION.md)
- [Data Sources](docs/DATA_SOURCES.md)
- [Risk Model](docs/RISK_MODEL.md)
- [Factor Library](docs/FACTOR_LIBRARY.md)
- [Limitations](docs/LIMITATIONS.md)

## Limitations

Optedge is a research and decision-support tool, not financial advice and not an autonomous trading system.

Signals require human review. Performance depends on data quality, fills, spreads, slippage, liquidity, regime changes, news shocks, earnings gaps, and sample size.

See [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## License

MIT. See [LICENSE](LICENSE).
