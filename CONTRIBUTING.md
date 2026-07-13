<!-- Purpose: Define a safe, reproducible contribution workflow for Optedge. -->

# Contributing to Optedge

Thank you for helping improve Optedge. Contributions are welcome when they keep the project inspectable, evidence-driven, local-first, and fail-closed at every broker boundary.

By participating, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Before You Start

- Search existing issues and pull requests before opening a duplicate.
- Use an issue for a substantial design change so evidence, scope, and safety implications can be discussed first.
- Never include credentials, account numbers, holdings, order history, private broker responses, or unsanitized runtime artifacts in an issue, commit, test fixture, or screenshot.
- Treat all strategy and performance claims as hypotheses until they are supported by chronological, after-cost evidence.

## Development Setup

Python 3.11 through 3.13 is supported; Python 3.12 is recommended.

```powershell
py -3.12 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
$env:OPTEDGE_CONTACT = Read-Host "Real operator email for SEC requests"
python setup_check.py
```

Run the release checks before submitting a pull request:

```powershell
python -m pytest
python -m ruff check . --select E9,F63,F7,F82,F401,F841,B033,B007,F541
python -m build
```

Network-dependent tests must use deterministic fixtures or mocks. A test must not require a real brokerage account or place, cancel, exercise, or modify an order.

`OPTEDGE_CONTACT` is required only for real SEC-backed requests. Tests should inject synthetic environment mappings rather than depend on or expose a contributor's actual address.

## Engineering Expectations

- Preserve exact instrument identity across research, planning, review, and validation.
- Keep current executable, current shadow, legacy, modeled, and broker-observed evidence explicitly separated.
- Use upstream timestamps for freshness; rewriting a local file must not make stale market or broker data appear fresh.
- Fail closed when account scope, quantities, option identity, quote quality, pagination, exposure, or evidence cannot be proven.
- Keep research lifecycle rows, local paper rows, broker-linked lifecycle rows, and normalized broker holdings as distinct domains.
- Do not add credential scraping, unofficial password-based Robinhood access, unattended execution, order loops, background broker writes, or automatic retries.
- Do not weaken a guardrail merely to make a dashboard state look ready.
- Add regression tests for every behavioral change, especially evidence eligibility, sizing, portfolio exposure, broker reconciliation, and packet construction.

## Pull Requests

Keep each pull request focused on one coherent change. Explain:

1. What problem it solves.
2. What behavior changed.
3. Which evidence or tests support the change.
4. What remains unverified or intentionally out of scope.
5. Whether data lineage, risk, privacy, or broker boundaries changed.

Use descriptive commit messages such as `Validate edge by independent entry day`. GitHub shows the most recent commit touching a path beside that file; this is history, not a per-file description field. Document file purposes in READMEs and docstrings instead of manufacturing one commit per file.

## Reporting Security Problems

Do not open a public issue for a vulnerability or exposed secret. Follow [SECURITY.md](SECURITY.md) instead.
