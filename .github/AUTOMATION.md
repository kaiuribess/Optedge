<!-- Purpose: Explain repository automation and dependency maintenance. -->

# Repository Automation

This directory automates repository quality checks and dependency maintenance.

- `workflows/ci.yml` tests the installed application on Python 3.11 through 3.13 and runs the focused lint gate.
- `dependabot.yml` checks Python and GitHub Actions dependencies each week.
- Workflow permissions remain read-only unless a future task explicitly requires more access.

Never place credentials, broker data, or local runtime output in workflow files.
