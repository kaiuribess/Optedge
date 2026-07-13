<!-- Purpose: Capture release, evidence, privacy, and safety context for every pull request. -->

## Summary

<!-- What changed, and why? -->

## Evidence and Validation

<!-- List the exact tests, fixtures, or manual checks performed. -->

- [ ] `python -m pytest`
- [ ] Focused tests for the changed behavior
- [ ] Ruff release lint command from `CONTRIBUTING.md`
- [ ] Documentation links and examples reviewed when applicable

## Safety and Data Review

- [ ] No credentials, account numbers, holdings, raw broker captures, or private runtime artifacts are included.
- [ ] Research, paper, broker-linked lifecycle, and broker state remain explicitly separated.
- [ ] Evidence lineage, quote freshness, account scope, and fail-closed behavior are preserved or strengthened.
- [ ] No unattended broker action, automatic order retry, or profit guarantee was introduced.
- [ ] User-visible limitations and any unverified assumptions are documented.

## Screenshots

<!-- Add sanitized screenshots only when the interface changed. -->
