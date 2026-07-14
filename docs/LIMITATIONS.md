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
- Fixed-horizon option cost uses the greater of the configured floor and the recorded entry spread. This avoids treating a known spread as free, but it does not reconstruct the exit spread, order-book depth, queue position, partial fills, or a market gap. A row with no entry spread remains research-visible but cannot clear the complete option spread-coverage gate.
- Futures continuous-contract prices may differ from tradable contract-month prices.
- Learned exits stay disabled until the per-asset sample is large enough.
- Dynamic exits are conservative heuristics, not broker-side orders.
- Entry-day bootstrap intervals reduce same-day pseudo-replication, but sector, macro-regime, overlapping-horizon, and shared-factor dependence can remain.
- SPY is not an ideal benchmark for every asset, direction, volatility profile, or holding period.
- Slippage stress scales a recorded assumption. It cannot reconstruct missing historical order books, gaps, halts, rejected orders, or queue position.
- First-half versus recent-half stability is a screen, not a full walk-forward or regime-conditional validation study.
- The historical factor-IC command is a look-ahead diagnostic because it compares current factors with already-realized returns. It is ineligible for model promotion and is not evidence of a tradable backtest.
- The model firewall prevents ordinary same-scan fitting and requires purged out-of-sample promotion evidence, but it cannot eliminate data-mining, feature leakage outside the checked boundary, multiple-testing risk, regime decay, or implementation error. A trusted champion is still a fallible model, not a profit guarantee.
- Research lifecycle rows and Agentic paper rows are not verified broker positions. Only explicitly broker-linked lifecycle rows participate in live reconciliation, and the normalized broker snapshot remains the authoritative captured state.
- The total-open portfolio gate is a conservative point-in-time control, not continuous risk management. It blocks ambiguous, short, pending, unpriced, nonstandard-multiplier, expired-but-nonzero, or working-order states and must be recomputed from fresh same-account broker data before review. Adjusted option deliverables are blocked separately by the trade-plan layer.
- The account drawdown ledger is a new-entry interlock, not a broker statement or continuous risk monitor. Policy v2 observes account equity only when a valid snapshot is explicitly normalized and needs at least two observations spanning 18 hours and two New York calendar dates. It blocks an unexplained adjacent change of at least 25% because it cannot distinguish market P&L from deposits, withdrawals, transfers, fees, or corrections. Exact tail-to-snapshot equality can be easier to establish in a stable or after-hours account state; the safe response to changing fields is to recapture and review, not weaken equality. Its `1%`, `0.5%`, and `0.25%` ceilings reduce proposed risk but cannot guarantee the realized loss.
- The real-data ledger defaults outside the repository under `OPTEDGE_STATE_DIR` or the per-user OS state directory, while custom/test data directories remain self-contained. Successful atomic replacements leave the primary and `.bak` sidecar on the same newest chain; a missing required file, rollback, divergence, or lagging sidecar blocks review. Explicit normalization can reseal a validated sidecar left behind by an interrupted final write without adding an observation. These are local integrity controls rather than cryptographic authentication against an attacker who can replace all local state, and Optedge does not automatically rebaseline.
- Free-form share inputs can calculate a local sizing plan, but only an exact, fresh, actionable `top_shares_*.parquet` candidate can attest a manual broker packet. This prevents evidence borrowing; it does not prove the candidate will remain attractive or fillable.
- The versioned `acct_` key is a truncated pseudonymous SHA-256 identifier, not authentication, authorization, or encryption. The `...last4` mask is even less specific and may match multiple accounts; neither should be exposed unnecessarily, and only the full derived key can join normalized account state.
- Option candidate attestation proves that one canonical row appeared once and identically in two fresh inert local artifacts with a reconciled 90-day DTE floor. It cannot prove the future quote, broker availability, fill, or profitability; all live broker checks remain mandatory.
- The `1%` share and `15%` option spread limits are hard preflight ceilings, not promises of execution quality. A quote can move after the check, an option candidate may impose a stricter ceiling, and a limit order may remain unfilled.
- Blocking same-symbol share/option overlap is intentionally conservative. It prevents the narrow entry flow from overlooking obvious cross-asset concentration, but it is not a full delta, beta, sector, volatility, or portfolio-correlation model.
- Long-share market value and conservatively marked long-option debit are capital-at-risk proxies, not forecasts of liquidation value. Gaps, changing quotes, exercise/assignment, and orders submitted outside Optedge can change exposure immediately after capture.
- Robinhood options level 2 or 3 indicates account permission for supported strategies. Level 2 can permit the narrow long-call/long-put flow, but permission does not validate Optedge, guarantee suitability, ensure liquidity, or authorize an order.
- A `100` option multiplier does not by itself prove a standard deliverable. The manual flow also requires one active buy-to-open instrument, exact `instrument.chain_id` linkage to one complete chain, matching nonnumeric symbols, `cash_component=null`, and the exact equity in `underlying_instruments`. It blocks when any field, match, or preview is missing, ambiguous, adjusted, or nonstandard. This is deliberately conservative because broker metadata may be incomplete or inconsistent across surfaces.
- The packet-scoped UUIDv5 `ref_id` gives one logical order a deterministic retry identity; it does not itself prove that Robinhood accepted, deduplicated, rejected, or filled an order. Automatic retry is forbidden, and uncertain state must be resolved by reading current broker orders first.
- The post-confirmation re-read of account/portfolio, positions, orders, quote, instrument, chain, and packet expiry reduces stale-state placement risk, but it cannot make separate broker calls atomic. Any observed change or failed proof aborts the sole place call; an external change in the final network interval remains a brokerage race outside Optedge's control.
- The current Optedge packet covers a narrow entry-review flow. Planning stops and targets are not broker orders, and the local app does not provide an automated exit, cancellation, exercise, assignment, or expiry-management system.
- Option contracts can expire worthless, be assigned or exercised, change deliverables after corporate actions, or become difficult to close. A displayed stop cannot guarantee a limited loss.
- Taxes, wash sales, pattern-day-trading restrictions, account type, settlement, margin, and jurisdiction-specific rules are outside Optedge's scope.

Backtests and forward tests are evidence, not guarantees. A clean validation report, a passing model firewall, reduced position size, or an accepted broker preview should increase discipline, but none removes trading risk or guarantees profit.

Edge Lab's `validated` label means that one stored asset lane cleared the project's current minimum evidence rules. It is not a forecast for the next trade, a portfolio recommendation, or a claim that the thresholds are universally sufficient. Thresholds should not be weakened merely to obtain a passing label.

`archive.py` is a safe reset helper for generated artifacts. It moves files into `archive/`; it is not a data-deletion tool and it does not modify source code or local keys.
