<!-- Purpose: Explain Optedge architecture and execution boundaries. -->

# Architecture

Optedge is a local research cockpit built around a scan, fuse, size, log, and validate loop.

## Flow

1. Universe construction combines configured tickers with live WSB discovery.
2. Engines collect independent factors: options mispricing, sentiment, news, fundamentals, earnings, insider activity, Congress, futures, macro, flows, and technicals.
3. The model firewall selects either digest-valid promoted champions or deterministic source-controlled defaults; ordinary scans never train or immediately reuse a challenger.
4. Fusion combines factor rows into ranked options, shares, value plays, and futures.
5. Sizing applies EV, fractional Kelly, slippage, sector caps, and setup-quality multipliers.
6. Tracking writes signal logs and asset-specific position state with the active predictor, runtime-weight, policy, methodology, and experiment identities frozen before outcomes exist.
7. Every scan reprices and reanalyzes exits for open options, shares, and futures.
8. Pre-guard qualification freezes the current strategy decision for shadow validation while portfolio guardrails can still block execution.
9. Fixed-horizon validation settles independent 1/3/5/10/20-session outcomes without using partial sessions or repeated intraday theses; option cost is at least the signal-time spread when that spread exists.
10. Edge Lab separates current executable, current shadow, and legacy lanes; it applies entry-day uncertainty, cost stress, time stability, benchmark, spread-coverage, and outcome-quality requirements per asset.
11. Lifecycle validation reads logged signals and closed/open research positions to produce a formal report.
12. Trade Desk compares at most three freshness-gated exact setups with the No Trade baseline, without treating raw cross-asset scores as comparable.
13. The manual-review gate requires an exact fresh actionable option or share candidate. An option must appear exactly once with identical canonical content in both fresh inert cycle and queue artifacts; the gate binds their digests, the row digest/fingerprint, and the canonical 90-DTE floor. It then applies the durable v2 same-account drawdown multiplier, calculates deterministic stop/capital risk, recomputes same-account broker exposure, and assembles blockers from evidence, freshness, permissions, conservative buying power, cross-asset overlap, total-open headroom, reconciliation, and order state. A free-form share plan can be sized locally but cannot produce a broker packet.
14. If every gate passes, the local app emits one expiring review packet with one deterministic packet-scoped UUIDv5 `ref_id`. A connected Robinhood task independently refreshes live state, proves exact option instrument-to-chain identity where applicable, previews the unchanged order, asks for explicit confirmation, then re-reads the exact account, portfolio, positions, orders, quote, instrument, and chain and re-checks packet expiry immediately before its sole place call.

## Main Modules

- `run.py`: compatibility entry point for the current live scanner.
- `engines/`: individual data/factor collectors.
- `fusion/`: cross-factor ranking and watchlist generation.
- `backtest/`: sizing, fixed-horizon and current-mark forward tests, Edge Lab, read-only option-history cache/upgrade logic, position tracking, calibration, drawdown controls.
- `dashboard/`: standalone scan-dashboard rendering.
- `reports/`: formal validation reports and research artifacts.
- `risk/`: research guardrails, pure hash-chained account drawdown evaluation, same-account portfolio exposure controls, and review-only trade-plan construction.
- `scripts/local_cockpit.py`: loopback-only Trade Desk server, local APIs, exact candidate handoff, broker reconciliation, and evidence presentation.
- `scripts/robinhood_research_bridge.py`: bounded read-only request/cache bridge for interactive equity and exact-option research.
- `archive.py`: safe generated-artifact archive/reset helper.

## Trust Boundaries

```text
Free/configured market sources -> local Python process
                                      |
                                      v
                           ignored research/model artifacts
                                      |
                                      v
                         validation + Edge Lab + model firewall --+
                                                                   |
Read-only Robinhood capture -> normalized snapshot + OS-state equity ledger |
                              |                                    |
                              +------------> Trade Desk <----------+
                                                |
                                                | expiring packet
                                                v
                                  Connected Codex/Robinhood task
                                                |
                                                | fresh preview + confirmation
                                                v
                                       Robinhood broker state
```

The local process never receives a Robinhood password, cookie, MFA code, or authenticated token. It cannot place a broker order. Read-only captures and normalized snapshots are local evidence artifacts, not proof that an order was submitted or filled.

The Trade Desk binds only to loopback, rejects unknown Host headers, and protects state-changing local requests with a per-launch same-origin token. The broker packet is short-lived, single-order, limit-only, and invalid if reviewed fields change. Its UUIDv5 `ref_id` is deterministic for the packet so a deliberate retry of the same logical order reuses one identity; it never authorizes an automatic retry. Batches, background schedules, automatic retries, and unattended placement are prohibited.

Adaptive artifacts are a separate trust boundary. Research fitting produces an untrusted shadow; only an explicitly promoted artifact with complete asset-isolated, purged out-of-sample evidence, fresh source and outcome digests, and an intact content digest can affect ranking. Otherwise the scanner uses deterministic source-controlled defaults. Fixed-horizon provenance freezes the exact active predictor and runtime-weight identities, so results cannot cross model boundaries silently.

Research positions, local paper positions, broker-linked lifecycle rows, and normalized broker positions are different domains. Ordinary lifecycle JSON tracks Optedge recommendations and does not prove a holding. Agentic paper files simulate decisions and do not prove a holding. Reconciliation compares normalized broker state only with lifecycle rows carrying explicit broker-linkage evidence; unlinked research and paper rows remain separate informational counts and cannot create a live mismatch.

Manual account identity is also explicit. Schema `optedge_robinhood_account_key_derivation_v1` derives `acct_` plus the first 16 lowercase hexadecimal characters of SHA-256 over UTF-8 `optedge-robinhood-account-v1|<trimmed get_accounts.account_number>`. The normalized snapshot and review packet do not persist the raw number; the private ignored raw capture still contains it because scoped broker reads require it. A `...last4` mask is only a human-readable label and is not unique enough for account joins or selection.

The account-loss interlock and total-open allocation gate are stricter than reconciliation. Interlock policy v2 requires a fresh, intact, single-account equity hash chain with at least two observations spanning at least 18 hours and two New York calendar dates, and its latest observation must exactly match the current normalized snapshot. It scales the `1%` manual-review risk ceiling to `0.5%` at a `5%` high-water drawdown, to `0.25%` at `8%`, and blocks at `10%` or a `3%` New York-session loss. For the real repository data directory, the ledger lives outside the checkout under `OPTEDGE_STATE_DIR` or the per-user OS state directory; custom/test data directories stay local. Atomic file replacement leaves the primary and `.bak` sidecar on the same newest chain after a successful append. A missing primary or required sidecar, rollback, divergence, or lagging sidecar blocks review. Explicit normalization may reseal a validated lagging sidecar after an interrupted write without creating an observation; the system never automatically rebaselines. The allocation gate calculates existing capital at risk directly from fresh normalized positions for exactly one account, adds the proposed order's full share notional or full long-option debit, and compares the sum with `min(planner equity, same-account live total_value) x allocation fraction`. It never substitutes local research or paper rows for broker exposure. Any same-account nonterminal order, same-symbol cross-asset exposure, or ambiguous state blocks the calculation.

## State Domains

| Domain | Examples | What it proves |
|---|---|---|
| Research lifecycle | `data/open_positions.json`, share/futures equivalents | Optedge is tracking a recommendation; not a broker holding. |
| Local paper | `data/agentic_paper_positions.json`, paper decision journal | A simulated/manual paper event; not a broker holding or fill. |
| Broker-linked lifecycle | A lifecycle row with explicit broker scope/identifier fields | Eligible for comparison with normalized broker state; still not authoritative by itself. |
| Normalized broker snapshot | `data/robinhood_broker_snapshot.json` | A timestamped, read-only representation of captured broker state; authoritative only within its freshness and completeness checks. |
| Account-equity ledger | `OPTEDGE_STATE_DIR/account_<digest>.json` or the per-user OS state directory; custom/test data uses `<data_dir>/robinhood_account_equity_ledgers/` | A pseudonymous hash chain with an atomic `.bak` sidecar, used only to reduce or block new-entry risk; not a transaction ledger or broker statement. |
| Trade Desk packet | In-memory expiring review request | One immutable proposal eligible to begin external review; not an order, submission, or fill. |

## Asset Lifecycles

Options use option-chain pricing, theoretical value, IV/skew/DTE fields, stop/target/expiry exits, and dynamic exit review after hard exits.

An option candidate handed to the Trade Desk retains symbol, call/put side, strike, expiration, underlying type, quote source, and candidate fingerprint. It must occur exactly once in both a no-more-than-45-minute-old cycle and queue, the two canonical rows must match, and the attestation freezes the cycle, queue, and row SHA-256 digests plus the row fingerprint. DTE is recomputed from expiry and the cycle's UTC date and must be at least 90. Both artifacts must preserve their explicit no-execution controls. An incomplete option cannot silently become a share plan.

Live option review also requires exactly one active buy-to-open tradable instrument, then exactly one complete chain whose `id` equals `instrument.chain_id`. Instrument and chain symbols must equal the planned nonnumeric underlying, the chain multiplier must be `100`, `cash_component` must be `null`, and `underlying_instruments` must contain that exact equity. Missing or ambiguous proof blocks; a `100` multiplier alone is insufficient. Live spread caps are hard ceilings—`1%` for shares and `15%` for options, with stricter candidate limits preserved.

Fixed-horizon option validation prefers exact Robinhood regular-session trade bars supplied through the read-only Codex connector cache. The local process emits bounded contract requests but has no broker credentials or order capability. Missing exact target-date bars fall back to the labeled constant-entry-IV proxy. Both paths subtract the greater of the configured option-cost floor and the recorded entry spread when present; missing spread coverage remains a blocker for live option evidence.

Interactive lookup uses a separate read-only Robinhood research cache. Search queues a symbol or exact option request, a connected Codex review collects only market-research fields, and the cockpit merges the result with local factors. Broker quote timestamps control freshness, and a material local-mid versus broker-mark mismatch is an action blocker. This cache never represents a position, order, or fill.

Shares use equity prices and non-option factors such as sentiment, news, fundamentals, insider activity, analyst data, macro context, technicals, sector flow, and filings. They do not require strikes, expiries, Greeks, or option-chain fields. Ordinary scans preserve the last history-bar close and its session, then derive deterministic entry/stop/target geometry from that reference. This is not a live quote, so a valid attestation can record `candidate_quote_available=false`; a fresh connected Robinhood bid/ask remains mandatory. A share broker packet must bind to one exact fresh actionable `top_shares_*.parquet` row and its identity, price-reference provenance, geometry, sizing cap, actionability, guard state, digest, and fingerprint. Arbitrary planner input remains useful for local sizing but cannot borrow a different row's evidence or enter manual broker review.

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

Refactors should keep command routing inside `optedge/`, preserve the compatibility launchers, and move one bounded responsibility at a time without weakening evidence lineage, the loopback boundary, or the manual-only broker handoff.
