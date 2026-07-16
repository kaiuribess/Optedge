# Purpose: Test complete direct broker reads and redacted-only snapshot persistence.
from __future__ import annotations

import copy
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

import optedge.robinhood_snapshot_sync as snapshot_sync_module
from optedge.robinhood_snapshot_sync import (
    DIRECT_SNAPSHOT_SYNC_SCHEMA,
    RobinhoodSnapshotSyncError,
    sync_robinhood_broker_snapshot,
)

ACCOUNT_NUMBER = "RH1234567890"
FIXED_NOW = datetime(2026, 7, 16, 18, 30, tzinfo=UTC)


def _account():
    return {
        "account_number": ACCOUNT_NUMBER,
        "rhs_account_number": "RHS0987654321",
        "brokerage_account_type": "individual",
        "type": "cash",
        "nickname": f"Agentic {ACCOUNT_NUMBER}",
        "is_default": True,
        "state": "active",
        "deactivated": False,
        "permanently_deactivated": False,
        "agentic_allowed": True,
        "option_level": "option_level_2",
    }


def _portfolio():
    return {
        "data": {
            "buying_power": {
                "buying_power": "800.00",
                "unleveraged_buying_power": "650.00",
                "display_currency": "USD",
            },
            "cash": "700.00",
            "currency": "USD",
            "equity_value": "1000.00",
            "options_value": "0.00",
            "crypto_value": "0.00",
            "event_contracts_value": "0.00",
            "fixed_income_value": "0.00",
            "futures_value": "0.00",
            "mutual_funds_value": "0.00",
            "pending_deposits": "0.00",
            "total_value": "1000.00",
        }
    }


def _page(key, rows, next_value=None):
    return {"data": {key: copy.deepcopy(rows), "next": next_value}}


class FakeManager:
    def __init__(self):
        self.state = "connected"
        self.calls: list[tuple[str, dict]] = []
        self.schemas = {
            "get_accounts": {
                "type": "object",
                "properties": {"cursor": {"type": "string"}},
                "additionalProperties": False,
            },
            "get_portfolio": {
                "type": "object",
                "required": ["account_number"],
                "properties": {"account_number": {"type": "string"}},
                "additionalProperties": False,
            },
            **{
                name: {
                    "type": "object",
                    "required": ["account_number"],
                    "properties": {
                        "account_number": {"type": "string"},
                        "cursor": {"type": "string"},
                    },
                    "additionalProperties": False,
                }
                for name in (
                    "get_equity_positions",
                    "get_option_positions",
                    "get_equity_orders",
                    "get_option_orders",
                )
            },
            "get_option_instruments": {
                "type": "object",
                "required": ["ids"],
                "properties": {
                    "ids": {"type": "string"},
                    "cursor": {"type": "string"},
                },
                "additionalProperties": False,
            },
        }
        self.responses = {
            "get_accounts": {"data": {"accounts": [_account()]}},
            "get_portfolio": _portfolio(),
            "get_equity_positions": _page("positions", []),
            "get_option_positions": _page("positions", []),
            "get_equity_orders": _page("orders", []),
            "get_option_orders": _page("orders", []),
            "get_option_instruments": _page("instruments", []),
        }

    def status(self):
        return {"connection_state": self.state}

    def read_tool_input_schema(self, name):
        return copy.deepcopy(self.schemas[name])

    def call_read_tool(self, name, arguments, *, timeout_seconds=None):
        self.calls.append((name, dict(arguments)))
        assert timeout_seconds is not None
        assert 0 < timeout_seconds <= snapshot_sync_module.MAX_READ_CALL_SECONDS
        response = self.responses[name]
        if callable(response):
            response = response(dict(arguments))
        return copy.deepcopy(response)


def test_direct_sync_reads_every_account_scope_and_persists_only_redacted_state(tmp_path: Path):
    manager = FakeManager()
    result = sync_robinhood_broker_snapshot(
        manager,
        data_dir=tmp_path,
        now=lambda: FIXED_NOW,
    )

    assert result["schema"] == DIRECT_SNAPSHOT_SYNC_SCHEMA
    assert result["ok"] is True
    assert result["snapshot_ready"] is True
    assert result["account_count"] == 1
    assert result["raw_bundle_written"] is False
    assert result["account_numbers_persisted"] is False
    assert [name for name, _ in manager.calls] == [
        "get_accounts",
        "get_portfolio",
        "get_equity_positions",
        "get_option_positions",
        "get_equity_orders",
        "get_option_orders",
    ]
    for name, arguments in manager.calls[1:]:
        assert arguments["account_number"] == ACCOUNT_NUMBER, name

    snapshot_path = tmp_path / "robinhood_broker_snapshot.json"
    encoded = snapshot_path.read_text(encoding="utf-8")
    assert ACCOUNT_NUMBER not in encoded
    assert "RHS0987654321" not in encoded
    snapshot = json.loads(encoded)
    assert snapshot["accounts"][0]["account_mask"] == "...7890"
    ledger_files = list((tmp_path / "robinhood_account_equity_ledgers").glob("*.json"))
    assert len(ledger_files) == 1
    assert ACCOUNT_NUMBER not in ledger_files[0].read_text(encoding="utf-8")


def test_direct_sync_follows_one_proven_cursor_and_preserves_linkage(tmp_path: Path):
    manager = FakeManager()

    def option_orders(arguments):
        if arguments.get("cursor") == "cursor-2":
            return _page("orders", [])
        return _page(
            "orders",
            [],
            "https://api.robinhood.com/options/orders/?cursor=cursor-2",
        )

    manager.responses["get_option_orders"] = option_orders
    result = sync_robinhood_broker_snapshot(
        manager,
        data_dir=tmp_path,
        now=lambda: FIXED_NOW,
    )

    assert result["ok"] is True
    option_calls = [args for name, args in manager.calls if name == "get_option_orders"]
    assert option_calls == [
        {"account_number": ACCOUNT_NUMBER},
        {"account_number": ACCOUNT_NUMBER, "cursor": "cursor-2"},
    ]
    saved = json.loads((tmp_path / "robinhood_broker_snapshot.json").read_text())
    assert saved["normalization_blockers"] == []


def test_direct_sync_resolves_option_instrument_ids_from_live_schema(tmp_path: Path):
    manager = FakeManager()
    manager.responses["get_option_positions"] = _page(
        "positions",
        [{
            "average_price": "1.25",
            "chain_id": "chain-1",
            "chain_symbol": "AAPL",
            "expiration_date": "2027-01-15",
            "option_id": "option-1",
            "quantity": "1.00",
            "pending_buy_quantity": "0.00",
            "pending_sell_quantity": "0.00",
            "pending_assignment_quantity": "0.00",
            "pending_exercise_quantity": "0.00",
            "pending_expiration_quantity": "0.00",
            "trade_value_multiplier": "100.00",
            "type": "long",
        }],
    )
    manager.responses["get_option_instruments"] = _page(
        "instruments",
        [{
            "id": "option-1",
            "chain_id": "chain-1",
            "chain_symbol": "AAPL",
            "expiration_date": "2027-01-15",
            "strike_price": "200.00",
            "type": "call",
            "state": "active",
            "tradability": "tradable",
            "underlying_type": "equity",
        }],
    )

    result = sync_robinhood_broker_snapshot(
        manager,
        data_dir=tmp_path,
        now=lambda: FIXED_NOW,
    )

    assert result["option_instrument_count"] == 1
    assert ("get_option_instruments", {"ids": "option-1"}) in manager.calls
    saved = json.loads((tmp_path / "robinhood_broker_snapshot.json").read_text())
    assert saved["option_positions"][0]["strike_price"] == 200.0
    assert saved["option_positions"][0]["option_type"] == "call"


def test_direct_sync_fails_before_writing_on_unprovable_pagination(tmp_path: Path):
    manager = FakeManager()
    manager.responses["get_option_orders"] = _page(
        "orders",
        [],
        "https://api.robinhood.com/options/orders/?page=2",
    )

    with pytest.raises(RobinhoodSnapshotSyncError) as exc_info:
        sync_robinhood_broker_snapshot(
            manager,
            data_dir=tmp_path,
            now=lambda: FIXED_NOW,
        )
    assert exc_info.value.code == "pagination_cursor_invalid"
    assert not (tmp_path / "robinhood_broker_snapshot.json").exists()
    assert not (tmp_path / "robinhood_account_equity_ledgers").exists()


def test_direct_sync_requires_an_explicit_connected_session(tmp_path: Path):
    manager = FakeManager()
    manager.state = "disconnected"

    with pytest.raises(RobinhoodSnapshotSyncError) as exc_info:
        sync_robinhood_broker_snapshot(
            manager,
            data_dir=tmp_path,
            now=lambda: FIXED_NOW,
        )
    assert exc_info.value.code == "robinhood_not_connected"
    assert manager.calls == []


def test_direct_sync_rejects_a_concurrent_process_wide_sync(tmp_path: Path):
    entered = threading.Event()
    release = threading.Event()
    first_manager = FakeManager()
    second_manager = FakeManager()
    original_accounts = first_manager.responses["get_accounts"]

    def blocked_accounts(_arguments):
        entered.set()
        assert release.wait(timeout=5)
        return original_accounts

    first_manager.responses["get_accounts"] = blocked_accounts
    first_result: list[dict] = []
    first_errors: list[BaseException] = []

    def run_first():
        try:
            first_result.append(
                sync_robinhood_broker_snapshot(
                    first_manager,
                    data_dir=tmp_path / "first",
                    now=lambda: FIXED_NOW,
                )
            )
        except BaseException as exc:  # pragma: no cover - assertion aid
            first_errors.append(exc)

    thread = threading.Thread(target=run_first)
    thread.start()
    assert entered.wait(timeout=5)
    try:
        with pytest.raises(RobinhoodSnapshotSyncError) as exc_info:
            sync_robinhood_broker_snapshot(
                second_manager,
                data_dir=tmp_path / "second",
                now=lambda: FIXED_NOW,
            )
        assert exc_info.value.code == "sync_already_active"
        assert second_manager.calls == []
        assert not (tmp_path / "second" / "robinhood_broker_snapshot.json").exists()
    finally:
        release.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert first_errors == []
    assert first_result[0]["ok"] is True


def test_direct_sync_total_call_budget_fails_before_any_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    manager = FakeManager()
    monkeypatch.setattr(snapshot_sync_module, "MAX_TOTAL_MANAGER_CALLS", 2)

    with pytest.raises(RobinhoodSnapshotSyncError) as exc_info:
        sync_robinhood_broker_snapshot(
            manager,
            data_dir=tmp_path,
            now=lambda: FIXED_NOW,
        )

    assert exc_info.value.code == "snapshot_call_budget_exceeded"
    assert manager.calls == []
    assert not (tmp_path / "robinhood_broker_snapshot.json").exists()


def test_direct_sync_total_page_budget_fails_before_any_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    manager = FakeManager()
    monkeypatch.setattr(snapshot_sync_module, "MAX_TOTAL_CAPTURED_PAGES", 1)

    with pytest.raises(RobinhoodSnapshotSyncError) as exc_info:
        sync_robinhood_broker_snapshot(
            manager,
            data_dir=tmp_path,
            now=lambda: FIXED_NOW,
        )

    assert exc_info.value.code == "snapshot_page_budget_exceeded"
    assert [name for name, _ in manager.calls] == [
        "get_accounts",
        "get_portfolio",
    ]
    assert not (tmp_path / "robinhood_broker_snapshot.json").exists()


def test_direct_sync_monotonic_deadline_discards_late_read_before_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    manager = FakeManager()
    ticks = iter([0.0] * 8 + [1.1])
    monkeypatch.setattr(snapshot_sync_module, "SNAPSHOT_SYNC_DEADLINE_SECONDS", 1.0)

    with pytest.raises(RobinhoodSnapshotSyncError) as exc_info:
        sync_robinhood_broker_snapshot(
            manager,
            data_dir=tmp_path,
            now=lambda: FIXED_NOW,
            monotonic=lambda: next(ticks),
        )

    assert exc_info.value.code == "snapshot_sync_deadline_exceeded"
    assert [name for name, _ in manager.calls] == ["get_accounts"]
    assert not (tmp_path / "robinhood_broker_snapshot.json").exists()
