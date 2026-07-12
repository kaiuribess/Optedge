# Purpose: Run one research scan through the orchestrator.
"""Single scan mode."""
from __future__ import annotations

from optedge.orchestrator import main


def run() -> int:
    return main()
