<!-- Purpose: Explain backtesting, sizing, and position lifecycle validation. -->

# Backtesting and Lifecycle Validation

This package validates signals and manages simulated trade lifecycles for options, shares, and futures.

- Measures fixed-horizon and current-mark outcomes.
- Sizes and tracks simulated positions with drawdown and portfolio-risk controls.
- Calibrates predictions and learns conservative policies only from eligible evidence.
- Builds Edge Lab's asset-specific evidence lanes, horizon-length circular moving-block confidence bounds, cost stress, and fail-closed live-review eligibility.

It writes ignored state under `data/`. Modeled or broker-market observations are research evidence, not verified fills.

The historical IC path is quarantined as `diagnostic_only_lookahead`: it compares current scores with already-realized returns and cannot promote model weights. The variable-age current-mark accuracy refit is disabled by default. Fixed-session, frozen, current-method outcomes are the decision-evidence path.

Live-review evidence is bound to the exact signal-time strategy, methodology, experiment, and policy digest. The outcome parquet and fixed-horizon summary must be digest-matched and no more than 96 hours old; mature resolution plus raw-return, after-cost, nonnegative-slippage, benchmark, and cost-reconciliation coverage must be complete. Shadow and legacy evidence remain visible but blocked from live eligibility.

The live gate requires at least 200 independent outcomes across 30 entry days and at least 30 effective blocks whose length is at least the holding horizon. At the default ten-session horizon, the block rule implies about 300 distinct entry days and is therefore stricter than the baseline day floor. Option performance gates use broker-observed outcomes only; modeled option proxies are research-only and cannot improve live eligibility.
