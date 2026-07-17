# Purpose: Verify two-click, single-use Robinhood option execution safety.
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import optedge.robinhood_option_execution as execution
from optedge.robinhood_option_execution import (
    RobinhoodOptionExecutionError,
    RobinhoodOptionExecutionService,
)

NOW = datetime(2026, 7, 17, 20, 0, tzinfo=UTC)
RAW_ACCOUNT = "RH-123456789"


def _report(*, ready: bool = True) -> dict:
    return {
        "ready_for_manual_review": ready,
        "artifact_digest_sha256": "report-digest",
        "candidate": {
            "symbol": "HYG",
            "label": "HYG 2026-12-18 P 75",
            "quantity_cap": 1,
            "candidate_digest_sha256": "candidate-digest",
        },
        "contract": {"option_id": "option-1"},
        "quote": {"ask_price": 0.50, "limit_cap": 0.52},
    }


class _Manager:
    def __init__(self) -> None:
        self.review_calls: list[tuple[str, dict]] = []
        self.place_calls: list[dict] = []

    def call_read_tool(self, name: str, arguments: dict, *, timeout_seconds: float):
        if name == "get_accounts":
            return {
                "data": {
                    "accounts": [
                        {
                            "account_number": RAW_ACCOUNT,
                            "agentic_allowed": True,
                            "active": True,
                            "option_level": "option_level_2",
                        }
                    ]
                }
            }
        if name == "get_portfolio":
            return {
                "data": {
                    "total_value": 10_000,
                    "buying_power": 500,
                    "unleveraged_buying_power": 500,
                }
            }
        if name in {"get_option_positions", "get_equity_positions"}:
            return {"data": {"positions": [], "next": None}}
        if name in {"get_option_orders", "get_equity_orders"}:
            return {"data": {"orders": [], "next": None}}
        raise AssertionError(name)

    def call_review_tool(self, name: str, arguments: dict, *, timeout_seconds: float):
        self.review_calls.append((name, dict(arguments)))
        return {"estimated_cost": "50.00", "order_checks": []}

    def place_confirmed_option_order(self, arguments: dict, *, timeout_seconds: float):
        self.place_calls.append(dict(arguments))
        return {"order_id": "order-1", "account_number": RAW_ACCOUNT}


def test_preview_then_place_consumes_one_short_lived_token(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(execution, "check_best_option_finalist", lambda *args, **kwargs: _report())
    manager = _Manager()
    service = RobinhoodOptionExecutionService(manager, data_dir=tmp_path)
    choice = service.account_choices()["accounts"][0]
    assert RAW_ACCOUNT not in str(choice)

    preview = service.review(candidate_index=0, account_key=choice["account_key"], now=NOW)
    assert preview["status"] == "preview_ready"
    assert preview["order"]["maximum_debit"] == 50
    assert manager.review_calls[0][1]["legs"][0]["option_id"] == "option-1"
    assert manager.place_calls == []

    placed = service.place(
        confirmation_token=preview["confirmation_token"],
        confirmation_text="PLACE",
        now=NOW + timedelta(seconds=5),
    )
    assert placed["status"] == "order_sent"
    assert placed["confirmation_consumed"] is True
    assert placed["automatic_retry_enabled"] is False
    assert len(manager.place_calls) == 1
    assert RAW_ACCOUNT not in str(placed)

    with pytest.raises(RobinhoodOptionExecutionError, match="confirmation_invalid_or_consumed"):
        service.place(
            confirmation_token=preview["confirmation_token"],
            confirmation_text="PLACE",
            now=NOW + timedelta(seconds=6),
        )
    assert len(manager.place_calls) == 1


def test_preview_refuses_blocked_optedge_gate_before_broker_review(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        execution,
        "check_best_option_finalist",
        lambda *args, **kwargs: _report(ready=False),
    )
    manager = _Manager()
    service = RobinhoodOptionExecutionService(manager, data_dir=tmp_path)
    choice = service.account_choices()["accounts"][0]
    with pytest.raises(RobinhoodOptionExecutionError, match="optedge_or_robinhood_gate_blocked"):
        service.review(candidate_index=0, account_key=choice["account_key"], now=NOW)
    assert manager.review_calls == []
    assert manager.place_calls == []
