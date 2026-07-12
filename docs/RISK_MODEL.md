<!-- Purpose: Define position-sizing and portfolio-risk controls. -->

# Risk Model

Optedge is designed as a research and decision-support system, not an automatic execution engine.

## Sizing

The sizing layer uses:

- Expected value after estimated fill slippage.
- Fractional Kelly sizing.
- Per-trade caps for options and shares.
- Risk-budget sizing for futures using point value, ATR-like distance, and micro-contract preference.
- Sector concentration caps.
- DTE-aware option win-probability discounts.
- Earnings IV-crush discounts.
- Time-of-day liquidity discounts.

The Trade Desk adds a separate deterministic sizing layer for manual review. It does not use conviction or Kelly to increase size:

- `risk budget = account equity x risk per trade`
- `allocation cap = min(account equity x max allocation, available buying power when supplied)`
- `shares = floor(min(risk budget / planned stop loss per share, allocation cap / entry price))`
- `contracts = floor(min(risk budget / full debit per contract, allocation cap / full debit per contract))`

For shares, planned stop loss per share is the entry-to-stop distance plus round-trip slippage. For long options, the full debit, not the planned premium stop, is the risk-budget basis. This prevents a stop assumption from permitting more contracts than the account-level risk budget can absorb if the option loses its entire value. Round-trip slippage is still added to the planned stop loss and removed from planned reward.

The planner keeps stop-risk and maximum capital-loss measures separate:

- A long share's planned stop loss is quantity times the entry-to-stop distance plus slippage. Its maximum capital-loss reference is the full entry notional.
- A long option's planned stop loss is quantity times the entry-to-stop premium distance, the standard `100x` multiplier, and slippage. Its maximum capital-loss reference is the full debit.
- A short share's capital loss is unbounded, so the current Robinhood handoff blocks short-share execution even though research sizing can still be calculated.

Stops are not guaranteed fills. A gap, trading halt, liquidity failure, or option expiry can produce a loss greater than the planned stop loss. Missing entry, stop, target, multiplier, or account limits makes size unavailable rather than silently treating the value as zero.

The resulting Robinhood packet is review-only and entry-only. It contains no broker credentials or selected account number, expires 10 minutes after creation, and does not place the planning stop or target. The local gate requires healthy validation evidence, fresh broker and research snapshots, no duplicate exposure or logical working order, and one same active account that satisfies portfolio-equity, risk-fraction, allocation-fraction, permission, and conservative buying-power checks. It never combines equity from one account with buying power or permissions from another.

The connected task repeats that math against the chosen account's live `total_value`. For options, full debit must fit live equity times both the risk and allocation fractions. For shares, planned stop loss must fit live equity times the risk fraction and full notional must fit live equity times the allocation fraction. Cost must also fit the smaller of reported buying power and unleveraged buying power. A planner equity assumption may be lower than live equity, but it may not materially overstate it.

The live quote gate is numeric: bid and ask must be positive, ask must be at least bid, quote timestamps must be no more than 120 seconds old, and `(ask - bid) / ((ask + bid) / 2)` must be no greater than the packet cap. The hard cap is 15% for options and 1% for shares; an option candidate may carry a stricter cap. The task stops when any field or timestamp is missing or when the live ask exceeds the packet limit. It cannot raise the limit or place anything until the user confirms the exact broker preview.

The packet supports one logical order at a time. It prohibits batches, scheduled tasks, loops, automatic retries, and field changes between review and placement. If placement status is uncertain, the broker order state must be queried before any further action.

## Guardrails

`risk/research_guard.py` warns or blocks trust when:

- Fewer than 500 closed signals are available.
- Max drawdown is worse than -20%.
- Spread buckets validate poorly.
- An option recommendation has a spread above 15%.
- A new long-option recommendation has negative modeled buyer edge after the round-trip spread.
- Win rate is below a simple breakeven threshold.
- Model weights appear stale.
- Key data engines return no data.

## Exit Reviews

Every scan reanalyzes exits for open options, shares, and futures.

Hard exits always run first and cannot be overridden:

- Options: stop, target, expiry.
- Shares: stop and target.
- Futures: direction-aware stop and target.

Dynamic exit review runs second. It produces an `exit_pressure` score from 0 to 100 and logs every review to `data/exit_reviews.jsonl`.

- 0-39: hold.
- 40-59: watch.
- 60-79: tighten stop.
- 80-100: close early.

The pressure model considers confidence drops, score deterioration, news/sentiment flips, macro regime changes, research guard warnings, engine health, age, repeated reprice failures, and asset-specific risks such as option DTE decay, share trend deterioration, or futures score reversal.

New positions receive a one-hour grace period from soft `tighten_stop` and `close_early` actions so the entry and exit passes in the same scan cannot manufacture zero-duration trades. Hard stops, hard targets, expiry, and research-guard blocks remain immediate. Closed option contracts also have a 24-hour reentry cooldown to prevent same-contract churn.

## Learned Exit Policy

`backtest/exit_learning.py` can refit conservative exit thresholds from closed trades and exit-review history. Learning activates per asset only after at least 100 independent eligible closures, 20 exit reviews, and 10 distinct closed-entry and review days. Same-scan dynamic exits are retained in performance results but excluded from policy learning because they are lifecycle churn rather than swing outcomes.

Learned thresholds are clamped and can move by at most 5 points per refit. Learned policy never overrides hard stops, hard targets, expiry exits, or research-guard blocks. If the policy is missing, malformed, stale, or under-sampled, defaults are used.

Entry readiness is evaluated from executable swing outcomes after slippage when that validation view is available. All closures are still reported, but Watch/Skip rows, zero-size recommendations, blocked entries, and same-scan lifecycle churn cannot make the guard appear statistically mature.

## Adaptive Factor Weights

Source-controlled `config.py` weights are the default priors. A runtime override is allowed only after at least 500 independent lifecycle outcomes across 10 distinct entry days. Repeated forward-test snapshots and same-scan dynamic exits are not treated as independent evidence.

Adaptive fitting uses after-slippage outcomes, day-balanced sample weights, chronological validation splits, and positive-only coefficients. Learned weights receive only a 25% blend against the configured priors, no factor may exceed 30%, and the full current factor set must remain represented.

`config_runtime.py` is parsed as data rather than executed. It is ignored when its evidence metadata is missing, it does not cover every configured factor, either the override or its newest training outcome is older than 14 days, or it fails a concentration or normalization check. The historical snapshot IC report is diagnostic only: it compares current factors with already-realized returns and is not accepted as walk-forward training evidence.

## Human Review

The output should be treated as a prioritized research board. Fill quality, news shocks, data gaps, spreads, and regime changes can dominate model expectations.
