<!-- Purpose: Explain research gates and manual trade-plan safeguards. -->

# Research, Portfolio, and Trade-Plan Guardrails

This package enforces conservative research gates, deterministic trade planning, and same-account portfolio-exposure limits.

| Module | Responsibility |
|---|---|
| `research_guard.py` | Checks validation quality, drawdown, spreads, model freshness, and engine health. |
| `portfolio.py` | Pure, read-only calculation of normalized same-account broker exposure and post-trade total-open headroom. |
| `trade_plan.py` | Sizes whole-share and single-leg long-option proposals and builds short-lived, approval-gated Robinhood review packets. |

The portfolio gate uses the lower of assumed and live same-account equity. Reconciled long-share value and conservatively marked long-option debit are added to the proposal's full share notional or option debit. Research recommendations and local paper positions are not treated as broker holdings. Invalid or contradictory quantities, conflicting valuations, ambiguous account identity, short, unpriced, unscoped, pending, nonstandard-multiplier, or working-order states block the calculation instead of being estimated; adjusted option deliverables are separately blocked by the trade-plan layer.

Nothing in this package connects to Robinhood or places an order; the separate `optedge` connector package owns the bounded OAuth/read/preview client. Packet digests detect local modification but do not authenticate or authorize a broker call. Every packet still requires fresh account, position, order, instrument, and quote review. The current release stops at broker preview, so any later confirmation and submission must occur in a Robinhood-supported surface outside Optedge.
