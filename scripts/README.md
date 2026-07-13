<!-- Purpose: Explain cockpit utilities and safe research handoffs. -->

# Cockpit and Research Utilities

This directory contains the user-facing utilities around the core Optedge package.

- Serves the local Trade Desk and runs focused symbol or contract research jobs.
- Resolves symbols, exports paper research, and manages bounded local queues.
- Normalizes read-only Robinhood captures, rejecting invalid or contradictory quantities, pending transitions, and account identities, and builds approval-gated review handoffs.
- Preserves exact option identity from candidate to planner and fails closed when required evidence, freshness, account, permission, risk, or order-state fields are missing.

These tools do not store Robinhood credentials or place broker orders. Ignored outputs may contain private account or research context.

Robinhood handoffs are short-lived, single-order, entry-review packets. Their content and prompt digests detect modification but do not provide authentication or broker authority. They prohibit schedules, batches, loops, automatic retries, and placement without an unchanged broker preview plus explicit confirmation. Stops, targets, exercise, assignment, expiration, and exits remain outside the local packet.
