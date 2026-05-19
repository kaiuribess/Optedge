import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reports import validation_report


def _write_json(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows), encoding="utf-8")


def test_validation_counts_open_options_and_futures():
    with tempfile.TemporaryDirectory() as td:
        old_data = validation_report.DATA_DIR
        old_logs = validation_report.LOGS_DIR
        validation_report.DATA_DIR = Path(td) / "data"
        validation_report.LOGS_DIR = Path(td) / "logs"
        try:
            _write_json(
                validation_report.DATA_DIR / "open_positions.json",
                [{"ticker": "AAA", "entry_time": "2020-01-01T00:00:00+00:00"}],
            )
            _write_json(
                validation_report.DATA_DIR / "open_futures_positions.json",
                [{"symbol": "ES=F", "entry_time": "2020-01-01T00:00:00+00:00"}],
            )
            summary = validation_report.build_summary(
                scope="current_model",
                since="2099-01-01T00:00:00+00:00",
            )
            assert summary["open_positions"] == 2
            assert summary["assets"]["option"]["open_positions"] == 1
            assert summary["assets"]["futures"]["open_positions"] == 1
        finally:
            validation_report.DATA_DIR = old_data
            validation_report.LOGS_DIR = old_logs


def test_validation_current_model_keeps_old_open_positions():
    with tempfile.TemporaryDirectory() as td:
        old_data = validation_report.DATA_DIR
        old_logs = validation_report.LOGS_DIR
        validation_report.DATA_DIR = Path(td) / "data"
        validation_report.LOGS_DIR = Path(td) / "logs"
        try:
            _write_json(
                validation_report.DATA_DIR / "open_share_positions.json",
                [{"ticker": "OLD", "entry_time": "2020-01-01T00:00:00+00:00"}],
            )
            _write_json(
                validation_report.DATA_DIR / "closed_share_positions.json",
                [{"ticker": "OLD", "entry_time": "2020-01-01T00:00:00+00:00", "pnl_pct": 0.1}],
            )
            summary = validation_report.build_summary(
                scope="current_model",
                since="2099-01-01T00:00:00+00:00",
            )
            assert summary["open_positions"] == 1
            assert summary["assets"]["share"]["open_positions"] == 1
            assert summary["closed_positions"] == 0
        finally:
            validation_report.DATA_DIR = old_data
            validation_report.LOGS_DIR = old_logs


def test_position_aging_counts_open_positions_by_asset():
    with tempfile.TemporaryDirectory() as td:
        old_data = validation_report.DATA_DIR
        validation_report.DATA_DIR = Path(td) / "data"
        try:
            _write_json(
                validation_report.DATA_DIR / "open_positions.json",
                [{"ticker": "AAA", "entry_time": "2020-01-01T00:00:00+00:00"}],
            )
            _write_json(
                validation_report.DATA_DIR / "open_share_positions.json",
                [{"ticker": "BBB", "entry_time": "2020-01-02T00:00:00+00:00"}],
            )
            open_df, _ = validation_report.load_positions()
            aging = validation_report._position_aging(open_df)
            assert aging["open_count"] == 2
            assert aging["asset_breakdown"]["option"] == 1
            assert aging["asset_breakdown"]["share"] == 1
        finally:
            validation_report.DATA_DIR = old_data


def test_current_model_does_not_hide_active_signal_logs():
    with tempfile.TemporaryDirectory() as td:
        old_data = validation_report.DATA_DIR
        old_logs = validation_report.LOGS_DIR
        old_loader = validation_report.load_signal_logs
        validation_report.DATA_DIR = Path(td) / "data"
        validation_report.LOGS_DIR = Path(td) / "logs"
        try:
            validation_report.load_signal_logs = lambda: pd.DataFrame([{
                "ticker": "AAA",
                "entry_time": "2026-05-18T14:42:18+00:00",
            }])
            summary = validation_report.build_summary(
                scope="current_model",
                since="2099-01-01T00:00:00+00:00",
            )
            assert summary["total_signals"] == 1
        finally:
            validation_report.DATA_DIR = old_data
            validation_report.LOGS_DIR = old_logs
            validation_report.load_signal_logs = old_loader


if __name__ == "__main__":
    test_validation_counts_open_options_and_futures()
    test_validation_current_model_keeps_old_open_positions()
    test_position_aging_counts_open_positions_by_asset()
    test_current_model_does_not_hide_active_signal_logs()
    print("4/4 validation report tests passed")
