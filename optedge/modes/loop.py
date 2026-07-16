# Purpose: Run repeated research scans through the orchestrator.
"""Loop mode."""

from __future__ import annotations

from optedge.orchestrator import main_loop


def run() -> int:
    return main_loop()
