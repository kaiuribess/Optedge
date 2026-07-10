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
- Learning-eligible closures, excluded churn, and distinct closed-entry days by asset.

Dynamic/self-learning exits should be judged only after enough closed outcomes exist. Early reports will usually warn that sample size is too small.

Performance metrics retain every closed recommendation, including same-scan exits, so poor lifecycle behavior is not hidden. Exit-policy learning uses a stricter subset: same-scan dynamic exits and duplicate episodes are excluded, and learned thresholds fall back to defaults when the policy is stale or the independent sample is under the activation minimum.

The research guard uses the independent swing sample's after-slippage metrics for entry readiness. Raw all-closure metrics remain alongside it for auditability. A clean raw history therefore cannot override weak drawdown, win rate, or sample size in the independent swing evidence.

Factor IC is also calculated from independent swing outcomes. Each factor is labeled `supportive`, `adverse`, `weak`, or `insufficient_history`; a directional label requires at least 100 eligible outcomes across 10 distinct entry days. Raw all-closure factor IC remains in `validation_summary.json` for comparison, while `factor_ic_summary.json` contains the cleaner independent-swing view used by the dashboard.

## Sample Size

Closed signal count below 500 is marked as too small. This does not mean the system is broken. It means the research evidence is not mature enough to trust sizing without human review.

## Missing Buckets

Some old position rows may not include newer fields such as `spread_pct`. The report shows those as unavailable rather than inventing values.
