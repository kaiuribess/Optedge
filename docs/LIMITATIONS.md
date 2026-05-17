# Limitations

Optedge is a research and decision-support tool, not an autonomous trading system.

Signals require human review before any trade is placed.

Performance depends on:

- Data quality and data-source availability.
- Bid/ask spreads and real fill prices.
- Slippage, commissions, assignment risk, and liquidity.
- Market regime changes.
- Earnings gaps, news shocks, and macro events.
- Small sample sizes during early forward testing.
- Provider differences in options chains, Greeks, and theoretical prices.
- Futures continuous-contract prices may differ from tradable contract-month prices.
- Learned exits stay disabled until the per-asset sample is large enough.
- Dynamic exits are conservative heuristics, not broker-side orders.

Backtests and forward tests are evidence, not guarantees. A clean validation report should increase confidence, but it does not remove trading risk.

`archive.py` is a safe reset helper for generated artifacts. It moves files into `archive/`; it is not a data-deletion tool and it does not modify source code or local keys.
