import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from risk.research_guard import apply_to_recommendations, build_guard_report

import pandas as pd


def test_guard_warns_on_small_sample():
    report = build_guard_report({"closed_positions": 12, "after_slippage": {"win_rate": 0.55}})
    assert report["status"] == "review"
    assert any(w["code"] == "sample_size" for w in report["warnings"])


def test_guard_blocks_wide_option_spread():
    df = pd.DataFrame(
        [{
            "ticker": "XYZ",
            "spread_pct": 0.20,
            "trade_status": "Trade",
            "is_actionable": True,
            "suggested_contracts": 2,
            "suggested_dollars": 500,
            "actual_dollars": 420,
        }]
    )
    guarded, _ = apply_to_recommendations(df, guard_report={"status": "clear", "warnings": []})
    assert guarded.loc[0, "trade_status"] == "Watch"
    assert guarded.loc[0, "is_actionable"] is False or guarded.loc[0, "is_actionable"] == 0
    assert guarded.loc[0, "suggested_contracts"] == 0


if __name__ == "__main__":
    test_guard_warns_on_small_sample()
    test_guard_blocks_wide_option_spread()
    print("2/2 research guard tests passed")
