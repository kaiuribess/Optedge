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

## Guardrails

`risk/research_guard.py` warns or blocks trust when:

- Fewer than 500 closed signals are available.
- Max drawdown is worse than -20%.
- Spread buckets validate poorly.
- An option recommendation has a spread above 15%.
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

## Learned Exit Policy

`backtest/exit_learning.py` can refit conservative exit thresholds from closed trades and exit-review history. Learning activates per asset only after at least 100 closed positions, 20 exit reviews, and 10 distinct review days.

Learned thresholds are clamped and can move by at most 5 points per refit. Learned policy never overrides hard stops, hard targets, expiry exits, or research-guard blocks. If the policy is missing, malformed, stale, or under-sampled, defaults are used.

## Human Review

The output should be treated as a prioritized research board. Fill quality, news shocks, data gaps, spreads, and regime changes can dominate model expectations.
