<!-- Purpose: Explain backtesting, sizing, and position lifecycle validation. -->

# Backtesting and Lifecycle Validation

This package validates signals and manages simulated trade lifecycles for options, shares, and futures.

- Measures fixed-horizon and current-mark outcomes.
- Sizes and tracks simulated positions with drawdown and portfolio-risk controls.
- Calibrates predictions and learns conservative policies only from eligible evidence.

It writes ignored state under `data/`. Modeled or broker-market observations are research evidence, not verified fills.
