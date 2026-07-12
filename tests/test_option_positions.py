# Purpose: Test option entries expiry deduplication and recovery.
import sys
import tempfile
import json
from datetime import datetime, timedelta, timezone
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
        "expiry": "2099-06-18",
        "dte": 30,
        "mid": 2.0,
        "spot": 190.0,
        "suggested_contracts": 1,
        "trade_status": "Trade",
        "is_actionable": True,
        "research_guard_status": "warning",
        "stop_price": 1.0,
        "target_price": 4.0,
        "underlying_type": "equity",
        "contract_multiplier": 100,
        "deliverable": "100 shares",
        "settlement_style": "pm_physical",
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
            row = json.loads(positions.OPEN_FILE.read_text())[0]
            assert row["asset"] == "option"
            assert row["position_id"].startswith("option|AAPL|call|200|2099-06-18|")
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


def test_option_position_dedupes_duplicate_rows_in_same_batch():
    with tempfile.TemporaryDirectory() as td:
        old_data, old_open, old_closed = positions.DATA_DIR, positions.OPEN_FILE, positions.CLOSED_FILE
        _patch_files(td)
        try:
            added = positions.add_new_signals(
                pd.DataFrame([_valid_row(), _valid_row()]),
                datetime.now(timezone.utc),
            )
            assert added == 1
            assert positions.summary()["open_count"] == 1
        finally:
            positions.DATA_DIR, positions.OPEN_FILE, positions.CLOSED_FILE = old_data, old_open, old_closed


def test_option_position_respects_reentry_cooldown():
    with tempfile.TemporaryDirectory() as td:
        old_data, old_open, old_closed = positions.DATA_DIR, positions.OPEN_FILE, positions.CLOSED_FILE
        _patch_files(td)
        try:
            asof = datetime.now(timezone.utc)
            positions.CLOSED_FILE.write_text(json.dumps([{
                "ticker": "AAPL",
                "side": "call",
                "strike": 200.0,
                "expiry": "2099-06-18",
                "exit_time": (asof - timedelta(hours=1)).isoformat(),
                "exit_reason": "dynamic_exit",
            }]))
            added = positions.add_new_signals(pd.DataFrame([_valid_row()]), asof)
            assert added == 0
            assert positions.summary()["open_count"] == 0
        finally:
            positions.DATA_DIR, positions.OPEN_FILE, positions.CLOSED_FILE = old_data, old_open, old_closed


def test_option_position_stays_open_through_expiration_date():
    with tempfile.TemporaryDirectory() as td:
        old_data, old_open, old_closed = positions.DATA_DIR, positions.OPEN_FILE, positions.CLOSED_FILE
        _patch_files(td)
        try:
            asof = datetime(2026, 6, 18, 20, 0, tzinfo=timezone.utc)
            positions.OPEN_FILE.write_text(json.dumps([{
                "ticker": "AAPL",
                "side": "call",
                "strike": 200.0,
                "expiry": "2026-06-18",
                "entry_price": 2.0,
                "entry_time": "2026-06-01T00:00:00+00:00",
            }]))
            summary = positions.close_expired_positions(asof, log_reviews=False)
            assert summary["closed_this_iter"] == 0
            assert summary["open"] == 1
        finally:
            positions.DATA_DIR, positions.OPEN_FILE, positions.CLOSED_FILE = old_data, old_open, old_closed


def test_option_position_closes_expired_without_chain_reprice():
    with tempfile.TemporaryDirectory() as td:
        old_data, old_open, old_closed = positions.DATA_DIR, positions.OPEN_FILE, positions.CLOSED_FILE
        _patch_files(td)
        try:
            rows = [
                {
                    "ticker": "AAPL",
                    "side": "call",
                    "strike": 200.0,
                    "expiry": "2026-06-18",
                    "entry_price": 2.0,
                    "entry_time": "2026-06-01T00:00:00+00:00",
                    "stop_price": 1.0,
                    "target_price": 4.0,
                },
                {
                    "ticker": "MSFT",
                    "side": "call",
                    "strike": 400.0,
                    "expiry": "2026-12-18",
                    "entry_price": 3.0,
                    "entry_time": "2026-06-01T00:00:00+00:00",
                    "stop_price": 1.5,
                    "target_price": 6.0,
                },
            ]
            positions.OPEN_FILE.write_text(json.dumps(rows))
            summary = positions.close_expired_positions(
                datetime(2026, 6, 20, tzinfo=timezone.utc),
                log_reviews=False,
                fetch_expiry_history=False,
            )
            assert summary["closed_this_iter"] == 1
            open_rows = json.loads(positions.OPEN_FILE.read_text())
            closed_rows = json.loads(positions.CLOSED_FILE.read_text())
            assert [r["ticker"] for r in open_rows] == ["MSFT"]
            assert len(closed_rows) == 1
            assert closed_rows[0]["ticker"] == "AAPL"
            assert closed_rows[0]["exit_reason"] == "expired"
            assert closed_rows[0]["exit_price"] is None
            assert closed_rows[0]["pnl_pct"] is None
            assert closed_rows[0]["validation_eligible"] is False
            assert closed_rows[0]["expiry_close_price_source"] == "unresolved_no_expiry_market_data"
            assert closed_rows[0]["exit_time"].startswith("2026-06-18T20:00:00")
        finally:
            positions.DATA_DIR, positions.OPEN_FILE, positions.CLOSED_FILE = old_data, old_open, old_closed


def test_option_position_expiry_uses_historical_underlying_close():
    with tempfile.TemporaryDirectory() as td:
        old_data, old_open, old_closed = positions.DATA_DIR, positions.OPEN_FILE, positions.CLOSED_FILE
        _patch_files(td)
        try:
            rows = [{
                "ticker": "AAPL",
                "side": "call",
                "strike": 200.0,
                "expiry": "2026-06-18",
                "entry_price": 2.0,
                "entry_time": "2026-06-01T00:00:00+00:00",
                "stop_price": 1.0,
                "target_price": 4.0,
                "underlying_type": "equity",
                "contract_multiplier": 100,
                "deliverable": "100 shares",
                "settlement_style": "pm_physical",
            }]
            positions.OPEN_FILE.write_text(json.dumps(rows))
            def fake_history(ticker, period="1y", interval="1d", cache_age=3600):
                assert ticker == "AAPL"
                frame = pd.DataFrame(
                    {"Close": [203.0, 205.0]},
                    index=pd.to_datetime(["2026-06-17", "2026-06-18"], utc=True),
                )
                frame.attrs["history_source"] = "test_history"
                frame.attrs["history_quality"] = "observed_test"
                frame.attrs["price_basis"] = "unadjusted_close"
                return frame

            summary = positions.close_expired_positions(
                datetime(2026, 6, 20, tzinfo=timezone.utc),
                log_reviews=False,
                history_fetcher=fake_history,
                option_history_path=Path(td) / "missing_option_history.json",
            )
            assert summary["closed_this_iter"] == 1
            closed_rows = json.loads(positions.CLOSED_FILE.read_text())
            assert closed_rows[0]["exit_price"] == 5.0
            assert closed_rows[0]["pnl_pct"] == 1.5
            assert closed_rows[0]["expiry_close_price_source"] == "intrinsic_proxy_from_underlying_expiry_close"
            assert closed_rows[0]["expiry_underlying_price"] == 205.0
            assert closed_rows[0]["expiry_underlying_price_date"] == "2026-06-18"
            assert closed_rows[0]["expiry_underlying_type"] == "equity"
            assert closed_rows[0]["underlying_type"] == "equity"
            assert closed_rows[0]["contract_multiplier"] == 100.0
            assert closed_rows[0]["deliverable"] == "100 shares"
            assert closed_rows[0]["expiry_contract_multiplier"] == 100.0
            assert closed_rows[0]["expiry_deliverable"] == "100 shares"
            assert closed_rows[0]["expiry_deliverable_is_standard"] is True
            assert closed_rows[0]["expiry_underlying_price_basis"] == "unadjusted_close"
            assert closed_rows[0]["validation_eligible"] is True
        finally:
            positions.DATA_DIR, positions.OPEN_FILE, positions.CLOSED_FILE = old_data, old_open, old_closed


def test_expiry_cleanup_removes_open_duplicate_without_duplicating_closed_history():
    with tempfile.TemporaryDirectory() as td:
        old_data, old_open, old_closed = positions.DATA_DIR, positions.OPEN_FILE, positions.CLOSED_FILE
        _patch_files(td)
        try:
            row = {
                "position_id": "option|AAPL|call|200|2026-06-18|entry",
                "ticker": "AAPL",
                "side": "call",
                "strike": 200.0,
                "expiry": "2026-06-18",
                "entry_price": 2.0,
                "entry_time": "2026-06-01T00:00:00+00:00",
            }
            positions.OPEN_FILE.write_text(json.dumps([row]))
            positions.CLOSED_FILE.write_text(json.dumps([{**row, "exit_reason": "expired"}]))
            summary = positions.close_expired_positions(
                datetime(2026, 6, 20, tzinfo=timezone.utc),
                log_reviews=False,
                fetch_expiry_history=False,
            )
            assert summary["expired_removed_from_open"] == 1
            assert summary["closed_this_iter"] == 0
            assert summary["deduped_existing_closed"] == 1
            assert json.loads(positions.OPEN_FILE.read_text()) == []
            assert len(json.loads(positions.CLOSED_FILE.read_text())) == 1
        finally:
            positions.DATA_DIR, positions.OPEN_FILE, positions.CLOSED_FILE = old_data, old_open, old_closed


def test_expired_position_stays_open_when_closed_history_write_fails():
    with tempfile.TemporaryDirectory() as td:
        old_data, old_open, old_closed = positions.DATA_DIR, positions.OPEN_FILE, positions.CLOSED_FILE
        old_save = positions._save
        _patch_files(td)
        try:
            row = {
                "position_id": "option|AAPL|call|200|2026-06-18|entry",
                "ticker": "AAPL",
                "side": "call",
                "strike": 200.0,
                "expiry": "2026-06-18",
                "entry_price": 2.0,
                "entry_time": "2026-06-01T00:00:00+00:00",
            }
            positions.OPEN_FILE.write_text(json.dumps([row]), encoding="utf-8")

            def fail_closed_write(path, rows):
                if path == positions.CLOSED_FILE:
                    raise OSError("simulated closed-history write failure")
                return old_save(path, rows)

            positions._save = fail_closed_write
            try:
                positions.close_expired_positions(
                    datetime(2026, 6, 20, tzinfo=timezone.utc),
                    log_reviews=False,
                    fetch_expiry_history=False,
                )
                raise AssertionError("expected closed-history write failure")
            except OSError as exc:
                assert "simulated closed-history" in str(exc)

            assert json.loads(positions.OPEN_FILE.read_text(encoding="utf-8")) == [row]
            assert not positions.CLOSED_FILE.exists()
        finally:
            positions._save = old_save
            positions.DATA_DIR, positions.OPEN_FILE, positions.CLOSED_FILE = old_data, old_open, old_closed


if __name__ == "__main__":
    test_option_position_adds_trade_row()
    test_option_position_skips_watch_row()
    test_option_position_skips_blocked_guard_row()
    test_option_position_skips_zero_contract_row()
    test_option_position_dedupes_duplicate_rows_in_same_batch()
    test_option_position_respects_reentry_cooldown()
    test_option_position_stays_open_through_expiration_date()
    test_option_position_closes_expired_without_chain_reprice()
    test_option_position_expiry_uses_historical_underlying_close()
    test_expiry_cleanup_removes_open_duplicate_without_duplicating_closed_history()
    test_expired_position_stays_open_when_closed_history_write_fails()
    print("11/11 option position tests passed")
