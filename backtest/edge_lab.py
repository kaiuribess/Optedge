# Purpose: Measure whether swing-trading evidence survives costs, time, and correlation.
"""Fail-closed evidence scoring for the Optedge swing-trading cockpit.

Live-review evidence is bound to the exact strategy and fixed-horizon policy
that existed when a signal was logged.  Same-day signals are averaged first,
then resampled in circular blocks at least as long as the holding horizon.
Modeled option outcomes remain visible research, but only exact broker-market
observations may satisfy option performance requirements.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backtest.fixed_horizon import (
    EVIDENCE_PROVENANCE_COLUMNS,
    METHODOLOGY_VERSION,
    current_evidence_provenance,
    outcome_has_current_provenance,
    outcome_set_digest,
)

HEADLINE_HORIZON = 10
DISPLAY_HORIZONS = (5, 10, 20)
MIN_RESEARCH_OUTCOMES = 50
MIN_RESEARCH_ENTRY_DAYS = 10
MIN_LIVE_OUTCOMES = 200
MIN_LIVE_ENTRY_DAYS = 30
MIN_LIVE_EFFECTIVE_BLOCKS = 30
MIN_LIVE_PROFIT_FACTOR = 1.15
MIN_OPTION_OBSERVED_COVERAGE = 0.50
BOOTSTRAP_SAMPLES = 2_000
MAX_SOURCE_AGE_HOURS = 96.0
COST_RECONCILIATION_ATOL = 1e-10
COST_RECONCILIATION_RTOL = 1e-8

REQUIRED_OUTCOME_COLUMNS = frozenset(
    {
        "asset",
        "horizon_sessions",
        "entry_time",
        "pnl_pct",
        "slippage_assumption_pct",
        "pnl_pct_after_slippage",
        "excess_vs_spy_pct",
        "is_scored",
        "is_independent",
        "eligible_for_executable_metrics",
        "eligible_for_shadow_metrics",
        "outcome_quality",
        "outcome_id",
        "independent_key",
        "methodology_version",
        "resolution_status",
        "resolution_reason",
    }
)
_BOOL_COLUMNS = (
    "is_scored",
    "is_independent",
    "eligible_for_executable_metrics",
    "eligible_for_shadow_metrics",
)
_TRUE_TEXT = frozenset({"1", "true", "yes", "y"})
_FALSE_TEXT = frozenset({"0", "false", "no", "n"})


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _strict_bool(value: Any) -> bool | None:
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    text = str(value).strip().lower()
    if text in _TRUE_TEXT:
        return True
    if text in _FALSE_TEXT:
        return False
    return None


def _bool_column(frame: pd.DataFrame, column: str, default: bool = False) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(default, index=frame.index, dtype=bool)
    return frame[column].map(_strict_bool).fillna(default).astype(bool)


def _numeric_column(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(np.nan, index=frame.index, dtype=float)
    return pd.to_numeric(frame[column], errors="coerce")


def _finite_mask(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    return numeric.notna() & np.isfinite(numeric.astype(float))


def _profit_factor(values: pd.Series) -> tuple[float | None, bool]:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    clean = clean[np.isfinite(clean)]
    if clean.empty:
        return None, False
    gross_profit = float(clean[clean > 0].sum())
    gross_loss = abs(float(clean[clean < 0].sum()))
    if gross_loss <= 0:
        return None, gross_profit > 0
    return gross_profit / gross_loss, False


def _mean(values: pd.Series) -> float | None:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    clean = clean[np.isfinite(clean)]
    return float(clean.mean()) if not clean.empty else None


def _daily_block_interval(
    daily_returns: pd.Series,
    *,
    block_length: int = 1,
    samples: int = BOOTSTRAP_SAMPLES,
    seed: int = 17,
) -> tuple[float | None, float | None]:
    """Return a deterministic 90% circular moving-block bootstrap interval."""
    values = pd.to_numeric(daily_returns, errors="coerce").dropna().to_numpy(dtype=float)
    values = values[np.isfinite(values)]
    if len(values) < 2:
        return None, None
    block = min(len(values), max(1, int(block_length)))
    blocks_per_draw = int(math.ceil(len(values) / block))
    rng = np.random.default_rng(seed)
    starts = rng.integers(
        0,
        len(values),
        size=(max(200, int(samples)), blocks_per_draw),
    )
    offsets = np.arange(block, dtype=int)
    draw_index = (starts[:, :, None] + offsets[None, None, :]) % len(values)
    means = values[draw_index.reshape(len(starts), -1)[:, : len(values)]].mean(axis=1)
    return float(np.quantile(means, 0.05)), float(np.quantile(means, 0.95))


def _empty_stats() -> dict[str, Any]:
    return {
        "n": 0,
        "gate_rows": 0,
        "unique_entry_days": 0,
        "effective_horizon_blocks": 0,
        "block_length_sessions": None,
        "signals_per_entry_day": None,
        "win_rate": None,
        "avg_return_after_costs": None,
        "median_return_after_costs": None,
        "profit_factor": None,
        "profit_factor_no_losses": None,
        "daily_block_ci_90_low": None,
        "daily_block_ci_90_high": None,
        "avg_excess_vs_spy": None,
        "avg_return_at_1_5x_costs": None,
        "avg_return_at_2x_costs": None,
        "first_half_daily_avg": None,
        "recent_half_daily_avg": None,
        "broker_observed_coverage": None,
        "modeled_proxy_coverage": None,
        "entry_time_coverage": None,
        "raw_return_coverage": None,
        "after_cost_coverage": None,
        "slippage_coverage": None,
        "nonnegative_slippage_coverage": None,
        "spy_excess_coverage": None,
        "cost_reconciliation_coverage": None,
    }


def evidence_stats(
    frame: pd.DataFrame,
    *,
    horizon_sessions: int = HEADLINE_HORIZON,
    seed: int = 17,
) -> dict[str, Any]:
    """Summarize one scored evidence slice with complete-data diagnostics."""
    if frame is None or frame.empty:
        return _empty_stats()
    work = frame.copy()
    total = int(len(work))
    raw = _numeric_column(work, "pnl_pct")
    after_costs = _numeric_column(work, "pnl_pct_after_slippage")
    slippage = _numeric_column(work, "slippage_assumption_pct")
    excess = _numeric_column(work, "excess_vs_spy_pct")
    entry_time = pd.to_datetime(work.get("entry_time"), errors="coerce", utc=True)
    entry_mask = entry_time.notna()
    raw_mask = _finite_mask(raw)
    after_mask = _finite_mask(after_costs)
    slippage_mask = _finite_mask(slippage)
    excess_mask = _finite_mask(excess)
    nonnegative_slippage = slippage_mask & slippage.ge(0)
    reconciliation = (
        raw_mask
        & after_mask
        & slippage_mask
        & pd.Series(
            np.isclose(
                after_costs.to_numpy(dtype=float),
                (raw - slippage).to_numpy(dtype=float),
                rtol=COST_RECONCILIATION_RTOL,
                atol=COST_RECONCILIATION_ATOL,
                equal_nan=False,
            ),
            index=work.index,
        )
    )
    valid_after = after_mask & entry_mask
    valid = pd.DataFrame(
        {
            "after_costs": after_costs,
            "entry_day": entry_time.dt.date,
        },
        index=work.index,
    ).loc[valid_after]
    if valid.empty:
        stats = _empty_stats()
        stats.update(
            {
                "gate_rows": total,
                "block_length_sessions": max(1, int(horizon_sessions)),
                "entry_time_coverage": float(entry_mask.mean()),
                "raw_return_coverage": float(raw_mask.mean()),
                "after_cost_coverage": float(after_mask.mean()),
                "slippage_coverage": float(slippage_mask.mean()),
                "nonnegative_slippage_coverage": float(nonnegative_slippage.mean()),
                "spy_excess_coverage": float(excess_mask.mean()),
                "cost_reconciliation_coverage": float(reconciliation.mean()),
            }
        )
        return stats

    daily = valid.groupby("entry_day", sort=True)["after_costs"].mean()
    block_length = max(1, int(horizon_sessions))
    ci_low, ci_high = _daily_block_interval(
        daily,
        block_length=block_length,
        seed=seed,
    )
    split = max(1, len(daily) // 2)
    first_half = daily.iloc[:split]
    recent_half = daily.iloc[split:] if split < len(daily) else pd.Series(dtype=float)
    complete_costs = bool(raw_mask.all() and nonnegative_slippage.all())
    complete_benchmark = bool(excess_mask.all())
    stress_1_5x = raw - 1.5 * slippage if complete_costs else pd.Series(dtype=float)
    stress_2x = raw - 2.0 * slippage if complete_costs else pd.Series(dtype=float)
    profit_factor, profit_factor_no_losses = _profit_factor(valid["after_costs"])

    quality = work.get("outcome_quality")
    if isinstance(quality, pd.Series):
        normalized_quality = quality.fillna("").astype(str).str.strip().str.lower()
        broker_coverage = float((normalized_quality == "broker_market_observed").mean())
        proxy_coverage = float((normalized_quality == "modeled_option_proxy").mean())
    else:
        broker_coverage = None
        proxy_coverage = None

    return {
        "n": int(len(valid)),
        "gate_rows": total,
        "unique_entry_days": int(len(daily)),
        "effective_horizon_blocks": int(len(daily) // block_length),
        "block_length_sessions": block_length,
        "signals_per_entry_day": float(len(valid) / len(daily)) if len(daily) else None,
        "win_rate": float((valid["after_costs"] > 0).mean()),
        "avg_return_after_costs": float(valid["after_costs"].mean()),
        "median_return_after_costs": float(valid["after_costs"].median()),
        "profit_factor": profit_factor,
        "profit_factor_no_losses": profit_factor_no_losses,
        "daily_block_ci_90_low": ci_low,
        "daily_block_ci_90_high": ci_high,
        "avg_excess_vs_spy": _mean(excess) if complete_benchmark else None,
        "avg_return_at_1_5x_costs": _mean(stress_1_5x),
        "avg_return_at_2x_costs": _mean(stress_2x),
        "first_half_daily_avg": _mean(first_half),
        "recent_half_daily_avg": _mean(recent_half),
        "broker_observed_coverage": broker_coverage,
        "modeled_proxy_coverage": proxy_coverage,
        "entry_time_coverage": float(entry_mask.mean()),
        "raw_return_coverage": float(raw_mask.mean()),
        "after_cost_coverage": float(after_mask.mean()),
        "slippage_coverage": float(slippage_mask.mean()),
        "nonnegative_slippage_coverage": float(nonnegative_slippage.mean()),
        "spy_excess_coverage": float(excess_mask.mean()),
        "cost_reconciliation_coverage": float(reconciliation.mean()),
    }


def _requirement(code: str, label: str, met: bool, actual: Any, target: str) -> dict[str, Any]:
    return {
        "code": code,
        "label": label,
        "met": bool(met),
        "actual": actual,
        "target": target,
    }


def _coverage_is_complete(stats: dict[str, Any], key: str) -> bool:
    value = _number(stats.get(key))
    return value is not None and math.isclose(value, 1.0, rel_tol=0.0, abs_tol=1e-12)


def _resolution_stats(
    selected: pd.DataFrame,
    external: dict[str, Any] | None,
    *,
    require_external: bool,
) -> dict[str, Any]:
    local_expected = int(len(selected))
    local_scored = int(_bool_column(selected, "is_scored").sum()) if local_expected else 0
    local_excluded = local_expected - local_scored
    expected = local_expected
    scored = local_scored
    excluded = local_excluded
    pending = 0
    reasons = (
        selected.loc[~_bool_column(selected, "is_scored"), "resolution_reason"]
        .fillna("unknown")
        .astype(str)
        .value_counts()
        .to_dict()
        if local_expected and "resolution_reason" in selected.columns
        else {}
    )
    attested = not require_external
    external_consistent = not require_external
    if isinstance(external, dict):
        values = {
            key: _number(external.get(key)) for key in ("expected", "scored", "excluded", "pending")
        }
        if all(
            value is not None and value >= 0 and value == int(value) for value in values.values()
        ):
            candidate_expected = int(values["expected"])
            candidate_scored = int(values["scored"])
            candidate_excluded = int(values["excluded"])
            candidate_pending = int(values["pending"])
            external_consistent = bool(
                candidate_expected >= local_expected
                and candidate_scored == local_scored
                and candidate_excluded == local_excluded
                and candidate_expected == candidate_scored + candidate_excluded + candidate_pending
            )
            if external_consistent:
                expected = candidate_expected
                scored = candidate_scored
                excluded = candidate_excluded
                pending = candidate_pending
                attested = True
                if isinstance(external.get("exclusion_reasons"), dict):
                    reasons = dict(external["exclusion_reasons"])
    reconciled = bool(attested and external_consistent and expected == scored + excluded + pending)
    coverage = scored / expected if expected > 0 and reconciled else None
    return {
        "resolution_expected": expected,
        "resolution_scored": scored,
        "resolution_excluded": excluded,
        "resolution_pending": pending,
        "resolution_coverage": coverage,
        "resolution_reconciled": reconciled,
        "resolution_attested": attested,
        "resolution_exclusion_reasons": reasons,
    }


def _positive_profit_factor(value: Any, threshold: float, *, no_losses: bool = False) -> bool:
    if no_losses:
        return True
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return False
    return not math.isnan(parsed) and parsed >= threshold


def _verdict(
    asset: str,
    lane: str,
    stats: dict[str, Any],
    *,
    source_attestation: dict[str, Any],
) -> dict[str, Any]:
    n = int(stats.get("n") or 0)
    days = int(stats.get("unique_entry_days") or 0)
    effective_blocks = int(stats.get("effective_horizon_blocks") or 0)
    avg_return = _number(stats.get("avg_return_after_costs"))
    ci_low = _number(stats.get("daily_block_ci_90_low"))
    ci_high = _number(stats.get("daily_block_ci_90_high"))
    profit_factor = stats.get("profit_factor")
    profit_factor_no_losses = stats.get("profit_factor_no_losses") is True
    profit_factor_display = "no_losses" if profit_factor_no_losses else profit_factor
    excess = _number(stats.get("avg_excess_vs_spy"))
    stress_2x = _number(stats.get("avg_return_at_2x_costs"))
    first_half = _number(stats.get("first_half_daily_avg"))
    recent_half = _number(stats.get("recent_half_daily_avg"))
    observed = _number(stats.get("broker_observed_coverage"))
    resolution_coverage = _number(stats.get("resolution_coverage"))
    source_ok = source_attestation.get("met") is True
    is_current_executable = lane == "current_method_executable"

    requirements = [
        _requirement(
            "source_attestation",
            "Fresh policy-bound evidence source",
            source_ok,
            source_attestation.get("reason") or source_attestation.get("status"),
            f"policy-bound and <= {MAX_SOURCE_AGE_HOURS:.0f}h old",
        ),
        _requirement(
            "current_method_executable",
            "Current-method executable evidence",
            is_current_executable,
            lane,
            "current_method_executable",
        ),
        _requirement(
            "resolution_coverage",
            "Scored resolution coverage",
            resolution_coverage is not None
            and math.isclose(resolution_coverage, 1.0, rel_tol=0.0, abs_tol=1e-12)
            and int(stats.get("resolution_excluded") or 0) == 0
            and int(stats.get("resolution_pending") or 0) == 0
            and stats.get("resolution_reconciled") is True
            and stats.get("resolution_attested") is True,
            resolution_coverage,
            "100% scored; 0 excluded or pending",
        ),
    ]
    for code, label in (
        ("entry_time_coverage", "Valid entry-time coverage"),
        ("raw_return_coverage", "Finite raw-return coverage"),
        ("after_cost_coverage", "Finite after-cost coverage"),
        ("slippage_coverage", "Finite slippage coverage"),
        ("nonnegative_slippage_coverage", "Nonnegative slippage coverage"),
        ("cost_reconciliation_coverage", "Raw-minus-cost reconciliation"),
        ("spy_excess_coverage", "SPY benchmark coverage"),
    ):
        requirements.append(
            _requirement(
                code,
                label,
                _coverage_is_complete(stats, code),
                stats.get(code),
                "100%",
            )
        )
    requirements.extend(
        [
            _requirement(
                "outcomes",
                "Observed independent outcomes",
                n >= MIN_LIVE_OUTCOMES,
                n,
                f">= {MIN_LIVE_OUTCOMES}",
            ),
            _requirement(
                "entry_days",
                "Distinct entry days",
                days >= MIN_LIVE_ENTRY_DAYS,
                days,
                f">= {MIN_LIVE_ENTRY_DAYS}",
            ),
            _requirement(
                "effective_blocks",
                "Effective horizon-length blocks",
                effective_blocks >= MIN_LIVE_EFFECTIVE_BLOCKS,
                effective_blocks,
                f">= {MIN_LIVE_EFFECTIVE_BLOCKS}",
            ),
            _requirement(
                "positive_after_costs",
                "Average return after costs",
                avg_return is not None and avg_return > 0,
                avg_return,
                "> 0",
            ),
            _requirement(
                "positive_ci",
                "90% moving-block lower bound",
                ci_low is not None and ci_low > 0,
                ci_low,
                "> 0",
            ),
            _requirement(
                "profit_factor",
                "Profit factor after costs",
                _positive_profit_factor(
                    profit_factor,
                    MIN_LIVE_PROFIT_FACTOR,
                    no_losses=profit_factor_no_losses,
                ),
                profit_factor_display,
                f">= {MIN_LIVE_PROFIT_FACTOR:.2f}",
            ),
            _requirement(
                "spy_excess",
                "Average excess return vs SPY",
                excess is not None and excess > 0,
                excess,
                "> 0",
            ),
            _requirement(
                "double_costs",
                "Average return at 2x assumed costs",
                stress_2x is not None and stress_2x > 0,
                stress_2x,
                "> 0",
            ),
            _requirement(
                "time_stability_early",
                "First-half daily average",
                first_half is not None and first_half > 0,
                first_half,
                "> 0",
            ),
            _requirement(
                "time_stability_recent",
                "Recent-half daily average",
                recent_half is not None and recent_half > 0,
                recent_half,
                "> 0",
            ),
        ]
    )
    if asset == "option":
        requirements.append(
            _requirement(
                "observed_option_coverage",
                "Broker-observed option outcome coverage",
                observed is not None and observed >= MIN_OPTION_OBSERVED_COVERAGE,
                observed,
                f">= {MIN_OPTION_OBSERVED_COVERAGE:.0%}",
            )
        )

    live_eligible = all(row["met"] for row in requirements)
    has_research_sample = n >= MIN_RESEARCH_OUTCOMES and days >= MIN_RESEARCH_ENTRY_DAYS
    materially_adverse = has_research_sample and (
        (avg_return is not None and avg_return <= 0)
        or (
            profit_factor is not None
            and not _positive_profit_factor(
                profit_factor,
                1.0,
                no_losses=profit_factor_no_losses,
            )
        )
        or (ci_high is not None and ci_high < 0)
    )
    research_promising = has_research_sample and all(
        [
            avg_return is not None and avg_return > 0,
            ci_low is not None and ci_low > 0,
            _positive_profit_factor(
                profit_factor,
                1.10,
                no_losses=profit_factor_no_losses,
            ),
            stress_2x is not None and stress_2x > 0,
            recent_half is not None and recent_half > 0,
        ]
    )

    if live_eligible:
        status, label, tone = "validated", "Validated for manual review", "ok"
    elif materially_adverse:
        status, label, tone = "adverse", "Adverse after-cost evidence", "bad"
    elif research_promising:
        status, label, tone = "promising", "Promising, still paper-only", "warn"
    elif not has_research_sample:
        status, label, tone = "insufficient", "Insufficient independent evidence", "warn"
    else:
        status, label, tone = "fragile", "Fragile or mixed evidence", "warn"

    unmet = [row for row in requirements if not row["met"]]
    primary_blocker = unmet[0]["label"] if unmet else None
    if materially_adverse:
        if avg_return is not None and avg_return <= 0:
            primary_blocker = f"After-cost mean is {avg_return:.2%}"
        elif profit_factor is not None and not _positive_profit_factor(
            profit_factor,
            1.0,
            no_losses=profit_factor_no_losses,
        ):
            primary_blocker = f"After-cost profit factor is {float(profit_factor):.2f}"
        elif ci_high is not None and ci_high < 0:
            primary_blocker = f"90% moving-block upper bound is {ci_high:.2%}"
    return {
        "status": status,
        "label": label,
        "tone": tone,
        "live_capital_eligible": live_eligible,
        "paper_tracking_allowed": True,
        "primary_blocker": primary_blocker,
        "requirements_met": len(requirements) - len(unmet),
        "requirements_total": len(requirements),
        "requirements": requirements,
    }


def _evidence_lane(frame: pd.DataFrame) -> tuple[str, pd.DataFrame]:
    if frame is None or frame.empty:
        return "legacy_research_only", frame
    current_mask = frame.apply(outcome_has_current_provenance, axis=1).astype(bool)
    executable = frame[current_mask & _bool_column(frame, "eligible_for_executable_metrics")]
    if not executable.empty:
        return "current_method_executable", executable
    shadow = frame[current_mask & _bool_column(frame, "eligible_for_shadow_metrics")]
    if not shadow.empty:
        return "current_method_shadow", shadow
    return "legacy_research_only", frame


def _schema_errors(frame: pd.DataFrame) -> list[str]:
    missing = sorted(REQUIRED_OUTCOME_COLUMNS.difference(frame.columns))
    errors = [f"missing required column: {column}" for column in missing]
    if missing:
        return errors
    for column in _BOOL_COLUMNS:
        invalid = frame[column].map(_strict_bool).isna()
        if invalid.any():
            errors.append(
                f"{column} contains {int(invalid.sum())} invalid or missing boolean value(s)"
            )
    assets = (
        frame["asset"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.lower()
        .replace(
            {
                "options": "option",
                "shares": "share",
                "future": "futures",
            }
        )
    )
    invalid_assets = ~assets.isin({"option", "share", "futures"})
    if invalid_assets.any():
        errors.append(f"asset contains {int(invalid_assets.sum())} unsupported value(s)")
    horizons = pd.to_numeric(frame["horizon_sessions"], errors="coerce")
    invalid_horizons = (
        horizons.isna() | ~np.isfinite(horizons) | horizons.le(0) | horizons.ne(np.floor(horizons))
    )
    if invalid_horizons.any():
        errors.append(f"horizon_sessions contains {int(invalid_horizons.sum())} invalid value(s)")
    entries = pd.to_datetime(frame["entry_time"], errors="coerce", utc=True)
    if entries.isna().any():
        errors.append(f"entry_time contains {int(entries.isna().sum())} invalid value(s)")
    methodology = pd.to_numeric(frame["methodology_version"], errors="coerce")
    invalid_methodology = (
        methodology.isna()
        | ~np.isfinite(methodology)
        | methodology.le(0)
        | methodology.ne(np.floor(methodology))
    )
    if invalid_methodology.any():
        errors.append(
            f"methodology_version contains {int(invalid_methodology.sum())} invalid value(s)"
        )
    outcome_ids = frame["outcome_id"].fillna("").astype(str).str.strip()
    if outcome_ids.eq("").any():
        errors.append(f"outcome_id contains {int(outcome_ids.eq('').sum())} blank value(s)")
    duplicate_outcomes = outcome_ids.duplicated(keep=False)
    if duplicate_outcomes.any():
        errors.append(f"outcome_id contains {int(duplicate_outcomes.sum())} duplicate row(s)")
    independent = frame[_bool_column(frame, "is_independent")].copy()
    if not independent.empty:
        keys = independent["independent_key"].fillna("").astype(str).str.strip()
        if keys.eq("").any():
            errors.append(
                f"independent_key contains {int(keys.eq('').sum())} blank independent value(s)"
            )
        composite = pd.DataFrame(
            {
                "independent_key": keys,
                "horizon_sessions": pd.to_numeric(independent["horizon_sessions"], errors="coerce"),
            },
            index=independent.index,
        )
        duplicate_independent = composite.duplicated(
            ["independent_key", "horizon_sessions"],
            keep=False,
        )
        if duplicate_independent.any():
            errors.append(
                "independent_key+horizon_sessions contains "
                f"{int(duplicate_independent.sum())} duplicate independent row(s)"
            )
    scored = _bool_column(frame, "is_scored")
    resolution = frame["resolution_status"].fillna("").astype(str).str.strip().str.lower()
    inconsistent = (scored & resolution.ne("scored")) | (~scored & resolution.eq("scored"))
    if inconsistent.any():
        errors.append(
            f"resolution_status disagrees with is_scored on {int(inconsistent.sum())} row(s)"
        )
    return errors


def _unavailable_report(
    label: str,
    blocker: str,
    *,
    headline_horizon: int,
    validation_errors: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "label": label,
        "tone": "bad",
        "live_capital_eligible": False,
        "headline_horizon_sessions": int(headline_horizon),
        "asset_rows": [],
        "matrix": [],
        "primary_blocker": blocker,
        "validation_errors": validation_errors or [],
    }


def analyze_edge_outcomes(
    outcomes: pd.DataFrame,
    *,
    headline_horizon: int = HEADLINE_HORIZON,
    source_attestation: dict[str, Any] | None = None,
    resolution_coverage: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build an asset/horizon matrix and a fail-closed live-capital gate."""
    if outcomes is None or outcomes.empty:
        return _unavailable_report(
            "Edge evidence unavailable",
            "No fixed-horizon outcomes are available.",
            headline_horizon=headline_horizon,
        )
    work = outcomes.copy()
    errors = _schema_errors(work)
    if errors:
        return _unavailable_report(
            "Edge evidence failed validation",
            errors[0],
            headline_horizon=headline_horizon,
            validation_errors=errors,
        )
    work["asset"] = (
        work["asset"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.lower()
        .replace(
            {
                "options": "option",
                "shares": "share",
                "future": "futures",
            }
        )
    )
    independent = _bool_column(work, "is_independent")
    work = work[independent].copy()
    if work.empty:
        return _unavailable_report(
            "Independent evidence unavailable",
            "No independent fixed-horizon outcomes are available.",
            headline_horizon=headline_horizon,
        )

    attestation = dict(
        source_attestation
        or {
            "status": "in_memory_validated",
            "met": True,
            "reason": None,
        }
    )
    coverage_lookup: dict[tuple[str, int, str], dict[str, Any]] = {}
    for row in resolution_coverage or []:
        if not isinstance(row, dict):
            continue
        horizon = _number(row.get("horizon_sessions"))
        if horizon is None or horizon != int(horizon):
            continue
        key = (
            str(row.get("asset") or "").strip().lower(),
            int(horizon),
            str(row.get("evidence_lane") or "").strip(),
        )
        coverage_lookup[key] = row

    horizon_values = pd.to_numeric(work["horizon_sessions"], errors="coerce")
    assets = ("option", "share", "futures")
    matrix: list[dict[str, Any]] = []
    requested_horizons = sorted(set(DISPLAY_HORIZONS).union({int(headline_horizon)}))
    for asset_index, asset in enumerate(assets):
        for horizon in requested_horizons:
            subset = work[work["asset"].eq(asset) & horizon_values.eq(horizon)].copy()
            lane, selected = _evidence_lane(subset)
            scored_selected = selected[_bool_column(selected, "is_scored")].copy()
            aggregate_stats = evidence_stats(
                scored_selected,
                horizon_sessions=horizon,
                seed=17 + asset_index * 100 + horizon,
            )
            if asset == "option":
                quality = (
                    scored_selected["outcome_quality"]
                    .fillna("")
                    .astype(str)
                    .str.strip()
                    .str.lower()
                )
                observed = scored_selected[quality.eq("broker_market_observed")].copy()
                live_stats = evidence_stats(
                    observed,
                    horizon_sessions=horizon,
                    seed=17 + asset_index * 100 + horizon,
                )
                live_stats["broker_observed_coverage"] = aggregate_stats.get(
                    "broker_observed_coverage"
                )
                live_stats["modeled_proxy_coverage"] = aggregate_stats.get("modeled_proxy_coverage")
            else:
                live_stats = dict(aggregate_stats)
            resolution = _resolution_stats(
                selected,
                coverage_lookup.get((asset, horizon, lane)),
                require_external=bool(attestation.get("requires_resolution_attestation")),
            )
            live_stats.update(resolution)
            verdict = _verdict(
                asset,
                lane,
                live_stats,
                source_attestation=attestation,
            )
            matrix.append(
                {
                    "asset": asset,
                    "horizon_sessions": int(horizon),
                    "evidence_lane": lane,
                    **live_stats,
                    **verdict,
                    "research_all_outcomes": aggregate_stats,
                    "live_metric_basis": (
                        "broker_market_observed_only"
                        if asset == "option"
                        else "all_scored_current_lane_outcomes"
                    ),
                }
            )

    asset_rows = [row for row in matrix if row["horizon_sessions"] == int(headline_horizon)]
    eligible = [row for row in asset_rows if row.get("live_capital_eligible")]
    adverse = [row for row in asset_rows if row.get("status") == "adverse"]
    if attestation.get("met") is not True:
        status, label, tone = "blocked", "Evidence source must be refreshed", "bad"
        primary_blocker = str(
            attestation.get("reason") or "The evidence source is stale or not policy-bound."
        )
    elif eligible:
        status, label, tone = "validated", "At least one asset lane is validated", "ok"
        primary_blocker = None
    elif adverse:
        status, label, tone = "blocked", "Live edge is not proven", "bad"
        primary_blocker = f"{adverse[0]['asset'].title()}: {adverse[0].get('primary_blocker') or adverse[0]['label']}"
    else:
        status, label, tone = "paper_only", "Paper evidence is still maturing", "warn"
        first = asset_rows[0] if asset_rows else {}
        primary_blocker = first.get("primary_blocker") or "Current-method evidence has not matured."

    return {
        "status": status,
        "label": label,
        "tone": tone,
        "live_capital_eligible": bool(eligible) and attestation.get("met") is True,
        "headline_horizon_sessions": int(headline_horizon),
        "validated_assets": [row["asset"] for row in eligible]
        if attestation.get("met") is True
        else [],
        "primary_blocker": primary_blocker,
        "asset_rows": asset_rows,
        "matrix": matrix,
        "source_attestation": attestation,
        "methodology": {
            "confidence_interval": (
                "90% deterministic circular moving-block bootstrap over entry-day averages; "
                "block length is at least the holding horizon"
            ),
            "cost_stress": "Requires reconciled raw and after-cost outcomes, then reprices at 1.5x and 2x recorded slippage",
            "live_lane": "Only exact current-policy executable outcomes can clear live-capital review",
            "option_reality_check": (
                "Option performance gates use broker-market-observed outcomes only; "
                "modeled proxies remain research-only"
            ),
            "source_freshness_hours": MAX_SOURCE_AGE_HOURS,
        },
    }


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _source_meta(path: Path, now: datetime) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False, "modified_at": None, "age_hours": None}
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    return {
        "path": str(path),
        "exists": True,
        "modified_at": modified.isoformat(),
        "age_hours": max(0.0, (now - modified).total_seconds() / 3600.0),
    }


def _source_attestation(
    outcomes: pd.DataFrame,
    summary: dict[str, Any],
    source: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    blockers: list[str] = []
    for key in ("fixed_horizon_outcomes", "fixed_horizon_summary"):
        meta = source.get(key) if isinstance(source.get(key), dict) else {}
        age = _number(meta.get("age_hours"))
        if not meta.get("exists"):
            blockers.append(f"{key} is missing")
        elif age is None:
            blockers.append(f"{key} has no valid modification time")
        elif age > MAX_SOURCE_AGE_HOURS:
            blockers.append(f"{key} is {age:.1f}h old; maximum is {MAX_SOURCE_AGE_HOURS:.0f}h")
    expected = current_evidence_provenance()
    if not summary:
        blockers.append("fixed_horizon_summary.json is unreadable")
    else:
        if _number(summary.get("methodology_version")) != METHODOLOGY_VERSION:
            blockers.append("fixed-horizon summary methodology version does not match")
        for column in EVIDENCE_PROVENANCE_COLUMNS:
            wanted = expected[column]
            actual = summary.get(column)
            if column == "fixed_horizon_methodology_version":
                if _number(actual) != int(wanted):
                    blockers.append(f"fixed-horizon summary {column} does not match")
            elif str(actual or "") != str(wanted):
                blockers.append(f"fixed-horizon summary {column} does not match")
        digest = str(summary.get("outcomes_digest_sha256") or "")
        if digest != outcome_set_digest(outcomes):
            blockers.append("fixed-horizon summary outcome digest does not match the parquet")
        generated = pd.to_datetime(summary.get("generated_at"), errors="coerce", utc=True)
        if pd.isna(generated):
            blockers.append("fixed-horizon summary generated_at is invalid")
        else:
            generated_at = generated.to_pydatetime()
            age = (now - generated_at).total_seconds() / 3600.0
            if age < -1.0:
                blockers.append("fixed-horizon summary is future-dated")
            elif age > MAX_SOURCE_AGE_HOURS:
                blockers.append(
                    f"fixed-horizon summary was generated {age:.1f}h ago; maximum is {MAX_SOURCE_AGE_HOURS:.0f}h"
                )
        if not isinstance(summary.get("resolution_coverage"), list):
            blockers.append("fixed-horizon summary resolution coverage is missing")
    return {
        "status": "current" if not blockers else "blocked",
        "met": not blockers,
        "reason": blockers[0] if blockers else None,
        "blockers": blockers,
        "max_age_hours": MAX_SOURCE_AGE_HOURS,
        "checked_at": now.isoformat(),
        "strategy_version": expected["strategy_version"],
        "methodology_version": METHODOLOGY_VERSION,
        "policy_digest": expected["fixed_horizon_policy_digest"],
        "requires_resolution_attestation": True,
    }


def build_edge_lab(data_dir: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Load local validation sources and build the dashboard Edge Lab payload."""
    data_dir = Path(data_dir)
    generated_at = now or datetime.now(UTC)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)
    else:
        generated_at = generated_at.astimezone(UTC)
    outcome_path = data_dir / "fixed_horizon_outcomes.parquet"
    fixed_summary_path = data_dir / "fixed_horizon_summary.json"
    validation_summary_path = data_dir / "validation_summary.json"
    fixed_summary = _read_json(fixed_summary_path)
    validation_summary = _read_json(validation_summary_path)
    source = {
        "fixed_horizon_outcomes": _source_meta(outcome_path, generated_at),
        "fixed_horizon_summary": _source_meta(fixed_summary_path, generated_at),
        "validation_summary": _source_meta(validation_summary_path, generated_at),
    }
    if not outcome_path.exists():
        return {
            "schema": "optedge_edge_lab_v1",
            "generated_at": generated_at.isoformat(),
            **_unavailable_report(
                "Run validation to build Edge Lab",
                "fixed_horizon_outcomes.parquet is missing.",
                headline_horizon=HEADLINE_HORIZON,
            ),
            "source": source,
        }
    try:
        outcomes = pd.read_parquet(outcome_path)
    except Exception as exc:
        return {
            "schema": "optedge_edge_lab_v1",
            "generated_at": generated_at.isoformat(),
            **_unavailable_report(
                "Edge evidence could not be read",
                f"Fixed-horizon outcome read failed: {type(exc).__name__}",
                headline_horizon=HEADLINE_HORIZON,
            ),
            "source": source,
        }

    headline = int(
        _number(fixed_summary.get("headline_horizon_sessions"))
        or _number((validation_summary.get("fixed_horizon") or {}).get("headline_horizon_sessions"))
        or HEADLINE_HORIZON
    )
    attestation = _source_attestation(
        outcomes,
        fixed_summary,
        source,
        generated_at,
    )
    report = analyze_edge_outcomes(
        outcomes,
        headline_horizon=headline,
        source_attestation=attestation,
        resolution_coverage=(
            fixed_summary.get("resolution_coverage")
            if isinstance(fixed_summary.get("resolution_coverage"), list)
            else []
        ),
    )
    return {
        "schema": "optedge_edge_lab_v1",
        "generated_at": generated_at.isoformat(),
        "validation_generated_at": validation_summary.get("generated_at"),
        "fixed_horizon_generated_at": fixed_summary.get("generated_at"),
        **report,
        "source": source,
    }
