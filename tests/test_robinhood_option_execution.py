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
                    "buying_power": {
                        "buying_power": "500.00",
                        "unleveraged_buying_power": "500.00",
                        "display_currency": "USD",
                    },
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

    def read_tool_input_schema(self, name: str):
        assert name == "get_option_quotes"
        return {
            "type": "object",
            "properties": {"option_ids": {"type": "array", "items": {"type": "string"}}},
        }

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


class _PositionManager(_Manager):
    def call_read_tool(self, name: str, arguments: dict, *, timeout_seconds: float):
        if name == "get_option_positions":
            return {
                "data": {
                    "positions": [
                        {
                            "option_id": "option-1",
                            "chain_symbol": "HYG",
                            "type": "long",
                            "quantity": "1.0000",
                            "average_price": "100.00",
                            "expiration_date": "2026-12-18",
                            "pending_buy_quantity": "0",
                            "pending_sell_quantity": "0",
                            "pending_exercise_quantity": "0",
                            "pending_assignment_quantity": "0",
                            "pending_expiration_quantity": "0",
                        }
                    ],
                    "next": None,
                }
            }
        if name == "get_option_quotes":
            return {
                "data": {
                    "quotes": [
                        {
                            "option_id": "option-1",
                            "bid_price": "1.40",
                            "ask_price": "1.50",
                            "mark_price": "1.45",
                            "updated_at": NOW.isoformat(),
                        }
                    ],
                    "next": None,
                }
            }
        return super().call_read_tool(name, arguments, timeout_seconds=timeout_seconds)


def test_portfolio_analysis_requires_fresh_profit_or_hard_risk_signal(tmp_path: Path):
    manager = _PositionManager()
    service = RobinhoodOptionExecutionService(manager, data_dir=tmp_path)
    choice = service.account_choices()["accounts"][0]

    analysis = service.portfolio_analysis(account_key=choice["account_key"], now=NOW)

    assert analysis["new_option_entry_allowed"] is False
    assert analysis["automatic_exit_candidate_count"] == 0
    assert analysis["holdings"][0]["action"] == "hold"
    assert analysis["holdings"][0]["position_type"] == "long"
    assert analysis["holdings"][0]["broker_reference_action"] == "take_profit"
    assert analysis["holdings"][0]["broker_close_ready"] is True
    assert analysis["holdings"][0]["unrealized_return_fraction"] == 0.45


def test_automated_exit_still_reviews_and_places_exact_close_once(tmp_path: Path):
    manager = _PositionManager()
    service = RobinhoodOptionExecutionService(manager, data_dir=tmp_path)
    choice = service.account_choices()["accounts"][0]

    result = service.execute_automated_exit(
        account_key=choice["account_key"],
        option_id="option-1",
        authorization_text=execution.AUTOMATION_AUTHORIZATION_TEXT,
        optedge_exit_action="hard_target",
        optedge_decision_digest="a" * 64,
        now=NOW,
    )

    assert result["status"] == "exit_order_sent"
    assert len(manager.review_calls) == 1
    assert manager.review_calls[0][1]["legs"][0] == {
        "option_id": "option-1",
        "side": "sell",
        "position_effect": "close",
        "ratio_quantity": 1,
    }
    assert len(manager.place_calls) == 1
    assert manager.place_calls[0]["ref_id"]
    assert result["automatic_retry_enabled"] is False


def test_automated_exit_requires_normal_optedge_decision_capability(tmp_path: Path):
    manager = _PositionManager()
    service = RobinhoodOptionExecutionService(manager, data_dir=tmp_path)
    choice = service.account_choices()["accounts"][0]

    with pytest.raises(RobinhoodOptionExecutionError, match="optedge_exit_action_required"):
        service.execute_automated_exit(
            account_key=choice["account_key"],
            option_id="option-1",
            authorization_text=execution.AUTOMATION_AUTHORIZATION_TEXT,
            optedge_exit_action="take_profit_reference",
            optedge_decision_digest="a" * 64,
            now=NOW,
        )

    assert manager.review_calls == []
    assert manager.place_calls == []
