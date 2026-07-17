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
- Free historical option bid/ask coverage is incomplete. Fixed-horizon validation can use exact non-interpolated Robinhood option trade bars from a user-triggered direct read or manual connector cache, but these are last-trade bars rather than bid/ask fills. Missing target-date bars use a clearly labeled constant-entry-IV model proxy. Neither method is an independently verified Optedge fill.
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
- Direct broker sync is a user-triggered point-in-time read, not monitoring. It performs no background polling or retries, follows at most 50 proven cursor-linked pages per collection, and fails before replacing the snapshot when an account scope or pagination proof is incomplete. Raw account identifiers still exist transiently in process memory and at Robinhood because scoped reads require them; only the normalized local output is redacted.
- The account drawdown ledger is a new-entry interlock, not a broker statement or continuous risk monitor. Policy v2 observes account equity only when a valid direct sync or manual snapshot normalization succeeds and needs at least two observations spanning 18 hours and two New York calendar dates. It blocks an unexplained adjacent change of at least 25% because it cannot distinguish market P&L from deposits, withdrawals, transfers, fees, or corrections. Exact tail-to-snapshot equality can be easier to establish in a stable or after-hours account state; the safe response to changing fields is to recapture and review, not weaken equality. Its `1%`, `0.5%`, and `0.25%` ceilings reduce proposed risk but cannot guarantee the realized loss.
- The real-data ledger defaults outside the repository under `OPTEDGE_STATE_DIR` or the per-user OS state directory, while custom/test data directories remain self-contained. Successful atomic replacements leave the primary and `.bak` sidecar on the same newest chain; a missing required file, rollback, divergence, or lagging sidecar blocks review. Explicit normalization can reseal a validated sidecar left behind by an interrupted final write without adding an observation. These are local integrity controls rather than cryptographic authentication against an attacker who can replace all local state, and Optedge does not automatically rebaseline.
- Free-form share inputs can calculate a local sizing plan, but only an exact, fresh, actionable `top_shares_*.parquet` candidate can attest a manual broker packet. This prevents evidence borrowing; it does not prove the candidate will remain attractive or fillable.
- The versioned `acct_` key is a truncated pseudonymous SHA-256 identifier, not authentication, authorization, or encryption. The `...last4` mask is even less specific and may match multiple accounts; neither should be exposed unnecessarily, and only the full derived key can join normalized account state.
- Option candidate attestation proves only that one canonical row appeared once and identically in two fresh inert local artifacts with the matching profile policy. Normal swing retains the 90-day floor; explicit LEAPS requires 365-900 DTE and the isolated `option_leaps_swing` evidence lane. Neither profile proves the future quote, broker availability, fill, or profitability; all live broker checks remain mandatory.
- LEAPS have more time to expiry, not guaranteed safety. They can still lose the entire debit, suffer volatility compression and wide spreads, and gap through the 25% premium-loss planning reference. The 3/5/10-session reviews and 20-session maximum planned hold are research controls, not broker-side exits.
- The `1%` share, `15%` normal-option, and `10%` `leaps_swing` spread limits are hard preflight ceilings, not promises of execution quality. A quote can move after the check, a candidate may impose a stricter ceiling, and a limit order may remain unfilled.
- Blocking same-symbol share/option overlap is intentionally conservative. It prevents the narrow entry flow from overlooking obvious cross-asset concentration, but it is not a full delta, beta, sector, volatility, or portfolio-correlation model.
- Long-share market value and conservatively marked long-option debit are capital-at-risk proxies, not forecasts of liquidation value. Gaps, changing quotes, exercise/assignment, and orders submitted outside Optedge can change exposure immediately after capture.
- Robinhood options level 2 or 3 indicates account permission for supported strategies. Level 2 can permit the narrow long-call/long-put flow, but permission does not validate Optedge, guarantee suitability, ensure liquidity, or authorize an order.
- A `100` option multiplier does not by itself prove a standard deliverable. The manual flow also requires one active buy-to-open instrument, exact `instrument.chain_id` linkage to one complete chain, matching nonnumeric symbols, `cash_component=null`, and the exact equity in `underlying_instruments`. It blocks when any field, match, or preview is missing, ambiguous, adjusted, or nonstandard. This is deliberately conservative because broker metadata may be incomplete or inconsistent across surfaces.
- The packet-scoped UUIDv5 `ref_id` gives one logical review packet a deterministic audit identity; it is not broker authorization and proves no acceptance, rejection, or fill. Automatic retry is forbidden.
- The current release exposes connection, allowlisted account reads/reviews, and one fixed long-option order method. A preview can still become stale immediately and does not make later broker calls atomic; placement therefore re-reads state and can still be rejected, remain unfilled, partially fill, or fill at an unfavorable time.
- Guarded automation is deliberately narrow: one selected Agentic account, one concurrent long-option position, one limit order at a time, no automatic retry, and a maximum of three configured orders per New York day. It is not a general portfolio manager. Multiple holdings, equities, adjusted contracts, cancellations, exercise, assignment, and ambiguous expiry events require manual review.
- Option contracts can expire worthless, be assigned or exercised, change deliverables after corporate actions, or become difficult to close. A displayed stop cannot guarantee a limited loss.
- Taxes, wash sales, pattern-day-trading restrictions, account type, settlement, margin, and jurisdiction-specific rules are outside Optedge's scope.

Backtests and forward tests are evidence, not guarantees. A clean validation report, a passing model firewall, reduced position size, or an accepted broker preview should increase discipline, but none removes trading risk or guarantees profit.

Edge Lab's `validated` label means that one stored asset lane cleared the project's current minimum evidence rules. It is not a forecast for the next trade, a portfolio recommendation, or a claim that the thresholds are universally sufficient. Thresholds should not be weakened merely to obtain a passing label.

`archive.py` is a safe reset helper for generated artifacts. It moves files into `archive/`; it is not a data-deletion tool and it does not modify source code or local keys.
