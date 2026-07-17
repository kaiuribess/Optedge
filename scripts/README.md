<!-- Purpose: Explain cockpit utilities and safe research handoffs. -->

# Cockpit and Research Utilities

This directory contains the user-facing utilities around the core Optedge package.

- Serves the local Trade Desk and runs focused symbol or contract research jobs.
- Resolves symbols, exports paper research, and manages bounded local queues.
- Connects to Robinhood through browser OAuth for bounded reads and reviews, one fixed confirmed long-option order boundary, and complete account-snapshot normalization while rejecting invalid or contradictory quantities, pending transitions, and account identities.
- Keeps normal `90+` DTE swing options separate from the explicit `365-900` DTE `leaps_swing` profile and its isolated evidence lane.
- Preserves exact option identity from candidate to planner and fails closed when required evidence, freshness, account, permission, risk, or order-state fields are missing.

These tools never accept a Robinhood password, MFA code, cookie, or API key. OAuth grants are stored only in the operating-system credential vault, never in a project file or environment-variable fallback. The cockpit can place a narrowly constrained reviewed option order only after explicit confirmation or temporary guarded arming; ignored outputs may contain private account or research context.

Robinhood handoffs are short-lived, single-entry broker-preview packets. Their content and prompt digests detect modification but do not provide authentication or broker authority. Manual placement consumes a separate short-lived confirmation. The optional local controller is off by default, requires temporary explicit arming for unattended entry/exit, checks the account before each cycle, and never retries an attempted broker order automatically. Generic stops, cancellations, exercise, assignment, and ambiguous position management remain outside the narrow boundary.
