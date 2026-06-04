import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest import exit_rules, share_positions


def _patch_files(td):
    share_positions.OPEN_FILE = Path(td) / "open_share_positions.json"
    share_positions.CLOSED_FILE = Path(td) / "closed_share_positions.json"
    share_positions.DATA_DIR = Path(td)
    exit_rules.DATA_DIR = Path(td)
    exit_rules.EXIT_REVIEWS_FILE = Path(td) / "exit_reviews.jsonl"


def _restore_exit_rules(old_data, old_file):
    exit_rules.DATA_DIR = old_data
    exit_rules.EXIT_REVIEWS_FILE = old_file


def test_share_position_closes_on_stop():
    with tempfile.TemporaryDirectory() as td:
        old_data, old_file = exit_rules.DATA_DIR, exit_rules.EXIT_REVIEWS_FILE
        old_price = share_positions._latest_price
        _patch_files(td)
        try:
            asof = datetime.now(timezone.utc)
            share_positions.add_new_share_signals(pd.DataFrame([{"ticker": "AAA", "spot": 10, "stop_pct": -0.1, "target_pct": 0.2}]), asof)
            share_positions._latest_price = lambda ticker: 8.5
            res = share_positions.mark_to_market_shares(asof, None)
            assert res["closed_this_iter"] == 1
        finally:
            share_positions._latest_price = old_price
            _restore_exit_rules(old_data, old_file)


def test_share_position_closes_on_target():
    with tempfile.TemporaryDirectory() as td:
        old_data, old_file = exit_rules.DATA_DIR, exit_rules.EXIT_REVIEWS_FILE
        old_price = share_positions._latest_price
        _patch_files(td)
        try:
            asof = datetime.now(timezone.utc)
            share_positions.add_new_share_signals(pd.DataFrame([{"ticker": "AAA", "spot": 10, "stop_pct": -0.1, "target_pct": 0.2}]), asof)
            share_positions._latest_price = lambda ticker: 12.5
            res = share_positions.mark_to_market_shares(asof, None)
            assert res["closed_this_iter"] == 1
        finally:
            share_positions._latest_price = old_price
            _restore_exit_rules(old_data, old_file)


def test_share_position_keeps_open_on_reprice_failure():
    with tempfile.TemporaryDirectory() as td:
        old_data, old_file = exit_rules.DATA_DIR, exit_rules.EXIT_REVIEWS_FILE
        old_price = share_positions._latest_price
        _patch_files(td)
        try:
            asof = datetime.now(timezone.utc)
            share_positions.add_new_share_signals(pd.DataFrame([{"ticker": "AAA", "spot": 10}]), asof)
            share_positions._latest_price = lambda ticker: None
            res = share_positions.mark_to_market_shares(asof, None)
            assert res["open"] == 1
        finally:
            share_positions._latest_price = old_price
            _restore_exit_rules(old_data, old_file)


def test_share_position_backfills_missing_entry_price():
    with tempfile.TemporaryDirectory() as td:
        old_data, old_file = exit_rules.DATA_DIR, exit_rules.EXIT_REVIEWS_FILE
        old_price = share_positions._latest_price
        _patch_files(td)
        try:
            asof = datetime.now(timezone.utc)
            share_positions._latest_price = lambda ticker: 10.0
            added = share_positions.add_new_share_signals(pd.DataFrame([{
                "ticker": "AAA", "trade_status": "Trade", "is_actionable": True,
            }]), asof)
            assert added == 1
            assert share_positions.summary()["open_count"] == 1
        finally:
            share_positions._latest_price = old_price
            _restore_exit_rules(old_data, old_file)


def test_share_position_skips_watch_rows_when_status_present():
    with tempfile.TemporaryDirectory() as td:
        old_data, old_file = exit_rules.DATA_DIR, exit_rules.EXIT_REVIEWS_FILE
        _patch_files(td)
        try:
            asof = datetime.now(timezone.utc)
            added = share_positions.add_new_share_signals(pd.DataFrame([{
                "ticker": "AAA", "spot": 10, "trade_status": "Watch",
            }]), asof)
            assert added == 0
            assert share_positions.summary()["open_count"] == 0
        finally:
            _restore_exit_rules(old_data, old_file)


if __name__ == "__main__":
    test_share_position_closes_on_stop()
    test_share_position_closes_on_target()
    test_share_position_keeps_open_on_reprice_failure()
    test_share_position_backfills_missing_entry_price()
    test_share_position_skips_watch_rows_when_status_present()
    print("5/5 share position tests passed")
