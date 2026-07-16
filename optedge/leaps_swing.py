# Purpose: Pure, fail-closed scoring for true LEAPS contracts used as swing trades.
"""Evaluate true LEAPS contracts without confusing contract DTE with hold time.

The scorer is deterministic and side-effect free.  A quality score is useful
for research ordering, but it never overrides policy or data blockers.  Only a
candidate with no blocker receives a non-zero execution score, and even an
``execution_ready`` result is merely eligible for a separate manual review.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from optedge.strategy_profile import LEAPS_EVIDENCE_LANE, LEAPS_SWING_PROFILE


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _first_number(row: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        result = _number(row.get(key))
        if result is not None:
            return result
    return None


def _quote_is_live(value: Any) -> bool:
    quality = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not quality:
        return False
    if any(token in quality for token in ("delayed", "free", "research", "indicative")):
        return False
    return any(token in quality for token in ("live", "broker", "real_time", "realtime"))


def _premium_dollars(row: Mapping[str, Any]) -> float | None:
    direct = _first_number(
        row,
        "premium_dollars",
        "estimated_premium_dollars",
        "one_contract_premium_dollars",
    )
    if direct is not None:
        return direct
    premium = _first_number(row, "mid", "entry_price", "reference_entry_price")
    return premium * 100.0 if premium is not None else None


def score_leaps_swing_candidate(
    row: Mapping[str, Any],
    *,
    quote_age_seconds: Any = None,
    account_budget: Any = None,
) -> dict[str, Any]:
    """Return a fail-closed LEAPS-swing assessment for one long-option row.

    ``account_budget`` is optional because chain research can run before an
    account is selected.  When supplied, one contract's full debit must fit.
    Quote age may be supplied directly or in ``row["quote_age_seconds"]``.
    """
    profile = LEAPS_SWING_PROFILE
    candidate = row if isinstance(row, Mapping) else {}
    hard_blockers: list[str] = []
    data_blockers: list[str] = []
    warnings: list[str] = []

    dte = _first_number(candidate, "dte")
    hold_sessions = _first_number(candidate, "planned_hold_sessions", "hold_sessions")
    if hold_sessions is None:
        hold_sessions = float(profile.default_hold_sessions)
    if dte is None:
        data_blockers.append("DTE is missing or invalid")
    elif dte < profile.option_min_dte or dte > profile.option_max_dte:
        hard_blockers.append(
            f"DTE {dte:g} is outside the true-LEAPS window "
            f"{profile.option_min_dte}-{profile.option_max_dte}"
        )
    if (
        hold_sessions <= 0
        or not float(hold_sessions).is_integer()
        or hold_sessions > profile.max_hold_sessions
    ):
        hard_blockers.append(
            f"planned hold must be a whole 1-{profile.max_hold_sessions} sessions"
        )

    delta = _first_number(candidate, "delta")
    abs_delta = abs(delta) if delta is not None else None
    if abs_delta is None:
        data_blockers.append("option delta is missing or invalid")
    elif abs_delta < profile.min_abs_delta or abs_delta > profile.max_abs_delta:
        hard_blockers.append(
            f"absolute delta {abs_delta:.3f} is outside "
            f"{profile.min_abs_delta:.2f}-{profile.max_abs_delta:.2f}"
        )

    spread = _first_number(candidate, "spread_pct", "source_spread_pct")
    if spread is None:
        data_blockers.append("bid/ask spread is missing or invalid")
    elif spread < 0:
        hard_blockers.append("bid/ask spread cannot be negative")
    elif spread > profile.max_spread_pct:
        hard_blockers.append(
            f"spread {spread:.1%} exceeds the {profile.max_spread_pct:.0%} hard cap"
        )

    open_interest = _first_number(candidate, "openInterest", "open_interest")
    volume = _first_number(candidate, "volume", "daily_volume")
    if open_interest is None:
        data_blockers.append("open interest is missing or invalid")
    elif open_interest < profile.min_open_interest:
        hard_blockers.append(
            f"open interest {open_interest:g} is below {profile.min_open_interest}"
        )
    if volume is None:
        data_blockers.append("daily option volume is missing or invalid")
    elif volume < 0:
        hard_blockers.append("daily option volume cannot be negative")
    elif (
        open_interest is not None
        and profile.min_open_interest <= open_interest < profile.preferred_open_interest
        and volume < profile.min_daily_volume
    ):
        hard_blockers.append(
            f"OI below {profile.preferred_open_interest} requires daily volume "
            f">= {profile.min_daily_volume}"
        )
    elif open_interest is not None and open_interest >= profile.preferred_open_interest and volume == 0:
        warnings.append("no same-day volume; deep open interest keeps this researchable")

    confidence = _first_number(candidate, "confidence")
    if confidence is None:
        data_blockers.append("confidence is missing or invalid")
    elif confidence < profile.min_confidence:
        hard_blockers.append(
            f"confidence {confidence:g} is below {profile.min_confidence:g}"
        )

    after_cost_edge = _first_number(
        candidate,
        "after_cost_edge_pct",
        "net_edge_pct",
        "buyer_edge_pct",
    )
    if after_cost_edge is None:
        data_blockers.append("after-cost directional edge is missing or invalid")
    elif after_cost_edge <= 0:
        hard_blockers.append("after-cost directional edge must be positive")

    quote_quality = candidate.get("quote_quality")
    live_quote = _quote_is_live(quote_quality)
    if not str(quote_quality or "").strip():
        data_blockers.append("quote quality is missing")
    elif not live_quote:
        data_blockers.append("quote is delayed, free, indicative, or otherwise not broker-live")
    age = _number(quote_age_seconds)
    if age is None:
        age = _first_number(candidate, "quote_age_seconds")
    if age is None:
        data_blockers.append("quote age is missing")
    elif age < -5:
        hard_blockers.append("quote age is implausibly in the future")
    elif age > profile.max_quote_age_seconds:
        data_blockers.append(
            f"quote is older than {profile.max_quote_age_seconds:g} seconds"
        )

    budget = _number(account_budget)
    premium_dollars = _premium_dollars(candidate)
    if account_budget is not None and budget is None:
        hard_blockers.append("account budget must be a finite positive number")
    elif budget is not None and budget <= 0:
        hard_blockers.append("account budget must be positive")
    elif budget is not None:
        if premium_dollars is None or premium_dollars <= 0:
            data_blockers.append("one-contract full debit is missing")
        elif premium_dollars > budget:
            hard_blockers.append(
                f"one-contract debit ${premium_dollars:,.2f} exceeds "
                f"the ${budget:,.2f} account budget"
            )
    else:
        warnings.append("account budget not supplied; downstream sizing must still pass")

    spread_points = 0
    if spread is not None and 0 <= spread <= profile.max_spread_pct:
        if spread <= 0.04:
            spread_points = 20
        elif spread <= 0.06:
            spread_points = 18
        elif spread <= profile.preferred_max_spread_pct:
            spread_points = 15
        else:
            spread_points = 10

    oi_points = 0
    if open_interest is not None:
        if open_interest >= 2_000:
            oi_points = 15
        elif open_interest >= 1_000:
            oi_points = 13
        elif open_interest >= profile.preferred_open_interest:
            oi_points = 11
        elif open_interest >= profile.min_open_interest:
            oi_points = 7
    volume_points = 0
    if volume is not None:
        if volume >= 100:
            volume_points = 5
        elif volume >= 25:
            volume_points = 4
        elif volume >= profile.min_daily_volume:
            volume_points = 3
        elif volume > 0:
            volume_points = 1

    delta_points = 0
    if abs_delta is not None:
        if profile.preferred_min_abs_delta <= abs_delta <= profile.preferred_max_abs_delta:
            delta_points = 20
        elif profile.min_abs_delta <= abs_delta <= profile.max_abs_delta:
            delta_points = 15
    dte_points = 0
    if dte is not None:
        if profile.preferred_min_dte <= dte <= profile.preferred_max_dte:
            dte_points = 10
        elif profile.option_min_dte <= dte <= profile.option_max_dte:
            dte_points = 7
    confidence_points = 0
    if confidence is not None:
        if confidence >= 80:
            confidence_points = 10
        elif confidence >= 70:
            confidence_points = 8
        elif confidence >= profile.min_confidence:
            confidence_points = 6
    edge_points = 10 if after_cost_edge is not None and after_cost_edge > 0 else 0
    freshness_points = 0
    if live_quote and age is not None and 0 <= age <= profile.max_quote_age_seconds:
        freshness_points = 5 if age <= 60 else 3
    budget_points = 5 if budget is None else (
        5
        if premium_dollars is not None and 0 < premium_dollars <= budget
        else 0
    )
    breakdown = {
        "spread": spread_points,
        "open_interest": oi_points,
        "volume": volume_points,
        "delta": delta_points,
        "dte_runway": dte_points,
        "confidence": confidence_points,
        "after_cost_edge": edge_points,
        "quote_freshness": freshness_points,
        "budget_fit": budget_points,
    }
    quality_score = int(sum(breakdown.values()))

    hard_blockers = list(dict.fromkeys(hard_blockers))
    data_blockers = list(dict.fromkeys(data_blockers))
    warnings = list(dict.fromkeys(warnings))
    if hard_blockers:
        status = "blocked"
    elif data_blockers:
        status = "research_only"
    else:
        status = "execution_ready"
    execution_ready = status == "execution_ready"

    return {
        "strategy_profile": profile.name,
        "policy_version": profile.policy_version,
        "evidence_lane": LEAPS_EVIDENCE_LANE,
        "status": status,
        "execution_ready": execution_ready,
        "research_only": status == "research_only",
        "quality_score": quality_score,
        "execution_score": quality_score if execution_ready else 0,
        "hard_blockers": hard_blockers,
        "data_blockers": data_blockers,
        "warnings": warnings,
        "score_breakdown": breakdown,
        "contract_policy": {
            "dte": dte,
            "dte_min": profile.option_min_dte,
            "dte_max": profile.option_max_dte,
            "abs_delta": abs_delta,
            "spread_pct": spread,
            "open_interest": open_interest,
            "daily_volume": volume,
            "confidence": confidence,
            "after_cost_edge_pct": after_cost_edge,
            "quote_age_seconds": age,
            "quote_quality": quote_quality,
            "one_contract_premium_dollars": premium_dollars,
            "account_budget": budget,
        },
        "holding_policy": {
            "planned_hold_sessions": int(hold_sessions),
            "review_sessions": list(profile.review_sessions),
            "max_hold_sessions": profile.max_hold_sessions,
            "contract_dte_is_not_hold_time": True,
        },
        "management_references": {
            "stop_loss_fraction": profile.stop_loss_fraction,
            "target_gain_fraction": profile.target_gain_fraction,
            "breakeven_review_trigger_fraction": (
                profile.breakeven_review_trigger_fraction
            ),
            "manual_management_only": profile.manual_management_only,
        },
        "does_not_place_orders": True,
    }


__all__ = ["score_leaps_swing_candidate"]
