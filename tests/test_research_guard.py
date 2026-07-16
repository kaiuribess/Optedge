# Purpose: Test research blocking without evidence deadlocks.
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from backtest.sizing import add_pre_guard_qualification  # noqa: E402
from risk.research_guard import apply_to_recommendations, build_guard_report  # noqa: E402


def test_guard_warns_on_small_sample():
    report = build_guard_report({"closed_positions": 12, "after_slippage": {"win_rate": 0.55}})
    assert report["status"] == "review"
    assert any(w["code"] == "sample_size" for w in report["warnings"])


def test_guard_blocks_wide_option_spread():
    df = pd.DataFrame(
        [
            {
                "ticker": "XYZ",
                "spread_pct": 0.20,
                "trade_status": "Trade",
                "is_actionable": True,
                "suggested_contracts": 2,
                "suggested_dollars": 500,
                "actual_dollars": 420,
            }
        ]
    )
    guarded, _ = apply_to_recommendations(df, guard_report={"status": "clear", "warnings": []})
    assert guarded.loc[0, "trade_status"] == "Watch"
    assert guarded.loc[0, "is_actionable"] is False or guarded.loc[0, "is_actionable"] == 0
    assert guarded.loc[0, "suggested_contracts"] == 0


def test_guard_uses_independent_swing_sample_when_available():
    report = build_guard_report(
        {
            "closed_positions": 1000,
            "after_slippage": {
                "n": 1000,
                "win_rate": 0.75,
                "max_drawdown": -0.05,
            },
            "swing_eligible_closed_positions": 259,
            "swing_eligible_after_slippage": {
                "n": 259,
                "win_rate": 0.41,
                "max_drawdown": -0.75,
            },
        }
    )
    assert report["closed_signals"] == 259
    assert report["validation_basis"] == "executable_swing_after_slippage"
    assert report["status"] == "blocked"
    assert any(w["code"] == "sample_size" for w in report["warnings"])
    assert any(w["code"] == "drawdown" for w in report["warnings"])


def test_guard_tracks_shadow_fixed_horizon_without_creating_evidence_deadlock():
    small = build_guard_report(
        {
            "closed_positions": 500,
            "after_slippage": {"n": 500, "win_rate": 0.55, "max_drawdown": -0.05},
            "fixed_horizon": {
                "headline_shadow": {"n": 20, "unique_entry_days": 4},
            },
        }
    )
    warning = next(item for item in small["warnings"] if item["code"] == "fixed_horizon_sample")
    assert warning["blocks_trading"] is False

    adverse = build_guard_report(
        {
            "closed_positions": 500,
            "after_slippage": {"n": 500, "win_rate": 0.55, "max_drawdown": -0.05},
            "fixed_horizon": {
                "headline_shadow": {
                    "n": 120,
                    "unique_entry_days": 12,
                    "avg_return": -0.01,
                    "avg_excess_vs_spy": -0.02,
                },
            },
        }
    )
    assert adverse["status"] == "blocked"
    assert any(item["code"] == "fixed_horizon_return" for item in adverse["warnings"])
    assert any(item["code"] == "fixed_horizon_benchmark" for item in adverse["warnings"])


def test_blocked_guard_preserves_pre_guard_shadow_qualification():
    original = pd.DataFrame(
        [
            {
                "ticker": "XYZ",
                "trade_status": "Trade",
                "is_actionable": True,
                "suggested_contracts": 2,
                "buyer_edge_pct": 0.08,
            }
        ]
    )
    marked = add_pre_guard_qualification(original, asset="option")
    guarded, _ = apply_to_recommendations(
        marked,
        guard_report={
            "status": "blocked",
            "warnings": [{"message": "validation blocked"}],
        },
    )
    assert bool(guarded.loc[0, "strategy_qualified_pre_guard"])
    assert guarded.loc[0, "pre_guard_suggested_contracts"] == 2
    assert guarded.loc[0, "trade_status"] == "Watch"
    assert guarded.loc[0, "suggested_contracts"] == 0


if __name__ == "__main__":
    test_guard_warns_on_small_sample()
    test_guard_blocks_wide_option_spread()
    test_guard_uses_independent_swing_sample_when_available()
    test_guard_tracks_shadow_fixed_horizon_without_creating_evidence_deadlock()
    test_blocked_guard_preserves_pre_guard_shadow_qualification()
    print("5/5 research guard tests passed")
