"""Check Optedge's read-only IBKR paper market-data connection."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ibkr_provider


def _load_open_option(path: Path) -> dict:
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        rows = []
    for row in rows if isinstance(rows, list) else []:
        if row.get("ticker") and row.get("expiry") and row.get("side") and row.get("strike"):
            return row
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Check IBKR quote access for Optedge")
    parser.add_argument("--ticker")
    parser.add_argument("--expiry", help="YYYY-MM-DD")
    parser.add_argument("--side", choices=["call", "put"])
    parser.add_argument("--strike", type=float)
    args = parser.parse_args()

    if args.ticker:
        if not all([args.expiry, args.side, args.strike]):
            print("--ticker requires --expiry --side --strike")
            return 2
        position = {
            "ticker": args.ticker,
            "expiry": args.expiry,
            "side": args.side,
            "strike": args.strike,
        }
    else:
        position = _load_open_option(ROOT / "data" / "open_positions.json")

    if not position:
        print("No option position found. Pass --ticker --expiry --side --strike.")
        return 2
    print("Testing IBKR quote for:", {
        "ticker": position.get("ticker"),
        "expiry": position.get("expiry"),
        "side": position.get("side"),
        "strike": position.get("strike"),
    })
    quote = ibkr_provider.quote_option_position(position)
    if not quote:
        print("IBKR quote unavailable.")
        reason = ibkr_provider.disabled_reason()
        if reason:
            print("Reason:", reason)
        print("Check TWS Paper is open, API is enabled, port is 7497, and ib_insync is installed.")
        return 1
    print("IBKR quote OK:", quote)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
