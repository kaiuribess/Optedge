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
12. Trade Desk compares at most three freshness-gated exact setups with the No Trade baseline, without treating raw cross-asset scores as comparable. Its first screen reads local artifacts only: a fresh saved Swing Climate may be reused, otherwise a visibly unavailable, defensive climate fallback applies until the operator explicitly refreshes deeper research.
13. The manual-review gate requires an exact fresh actionable option or share candidate. An option must appear exactly once with identical canonical content in both fresh inert cycle and queue artifacts; the gate binds their digests, row digest/fingerprint, execution profile, and DTE policy. The normal swing lane retains its `90+` DTE default. Explicit `leaps_swing` uses `365-900` DTE, the isolated `option_leaps_swing` evidence lane, 3/5/10-session reviews, and a 20-session maximum planned hold. The gate then applies the durable v2 same-account drawdown multiplier, calculates deterministic stop/capital risk, recomputes same-account broker exposure, and assembles blockers from evidence, freshness, permissions, conservative buying power, cross-asset overlap, total-open headroom, reconciliation, and order state.
14. One user-triggered ten-ticker research scan starts with Optedge-ranked underlyings, selects at most one exact 90-900 DTE contract per ticker through the existing free provider stack, and checks each identity and quote through bounded official Robinhood reads. It reports missing contracts and keeps live market quality separate from model-estimated after-cost edge. Its rows are never promoted to the execution queue automatically.
15. The separate queued-contract verifier preserves normal queue order and inspects only `orders[0:10]` through bounded official Robinhood chain, instrument, and quote reads. It proves exact contract uniqueness and standard-contract metadata, enforces the frozen limit, 120-second quote freshness, profile spread/liquidity/delta rules, and binds every result to unchanged cycle and queue digests. It cannot clear a blocked local entry gate, preview an order, or search for a replacement contract after seeing a live quote.
16. If every gate passes, the local app emits one expiring review packet with one deterministic packet-scoped UUIDv5 `ref_id`. The direct official Robinhood MCP connection can perform allowlisted reads, one broker review, and one fixed confirmed option placement after browser OAuth; a separately connected Robinhood task remains a manual packet fallback. Manual placement consumes a 60-second in-memory confirmation and re-reads state before the one broker call.
17. The separate automation controller defaults to `off`. Every cycle first reads account capacity, runs the normal full Optedge pipeline, then re-syncs the account so long research cannot leave broker state stale. `approval_required` analyzes exact holdings, checks up to ten exact contracts, and returns choices without placement. `automatic` requires two risk acknowledgements and an exact arming phrase, expires after eight hours, disarms on restart/disconnect, runs locally without Codex, permits one concurrent option position, requires an exact normal-Optedge lifecycle exit decision before a close, previews every entry or exit, records an attempted candidate before placement, and never retries it automatically.

## Main Modules

- `run.py`: compatibility entry point for the current live scanner.
- `engines/`: individual data/factor collectors.
- `fusion/`: cross-factor ranking and watchlist generation.
- `backtest/`: sizing, fixed-horizon and current-mark forward tests, Edge Lab, read-only option-history cache/upgrade logic, position tracking, calibration, drawdown controls.
- `backtest/leaps_edge.py`: profile-isolated LEAPS evidence across the 5-, 10-, and 20-session horizons.
- `dashboard/`: standalone scan-dashboard rendering.
- `reports/`: formal validation reports and research artifacts.
- `risk/`: research guardrails, pure hash-chained account drawdown evaluation, same-account portfolio exposure controls, and review-only trade-plan construction.
- `scripts/local_cockpit.py`: loopback-only Trade Desk server, local APIs, exact candidate handoff, broker reconciliation, and evidence presentation.
- `optedge/leaps_swing.py`: canonical LEAPS contract, quote, liquidity, risk, and management policy.
- `optedge/robinhood_mcp.py`: official endpoint, browser OAuth, operating-system-vault storage, sanitized status, and strict read/review tool allowlists.
- `optedge/robinhood_connection.py`: bounded synchronous cockpit bridge to one private asynchronous MCP connection; no poller, retry loop, or generic dispatcher, with one fixed confirmed-option placement method.
- `optedge/robinhood_option_execution.py`: exact-account portfolio analysis plus single-option review, confirmation, entry, and narrowly gated long-option exit execution.
- `optedge/robinhood_automation.py`: opt-in local approval/automatic policy, session arming, daily limits, market window, candidate de-duplication, and audit state.
- `optedge/robinhood_finalist.py`: fail-closed exact option resolver for both the research-only ten-ticker comparison and the short-lived queued-candidate live-quote gate; only the latter is cryptographically bound to the unchanged normal Optedge queue and cycle.
- `optedge/robinhood_snapshot_sync.py`: one explicit complete account-read transaction with bounded cursor proofs and redacted-only atomic persistence.
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
Complete broker read bundle -> normalized snapshot + OS-state equity ledger |
                              |                                      |
                              +--------------> Trade Desk <----------+
                                                  |
                                                  | expiring packet
                                                  v
Browser OAuth -> OS credential vault -> official Robinhood MCP connection
                                                  |
                                                  | one-shot exact finalist market reads
                                                  v
                            short-lived digest-bound finalist check
                                                  |
                                                  | separate complete account read
                                                  v
                              complete in-memory account bundle
                                                  |
                                                  | redact + atomic replace
                                                  v
                              snapshot + pseudonymous equity ledger
                                                  |
                                                  | review + explicit confirmation
                                                  v
                                      one fixed option order boundary
```

The local process never receives a Robinhood password, cookie, MFA code, or API key. Its OAuth token and dynamic client registration are stored only in the operating-system credential vault and are loaded for the bounded official MCP session; there is no plaintext file or environment-variable fallback. Large Windows OAuth envelopes are stored as SHA-256-verified, generation-bound Credential Manager chunks committed through a vault manifest so the platform's single-entry size limit cannot force weaker storage. The client exposes allowlisted reads and reviews plus one fixed confirmed-option placement boundary, never a generic dispatcher. Read bundles, normalized snapshots, and previews remain local evidence artifacts; only a successful broker placement response is recorded as an order response, not proof of a fill.

The Trade Desk binds only to loopback, rejects unknown Host headers, and protects state-changing local requests with a per-launch same-origin token. The OAuth callback additionally requires an exact loopback URI and one-time state validation. The broker packet is short-lived, single-order, limit-only, and invalid if reviewed fields change. Its UUIDv5 `ref_id` is deterministic audit context; it is not broker authority. Generic placement, batches, concurrent positions, market orders, and automatic broker retries are prohibited. The only unattended path is the explicitly armed local controller described above; it stays behind the fixed previewed-option boundary and daily/session/market-window limits.

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

Options use option-chain pricing, theoretical value, IV/skew/DTE fields, stop/target/expiry exits, and dynamic exit review after hard exits. The normal swing execution profile keeps the existing `90+` DTE default. The separately selected `leaps_swing` profile uses `365-900` DTE contracts but a short thesis lifecycle: reviews after 3, 5, and 10 sessions and a maximum planned hold of 20 sessions.

An option candidate handed to the Trade Desk retains symbol, call/put side, strike, expiration, underlying type, quote source, candidate fingerprint, execution profile, evidence lane, and profile-policy version. It must occur exactly once in both a no-more-than-45-minute-old cycle and queue, the two canonical rows must match, and the attestation freezes the cycle, queue, and row SHA-256 digests plus the row fingerprint. DTE is recomputed from expiry and the cycle's UTC date. Normal swing requires at least 90 days. LEAPS requires the exact `leaps_swing` profile and `option_leaps_swing` lane, `365-900` DTE, an execution-ready assessment, and empty hard/data blockers. Both artifacts preserve their no-execution controls. An incomplete option cannot silently become a share plan or borrow evidence from another profile.

Live option review also requires exactly one active buy-to-open tradable instrument, then exactly one complete chain whose `id` equals `instrument.chain_id`. Instrument and chain symbols must equal the planned nonnumeric underlying, the chain multiplier must be `100`, `cash_component` must be `null`, and `underlying_instruments` must contain that exact equity. Missing or ambiguous proof blocks; a `100` multiplier alone is insufficient. Live spread caps are hard ceilings: `1%` for shares, `15%` for normal swing options, and `10%` for `leaps_swing`, with stricter candidate limits preserved.

Fixed-horizon option validation prefers exact Robinhood regular-session trade bars supplied through the direct allowlisted read client or a manual connector cache. Missing exact target-date bars fall back to the labeled constant-entry-IV proxy. Both paths subtract the greater of the configured option-cost floor and the recorded entry spread when present; missing spread coverage remains a blocker for live option evidence. LEAPS adds a profile-specific gate that requires every 5-, 10-, and 20-session cohort to be entirely broker-market-observed and independently pass its after-cost, uncertainty, benchmark, stability, and coverage requirements.

Interactive lookup uses either an explicit direct read or the separate read-only Robinhood research cache. Search queues a symbol or exact option request, the selected read path collects only market-research fields, and the cockpit merges the result with local factors. Broker quote timestamps control freshness, and a material local-mid versus broker-mark mismatch is an action blocker. The cache never represents a position, order, or fill.

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

Refactors should keep command routing inside `optedge/`, preserve the compatibility launchers, and move one bounded responsibility at a time without weakening evidence lineage, the loopback boundary, or the fixed previewed-option broker handoff.
