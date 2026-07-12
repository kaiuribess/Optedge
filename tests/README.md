<!-- Purpose: Explain regression coverage and broker-safety testing. -->

# Regression and Safety Tests

This directory protects Optedge research, dashboard, risk, broker-boundary, and provider behavior across Python 3.11 through 3.13.

- Verifies provider fallbacks, pricing, lifecycle tracking, validation, and strategy policy.
- Exercises the CLI, cockpit, focused jobs, reports, and generated examples.
- Enforces Robinhood snapshot, sizing, duplicate-order, and manual-review safety gates.

Tests use fixtures, temporary files, and mocked providers—never real credentials or live order placement.
