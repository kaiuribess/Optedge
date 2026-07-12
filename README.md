# Optedge - Multi-Asset Market Research Cockpit

![CI](https://github.com/kaiuribess/Optedge/actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.11--3.13-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-research-orange)

Optedge is a local research cockpit for options, shares, futures, and value ideas. It combines options-chain analytics, market data, retail attention, news, fundamentals, filings, macro context, sizing rules, lifecycle tracking, validation reports, and guardrails into one local dashboard.

Optedge is built for research and decision support. The local app does not place trades, store broker credentials, or promise future returns. When a Codex/Robinhood MCP connector is available, broker checks and any order workflow stay outside the local repo and remain approval-gated.

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
| Options pricing, surface, and flow | Black-Scholes, CRR binomial, Bjerksund-Stensland, CBOE theoretical price, ensemble weights, IV rank, IV premium, directional buyer/seller edge after spread, skew, surface anomalies, DTE, delta, open interest, bid/ask spread, unusual options activity, put/call ratios, and contract-level call/put ranking |
| Sentiment, social, and retail attention | WSB, r/options, StockTwits-style social signals, ApeWisdom/Twitter-style attention, FinBERT, VADER, keyword/degen-aware scoring, Google Trends, and attention momentum |
| News, earnings, and catalysts | Recent headlines, headline sentiment, news momentum, earnings calendar, days-to-earnings, whisper signals, IV-crush risk, FDA/biotech catalysts, and event proximity |
| Fundamentals and value | Market cap, valuation, quality, P/E, FCF yield, earnings yield, EV/EBITDA, SEC companyfacts balance-sheet context, margin/ROIC proxies, deep value buckets, Graham-style score, and Magic Formula-style quality/value composite |
| Insider, filings, and Congress | SEC Form 4 parsing, recent SEC filing lookup, insider buys/sells, officer/director weighting, Finnhub MSPR aggregate insider sentiment, Form 144 planned sales, buybacks, 13F context, cluster-buy detection, and STOCK Act disclosures |
| Macro, rates, credit, and volatility | VIX, SPY momentum, Treasury yields, curve slope, CPI, unemployment, Fed funds, HY/IG credit spreads, keyless FRED CSV fallback, and volatility regime context |
| Futures, commodities, and crypto | Equity index, rates, energy, metals, agriculture, crypto futures, trend/range/volatility features, CFTC CoT, EIA energy data, USDA WASDE, and Hyperliquid-style crypto context |
| Market structure and technicals | Dark-pool/FINRA short-volume proxy, SEC fails-to-deliver context, short interest, squeeze setups, sector ETF flows, trend, momentum, RSI, MACD, relative strength, 52-week range position, and volatility regime |
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
- `data/fixed_horizon_outcomes.parquet`
- `data/fixed_horizon_summary.json`
- `data/robinhood_option_history_requests.json`
- `data/robinhood_option_history_coverage.json`

Validation keeps the compatibility label `current_model`, but the default scope means the current unarchived experiment. The latest `archive.py` reset establishes the experiment boundary; ordinary model-weight updates do not hide outcomes. Open positions are always counted from the current open-position state files, while closed-position metrics only become meaningful after enough recommendations close.

Early reports are expected to show small-sample warnings. Fixed-horizon evidence scores one independent thesis per asset, ticker, direction, and entry day after 1, 3, 5, 10, and 20 completed sessions. A shadow row records that the current strategy passed before portfolio-level guardrails; this lets validation accumulate while actual sizing remains blocked. Shares and futures use observed historical closes. Options prefer exact, non-interpolated Robinhood option trade bars from the read-only Codex connector cache, then fall back to a clearly labeled constant-entry-IV model proxy when no exact target-date bar exists. Neither source proves an Optedge fill. Learned exits remain inactive until minimum evidence thresholds are met. Negative or uncorrelated forward results should be treated seriously; this project is a research system, not proof of alpha.

Build or inspect the bounded read-only option-history queue with:

```bash
python scripts/refresh_robinhood_option_history.py --status
```

The local process never receives Robinhood credentials. It writes exact contract requests and a safe agent prompt; a connected Codex/Robinhood session may satisfy those requests using read-only contract and historical-bar tools. The next validation refresh automatically upgrades matching proxy outcomes to broker-observed bars.

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

Manual setup in PowerShell:

```powershell
py -3.12 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python setup_check.py
```

The requirements file installs this checkout in editable mode. Runtime and
development dependency declarations live in `pyproject.toml` so installers,
CI, and the `optedge` command share one source of truth.

Optedge is intentionally source-first: use the installer or editable setup
above so private runtime state stays in this checkout's ignored `data/`
directory. A system-wide, non-editable install is not a supported trading-data
layout.

Python `3.11` through `3.13` is supported; Python `3.12` is recommended.

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

After installation, the equivalent console entry point is also available:

```bash
optedge --help
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

All `--loop` examples above are local research refreshes. They do not schedule a Codex task, send recurring Codex messages, or initiate the Robinhood review/placement flow.

Forward test logged signals:

```bash
python run.py --forward
```

The command shows mixed-age current marks as monitoring telemetry and separately writes leakage-resistant fixed-session outcomes. Only current-method, independently sampled, executable rows can enter the fixed-horizon headline.

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

When the authenticated Robinhood MCP connector is available, lookup also writes a bounded read-only refresh request. A connected Codex review can attach Robinhood's current equity quote, official close, fundamentals, earnings timing, recent price history, and the exact option contract's mark, spread, Greeks, volume, open interest, tradability, and history. Broker quote timestamps drive freshness labels. Large differences between a saved local option mid and the broker mark block the swing verdict until the local chain is refreshed. No account number, credential, position, order, or fill is stored in this research cache.

Inspect the read-only lookup cache and queue with:

```bash
python scripts/robinhood_research_bridge.py --status
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

The cockpit opens at `http://127.0.0.1:8765` by default and reads local files from `data/`. It refuses non-loopback/LAN bindings, rejects unknown Host headers, and protects every state-changing request with a per-launch same-origin token. It gives you:

- A decision-first **Trade Desk** as the default screen: market regime, evidence quality, validation/risk gate, and Robinhood review readiness.
- A stop-based trade planner for whole shares and long calls/puts, with risk budget, allocation cap, slippage, planned stop loss, maximum capital-loss reference, reward/risk, and breakeven win rate.
- One short-lived manual Robinhood review packet you can copy or download. The packet requires live broker review and exact confirmation, and explicitly forbids batches, loops, scheduled tasks, repeated orders, and automatic retries.
- Instant symbol lookup across latest option, share, value, futures, and open-position artifacts.
- Read-only Robinhood ticker and exact-option context when the connector cache has a matching record, with explicit quote age and source labels.
- Focused scan launcher: type a ticker, company name, or option idea and click **Run focused scan**.
- Full/quick focused-scan modes, optional bankroll override, and aggressive sizing toggle.
- Open option/share/futures counts.
- Quick links to the latest dashboard, validation report, validation JSON, option-history and broker-research queues, equity curve, and external paper-order export.
- A browser UI that does not rerun engines until you choose to run a new scan.

### Manual Robinhood Review

Optedge uses Robinhood's official Agentic Trading connection as an external broker boundary. The local dashboard never asks for or stores a Robinhood password, token, cookie, MFA code, or API key, and it has no broker-order endpoint. Review packets contain an account placeholder; the user chooses an eligible account inside the connected Robinhood task.

1. Set up an eligible account using [Robinhood's Agentic Trading overview](https://robinhood.com/us/en/support/articles/agentic-trading-overview/).
2. Connect the Robinhood Trading integration in Codex.
3. Refresh the read-only broker snapshot and normalize it with `python scripts/normalize_robinhood_broker_snapshot.py`. Use the account-scoped `optedge_robinhood_mcp_read_bundle_v2` capture format in [docs/THIRD_PARTY_FORWARD_TESTING.md](docs/THIRD_PARTY_FORWARD_TESTING.md): preserve the exact decoded `data` envelopes and collection lists; include portfolio, equity-position, option-position, equity-order, and option-order reads for every returned account under the exact request `account_number`; keep those account-scoped reads out of the top level; finish every paginated page with an explicit `data.next` key and the final value set to `null`; preserve each follow-up request cursor when the preceding `next` URL exposes one; and join every open option `option_id` to `get_option_instruments`. Missing/wrong-shaped sections, malformed collection rows, incomplete or unlinked pagination, unscoped reads, missing option instruments, or a missing source timestamp are blocked rather than guessed. Manual review accepts only `optedge_robinhood_broker_snapshot_v1` normalized from this v2 bundle; legacy snapshots remain display-only.
4. For an option, build a fresh research queue with `python scripts/export_robinhood_agentic_queue.py --account-budget 500`; the exact contract must remain in the current manual-review candidate set and must explicitly identify an equity/ETF underlying as `underlying_type=equity`.
5. In **Trade Desk**, load a current manual candidate or enter a share plan, verify the account-equity/risk/allocation assumptions and proposed entry, stop, and target, then click **Calculate plan**. The gate requires one same active account to satisfy portfolio value, permissions, conservative buying power, risk, and allocation checks; it never mixes capacity across accounts.
6. If the gate passes, copy the Robinhood review request into one connected task before its 10-minute expiry.
7. The connected task must refresh that exact account, positions, working orders, exact instrument, tradability, and live quote. The live instrument's `underlying_type` must exactly match the packet. It recomputes risk against live portfolio value, uses the smaller of explicit buying power and unleveraged buying power, requires positive bid/ask values no older than 120 seconds, enforces the packet's numeric spread cap, and never raises the packet limit. It then calls the Robinhood review tool, shows the complete preview and alerts, and asks you to confirm that exact order.
8. Placement is allowed only after that exact confirmation and only once with unchanged reviewed fields. A submitted order is not treated as filled until Robinhood reports its broker state.

The broker handoff is manual and on-demand. Optedge does not create a recurring Codex task or automatic trade loop. The legacy Robinhood queue and local auto-paper script are research/paper-only and cannot authorize a broker review or placement.

Current packet support is intentionally narrow: long share/ETF buys and standard `100x`-multiplier, single-leg long calls or puts on equity/ETF underlyings, using limit good-for-day orders during regular hours. Index options (including `^` symbols and known roots such as SPX, NDX, RUT, and VIX), missing/non-equity `underlying_type`, short shares, short options, spreads, adjusted option deliverables, market orders, futures, and crypto are blocked. Every nonterminal broker option order must contain exactly one valid object leg to establish exact identity; multi-leg or malformed-leg orders block review instead of having extra legs discarded. Existing option positions or working open orders in the same symbol and long-call/long-put direction block a new entry even when strike or expiry differs. An equity review needs an active, funded, agentic-accessible account with explicit portfolio value. An option review needs one and the same account to be active, agentic-accessible, funded, and approved for options level 2 or 3; permissions or capacity split across accounts do not qualify. Live spreads are capped at `1%` for shares and at the smaller of the candidate cap or the `15%` option hard cap.

V2 broker readiness uses only a positive `portfolio.total_value` plus both explicit `buying_power` and `unleveraged_buying_power`; it does not substitute equity aliases or cash. Reconciliation assigns each account a stable pseudonymous key and compares signed position type and aggregate quantity per account and exact option contract, so same-nickname accounts, long/short differences, and quantity differences cannot be mistaken for a match.

The entry packet does not place a stop or target order. Those values are planning references only. For a long option, the maximum capital-loss reference is the full debit, and the full debit must fit both the risk budget and allocation cap. For long shares, the planner shows both stop-based loss and full entry notional; a stop is not guaranteed to limit a gap loss.

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
- Independent 1/3/5/10/20-session outcomes with 95% win-rate intervals and SPY/QQQ excess returns.
- Explicit outcome quality labels that keep observed stock/futures closes and exact Robinhood option bars separate from modeled option proxies.

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

- Options chains and price history, with optional read-only Robinhood/Codex caches for exact contract bars and interactive exact-contract quote checks.
- Cboe daily market statistics and total/equity/index put-call ratio CSVs for delayed options sentiment context.
- FRED public graph CSV macro stress context for credit, rates, labor, inflation, growth, and liquidity.
- Nasdaq Trader symbol directory for broader official ticker/ETF search and universe hygiene.
- Nasdaq public stock screener for delayed small-cap mover discovery in Swing Scout.
- Reddit and retail-attention feeds.
- SEC EDGAR filings.
- SEC fails-to-deliver files for delayed settlement-pressure context.
- News and earnings feeds.
- Macro, rates, credit, energy, agriculture, volatility, and futures context.
- Optional FinBERT sentiment scoring when the local environment supports it.
- Optional Tradier production token for broker/live option chains; free CBOE/NASDAQ/Yahoo/yfinance fallbacks remain the default.

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

Install the development extra and run the same full-suite commands used by CI:

```powershell
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check . --select E9,F63,F7,F82,F401,F841,B033,B007,F541
```

Individual `tests/test_*.py` files can still be run directly while developing, but a clean full `python -m pytest` run is the release check.

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
