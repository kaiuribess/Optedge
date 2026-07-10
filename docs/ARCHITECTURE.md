# Architecture

Optedge is a local research cockpit built around a scan, fuse, size, log, and validate loop.

## Flow

1. Universe construction combines configured tickers with live WSB discovery.
2. Engines collect independent factors: options mispricing, sentiment, news, fundamentals, earnings, insider activity, Congress, futures, macro, flows, and technicals.
3. Fusion combines factor rows into ranked options, shares, value plays, and futures.
4. Sizing applies EV, fractional Kelly, slippage, sector caps, and setup-quality multipliers.
5. Tracking writes signal logs and asset-specific position state.
6. Every scan reprices and reanalyzes exits for open options, shares, and futures.
7. Pre-guard qualification freezes the current strategy decision for shadow validation while portfolio guardrails can still block execution.
8. Fixed-horizon validation settles independent 1/3/5/10/20-session outcomes without using partial sessions or repeated intraday theses.
9. Lifecycle validation reads logged signals and closed/open positions to produce a formal research report.

## Main Modules

- `run.py`: compatibility entry point for the current live scanner.
- `engines/`: individual data/factor collectors.
- `fusion/`: cross-factor ranking and watchlist generation.
- `backtest/`: sizing, fixed-horizon and current-mark forward tests, read-only option-history cache/upgrade logic, position tracking, calibration, drawdown controls.
- `dashboard/`: local HTML cockpit rendering.
- `reports/`: formal validation reports and research artifacts.
- `risk/`: research safety guardrails.
- `scripts/robinhood_research_bridge.py`: bounded read-only request/cache bridge for interactive equity and exact-option research.
- `archive.py`: safe generated-artifact archive/reset helper.

## Asset Lifecycles

Options use option-chain pricing, theoretical value, IV/skew/DTE fields, stop/target/expiry exits, and dynamic exit review after hard exits.

Fixed-horizon option validation prefers exact Robinhood regular-session trade bars supplied through the read-only Codex connector cache. The local process emits bounded contract requests but has no broker credentials or order capability. Missing exact target-date bars fall back to the labeled constant-entry-IV proxy.

Interactive lookup uses a separate read-only Robinhood research cache. Search queues a symbol or exact option request, a connected Codex review collects only market-research fields, and the cockpit merges the result with local factors. Broker quote timestamps control freshness, and a material local-mid versus broker-mark mismatch is an action blocker. This cache never represents a position, order, or fill.

Shares use equity prices and non-option factors such as sentiment, news, fundamentals, insider activity, analyst data, macro context, technicals, sector flow, and filings. They do not require strikes, expiries, Greeks, or option-chain fields.

Futures use futures scores, macro context, momentum, volatility, range position, point-value sizing, micro-contract preference, ATR-like stops, and direction-aware long/short exits.

State files are intentionally plain JSON:

- `data/open_positions.json` and `data/closed_positions.json`
- `data/open_share_positions.json` and `data/closed_share_positions.json`
- `data/open_futures_positions.json` and `data/closed_futures_positions.json`
- `data/exit_reviews.jsonl`
- `data/exit_policy.json`

## Archive Reset

`python archive.py` moves generated run artifacts into `archive/run_YYYYMMDD_HHMMSS/` while preserving `data/` and `logs/` subfolders. It does not touch source code, config files, docs, tests, engines, backtest modules, fusion, dashboard, or `.github`.

Use `python archive.py --dry-run` to preview moves. Use `python archive.py --keep-learned` to preserve learned/adaptive files such as `data/model_weights.json`, `data/exit_policy.json`, `data/exit_policy_history.jsonl`, and `data/exit_reviews.jsonl`.

## Refactor Direction

The long-term app shape is:

```text
optedge/
  cli.py
  orchestrator.py
  engine_registry.py
  modes/
    scan.py
    forward.py
    backtest.py
    loop.py
```

The current release adds the research/reporting layer first because it improves trust without destabilizing the live scanner.
