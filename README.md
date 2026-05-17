# Optedge — Multi-Factor Options & Market Research Cockpit

![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-research-orange)

Optedge is a local research cockpit for options, shares, futures, and value ideas. It combines live market data, options-chain analytics, retail attention, fundamentals, filings, macro context, sizing rules, and validation reports into one dashboard.

It is built for research and decision support. It is not an autonomous trading system, and it does not place trades.

## What It Does

- Ranks long calls, long puts, share buys, value plays, and futures ideas.
- Scores signals across mispricing, IV, skew, sentiment, fundamentals, insider activity, news, earnings, macro, Congress, social attention, technicals, and market structure.
- Applies EV, estimated slippage, fractional Kelly, per-trade caps, sector caps, and exit triggers.
- Tracks open and closed recommendations locally.
- Generates an interactive HTML dashboard.
- Produces a formal validation report with win rate, returns, drawdown, profit factor, buckets, benchmarks, and sample-size warnings.

## Current Validation Status

Run:

```bash
python run.py --validation-report
```

This writes:

- `data/validation_report.html`
- `data/validation_summary.json`
- `data/equity_curve.png`

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

## Dashboard

Each scan writes a local dashboard to `data/dashboard_*.html` and opens it in the browser by default.

The dashboard includes:

- Macro regime and run statistics.
- Live analytics and open-position P&L charts.
- Calls, puts, shares, value, and futures cards.
- Search, sort, ready/watch filters, asset filters, compact mode, and expandable sections.
- Engine telemetry and empty-engine diagnostics.
- TradingView watchlist export.

Generated dashboards are ignored by Git so local research output stays private.

## Validation Report

The validation report is the main proof layer for the research loop. It reports:

- Total signals.
- Closed versus open positions.
- Win rate, average return, median return, profit factor, and max drawdown.
- Calls versus puts performance.
- DTE, spread, and confidence bucket performance.
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
