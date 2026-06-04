import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest import positions


def _patch_files(td):
    positions.DATA_DIR = Path(td)
    positions.OPEN_FILE = Path(td) / "open_positions.json"
    positions.CLOSED_FILE = Path(td) / "closed_positions.json"


def _valid_row(**overrides):
    row = {
        "ticker": "AAPL",
        "side": "call",
        "strike": 200.0,
        "expiry": "2026-06-18",
        "dte": 30,
        "mid": 2.0,
        "spot": 190.0,
        "suggested_contracts": 1,
        "trade_status": "Trade",
        "is_actionable": True,
        "research_guard_status": "warning",
        "stop_price": 1.0,
        "target_price": 4.0,
    }
    row.update(overrides)
    return row


def test_option_position_adds_trade_row():
    with tempfile.TemporaryDirectory() as td:
        old_data, old_open, old_closed = positions.DATA_DIR, positions.OPEN_FILE, positions.CLOSED_FILE
        _patch_files(td)
        try:
            added = positions.add_new_signals(
                pd.DataFrame([_valid_row()]),
                datetime.now(timezone.utc),
            )
            assert added == 1
            assert positions.summary()["open_count"] == 1
        finally:
            positions.DATA_DIR, positions.OPEN_FILE, positions.CLOSED_FILE = old_data, old_open, old_closed


def test_option_position_skips_watch_row():
    with tempfile.TemporaryDirectory() as td:
        old_data, old_open, old_closed = positions.DATA_DIR, positions.OPEN_FILE, positions.CLOSED_FILE
        _patch_files(td)
        try:
            added = positions.add_new_signals(
                pd.DataFrame([_valid_row(trade_status="Watch")]),
                datetime.now(timezone.utc),
            )
            assert added == 0
            assert positions.summary()["open_count"] == 0
        finally:
            positions.DATA_DIR, positions.OPEN_FILE, positions.CLOSED_FILE = old_data, old_open, old_closed


def test_option_position_skips_blocked_guard_row():
    with tempfile.TemporaryDirectory() as td:
        old_data, old_open, old_closed = positions.DATA_DIR, positions.OPEN_FILE, positions.CLOSED_FILE
        _patch_files(td)
        try:
            added = positions.add_new_signals(
                pd.DataFrame([_valid_row(research_guard_status="blocked")]),
                datetime.now(timezone.utc),
            )
            assert added == 0
            assert positions.summary()["open_count"] == 0
        finally:
            positions.DATA_DIR, positions.OPEN_FILE, positions.CLOSED_FILE = old_data, old_open, old_closed


def test_option_position_skips_zero_contract_row():
    with tempfile.TemporaryDirectory() as td:
        old_data, old_open, old_closed = positions.DATA_DIR, positions.OPEN_FILE, positions.CLOSED_FILE
        _patch_files(td)
        try:
            added = positions.add_new_signals(
                pd.DataFrame([_valid_row(suggested_contracts=0)]),
                datetime.now(timezone.utc),
            )
            assert added == 0
            assert positions.summary()["open_count"] == 0
        finally:
            positions.DATA_DIR, positions.OPEN_FILE, positions.CLOSED_FILE = old_data, old_open, old_closed


if __name__ == "__main__":
    test_option_position_adds_trade_row()
    test_option_position_skips_watch_row()
    test_option_position_skips_blocked_guard_row()
    test_option_position_skips_zero_contract_row()
    print("4/4 option position tests passed")
