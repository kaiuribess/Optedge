<!-- Purpose: Explain cockpit utilities and safe research handoffs. -->

# Cockpit and Research Utilities

This directory contains the user-facing utilities around the core Optedge package.

- Serves the local Trade Desk and runs focused symbol or contract research jobs.
- Resolves symbols, exports paper research, and manages bounded local queues.
- Connects to Robinhood through browser OAuth for bounded, user-triggered reads and previews, and normalizes complete account snapshots while rejecting invalid or contradictory quantities, pending transitions, and account identities.
- Keeps normal `90+` DTE swing options separate from the explicit `365-900` DTE `leaps_swing` profile and its isolated evidence lane.
- Preserves exact option identity from candidate to planner and fails closed when required evidence, freshness, account, permission, risk, or order-state fields are missing.

These tools never accept a Robinhood password, MFA code, cookie, or API key and never place broker orders. OAuth grants are stored only in the operating-system credential vault, never in a project file or environment-variable fallback. Ignored outputs may contain private account or research context.

Robinhood handoffs are short-lived, single-entry broker-preview packets. Their content and prompt digests detect modification but do not provide authentication or broker authority. They prohibit schedules, batches, loops, and automatic retries. The current release stops at preview and exposes no placement API; any later submission decision, plus stops, targets, exercise, assignment, expiration, and exits, remains outside the local packet.
