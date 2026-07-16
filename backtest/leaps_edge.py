# Purpose: Validate LEAPS-swing evidence independently from generic option outcomes.
"""Profile-isolated after-cost evidence gates for LEAPS swing candidates.

Long expiry is an instrument choice, not a holding-period promise.  This
module therefore evaluates the dedicated LEAPS swing lane at 5, 10, and 20
completed sessions and refuses to borrow results from shorter-dated option
strategies.  Modeled option marks remain visible research; only exact broker
market observations can satisfy the live-capital gate.
"""
from __future__ import annotations

import math
from typing import Any

import pandas as pd

from backtest.edge_lab import (
    MIN_LIVE_EFFECTIVE_BLOCKS,
    MIN_LIVE_ENTRY_DAYS,
    MIN_LIVE_OUTCOMES,
    MIN_LIVE_PROFIT_FACTOR,
    evidence_stats,
)
from backtest.fixed_horizon import outcome_has_current_provenance
from optedge.strategy_profile import LEAPS_EVIDENCE_LANE, LEAPS_SWING_PROFILE

PROFILE_NAME = LEAPS_SWING_PROFILE.name
REQUIRED_HORIZONS = tuple(LEAPS_SWING_PROFILE.evidence_horizons_sessions)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _strict_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _profile_mask(frame: pd.DataFrame) -> pd.Series:
    profile = frame.get("execution_profile", pd.Series("", index=frame.index))
    lane = frame.get("strategy_evidence_lane", pd.Series("", index=frame.index))
    profile = profile.fillna("").astype(str).str.strip().str.lower()
    lane = lane.fillna("").astype(str).str.strip().str.lower()
    return profile.eq(PROFILE_NAME) & lane.eq(LEAPS_EVIDENCE_LANE)


def _requirement(code: str, label: str, met: bool, actual: Any, target: str) -> dict[str, Any]:
    return {
        "code": code,
        "label": label,
        "met": bool(met),
        "actual": actual,
        "target": target,
    }


def _horizon_verdict(
    frame: pd.DataFrame,
    horizon: int,
    *,
    source_attestation: dict[str, Any],
) -> dict[str, Any]:
    horizon_values = pd.to_numeric(frame.get("horizon_sessions"), errors="coerce")
    population = frame[horizon_values.eq(int(horizon))].copy()
    scored = population.get(
        "is_scored",
        pd.Series(False, index=population.index),
    ).map(_strict_bool)
    resolution = population.get(
        "resolution_status",
        pd.Series("", index=population.index),
    ).fillna("").astype(str).str.strip().str.lower()
    scored_status = resolution.eq("scored")
    pending_status = resolution.eq("pending")
    excluded_status = resolution.eq("excluded")
    recognized_status = scored_status | pending_status | excluded_status
    inconsistent_scored_status = scored.ne(scored_status)

    population_total = int(len(population))
    resolved_scored = int(scored_status.sum())
    pending = int(pending_status.sum())
    excluded = int(excluded_status.sum())
    wrong_population = int((~recognized_status | inconsistent_scored_status).sum())
    resolution_reconciled = (
        resolved_scored + pending + excluded == population_total
        and wrong_population == 0
    )

    executable = population.get(
        "eligible_for_executable_metrics",
        pd.Series(False, index=population.index),
    ).map(_strict_bool)
    independent = population.get(
        "is_independent",
        pd.Series(False, index=population.index),
    ).map(_strict_bool)
    current = population.apply(outcome_has_current_provenance, axis=1).astype(bool)
    selected = population[executable & independent & current & scored & scored_status].copy()
    scored_rows = selected.copy()
    quality = scored_rows.get("outcome_quality", pd.Series("", index=scored_rows.index))
    quality = quality.fillna("").astype(str).str.strip().str.lower()
    observed = scored_rows[quality.eq("broker_market_observed")].copy()
    stats = evidence_stats(observed, horizon_sessions=int(horizon), seed=9100 + int(horizon))

    total_scored = int(len(scored_rows))
    observed_count = int(len(observed))
    observed_coverage = observed_count / total_scored if total_scored else None
    n = int(stats.get("n") or 0)
    days = int(stats.get("unique_entry_days") or 0)
    blocks = int(stats.get("effective_horizon_blocks") or 0)
    avg_return = _number(stats.get("avg_return_after_costs"))
    ci_low = _number(stats.get("daily_block_ci_90_low"))
    profit_factor = _number(stats.get("profit_factor"))
    no_losses = stats.get("profit_factor_no_losses") is True
    excess = _number(stats.get("avg_excess_vs_spy"))
    double_costs = _number(stats.get("avg_return_at_2x_costs"))
    first_half = _number(stats.get("first_half_daily_avg"))
    recent_half = _number(stats.get("recent_half_daily_avg"))

    requirements = [
        _requirement(
            "source_attestation",
            "Fresh policy-bound evidence source",
            source_attestation.get("met") is True,
            source_attestation.get("status"),
            "current and policy-bound",
        ),
        _requirement(
            "profile_isolation",
            "Dedicated LEAPS swing profile",
            bool(population_total),
            population_total,
            f"> 0 {PROFILE_NAME} rows",
        ),
        _requirement(
            "broker_observed_coverage",
            "Broker-observed option outcomes",
            observed_coverage is not None and math.isclose(observed_coverage, 1.0, abs_tol=1e-12),
            observed_coverage,
            "100%",
        ),
        _requirement(
            "resolution_complete",
            "Complete, reconciled outcome population",
            (
                population_total > 0
                and resolution_reconciled
                and pending == 0
                and excluded == 0
            ),
            {
                "population": population_total,
                "scored": resolved_scored,
                "pending": pending,
                "excluded": excluded,
                "wrong_population": wrong_population,
            },
            "all rows scored; 0 pending, excluded, or wrong-population rows",
        ),
        _requirement("outcomes", "Independent outcomes", n >= MIN_LIVE_OUTCOMES, n, f">= {MIN_LIVE_OUTCOMES}"),
        _requirement("entry_days", "Distinct entry days", days >= MIN_LIVE_ENTRY_DAYS, days, f">= {MIN_LIVE_ENTRY_DAYS}"),
        _requirement("effective_blocks", "Effective horizon blocks", blocks >= MIN_LIVE_EFFECTIVE_BLOCKS, blocks, f">= {MIN_LIVE_EFFECTIVE_BLOCKS}"),
        _requirement("positive_after_costs", "Average return after costs", avg_return is not None and avg_return > 0, avg_return, "> 0"),
        _requirement("positive_ci", "90% moving-block lower bound", ci_low is not None and ci_low > 0, ci_low, "> 0"),
        _requirement(
            "profit_factor",
            "Profit factor after costs",
            no_losses or (profit_factor is not None and profit_factor >= MIN_LIVE_PROFIT_FACTOR),
            "no_losses" if no_losses else profit_factor,
            f">= {MIN_LIVE_PROFIT_FACTOR:.2f}",
        ),
        _requirement("spy_excess", "Average excess return vs SPY", excess is not None and excess > 0, excess, "> 0"),
        _requirement("double_costs", "Average return at 2x costs", double_costs is not None and double_costs > 0, double_costs, "> 0"),
        _requirement("first_half", "First-half daily average", first_half is not None and first_half > 0, first_half, "> 0"),
        _requirement("recent_half", "Recent-half daily average", recent_half is not None and recent_half > 0, recent_half, "> 0"),
    ]
    for code, label in (
        ("entry_time_coverage", "Valid entry-time coverage"),
        ("raw_return_coverage", "Finite raw-return coverage"),
        ("after_cost_coverage", "Finite after-cost coverage"),
        ("slippage_coverage", "Finite slippage coverage"),
        ("nonnegative_slippage_coverage", "Nonnegative slippage coverage"),
        ("spy_excess_coverage", "SPY benchmark coverage"),
        ("cost_reconciliation_coverage", "Raw-minus-cost reconciliation"),
        ("entry_spread_coverage", "Finite entry-spread coverage"),
        ("nonnegative_entry_spread_coverage", "Nonnegative entry-spread coverage"),
        ("cost_covers_entry_spread_coverage", "Recorded cost covers entry spread"),
    ):
        value = _number(stats.get(code))
        requirements.append(
            _requirement(
                code,
                label,
                value is not None and math.isclose(value, 1.0, abs_tol=1e-12),
                value,
                "100%",
            )
        )
    unmet = [row for row in requirements if not row["met"]]
    return {
        "horizon_sessions": int(horizon),
        "status": "validated" if not unmet else "paper_only",
        "live_capital_eligible": not unmet,
        "primary_blocker": unmet[0]["label"] if unmet else None,
        "requirements_met": len(requirements) - len(unmet),
        "requirements_total": len(requirements),
        "requirements": requirements,
        "broker_observed_coverage": observed_coverage,
        "resolution_population": {
            "population": population_total,
            "scored": resolved_scored,
            "pending": pending,
            "excluded": excluded,
            "wrong_population": wrong_population,
            "reconciled": resolution_reconciled,
        },
        **stats,
    }


def analyze_leaps_swing_evidence(
    outcomes: pd.DataFrame | None,
    *,
    source_attestation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a fail-closed, profile-isolated LEAPS evidence report."""
    attestation = dict(
        source_attestation
        or {"status": "in_memory_unattested", "met": False, "reason": "Source is not attested."}
    )
    if outcomes is None or outcomes.empty:
        return {
            "schema": "optedge_leaps_swing_evidence_v1",
            "profile": PROFILE_NAME,
            "evidence_lane": LEAPS_EVIDENCE_LANE,
            "status": "paper_only",
            "label": "LEAPS evidence has not started",
            "live_capital_eligible": False,
            "primary_blocker": "No profile-specific fixed-horizon outcomes are available.",
            "required_horizons_sessions": list(REQUIRED_HORIZONS),
            "horizons": [],
            "source_attestation": attestation,
        }

    work = outcomes.copy()
    asset = work.get("asset", pd.Series("", index=work.index))
    asset = asset.fillna("").astype(str).str.strip().str.lower()
    work = work[asset.isin({"option", "options"}) & _profile_mask(work)].copy()
    if work.empty:
        return {
            "schema": "optedge_leaps_swing_evidence_v1",
            "profile": PROFILE_NAME,
            "evidence_lane": LEAPS_EVIDENCE_LANE,
            "status": "paper_only",
            "label": "LEAPS evidence is isolated and still empty",
            "live_capital_eligible": False,
            "primary_blocker": "Generic option outcomes cannot authorize the LEAPS swing lane.",
            "required_horizons_sessions": list(REQUIRED_HORIZONS),
            "horizons": [],
            "source_attestation": attestation,
        }

    horizons = [
        _horizon_verdict(work, horizon, source_attestation=attestation)
        for horizon in REQUIRED_HORIZONS
    ]
    eligible = bool(horizons) and all(row["live_capital_eligible"] for row in horizons)
    first_blocker = next((row["primary_blocker"] for row in horizons if row["primary_blocker"]), None)
    return {
        "schema": "optedge_leaps_swing_evidence_v1",
        "profile": PROFILE_NAME,
        "evidence_lane": LEAPS_EVIDENCE_LANE,
        "status": "validated" if eligible else "paper_only",
        "label": "LEAPS swing evidence validated" if eligible else "LEAPS swing remains paper-only",
        "live_capital_eligible": eligible,
        "primary_blocker": first_blocker,
        "required_horizons_sessions": list(REQUIRED_HORIZONS),
        "horizons": horizons,
        "source_attestation": attestation,
        "notes": [
            "The LEAPS lane cannot borrow generic option evidence.",
            "Every required horizon must pass using 100% broker-market-observed option outcomes.",
            "A long-dated contract may still use a short 3/5/10-session review cadence and 20-session time limit.",
        ],
    }
