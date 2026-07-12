<!-- Purpose: Explain research gates and manual trade-plan safeguards. -->

# Research and Trade-Plan Guardrails

This package enforces conservative research gates and deterministic trade-planning rules.

- Checks validation quality, drawdown, spreads, model freshness, and engine health.
- Sizes whole-share and single-leg long-option plans within risk and allocation limits.
- Builds short-lived, approval-gated Robinhood review packets.

It never connects to Robinhood or places an order. Every packet still requires fresh broker review and explicit confirmation.
