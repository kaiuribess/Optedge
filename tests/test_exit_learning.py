# Purpose: Test evidence-gated exit-threshold learning.
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest import exit_learning


def test_default_policy_when_sample_too_small():
    assert not exit_learning.enough_data_for_learning(
        "share",
        pd.DataFrame([{"asset": "share"}]),
        pd.DataFrame([{"asset": "share"}]),
    )
    policy = exit_learning.get_policy_for_asset("share")
    assert policy["close_pressure_threshold"] == 80


def test_thresholds_are_clamped_and_step_limited():
    clamped = exit_learning._clamp_thresholds({
        "watch_pressure_threshold": 10,
        "tighten_pressure_threshold": 10,
        "close_pressure_threshold": 100,
    })
    assert 35 <= clamped["watch_pressure_threshold"] <= 55
    assert 55 <= clamped["tighten_pressure_threshold"] <= 75
    assert 70 <= clamped["close_pressure_threshold"] <= 90
    assert exit_learning._step(80, 50) == 75
    assert exit_learning._step(80, 100) == 85


def test_malformed_policy_falls_back_to_defaults():
    with tempfile.TemporaryDirectory() as td:
        old_file = exit_learning.POLICY_FILE
        exit_learning.POLICY_FILE = Path(td) / "exit_policy.json"
        exit_learning.POLICY_FILE.write_text("{bad")
        try:
            policy = exit_learning.load_exit_policy()
            assert policy["assets"]["option"]["close_pressure_threshold"] == 80
        finally:
            exit_learning.POLICY_FILE = old_file


def _learning_frames(*, same_scan: bool) -> tuple[pd.DataFrame, pd.DataFrame]:
    base = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)
    closed = []
    for index in range(120):
        entry = base + timedelta(days=index % 12, minutes=index)
        exit_time = entry if same_scan else entry + timedelta(hours=2)
        closed.append({
            "asset": "option",
            "position_id": f"option-{index}",
            "entry_is_actionable": True,
            "trade_status": "Trade",
            "research_guard_status": "review",
            "suggested_contracts": 1,
            "entry_time": entry.isoformat(),
            "exit_time": exit_time.isoformat(),
            "exit_reason": "dynamic_exit",
            "pnl_pct": -0.05,
        })
    reviews = []
    for index in range(24):
        timestamp = base + timedelta(days=index % 12, hours=4)
        reviews.append({
            "asset": "option",
            "timestamp": timestamp.isoformat(),
            "action": "hold",
            "exit_pressure": 20,
        })
    return pd.DataFrame(closed), pd.DataFrame(reviews)


def test_same_scan_dynamic_churn_does_not_activate_learning():
    closed, reviews = _learning_frames(same_scan=True)
    assert exit_learning.eligible_closed_for_learning("option", closed).empty
    assert not exit_learning.enough_data_for_learning("option", closed, reviews)


def test_independent_multi_day_outcomes_can_activate_learning():
    closed, reviews = _learning_frames(same_scan=False)
    eligible = exit_learning.eligible_closed_for_learning("option", closed)
    assert len(eligible) == 120
    assert exit_learning.enough_data_for_learning("option", closed, reviews)


def test_non_executable_rows_are_excluded_from_learning():
    rows = pd.DataFrame([
        {"asset": "option", "position_id": "trade", "trade_status": "Trade",
         "entry_trade_status": "Trade", "suggested_contracts": 1,
         "research_guard_status": "review", "entry_research_guard_status": "review",
         "entry_is_actionable": True, "pnl_pct": 0.1},
        {"asset": "option", "position_id": "watch", "trade_status": "Watch",
         "suggested_contracts": 1, "research_guard_status": "review", "pnl_pct": 0.1},
        {"asset": "option", "position_id": "zero", "trade_status": "Trade",
         "suggested_contracts": 0, "research_guard_status": "review", "pnl_pct": 0.1},
        {"asset": "option", "position_id": "blocked", "trade_status": "Trade",
         "suggested_contracts": 1, "research_guard_status": "blocked", "pnl_pct": 0.1},
        {"asset": "option", "position_id": "explicit", "trade_status": "Trade",
         "suggested_contracts": 1, "research_guard_status": "review",
         "entry_is_actionable": False, "pnl_pct": 0.1},
    ])
    eligible = exit_learning.eligible_closed_for_learning("option", rows)
    summary = exit_learning.execution_eligibility_summary("option", rows)
    assert eligible["position_id"].tolist() == ["trade"]
    assert summary["execution_eligible_closed_positions"] == 1
    assert summary["non_executable_closed_positions"] == 4
    assert summary["excluded_non_actionable_status"] == 1
    assert summary["excluded_non_positive_size"] == 1
    assert summary["excluded_guard_blocked"] == 1
    assert summary["excluded_explicit_not_actionable"] == 1


def test_unresolved_or_nonfinite_outcomes_are_excluded_from_learning():
    base = {
        "asset": "option",
        "trade_status": "Trade",
        "suggested_contracts": 1,
        "research_guard_status": "review",
        "entry_time": "2026-01-02T15:00:00+00:00",
        "exit_time": "2026-01-03T15:00:00+00:00",
        "exit_reason": "expired",
    }
    rows = pd.DataFrame([
        {**base, "position_id": "resolved", "pnl_pct": 0.2, "validation_eligible": True},
        {**base, "position_id": "unresolved", "pnl_pct": None, "validation_eligible": False},
        {**base, "position_id": "missing-pnl", "pnl_pct": None},
        {**base, "position_id": "infinite", "pnl_pct": float("inf")},
    ])

    eligible = exit_learning.eligible_closed_for_learning("option", rows)

    assert eligible["position_id"].tolist() == ["resolved"]


def test_insufficient_learning_resets_old_thresholds_to_defaults():
    current = {
        "learned_active": True,
        "watch_pressure_threshold": 35,
        "tighten_pressure_threshold": 55,
        "close_pressure_threshold": 70,
    }
    learned, reasons = exit_learning._learn_asset(
        "option",
        current,
        pd.DataFrame([{"asset": "option"}]),
        pd.DataFrame([{"asset": "option"}]),
    )
    assert learned["learned_active"] is False
    assert learned["close_pressure_threshold"] == 80
    assert "insufficient independent sample size" in reasons


def test_stale_policy_falls_back_to_defaults():
    with tempfile.TemporaryDirectory() as td:
        old_file = exit_learning.POLICY_FILE
        exit_learning.POLICY_FILE = Path(td) / "exit_policy.json"
        stale = exit_learning._deepcopy_default()
        stale["generated_at"] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        stale["policy_version"] = "stale-learned"
        stale["assets"]["option"]["learned_active"] = True
        stale["assets"]["option"]["close_pressure_threshold"] = 70
        exit_learning.POLICY_FILE.write_text(json.dumps(stale))
        try:
            policy = exit_learning.get_policy_for_asset("option")
            assert policy["learned_active"] is False
            assert policy["close_pressure_threshold"] == 80
            assert policy["policy_version"] == "default"
        finally:
            exit_learning.POLICY_FILE = old_file


if __name__ == "__main__":
    test_default_policy_when_sample_too_small()
    test_thresholds_are_clamped_and_step_limited()
    test_malformed_policy_falls_back_to_defaults()
    test_same_scan_dynamic_churn_does_not_activate_learning()
    test_independent_multi_day_outcomes_can_activate_learning()
    test_non_executable_rows_are_excluded_from_learning()
    test_insufficient_learning_resets_old_thresholds_to_defaults()
    test_stale_policy_falls_back_to_defaults()
    print("8/8 exit learning tests passed")
