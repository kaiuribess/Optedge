# Purpose: Test fail-closed scoring for true LEAPS contracts used as swing trades.
import pytest

from optedge.leaps_swing import score_leaps_swing_candidate
from optedge.strategy_profile import LEAPS_EVIDENCE_LANE


def _candidate(**overrides):
    row = {
        "asset": "option",
        "symbol": "AAPL",
        "side": "call",
        "dte": 500,
        "delta": 0.67,
        "spread_pct": 0.04,
        "openInterest": 2_000,
        "volume": 100,
        "confidence": 82,
        "after_cost_edge_pct": 0.05,
        "quote_quality": "live_or_broker",
        "mid": 5.0,
        "planned_hold_sessions": 10,
    }
    row.update(overrides)
    return row


def test_clean_candidate_is_execution_ready_for_manual_review_only():
    result = score_leaps_swing_candidate(
        _candidate(),
        quote_age_seconds=30,
        account_budget=1_000,
    )

    assert result["status"] == "execution_ready"
    assert result["execution_ready"] is True
    assert result["research_only"] is False
    assert result["quality_score"] == 100
    assert result["execution_score"] == 100
    assert result["hard_blockers"] == []
    assert result["data_blockers"] == []
    assert result["evidence_lane"] == LEAPS_EVIDENCE_LANE
    assert result["does_not_place_orders"] is True
    assert result["management_references"] == {
        "stop_loss_fraction": 0.25,
        "target_gain_fraction": 0.35,
        "breakeven_review_trigger_fraction": 0.20,
        "manual_management_only": True,
    }


@pytest.mark.parametrize(
    ("changes", "quote_age", "expected_blocker"),
    [
        ({"quote_quality": "free_or_delayed"}, 30, "not broker-live"),
        ({"quote_quality": ""}, 30, "quote quality is missing"),
        ({"delta": None}, 30, "delta is missing"),
        ({}, None, "quote age is missing"),
        ({}, 121, "older than 120 seconds"),
    ],
)
def test_missing_or_delayed_market_data_stays_research_only(
    changes,
    quote_age,
    expected_blocker,
):
    result = score_leaps_swing_candidate(
        _candidate(**changes),
        quote_age_seconds=quote_age,
        account_budget=1_000,
    )

    assert result["status"] == "research_only"
    assert result["execution_ready"] is False
    assert result["research_only"] is True
    assert result["hard_blockers"] == []
    assert result["execution_score"] == 0
    assert expected_blocker in " | ".join(result["data_blockers"])


def test_contract_dte_and_planned_hold_are_distinct_controls():
    result = score_leaps_swing_candidate(
        _candidate(dte=500, planned_hold_sessions=10),
        quote_age_seconds=30,
        account_budget=1_000,
    )

    assert result["status"] == "execution_ready"
    assert result["contract_policy"]["dte"] == 500
    assert result["holding_policy"]["planned_hold_sessions"] == 10
    assert result["holding_policy"]["review_sessions"] == [3, 5, 10]
    assert result["holding_policy"]["max_hold_sessions"] == 20
    assert result["holding_policy"]["contract_dte_is_not_hold_time"] is True

    too_long = score_leaps_swing_candidate(
        _candidate(dte=500, planned_hold_sessions=21),
        quote_age_seconds=30,
        account_budget=1_000,
    )
    assert too_long["status"] == "blocked"
    assert "whole 1-20 sessions" in " | ".join(too_long["hard_blockers"])


@pytest.mark.parametrize(
    ("changes", "expected_blocker"),
    [
        ({"dte": 364}, "outside the true-LEAPS window"),
        ({"dte": 901}, "outside the true-LEAPS window"),
        ({"delta": 0.90}, "absolute delta"),
        ({"spread_pct": 0.101}, "exceeds the 10% hard cap"),
        ({"openInterest": 249}, "open interest"),
        ({"openInterest": 300, "volume": 9}, "requires daily volume >= 10"),
        ({"confidence": 64.9}, "confidence"),
        ({"after_cost_edge_pct": 0.0}, "must be positive"),
    ],
)
def test_hard_policy_failures_cannot_be_offset_by_other_high_scores(
    changes,
    expected_blocker,
):
    result = score_leaps_swing_candidate(
        _candidate(**changes),
        quote_age_seconds=10,
        account_budget=1_000,
    )

    assert result["status"] == "blocked"
    assert result["execution_ready"] is False
    assert result["execution_score"] == 0
    assert result["quality_score"] >= 70
    assert expected_blocker in " | ".join(result["hard_blockers"])


def test_deep_open_interest_can_be_researchable_without_same_day_volume():
    result = score_leaps_swing_candidate(
        _candidate(openInterest=500, volume=0, delta=-0.65),
        quote_age_seconds=60,
        account_budget=1_000,
    )

    assert result["status"] == "execution_ready"
    assert result["contract_policy"]["abs_delta"] == 0.65
    assert any("deep open interest" in warning for warning in result["warnings"])


def test_supplied_account_budget_enforces_one_contract_full_debit():
    result = score_leaps_swing_candidate(
        _candidate(mid=6.0),
        quote_age_seconds=30,
        account_budget=599,
    )

    assert result["status"] == "blocked"
    assert result["execution_score"] == 0
    assert "one-contract debit $600.00" in " | ".join(result["hard_blockers"])


def test_account_budget_is_optional_for_contract_research():
    result = score_leaps_swing_candidate(
        _candidate(),
        quote_age_seconds=30,
    )

    assert result["status"] == "execution_ready"
    assert any("downstream sizing" in warning for warning in result["warnings"])
