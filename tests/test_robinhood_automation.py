# Purpose: Verify guarded Robinhood automation modes and one-shot behavior.
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from optedge.robinhood_automation import (
    ARMING_PHRASE,
    RobinhoodAutomationController,
    RobinhoodAutomationError,
)

NOW = datetime(2026, 7, 17, 18, 0, tzinfo=UTC)  # 2:00 PM New York
ACCOUNT_KEY = "acct_0123456789abcdef"


class _ExecutionService:
    def __init__(self, holdings: list[dict] | None = None) -> None:
        self.holdings = holdings or []
        self.entry_calls: list[dict] = []
        self.exit_calls: list[dict] = []

    def portfolio_analysis(self, *, account_key: str, now: datetime):
        assert account_key == ACCOUNT_KEY
        return {
            "schema": "optedge_robinhood_portfolio_analysis_v1",
            "generated_at": now.isoformat(),
            "new_option_entry_allowed": not self.holdings,
            "holdings": self.holdings,
        }

    def execute_automated_entry(self, **kwargs):
        self.entry_calls.append(dict(kwargs))
        return {"status": "order_sent", "automatic_retry_enabled": False}

    def execute_automated_exit(self, **kwargs):
        self.exit_calls.append(dict(kwargs))
        return {"status": "exit_order_sent", "automatic_retry_enabled": False}


def _controller(tmp_path: Path, service: _ExecutionService):
    return RobinhoodAutomationController(
        execution_service=service,
        data_dir=tmp_path,
        snapshot_syncer=lambda: {"ok": True, "account_count": 1, "counts": {}},
        research_refresher=lambda analysis: {"ok": True, "status": "completed"},
        exit_analyzer=lambda analysis, now: {
            "portfolio_analysis": analysis,
            "reviews": [],
            "research_source": {"available": True},
            "broker_snapshot_source": {"available": True},
        },
        shortlist_builder=lambda: {"status": "ready"},
        finalist_checker=lambda: {
            "reports": [
                {
                    "ready_for_manual_review": True,
                    "market_check_passed": True,
                    "candidate": {
                        "label": "HYG 2026-12-18 P 75",
                        "symbol": "HYG",
                        "after_cost_edge_pct": 0.08,
                        "candidate_digest_sha256": "candidate-digest",
                    },
                    "blockers": [],
                }
            ]
        },
        clock=lambda: NOW,
    )


def test_approval_mode_analyzes_and_returns_buy_choice_without_placing(tmp_path: Path):
    service = _ExecutionService()
    controller = _controller(tmp_path, service)
    configured = controller.configure(
        {"mode": "approval_required", "account_key": ACCOUNT_KEY}, now=NOW
    )

    result = controller.run_once(trigger="manual", now=NOW)

    assert configured["session_armed"] is False
    assert result["status"] == "analysis_complete"
    assert result["action"] == "choose_candidate"
    assert result["eligible_candidate_count"] == 1
    assert service.entry_calls == []
    assert service.exit_calls == []


def test_automatic_mode_requires_explicit_risk_acknowledgements(tmp_path: Path):
    controller = _controller(tmp_path, _ExecutionService())
    with pytest.raises(RobinhoodAutomationError, match="automation_arming_phrase_invalid"):
        controller.configure(
            {"mode": "automatic", "account_key": ACCOUNT_KEY},
            now=NOW,
        )


def test_failed_normal_optedge_refresh_blocks_every_broker_action(tmp_path: Path):
    service = _ExecutionService()
    controller = _controller(tmp_path, service)
    controller.research_refresher = lambda analysis: {
        "ok": False,
        "status": "normal_optedge_refresh_failed",
    }
    controller.configure(
        {"mode": "approval_required", "account_key": ACCOUNT_KEY}, now=NOW
    )

    result = controller.run_once(trigger="manual", now=NOW)

    assert result["status"] == "research_refresh_blocked"
    assert result["action"] == "hold"
    assert service.entry_calls == []
    assert service.exit_calls == []


def test_armed_automatic_mode_places_one_candidate_and_never_retries_it(tmp_path: Path):
    service = _ExecutionService()
    controller = _controller(tmp_path, service)
    configured = controller.configure(
        {
            "mode": "automatic",
            "account_key": ACCOUNT_KEY,
            "arming_phrase": ARMING_PHRASE,
            "acknowledge_unattended_trading": True,
            "acknowledge_losses_possible": True,
        },
        now=NOW,
    )

    first = controller.run_once(trigger="manual", now=NOW)
    second = controller.run_once(trigger="manual", now=NOW)

    assert configured["session_armed"] is True
    assert first["status"] == "order_sent"
    assert second["status"] == "candidate_already_attempted"
    assert len(service.entry_calls) == 1
    assert controller.status(now=NOW)["today"]["orders_sent"] == 1


def test_existing_position_is_analyzed_before_entry_and_profit_exit_can_run(tmp_path: Path):
    holding = {
        "asset": "option",
        "symbol": "HYG",
        "option_id": "option-1",
        "quantity": 1,
        "action": "take_profit",
        "signals": ["profit_target"],
        "auto_exit_eligible": True,
    }
    service = _ExecutionService([holding])
    controller = _controller(tmp_path, service)
    controller.configure(
        {
            "mode": "automatic",
            "account_key": ACCOUNT_KEY,
            "arming_phrase": ARMING_PHRASE,
            "acknowledge_unattended_trading": True,
            "acknowledge_losses_possible": True,
        },
        now=NOW,
    )

    result = controller.run_once(trigger="manual", now=NOW)

    assert result["status"] == "order_sent"
    assert result["action"] == "take_profit"
    assert len(service.exit_calls) == 1
    assert service.entry_calls == []
    assert controller.status(now=NOW)["today"]["orders_sent"] == 1


def test_multiple_holdings_are_fully_reported_but_never_auto_sold(tmp_path: Path):
    holdings = [
        {
            "asset": "option",
            "symbol": symbol,
            "option_id": f"option-{index}",
            "quantity": 1,
            "action": "take_profit",
            "signals": ["profit_target"],
            "auto_exit_eligible": True,
        }
        for index, symbol in enumerate(("HYG", "HOOD"), start=1)
    ]
    service = _ExecutionService(holdings)
    controller = _controller(tmp_path, service)
    controller.configure(
        {
            "mode": "automatic",
            "account_key": ACCOUNT_KEY,
            "arming_phrase": ARMING_PHRASE,
            "acknowledge_unattended_trading": True,
            "acknowledge_losses_possible": True,
        },
        now=NOW,
    )

    result = controller.run_once(trigger="manual", now=NOW)

    assert result["action"] == "hold_for_manual_review"
    assert len(result["portfolio_analysis"]["holdings"]) == 2
    assert service.exit_calls == []
    assert service.entry_calls == []


def test_automatic_policy_must_be_rearmed_after_restart(tmp_path: Path):
    first = _controller(tmp_path, _ExecutionService())
    first.configure(
        {
            "mode": "automatic",
            "account_key": ACCOUNT_KEY,
            "arming_phrase": ARMING_PHRASE,
            "acknowledge_unattended_trading": True,
            "acknowledge_losses_possible": True,
        },
        now=NOW,
    )

    restarted = _controller(tmp_path, _ExecutionService())
    status = restarted.status(now=NOW)

    assert status["mode"] == "automatic"
    assert status["session_armed"] is False
    assert status["rearm_required_after_restart"] is True
