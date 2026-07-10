"""CLI wrapper for the read-only Robinhood option-history bridge."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.option_history import main  # noqa: E402, I001


if __name__ == "__main__":
    raise SystemExit(main())
