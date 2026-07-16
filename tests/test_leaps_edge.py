# Purpose: Verify profile-isolated, broker-observed LEAPS evidence gates.
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd

from backtest import fixed_horizon
from backtest.leaps_edge import analyze_leaps_swing_evidence
from optedge.strategy_profile import LEAPS_EVIDENCE_LANE


def _outcomes(
    *, rows_per_horizon: int = 620, quality: str = "broker_market_observed"
) -> pd.DataFrame:
    provenance = fixed_horizon.current_evidence_provenance()
    start = datetime(2023, 1, 2, tzinfo=UTC)
    rows = []
    for horizon in (5, 10, 20):
        for index in range(rows_per_horizon):
            entry = start + timedelta(days=index)
            raw_return = 0.025 if index % 5 else -0.01
            slippage = 0.005
            after_cost = raw_return - slippage
            rows.append(
                {
                    **provenance,
                    "methodology_version": fixed_horizon.METHODOLOGY_VERSION,
                    "outcome_id": f"leaps-{horizon}-{index}",
                    "asset": "option",
                    "execution_profile": "leaps_swing",
                    "strategy_evidence_lane": LEAPS_EVIDENCE_LANE,
                    "horizon_sessions": horizon,
                    "entry_time": entry.isoformat(),
                    "pnl_pct": raw_return,
                    "slippage_assumption_pct": slippage,
                    "pnl_pct_after_slippage": after_cost,
                    "excess_vs_spy_pct": after_cost - 0.002,
                    "spread_pct": 0.004,
                    "is_scored": True,
                    "is_independent": True,
                    "eligible_for_executable_metrics": True,
                    "eligible_for_shadow_metrics": True,
                    "outcome_quality": quality,
                    "resolution_status": "scored",
                    "resolution_reason": "",
                    "independent_key": f"leaps_swing|option|T{index}|long_call|{entry.date()}",
                }
            )
    return pd.DataFrame(rows)


def test_generic_option_evidence_cannot_authorize_leaps_lane():
    frame = _outcomes()
    frame["execution_profile"] = "swing_execution"
    frame["strategy_evidence_lane"] = "option_general"
    report = analyze_leaps_swing_evidence(
        frame,
        source_attestation={"status": "fresh", "met": True},
    )
    assert report["live_capital_eligible"] is False
    assert "Generic option outcomes" in report["primary_blocker"]


def test_leaps_lane_requires_100_percent_broker_observed_outcomes():
    frame = _outcomes()
    frame.loc[frame.index[0], "outcome_quality"] = "modeled_option_proxy"
    report = analyze_leaps_swing_evidence(
        frame,
        source_attestation={"status": "fresh", "met": True},
    )
    assert report["live_capital_eligible"] is False
    five = next(row for row in report["horizons"] if row["horizon_sessions"] == 5)
    observed = next(
        row for row in five["requirements"] if row["code"] == "broker_observed_coverage"
    )
    assert observed["met"] is False


def test_complete_profile_specific_evidence_must_pass_every_horizon():
    report = analyze_leaps_swing_evidence(
        _outcomes(),
        source_attestation={"status": "fresh", "met": True},
    )
    assert report["live_capital_eligible"] is True
    assert report["status"] == "validated"
    assert [row["horizon_sessions"] for row in report["horizons"]] == [5, 10, 20]


def _horizon_requirement(report: dict, horizon: int, code: str) -> dict:
    verdict = next(row for row in report["horizons"] if row["horizon_sessions"] == horizon)
    return next(row for row in verdict["requirements"] if row["code"] == code)


def test_pending_leaps_outcome_cannot_disappear_behind_evidence_filters():
    frame = _outcomes()
    index = frame.index[0]
    frame.loc[index, "is_scored"] = False
    frame.loc[index, "resolution_status"] = "pending"
    frame.loc[index, "eligible_for_executable_metrics"] = False
    frame.loc[index, "is_independent"] = False
    frame.loc[index, "methodology_version"] = 0

    report = analyze_leaps_swing_evidence(
        frame,
        source_attestation={"status": "fresh", "met": True},
    )

    resolution = _horizon_requirement(report, 5, "resolution_complete")
    assert report["live_capital_eligible"] is False
    assert resolution["met"] is False
    assert resolution["actual"] == {
        "population": 620,
        "scored": 619,
        "pending": 1,
        "excluded": 0,
        "wrong_population": 0,
    }


def test_excluded_leaps_outcome_cannot_disappear_behind_evidence_filters():
    frame = _outcomes()
    index = frame.index[0]
    frame.loc[index, "is_scored"] = False
    frame.loc[index, "resolution_status"] = "excluded"
    frame.loc[index, "eligible_for_executable_metrics"] = False
    frame.loc[index, "is_independent"] = False
    frame.loc[index, "methodology_version"] = 0

    report = analyze_leaps_swing_evidence(
        frame,
        source_attestation={"status": "fresh", "met": True},
    )

    resolution = _horizon_requirement(report, 5, "resolution_complete")
    assert report["live_capital_eligible"] is False
    assert resolution["met"] is False
    assert resolution["actual"] == {
        "population": 620,
        "scored": 619,
        "pending": 0,
        "excluded": 1,
        "wrong_population": 0,
    }


def test_unrecognized_resolution_is_a_wrong_population_row():
    frame = _outcomes()
    index = frame.index[0]
    frame.loc[index, "is_scored"] = False
    frame.loc[index, "resolution_status"] = "unknown"
    frame.loc[index, "eligible_for_executable_metrics"] = False

    report = analyze_leaps_swing_evidence(
        frame,
        source_attestation={"status": "fresh", "met": True},
    )

    resolution = _horizon_requirement(report, 5, "resolution_complete")
    assert report["live_capital_eligible"] is False
    assert resolution["met"] is False
    assert resolution["actual"]["wrong_population"] == 1


def test_profile_is_part_of_fixed_horizon_independence_identity():
    base = {
        **fixed_horizon.current_evidence_provenance(),
        "asset": "option",
        "ticker": "AAPL",
        "side": "call",
        "strike": 200,
        "expiry": "2028-01-21",
        "entry_time": "2026-07-16T16:00:00+00:00",
        "is_buy": True,
        "trade_status": "trade",
        "is_actionable": True,
        "buyer_edge_pct": 0.03,
        "pricing_edge_ok": True,
        "suggested_contracts": 1,
        "strategy_qualified_pre_guard": True,
    }
    prepared = fixed_horizon.prepare_signals(
        pd.DataFrame(
            [
                {**base, "execution_profile": "swing_execution"},
                {
                    **base,
                    "execution_profile": "leaps_swing",
                    "strategy_evidence_lane": LEAPS_EVIDENCE_LANE,
                },
            ]
        )
    )
    assert len(prepared) == 2
    assert prepared["signal_id"].nunique() == 2
    assert prepared["independent_key"].nunique() == 2
