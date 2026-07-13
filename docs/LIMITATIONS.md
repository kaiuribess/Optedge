<!-- Purpose: Document known research and execution limitations. -->

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
- Robinhood MCP research is only as current as its upstream quote timestamp. A freshly collected record may still contain an aging or stale market quote, especially after the options session closes.
- Free historical option bid/ask coverage is incomplete. Fixed-horizon validation can use exact non-interpolated Robinhood option trade bars when a connected Codex session has cached them, but these are last-trade bars rather than bid/ask fills. Missing target-date bars use a clearly labeled constant-entry-IV model proxy. Neither method is an independently verified Optedge fill.
- Futures continuous-contract prices may differ from tradable contract-month prices.
- Learned exits stay disabled until the per-asset sample is large enough.
- Dynamic exits are conservative heuristics, not broker-side orders.
- Entry-day bootstrap intervals reduce same-day pseudo-replication, but sector, macro-regime, overlapping-horizon, and shared-factor dependence can remain.
- SPY is not an ideal benchmark for every asset, direction, volatility profile, or holding period.
- Slippage stress scales a recorded assumption. It cannot reconstruct missing historical order books, gaps, halts, rejected orders, or queue position.
- First-half versus recent-half stability is a screen, not a full walk-forward or regime-conditional validation study.
- The historical factor-IC command is a look-ahead diagnostic because it compares current factors with already-realized returns. It is ineligible for model promotion and is not evidence of a tradable backtest.
- Research lifecycle rows and Agentic paper rows are not verified broker positions. Only explicitly broker-linked lifecycle rows participate in live reconciliation, and the normalized broker snapshot remains the authoritative captured state.
- The total-open portfolio gate is a conservative point-in-time control, not continuous risk management. It blocks ambiguous, short, pending, unpriced, nonstandard-multiplier, expired-but-nonzero, or working-order states and must be recomputed from fresh same-account broker data before review. Adjusted option deliverables are blocked separately by the trade-plan layer.
- Long-share market value and conservatively marked long-option debit are capital-at-risk proxies, not forecasts of liquidation value. Gaps, changing quotes, exercise/assignment, and orders submitted outside Optedge can change exposure immediately after capture.
- Robinhood options level 2 or 3 indicates account permission for supported strategies; it does not validate Optedge, guarantee suitability, ensure liquidity, or authorize an order.
- The current Optedge packet covers a narrow entry-review flow. Planning stops and targets are not broker orders, and the local app does not provide an automated exit, cancellation, exercise, assignment, or expiry-management system.
- Option contracts can expire worthless, be assigned or exercised, change deliverables after corporate actions, or become difficult to close. A displayed stop cannot guarantee a limited loss.
- Taxes, wash sales, pattern-day-trading restrictions, account type, settlement, margin, and jurisdiction-specific rules are outside Optedge's scope.

Backtests and forward tests are evidence, not guarantees. A clean validation report should increase confidence, but it does not remove trading risk.

Edge Lab's `validated` label means that one stored asset lane cleared the project's current minimum evidence rules. It is not a forecast for the next trade, a portfolio recommendation, or a claim that the thresholds are universally sufficient. Thresholds should not be weakened merely to obtain a passing label.

`archive.py` is a safe reset helper for generated artifacts. It moves files into `archive/`; it is not a data-deletion tool and it does not modify source code or local keys.
