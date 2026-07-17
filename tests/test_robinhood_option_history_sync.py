# Purpose: Prove bounded exact-history collection and atomic failure behavior.
from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backtest import option_history
from optedge.robinhood_option_history_sync import (
    RobinhoodOptionHistorySyncError,
    sync_robinhood_option_histories,
)

NOW = datetime(2026, 7, 16, 20, 0, tzinfo=UTC)


def _request() -> dict:
    return {
        "request_id": "AAA|2026-12-18|call|100",
        "contract_key": "AAA|2026-12-18|call|100",
        "symbol": "AAA",
        "expiry": "2026-12-18",
        "side": "call",
        "strike": 100.0,
        "state": "active",
        "start_time": "2026-07-01T00:00:00Z",
        "end_time": "2026-07-16T20:00:00Z",
        "interval": "day",
        "bounds": "regular",
    }


def _write_packet(data_dir: Path) -> None:
    (data_dir / option_history.REQUESTS_PATH.name).write_text(
        json.dumps(
            {
                "schema": option_history.REQUEST_SCHEMA,
                "generated_at": NOW.isoformat(),
                "request_count": 1,
                "requests": [_request()],
            }
        ),
        encoding="utf-8",
    )


class _Manager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.instruments = [
            {
                "id": "option-1",
                "chain_id": "chain-1",
                "chain_symbol": "AAA",
                "underlying_type": "equity",
                "expiration_date": "2026-12-18",
                "strike_price": "100",
                "type": "call",
                "state": "active",
                "tradability": "tradable",
                "occ_symbol": "AAA   261218C00100000",
            }
        ]

    def read_tool_input_schema(self, name: str) -> dict:
        if name == "get_option_chains":
            return {
                "type": "object",
                "required": ["underlying_symbol"],
                "properties": {
                    "underlying_symbol": {"type": "string"},
                    "cursor": {"type": "string"},
                },
                "additionalProperties": False,
            }
        if name == "get_option_instruments":
            return {
                "type": "object",
                "required": ["chain_id", "expiration_dates", "strike_price", "type"],
                "properties": {
                    "chain_id": {"type": "string"},
                    "expiration_dates": {"type": "string"},
                    "strike_price": {"type": "string"},
                    "type": {"type": "string"},
                    "state": {"type": "string"},
                    "tradability": {"type": "string"},
                    "cursor": {"type": "string"},
                },
                "additionalProperties": False,
            }
        if name == "get_option_historicals":
            return {
                "type": "object",
                "required": ["ids", "interval", "span", "bounds"],
                "properties": {
                    "ids": {"type": "array", "items": {"type": "string"}},
                    "interval": {"type": "string"},
                    "span": {
                        "type": "string",
                        "enum": ["day", "week", "month", "3month", "year"],
                    },
                    "bounds": {"type": "string"},
                },
                "additionalProperties": False,
            }
        raise AssertionError(name)

    def call_read_tool(self, name: str, arguments: dict, *, timeout_seconds: float) -> dict:
        assert 0 < timeout_seconds <= 12
        self.calls.append((name, copy.deepcopy(arguments)))
        if name == "get_option_chains":
            return {
                "data": {
                    "chains": [
                        {
                            "id": "chain-1",
                            "symbol": "AAA",
                            "expiration_dates": ["2026-12-18"],
                        }
                    ],
                    "next": None,
                }
            }
        if name == "get_option_instruments":
            return {"data": {"instruments": copy.deepcopy(self.instruments), "next": None}}
        if name == "get_option_historicals":
            return {
                "data": {
                    "historicals": [
                        {
                            "instrument_id": "option-1",
                            "data_points": [
                                {
                                    "begins_at": "2026-07-15T00:00:00Z",
                                    "open_price": "2.0",
                                    "high_price": "2.4",
                                    "low_price": "1.9",
                                    "close_price": "2.3",
                                    "session": "reg",
                                    "interpolated": False,
                                }
                            ],
                        }
                    ],
                    "next": None,
                }
            }
        raise AssertionError(name)


def test_exact_history_batch_writes_only_normalized_read_data(tmp_path: Path):
    _write_packet(tmp_path)
    manager = _Manager()
    result = sync_robinhood_option_histories(
        manager,
        data_dir=tmp_path,
        max_requests=1,
        now=NOW,
    )
    assert result["ok"] is True
    assert result["completed_count"] == 1
    assert result["bar_count"] == 1
    assert [name for name, _ in manager.calls] == [
        "get_option_chains",
        "get_option_instruments",
        "get_option_historicals",
    ]
    assert manager.calls[-1][1] == {
        "ids": ["option-1"],
        "interval": "day",
        "span": "month",
        "bounds": "regular",
    }
    snapshot = json.loads(
        (tmp_path / option_history.SNAPSHOT_PATH.name).read_text(encoding="utf-8")
    )
    assert snapshot["contracts"][0]["contract_key"] == "AAA|2026-12-18|call|100"
    assert snapshot["contracts"][0]["bars"][0]["close_price"] == 2.3
    assert result["does_not_place_orders"] is True
    assert result["does_not_preview_orders"] is True


def test_ambiguous_instrument_fails_without_partial_snapshot(tmp_path: Path):
    _write_packet(tmp_path)
    manager = _Manager()
    duplicate = copy.deepcopy(manager.instruments[0])
    duplicate["id"] = "option-2"
    manager.instruments.append(duplicate)
    with pytest.raises(RobinhoodOptionHistorySyncError) as caught:
        sync_robinhood_option_histories(manager, data_dir=tmp_path, max_requests=1, now=NOW)
    assert caught.value.code == "exact_instrument_ambiguous"
    assert not (tmp_path / option_history.SNAPSHOT_PATH.name).exists()
    assert all("review" not in name and "place" not in name for name, _ in manager.calls)


def test_unknown_required_historical_field_fails_before_history_call(tmp_path: Path):
    _write_packet(tmp_path)

    class ChangedManager(_Manager):
        def read_tool_input_schema(self, name: str) -> dict:
            schema = super().read_tool_input_schema(name)
            if name == "get_option_historicals":
                schema["required"].append("new_required_field")
                schema["properties"]["new_required_field"] = {"type": "string"}
            return schema

    manager = ChangedManager()
    with pytest.raises(RobinhoodOptionHistorySyncError) as caught:
        sync_robinhood_option_histories(manager, data_dir=tmp_path, max_requests=1, now=NOW)
    assert caught.value.code == "historical_schema_changed"
    assert [name for name, _ in manager.calls] == [
        "get_option_chains",
        "get_option_instruments",
    ]
    assert not (tmp_path / option_history.SNAPSHOT_PATH.name).exists()
