# Purpose: Run Optedge with python -m optedge.
"""Allow installed and source checkouts to run with ``python -m optedge``."""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
