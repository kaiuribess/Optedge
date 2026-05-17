"""Command-line entry point for Optedge."""
from __future__ import annotations

import sys

from . import orchestrator


def main() -> int:
    """Route CLI calls to the correct application mode."""
    if any(arg == "--loop" or arg.startswith("--loop=") for arg in sys.argv):
        return orchestrator.main_loop()
    return orchestrator.main()


if __name__ == "__main__":
    raise SystemExit(main())
