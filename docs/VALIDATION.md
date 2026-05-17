# Validation

Run:

```bash
python reports/validation_report.py
```

Outputs:

- `data/validation_report.html`
- `data/validation_summary.json`
- `data/equity_curve.png`

The report reads local signal logs from `logs/` and position state from `data/open_positions.json` and `data/closed_positions.json`.

By default, the primary metrics use the current model era only. Optedge chooses the latest timestamp from `config_runtime.py`, `data/model_weights.json`, and `data/predictor_coefs.json` as the cutoff. Older closed positions remain counted as stale/excluded, but they do not prove whether the current model is working.

Use `--all-time` when you intentionally want the full historical view:

```bash
python reports/validation_report.py --all-time
```

## Metrics

The report includes total logged signals, open versus closed positions, win rate, average return, median return, profit factor, max drawdown, calls versus puts performance, DTE buckets, spread buckets, confidence buckets, after-slippage performance, SPY and QQQ comparison when market data is reachable, and a random baseline.

## Sample Size

Closed signal count below 500 is marked as too small. This does not mean the system is broken. It means the research evidence is not mature enough to trust sizing without human review.

## Missing Buckets

Some old position rows may not include newer fields such as `spread_pct`. The report shows those as unavailable rather than inventing values.
