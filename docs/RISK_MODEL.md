# Risk Model

Optedge is designed as a research and decision-support system, not an automatic execution engine.

## Sizing

The sizing layer uses:

- Expected value after estimated fill slippage.
- Fractional Kelly sizing.
- Per-trade caps for options and shares.
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

## Human Review

The output should be treated as a prioritized research board. Fill quality, news shocks, data gaps, spreads, and regime changes can dominate model expectations.
