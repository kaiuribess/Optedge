<!-- Purpose: Explain Optedge architecture and execution boundaries. -->

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
9. Edge Lab separates current executable, current shadow, and legacy lanes; it applies entry-day uncertainty, cost stress, time stability, benchmark, and outcome-quality requirements per asset.
10. Lifecycle validation reads logged signals and closed/open research positions to produce a formal report.
11. Trade Desk preserves exact candidate identity, calculates deterministic stop/capital risk, recomputes same-account broker exposure, and assembles blockers from evidence, freshness, permissions, conservative buying power, total-open headroom, reconciliation, and order state.
12. If every gate passes, the local app emits one expiring review packet. A connected Robinhood task independently refreshes live state, previews the unchanged order, and asks for explicit confirmation outside the local application.

## Main Modules

- `run.py`: compatibility entry point for the current live scanner.
- `engines/`: individual data/factor collectors.
- `fusion/`: cross-factor ranking and watchlist generation.
- `backtest/`: sizing, fixed-horizon and current-mark forward tests, Edge Lab, read-only option-history cache/upgrade logic, position tracking, calibration, drawdown controls.
- `dashboard/`: standalone scan-dashboard rendering.
- `reports/`: formal validation reports and research artifacts.
- `risk/`: research guardrails, pure same-account portfolio exposure controls, and review-only trade-plan construction.
- `scripts/local_cockpit.py`: loopback-only Trade Desk server, local APIs, exact candidate handoff, broker reconciliation, and evidence presentation.
- `scripts/robinhood_research_bridge.py`: bounded read-only request/cache bridge for interactive equity and exact-option research.
- `archive.py`: safe generated-artifact archive/reset helper.

## Trust Boundaries

```text
Free/configured market sources
        |
        v
Local Python process ----> Git-ignored research artifacts
        |                              |
        v                              v
Loopback-only Trade Desk <---- validation + Edge Lab
        |
        | expiring review packet (no credential, no selected account)
        v
Connected Codex/Robinhood task
        |
        | fresh account + instrument + quote + preview + user confirmation
        v
Robinhood broker state
```

The local process never receives a Robinhood password, cookie, MFA code, or authenticated token. It cannot place a broker order. Read-only captures and normalized snapshots are local evidence artifacts, not proof that an order was submitted or filled.

The Trade Desk binds only to loopback, rejects unknown Host headers, and protects state-changing local requests with a per-launch same-origin token. The broker packet is short-lived, single-order, limit-only, and invalid if reviewed fields change. Batches, background schedules, automatic retries, and unattended placement are prohibited.

Research positions, local paper positions, broker-linked lifecycle rows, and normalized broker positions are different domains. Ordinary lifecycle JSON tracks Optedge recommendations and does not prove a holding. Agentic paper files simulate decisions and do not prove a holding. Reconciliation compares normalized broker state only with lifecycle rows carrying explicit broker-linkage evidence; unlinked research and paper rows remain separate informational counts and cannot create a live mismatch.

The total-open allocation gate is stricter than reconciliation. It calculates existing capital at risk directly from fresh normalized positions for exactly one account, adds the proposed order's full share notional or full long-option debit, and compares the sum with `min(planner equity, same-account live total_value) x allocation fraction`. It never substitutes local research or paper rows for broker exposure. Any same-account nonterminal order or ambiguous exposure state blocks the calculation.

## State Domains

| Domain | Examples | What it proves |
|---|---|---|
| Research lifecycle | `data/open_positions.json`, share/futures equivalents | Optedge is tracking a recommendation; not a broker holding. |
| Local paper | `data/agentic_paper_positions.json`, paper decision journal | A simulated/manual paper event; not a broker holding or fill. |
| Broker-linked lifecycle | A lifecycle row with explicit broker scope/identifier fields | Eligible for comparison with normalized broker state; still not authoritative by itself. |
| Normalized broker snapshot | `data/robinhood_broker_snapshot.json` | A timestamped, read-only representation of captured broker state; authoritative only within its freshness and completeness checks. |
| Trade Desk packet | In-memory expiring review request | One immutable proposal eligible to begin external review; not an order, submission, or fill. |

## Asset Lifecycles

Options use option-chain pricing, theoretical value, IV/skew/DTE fields, stop/target/expiry exits, and dynamic exit review after hard exits.

An option candidate handed to the Trade Desk retains symbol, call/put side, strike, expiration, underlying type, quote source, and candidate fingerprint. An incomplete option cannot silently become a share plan.

Fixed-horizon option validation prefers exact Robinhood regular-session trade bars supplied through the read-only Codex connector cache. The local process emits bounded contract requests but has no broker credentials or order capability. Missing exact target-date bars fall back to the labeled constant-entry-IV proxy.

Interactive lookup uses a separate read-only Robinhood research cache. Search queues a symbol or exact option request, a connected Codex review collects only market-research fields, and the cockpit merges the result with local factors. Broker quote timestamps control freshness, and a material local-mid versus broker-mark mismatch is an action blocker. This cache never represents a position, order, or fill.

Shares use equity prices and non-option factors such as sentiment, news, fundamentals, insider activity, analyst data, macro context, technicals, sector flow, and filings. They do not require strikes, expiries, Greeks, or option-chain fields.

Futures use futures scores, macro context, momentum, volatility, range position, point-value sizing, micro-contract preference, ATR-like stops, and direction-aware long/short exits.

Research lifecycle state files are intentionally plain JSON:

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
