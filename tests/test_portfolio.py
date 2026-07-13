# Purpose: Verify fail-closed same-account broker portfolio exposure controls.
from __future__ import annotations

from copy import deepcopy

from risk.portfolio import (
    evaluate_post_trade_portfolio,
    summarize_broker_account_capital_at_risk,
)


def _snapshot() -> dict:
    return {
        "normalization_blockers": [],
        "accounts": [
            {"account_key": "agentic"},
            {"account_key": "other"},
        ],
        "option_positions": [
            {
                "account_key": "agentic",
                "symbol": "AAPL",
                "option_type": "call",
                "position_type": "long",
                "strike_price": 200,
                "expiration_date": "2027-01-15",
                "quantity": 2,
                "signed_quantity": 2,
                "trade_value_multiplier": 100,
                "mark_price": 1.20,
                "current_price": 1.20,
                "ask_price": 1.30,
            },
            {
                "account_key": "other",
                "symbol": "MSFT",
                "option_type": "put",
                "position_type": "short",
                "strike_price": 500,
                "expiration_date": "2027-01-15",
                "quantity": 99,
                "signed_quantity": -99,
                "trade_value_multiplier": 100,
            },
            {"quantity": 0},
        ],
        "equity_positions": [
            {
                "account_key": "agentic",
                "symbol": "HOOD",
                "position_type": "long",
                "quantity": 5,
                "signed_quantity": 5,
                "market_value": 100,
                "current_price": 19,
            },
            {
                "account_key": "other",
                "symbol": "SPY",
                "position_type": "short",
                "quantity": 10,
                "signed_quantity": -10,
            },
        ],
        "option_orders": [
            {"account_key": "agentic", "state": "filled", "quantity": "bad"},
            {"state": "queued", "quantity": 0},
        ],
        "equity_orders": [],
    }


def test_summary_uses_conservative_long_exposure_for_only_requested_account():
    summary = summarize_broker_account_capital_at_risk(
        _snapshot(), "agentic", asof="2026-07-13"
    )

    assert summary["status"] == "ready"
    assert summary["capital_at_risk_dollars"] == 360.0
    assert summary["option_capital_at_risk_dollars"] == 260.0
    assert summary["equity_capital_at_risk_dollars"] == 100.0
    assert summary["position_count"] == 2
    assert summary["positions"][0]["conservative_price"] == 1.3
    assert summary["positions"][0]["price_basis"] == "max_valid_ask_mark_or_current"


def test_equity_falls_back_to_absolute_quantity_times_current_price():
    snapshot = _snapshot()
    snapshot["option_positions"] = []
    snapshot["equity_positions"][0]["market_value"] = None
    snapshot["equity_positions"][0]["current_price"] = 21.5

    summary = summarize_broker_account_capital_at_risk(snapshot, "agentic", "2026-07-13")

    assert summary["eligible"] is True
    assert summary["capital_at_risk_dollars"] == 107.5
    assert summary["positions"][0]["price_basis"] == "absolute_quantity_times_current_price"


def test_equity_market_value_must_reconcile_with_quantity_times_current_price():
    snapshot = _snapshot()
    snapshot["option_positions"] = []
    snapshot["equity_positions"] = [{
        "account_key": "agentic",
        "symbol": "AAPL",
        "position_type": "long",
        "quantity": 100,
        "signed_quantity": 100,
        "market_value": 100,
        "current_price": 100,
    }]

    summary = summarize_broker_account_capital_at_risk(snapshot, "agentic", "2026-07-13")

    assert summary["status"] == "blocked"
    assert summary["capital_at_risk_dollars"] is None
    assert any("does not reconcile" in blocker for blocker in summary["blockers"])


def test_unscoped_nonzero_position_fails_closed_but_zero_position_is_ignored():
    snapshot = _snapshot()
    snapshot["equity_positions"].append(
        {
            "symbol": "NVDA",
            "position_type": "long",
            "quantity": 1,
            "signed_quantity": 1,
            "market_value": 150,
        }
    )

    summary = summarize_broker_account_capital_at_risk(snapshot, "agentic", "2026-07-13")

    assert summary["eligible"] is False
    assert summary["capital_at_risk_dollars"] is None
    assert any("not account-scoped" in blocker for blocker in summary["blockers"])


def test_short_or_ambiguous_positions_fail_closed():
    for asset, position in (
        (
            "option_positions",
            {
                "account_key": "agentic",
                "symbol": "AAPL",
                "option_type": "put",
                "position_type": "short",
                "strike_price": 150,
                "expiration_date": "2027-01-15",
                "quantity": 1,
                "signed_quantity": -1,
                "trade_value_multiplier": 100,
                "mark_price": 2,
            },
        ),
        (
            "equity_positions",
            {
                "account_key": "agentic",
                "symbol": "TSLA",
                "position_type": "short",
                "quantity": 2,
                "signed_quantity": -2,
                "market_value": -600,
            },
        ),
    ):
        snapshot = _snapshot()
        snapshot["option_positions"] = []
        snapshot["equity_positions"] = []
        snapshot[asset] = [position]

        summary = summarize_broker_account_capital_at_risk(snapshot, "agentic", "2026-07-13")

        assert summary["status"] == "blocked"
        assert any("short" in blocker for blocker in summary["blockers"])


def test_option_mark_expiry_and_multiplier_are_strict():
    for field, value, message in (
        ("mark_price", None, "current mark"),
        ("expiration_date", "not-a-date", "expiration"),
        ("expiration_date", "2026-01-01", "expired"),
        ("trade_value_multiplier", None, "100-share multiplier"),
        ("trade_value_multiplier", 10, "100-share multiplier"),
    ):
        snapshot = _snapshot()
        snapshot["equity_positions"] = []
        row = snapshot["option_positions"][0]
        row[field] = value
        if field == "mark_price":
            row["current_price"] = None

        summary = summarize_broker_account_capital_at_risk(snapshot, "agentic", "2026-07-13")

        assert summary["status"] == "blocked"
        assert any(message in blocker for blocker in summary["blockers"])


def test_only_nonterminal_same_account_orders_block():
    snapshot = _snapshot()
    snapshot["option_orders"].extend(
        [
            {"account_key": "other", "state": "queued", "quantity": 20},
            {"account_key": "agentic", "state": "cancelled", "quantity": 20},
            {"account_key": "agentic", "state": "queued", "quantity": 1},
        ]
    )

    blocked = summarize_broker_account_capital_at_risk(snapshot, "agentic", "2026-07-13")
    assert blocked["status"] == "blocked"
    assert blocked["same_account_nonterminal_order_count"] == 1

    snapshot["option_orders"][-1]["state"] = "filled"
    ready = summarize_broker_account_capital_at_risk(snapshot, "agentic", "2026-07-13")
    assert ready["status"] == "ready"


def test_pending_option_transitions_fail_closed_before_exposure_math():
    for field, value in (
        ("pending_buy_quantity", 1),
        ("pending_sell_quantity", 1),
        ("pending_exercise_quantity", 1),
        ("pending_assignment_quantity", 1),
        ("pending_expiration_quantity", 1),
        ("pending_assignment_quantity", "unknown"),
    ):
        snapshot = _snapshot()
        snapshot["option_positions"][0][field] = value

        summary = summarize_broker_account_capital_at_risk(
            snapshot,
            "agentic",
            "2026-07-13",
        )

        assert summary["status"] == "blocked"
        assert any("pending" in blocker for blocker in summary["blockers"])


def test_pending_option_transition_in_another_account_does_not_contaminate_scope():
    snapshot = _snapshot()
    snapshot["option_positions"][1]["pending_assignment_quantity"] = 1

    summary = summarize_broker_account_capital_at_risk(
        snapshot,
        "agentic",
        "2026-07-13",
    )

    assert summary["status"] == "ready"
    assert summary["capital_at_risk_dollars"] == 360.0
    assert not any("pending" in blocker for blocker in summary["blockers"])


def test_normalization_blockers_or_duplicate_account_scope_fail_closed():
    snapshot = _snapshot()
    snapshot["normalization_blockers"] = ["capture incomplete"]
    snapshot["accounts"].append({"account_key": "agentic"})

    summary = summarize_broker_account_capital_at_risk(snapshot, "agentic", "2026-07-13")

    assert summary["status"] == "blocked"
    assert any("normalization" in blocker for blocker in summary["blockers"])
    assert any("exactly one" in blocker for blocker in summary["blockers"])


def test_post_trade_gate_uses_lower_equity_and_returns_attestation_arithmetic():
    summary = summarize_broker_account_capital_at_risk(
        _snapshot(), "agentic", asof="2026-07-13"
    )

    gate = evaluate_post_trade_portfolio(
        summary,
        proposed_capital_at_risk=100,
        assumed_equity=5_000,
        live_equity=4_000,
        allocation_fraction=0.20,
    )

    assert gate["status"] == "allowed"
    assert gate["equity_basis_dollars"] == 4_000.0
    assert gate["allocation_cap_dollars"] == 800.0
    assert gate["current_capital_at_risk_dollars"] == 360.0
    assert gate["proposed_capital_at_risk_dollars"] == 100.0
    assert gate["post_trade_capital_at_risk_dollars"] == 460.0
    assert gate["headroom_before_trade_dollars"] == 440.0
    assert gate["headroom_after_trade_dollars"] == 340.0
    assert gate["utilization_after"] == 0.575
    assert gate["exposure_schema"] == "optedge_broker_portfolio_exposure_v1"
    assert gate["position_count"] == 2
    assert gate["same_account_nonterminal_order_count"] == 0


def test_post_trade_gate_blocks_cap_breach_and_blocked_exposure():
    summary = summarize_broker_account_capital_at_risk(
        _snapshot(), "agentic", asof="2026-07-13"
    )
    over_cap = evaluate_post_trade_portfolio(
        summary,
        proposed_capital_at_risk=500,
        assumed_equity=4_000,
        live_equity=4_000,
        allocation_fraction=0.20,
    )
    assert over_cap["allowed"] is False
    assert over_cap["post_trade_capital_at_risk_dollars"] == 860.0
    assert over_cap["headroom_after_trade_dollars"] == -60.0

    blocked_summary = deepcopy(summary)
    blocked_summary["status"] = "blocked"
    blocked_summary["eligible"] = False
    blocked_summary["capital_at_risk_dollars"] = None
    fail_closed = evaluate_post_trade_portfolio(
        blocked_summary,
        proposed_capital_at_risk=10,
        assumed_equity=4_000,
        live_equity=4_000,
        allocation_fraction=0.20,
    )
    assert fail_closed["allowed"] is False
    assert any("exposure summary" in blocker for blocker in fail_closed["blockers"])


def test_post_trade_gate_extracts_option_or_share_capital_from_trade_plan():
    empty = _snapshot()
    empty["option_positions"] = []
    empty["equity_positions"] = []
    summary = summarize_broker_account_capital_at_risk(empty, "agentic", "2026-07-13")

    for plan in (
        {
            "asset": "option",
            "status": "ready_for_manual_review",
            "is_actionable": True,
            "validation": {"ok": True, "errors": [], "warnings": []},
            "order": {"estimated_debit_dollars": 200},
            "risk": {
                "planned_max_loss_dollars": 200,
                "full_option_debit_at_risk_dollars": 200,
                "max_loss_is_unbounded": False,
            },
        },
        {
            "asset": "share",
            "status": "ready_for_manual_review",
            "is_actionable": True,
            "validation": {"ok": True, "errors": [], "warnings": []},
            "order": {"estimated_notional_dollars": 200},
            "risk": {
                "full_share_notional_at_risk_dollars": 200,
                "planned_max_loss_dollars": 20,
                "max_loss_is_unbounded": False,
            },
        },
    ):
        gate = evaluate_post_trade_portfolio(
            summary,
            plan,
            assumed_equity=2_000,
            live_equity=1_500,
            allocation_fraction=0.20,
        )
        assert gate["allowed"] is True
        assert gate["proposed_capital_at_risk_dollars"] == 200.0

    unsafe_plan = {
        "asset": "share",
        "status": "ready_for_manual_review",
        "is_actionable": "false",
        "validation": {"ok": True, "errors": []},
        "order": {"estimated_notional_dollars": 200},
        "risk": {
            "full_share_notional_at_risk_dollars": 200,
            "max_loss_is_unbounded": False,
        },
    }
    strict_gate = evaluate_post_trade_portfolio(
        summary,
        unsafe_plan,
        assumed_equity=2_000,
        live_equity=1_500,
        allocation_fraction=0.20,
    )
    assert strict_gate["allowed"] is False
    assert any("not actionable" in blocker for blocker in strict_gate["blockers"])

    truthy_eligible = deepcopy(summary)
    truthy_eligible["eligible"] = "false"
    strict_exposure_gate = evaluate_post_trade_portfolio(
        truthy_eligible,
        proposed_capital_at_risk=10,
        assumed_equity=2_000,
        live_equity=1_500,
        allocation_fraction=0.20,
    )
    assert strict_exposure_gate["allowed"] is False
    assert any("exposure summary" in blocker for blocker in strict_exposure_gate["blockers"])
