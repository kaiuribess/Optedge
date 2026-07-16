# Purpose: Test futures entries exits reversals and tracking.
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest import exit_rules, futures_positions  # noqa: E402


def _patch_files(td):
    futures_positions.OPEN_FILE = Path(td) / "open_futures_positions.json"
    futures_positions.CLOSED_FILE = Path(td) / "closed_futures_positions.json"
    futures_positions.DATA_DIR = Path(td)
    exit_rules.DATA_DIR = Path(td)
    exit_rules.EXIT_REVIEWS_FILE = Path(td) / "exit_reviews.jsonl"


def _restore_exit_rules(old_data, old_file):
    exit_rules.DATA_DIR = old_data
    exit_rules.EXIT_REVIEWS_FILE = old_file


def _signal(direction="long"):
    return pd.DataFrame(
        [
            {
                "symbol": "ES=F",
                "name": "S&P 500",
                "direction": direction,
                "trade_status": "Trade",
                "entry_price": 100,
                "stop_price": 95 if direction == "long" else 105,
                "target_price": 110 if direction == "long" else 90,
                "point_value": 5,
                "suggested_contracts": 1,
                "futures_score": 1 if direction == "long" else -1,
            }
        ]
    )


def test_futures_long_closes_on_stop():
    with tempfile.TemporaryDirectory() as td:
        old_data, old_file = exit_rules.DATA_DIR, exit_rules.EXIT_REVIEWS_FILE
        _patch_files(td)
        try:
            asof = datetime.now(UTC)
            futures_positions.add_new_futures_signals(_signal("long"), asof)
            futures_positions._latest_price = lambda symbol: 94
            assert futures_positions.mark_to_market_futures(asof, None)["closed_this_iter"] == 1
        finally:
            _restore_exit_rules(old_data, old_file)


def test_futures_short_closes_on_target():
    with tempfile.TemporaryDirectory() as td:
        old_data, old_file = exit_rules.DATA_DIR, exit_rules.EXIT_REVIEWS_FILE
        _patch_files(td)
        try:
            asof = datetime.now(UTC)
            futures_positions.add_new_futures_signals(_signal("short"), asof)
            futures_positions._latest_price = lambda symbol: 89
            assert futures_positions.mark_to_market_futures(asof, None)["closed_this_iter"] == 1
        finally:
            _restore_exit_rules(old_data, old_file)


def test_futures_closes_on_score_reversal():
    with tempfile.TemporaryDirectory() as td:
        old_data, old_file = exit_rules.DATA_DIR, exit_rules.EXIT_REVIEWS_FILE
        _patch_files(td)
        try:
            asof = datetime.now(UTC)
            futures_positions.add_new_futures_signals(_signal("long"), asof)
            futures_positions._latest_price = lambda symbol: 101
            current = pd.DataFrame([{"symbol": "ES=F", "futures_score": -1.0}])
            assert futures_positions.mark_to_market_futures(asof, current)["closed_this_iter"] == 1
        finally:
            _restore_exit_rules(old_data, old_file)


def test_futures_skips_zero_size_and_guard_blocked_rows():
    with tempfile.TemporaryDirectory() as td:
        old_data, old_file = exit_rules.DATA_DIR, exit_rules.EXIT_REVIEWS_FILE
        _patch_files(td)
        try:
            asof = datetime.now(UTC)
            zero = _signal("long")
            zero.loc[0, "suggested_contracts"] = 0
            blocked = _signal("short")
            blocked.loc[0, "research_guard_status"] = "blocked"
            assert futures_positions.add_new_futures_signals(zero, asof) == 0
            assert futures_positions.add_new_futures_signals(blocked, asof) == 0
            assert futures_positions.summary()["open_count"] == 0
        finally:
            _restore_exit_rules(old_data, old_file)


if __name__ == "__main__":
    test_futures_long_closes_on_stop()
    test_futures_short_closes_on_target()
    test_futures_closes_on_score_reversal()
    test_futures_skips_zero_size_and_guard_blocked_rows()
    print("4/4 futures position tests passed")
