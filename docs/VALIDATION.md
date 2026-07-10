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
- Current-method shadow rows freeze the strategy's qualification and intended size before portfolio-level guardrails. They can build research evidence while broker/execution eligibility remains blocked, avoiding a validation deadlock.
- Current long-option evidence must include the directional buyer-edge fields used by the current pricing gate. Older absolute-anomaly rows remain telemetry-only.
- Shares and futures use observed closes. Futures may use a labeled ETF proxy only when the continuous-contract history is unavailable.
- Options first look for an exact, non-interpolated target-date trade bar in `data/robinhood_option_history_snapshot.json`. Matching bars are labeled `broker_market_observed`. When no exact bar is cached, the evaluator uses a labeled Black-Scholes mark with entry IV held constant. Both paths apply the configured slippage assumption, and neither path is evidence that Optedge received a fill.
- The report shows executed and current-method shadow samples separately. The research headline remains untrusted below 100 shadow outcomes or 10 distinct entry days.

`python scripts/refresh_robinhood_option_history.py --status` writes a bounded exact-contract request queue, an agent-safe read-only prompt, and a coverage report. A connected Codex/Robinhood session can resolve those requests with `get_option_instruments` and `get_option_historicals`. Interpolated gap-fill bars are rejected. When new exact bars arrive, previously modeled outcomes are upgraded in place on the next fixed-horizon refresh.

The output includes Wilson 95% intervals for win rate, after-cost returns, profit factor, normalized drawdown, SPY/QQQ excess returns, outcome-quality counts, pending horizons, exclusions, and factor IC by horizon.

## Sample Size

Closed signal count below 500 is marked as too small. This does not mean the system is broken. It means the research evidence is not mature enough to trust sizing without human review.

## Missing Buckets

Some old position rows may not include newer fields such as `spread_pct`. The report shows those as unavailable rather than inventing values.
