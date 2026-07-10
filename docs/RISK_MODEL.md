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
