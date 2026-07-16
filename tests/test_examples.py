# Purpose: Test example validation output against the current schema.
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_validation_summary_example_matches_current_top_level_contract() -> None:
    example = json.loads(
        (ROOT / "examples" / "validation_summary.example.json").read_text(encoding="utf-8")
    )
    required = {
        "generated_at",
        "validation_scope",
        "validation_scope_basis",
        "validation_basis",
        "swing_eligible_closed_positions",
        "swing_eligible_overall",
        "swing_eligible_after_slippage",
        "equity_curve",
        "overall",
        "after_slippage",
        "assets",
        "exit_effectiveness",
        "factor_ic_basis",
        "factor_ic",
        "position_aging",
        "random_baseline",
        "fixed_horizon",
        "warnings",
    }

    assert required <= set(example)
    assert example["fixed_horizon"]["methodology_version"] == 7
    assert set(example["assets"]) == {"option", "share", "futures"}
