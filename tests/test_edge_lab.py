# Purpose: Verify conservative, correlation-aware swing-edge evidence scoring.
from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import edge_lab as edge_lab_module
from backtest import fixed_horizon
from backtest.edge_lab import analyze_edge_outcomes, build_edge_lab, evidence_stats


def _outcomes(
    *,
    asset: str = "share",
    days: int = 320,
    rows_per_day: int = 1,
    positive: bool = True,
    executable: bool = True,
) -> pd.DataFrame:
    rows = []
    start = datetime(2026, 1, 2, 15, tzinfo=UTC)
    for day in range(days):
        for row_index in range(rows_per_day):
            if positive:
                raw_return = -0.004 if row_index == 0 and day % 20 == 0 else 0.032
            else:
                raw_return = 0.004 if row_index == 0 and day % 7 == 0 else -0.022
            slippage = 0.002
            outcome = {
                "asset": asset,
                "horizon_sessions": 10,
                "entry_time": (start + timedelta(days=day)).isoformat(),
                "pnl_pct": raw_return,
                "slippage_assumption_pct": slippage,
                "spread_pct": slippage if asset == "option" else None,
                "pnl_pct_after_slippage": raw_return - slippage,
                "excess_vs_spy_pct": raw_return - slippage - 0.003,
                "is_scored": True,
                "is_independent": True,
                "eligible_for_executable_metrics": executable,
                "eligible_for_shadow_metrics": False,
                "outcome_quality": (
                    "broker_market_observed" if asset == "option" else "market_observed"
                ),
                "outcome_id": f"{asset}-{day}-{row_index}:10",
                "independent_key": f"{asset}|SYM{row_index}|long|{day}",
                "methodology_version": fixed_horizon.METHODOLOGY_VERSION,
                "resolution_status": "scored",
                "resolution_reason": "",
            }
            outcome.update(fixed_horizon.current_evidence_provenance())
            rows.append(outcome)
    return pd.DataFrame(rows)


def test_positive_current_method_evidence_can_clear_every_live_gate():
    report = analyze_edge_outcomes(_outcomes())
    share = next(row for row in report["asset_rows"] if row["asset"] == "share")

    assert share["status"] == "validated"
    assert share["live_capital_eligible"] is True
    assert share["requirements_met"] == share["requirements_total"]
    assert report["live_capital_eligible"] is True
    assert report["validated_assets"] == ["share"]


def test_legacy_research_cannot_authorize_live_capital_even_when_positive():
    report = analyze_edge_outcomes(_outcomes(executable=False))
    share = next(row for row in report["asset_rows"] if row["asset"] == "share")

    assert share["evidence_lane"] == "legacy_research_only"
    assert share["status"] == "promising"
    assert share["live_capital_eligible"] is False
    assert report["status"] == "paper_only"


def test_adverse_after_cost_evidence_is_explicitly_blocked():
    report = analyze_edge_outcomes(_outcomes(positive=False))
    share = next(row for row in report["asset_rows"] if row["asset"] == "share")

    assert share["status"] == "adverse"
    assert share["avg_return_after_costs"] < 0
    assert share["profit_factor"] < 1
    assert "After-cost mean" in share["primary_blocker"]
    assert report["status"] == "blocked"


def test_same_day_duplicates_do_not_inflate_independent_entry_days():
    frame = _outcomes(days=2, rows_per_day=500)
    stats = evidence_stats(frame)

    assert stats["n"] == 1000
    assert stats["unique_entry_days"] == 2
    assert stats["signals_per_entry_day"] == 500


def test_evidence_lane_freezes_current_provenance_once(monkeypatch):
    frame = _outcomes(days=12)
    expected = fixed_horizon.current_evidence_provenance()
    calls = 0

    def current_provenance():
        nonlocal calls
        calls += 1
        return expected

    monkeypatch.setattr(edge_lab_module, "current_evidence_provenance", current_provenance)
    lane, selected = edge_lab_module._evidence_lane(frame)

    assert calls == 1
    assert lane == "current_method_executable"
    assert len(selected) == len(frame)


def test_options_require_broker_observed_outcome_coverage():
    frame = _outcomes(asset="option")
    frame["outcome_quality"] = "modeled_option_proxy"
    report = analyze_edge_outcomes(frame)
    option = next(row for row in report["asset_rows"] if row["asset"] == "option")

    coverage = next(
        row for row in option["requirements"] if row["code"] == "observed_option_coverage"
    )
    assert coverage["met"] is False
    assert option["modeled_proxy_coverage"] == 1
    assert option["live_capital_eligible"] is False


def test_option_cost_assumption_must_cover_every_recorded_entry_spread():
    frame = _outcomes(asset="option")
    frame["spread_pct"] = 0.12

    report = analyze_edge_outcomes(frame)
    option = next(row for row in report["asset_rows"] if row["asset"] == "option")
    requirement = next(
        row for row in option["requirements"] if row["code"] == "cost_covers_entry_spread_coverage"
    )

    assert requirement["met"] is False
    assert requirement["actual"] == 0.0
    assert option["live_capital_eligible"] is False


def test_missing_fixed_horizon_source_fails_closed():
    with tempfile.TemporaryDirectory() as td:
        report = build_edge_lab(Path(td))

    assert report["status"] == "unavailable"
    assert report["live_capital_eligible"] is False
    assert "fixed_horizon_outcomes.parquet" in report["primary_blocker"]


def _requirement(row: dict, code: str) -> dict:
    return next(item for item in row["requirements"] if item["code"] == code)


def test_missing_required_columns_return_structured_unavailable_result():
    report = analyze_edge_outcomes(pd.DataFrame({"asset": ["share"]}))

    assert report["status"] == "unavailable"
    assert report["live_capital_eligible"] is False
    assert any("missing required column" in item for item in report["validation_errors"])


def test_missing_or_negative_costs_and_partial_benchmark_coverage_fail_closed():
    missing_cost = _outcomes()
    missing_cost["slippage_assumption_pct"] = float("nan")
    missing_report = analyze_edge_outcomes(missing_cost)
    missing_share = next(row for row in missing_report["asset_rows"] if row["asset"] == "share")
    assert _requirement(missing_share, "slippage_coverage")["met"] is False
    assert _requirement(missing_share, "double_costs")["met"] is False
    assert missing_share["live_capital_eligible"] is False

    negative_cost = _outcomes()
    negative_cost["slippage_assumption_pct"] = -0.002
    negative_cost["pnl_pct_after_slippage"] = negative_cost["pnl_pct"] + 0.002
    negative_report = analyze_edge_outcomes(negative_cost)
    negative_share = next(row for row in negative_report["asset_rows"] if row["asset"] == "share")
    assert _requirement(negative_share, "nonnegative_slippage_coverage")["met"] is False

    partial_benchmark = _outcomes()
    partial_benchmark["excess_vs_spy_pct"] = float("nan")
    partial_benchmark.loc[partial_benchmark.index[0], "excess_vs_spy_pct"] = 0.01
    benchmark_report = analyze_edge_outcomes(partial_benchmark)
    benchmark_share = next(row for row in benchmark_report["asset_rows"] if row["asset"] == "share")
    assert _requirement(benchmark_share, "spy_excess_coverage")["met"] is False
    assert _requirement(benchmark_share, "spy_excess")["met"] is False


def test_after_cost_arithmetic_must_reconcile_for_every_gate_row():
    frame = _outcomes()
    frame["pnl_pct_after_slippage"] += 0.01
    report = analyze_edge_outcomes(frame)
    share = next(row for row in report["asset_rows"] if row["asset"] == "share")

    assert _requirement(share, "cost_reconciliation_coverage")["met"] is False
    assert share["live_capital_eligible"] is False


def test_retired_strategy_and_duplicate_evidence_cannot_be_current():
    retired = _outcomes()
    retired["strategy_version"] = "retired-strategy"
    report = analyze_edge_outcomes(retired)
    share = next(row for row in report["asset_rows"] if row["asset"] == "share")
    assert share["evidence_lane"] == "legacy_research_only"
    assert share["live_capital_eligible"] is False

    duplicated = pd.concat([_outcomes(), _outcomes().iloc[[0]]], ignore_index=True)
    duplicate_report = analyze_edge_outcomes(duplicated)
    assert duplicate_report["status"] == "unavailable"
    assert any("duplicate" in item for item in duplicate_report["validation_errors"])


def test_independent_key_horizon_duplicates_fail_closed_even_with_unique_outcome_ids():
    frame = _outcomes()
    duplicate = frame.iloc[[0]].copy()
    duplicate["outcome_id"] = "different-outcome-id:10"
    frame = pd.concat([frame, duplicate], ignore_index=True)

    report = analyze_edge_outcomes(frame)

    assert report["status"] == "unavailable"
    assert any("independent_key+horizon_sessions" in item for item in report["validation_errors"])


def test_horizon_length_blocks_prevent_overlapping_windows_from_looking_independent():
    frame = _outcomes(days=40, rows_per_day=10)
    regimes = (-0.03, 0.08, -0.03, 0.08)
    for day in range(40):
        indexes = frame.index[day * 10 : (day + 1) * 10]
        after_cost = regimes[day // 10]
        frame.loc[indexes, "pnl_pct"] = after_cost + 0.002
        frame.loc[indexes, "pnl_pct_after_slippage"] = after_cost
        frame.loc[indexes, "excess_vs_spy_pct"] = after_cost - 0.001
    report = analyze_edge_outcomes(frame)
    share = next(row for row in report["asset_rows"] if row["asset"] == "share")

    assert share["n"] == 400
    assert share["effective_horizon_blocks"] == 4
    assert share["daily_block_ci_90_low"] < 0
    assert _requirement(share, "effective_blocks")["met"] is False
    assert share["live_capital_eligible"] is False


def test_modeled_option_winners_cannot_override_losing_observed_outcomes():
    frame = _outcomes(asset="option", rows_per_day=4)
    for day in range(320):
        indexes = frame.index[day * 4 : (day + 1) * 4]
        observed = indexes[:2]
        modeled = indexes[2:]
        frame.loc[observed, "outcome_quality"] = "broker_market_observed"
        frame.loc[observed, "pnl_pct"] = -0.008
        frame.loc[observed, "pnl_pct_after_slippage"] = -0.01
        frame.loc[observed, "excess_vs_spy_pct"] = -0.011
        frame.loc[modeled, "outcome_quality"] = "modeled_option_proxy"
        frame.loc[modeled, "pnl_pct"] = 0.052
        frame.loc[modeled, "pnl_pct_after_slippage"] = 0.05
        frame.loc[modeled, "excess_vs_spy_pct"] = 0.049
    report = analyze_edge_outcomes(frame)
    option = next(row for row in report["asset_rows"] if row["asset"] == "option")

    assert option["live_metric_basis"] == "broker_market_observed_only"
    assert option["broker_observed_coverage"] == 0.5
    assert np.isclose(option["avg_return_after_costs"], -0.01)
    assert option["research_all_outcomes"]["avg_return_after_costs"] > 0
    assert option["live_capital_eligible"] is False


def test_excluded_or_pending_resolution_coverage_blocks_live_review():
    frame = _outcomes()
    frame.loc[frame.index[0], "is_scored"] = False
    frame.loc[frame.index[0], "resolution_status"] = "excluded"
    frame.loc[frame.index[0], "resolution_reason"] = "test_exclusion"
    report = analyze_edge_outcomes(frame)
    share = next(row for row in report["asset_rows"] if row["asset"] == "share")

    assert share["resolution_coverage"] < 1
    assert share["resolution_excluded"] == 1
    assert _requirement(share, "resolution_coverage")["met"] is False


def test_required_resolution_attestation_cannot_disagree_with_persisted_rows():
    frame = _outcomes()
    source = {
        "status": "current",
        "met": True,
        "reason": None,
        "requires_resolution_attestation": True,
    }
    inconsistent = [
        {
            "asset": "share",
            "horizon_sessions": 10,
            "evidence_lane": "current_method_executable",
            "expected": len(frame) + 1,
            "scored": len(frame) + 1,
            "excluded": 0,
            "pending": 0,
        }
    ]
    report = analyze_edge_outcomes(
        frame,
        source_attestation=source,
        resolution_coverage=inconsistent,
    )
    share = next(row for row in report["asset_rows"] if row["asset"] == "share")

    assert share["resolution_attested"] is False
    assert share["resolution_reconciled"] is False
    assert _requirement(share, "resolution_coverage")["met"] is False


def test_stale_policy_bound_source_is_reported_and_blocks_review():
    frame = _outcomes()
    now = datetime.now(UTC)
    stale = now - timedelta(hours=100)
    provenance = fixed_horizon.current_evidence_provenance()
    resolution = [
        {
            "asset": "share",
            "horizon_sessions": 10,
            "evidence_lane": "current_method_executable",
            "expected": len(frame),
            "scored": len(frame),
            "excluded": 0,
            "pending": 0,
            "resolution_coverage": 1.0,
            "exclusion_reasons": {},
        }
    ]
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        outcome_path = root / "fixed_horizon_outcomes.parquet"
        summary_path = root / "fixed_horizon_summary.json"
        frame.to_parquet(outcome_path, index=False)
        summary = {
            "generated_at": stale.isoformat(),
            "methodology_version": fixed_horizon.METHODOLOGY_VERSION,
            "headline_horizon_sessions": 10,
            **provenance,
            "outcomes_digest_sha256": fixed_horizon.outcome_set_digest(frame),
            "resolution_coverage": resolution,
        }
        summary_path.write_text(json.dumps(summary), encoding="utf-8")
        timestamp = stale.timestamp()
        os.utime(outcome_path, (timestamp, timestamp))
        os.utime(summary_path, (timestamp, timestamp))
        report = build_edge_lab(root, now=now)

    assert report["status"] == "blocked"
    assert report["source_attestation"]["met"] is False
    assert "maximum is 96h" in report["primary_blocker"]
    assert report["live_capital_eligible"] is False


def test_block_bootstrap_is_deterministic():
    frame = _outcomes()
    first = evidence_stats(frame, horizon_sessions=10, seed=23)
    second = evidence_stats(frame, horizon_sessions=10, seed=23)

    assert first["daily_block_ci_90_low"] == second["daily_block_ci_90_low"]
    assert first["daily_block_ci_90_high"] == second["daily_block_ci_90_high"]


def test_all_winner_profit_factor_remains_strict_json_and_passes_without_losses():
    frame = _outcomes()
    frame["pnl_pct"] = 0.032
    frame["pnl_pct_after_slippage"] = 0.030
    frame["excess_vs_spy_pct"] = 0.027

    report = analyze_edge_outcomes(frame)
    share = next(row for row in report["asset_rows"] if row["asset"] == "share")

    assert share["profit_factor"] is None
    assert share["profit_factor_no_losses"] is True
    assert _requirement(share, "profit_factor")["met"] is True
    assert share["live_capital_eligible"] is True
    json.dumps(report, allow_nan=False)
