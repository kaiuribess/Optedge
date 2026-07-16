<!-- Purpose: Define strategy-validation and evidence standards. -->

# Validation

Run:

```bash
python reports/validation_report.py
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

The report reads local signal logs from `logs/` and position state from the option, share, and futures JSON files in `data/`.

By default, the primary metrics use the current unarchived experiment. An actual `archive.py` reset establishes the boundary; ordinary rewrites of `config_runtime.py`, `data/model_weights.json`, or `data/predictor_coefs.json` do not. If no archive/reset boundary exists, Optedge includes all unarchived outcomes instead of silently hiding results. Use `--since` when you need an explicit custom cutoff.

Use `--all-time` when you intentionally want the full historical view:

```bash
python reports/validation_report.py --all-time
```

## Metrics

The report includes total logged signals, open versus closed positions, win rate, average return, median return, profit factor, max drawdown, calls versus puts performance, DTE buckets, spread buckets, confidence buckets, after-slippage performance, SPY and QQQ comparison when market data is reachable, and a random baseline.

It also separates options, shares, and futures:

- Open and closed count by asset.
- Win rate, average return, median return, profit factor, and max drawdown by asset.
- Exit reason breakdown by asset.
- Dynamic exit action counts from `data/exit_reviews.jsonl`.
- Dynamic versus hard-exit effectiveness where closed samples exist.
- Futures `pnl_points` and `pnl_dollars` when available.
- Current exit policy and whether learned exits are active per asset.
- Execution-eligible closures, excluded non-executable rows, excluded churn, and distinct closed-entry days by asset.

Dynamic/self-learning exits should be judged only after enough closed outcomes exist. Early reports will usually warn that sample size is too small.

Performance metrics retain every closed recommendation, including same-scan exits, so poor lifecycle behavior is not hidden. Headline swing evidence and exit-policy learning use a stricter executable subset: `Watch`/`Skip`, explicitly non-actionable, guard-blocked, zero-size, same-scan dynamic exits, and duplicate episodes are excluded. Learned thresholds fall back to defaults when the policy is stale or the executable sample is under the activation minimum.

The research guard uses the executable swing sample's after-slippage metrics for entry readiness. Raw all-closure metrics remain alongside it for auditability. A large shadow-recommendation history therefore cannot override weak drawdown, win rate, or sample size in trades that were actually sized and eligible to open.

Factor IC is also calculated from executable swing outcomes. Each factor is labeled `supportive`, `adverse`, `weak`, or `insufficient_history`; a directional label requires at least 100 executable outcomes across 10 distinct entry days. Raw all-closure factor IC remains in `validation_summary.json` for comparison, while `factor_ic_summary.json` contains the cleaner executable-swing view used by the dashboard.

## Fixed-Session Evidence

The lifecycle report answers whether tracked recommendations eventually hit an exit. The fixed-horizon lane answers whether the entry signal had edge after exactly 1, 3, 5, 10, or 20 completed market sessions.

- Intraday repeats collapse to the first thesis for each asset, ticker, direction, and entry day.
- A horizon remains pending until the required completed session exists; the current partial session is never scored.
- `Watch`, `Skip`, guard-blocked, zero-size, and legacy-unverified rows cannot enter executed metrics.
- New signals are stamped with the exact provenance schema, strategy version, fixed-horizon methodology version, stable policy digest, model-trust schema and status, active predictor SHA-256 identity, active runtime-weight SHA-256 identity, option-adaptation status, execution profile, strategy evidence lane, and experiment ID. Those fields carry into every scored or excluded outcome. The experiment identity itself binds the strategy, policy, and active-model identities.
- `fixed_horizon_summary.json` records an outcome-set digest and mature-resolution coverage by asset, horizon, and evidence lane. A digest mismatch, duplicate evidence key, excluded mature outcome, or unresolved mature outcome fails closed in Edge Lab.
- Current-method shadow rows freeze the strategy's qualification and intended size before portfolio-level guardrails. They can build research evidence while broker/execution eligibility remains blocked, avoiding a validation deadlock.
- Current long-option evidence must include the directional buyer-edge fields used by the current pricing gate. Older absolute-anomaly rows remain telemetry-only.
- Shares and futures use observed closes. Futures may use a labeled ETF proxy only when the continuous-contract history is unavailable.
- Options first look for an exact, non-interpolated target-date trade bar in `data/robinhood_option_history_snapshot.json`. Matching bars are labeled `broker_market_observed`. When no exact bar is cached, the evaluator uses a labeled Black-Scholes mark with entry IV held constant. Both paths record cost as the greater of the configured option-cost floor and the signal's entry spread when that spread is available. A missing entry spread uses a clearly labeled floor fallback rather than an invented spread, and neither valuation path is evidence that Optedge received a fill.
- The report shows executed and current-method shadow samples separately. The base research headline remains untrusted below 100 shadow outcomes or 10 distinct entry days. That reporting floor is less strict than Edge Lab's live-review gate and must not be read as live-capital approval.

`python scripts/refresh_robinhood_option_history.py --status` writes a bounded exact-contract request queue, an agent-safe read-only prompt, and a coverage report. A user-triggered direct Robinhood read can resolve those requests with allowlisted instrument and historical-bar tools; a separately connected Codex/Robinhood session remains a manual read-only fallback. Interpolated gap-fill bars are rejected. When new exact bars arrive, previously modeled outcomes are upgraded in place on the next fixed-horizon refresh.

The explicit `leaps_swing` profile never borrows the ordinary option lane merely because a contract has a long expiration. It requires outcomes stamped with both `execution_profile=leaps_swing` and `strategy_evidence_lane=option_leaps_swing`. Its 5-, 10-, and 20-session evidence slices must each independently satisfy the full provenance, resolution, coverage, after-cost, uncertainty, benchmark, and stability gates using 100% exact broker-market-observed outcomes.

The output includes Wilson 95% intervals for win rate, after-cost returns, profit factor, normalized drawdown, SPY/QQQ excess returns, outcome-quality counts, pending horizons, exclusions, and factor IC by horizon.

## Edge Lab Overlay

`backtest/edge_lab.py` turns fixed-session outcomes into the evidence panel used by the Trade Desk. It does not replace the full validation report; it applies a narrower, stricter manual-review gate to the planned asset.

- Current-method executable, current-method shadow, and legacy research rows stay in separate lanes.
- Only current-method executable evidence can validate an asset for manual review.
- The outcome parquet and fixed-horizon summary must be policy-bound, digest-matched, and no more than 96 hours old. The summary's report timestamp must also be within 96 hours, which safely spans a normal market weekend without accepting indefinitely stale evidence.
- Confidence uses a deterministic circular moving-block bootstrap over independent entry-day averages. Block length is at least the holding horizon, and the gate requires at least 30 effective horizon-length blocks.
- The baseline sample floors remain at least 200 independent outcomes across at least 30 distinct entry days; these do not override the stricter effective-block rule.
- At the default ten-session horizon, 30 effective blocks require about 300 distinct entry days. Repeated same-day signals and raw row count cannot replace that time coverage.
- Mature resolution coverage must be 100%, with no excluded or pending gated outcome.
- Entry time, raw return, after-cost return, nonnegative slippage, SPY excess, and raw-minus-slippage reconciliation must each have 100% coverage. Missing costs or benchmarks are never treated as zero.
- The option lane additionally requires 100% finite nonnegative entry-spread coverage and 100% proof that recorded cost is at least the entry spread. A labeled cost-floor fallback keeps the outcome visible but cannot clear those option checks when its entry spread is missing.
- Nominal after-cost results are stressed at 1.5 and 2 times the recorded slippage assumption.
- The first and recent chronological halves must both remain positive.
- Option performance gates use only exact `broker_market_observed` outcomes. Modeled proxies remain research-only and cannot improve any live performance requirement; the observed cohort must cover at least 50% of the selected current option cohort.
- Legacy and current-method shadow evidence remain visible for audit and research but can never authorize live-capital review.
- A share result cannot authorize an option, and an option result cannot authorize another asset.

After the source, provenance, resolution, coverage, lane, and effective-block gates pass, the performance requirements are a positive after-cost mean, positive 90% horizon-length moving-block lower bound, profit factor of at least 1.15, positive SPY excess, positive doubled-cost mean, and positive first/recent halves. See [Edge Lab Methodology](EDGE_LAB.md) for the complete status logic and limitations.

## Calibration Basis

Calibration prefers `pnl_pct_after_slippage` and reports its return basis. Asset-specific prediction columns are used: option rows use the option prediction and share/futures rows use the stock-direction prediction. Mixed-asset calibration selects the appropriate prediction for each row instead of comparing every asset against one generic field.

Calibration describes alignment between stored predictions and stored outcomes. It does not prove fill quality or live profitability.

## Adaptive Model Promotion Firewall

Normal scans are inference-only. They never fit, save, or immediately consume new predictor coefficients or fusion weights. Research fitting APIs return `shadow_untrusted` candidates in memory; saving a research shadow does not make it executable.

A predictor can affect ranking only when its artifact uses the trusted-champion schema and passes every fail-closed check: one explicit asset family, fixed holding horizon, after-cost target basis, complete coefficient coverage, at least three purged expanding-window out-of-sample folds, at least 500 OOS predictions across at least 30 entry days and 30 effective horizon blocks, positive after-cost and doubled-cost results, a positive challenger-versus-champion lower bound, source evidence no more than 14 days old, policy and outcome digests, and an intact content digest. Legacy `predictor_coefs.json` files load as zero coefficients.

The stock-return predictor accepts share targets only. Option and futures lifecycle returns cannot train it. Option adaptation remains disabled until a separate option-specific path has direct broker-observed targets and passes the same out-of-sample promotion standard.

Runtime fusion weights are global, so their champion schema is stricter: every configured factor must be present, weights must remain normalized with no factor above 30%, and separate share and option out-of-sample attestations are required. Each asset attestation needs the purged-fold, prediction-count, effective-block, positive champion-delta, and positive doubled-cost checks; the option attestation must use direct broker-observed targets. Legacy `config_runtime.py` files and research-shadow weight files fail closed to source-controlled weights.

The fixed-horizon policy records that ordinary-scan training is disabled and option adaptation is disabled pending direct broker-observed out-of-sample evidence. Current-lane eligibility requires the exact model-trust state and both active-model digests frozen at signal time. A later champion, weight set, or policy change starts a different experiment identity; it cannot claim the previous identity's outcomes.

## Diagnostic Quarantine

`python run.py --backtest` is retained as a historical factor-IC diagnostic. It compares current factor scores with returns that have already been realized, which is look-ahead. Its records are labeled:

- `basis=current_scores_vs_already_realized_returns`
- `evidence_status=diagnostic_only_lookahead`
- `eligible_for_model_promotion=false`

The diagnostic may help find coding mistakes or generate hypotheses, but it cannot promote model weights, clear Edge Lab, or support a performance claim. The old variable-age current-mark model-accuracy refit is disabled by default for the same reason: current marks are monitoring telemetry rather than fixed forecast horizons.

## Sample Size

Closed signal count below 500 is marked as too small. This does not mean the system is broken. It means the research evidence is not mature enough to trust sizing without human review.

## Missing Buckets

Some old position rows may not include newer fields such as `spread_pct`. The report shows those as unavailable rather than inventing values.
