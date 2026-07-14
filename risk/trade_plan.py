# Purpose: Pure trade sizing and approval-gated Robinhood review packets.
"""Pure trade sizing and approval-gated Robinhood review packets.

This module deliberately has no filesystem, network, broker, credential, or
automation dependency.  It turns explicit risk inputs into a deterministic
whole-unit trade plan, then converts an actionable plan into manual Robinhood
MCP review instructions.  The packet boundary checks the current clock only to
reject stale or overlong review packets.  A review packet never places an order.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any

from optedge.strategy_profile import (
    SWING_EXECUTION_MAX_OPTION_SPREAD_PCT,
    SWING_EXECUTION_OPTION_MIN_DTE,
)

TRADE_PLAN_SCHEMA = "optedge_trade_plan_v1"
ACCOUNT_LIMITS_SCHEMA = "optedge_account_limits_v1"
ROBINHOOD_EQUITY_REVIEW_SCHEMA = "optedge_robinhood_equity_review_plan_v1"
ROBINHOOD_OPTION_REVIEW_SCHEMA = "optedge_robinhood_option_review_plan_v1"
MANUAL_REVIEW_PACKET_SCHEMA = "optedge_manual_robinhood_review_packet_v2"
MANUAL_REVIEW_PACKET_INTEGRITY_SCHEMA = "optedge_manual_review_integrity_v1"

ACCOUNT_NUMBER_PLACEHOLDER = "<explicit_user_confirmed_account_number>"
OPTION_ID_PLACEHOLDER = "<option_id_from_get_option_instruments>"
REF_ID_PLACEHOLDER = "<fresh_uuid_generated_once_after_exact_confirmation>"
SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")
MAX_MANUAL_REVIEW_PACKET_TTL_SECONDS = 15 * 60
MAX_MANUAL_REVIEW_CLOCK_SKEW_SECONDS = 60
MAX_TRADE_RISK_FRACTION = 0.02
MAX_ACCOUNT_ALLOCATION_FRACTION = 0.25
MANUAL_REVIEW_BASE_RISK_FRACTION = 0.01
ACCOUNT_DRAWDOWN_REVIEW_SCHEMA = "optedge_account_drawdown_review_constraints_v1"
ACCOUNT_DRAWDOWN_INTERLOCK_SCHEMA = "optedge_robinhood_account_drawdown_interlock_v1"
ACCOUNT_DRAWDOWN_POLICY_VERSION = "robinhood_account_drawdown_v2"
SHARE_CANDIDATE_REVIEW_SCHEMA = "optedge_share_candidate_review_attestation_v1"
OPTION_CANDIDATE_REVIEW_SCHEMA = "optedge_option_candidate_review_attestation_v1"
ACCOUNT_KEY_DERIVATION_SCHEMA = "optedge_robinhood_account_key_derivation_v1"
ACCOUNT_KEY_DERIVATION_NAMESPACE = "optedge-robinhood-account-v1|"
ACCOUNT_KEY_DERIVATION_HEX_LENGTH = 16
OPTEDGE_ORDER_REF_NAMESPACE = uuid.UUID("60d21b6d-517b-5d2d-b303-6ce65ff6a725")

__all__ = [
    "calculate_account_limits",
    "size_share_trade",
    "size_long_option_trade",
    "build_robinhood_equity_review_plan",
    "build_robinhood_option_review_plan",
    "build_manual_robinhood_review_packet",
    "validate_manual_robinhood_review_packet",
    "render_manual_robinhood_review_prompt",
]


def _number(value: Any) -> float | None:
    """Return a finite float without converting missing/invalid data to zero."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _money(value: float | None) -> float | None:
    return None if value is None else _cent_price(value)


def _cent_price(value: float, *, rounding: str = ROUND_HALF_UP) -> float:
    """Return an exact two-decimal price using explicit, broker-safe rounding."""
    return float(Decimal(str(float(value))).quantize(Decimal("0.01"), rounding=rounding))


def _entry_limit_price(value: float, direction: str) -> float:
    """Normalize executable limits before sizing so displayed and risk math agree.

    A buy limit rounds up to avoid understating committed capital. A sell limit
    rounds down so a research-only short plan never assumes a better fill than
    the price sent to the broker.
    """
    rounding = ROUND_CEILING if direction == "long" else ROUND_FLOOR
    return _cent_price(value, rounding=rounding)


def _metric(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(float(value), digits)


def _same_money(left: Any, right: Any, *, tolerance: float = 0.011) -> bool:
    left_value = _number(left)
    right_value = _number(right)
    return (
        left_value is not None
        and right_value is not None
        and abs(left_value - right_value) <= tolerance
    )


def _issue(code: str, field: str, message: str) -> dict[str, str]:
    return {"code": code, "field": field, "message": message}


def _clean_symbol(value: Any, errors: list[dict[str, str]]) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol:
        errors.append(_issue("missing_symbol", "symbol", "A symbol is required."))
    elif not SYMBOL_PATTERN.fullmatch(symbol):
        errors.append(
            _issue(
                "invalid_symbol",
                "symbol",
                "symbol must be 1-15 ticker characters using letters, numbers, period, or hyphen.",
            )
        )
    return symbol


def _prompt_text(value: Any, *, limit: int = 180) -> str:
    """Flatten untrusted display text before it enters an agent instruction."""
    text = " ".join(str(value or "").replace("\x00", "").split())
    return text[:limit]


def _required_positive(value: Any, field: str, errors: list[dict[str, str]]) -> float | None:
    result = _number(value)
    if result is None:
        errors.append(_issue(f"missing_or_invalid_{field}", field, f"{field} must be a finite number."))
        return None
    if result <= 0:
        errors.append(_issue(f"non_positive_{field}", field, f"{field} must be greater than zero."))
        return None
    return result


def _required_nonnegative(value: Any, field: str, errors: list[dict[str, str]]) -> float | None:
    result = _number(value)
    if result is None:
        errors.append(_issue(f"missing_or_invalid_{field}", field, f"{field} must be a finite number."))
        return None
    if result < 0:
        errors.append(_issue(f"negative_{field}", field, f"{field} cannot be negative."))
        return None
    return result


def _validation(errors: list[dict[str, str]], warnings: list[dict[str, str]]) -> dict[str, Any]:
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def _capacity_count(capacity: float, cost: float) -> int:
    if capacity < 0 or cost <= 0:
        return 0
    return max(0, int(math.floor((capacity / cost) + 1e-12)))


def _binding_constraints(capacities: dict[str, int]) -> list[str]:
    if not capacities:
        return []
    floor_value = min(capacities.values())
    return [name for name, value in capacities.items() if value == floor_value]


def calculate_account_limits(
    account_equity: Any,
    risk_fraction: Any,
    allocation_fraction: Any,
    *,
    buying_power: Any = None,
) -> dict[str, Any]:
    """Convert account-level fractions into explicit dollar limits.

    Fractions are decimal values: ``0.01`` means 1%.  Buying power is optional;
    when supplied, it can only reduce the effective allocation cap.
    """
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    equity = _required_positive(account_equity, "account_equity", errors)
    risk = _required_positive(risk_fraction, "risk_fraction", errors)
    allocation = _required_positive(allocation_fraction, "allocation_fraction", errors)
    power = None
    if buying_power is not None:
        power = _required_nonnegative(buying_power, "buying_power", errors)

    if risk is not None and risk > MAX_TRADE_RISK_FRACTION:
        errors.append(
            _issue(
                "risk_fraction_above_hard_cap",
                "risk_fraction",
                f"risk_fraction cannot exceed {MAX_TRADE_RISK_FRACTION:.2%}.",
            )
        )
    if allocation is not None and allocation > MAX_ACCOUNT_ALLOCATION_FRACTION:
        errors.append(
            _issue(
                "allocation_fraction_above_hard_cap",
                "allocation_fraction",
                "allocation_fraction cannot exceed 25% of account equity.",
            )
        )

    risk_budget = None
    requested_allocation = None
    effective_allocation = None
    if not errors and equity is not None and risk is not None and allocation is not None:
        risk_budget = equity * risk
        requested_allocation = equity * allocation
        effective_allocation = min(requested_allocation, power) if power is not None else requested_allocation
        if effective_allocation == 0:
            errors.append(_issue("no_buying_power", "buying_power", "No positive buying power is available."))

    return {
        "schema": ACCOUNT_LIMITS_SCHEMA,
        "status": "ready" if not errors else "invalid",
        "account_equity": _money(equity),
        "risk_fraction": _metric(risk),
        "allocation_fraction": _metric(allocation),
        "buying_power": _money(power),
        "risk_budget_dollars": _money(risk_budget),
        "requested_allocation_cap_dollars": _money(requested_allocation),
        "effective_allocation_cap_dollars": _money(effective_allocation),
        "validation": _validation(errors, warnings),
        "notes": [
            "Buying power is optional and only tightens the allocation cap.",
            "No account identifier or broker credential is accepted or stored.",
        ],
    }


def _empty_trade_plan(
    *,
    asset: str,
    symbol: str,
    direction: str,
    order: dict[str, Any],
    risk_budget: float | None,
    allocation_cap: float | None,
    errors: list[dict[str, str]],
    warnings: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "schema": TRADE_PLAN_SCHEMA,
        "asset": asset,
        "symbol": symbol,
        "direction": direction,
        "status": "invalid",
        "is_actionable": False,
        "order": order,
        "capacity": {
            "units_by_risk_budget": None,
            "units_by_allocation_cap": None,
            "binding_constraints": [],
        },
        "risk": {
            "risk_budget_dollars": _money(risk_budget),
            "allocation_cap_dollars": _money(allocation_cap),
            "planned_risk_per_unit_dollars": None,
            "planned_stop_risk_per_unit_dollars": None,
            "planned_stop_loss_dollars": None,
            "planned_max_loss_dollars": None,
            "full_share_notional_at_risk_dollars": None,
            "full_option_debit_at_risk_dollars": None,
            "max_loss_is_unbounded": False,
            "reward_per_unit_dollars": None,
            "planned_reward_dollars": None,
            "reward_risk_ratio": None,
            "max_loss_reward_risk_ratio": None,
            "breakeven_win_rate": None,
            "max_loss_breakeven_win_rate": None,
            "risk_budget_utilization": None,
            "stop_risk_budget_utilization": None,
            "allocation_cap_utilization": None,
            "risk_budget_basis": None,
            "stop_is_not_broker_order": True,
        },
        "validation": _validation(errors, warnings),
    }


def size_share_trade(
    *,
    symbol: Any,
    direction: Any,
    entry_price: Any,
    stop_price: Any,
    target_price: Any,
    risk_budget_dollars: Any,
    allocation_cap_dollars: Any,
    round_trip_slippage_per_share: Any = 0.0,
) -> dict[str, Any]:
    """Size a long or short share trade using whole shares only."""
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    clean_symbol = _clean_symbol(symbol, errors)
    clean_direction = str(direction or "").strip().lower()
    if clean_direction not in {"long", "short"}:
        errors.append(_issue("invalid_direction", "direction", "direction must be 'long' or 'short'."))

    entry = _required_positive(entry_price, "entry_price", errors)
    stop = _required_nonnegative(stop_price, "stop_price", errors)
    target = _required_nonnegative(target_price, "target_price", errors)
    risk_budget = _required_positive(risk_budget_dollars, "risk_budget_dollars", errors)
    allocation_cap = _required_positive(allocation_cap_dollars, "allocation_cap_dollars", errors)
    slippage = _required_nonnegative(
        round_trip_slippage_per_share,
        "round_trip_slippage_per_share",
        errors,
    )

    # Size from the exact cent values that will appear in a broker review.
    if entry is not None and clean_direction in {"long", "short"}:
        entry = _entry_limit_price(entry, clean_direction)
    if stop is not None:
        stop = _cent_price(stop)
    if target is not None:
        target = _cent_price(target)

    if entry is not None and stop is not None:
        if clean_direction == "long" and stop >= entry:
            errors.append(_issue("invalid_long_stop", "stop_price", "A long-share stop must be below entry."))
        if clean_direction == "short" and stop <= entry:
            errors.append(_issue("invalid_short_stop", "stop_price", "A short-share stop must be above entry."))
    if entry is not None and target is not None:
        if clean_direction == "long" and target <= entry:
            errors.append(_issue("invalid_long_target", "target_price", "A long-share target must be above entry."))
        if clean_direction == "short" and target >= entry:
            errors.append(_issue("invalid_short_target", "target_price", "A short-share target must be below entry."))

    side = "buy" if clean_direction == "long" else "sell" if clean_direction == "short" else None
    intent = "open_long" if clean_direction == "long" else "open_short" if clean_direction == "short" else None
    order = {
        "asset": "share",
        "symbol": clean_symbol or None,
        "direction": clean_direction or None,
        "intent": intent,
        "side": side,
        "order_type": "limit",
        "time_in_force": "gfd",
        "market_hours": "regular_hours",
        "quantity": None,
        "unit_name": "shares",
        "entry_price": _money(entry),
        "limit_price": _money(entry),
        "stop_price": _money(stop),
        "target_price": _money(target),
        "estimated_notional_dollars": None,
        "stop_order_included": False,
        "target_order_included": False,
    }
    if errors:
        return _empty_trade_plan(
            asset="share",
            symbol=clean_symbol,
            direction=clean_direction,
            order=order,
            risk_budget=risk_budget,
            allocation_cap=allocation_cap,
            errors=errors,
            warnings=warnings,
        )

    assert entry is not None and stop is not None and target is not None
    assert risk_budget is not None and allocation_cap is not None and slippage is not None
    planned_risk_per_share = abs(entry - stop) + slippage
    reward_per_share = abs(target - entry) - slippage
    if reward_per_share <= 0:
        errors.append(
            _issue(
                "non_positive_reward_after_slippage",
                "target_price",
                "Target distance must exceed round-trip slippage.",
            )
        )
        return _empty_trade_plan(
            asset="share",
            symbol=clean_symbol,
            direction=clean_direction,
            order=order,
            risk_budget=risk_budget,
            allocation_cap=allocation_cap,
            errors=errors,
            warnings=warnings,
        )

    capacities = {
        "risk_budget": _capacity_count(risk_budget, planned_risk_per_share),
        "allocation_cap": _capacity_count(allocation_cap, entry),
    }
    quantity = min(capacities.values())
    if quantity == 0:
        errors.append(
            _issue(
                "no_whole_shares_fit",
                "quantity",
                "The risk budget and allocation cap do not allow one whole share.",
            )
        )

    planned_loss = planned_risk_per_share * quantity
    planned_reward = reward_per_share * quantity
    notional = entry * quantity
    reward_risk = reward_per_share / planned_risk_per_share
    breakeven = planned_risk_per_share / (planned_risk_per_share + reward_per_share)
    order["quantity"] = quantity
    order["estimated_notional_dollars"] = _money(notional)
    if reward_risk < 1.5:
        warnings.append(
            _issue(
                "low_planned_reward_risk",
                "target_price",
                "Planned reward/risk is below 1.5; verify that the setup's evidence supports the lower payoff ratio.",
            )
        )
    warnings.append(
        _issue(
            "entry_order_only",
            "stop_price",
            "The Robinhood handoff places only the entry limit; stop and target remain planning references.",
        )
    )
    maximum_loss = notional if clean_direction == "long" else None
    max_loss_reward_risk = reward_per_share / entry if clean_direction == "long" else None
    max_loss_breakeven = entry / (entry + reward_per_share) if clean_direction == "long" else None
    status = "ready_for_manual_review" if quantity > 0 else "blocked"
    return {
        "schema": TRADE_PLAN_SCHEMA,
        "asset": "share",
        "symbol": clean_symbol,
        "direction": clean_direction,
        "status": status,
        "is_actionable": quantity > 0,
        "order": order,
        "capacity": {
            "units_by_risk_budget": capacities["risk_budget"],
            "units_by_allocation_cap": capacities["allocation_cap"],
            "binding_constraints": _binding_constraints(capacities),
        },
        "risk": {
            "risk_budget_dollars": _money(risk_budget),
            "allocation_cap_dollars": _money(allocation_cap),
            "planned_risk_per_unit_dollars": _money(planned_risk_per_share),
            "planned_stop_risk_per_unit_dollars": _money(planned_risk_per_share),
            "planned_stop_loss_dollars": _money(planned_loss),
            "planned_max_loss_dollars": _money(maximum_loss),
            "full_share_notional_at_risk_dollars": _money(notional),
            "full_option_debit_at_risk_dollars": None,
            "max_loss_is_unbounded": clean_direction == "short",
            "reward_per_unit_dollars": _money(reward_per_share),
            "planned_reward_dollars": _money(planned_reward),
            "reward_risk_ratio": _metric(reward_risk),
            "max_loss_reward_risk_ratio": _metric(max_loss_reward_risk),
            "breakeven_win_rate": _metric(breakeven),
            "max_loss_breakeven_win_rate": _metric(max_loss_breakeven),
            "risk_budget_utilization": _metric(planned_loss / risk_budget),
            "stop_risk_budget_utilization": _metric(planned_loss / risk_budget),
            "allocation_cap_utilization": _metric(notional / allocation_cap),
            "risk_budget_basis": "planned_stop_loss",
            "stop_is_not_broker_order": True,
        },
        "validation": _validation(errors, warnings),
    }


def size_long_option_trade(
    *,
    symbol: Any,
    option_type: Any,
    expiry: Any,
    strike: Any,
    entry_premium: Any,
    stop_premium: Any,
    target_premium: Any,
    risk_budget_dollars: Any,
    allocation_cap_dollars: Any,
    contract_multiplier: Any = 100,
    round_trip_slippage_per_contract: Any = 0.0,
    underlying_type: Any = "equity",
) -> dict[str, Any]:
    """Size a long call or put using whole contracts only.

    Both the risk budget and allocation cap limit the full option debit. The
    planned stop remains visible as a non-guaranteed management reference.
    """
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    clean_symbol = _clean_symbol(symbol, errors)
    clean_type = str(option_type or "").strip().lower()
    clean_expiry = str(expiry or "").strip()
    clean_underlying = str(underlying_type or "").strip().lower()
    if clean_type not in {"call", "put"}:
        errors.append(_issue("invalid_option_type", "option_type", "option_type must be 'call' or 'put'."))
    parsed_expiry = None
    try:
        parsed_expiry = date.fromisoformat(clean_expiry)
    except (TypeError, ValueError):
        errors.append(_issue("invalid_expiry", "expiry", "expiry must be YYYY-MM-DD."))
    if parsed_expiry is not None and parsed_expiry < date.today():
        errors.append(_issue("expired_contract", "expiry", "expiry cannot be in the past."))
    if clean_underlying not in {"equity", "index"}:
        errors.append(
            _issue("invalid_underlying_type", "underlying_type", "underlying_type must be 'equity' or 'index'.")
        )

    strike_value = _required_positive(strike, "strike", errors)
    entry = _required_positive(entry_premium, "entry_premium", errors)
    stop = _required_nonnegative(stop_premium, "stop_premium", errors)
    target = _required_positive(target_premium, "target_premium", errors)
    risk_budget = _required_positive(risk_budget_dollars, "risk_budget_dollars", errors)
    allocation_cap = _required_positive(allocation_cap_dollars, "allocation_cap_dollars", errors)
    multiplier = _required_positive(contract_multiplier, "contract_multiplier", errors)
    slippage = _required_nonnegative(
        round_trip_slippage_per_contract,
        "round_trip_slippage_per_contract",
        errors,
    )
    # Long-option buy limits round up before every capacity/risk calculation;
    # the review packet, debit, and maximum-loss figures then share one price.
    if entry is not None:
        entry = _entry_limit_price(entry, "long")
    if stop is not None:
        stop = _cent_price(stop)
    if target is not None:
        target = _cent_price(target)
    if multiplier is not None and not float(multiplier).is_integer():
        errors.append(_issue("non_integer_multiplier", "contract_multiplier", "contract_multiplier must be an integer."))
    if entry is not None and stop is not None and stop >= entry:
        errors.append(_issue("invalid_long_option_stop", "stop_premium", "A long-option stop must be below entry."))
    if entry is not None and target is not None and target <= entry:
        errors.append(_issue("invalid_long_option_target", "target_premium", "A long-option target must be above entry."))

    direction = f"long_{clean_type}" if clean_type in {"call", "put"} else ""
    contract_label = None
    if clean_symbol and clean_expiry and clean_type in {"call", "put"} and strike_value is not None:
        contract_label = f"{clean_symbol} {clean_expiry} {clean_type.upper()} {strike_value:g}"
    order = {
        "asset": "option",
        "symbol": clean_symbol or None,
        "direction": direction or None,
        "intent": "buy_to_open",
        "side": "buy",
        "position_effect": "open",
        "option_type": clean_type or None,
        "expiry": clean_expiry or None,
        "strike": _metric(strike_value),
        "underlying_type": clean_underlying or None,
        "contract_label": contract_label,
        "order_type": "limit",
        "time_in_force": "gfd",
        "market_hours": "regular_hours",
        "quantity": None,
        "unit_name": "contracts",
        "entry_price": _money(entry),
        "limit_price": _money(entry),
        "stop_price": _money(stop),
        "target_price": _money(target),
        "contract_multiplier": int(multiplier) if multiplier is not None and multiplier.is_integer() else None,
        "estimated_debit_dollars": None,
        "stop_order_included": False,
        "target_order_included": False,
    }
    if errors:
        return _empty_trade_plan(
            asset="option",
            symbol=clean_symbol,
            direction=direction,
            order=order,
            risk_budget=risk_budget,
            allocation_cap=allocation_cap,
            errors=errors,
            warnings=warnings,
        )

    assert entry is not None and stop is not None and target is not None
    assert risk_budget is not None and allocation_cap is not None
    assert multiplier is not None and slippage is not None
    debit_per_contract = entry * multiplier
    planned_risk_per_contract = (entry - stop) * multiplier + slippage
    reward_per_contract = (target - entry) * multiplier - slippage
    if reward_per_contract <= 0:
        errors.append(
            _issue(
                "non_positive_reward_after_slippage",
                "target_premium",
                "Target distance must exceed round-trip contract slippage.",
            )
        )
        return _empty_trade_plan(
            asset="option",
            symbol=clean_symbol,
            direction=direction,
            order=order,
            risk_budget=risk_budget,
            allocation_cap=allocation_cap,
            errors=errors,
            warnings=warnings,
        )

    capacities = {
        "risk_budget": _capacity_count(risk_budget, debit_per_contract),
        "allocation_cap": _capacity_count(allocation_cap, debit_per_contract),
    }
    quantity = min(capacities.values())
    if quantity == 0:
        errors.append(
            _issue(
                "no_whole_contracts_fit",
                "quantity",
                "The full-debit risk budget and allocation cap do not allow one contract.",
            )
        )

    planned_loss = planned_risk_per_contract * quantity
    full_debit = debit_per_contract * quantity
    planned_reward = reward_per_contract * quantity
    reward_risk = reward_per_contract / planned_risk_per_contract
    breakeven = planned_risk_per_contract / (planned_risk_per_contract + reward_per_contract)
    max_loss_reward_risk = reward_per_contract / debit_per_contract
    max_loss_breakeven = debit_per_contract / (debit_per_contract + reward_per_contract)
    order["quantity"] = quantity
    order["estimated_debit_dollars"] = _money(full_debit)
    if reward_risk < 1.5:
        warnings.append(
            _issue(
                "low_planned_reward_risk",
                "target_premium",
                "Planned stop reward/risk is below 1.5; verify that the setup's evidence supports it.",
            )
        )
    warnings.append(
        _issue(
            "entry_order_only",
            "stop_premium",
            "The Robinhood handoff places only the entry limit; stop and target premiums are planning references.",
        )
    )
    status = "ready_for_manual_review" if quantity > 0 else "blocked"
    return {
        "schema": TRADE_PLAN_SCHEMA,
        "asset": "option",
        "symbol": clean_symbol,
        "direction": direction,
        "status": status,
        "is_actionable": quantity > 0,
        "order": order,
        "capacity": {
            "units_by_risk_budget": capacities["risk_budget"],
            "units_by_allocation_cap": capacities["allocation_cap"],
            "binding_constraints": _binding_constraints(capacities),
        },
        "risk": {
            "risk_budget_dollars": _money(risk_budget),
            "allocation_cap_dollars": _money(allocation_cap),
            "planned_risk_per_unit_dollars": _money(planned_risk_per_contract),
            "planned_stop_risk_per_unit_dollars": _money(planned_risk_per_contract),
            "planned_stop_loss_dollars": _money(planned_loss),
            "planned_max_loss_dollars": _money(full_debit),
            "full_share_notional_at_risk_dollars": None,
            "full_option_debit_at_risk_dollars": _money(full_debit),
            "max_loss_is_unbounded": False,
            "reward_per_unit_dollars": _money(reward_per_contract),
            "planned_reward_dollars": _money(planned_reward),
            "reward_risk_ratio": _metric(reward_risk),
            "max_loss_reward_risk_ratio": _metric(max_loss_reward_risk),
            "breakeven_win_rate": _metric(breakeven),
            "max_loss_breakeven_win_rate": _metric(max_loss_breakeven),
            "risk_budget_utilization": _metric(full_debit / risk_budget),
            "stop_risk_budget_utilization": _metric(planned_loss / risk_budget),
            "allocation_cap_utilization": _metric(full_debit / allocation_cap),
            "risk_budget_basis": "full_option_debit",
            "stop_is_not_broker_order": True,
        },
        "validation": _validation(errors, warnings),
    }


def _review_errors(trade_plan: Any, expected_asset: str) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if not isinstance(trade_plan, dict):
        return [_issue("invalid_trade_plan", "trade_plan", "trade_plan must be a dictionary.")]
    if trade_plan.get("schema") != TRADE_PLAN_SCHEMA:
        errors.append(_issue("invalid_trade_plan_schema", "schema", "Trade plan schema is missing or unsupported."))
    if trade_plan.get("asset") != expected_asset:
        errors.append(
            _issue("wrong_asset", "asset", f"Expected a {expected_asset} trade plan.")
        )
    if trade_plan.get("status") != "ready_for_manual_review":
        errors.append(_issue("invalid_trade_plan_status", "status", "Trade plan is not ready for manual review."))
    if trade_plan.get("is_actionable") is not True:
        errors.append(_issue("trade_plan_not_actionable", "status", "Trade plan is not actionable."))
    validation = trade_plan.get("validation") if isinstance(trade_plan.get("validation"), dict) else {}
    if validation.get("ok") is not True or validation.get("errors"):
        errors.append(_issue("trade_plan_has_errors", "validation", "Trade plan contains validation errors."))
    order = trade_plan.get("order") if isinstance(trade_plan.get("order"), dict) else {}
    if order.get("asset") != expected_asset:
        errors.append(_issue("order_asset_mismatch", "order.asset", "Order asset does not match the trade plan."))
    symbol = str(order.get("symbol") or "").strip().upper()
    if not SYMBOL_PATTERN.fullmatch(symbol):
        errors.append(_issue("invalid_order_symbol", "symbol", "Order symbol is missing or invalid."))
    quantity = order.get("quantity")
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
        errors.append(_issue("invalid_quantity", "quantity", "A positive whole-unit quantity is required."))
    limit_price = _number(order.get("limit_price"))
    if limit_price is None or limit_price <= 0:
        errors.append(_issue("missing_limit_price", "limit_price", "A positive finite limit price is required."))
    if order.get("order_type") != "limit":
        errors.append(_issue("unsupported_order_type", "order_type", "Only limit entry orders are supported."))
    if order.get("time_in_force") != "gfd":
        errors.append(_issue("unsupported_time_in_force", "time_in_force", "Only good-for-day orders are supported."))
    if order.get("market_hours") != "regular_hours":
        errors.append(_issue("unsupported_market_hours", "market_hours", "Only regular-hours review is supported."))
    if order.get("stop_order_included") is not False or order.get("target_order_included") is not False:
        errors.append(
            _issue(
                "ambiguous_exit_order",
                "stop_order_included",
                "The packet must explicitly identify stop and target as planning references, not broker orders.",
            )
        )
    risk = trade_plan.get("risk") if isinstance(trade_plan.get("risk"), dict) else {}
    if risk.get("max_loss_is_unbounded") is not False:
        errors.append(
            _issue(
                "unbounded_or_unproven_maximum_loss",
                "risk.max_loss_is_unbounded",
                "Manual review requires maximum loss to be explicitly bounded.",
            )
        )
    if risk.get("stop_is_not_broker_order") is not True:
        errors.append(
            _issue(
                "ambiguous_stop_execution",
                "risk.stop_is_not_broker_order",
                "The planning stop must be explicitly identified as not resting at the broker.",
            )
        )
    planned_max_loss = _number(risk.get("planned_max_loss_dollars"))
    if planned_max_loss is None or planned_max_loss <= 0:
        errors.append(_issue("missing_max_loss", "planned_max_loss_dollars", "A positive maximum-loss reference is required."))
    return errors


def _blocked_review_plan(schema: str, asset: str, review_tool: str, place_tool: str, errors: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "schema": schema,
        "broker": "robinhood",
        "asset": asset,
        "status": "blocked",
        "review_allowed": False,
        "review_tool": review_tool,
        "place_tool_after_explicit_confirmation": place_tool,
        "review_arguments_template": None,
        "place_arguments_after_confirmation": None,
        "requires_explicit_user_confirmation_before_place": True,
        "script_submits_live_orders": False,
        "automation_allowed": False,
        "repeat_orders_allowed": False,
        "stores_credentials": False,
        "validation": _validation(errors, []),
    }


def build_robinhood_equity_review_plan(trade_plan: dict[str, Any]) -> dict[str, Any]:
    """Build a manual review-first plan using current Robinhood equity tool names."""
    errors = _review_errors(trade_plan, "share")
    order = trade_plan.get("order") if isinstance(trade_plan, dict) else {}
    direction = trade_plan.get("direction") if isinstance(trade_plan, dict) else None
    if order.get("intent") != "open_long" or order.get("side") != "buy" or direction != "long":
        errors.append(
            _issue(
                "unsupported_equity_intent",
                "intent",
                "Robinhood Agentic execution is fail-closed to long buy orders in this version.",
            )
        )
    quantity = order.get("quantity")
    limit_price = _number(order.get("limit_price"))
    risk = trade_plan.get("risk") if isinstance(trade_plan, dict) and isinstance(trade_plan.get("risk"), dict) else {}
    if isinstance(quantity, int) and not isinstance(quantity, bool) and quantity > 0 and limit_price is not None:
        expected_notional = round(quantity * limit_price, 2)
        if not _same_money(order.get("estimated_notional_dollars"), expected_notional):
            errors.append(
                _issue(
                    "equity_notional_mismatch",
                    "estimated_notional_dollars",
                    "Share notional does not reconcile to quantity times limit price.",
                )
            )
        if not _same_money(risk.get("full_share_notional_at_risk_dollars"), expected_notional):
            errors.append(
                _issue(
                    "share_max_loss_mismatch",
                    "full_share_notional_at_risk_dollars",
                    "Long-share capital-loss reference does not reconcile to the entry order.",
                )
            )
        if not _same_money(risk.get("planned_max_loss_dollars"), expected_notional):
            errors.append(
                _issue(
                    "share_planned_max_loss_mismatch",
                    "planned_max_loss_dollars",
                    "Long-share maximum loss must reconcile to the full entry notional.",
                )
            )
        allocation_cap = _number(risk.get("allocation_cap_dollars"))
        if allocation_cap is None or expected_notional > allocation_cap + 0.01:
            errors.append(
                _issue(
                    "equity_notional_exceeds_allocation_cap",
                    "allocation_cap_dollars",
                    "Share notional must fit inside the allocation cap.",
                )
            )
        entry_price = _number(order.get("entry_price"))
        stop_price = _number(order.get("stop_price"))
        target_price = _number(order.get("target_price"))
        planned_risk_per_unit = _number(risk.get("planned_risk_per_unit_dollars"))
        planned_stop_risk_per_unit = _number(
            risk.get("planned_stop_risk_per_unit_dollars")
        )
        planned_stop_loss = _number(risk.get("planned_stop_loss_dollars"))
        risk_budget = _number(risk.get("risk_budget_dollars"))
        if entry_price is None or not _same_money(entry_price, limit_price):
            errors.append(
                _issue(
                    "equity_entry_price_mismatch",
                    "order.entry_price",
                    "Share entry price must match the reviewed limit price.",
                )
            )
        if stop_price is None or stop_price < 0 or stop_price >= limit_price:
            errors.append(
                _issue(
                    "invalid_review_stop_price",
                    "order.stop_price",
                    "A long-share planning stop must be non-negative and below entry.",
                )
            )
        if target_price is None or target_price <= limit_price:
            errors.append(
                _issue(
                    "invalid_review_target_price",
                    "order.target_price",
                    "A long-share planning target must be finite and above entry.",
                )
            )
        if (
            planned_risk_per_unit is None
            or planned_risk_per_unit <= 0
            or planned_stop_risk_per_unit is None
            or not _same_money(planned_stop_risk_per_unit, planned_risk_per_unit)
        ):
            errors.append(
                _issue(
                    "share_stop_risk_per_unit_mismatch",
                    "risk.planned_stop_risk_per_unit_dollars",
                    "Per-share stop risk fields must be positive and reconcile exactly.",
                )
            )
        elif stop_price is not None and planned_risk_per_unit + 0.011 < limit_price - stop_price:
            errors.append(
                _issue(
                    "share_stop_risk_understates_price_distance",
                    "risk.planned_stop_risk_per_unit_dollars",
                    "Per-share stop risk cannot be smaller than entry minus the planning stop.",
                )
            )
        if (
            planned_risk_per_unit is None
            or planned_stop_loss is None
            or not _same_money(planned_stop_loss, planned_risk_per_unit * quantity)
        ):
            errors.append(
                _issue(
                    "share_planned_stop_loss_mismatch",
                    "risk.planned_stop_loss_dollars",
                    "Planned stop loss must equal per-share stop risk times quantity.",
                )
            )
        if risk_budget is None or planned_stop_loss is None or planned_stop_loss > risk_budget + 0.01:
            errors.append(
                _issue(
                    "share_stop_loss_exceeds_risk_budget",
                    "risk.planned_stop_loss_dollars",
                    "Planned stop loss must fit inside the trade-plan risk budget.",
                )
            )
        if risk.get("risk_budget_basis") != "planned_stop_loss":
            errors.append(
                _issue(
                    "unsafe_share_risk_budget_basis",
                    "risk.risk_budget_basis",
                    "Share review must size risk from the planned stop loss.",
                )
            )
    if errors:
        return _blocked_review_plan(
            ROBINHOOD_EQUITY_REVIEW_SCHEMA,
            "share",
            "review_equity_order",
            "place_equity_order",
            errors,
        )

    order = trade_plan["order"]
    review_args = {
        "account_number": ACCOUNT_NUMBER_PLACEHOLDER,
        "symbol": order["symbol"],
        "side": order["side"],
        "quantity": str(order["quantity"]),
        "type": "limit",
        "limit_price": f"{float(order['limit_price']):.2f}",
        "time_in_force": "gfd",
        "market_hours": "regular_hours",
    }
    place_args = dict(review_args)
    place_args["ref_id"] = REF_ID_PLACEHOLDER
    return {
        "schema": ROBINHOOD_EQUITY_REVIEW_SCHEMA,
        "broker": "robinhood",
        "asset": "share",
        "capability": "single_equity_limit_order",
        "intent": order.get("intent"),
        "status": "review_required_before_any_place_order",
        "review_allowed": True,
        "requires_agentic_allowed_account": True,
        "requires_short_sale_review": False,
        "requires_explicit_user_confirmation_before_place": True,
        "script_submits_live_orders": False,
        "automation_allowed": False,
        "repeat_orders_allowed": False,
        "stores_credentials": False,
        "preflight_read_tools": [
            "get_accounts",
            "get_portfolio",
            "get_equity_quotes",
            "get_equity_tradability",
            "get_equity_positions",
            "get_equity_orders",
            "get_option_positions",
            "get_option_orders",
        ],
        "review_tool": "review_equity_order",
        "place_tool_after_explicit_confirmation": "place_equity_order",
        "review_arguments_template": review_args,
        "place_arguments_after_confirmation": place_args,
        "confirmation_fields": [
            "account_number",
            "symbol",
            "side",
            "quantity",
            "type",
            "limit_price",
            "time_in_force",
            "market_hours",
        ],
        "hard_rules": [
            "The user must choose or clearly identify an agentic_allowed account; never default an account.",
            "Call review_equity_order first and show the market_data_disclosure and every broker alert verbatim.",
            "Ask the user to confirm the exact reviewed account, symbol, side, quantity, type, and limit price.",
            "Do not call place_equity_order without that exact confirmation.",
            "Follow every data.next/cursor page to null for all equity and option position/order reads before preview and after confirmation; stop on a missing link or failed page.",
            "Fetch a fresh equity quote for every held share symbol and a fresh option quote for every held option_id before either total-open-risk calculation; stop on any missing or stale mark.",
            "Block a recent matching filled opening order until the corresponding position is visible; broker feed reconciliation uncertainty is not permission to submit again.",
            "After confirmation, re-read positions, open orders, portfolio, exact quote, and tradability; reapply quote age, bid/ask, spread, ask-at-or-below-limit, tick, and packet-expiry gates immediately before placement.",
            "Use a limit order only; never change the reviewed fields during placement.",
            "Do not schedule or automatically retry; query orders on uncertainty and reuse the same packet ref_id for any deliberate retry of this logical order.",
        ],
        "validation": _validation([], []),
    }


def build_robinhood_option_review_plan(trade_plan: dict[str, Any]) -> dict[str, Any]:
    """Build a manual review-first plan for one long call or put."""
    errors = _review_errors(trade_plan, "option")
    order = trade_plan.get("order") if isinstance(trade_plan, dict) else {}
    for field in ("symbol", "option_type", "expiry", "strike", "underlying_type"):
        if order.get(field) in (None, ""):
            errors.append(_issue(f"missing_{field}", field, f"{field} is required for exact contract lookup."))
    if order.get("intent") != "buy_to_open":
        errors.append(_issue("unsupported_option_intent", "intent", "Only long buy-to-open options are supported."))
    if order.get("side") != "buy" or order.get("position_effect") != "open":
        errors.append(
            _issue(
                "invalid_option_leg",
                "side",
                "The option leg must be an explicit buy-to-open.",
            )
        )
    if order.get("option_type") not in {"call", "put"}:
        errors.append(_issue("invalid_option_type", "option_type", "Option type must be call or put."))
    if re.search(r"\d", str(order.get("symbol") or "")):
        errors.append(
            _issue(
                "numeric_adjusted_option_root",
                "symbol",
                "Numeric option roots are conservatively treated as adjusted/close-only and cannot open a new position.",
            )
        )
    try:
        expiry_value = date.fromisoformat(str(order.get("expiry") or ""))
    except ValueError:
        expiry_value = None
        errors.append(_issue("invalid_expiry", "expiry", "Option expiry must be YYYY-MM-DD."))
    if expiry_value is not None and expiry_value < date.today():
        errors.append(_issue("expired_contract", "expiry", "Expired options cannot be reviewed."))
    strike_value = _number(order.get("strike"))
    if strike_value is None or strike_value <= 0:
        errors.append(_issue("invalid_strike", "strike", "A positive finite strike is required."))
    elif SYMBOL_PATTERN.fullmatch(str(order.get("symbol") or "")) and order.get("option_type") in {"call", "put"}:
        expected_label = (
            f"{order['symbol']} {order.get('expiry')} "
            f"{str(order['option_type']).upper()} {strike_value:g}"
        )
        if order.get("contract_label") != expected_label:
            errors.append(
                _issue(
                    "contract_label_mismatch",
                    "contract_label",
                    "Contract label does not reconcile to symbol, expiry, type, and strike.",
                )
            )
    if order.get("underlying_type") not in {"equity", "index"}:
        errors.append(_issue("invalid_underlying_type", "underlying_type", "Underlying type must be equity or index."))
    elif order.get("underlying_type") != "equity":
        errors.append(
            _issue(
                "unsupported_index_option_review",
                "underlying_type",
                "Robinhood review is limited to equity/ETF options; index settlement and chain identity require an official settlement-aware workflow.",
            )
        )
    multiplier = order.get("contract_multiplier")
    if multiplier != 100:
        errors.append(
            _issue(
                "unsupported_contract_multiplier",
                "contract_multiplier",
                "Manual Robinhood review requires 100x, but multiplier alone does not prove a standard deliverable; live instrument and preview checks must also pass.",
            )
        )
    risk = trade_plan.get("risk") if isinstance(trade_plan, dict) and isinstance(trade_plan.get("risk"), dict) else {}
    full_debit = _number(risk.get("full_option_debit_at_risk_dollars"))
    risk_budget = _number(risk.get("risk_budget_dollars"))
    quantity = order.get("quantity")
    limit_price = _number(order.get("limit_price"))
    expected_debit = (
        round(quantity * limit_price * multiplier, 2)
        if isinstance(quantity, int)
        and not isinstance(quantity, bool)
        and quantity > 0
        and limit_price is not None
        and isinstance(multiplier, int)
        and not isinstance(multiplier, bool)
        and multiplier > 0
        else None
    )
    if expected_debit is not None and (
        not _same_money(order.get("estimated_debit_dollars"), expected_debit)
        or not _same_money(full_debit, expected_debit)
        or not _same_money(risk.get("planned_max_loss_dollars"), expected_debit)
    ):
        errors.append(
            _issue(
                "option_debit_mismatch",
                "full_option_debit_at_risk_dollars",
                "Option debit does not reconcile to quantity, limit price, and multiplier.",
            )
        )
    allocation_cap = _number(risk.get("allocation_cap_dollars"))
    if expected_debit is not None and (allocation_cap is None or expected_debit > allocation_cap + 0.01):
        errors.append(
            _issue(
                "option_debit_exceeds_allocation_cap",
                "allocation_cap_dollars",
                "Full option debit must fit inside the allocation cap.",
            )
        )
    if (
        full_debit is None
        or risk_budget is None
        or full_debit > risk_budget + 0.01
    ):
        errors.append(
            _issue(
                "full_debit_exceeds_risk_budget",
                "full_option_debit_at_risk_dollars",
                "Full option debit must fit inside the risk budget before broker review.",
            )
        )
    if errors:
        return _blocked_review_plan(
            ROBINHOOD_OPTION_REVIEW_SCHEMA,
            "option",
            "review_option_order",
            "place_option_order",
            errors,
        )

    leg = {
        "option_id": OPTION_ID_PLACEHOLDER,
        "side": "buy",
        "position_effect": "open",
        "ratio_quantity": 1,
    }
    review_args = {
        "account_number": ACCOUNT_NUMBER_PLACEHOLDER,
        "chain_symbol": order["symbol"],
        "underlying_type": order["underlying_type"],
        "legs": [leg],
        "quantity": str(order["quantity"]),
        "type": "limit",
        "price": f"{float(order['limit_price']):.2f}",
        "time_in_force": "gfd",
        "market_hours": "regular_hours",
    }
    place_args = {
        "account_number": ACCOUNT_NUMBER_PLACEHOLDER,
        "legs": [dict(leg)],
        "quantity": str(order["quantity"]),
        "type": "limit",
        "price": f"{float(order['limit_price']):.2f}",
        "time_in_force": "gfd",
        "market_hours": "regular_hours",
        "ref_id": REF_ID_PLACEHOLDER,
    }
    return {
        "schema": ROBINHOOD_OPTION_REVIEW_SCHEMA,
        "broker": "robinhood",
        "asset": "option",
        "capability": "single_leg_long_option_limit_order",
        "intent": "buy_to_open",
        "status": "review_required_before_any_place_order",
        "review_allowed": True,
        "requires_agentic_allowed_account": True,
        "requires_option_level_2_or_higher": True,
        "requires_explicit_user_confirmation_before_place": True,
        "script_submits_live_orders": False,
        "automation_allowed": False,
        "repeat_orders_allowed": False,
        "stores_credentials": False,
        "preflight_read_tools": [
            "get_accounts",
            "get_portfolio",
            "get_option_chains",
            "get_option_instruments",
            "get_option_quotes",
            "get_option_positions",
            "get_option_orders",
            "get_equity_positions",
            "get_equity_orders",
        ],
        "contract_lookup": {
            "chain_symbol": order["symbol"],
            "expected_underlying_type": order["underlying_type"],
            "expiration_date": order["expiry"],
            "expiration_dates": order["expiry"],
            "strike_price": str(order["strike"]),
            "type": order["option_type"],
            "expected_contract_label": order.get("contract_label"),
            "option_id": OPTION_ID_PLACEHOLDER,
            "note": "Resolve option_id with get_option_instruments; never infer it from local text.",
            "chain_query_arguments": {
                "underlying_symbol": order["symbol"],
            },
            "instrument_query_arguments": {
                "chain_symbol": order["symbol"],
                "expiration_dates": order["expiry"],
                "strike_price": str(order["strike"]),
                "type": order["option_type"],
                "state": "active",
                "tradability": "tradable",
            },
        },
        "review_tool": "review_option_order",
        "place_tool_after_explicit_confirmation": "place_option_order",
        "review_arguments_template": review_args,
        "place_arguments_after_confirmation": place_args,
        "confirmation_fields": [
            "account_number",
            "option_id",
            "chain_symbol",
            "option_type",
            "expiry",
            "strike",
            "side",
            "position_effect",
            "quantity",
            "type",
            "price",
            "time_in_force",
            "market_hours",
        ],
        "hard_rules": [
            "The user must choose an agentic_allowed, options-approved account; never default an account.",
            "Enumerate every option chain containing the exact expiry, query each chain_id through all get_option_instruments pages, and require exactly one total exact active/tradable instrument.",
            "Require the live get_option_instruments underlying_type to exactly match the packet; stop on any mismatch.",
            "Require an active buy-to-open-tradable instrument whose exact chain_symbol matches the plan and a chain with can_open_position true; reject numeric adjusted roots and any adjusted or nonstandard deliverable.",
            "Verify the chain trade_value_multiplier is 100, while treating multiplier alone as insufficient proof of a standard contract.",
            "Call review_option_order first and show quotes, fees, collateral, and every broker alert verbatim.",
            "Ask the user to confirm the exact reviewed account, contract, side, quantity, type, and price.",
            "Do not call place_option_order without that exact confirmation.",
            "Follow every data.next/cursor page to null for option chains, instruments, and all equity/option position/order reads before preview and after confirmation; stop on a missing link or failed page.",
            "Fetch a fresh equity quote for every held share symbol and a fresh option quote for every held option_id before either total-open-risk calculation; stop on any missing or stale mark.",
            "Block a recent matching filled buy-to-open order until the corresponding option position is visible; broker feed reconciliation uncertainty is not permission to submit again.",
            "After confirmation, re-read positions, open orders, portfolio, exact instrument, quote, and complete chain proof; reapply quote age, bid/ask, spread, ask-at-or-below-limit, tick, tradability, standard-deliverable, and packet-expiry gates immediately before placement.",
            "Use a single-leg buy-to-open limit order only; never change reviewed fields during placement.",
            "Do not schedule or automatically retry; query orders on uncertainty and reuse the same packet ref_id for any deliberate retry of this logical order.",
        ],
        "validation": _validation([], []),
    }


def _parse_aware_utc_timestamp(value: Any) -> datetime | None:
    """Parse one timezone-aware ISO timestamp without guessing a timezone."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _account_drawdown_review_errors(
    drawdown: Any,
    *,
    risk_fraction: float | None,
    portfolio_snapshot_digest: Any,
    portfolio_rows: list[Any],
    match_count: Any,
    issued_at: datetime | None,
) -> list[dict[str, str]]:
    """Verify the chained account-equity interlock carried into a packet."""
    errors: list[dict[str, str]] = []
    root = "review_constraints.drawdown"
    if not isinstance(drawdown, dict) or not drawdown:
        return [
            _issue(
                "missing_account_drawdown_constraints",
                root,
                "A chained same-account equity drawdown attestation is required.",
            )
        ]

    if drawdown.get("schema") != ACCOUNT_DRAWDOWN_REVIEW_SCHEMA:
        errors.append(
            _issue(
                "invalid_account_drawdown_review_schema",
                f"{root}.schema",
                "The account drawdown review schema is missing or unsupported.",
            )
        )
    if drawdown.get("policy_version") != ACCOUNT_DRAWDOWN_POLICY_VERSION:
        errors.append(
            _issue(
                "invalid_account_drawdown_policy",
                f"{root}.policy_version",
                "The account drawdown policy version is missing or unsupported.",
            )
        )
    if drawdown.get("status") != "allowed" or drawdown.get("allowed") is not True:
        errors.append(
            _issue(
                "account_drawdown_review_not_allowed",
                f"{root}.allowed",
                "Every eligible account must pass the account-equity drawdown interlock.",
            )
        )
    if drawdown.get("missing_or_unsafe_state_policy") != "block_new_entries":
        errors.append(
            _issue(
                "unsafe_missing_drawdown_state_policy",
                f"{root}.missing_or_unsafe_state_policy",
                "Missing, stale, or unsafe equity history must block new entries.",
            )
        )

    snapshot_digest = drawdown.get("broker_snapshot_digest_sha256")
    if not _is_sha256(snapshot_digest):
        errors.append(
            _issue(
                "invalid_drawdown_snapshot_digest",
                f"{root}.broker_snapshot_digest_sha256",
                "A full snapshot digest must bind drawdown and portfolio review to the same capture.",
            )
        )
    elif snapshot_digest != portfolio_snapshot_digest:
        errors.append(
            _issue(
                "drawdown_portfolio_snapshot_mismatch",
                f"{root}.broker_snapshot_digest_sha256",
                "Drawdown and portfolio review must use the exact same broker snapshot.",
            )
        )
    source_digest = drawdown.get("source_snapshot_digest_sha256")
    if not _is_sha256(source_digest):
        errors.append(
            _issue(
                "invalid_drawdown_source_snapshot_digest",
                f"{root}.source_snapshot_digest_sha256",
                "The equity ledger must bind to one normalized source snapshot digest.",
            )
        )

    base_risk = _number(drawdown.get("base_risk_fraction"))
    requested_risk = _number(drawdown.get("requested_risk_fraction"))
    if base_risk is None or abs(base_risk - MANUAL_REVIEW_BASE_RISK_FRACTION) > 1e-12:
        errors.append(
            _issue(
                "unsafe_drawdown_base_risk_fraction",
                f"{root}.base_risk_fraction",
                f"Manual Robinhood review must use the {MANUAL_REVIEW_BASE_RISK_FRACTION:.2%} base-risk ceiling.",
            )
        )
    if risk_fraction is not None and (
        requested_risk is None or abs(requested_risk - risk_fraction) > 1e-12
    ):
        errors.append(
            _issue(
                "drawdown_requested_risk_mismatch",
                f"{root}.requested_risk_fraction",
                "The drawdown interlock risk must match the planner account context.",
            )
        )

    rows = drawdown.get("eligible_accounts")
    if not isinstance(rows, list):
        rows = []
        errors.append(
            _issue(
                "missing_drawdown_account_attestations",
                f"{root}.eligible_accounts",
                "At least one same-account drawdown attestation is required.",
            )
        )
    count = drawdown.get("eligible_account_count")
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or count < 1
        or count != len(rows)
    ):
        errors.append(
            _issue(
                "drawdown_account_count_mismatch",
                f"{root}.eligible_account_count",
                "Drawdown attestation count must match a non-empty account list.",
            )
        )
    if isinstance(match_count, int) and not isinstance(match_count, bool) and count != match_count:
        errors.append(
            _issue(
                "drawdown_review_account_match_count_mismatch",
                f"{root}.eligible_account_count",
                "Drawdown attestations must match every eligible review account.",
            )
        )

    portfolio_by_account = {
        str(row.get("account_key") or "").strip(): row
        for row in portfolio_rows
        if isinstance(row, dict) and str(row.get("account_key") or "").strip()
    }
    seen: set[str] = set()
    for index, row in enumerate(rows):
        field = f"{root}.eligible_accounts[{index}]"
        if not isinstance(row, dict):
            errors.append(
                _issue(
                    "invalid_drawdown_account_attestation",
                    field,
                    "Each drawdown attestation must be an object.",
                )
            )
            continue
        if row.get("schema") != ACCOUNT_DRAWDOWN_INTERLOCK_SCHEMA:
            errors.append(
                _issue(
                    "invalid_account_drawdown_interlock_schema",
                    f"{field}.schema",
                    "The account drawdown interlock schema is unsupported.",
                )
            )
        if row.get("policy_version") != ACCOUNT_DRAWDOWN_POLICY_VERSION:
            errors.append(
                _issue(
                    "invalid_account_drawdown_interlock_policy",
                    f"{field}.policy_version",
                    "The account drawdown interlock policy does not match the review policy.",
                )
            )
        if (
            row.get("allowed") is not True
            or row.get("review_ready") is not True
            or row.get("status") not in {"ready", "reduced"}
            or row.get("blockers") != []
        ):
            errors.append(
                _issue(
                    "account_drawdown_interlock_blocked",
                    field,
                    "Every included account must have an explicit blocker-free drawdown result.",
                )
            )

        account_key = _prompt_text(row.get("account_key"), limit=96)
        if (
            not isinstance(row.get("account_key"), str)
            or not account_key
            or account_key != row.get("account_key", "").strip()
            or re.fullmatch(r"acct_[0-9a-f]{16}", account_key) is None
            or account_key in seen
        ):
            errors.append(
                _issue(
                    "invalid_or_duplicate_drawdown_account_key",
                    f"{field}.account_key",
                    "Each drawdown attestation needs one unique pseudonymous account key.",
                )
            )
        else:
            seen.add(account_key)

        account_mask = _prompt_text(row.get("account_mask"), limit=16)
        if (
            not isinstance(row.get("account_mask"), str)
            or account_mask != row.get("account_mask", "").strip()
            or re.fullmatch(r"\.\.\.[A-Za-z0-9]{4}", account_mask) is None
        ):
            errors.append(
                _issue(
                    "invalid_drawdown_account_mask",
                    f"{field}.account_mask",
                    "Each drawdown attestation needs the normalized masked broker account suffix.",
                )
            )

        observations = row.get("observation_count")
        if (
            not isinstance(observations, int)
            or isinstance(observations, bool)
            or observations < 2
        ):
            errors.append(
                _issue(
                    "insufficient_account_equity_history",
                    f"{field}.observation_count",
                    "At least two chained explicit broker observations are required.",
                )
            )
        baseline_started = _parse_aware_utc_timestamp(row.get("baseline_started_at"))
        baseline_span_hours = _number(row.get("baseline_span_hours"))
        baseline_ny_dates = row.get("baseline_ny_calendar_date_count")
        if baseline_started is None:
            errors.append(
                _issue(
                    "missing_drawdown_baseline_start",
                    f"{field}.baseline_started_at",
                    "A timezone-aware first observation must bind the account baseline.",
                )
            )
        if baseline_span_hours is None or baseline_span_hours < 18.0 - 1e-9:
            errors.append(
                _issue(
                    "insufficient_drawdown_baseline_span",
                    f"{field}.baseline_span_hours",
                    "The durable account baseline must span at least 18 hours.",
                )
            )
        if (
            not isinstance(baseline_ny_dates, int)
            or isinstance(baseline_ny_dates, bool)
            or baseline_ny_dates < 2
        ):
            errors.append(
                _issue(
                    "insufficient_drawdown_baseline_ny_dates",
                    f"{field}.baseline_ny_calendar_date_count",
                    "The durable account baseline must cover at least two New York calendar dates.",
                )
            )
        if not _is_sha256(row.get("ledger_digest_sha256")):
            errors.append(
                _issue(
                    "invalid_account_equity_ledger_digest",
                    f"{field}.ledger_digest_sha256",
                    "A full validated equity-ledger digest is required.",
                )
            )
        if row.get("source_snapshot_digest_sha256") != source_digest:
            errors.append(
                _issue(
                    "drawdown_account_source_snapshot_mismatch",
                    f"{field}.source_snapshot_digest_sha256",
                    "Every account interlock must match the same normalized source snapshot.",
                )
            )

        asof = _parse_aware_utc_timestamp(row.get("asof"))
        if asof is None:
            errors.append(
                _issue(
                    "invalid_drawdown_observation_time",
                    f"{field}.asof",
                    "A timezone-aware latest equity observation is required.",
                )
            )
        elif issued_at is not None:
            age_seconds = (issued_at - asof).total_seconds()
            if age_seconds < -MAX_MANUAL_REVIEW_CLOCK_SKEW_SECONDS or age_seconds > 90 * 60:
                errors.append(
                    _issue(
                        "stale_or_future_drawdown_observation",
                        f"{field}.asof",
                        "The latest equity observation must be current within 90 minutes.",
                    )
                )
        if asof is not None and baseline_started is not None:
            expected_span_hours = (asof - baseline_started).total_seconds() / 3600.0
            if (
                expected_span_hours < 0
                or baseline_span_hours is None
                or abs(baseline_span_hours - expected_span_hours) > 0.001
            ):
                errors.append(
                    _issue(
                        "drawdown_baseline_span_mismatch",
                        f"{field}.baseline_span_hours",
                        "Baseline span must exactly reconcile the first and latest chained observations.",
                    )
                )

        current = _number(row.get("current_equity_dollars"))
        high_water = _number(row.get("high_water_equity_dollars"))
        high_water_drawdown = _number(row.get("high_water_drawdown_fraction"))
        portfolio_row = portfolio_by_account.get(account_key)
        if portfolio_row is None:
            errors.append(
                _issue(
                    "drawdown_account_not_in_portfolio_attestation",
                    f"{field}.account_key",
                    "Drawdown and portfolio controls must attest the same account set.",
                )
            )
        elif account_mask != portfolio_row.get("account_mask"):
            errors.append(
                _issue(
                    "drawdown_portfolio_account_mask_mismatch",
                    f"{field}.account_mask",
                    "Drawdown and portfolio controls must identify the same masked broker account.",
                )
            )
        elif current is None or not _same_money(current, portfolio_row.get("live_equity_dollars")):
            errors.append(
                _issue(
                    "drawdown_live_equity_mismatch",
                    f"{field}.current_equity_dollars",
                    "Drawdown equity must match the same-account portfolio equity.",
                )
            )
        if (
            current is None
            or current <= 0
            or high_water is None
            or high_water <= 0
            or high_water + 0.011 < current
            or high_water_drawdown is None
        ):
            errors.append(
                _issue(
                    "invalid_drawdown_equity_state",
                    field,
                    "Current equity, high-water equity, and drawdown must be positive and internally consistent.",
                )
            )
        else:
            expected_drawdown = current / high_water - 1.0
            if (
                high_water_drawdown > 1e-9
                or high_water_drawdown < -1.0
                or abs(high_water_drawdown - expected_drawdown) > 1e-6
            ):
                errors.append(
                    _issue(
                        "account_drawdown_arithmetic_mismatch",
                        f"{field}.high_water_drawdown_fraction",
                        "High-water drawdown arithmetic does not reconcile.",
                    )
                )

        session_reference = _number(row.get("ny_session_reference_equity_dollars"))
        session_loss = _number(row.get("ny_session_loss_fraction"))
        if session_reference is None or session_reference <= 0 or session_loss is None:
            errors.append(
                _issue(
                    "missing_session_drawdown_state",
                    field,
                    "A current New York-session equity reference and loss calculation are required.",
                )
            )
        elif current is not None and abs(session_loss - (current / session_reference - 1.0)) > 1e-6:
            errors.append(
                _issue(
                    "session_drawdown_arithmetic_mismatch",
                    f"{field}.ny_session_loss_fraction",
                    "New York-session loss arithmetic does not reconcile.",
                )
            )

        risk_multiplier = _number(row.get("risk_multiplier"))
        max_risk = _number(row.get("max_allowed_risk_fraction"))
        expected_multiplier = None
        if high_water_drawdown is not None:
            loss = max(0.0, -high_water_drawdown)
            expected_multiplier = 0.0 if loss >= 0.10 - 1e-12 else 0.25 if loss >= 0.08 - 1e-12 else 0.5 if loss >= 0.05 - 1e-12 else 1.0
        if session_loss is not None and session_loss <= -0.03 + 1e-12:
            expected_multiplier = 0.0
        if (
            risk_multiplier is None
            or risk_multiplier <= 0
            or risk_multiplier > 1
            or expected_multiplier is None
            or abs(risk_multiplier - expected_multiplier) > 1e-12
        ):
            errors.append(
                _issue(
                    "unsafe_account_drawdown_risk_multiplier",
                    f"{field}.risk_multiplier",
                    "The drawdown result must apply the exact capital-preservation risk multiplier.",
                )
            )
        expected_max_risk = (
            MANUAL_REVIEW_BASE_RISK_FRACTION * risk_multiplier
            if risk_multiplier is not None
            else None
        )
        if (
            max_risk is None
            or expected_max_risk is None
            or abs(max_risk - expected_max_risk) > 1e-12
        ):
            errors.append(
                _issue(
                    "drawdown_risk_ceiling_mismatch",
                    f"{field}.max_allowed_risk_fraction",
                    "The account risk ceiling must equal 1% times the drawdown multiplier.",
                )
            )
        if risk_fraction is not None and (max_risk is None or risk_fraction > max_risk + 1e-12):
            errors.append(
                _issue(
                    "planner_risk_exceeds_drawdown_ceiling",
                    f"{field}.max_allowed_risk_fraction",
                    "The planned risk fraction exceeds the account drawdown ceiling.",
                )
            )

        policy = row.get("policy") if isinstance(row.get("policy"), dict) else {}
        expected_policy = {
            "minimum_baseline_observations": 2.0,
            "minimum_baseline_ny_calendar_dates": 2.0,
            "minimum_baseline_span_hours": 18.0,
            "half_risk_at_drawdown_fraction": 0.05,
            "quarter_risk_at_drawdown_fraction": 0.08,
            "block_at_drawdown_fraction": 0.10,
            "block_at_ny_session_loss_fraction": -0.03,
            "block_at_unexplained_adjacent_jump_fraction": 0.25,
        }
        for policy_field, expected in expected_policy.items():
            actual = _number(policy.get(policy_field))
            if actual is None or abs(actual - expected) > 1e-12:
                errors.append(
                    _issue(
                        "unsafe_account_drawdown_policy_threshold",
                        f"{field}.policy.{policy_field}",
                        "Account drawdown thresholds may not be weakened in a review packet.",
                    )
                )
        max_age = _number(policy.get("max_observation_age_minutes"))
        if max_age is None or max_age <= 0 or max_age > 90:
            errors.append(
                _issue(
                    "unsafe_account_drawdown_max_age",
                    f"{field}.policy.max_observation_age_minutes",
                    "Account equity observations must expire within 90 minutes.",
                )
            )
        if (
            policy.get("missing_or_unsafe_state_policy") != "block_new_entries"
            or policy.get("risk_multiplier_may_increase_risk") is not False
        ):
            errors.append(
                _issue(
                    "unsafe_account_drawdown_fail_closed_policy",
                    f"{field}.policy",
                    "The drawdown interlock must fail closed and may only reduce risk.",
                )
            )

    if seen != set(portfolio_by_account):
        errors.append(
            _issue(
                "drawdown_portfolio_account_set_mismatch",
                f"{root}.eligible_accounts",
                "Drawdown and portfolio attestations must cover the same account keys.",
            )
        )
    return errors


def _share_candidate_review_errors(
    candidate: Any,
    trade_plan: dict[str, Any],
    *,
    issued_at: datetime | None,
) -> list[dict[str, str]]:
    """Revalidate the immutable top_shares candidate carried into review."""
    errors: list[dict[str, str]] = []
    root = "review_constraints.candidate"
    if not isinstance(candidate, dict) or not candidate:
        return [
            _issue(
                "missing_share_candidate_attestation",
                root,
                "Share review requires one exact fresh top_shares candidate attestation.",
            )
        ]
    order = trade_plan.get("order") if isinstance(trade_plan.get("order"), dict) else {}
    request = (
        trade_plan.get("candidate_request")
        if isinstance(trade_plan.get("candidate_request"), dict)
        else {}
    )
    if candidate.get("schema") != SHARE_CANDIDATE_REVIEW_SCHEMA:
        errors.append(_issue("invalid_share_candidate_schema", f"{root}.schema", "The share candidate attestation schema is unsupported."))
    if candidate.get("status") != "allowed" or candidate.get("allowed") is not True:
        errors.append(_issue("share_candidate_not_allowed", f"{root}.allowed", "The exact share candidate must be explicitly allowed."))
    if candidate.get("blockers") != []:
        errors.append(_issue("share_candidate_has_blockers", f"{root}.blockers", "An allowed share candidate must contain an explicit empty blocker list."))
    symbol = str(order.get("symbol") or "").strip().upper()
    if (
        candidate.get("asset") != "share"
        or candidate.get("direction") != "long"
        or candidate.get("symbol") != symbol
        or trade_plan.get("direction") != "long"
    ):
        errors.append(_issue("share_candidate_identity_mismatch", root, "Candidate asset, symbol, and long direction must exactly match the plan."))
    if candidate.get("source_pattern") != "top_shares_*.parquet":
        errors.append(_issue("invalid_share_candidate_source_pattern", f"{root}.source_pattern", "Share candidates must come from the latest top_shares artifact family."))
    source_file = candidate.get("source_file")
    if not isinstance(source_file, str) or re.fullmatch(r"top_shares_[^/\\]+\.parquet", source_file) is None:
        errors.append(_issue("invalid_share_candidate_source_file", f"{root}.source_file", "A safe top_shares artifact filename is required."))
    if not _is_sha256(candidate.get("source_artifact_digest_sha256")):
        errors.append(_issue("invalid_share_candidate_artifact_digest", f"{root}.source_artifact_digest_sha256", "A full artifact digest must bind the share candidate."))
    if not _is_sha256(candidate.get("candidate_row_digest_sha256")):
        errors.append(_issue("invalid_share_candidate_row_digest", f"{root}.candidate_row_digest_sha256", "A full row digest must bind the selected share candidate."))
    fingerprint = candidate.get("candidate_fingerprint")
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{24}", fingerprint) is None:
        errors.append(_issue("invalid_share_candidate_fingerprint", f"{root}.candidate_fingerprint", "A 24-character candidate fingerprint is required."))
    if (
        request.get("candidate_fingerprint") != fingerprint
        or request.get("source_file") != source_file
        or (
            request.get("source_generated_at") not in (None, "")
            and request.get("source_generated_at") != candidate.get("candidate_source_generated_at")
        )
    ):
        errors.append(_issue("share_candidate_request_mismatch", "candidate_request", "The loaded candidate request must match the frozen candidate provenance."))
    price_session_text = candidate.get("candidate_source_price_session")
    try:
        price_session = (
            date.fromisoformat(price_session_text)
            if isinstance(price_session_text, str)
            else None
        )
    except ValueError:
        price_session = None
    issue_date = issued_at.date() if issued_at is not None else None
    if (
        price_session is None
        or price_session_text != price_session.isoformat()
        or (
            issue_date is not None
            and ((issue_date - price_session).days < 0 or (issue_date - price_session).days > 4)
        )
    ):
        errors.append(_issue("stale_or_invalid_share_price_session", f"{root}.candidate_source_price_session", "The last completed price-bar session must be nonfuture and no more than four calendar days old."))
    if candidate.get("candidate_source_price_basis") != "history_last_bar_close":
        errors.append(_issue("invalid_share_price_basis", f"{root}.candidate_source_price_basis", "Share candidate geometry must use history_last_bar_close."))

    max_age = _number(candidate.get("max_source_age_minutes"))
    artifact_at = _parse_aware_utc_timestamp(candidate.get("source_artifact_at"))
    reported_artifact_age = _number(candidate.get("source_artifact_age_minutes"))
    if max_age is None or max_age <= 0 or max_age > 45:
        errors.append(_issue("unsafe_share_candidate_max_age", f"{root}.max_source_age_minutes", "Share candidate source age must be capped at 45 minutes."))
    if artifact_at is None:
        errors.append(_issue("missing_share_candidate_artifact_time", f"{root}.source_artifact_at", "A timezone-aware artifact timestamp is required."))
    elif issued_at is not None and max_age is not None:
        actual_age = (issued_at - artifact_at).total_seconds() / 60.0
        if actual_age < -1 or actual_age > max_age + 1e-9:
            errors.append(_issue("stale_or_future_share_candidate_artifact", f"{root}.source_artifact_at", "The selected top_shares artifact must be current at packet issue."))
        if reported_artifact_age is None or abs(reported_artifact_age - max(0.0, actual_age)) > 1.0:
            errors.append(_issue("share_candidate_artifact_age_mismatch", f"{root}.source_artifact_age_minutes", "Reported artifact age must reconcile to its timestamp."))

    quote_fields_supplied = any(
        candidate.get(field) not in (None, "")
        for field in (
            "candidate_source_quote_at", "candidate_source_bid", "candidate_source_ask",
            "candidate_source_spread_fraction",
        )
    )
    quote_available = candidate.get("candidate_quote_available")
    if quote_available not in {True, False}:
        errors.append(_issue("missing_share_candidate_quote_availability", f"{root}.candidate_quote_available", "Candidate quote availability must be explicit."))
    elif quote_available is not quote_fields_supplied:
        errors.append(_issue("share_candidate_quote_availability_mismatch", f"{root}.candidate_quote_available", "Candidate quote availability must match the frozen quote fields."))
    quote_at = _parse_aware_utc_timestamp(candidate.get("candidate_source_quote_at"))
    quote_basis = str(candidate.get("candidate_source_quote_time_basis") or "").strip().lower()
    bid = _number(candidate.get("candidate_source_bid"))
    ask = _number(candidate.get("candidate_source_ask"))
    spread = _number(candidate.get("candidate_source_spread_fraction"))
    expected_spread = (
        (ask - bid) / ((ask + bid) / 2.0)
        if bid is not None and ask is not None and bid > 0 and ask >= bid
        else None
    )
    if quote_fields_supplied:
        if quote_at is None:
            errors.append(_issue("missing_share_candidate_quote_time", f"{root}.candidate_source_quote_at", "A supplied source quote needs a timezone-aware timestamp."))
        elif issued_at is not None and max_age is not None:
            quote_age = (issued_at - quote_at).total_seconds() / 60.0
            if quote_age < -1 or quote_age > max_age + 1e-9:
                errors.append(_issue("stale_or_future_share_candidate_quote", f"{root}.candidate_source_quote_at", "A supplied candidate source quote must be current at packet issue."))
        if not quote_basis or (
            quote_basis != "provider_response_received_at"
            and not (
                any(token in quote_basis for token in ("provider", "broker", "exchange"))
                and "quote" in quote_basis
            )
        ):
            errors.append(_issue("invalid_share_candidate_quote_basis", f"{root}.candidate_source_quote_time_basis", "A supplied quote time needs explicit provider, broker, or exchange provenance."))
        if not str(candidate.get("candidate_quote_quality") or "").strip():
            errors.append(_issue("missing_share_candidate_quote_quality", f"{root}.candidate_quote_quality", "Supplied candidate quote quality must be explicit."))
        if expected_spread is None or spread is None or abs(spread - expected_spread) > 1e-6 or spread > 0.01 + 1e-12:
            errors.append(_issue("unsafe_share_candidate_source_spread", f"{root}.candidate_source_spread_fraction", "Supplied candidate bid/ask must be positive, ordered, arithmetically consistent, and no wider than 1%."))

    if candidate.get("setup_gate_status") != "ready":
        errors.append(_issue("share_candidate_setup_not_ready", f"{root}.setup_gate_status", "The exact share candidate must clear its setup gate."))
    if str(candidate.get("trade_status") or "").strip().lower() in {"", "watch", "skip", "blocked"}:
        errors.append(_issue("share_candidate_not_actionable", f"{root}.trade_status", "The exact share candidate must be actionable."))
    if str(candidate.get("research_guard_status") or "").strip().lower() not in {
        "pass", "passed", "ok", "ready", "allowed", "validated",
    }:
        errors.append(_issue("share_candidate_research_guard_not_passed", f"{root}.research_guard_status", "The exact share candidate must pass the research guard."))
    for label, order_field, candidate_field in (
        ("entry", "limit_price", "entry_price"),
        ("stop", "stop_price", "stop_price"),
        ("target", "target_price", "target_price"),
    ):
        if not _same_money(order.get(order_field), candidate.get(candidate_field)):
            errors.append(_issue(f"share_candidate_{label}_mismatch", f"{root}.{candidate_field}", f"Share {label} must exactly match the frozen candidate geometry."))
    quantity = order.get("quantity")
    max_units = candidate.get("max_units")
    if (
        not isinstance(quantity, int)
        or isinstance(quantity, bool)
        or not isinstance(max_units, int)
        or isinstance(max_units, bool)
        or quantity <= 0
        or max_units <= 0
        or quantity > max_units
        or candidate.get("planned_quantity") != quantity
    ):
        errors.append(_issue("share_candidate_quantity_cap_mismatch", f"{root}.max_units", "Planned share quantity must be positive and no greater than the candidate cap."))
    planned_notional = _number(order.get("estimated_notional_dollars"))
    attested_notional = _number(candidate.get("planned_notional_dollars"))
    max_notional = _number(candidate.get("max_notional_dollars"))
    if (
        planned_notional is None
        or attested_notional is None
        or max_notional is None
        or not _same_money(planned_notional, attested_notional)
        or planned_notional > max_notional + 0.011
    ):
        errors.append(_issue("share_candidate_notional_cap_mismatch", f"{root}.max_notional_dollars", "Planned share notional must match the attestation and fit its capital cap."))
    if candidate.get("top_rank_limit") != 3:
        errors.append(_issue("unsafe_share_candidate_rank_limit", f"{root}.top_rank_limit", "Only the latest three actionable share candidates may enter review."))
    for flag in ("require_exact_geometry", "require_loaded_candidate_fingerprint"):
        if candidate.get(flag) is not True:
            errors.append(_issue(f"missing_share_candidate_{flag}", f"{root}.{flag}", f"Candidate control {flag} must be explicitly enabled."))
    return errors


def _option_candidate_review_errors(
    candidate: Any,
    trade_plan: dict[str, Any],
    quote: dict[str, Any],
    *,
    issued_at: datetime | None,
) -> list[dict[str, str]]:
    """Revalidate the exact option row frozen from both cycle and queue."""
    errors: list[dict[str, str]] = []
    root = "review_constraints.candidate"
    if not isinstance(candidate, dict) or not candidate:
        return [
            _issue(
                "missing_option_candidate_attestation",
                root,
                "Option review requires one exact fresh candidate attested by both the cycle and queue.",
            )
        ]
    order = trade_plan.get("order") if isinstance(trade_plan.get("order"), dict) else {}
    request = (
        trade_plan.get("candidate_request")
        if isinstance(trade_plan.get("candidate_request"), dict)
        else {}
    )

    if candidate.get("schema") != OPTION_CANDIDATE_REVIEW_SCHEMA:
        errors.append(_issue("invalid_option_candidate_schema", f"{root}.schema", "The option candidate attestation schema is unsupported."))
    if candidate.get("status") != "allowed" or candidate.get("allowed") is not True:
        errors.append(_issue("option_candidate_not_allowed", f"{root}.allowed", "The exact option candidate must be explicitly allowed."))
    if candidate.get("blockers") != []:
        errors.append(_issue("option_candidate_has_blockers", f"{root}.blockers", "An allowed option candidate must contain an explicit empty blocker list."))

    symbol = str(order.get("symbol") or "").strip().upper()
    option_type = str(order.get("option_type") or "").strip().lower()
    strike = _number(order.get("strike"))
    candidate_strike = _number(candidate.get("strike"))
    expiry = str(order.get("expiry") or "").strip()
    if (
        candidate.get("asset") != "option"
        or candidate.get("action") != "BUY_TO_OPEN"
        or candidate.get("order_type") != "limit"
        or candidate.get("time_in_force") != "day"
        or candidate.get("underlying_type") != "equity"
        or candidate.get("symbol") != symbol
        or candidate.get("option_type") != option_type
        or strike is None
        or candidate_strike is None
        or abs(strike - candidate_strike) > 1e-9
        or candidate.get("expiry") != expiry
        or trade_plan.get("direction") != f"long_{option_type}"
    ):
        errors.append(_issue("option_candidate_identity_mismatch", root, "Candidate action, contract identity, equity underlying, and long-entry intent must exactly match the plan."))

    fingerprint = candidate.get("candidate_fingerprint")
    row_digest = candidate.get("candidate_row_digest_sha256")
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{24}", fingerprint) is None:
        errors.append(_issue("invalid_option_candidate_fingerprint", f"{root}.candidate_fingerprint", "A 24-character candidate fingerprint is required."))
    if not _is_sha256(row_digest):
        errors.append(_issue("invalid_option_candidate_row_digest", f"{root}.candidate_row_digest_sha256", "A full digest must bind the exact candidate row."))
    elif fingerprint != row_digest[:24]:
        errors.append(_issue("option_candidate_fingerprint_digest_mismatch", f"{root}.candidate_fingerprint", "The candidate fingerprint must be the canonical row-digest prefix."))
    if not _is_sha256(candidate.get("cycle_digest_sha256")):
        errors.append(_issue("invalid_option_cycle_digest", f"{root}.cycle_digest_sha256", "A full digest must bind the source cycle."))
    if not _is_sha256(candidate.get("queue_digest_sha256")):
        errors.append(_issue("invalid_option_queue_digest", f"{root}.queue_digest_sha256", "A full digest must bind the source queue."))
    if request.get("candidate_fingerprint") not in (None, "", fingerprint):
        errors.append(_issue("option_candidate_request_mismatch", "candidate_request.candidate_fingerprint", "The loaded option-candidate fingerprint must match the frozen attestation."))

    if candidate.get("source_cycle_schema") != "optedge_robinhood_agentic_cycle_v1":
        errors.append(_issue("invalid_option_cycle_schema", f"{root}.source_cycle_schema", "Option review requires the current agentic-cycle schema."))
    if candidate.get("source_queue_schema") != "optedge_robinhood_agentic_options_queue_v1":
        errors.append(_issue("invalid_option_queue_schema", f"{root}.source_queue_schema", "Option review requires the current agentic options-queue schema."))
    max_age = _number(candidate.get("max_source_age_minutes"))
    if max_age is None or max_age <= 0 or max_age > 45:
        errors.append(_issue("unsafe_option_candidate_max_age", f"{root}.max_source_age_minutes", "Option candidate sources must expire within 45 minutes."))
    cycle_at = _parse_aware_utc_timestamp(candidate.get("cycle_generated_at"))
    queue_at = _parse_aware_utc_timestamp(candidate.get("queue_generated_at"))
    for label, timestamp in (("cycle", cycle_at), ("queue", queue_at)):
        if timestamp is None:
            errors.append(_issue(f"missing_option_{label}_timestamp", f"{root}.{label}_generated_at", f"A timezone-aware {label} timestamp is required."))
        elif issued_at is not None and max_age is not None:
            age = (issued_at - timestamp).total_seconds() / 60.0
            if age < -1 or age > max_age + 1e-9:
                errors.append(_issue(f"stale_or_future_option_{label}", f"{root}.{label}_generated_at", f"The option {label} must be current at packet issue."))
    dte = candidate.get("dte")
    try:
        expiry_date = date.fromisoformat(expiry)
    except ValueError:
        expiry_date = None
    if (
        not isinstance(dte, int)
        or isinstance(dte, bool)
        or dte < SWING_EXECUTION_OPTION_MIN_DTE
        or expiry_date is None
        or cycle_at is None
        or dte != (expiry_date - cycle_at.date()).days
    ):
        errors.append(_issue("option_candidate_dte_mismatch", f"{root}.dte", f"Candidate DTE must exactly reconcile the expiry and cycle date and be at least {SWING_EXECUTION_OPTION_MIN_DTE} days."))

    if candidate.get("exact_candidate_count_cycle") != 1:
        errors.append(_issue("option_cycle_membership_not_unique", f"{root}.exact_candidate_count_cycle", "The exact contract must occur once in the cycle candidate set."))
    if candidate.get("exact_candidate_count_queue") != 1:
        errors.append(_issue("option_queue_membership_not_unique", f"{root}.exact_candidate_count_queue", "The exact contract must occur once in the source queue."))
    if candidate.get("candidate_rows_match") is not True:
        errors.append(_issue("option_cycle_queue_candidate_mismatch", f"{root}.candidate_rows_match", "Cycle and queue must attest the same exact candidate row."))
    for field, expected in (
        ("entry_gate_new_entries_allowed_after_live_checks", True),
        ("cycle_auto_submit_allowed", False),
        ("cycle_does_not_place_orders", True),
        ("queue_does_not_place_orders", True),
        ("queue_execution_enabled", False),
    ):
        if candidate.get(field) is not expected:
            errors.append(_issue(f"unsafe_option_candidate_{field}", f"{root}.{field}", f"Option candidate control {field} must be exactly {expected}."))
    if candidate.get("queue_max_orders_to_submit") != 0:
        errors.append(_issue("unsafe_option_queue_submission_cap", f"{root}.queue_max_orders_to_submit", "The research queue must authorize zero broker submissions."))

    plan_quantity = order.get("quantity")
    quantity_cap = candidate.get("candidate_quantity_cap")
    if (
        not isinstance(plan_quantity, int)
        or isinstance(plan_quantity, bool)
        or not isinstance(quantity_cap, int)
        or isinstance(quantity_cap, bool)
        or plan_quantity <= 0
        or quantity_cap <= 0
        or plan_quantity > quantity_cap
        or candidate.get("planned_quantity") != plan_quantity
    ):
        errors.append(_issue("option_candidate_quantity_cap_mismatch", f"{root}.candidate_quantity_cap", "Planned contracts must be positive and no greater than the exact candidate cap."))
    plan_limit = _number(order.get("limit_price"))
    limit_cap = _number(candidate.get("candidate_limit_cap"))
    attested_limit = _number(candidate.get("planned_limit"))
    if (
        plan_limit is None
        or limit_cap is None
        or attested_limit is None
        or plan_limit <= 0
        or limit_cap <= 0
        or abs(plan_limit - attested_limit) > 1e-9
        or plan_limit > limit_cap + 1e-9
        or abs(plan_limit - _cent_price(plan_limit)) > 1e-9
        or abs(limit_cap - _cent_price(limit_cap)) > 1e-9
    ):
        errors.append(_issue("option_candidate_limit_cap_mismatch", f"{root}.candidate_limit_cap", "The cent-valid planned buy limit must match the attestation and not exceed the candidate cap."))

    spread_cap = _number(candidate.get("max_spread_fraction"))
    quote_at = _parse_aware_utc_timestamp(candidate.get("candidate_source_quote_at"))
    quote_basis = str(candidate.get("candidate_source_quote_time_basis") or "").strip().lower()
    bid = _number(candidate.get("candidate_source_bid"))
    ask = _number(candidate.get("candidate_source_ask"))
    spread = _number(candidate.get("candidate_source_spread_fraction"))
    expected_spread = (
        (ask - bid) / ((ask + bid) / 2.0)
        if bid is not None and ask is not None and bid > 0 and ask >= bid
        else None
    )
    if spread_cap is None or spread_cap <= 0 or spread_cap > SWING_EXECUTION_MAX_OPTION_SPREAD_PCT:
        errors.append(_issue("unsafe_option_candidate_spread_cap", f"{root}.max_spread_fraction", f"The frozen option-candidate spread cap must be positive and no greater than {SWING_EXECUTION_MAX_OPTION_SPREAD_PCT:.0%}."))
    if quote_at is None:
        errors.append(_issue("missing_option_candidate_quote_time", f"{root}.candidate_source_quote_at", "The candidate needs a timezone-aware source quote timestamp."))
    elif issued_at is not None and max_age is not None:
        quote_age = (issued_at - quote_at).total_seconds() / 60.0
        if quote_age < -1 or quote_age > max_age + 1e-9:
            errors.append(_issue("stale_or_future_option_candidate_quote", f"{root}.candidate_source_quote_at", "The candidate source quote must be current at packet issue."))
    if not quote_basis or (
        quote_basis != "provider_response_received_at"
        and not (
            any(token in quote_basis for token in ("provider", "broker", "exchange"))
            and "quote" in quote_basis
        )
    ):
        errors.append(_issue("invalid_option_candidate_quote_basis", f"{root}.candidate_source_quote_time_basis", "Option quote time needs explicit provider, broker, or exchange provenance."))
    if not str(candidate.get("candidate_quote_quality") or "").strip():
        errors.append(_issue("missing_option_candidate_quote_quality", f"{root}.candidate_quote_quality", "Option candidate quote quality must be explicit."))
    if not str(candidate.get("candidate_data_delay") or "").strip():
        errors.append(_issue("missing_option_candidate_data_delay", f"{root}.candidate_data_delay", "Option candidate data delay must be explicit."))
    if candidate.get("candidate_quote_is_research_only") not in {True, False}:
        errors.append(_issue("missing_option_candidate_quote_scope", f"{root}.candidate_quote_is_research_only", "Research-only quote scope must be explicit."))
    if (
        expected_spread is None
        or spread is None
        or abs(spread - expected_spread) > 1e-6
        or spread_cap is None
        or spread > spread_cap + 1e-12
        or spread > SWING_EXECUTION_MAX_OPTION_SPREAD_PCT + 1e-12
    ):
        errors.append(_issue("unsafe_option_candidate_source_spread", f"{root}.candidate_source_spread_fraction", f"Candidate bid/ask must be positive, ordered, arithmetically consistent, and within the frozen {SWING_EXECUTION_MAX_OPTION_SPREAD_PCT:.0%} hard cap."))

    exact_quote_fields = (
        "candidate_source_quote_at",
        "candidate_source_quote_time_basis",
        "candidate_quote_quality",
        "candidate_data_delay",
        "candidate_quote_is_research_only",
    )
    for field in exact_quote_fields:
        if quote.get(field) != candidate.get(field):
            errors.append(_issue("option_candidate_quote_constraint_mismatch", f"review_constraints.quote.{field}", "Quote constraints must exactly match the frozen option-candidate attestation."))
    for field in ("candidate_source_bid", "candidate_source_ask", "candidate_source_spread_fraction"):
        left = _number(quote.get(field))
        right = _number(candidate.get(field))
        if left is None or right is None or abs(left - right) > 1e-6:
            errors.append(_issue("option_candidate_quote_constraint_mismatch", f"review_constraints.quote.{field}", "Quote constraints must exactly match the frozen option-candidate attestation."))
    quote_spread_cap = _number(quote.get("max_spread_fraction"))
    if spread_cap is None or quote_spread_cap is None or abs(spread_cap - quote_spread_cap) > 1e-12:
        errors.append(_issue("option_candidate_quote_constraint_mismatch", "review_constraints.quote.max_spread_fraction", "The live spread cap must equal the stricter frozen candidate cap."))
    return errors


def _manual_review_context_errors(
    trade_plan: Any,
    *,
    snapshot_id: Any,
    issued_at: Any,
    expires_at: Any,
    review_gate_attested: bool,
    now: datetime,
) -> list[dict[str, str]]:
    """Require the Trade Desk's bounded account, quote, and gate context."""
    errors: list[dict[str, str]] = []
    asset = trade_plan.get("asset") if isinstance(trade_plan, dict) else None

    clean_snapshot_id = _prompt_text(snapshot_id, limit=160)
    if not isinstance(snapshot_id, str) or not snapshot_id.strip() or clean_snapshot_id != snapshot_id.strip():
        errors.append(
            _issue(
                "missing_or_invalid_snapshot_id",
                "snapshot_id",
                "A non-empty, single-line Trade Desk snapshot ID is required.",
            )
        )

    issued = _parse_aware_utc_timestamp(issued_at)
    expires = _parse_aware_utc_timestamp(expires_at)
    if issued is None:
        errors.append(
            _issue(
                "missing_or_invalid_issued_at",
                "issued_at",
                "A timezone-aware ISO packet issue time is required.",
            )
        )
    if expires is None:
        errors.append(
            _issue(
                "missing_or_invalid_expires_at",
                "expires_at",
                "A timezone-aware ISO packet expiry time is required.",
            )
        )
    if issued is not None and expires is not None:
        ttl_seconds = (expires - issued).total_seconds()
        if ttl_seconds <= 0:
            errors.append(
                _issue(
                    "invalid_review_window",
                    "expires_at",
                    "Packet expiry must be later than its issue time.",
                )
            )
        elif ttl_seconds > MAX_MANUAL_REVIEW_PACKET_TTL_SECONDS:
            errors.append(
                _issue(
                    "review_window_too_long",
                    "expires_at",
                    "Manual review packets may remain valid for at most 15 minutes.",
                )
            )
        if issued > now and (issued - now).total_seconds() > MAX_MANUAL_REVIEW_CLOCK_SKEW_SECONDS:
            errors.append(
                _issue(
                    "issued_at_in_future",
                    "issued_at",
                    "Packet issue time is too far in the future.",
                )
            )
        if expires <= now:
            errors.append(
                _issue(
                    "review_packet_expired",
                    "expires_at",
                    "The manual review packet has expired; rebuild it from fresh local and broker state.",
                )
            )

    if not review_gate_attested:
        errors.append(
            _issue(
                "review_gate_not_attested",
                "external_blockers",
                "The caller must attest that the external Trade Desk review gate ran, even when it found no blockers.",
            )
        )

    assumptions = (
        trade_plan.get("account_assumptions")
        if isinstance(trade_plan, dict) and isinstance(trade_plan.get("account_assumptions"), dict)
        else None
    )
    if assumptions is None:
        errors.append(
            _issue(
                "missing_account_assumptions",
                "account_assumptions",
                "Timestamped account-equity and risk assumptions from Trade Desk are required.",
            )
        )
        assumptions = {}

    equity = _number(assumptions.get("account_equity_dollars"))
    risk_fraction = _number(assumptions.get("risk_fraction"))
    allocation_fraction = _number(assumptions.get("allocation_fraction"))
    risk_budget = _number(assumptions.get("risk_budget_dollars"))
    allocation_cap = _number(assumptions.get("allocation_cap_dollars"))
    for field, value in (
        ("account_equity_dollars", equity),
        ("risk_fraction", risk_fraction),
        ("allocation_fraction", allocation_fraction),
        ("risk_budget_dollars", risk_budget),
        ("allocation_cap_dollars", allocation_cap),
    ):
        if value is None or value <= 0:
            errors.append(
                _issue(
                    f"missing_or_invalid_{field}",
                    f"account_assumptions.{field}",
                    f"A positive finite {field} assumption is required.",
                )
            )
    if risk_fraction is not None and risk_fraction > MAX_TRADE_RISK_FRACTION:
        errors.append(
            _issue(
                "risk_fraction_above_hard_cap",
                "account_assumptions.risk_fraction",
                f"Risk fraction cannot exceed {MAX_TRADE_RISK_FRACTION:.2%}.",
            )
        )
    if (
        allocation_fraction is not None
        and allocation_fraction > MAX_ACCOUNT_ALLOCATION_FRACTION
    ):
        errors.append(
            _issue(
                "allocation_fraction_above_hard_cap",
                "account_assumptions.allocation_fraction",
                "Allocation fraction cannot exceed 25%.",
            )
        )
    if equity is not None and risk_fraction is not None and risk_budget is not None:
        if not _same_money(risk_budget, equity * risk_fraction):
            errors.append(
                _issue(
                    "account_risk_budget_mismatch",
                    "account_assumptions.risk_budget_dollars",
                    "Risk budget must reconcile to assumed account equity times risk fraction.",
                )
            )
    if equity is not None and allocation_fraction is not None and allocation_cap is not None:
        if allocation_cap > equity * allocation_fraction + 0.011:
            errors.append(
                _issue(
                    "account_allocation_cap_mismatch",
                    "account_assumptions.allocation_cap_dollars",
                    "Allocation cap cannot exceed assumed account equity times allocation fraction.",
                )
            )

    risk = (
        trade_plan.get("risk")
        if isinstance(trade_plan, dict) and isinstance(trade_plan.get("risk"), dict)
        else {}
    )
    if risk_budget is not None and not _same_money(risk_budget, risk.get("risk_budget_dollars")):
        errors.append(
            _issue(
                "trade_plan_risk_budget_context_mismatch",
                "risk.risk_budget_dollars",
                "Trade-plan risk budget does not match the account context.",
            )
        )
    plan_allocation_cap = _number(risk.get("allocation_cap_dollars"))
    if (
        allocation_cap is not None
        and (
            plan_allocation_cap is None
            or plan_allocation_cap <= 0
            or plan_allocation_cap > allocation_cap + 0.011
        )
    ):
        errors.append(
            _issue(
                "trade_plan_allocation_cap_exceeds_context",
                "risk.allocation_cap_dollars",
                "Trade-plan allocation capacity must be positive and no greater than the account context cap.",
            )
        )

    constraints = (
        trade_plan.get("review_constraints")
        if isinstance(trade_plan, dict) and isinstance(trade_plan.get("review_constraints"), dict)
        else None
    )
    if constraints is None:
        errors.append(
            _issue(
                "missing_review_constraints",
                "review_constraints",
                "Account and live-quote review constraints from Trade Desk are required.",
            )
        )
        constraints = {}
    evidence = constraints.get("evidence") if isinstance(constraints.get("evidence"), dict) else {}
    account = constraints.get("account") if isinstance(constraints.get("account"), dict) else {}
    portfolio = constraints.get("portfolio") if isinstance(constraints.get("portfolio"), dict) else {}
    drawdown = constraints.get("drawdown") if isinstance(constraints.get("drawdown"), dict) else {}
    candidate = constraints.get("candidate") if isinstance(constraints.get("candidate"), dict) else {}
    quote = constraints.get("quote") if isinstance(constraints.get("quote"), dict) else {}
    if not evidence:
        errors.append(
            _issue(
                "missing_edge_evidence_constraints",
                "review_constraints.evidence",
                "A validated asset-specific Edge Lab attestation is required.",
            )
        )
    if not account:
        errors.append(_issue("missing_account_review_constraints", "review_constraints.account", "Account review constraints are required."))
    if not portfolio:
        errors.append(
            _issue(
                "missing_portfolio_review_constraints",
                "review_constraints.portfolio",
                "Same-account post-trade portfolio constraints are required.",
            )
        )
    if not drawdown:
        errors.append(
            _issue(
                "missing_account_drawdown_constraints",
                "review_constraints.drawdown",
                "A chained same-account equity drawdown attestation is required.",
            )
        )
    if not quote:
        errors.append(_issue("missing_quote_review_constraints", "review_constraints.quote", "Live-quote review constraints are required."))
    if asset == "share":
        errors.extend(
            _share_candidate_review_errors(
                candidate,
                trade_plan,
                issued_at=issued,
            )
        )
    elif asset == "option":
        errors.extend(
            _option_candidate_review_errors(
                candidate,
                trade_plan,
                quote,
                issued_at=issued,
            )
        )

    expected_evidence_asset = "option" if asset == "option" else "share"
    if evidence.get("schema") != "optedge_edge_lab_review_attestation_v1":
        errors.append(_issue("invalid_edge_evidence_schema", "review_constraints.evidence.schema", "The Edge Lab review attestation schema is unsupported."))
    if evidence.get("source_schema") != "optedge_edge_lab_v1":
        errors.append(_issue("invalid_edge_evidence_source", "review_constraints.evidence.source_schema", "Review evidence must come from the Edge Lab report."))
    evidence_digest = evidence.get("report_digest_sha256")
    if not isinstance(evidence_digest, str) or re.fullmatch(r"[0-9a-f]{64}", evidence_digest) is None:
        errors.append(_issue("invalid_edge_evidence_digest", "review_constraints.evidence.report_digest_sha256", "A full lowercase SHA-256 digest must bind the packet to one Edge Lab report."))
    if evidence.get("asset") != expected_evidence_asset:
        errors.append(_issue("edge_evidence_asset_mismatch", "review_constraints.evidence.asset", "Edge evidence must match the proposed asset class."))
    if evidence.get("edge_lab_status") != "validated":
        errors.append(_issue("edge_lab_not_validated", "review_constraints.evidence.edge_lab_status", "Edge Lab must have at least one validated live-capital lane."))
    if evidence.get("asset_lane_status") != "validated":
        errors.append(_issue("asset_edge_lane_not_validated", "review_constraints.evidence.asset_lane_status", "The proposed asset lane must be validated."))
    if evidence.get("asset_lane_live_capital_eligible") is not True:
        errors.append(_issue("asset_edge_lane_not_eligible", "review_constraints.evidence.asset_lane_live_capital_eligible", "The proposed asset lane must explicitly pass the live-capital evidence gate."))
    if evidence.get("evidence_lane") != "current_method_executable":
        errors.append(_issue("non_executable_edge_evidence", "review_constraints.evidence.evidence_lane", "Only current-method executable outcomes can authorize manual review."))
    if evidence.get("require_current_method_executable") is not True:
        errors.append(_issue("missing_current_method_edge_requirement", "review_constraints.evidence.require_current_method_executable", "The review must explicitly require current-method executable evidence."))
    headline_horizon = evidence.get("headline_horizon_sessions")
    if (
        not isinstance(headline_horizon, int)
        or isinstance(headline_horizon, bool)
        or headline_horizon <= 0
    ):
        errors.append(_issue("invalid_edge_evidence_horizon", "review_constraints.evidence.headline_horizon_sessions", "A positive integer evidence horizon is required."))

    if equity is not None and not _same_money(account.get("assumed_equity_dollars"), equity):
        errors.append(_issue("review_equity_context_mismatch", "review_constraints.account.assumed_equity_dollars", "Review equity must match the trade-plan account assumption."))
    for field, expected in (("risk_fraction", risk_fraction), ("allocation_fraction", allocation_fraction)):
        actual = _number(account.get(field))
        if expected is not None and (actual is None or abs(actual - expected) > 1e-9):
            errors.append(_issue(f"review_{field}_context_mismatch", f"review_constraints.account.{field}", f"Review {field} must match the trade-plan account assumption."))
    match_count = account.get("eligible_same_account_match_count")
    if not isinstance(match_count, int) or isinstance(match_count, bool) or match_count < 1:
        errors.append(
            _issue(
                "no_eligible_same_account_match",
                "review_constraints.account.eligible_same_account_match_count",
                "At least one single eligible account must pass equity, approval, risk, and buying-power checks.",
            )
        )
    for field in ("require_active", "require_agentic_allowed", "use_conservative_buying_power"):
        if account.get(field) is not True:
            errors.append(_issue(f"missing_{field}", f"review_constraints.account.{field}", f"{field} must be explicitly enabled."))
    if asset == "option" and account.get("require_options_approval") is not True:
        errors.append(_issue("missing_options_approval_gate", "review_constraints.account.require_options_approval", "Option review must require options approval on the selected account."))
    overstatement = _number(account.get("max_equity_overstatement_fraction"))
    if overstatement is None or overstatement < 0 or overstatement > 0.05:
        errors.append(_issue("unsafe_equity_overstatement_tolerance", "review_constraints.account.max_equity_overstatement_fraction", "Equity overstatement tolerance must be between 0% and 5%."))
    expected_account_key_derivation = {
        "schema": ACCOUNT_KEY_DERIVATION_SCHEMA,
        "algorithm": "sha256",
        "namespace": ACCOUNT_KEY_DERIVATION_NAMESPACE,
        "input_field": "get_accounts.account_number",
        "input_normalization": "strip_surrounding_whitespace",
        "output_prefix": "acct_",
        "lowercase_hex_characters": ACCOUNT_KEY_DERIVATION_HEX_LENGTH,
        "require_exact_eligible_key_match": True,
        "persist_raw_account_number": False,
    }
    if account.get("account_key_derivation") != expected_account_key_derivation:
        errors.append(
            _issue(
                "unsafe_account_key_derivation",
                "review_constraints.account.account_key_derivation",
                "The selected Robinhood account must use the exact versioned account-key derivation and must never persist the raw account number.",
            )
        )

    if portfolio.get("schema") != "optedge_portfolio_review_constraints_v1":
        errors.append(
            _issue(
                "invalid_portfolio_review_schema",
                "review_constraints.portfolio.schema",
                "The portfolio review constraint schema is missing or unsupported.",
            )
        )
    if portfolio.get("source") != "optedge_robinhood_broker_snapshot_v1":
        errors.append(
            _issue(
                "untrusted_portfolio_review_source",
                "review_constraints.portfolio.source",
                "Portfolio exposure must come from the normalized Robinhood broker snapshot.",
            )
        )
    if portfolio.get("raw_bundle_schema") != "optedge_robinhood_mcp_read_bundle_v2":
        errors.append(
            _issue(
                "untrusted_portfolio_raw_bundle",
                "review_constraints.portfolio.raw_bundle_schema",
                "Portfolio exposure requires a complete Robinhood read bundle.",
            )
        )
    snapshot_generated = _parse_aware_utc_timestamp(
        portfolio.get("broker_snapshot_generated_at")
    )
    if snapshot_generated is None:
        errors.append(
            _issue(
                "missing_portfolio_snapshot_timestamp",
                "review_constraints.portfolio.broker_snapshot_generated_at",
                "A timezone-aware broker snapshot timestamp is required.",
            )
        )
    elif issued is not None and snapshot_generated > issued + timedelta(seconds=MAX_MANUAL_REVIEW_CLOCK_SKEW_SECONDS):
        errors.append(
            _issue(
                "portfolio_snapshot_after_packet_issue",
                "review_constraints.portfolio.broker_snapshot_generated_at",
                "Broker snapshot time cannot be materially later than packet issue time.",
            )
        )
    snapshot_digest = portfolio.get("broker_snapshot_digest_sha256")
    if not isinstance(snapshot_digest, str) or re.fullmatch(r"[0-9a-f]{64}", snapshot_digest) is None:
        errors.append(
            _issue(
                "invalid_portfolio_snapshot_digest",
                "review_constraints.portfolio.broker_snapshot_digest_sha256",
                "A full lowercase SHA-256 digest must bind the portfolio review to one snapshot.",
            )
        )
    for field, expected in (
        ("same_account_only", True),
        ("local_research_counted_as_live", False),
    ):
        if portfolio.get(field) is not expected:
            errors.append(
                _issue(
                    f"unsafe_portfolio_{field}",
                    f"review_constraints.portfolio.{field}",
                    f"Portfolio constraint {field} must be explicitly {expected}.",
                )
            )
    if portfolio.get("nonterminal_order_policy") != "block":
        errors.append(
            _issue(
                "unsafe_portfolio_working_order_policy",
                "review_constraints.portfolio.nonterminal_order_policy",
                "Any same-account nonterminal order must block portfolio review.",
            )
        )
    if portfolio.get("cap_method") != (
        "min_assumed_and_live_same_account_equity_times_allocation_fraction"
    ):
        errors.append(
            _issue(
                "unsafe_portfolio_cap_method",
                "review_constraints.portfolio.cap_method",
                "The total-open cap must use the lower of assumed and live same-account equity.",
            )
        )
    expected_capital_basis = (
        "full_option_debit_at_risk_dollars"
        if asset == "option"
        else "full_share_notional_at_risk_dollars"
    )
    if portfolio.get("proposed_capital_basis") != expected_capital_basis:
        errors.append(
            _issue(
                "unsafe_portfolio_proposed_capital_basis",
                "review_constraints.portfolio.proposed_capital_basis",
                f"Portfolio proposed exposure must use {expected_capital_basis}.",
            )
        )

    portfolio_count = portfolio.get("eligible_account_count")
    portfolio_rows = portfolio.get("eligible_accounts")
    if not isinstance(portfolio_rows, list):
        errors.append(
            _issue(
                "missing_portfolio_attestations",
                "review_constraints.portfolio.eligible_accounts",
                "At least one same-account portfolio attestation is required.",
            )
        )
        portfolio_rows = []
    if (
        not isinstance(portfolio_count, int)
        or isinstance(portfolio_count, bool)
        or portfolio_count < 1
        or portfolio_count != len(portfolio_rows)
    ):
        errors.append(
            _issue(
                "portfolio_attestation_count_mismatch",
                "review_constraints.portfolio.eligible_account_count",
                "Portfolio attestation count must match a non-empty eligible-account list.",
            )
        )
    if isinstance(match_count, int) and not isinstance(match_count, bool):
        if portfolio_count != match_count:
            errors.append(
                _issue(
                    "portfolio_account_match_count_mismatch",
                    "review_constraints.portfolio.eligible_account_count",
                    "Portfolio attestations must match the eligible same-account count.",
                )
            )

    expected_proposed = _number(
        risk.get("full_option_debit_at_risk_dollars")
        if asset == "option"
        else risk.get("full_share_notional_at_risk_dollars")
    )
    seen_account_keys: set[str] = set()
    for index, row in enumerate(portfolio_rows):
        field = f"review_constraints.portfolio.eligible_accounts[{index}]"
        if not isinstance(row, dict):
            errors.append(
                _issue(
                    "invalid_portfolio_attestation",
                    field,
                    "Each portfolio attestation must be an object.",
                )
            )
            continue
        if row.get("schema") != "optedge_post_trade_portfolio_gate_v1":
            errors.append(
                _issue(
                    "invalid_post_trade_portfolio_schema",
                    f"{field}.schema",
                    "The post-trade portfolio attestation schema is unsupported.",
                )
            )
        if row.get("status") != "allowed" or row.get("allowed") is not True:
            errors.append(
                _issue(
                    "portfolio_attestation_not_allowed",
                    f"{field}.allowed",
                    "Every included portfolio attestation must explicitly allow the proposal.",
                )
            )
        if row.get("exposure_schema") != "optedge_broker_portfolio_exposure_v1":
            errors.append(
                _issue(
                    "invalid_portfolio_exposure_schema",
                    f"{field}.exposure_schema",
                    "The account exposure summary schema is unsupported.",
                )
            )
        position_count = row.get("position_count")
        if (
            not isinstance(position_count, int)
            or isinstance(position_count, bool)
            or position_count < 0
        ):
            errors.append(
                _issue(
                    "invalid_portfolio_position_count",
                    f"{field}.position_count",
                    "Portfolio position count must be a non-negative integer.",
                )
            )
        working_order_count = row.get("same_account_nonterminal_order_count")
        if working_order_count != 0:
            errors.append(
                _issue(
                    "portfolio_working_orders_present",
                    f"{field}.same_account_nonterminal_order_count",
                    "The same account must have zero nonterminal broker orders.",
                )
            )
        if row.get("blockers") != []:
            errors.append(
                _issue(
                    "portfolio_attestation_has_blockers",
                    f"{field}.blockers",
                    "An eligible portfolio attestation must contain an explicit empty blocker list.",
                )
            )
        account_key = _prompt_text(row.get("account_key"), limit=96)
        if (
            not isinstance(row.get("account_key"), str)
            or not account_key
            or account_key != row.get("account_key", "").strip()
            or re.fullmatch(r"acct_[0-9a-f]{16}", account_key) is None
            or account_key in seen_account_keys
        ):
            errors.append(
                _issue(
                    "invalid_or_duplicate_portfolio_account_key",
                    f"{field}.account_key",
                    "Each portfolio attestation needs one unique pseudonymous account key.",
                )
            )
        else:
            seen_account_keys.add(account_key)
        account_mask = _prompt_text(row.get("account_mask"), limit=16)
        if (
            not isinstance(row.get("account_mask"), str)
            or account_mask != row.get("account_mask", "").strip()
            or re.fullmatch(r"\.\.\.[A-Za-z0-9]{4}", account_mask) is None
        ):
            errors.append(
                _issue(
                    "invalid_portfolio_account_mask",
                    f"{field}.account_mask",
                    "Each portfolio attestation needs the normalized masked broker account suffix.",
                )
            )
        asof_text = row.get("asof")
        try:
            asof_date = date.fromisoformat(asof_text) if isinstance(asof_text, str) else None
        except ValueError:
            asof_date = None
        if (
            asof_date is None
            or asof_text != asof_date.isoformat()
            or (issued is not None and asof_date != issued.date())
        ):
            errors.append(
                _issue(
                    "invalid_portfolio_attestation_asof",
                    f"{field}.asof",
                    "Portfolio exposure must be recomputed for the packet issue date.",
                )
            )
        if row.get("equity_basis_method") != "min_assumed_and_live_same_account_equity":
            errors.append(
                _issue(
                    "unsafe_portfolio_equity_basis_method",
                    f"{field}.equity_basis_method",
                    "Portfolio capacity must use the lower of assumed and live same-account equity.",
                )
            )

        assumed = _number(row.get("assumed_equity_dollars"))
        live = _number(row.get("live_equity_dollars"))
        basis = _number(row.get("equity_basis_dollars"))
        row_allocation = _number(row.get("allocation_fraction"))
        cap = _number(row.get("allocation_cap_dollars"))
        current = _number(row.get("current_capital_at_risk_dollars"))
        proposed = _number(row.get("proposed_capital_at_risk_dollars"))
        post_trade = _number(row.get("post_trade_capital_at_risk_dollars"))
        headroom_before = _number(row.get("headroom_before_trade_dollars"))
        headroom_after = _number(row.get("headroom_after_trade_dollars"))
        if any(value is None or value <= 0 for value in (assumed, live, basis, row_allocation, cap, proposed)):
            errors.append(
                _issue(
                    "missing_portfolio_attestation_capacity",
                    field,
                    "Portfolio attestation equity, allocation, cap, and proposed exposure must be positive and finite.",
                )
            )
            continue
        if current is None or current < 0 or post_trade is None:
            errors.append(
                _issue(
                    "missing_portfolio_attestation_exposure",
                    field,
                    "Portfolio attestation current and post-trade exposure must be finite and non-negative.",
                )
            )
            continue
        assert assumed is not None and live is not None and basis is not None
        assert row_allocation is not None and cap is not None and proposed is not None
        assert current is not None and post_trade is not None
        if not _same_money(assumed, equity):
            errors.append(_issue("portfolio_assumed_equity_mismatch", f"{field}.assumed_equity_dollars", "Portfolio assumed equity must match the planner context."))
        if not _same_money(basis, min(assumed, live)):
            errors.append(_issue("portfolio_equity_basis_mismatch", f"{field}.equity_basis_dollars", "Portfolio equity basis must equal the lower of assumed and live equity."))
        if allocation_fraction is None or abs(row_allocation - allocation_fraction) > 1e-9:
            errors.append(_issue("portfolio_allocation_fraction_mismatch", f"{field}.allocation_fraction", "Portfolio allocation fraction must match the planner context."))
        if not _same_money(cap, basis * row_allocation):
            errors.append(_issue("portfolio_allocation_cap_mismatch", f"{field}.allocation_cap_dollars", "Portfolio allocation cap arithmetic does not reconcile."))
        if expected_proposed is None or not _same_money(proposed, expected_proposed):
            errors.append(_issue("portfolio_proposed_exposure_mismatch", f"{field}.proposed_capital_at_risk_dollars", "Proposed portfolio exposure must match the full option debit or share notional."))
        if not _same_money(post_trade, current + proposed):
            errors.append(_issue("portfolio_post_trade_exposure_mismatch", f"{field}.post_trade_capital_at_risk_dollars", "Post-trade exposure must equal current plus proposed exposure."))
        if post_trade > cap + 0.011:
            errors.append(_issue("portfolio_allocation_cap_exceeded", f"{field}.post_trade_capital_at_risk_dollars", "Post-trade exposure exceeds the total-open allocation cap."))
        if headroom_before is None or not _same_money(headroom_before, cap - current):
            errors.append(_issue("portfolio_headroom_before_mismatch", f"{field}.headroom_before_trade_dollars", "Pre-trade portfolio headroom arithmetic does not reconcile."))
        if headroom_after is None or not _same_money(headroom_after, cap - post_trade):
            errors.append(_issue("portfolio_headroom_after_mismatch", f"{field}.headroom_after_trade_dollars", "Post-trade portfolio headroom arithmetic does not reconcile."))

    if drawdown:
        errors.extend(
            _account_drawdown_review_errors(
                drawdown,
                risk_fraction=risk_fraction,
                portfolio_snapshot_digest=portfolio.get(
                    "broker_snapshot_digest_sha256"
                ),
                portfolio_rows=portfolio_rows,
                match_count=match_count,
                issued_at=issued,
            )
        )

    expected_quote_tool = "get_option_quotes" if asset == "option" else "get_equity_quotes"
    if quote.get("quote_tool") != expected_quote_tool:
        errors.append(_issue("missing_exact_quote_tool", "review_constraints.quote.quote_tool", f"Review must require {expected_quote_tool}."))
    max_quote_age = _number(quote.get("max_live_quote_age_seconds"))
    if max_quote_age is None or max_quote_age <= 0 or max_quote_age > 120:
        errors.append(_issue("unsafe_live_quote_age", "review_constraints.quote.max_live_quote_age_seconds", "Live quote age must be capped between 1 and 120 seconds."))
    max_spread = _number(quote.get("max_spread_fraction"))
    hard_spread_cap = SWING_EXECUTION_MAX_OPTION_SPREAD_PCT if asset == "option" else 0.01
    if max_spread is None or max_spread <= 0 or max_spread > hard_spread_cap:
        errors.append(
            _issue(
                "unsafe_spread_cap",
                "review_constraints.quote.max_spread_fraction",
                f"The bid/ask spread cap must be positive and no greater than {hard_spread_cap:.0%} for {asset} entries.",
            )
        )
    if quote.get("require_positive_bid_ask") is not True:
        errors.append(_issue("missing_positive_quote_gate", "review_constraints.quote.require_positive_bid_ask", "Review must require a positive, ordered bid and ask."))
    if quote.get("require_live_tick_validation") is not True:
        errors.append(_issue("missing_live_tick_gate", "review_constraints.quote.require_live_tick_validation", "Review must validate the live instrument tick size before preview."))
    if quote.get("limit_price_may_increase") is not False:
        errors.append(_issue("unsafe_limit_price_policy", "review_constraints.quote.limit_price_may_increase", "The packet limit price may never increase."))
    if asset == "option":
        order = trade_plan.get("order") if isinstance(trade_plan.get("order"), dict) else {}
        expected_chain_symbol = str(order.get("symbol") or "").strip().upper()
        if quote.get("expected_underlying_type") != "equity":
            errors.append(_issue("missing_option_equity_underlying_gate", "review_constraints.quote.expected_underlying_type", "Option review must require an equity underlying."))
        if quote.get("expected_chain_symbol") != expected_chain_symbol:
            errors.append(_issue("option_chain_symbol_constraint_mismatch", "review_constraints.quote.expected_chain_symbol", "The live instrument chain_symbol must exactly match the planned underlying."))
        if re.search(r"\d", expected_chain_symbol):
            errors.append(_issue("numeric_adjusted_option_root", "order.symbol", "Numeric adjusted option roots cannot open a new position."))
        if quote.get("expected_contract_multiplier") != 100:
            errors.append(_issue("missing_standard_option_multiplier_gate", "review_constraints.quote.expected_contract_multiplier", "The live chain multiplier must be exactly 100."))
        for field in (
            "require_active_instrument",
            "require_buy_to_open_tradable",
            "require_exact_chain_symbol",
            "require_exact_instrument_chain_id_match",
            "require_unique_chain_record",
            "require_unique_instrument_across_all_expiry_chains",
            "require_chain_can_open_position",
            "require_chain_cash_component_null",
            "require_chain_underlying_instrument_match",
            "require_complete_instrument_and_chain_lookup",
            "reject_numeric_adjusted_roots",
            "require_standard_contract_proof",
            "block_adjusted_or_nonstandard_deliverables",
        ):
            if quote.get(field) is not True:
                errors.append(_issue(f"missing_option_{field}", f"review_constraints.quote.{field}", f"Option control {field} must be explicitly enabled."))
    return errors


def _packet_id(
    trade_plan: dict[str, Any],
    snapshot_id: str | None,
    issued_at: str | None,
    expires_at: str | None,
) -> str:
    canonical = json.dumps(
        {
            "snapshot_id": snapshot_id,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "trade_plan": trade_plan,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "manual-review-" + hashlib.sha256(canonical).hexdigest()[:16]


def _packet_order_ref_id(packet_id: str) -> str:
    """Return the one Robinhood idempotency key for this logical packet order."""
    return str(uuid.uuid5(OPTEDGE_ORDER_REF_NAMESPACE, packet_id))


def _bind_packet_order_ref_id(
    review_plan: dict[str, Any],
    packet_id: str,
) -> dict[str, Any]:
    """Copy a ready review plan and replace its design-time ref placeholder."""
    bound = dict(review_plan)
    place_args = review_plan.get("place_arguments_after_confirmation")
    if isinstance(place_args, dict):
        bound["place_arguments_after_confirmation"] = {
            **place_args,
            "ref_id": _packet_order_ref_id(packet_id),
        }
    return bound


def _expected_confirmation_summary(trade_plan: Any) -> dict[str, Any]:
    """Derive the human confirmation fields from the canonical trade plan."""
    order = (
        trade_plan.get("order")
        if isinstance(trade_plan, dict) and isinstance(trade_plan.get("order"), dict)
        else {}
    )
    risk = (
        trade_plan.get("risk")
        if isinstance(trade_plan, dict) and isinstance(trade_plan.get("risk"), dict)
        else {}
    )
    assumptions = (
        trade_plan.get("account_assumptions")
        if isinstance(trade_plan, dict)
        and isinstance(trade_plan.get("account_assumptions"), dict)
        else {}
    )
    return {
        "account_number": ACCOUNT_NUMBER_PLACEHOLDER,
        "symbol": order.get("symbol"),
        "contract": order.get("contract_label"),
        "underlying_type": order.get("underlying_type"),
        "intent": order.get("intent"),
        "side": order.get("side"),
        "quantity": order.get("quantity"),
        "order_type": order.get("order_type"),
        "limit_price": order.get("limit_price"),
        "stop_price": order.get("stop_price"),
        "target_price": order.get("target_price"),
        "contract_multiplier": order.get("contract_multiplier"),
        "account_equity_assumption_dollars": assumptions.get("account_equity_dollars"),
        "risk_fraction": assumptions.get("risk_fraction"),
        "allocation_fraction": assumptions.get("allocation_fraction"),
        "risk_budget_dollars": assumptions.get("risk_budget_dollars"),
        "allocation_cap_dollars": assumptions.get("allocation_cap_dollars"),
        "planned_stop_loss_dollars": risk.get("planned_stop_loss_dollars"),
        "planned_max_loss_dollars": risk.get("planned_max_loss_dollars"),
        "full_share_notional_at_risk_dollars": risk.get(
            "full_share_notional_at_risk_dollars"
        ),
        "full_option_debit_at_risk_dollars": risk.get(
            "full_option_debit_at_risk_dollars"
        ),
        "max_loss_is_unbounded": risk.get("max_loss_is_unbounded") is True,
        "stop_is_not_broker_order": risk.get("stop_is_not_broker_order") is True,
    }


def _packet_content_digest(packet: Any) -> str | None:
    """Hash every semantic packet field, excluding rendered text and digests."""
    if not isinstance(packet, dict):
        return None
    canonical_packet = {
        key: value
        for key, value in packet.items()
        if key not in {"content_digest_sha256", "prompt", "prompt_digest_sha256"}
    }
    canonical = json.dumps(
        canonical_packet,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def validate_manual_robinhood_review_packet(
    packet: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Revalidate a ready packet's structure, arithmetic binding, age, and digests.

    The digests detect accidental or post-build modification; they are not a
    cryptographic signature and do not replace the mandatory fresh broker reads,
    preview, and explicit confirmation.
    """
    errors: list[dict[str, str]] = []
    if not isinstance(packet, dict):
        return {
            "schema": MANUAL_REVIEW_PACKET_INTEGRITY_SCHEMA,
            "ok": False,
            "errors": [
                _issue("invalid_manual_review_packet", "packet", "Packet must be an object.")
            ],
        }
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        errors.append(
            _issue(
                "invalid_validation_clock",
                "now",
                "Packet validation requires a timezone-aware clock.",
            )
        )
        current = current.replace(tzinfo=UTC)
    else:
        current = current.astimezone(UTC)

    if packet.get("schema") != MANUAL_REVIEW_PACKET_SCHEMA:
        errors.append(
            _issue(
                "invalid_manual_review_packet_schema",
                "schema",
                "Manual review packet schema is missing or unsupported.",
            )
        )
    if packet.get("content_digest_algorithm") != "sha256-canonical-json-v1":
        errors.append(
            _issue(
                "invalid_packet_digest_algorithm",
                "content_digest_algorithm",
                "Packet content must use the supported canonical SHA-256 digest.",
            )
        )
    expected_content_digest = _packet_content_digest(packet)
    if (
        not isinstance(packet.get("content_digest_sha256"), str)
        or packet.get("content_digest_sha256") != expected_content_digest
    ):
        errors.append(
            _issue(
                "manual_review_packet_content_changed",
                "content_digest_sha256",
                "Packet content no longer matches the digest created by Trade Desk.",
            )
        )
    prompt = packet.get("prompt")
    expected_prompt_digest = (
        hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if isinstance(prompt, str) and prompt
        else None
    )
    if (
        expected_prompt_digest is None
        or packet.get("prompt_digest_sha256") != expected_prompt_digest
    ):
        errors.append(
            _issue(
                "manual_review_packet_prompt_changed",
                "prompt_digest_sha256",
                "Rendered review instructions are missing or no longer match their digest.",
            )
        )

    expected_id = _packet_id(
        packet.get("trade_plan") if isinstance(packet.get("trade_plan"), dict) else {},
        packet.get("snapshot_id"),
        packet.get("issued_at"),
        packet.get("expires_at"),
    )
    if packet.get("packet_id") != expected_id:
        errors.append(
            _issue(
                "manual_review_packet_id_mismatch",
                "packet_id",
                "Packet identity does not match its trade plan and review window.",
            )
        )

    for field, expected in (
        ("does_not_place_orders", True),
        ("automation_allowed", False),
        ("repeat_orders_allowed", False),
        ("contains_credentials", False),
        ("requires_explicit_user_confirmation", True),
        ("standalone_broker_authority", False),
    ):
        if packet.get(field) is not expected:
            errors.append(
                _issue(
                    f"unsafe_packet_{field}",
                    field,
                    f"Packet field {field} must be explicitly {expected}.",
                )
            )

    ready = packet.get("status") == "manual_review_required"
    review = packet.get("review_plan") if isinstance(packet.get("review_plan"), dict) else {}
    if not ready or review.get("review_allowed") is not True:
        errors.append(
            _issue(
                "manual_review_packet_not_actionable",
                "status",
                "Only an unexpired packet that is explicitly ready for manual review is actionable.",
            )
        )
    if packet.get("review_gate_attested") is not True:
        errors.append(
            _issue(
                "manual_review_gate_not_attested",
                "review_gate_attested",
                "A ready packet must attest that the local review gate ran.",
            )
        )
    if packet.get("external_review_gate_blockers") != []:
        errors.append(
            _issue(
                "manual_review_packet_has_external_blockers",
                "external_review_gate_blockers",
                "A ready packet must contain an explicit empty external blocker list.",
            )
        )

    trade_plan = packet.get("trade_plan") if isinstance(packet.get("trade_plan"), dict) else {}
    asset = trade_plan.get("asset")
    expected_review = (
        build_robinhood_equity_review_plan(trade_plan)
        if asset == "share"
        else build_robinhood_option_review_plan(trade_plan)
        if asset == "option"
        else {}
    )
    if expected_review.get("review_allowed") is True:
        expected_review = _bind_packet_order_ref_id(expected_review, expected_id)
    if review != expected_review:
        errors.append(
            _issue(
                "manual_review_plan_changed",
                "review_plan",
                "Broker review instructions do not match the validated trade plan.",
            )
        )
    expected_ref_id = _packet_order_ref_id(expected_id)
    place_arguments = (
        review.get("place_arguments_after_confirmation")
        if isinstance(review.get("place_arguments_after_confirmation"), dict)
        else {}
    )
    if place_arguments.get("ref_id") != expected_ref_id:
        errors.append(
            _issue(
                "manual_review_order_ref_id_mismatch",
                "review_plan.place_arguments_after_confirmation.ref_id",
                "The one packet-scoped Robinhood idempotency key is missing or changed.",
            )
        )
    if packet.get("confirmation_summary") != _expected_confirmation_summary(trade_plan):
        errors.append(
            _issue(
                "manual_review_confirmation_changed",
                "confirmation_summary",
                "Confirmation fields do not match the validated trade plan.",
            )
        )
    plan_constraints = (
        trade_plan.get("review_constraints")
        if isinstance(trade_plan.get("review_constraints"), dict)
        else {}
    )
    if packet.get("review_constraints") != plan_constraints:
        errors.append(
            _issue(
                "manual_review_constraints_changed",
                "review_constraints",
                "Packet review constraints do not match the validated trade plan.",
            )
        )

    context_errors = _manual_review_context_errors(
        trade_plan,
        snapshot_id=packet.get("snapshot_id"),
        issued_at=packet.get("issued_at"),
        expires_at=packet.get("expires_at"),
        review_gate_attested=packet.get("review_gate_attested") is True,
        now=current,
    )
    errors.extend(context_errors)

    controls = packet.get("manual_controls") if isinstance(packet.get("manual_controls"), dict) else {}
    for field, expected in (
        ("one_logical_order_only", True),
        ("review_must_precede_place", True),
        ("exact_confirmation_must_follow_review", True),
        ("place_arguments_must_match_review", True),
        ("query_order_state_if_result_uncertain", True),
        ("never_collect_credentials", True),
        ("never_schedule_or_loop", True),
        ("entry_order_only", True),
        ("stop_and_target_are_not_placed", True),
        ("fresh_broker_quote_required", True),
        ("live_account_risk_recalculation_required", True),
        ("exact_account_key_derivation_required", True),
        ("chained_account_drawdown_interlock_required", True),
        ("live_total_open_portfolio_recalculation_required", True),
        ("complete_broker_pagination_required", True),
        ("recent_unreconciled_fill_block_required", True),
        ("fresh_quotes_for_all_open_exposure_required", True),
        ("post_confirmation_state_reread_required", True),
        ("post_confirmation_quote_and_instrument_reread_required", True),
        ("placement_time_expiry_recheck_required", True),
        ("live_tick_validation_required", True),
        ("limit_price_may_increase", False),
    ):
        if controls.get(field) is not expected:
            errors.append(
                _issue(
                    f"unsafe_manual_control_{field}",
                    f"manual_controls.{field}",
                    f"Manual control {field} must be explicitly {expected}.",
                )
            )

    return {
        "schema": MANUAL_REVIEW_PACKET_INTEGRITY_SCHEMA,
        "ok": not errors,
        "checked_at": current.isoformat(),
        "errors": errors,
        "content_digest_sha256": expected_content_digest,
        "digest_is_authentication": False,
    }


def build_manual_robinhood_review_packet(
    trade_plan: dict[str, Any],
    *,
    snapshot_id: str | None = None,
    issued_at: str | None = None,
    expires_at: str | None = None,
    external_blockers: list[str] | None = None,
) -> dict[str, Any]:
    """Package one fresh Trade Desk plan for manual, approval-gated review.

    ``external_blockers`` must be passed explicitly.  An empty list is the
    caller's attestation that the external Trade Desk gate ran and found no
    blockers; omitting it blocks the packet.
    """
    asset = trade_plan.get("asset") if isinstance(trade_plan, dict) else None
    if asset == "share":
        review_plan = build_robinhood_equity_review_plan(trade_plan)
    elif asset == "option":
        review_plan = build_robinhood_option_review_plan(trade_plan)
    else:
        review_plan = _blocked_review_plan(
            "optedge_robinhood_unknown_review_plan_v1",
            str(asset or "unknown"),
            "unknown",
            "unknown",
            [_issue("unsupported_asset", "asset", "Only share and long-option review packets are supported.")],
        )

    review_gate_attested = isinstance(external_blockers, list)
    blocker_messages: list[str] = []
    gate_input_errors: list[dict[str, str]] = []
    if review_gate_attested:
        for value in external_blockers:
            clean_value = _prompt_text(value)
            if not isinstance(value, str) or not clean_value:
                gate_input_errors.append(
                    _issue(
                        "invalid_external_review_gate_blocker",
                        "external_blockers",
                        "External review-gate blockers must be non-empty strings.",
                    )
                )
                continue
            blocker_messages.append(clean_value)

    context_errors = _manual_review_context_errors(
        trade_plan,
        snapshot_id=snapshot_id,
        issued_at=issued_at,
        expires_at=expires_at,
        review_gate_attested=review_gate_attested,
        now=datetime.now(UTC),
    )
    context_errors.extend(gate_input_errors)
    external_gate_errors = [
        _issue("external_review_gate_blocked", "review_gate", message)
        for message in blocker_messages
    ]
    if context_errors or external_gate_errors:
        existing_errors = (
            (review_plan.get("validation") or {}).get("errors") or []
            if isinstance(review_plan, dict)
            else []
        )
        review_plan = _blocked_review_plan(
            str(review_plan.get("schema") or "optedge_robinhood_blocked_review_plan_v1"),
            str(asset or "unknown"),
            str(review_plan.get("review_tool") or "unknown"),
            str(review_plan.get("place_tool_after_explicit_confirmation") or "unknown"),
            [*existing_errors, *context_errors, *external_gate_errors],
        )

    review_constraints = (
        trade_plan.get("review_constraints")
        if isinstance(trade_plan, dict) and isinstance(trade_plan.get("review_constraints"), dict)
        else {}
    )
    ready = review_plan.get("review_allowed") is True
    packet_id = _packet_id(
        trade_plan if isinstance(trade_plan, dict) else {},
        snapshot_id,
        issued_at,
        expires_at,
    )
    if ready:
        review_plan = _bind_packet_order_ref_id(review_plan, packet_id)
    packet = {
        "schema": MANUAL_REVIEW_PACKET_SCHEMA,
        "packet_id": packet_id,
        "snapshot_id": snapshot_id,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "broker": "robinhood",
        "status": "manual_review_required" if ready else "blocked",
        "does_not_place_orders": True,
        "automation_allowed": False,
        "repeat_orders_allowed": False,
        "contains_credentials": False,
        "standalone_broker_authority": False,
        "requires_explicit_user_confirmation": True,
        "review_gate_attested": review_gate_attested,
        "external_review_gate_blockers": blocker_messages,
        "context_validation": _validation(context_errors, []),
        "trade_plan": trade_plan,
        "review_constraints": review_constraints,
        "review_plan": review_plan,
        "confirmation_summary": _expected_confirmation_summary(trade_plan),
        "next_step": (
            f"Run {review_plan.get('review_tool')} and present its full preview; then ask for exact confirmation."
            if ready
            else "Fix the trade-plan and review-plan validation errors. Do not call a broker order tool."
        ),
        "manual_controls": {
            "one_logical_order_only": True,
            "review_must_precede_place": True,
            "exact_confirmation_must_follow_review": True,
            "place_arguments_must_match_review": True,
            "query_order_state_if_result_uncertain": True,
            "never_collect_credentials": True,
            "never_schedule_or_loop": True,
            "entry_order_only": True,
            "stop_and_target_are_not_placed": True,
            "fresh_broker_quote_required": True,
            "live_account_risk_recalculation_required": True,
            "exact_account_key_derivation_required": True,
            "chained_account_drawdown_interlock_required": True,
            "live_total_open_portfolio_recalculation_required": True,
            "complete_broker_pagination_required": True,
            "recent_unreconciled_fill_block_required": True,
            "fresh_quotes_for_all_open_exposure_required": True,
            "post_confirmation_state_reread_required": True,
            "post_confirmation_quote_and_instrument_reread_required": True,
            "placement_time_expiry_recheck_required": True,
            "live_tick_validation_required": True,
            "limit_price_may_increase": False,
        },
        "content_digest_algorithm": "sha256-canonical-json-v1",
    }
    packet["content_digest_sha256"] = _packet_content_digest(packet)
    packet["prompt"] = _render_manual_robinhood_review_prompt_unchecked(packet)
    packet["prompt_digest_sha256"] = hashlib.sha256(
        packet["prompt"].encode("utf-8")
    ).hexdigest()
    return packet


def _render_manual_robinhood_review_prompt_unchecked(packet: dict[str, Any]) -> str:
    """Render strict instructions for one manual Codex/Robinhood review."""
    review = packet.get("review_plan") if isinstance(packet.get("review_plan"), dict) else {}
    summary = packet.get("confirmation_summary") if isinstance(packet.get("confirmation_summary"), dict) else {}
    constraints = packet.get("review_constraints") if isinstance(packet.get("review_constraints"), dict) else {}
    evidence_constraints = constraints.get("evidence") if isinstance(constraints.get("evidence"), dict) else {}
    account_constraints = constraints.get("account") if isinstance(constraints.get("account"), dict) else {}
    portfolio_constraints = constraints.get("portfolio") if isinstance(constraints.get("portfolio"), dict) else {}
    drawdown_constraints = constraints.get("drawdown") if isinstance(constraints.get("drawdown"), dict) else {}
    candidate_constraints = constraints.get("candidate") if isinstance(constraints.get("candidate"), dict) else {}
    quote_constraints = constraints.get("quote") if isinstance(constraints.get("quote"), dict) else {}
    asset = _prompt_text(review.get("asset"))
    ready = packet.get("status") == "manual_review_required" and review.get("review_allowed") is True
    if not ready:
        errors = (review.get("validation") or {}).get("errors") or []
        error_lines = "\n".join(
            f"- {_prompt_text(row.get('code'))}: {_prompt_text(row.get('message'))}"
            for row in errors
            if isinstance(row, dict)
        ) or "- The packet is not actionable."
        return (
            "# Optedge Manual Robinhood Review\n\n"
            "STATUS: BLOCKED\n\n"
            "DO NOT CALL any Robinhood review or placement tool.\n"
            "Do not schedule, loop, repeat, or automate this packet.\n"
            "Do not request, accept, print, or store Robinhood passwords, tokens, API keys, MFA codes, or cookies.\n\n"
            f"Validation errors:\n{error_lines}\n"
        )

    order_label = _prompt_text(summary.get("contract") or summary.get("symbol") or "unknown instrument")
    review_tool = _prompt_text(review.get("review_tool"))
    place_tool = _prompt_text(review.get("place_tool_after_explicit_confirmation"))
    preflight = ", ".join(_prompt_text(value, limit=80) for value in (review.get("preflight_read_tools") or []))
    planned_stop = _number(summary.get("planned_stop_loss_dollars"))
    maximum_loss = _number(summary.get("planned_max_loss_dollars"))
    stop_price = _number(summary.get("stop_price"))
    target_price = _number(summary.get("target_price"))
    multiplier = summary.get("contract_multiplier")
    full_debit = summary.get("full_option_debit_at_risk_dollars")
    full_notional = _number(summary.get("full_share_notional_at_risk_dollars"))
    assumed_equity = _number(summary.get("account_equity_assumption_dollars"))
    risk_fraction = _number(summary.get("risk_fraction"))
    allocation_fraction = _number(summary.get("allocation_fraction"))
    max_quote_age_seconds = int(_number(quote_constraints.get("max_live_quote_age_seconds")) or 0)
    max_spread_fraction = _number(quote_constraints.get("max_spread_fraction"))
    max_equity_overstatement = _number(account_constraints.get("max_equity_overstatement_fraction"))
    portfolio_rows = (
        portfolio_constraints.get("eligible_accounts")
        if isinstance(portfolio_constraints.get("eligible_accounts"), list)
        else []
    )
    drawdown_rows = (
        drawdown_constraints.get("eligible_accounts")
        if isinstance(drawdown_constraints.get("eligible_accounts"), list)
        else []
    )
    evidence_lines = (
        f"- Edge evidence: {_prompt_text(evidence_constraints.get('asset'))} "
        f"{_prompt_text(evidence_constraints.get('evidence_lane'))} lane, "
        f"{_prompt_text(evidence_constraints.get('headline_horizon_sessions'))}-session horizon, "
        "validated for manual review\n"
    )
    share_candidate_lines = (
        f"- Exact share candidate: {_prompt_text(candidate_constraints.get('source_file'))} / "
        f"{_prompt_text(candidate_constraints.get('candidate_fingerprint'))}, "
        f"artifact digest {_prompt_text(candidate_constraints.get('source_artifact_digest_sha256'))}\n"
        if asset == "share" and candidate_constraints
        else ""
    )
    option_candidate_lines = (
        f"- Exact option candidate: fingerprint {_prompt_text(candidate_constraints.get('candidate_fingerprint'))}, "
        f"row digest {_prompt_text(candidate_constraints.get('candidate_row_digest_sha256'))}\n"
        f"- Source cycle: {_prompt_text(candidate_constraints.get('cycle_generated_at'))}, "
        f"digest {_prompt_text(candidate_constraints.get('cycle_digest_sha256'))}\n"
        f"- Source queue: {_prompt_text(candidate_constraints.get('queue_generated_at'))}, "
        f"digest {_prompt_text(candidate_constraints.get('queue_digest_sha256'))}\n"
        if asset == "option" and candidate_constraints
        else ""
    )
    candidate_lines = share_candidate_lines + option_candidate_lines
    stop_line = (
        f"- Planned stop-loss risk (not guaranteed): ${planned_stop:.2f}\n"
        if planned_stop is not None
        else ""
    )
    max_loss_line = (
        f"- Maximum capital-loss reference: ${maximum_loss:.2f}\n"
        if maximum_loss is not None
        else "- Maximum capital-loss reference: unbounded; broker review is blocked\n"
        if summary.get("max_loss_is_unbounded")
        else ""
    )
    notional_line = (
        f"- Full share notional exposed: ${full_notional:.2f}\n"
        if full_notional is not None
        else ""
    )
    debit_line = (
        f"- Full option debit at risk: ${float(full_debit):.2f}\n"
        if _number(full_debit) is not None
        else ""
    )
    stop_target_lines = (
        (f"- Planning stop reference: ${stop_price:.2f}\n" if stop_price is not None else "")
        + (f"- Planning target reference: ${target_price:.2f}\n" if target_price is not None else "")
    )
    multiplier_line = (
        f"- Expected contract multiplier: {int(multiplier)}x; verify it from the live chain\n"
        if isinstance(multiplier, int) and not isinstance(multiplier, bool)
        else ""
    )
    account_assumption_lines = (
        f"- Planner account-equity assumption: ${assumed_equity:.2f}\n"
        f"- Per-trade risk fraction: {risk_fraction:.2%}\n"
        f"- Maximum total-open allocation fraction: {allocation_fraction:.2%}\n"
        if assumed_equity is not None and risk_fraction is not None and allocation_fraction is not None
        else ""
    )
    portfolio_lines = "".join(
        (
            f"- Eligible snapshot account {index + 1} "
            f"(mask {_prompt_text(row.get('account_mask'))}; "
            f"account_key {_prompt_text(row.get('account_key'))}): "
            f"current ${float(row.get('current_capital_at_risk_dollars')):.2f} + "
            f"proposed ${float(row.get('proposed_capital_at_risk_dollars')):.2f} = "
            f"post-trade ${float(row.get('post_trade_capital_at_risk_dollars')):.2f} "
            f"against cap ${float(row.get('allocation_cap_dollars')):.2f}\n"
        )
        for index, row in enumerate(portfolio_rows)
        if isinstance(row, dict)
        and all(
            _number(row.get(field)) is not None
            for field in (
                "current_capital_at_risk_dollars",
                "proposed_capital_at_risk_dollars",
                "post_trade_capital_at_risk_dollars",
                "allocation_cap_dollars",
            )
        )
    )
    drawdown_lines = "".join(
        (
            f"- Account-loss firewall {index + 1} "
            f"(mask {_prompt_text(row.get('account_mask'))}; "
            f"account_key {_prompt_text(row.get('account_key'))}): "
            f"high-water drawdown {float(row.get('high_water_drawdown_fraction')):.2%}, "
            f"NY-session change {float(row.get('ny_session_loss_fraction')):.2%}, "
            f"risk multiplier {float(row.get('risk_multiplier')):.2f}x, "
            f"maximum risk {float(row.get('max_allowed_risk_fraction')):.2%}\n"
        )
        for index, row in enumerate(drawdown_rows)
        if isinstance(row, dict)
        and all(
            _number(row.get(field)) is not None
            for field in (
                "high_water_drawdown_fraction",
                "ny_session_loss_fraction",
                "risk_multiplier",
                "max_allowed_risk_fraction",
            )
        )
    )
    quote_policy_lines = (
        f"- Live quote maximum age: {max_quote_age_seconds} seconds\n"
        f"- Maximum live bid/ask spread: {max_spread_fraction:.2%}\n"
        "- Bid and ask must both be positive; ask must be at least bid; the packet limit may never increase\n"
        if max_quote_age_seconds > 0 and max_spread_fraction is not None
        else ""
    )
    if asset == "option":
        live_risk_rule = (
            "For the chosen account, require full option debit <= total_value x risk_fraction, "
            "existing same-account broker capital at risk + full option debit <= "
            "min(planner equity, live total_value) x the total-open allocation fraction, and "
            "full option debit <= conservative buying power."
        )
        live_quote_rule = (
            "Fetch every option-chain page, select every chain whose expiration_dates contains the exact planned expiry, "
            "then query every selected chain_id through every get_option_instruments page with the exact type, strike, "
            "expiry, active state, and tradable filters. Require exactly one total matching buy-to-open tradable equity "
            "instrument across all eligible chains, then call get_option_quotes for that option_id. Require quote.updated_at no older than the packet's "
            "maximum quote age, bid_price > 0, ask_price >= bid_price, and "
            "(ask_price - bid_price) / ((ask_price + bid_price) / 2) <= the packet spread cap. Also require the "
            "instrument.chain_symbol to exactly equal the planned nonnumeric underlying. Inspect the unique complete "
            "chain whose id exactly equals instrument.chain_id; require chain.symbol to equal the planned underlying, "
            "can_open_position to be true, trade_value_multiplier to equal 100, cash_component to be null, and underlying_instruments to contain the "
            "exact planned equity symbol. Reject numeric adjusted roots and STOP if any page, field, or unique match is "
            "missing or ambiguous, or if the instrument, chain, or preview shows any adjusted/nonstandard deliverable. "
            "A 100x multiplier alone is not proof of a standard contract."
        )
    else:
        live_risk_rule = (
            "For the chosen account, require planned stop loss <= total_value x risk_fraction, existing "
            "same-account broker capital at risk + full share notional <= min(planner equity, live total_value) "
            "x the total-open allocation fraction, and order notional <= conservative buying power."
        )
        live_quote_rule = (
            "Call get_equity_quotes for the exact symbol. Require venue_bid_time and venue_ask_time no older than the "
            "packet's maximum quote age, bid_price > 0, ask_price >= bid_price, and "
            "(ask_price - bid_price) / ((ask_price + bid_price) / 2) <= the packet spread cap."
        )
    packet_id = _prompt_text(packet.get("packet_id"))
    content_digest = _prompt_text(packet.get("content_digest_sha256"))
    issued_at = _prompt_text(packet.get("issued_at") or "not recorded")
    expires_at = _prompt_text(packet.get("expires_at") or "not recorded")
    review_template = json.dumps(review.get("review_arguments_template"), indent=2, sort_keys=True)
    lookup_template = json.dumps(review.get("contract_lookup"), indent=2, sort_keys=True)
    place_arguments = (
        review.get("place_arguments_after_confirmation")
        if isinstance(review.get("place_arguments_after_confirmation"), dict)
        else {}
    )
    packet_ref_id = _prompt_text(place_arguments.get("ref_id"))
    return (
        "# Optedge Manual Robinhood Review\n\n"
        "MANUAL, ONE-ORDER WORKFLOW ONLY. This packet never authorizes automation.\n\n"
        "## Packet identity\n"
        f"- Packet: {packet_id}\n"
        f"- Content digest: {content_digest}\n"
        f"- Issued: {issued_at}\n"
        f"- Expires: {expires_at}\n"
        f"- One logical-order ref_id: {packet_ref_id}\n"
        "- The digest detects modification; it is not a signature or broker authorization.\n"
        "- If the expiry is missing or has passed, stop. Recalculate from a fresh Optedge and broker snapshot.\n\n"
        "## Exact local plan\n"
        f"- Instrument: {order_label}\n"
        f"- Intent: {_prompt_text(summary.get('intent'))}\n"
        f"- Side: {_prompt_text(summary.get('side'))}\n"
        f"- Quantity: {_prompt_text(summary.get('quantity'))}\n"
        f"- Entry order: {_prompt_text(summary.get('order_type'))} at ${float(summary.get('limit_price')):.2f}\n"
        f"{stop_target_lines}{stop_line}{max_loss_line}{notional_line}{debit_line}{multiplier_line}"
        f"{evidence_lines}{candidate_lines}{account_assumption_lines}{portfolio_lines}{drawdown_lines}{quote_policy_lines}"
        "- Stop and target are planning references only; this packet does not place either exit order.\n\n"
        "## Exact review template\n"
        f"{review_template}\n\n"
        + (
            "## Exact option lookup template\n"
            f"{lookup_template}\n\n"
            if review.get("contract_lookup")
            else ""
        )
        +
        "## Mandatory sequence\n"
        "1. Use get_accounts and have the user choose or clearly identify the account. Never default an account. For every candidate, strip surrounding whitespace from the exact get_accounts.account_number, compute SHA-256 over the UTF-8 text 'optedge-robinhood-account-v1|' plus that trimmed account number, take the first 16 lowercase hexadecimal characters, and prefix them with 'acct_'. Require that derived account_key to exactly match an eligible account_key below; the last-four mask is display-only and is not a unique identity. Never print, return, or persist the raw account number.\n"
        "2. The chosen derived account_key must match both an eligible snapshot account and a blocker-free account-loss firewall row above, and both rows must show the same mask. Call get_portfolio for that exact account. Use total_value as live equity and the smaller of buying_power and unleveraged_buying_power as conservative buying power. Require live equity to match the packet's latest chained observation, the requested risk to remain within the displayed drawdown-adjusted ceiling, and the same account to be active, agentic_allowed, sufficiently funded, and options-approved when applicable.\n"
        f"   {live_risk_rule}\n"
        + (
            f"   STOP if planner equity exceeds live total_value by more than max($1, {max_equity_overstatement:.2%} of live total_value).\n"
            if max_equity_overstatement is not None
            else ""
        )
        + f"3. Perform the read-only preflight with: {preflight}. Read both equity and option positions and orders for the same account. For every paginated position/order response, follow each data.next/cursor link until it is null; for options, do the same for every chain and instrument lookup. STOP if any page, cursor linkage, or read fails. Fetch a fresh get_equity_quotes result for every held share symbol and a fresh get_option_quotes result for every held option_id from all position pages; apply the packet quote-age rules and STOP on any missing, stale, zero, crossed, or ambiguous mark. Block any short, ambiguous, pending assignment/exercise/expiration, missing mark, or nonterminal order. Also block a recent matching filled open-long or buy-to-open order until its corresponding position is visible; a lagging position feed is not permission to submit again. Recompute total open capital at risk using absolute share quantity x a conservative fresh share price and long-option quantity x 100 x max(fresh ask, fresh mark/current). If it differs from the packet or breaches the cap after this proposal, STOP and rebuild. If the same position exposure or logical working order already exists, STOP.\n"
        f"4. Resolve the exact active/tradable instrument. Require its underlying_type to exactly match the packet. {live_quote_rule} Validate the live instrument's minimum tick/tick-size rules before preview. If the packet limit is not valid, STOP and rebuild at an equal or lower valid buy limit; never round upward. If any field or timestamp is missing or the underlying type differs, STOP. If the live ask is above the packet limit, STOP and rebuild; never raise the limit.\n"
        f"5. Call {review_tool} FIRST with the review template. Never send a placeholder account number or option_id.\n"
        "6. Present the complete broker preview, compliance quote disclosure, alerts, fees, collateral, and estimated cost exactly as returned.\n"
        "7. Ask the user to confirm the exact reviewed account, instrument, side, quantity, type, and limit price.\n"
        "8. After that exact confirmation, immediately re-read every page of positions, open orders, portfolio, the exact instrument, and its live quote for the same account; for options, also repeat every page of the complete all-expiry-chain instrument proof. Refresh quotes for every held share symbol and option_id again. Recheck recent unmatched fills and recompute duplicate exposure, working-order, buying-power, drawdown, total-open-risk, tradability, tick, quote-age, positive/ordered bid-ask, spread, live ask <= packet limit, and standard-contract checks. Re-check that the packet has not expired. If expiry passed or any page/held-position quote is incomplete, stale, or ambiguous, or any relevant state differs or fails, STOP, rebuild, run a new broker preview, and obtain a new exact confirmation; do not place.\n"
        f"9. Only if that final re-read is unchanged and still passes every gate, immediately call {place_tool} once with unchanged reviewed fields and the exact packet-scoped ref_id {packet_ref_id}. Never generate a second ref_id for this logical order.\n"
        "10. Report the broker order ID and state. Submission is not a fill.\n\n"
        "## Hard prohibitions\n"
        "- No scheduled task, recurring Codex message, heartbeat, loop, batch, or automatic placement.\n"
        "- Never place or repeat an order without a new exact confirmation after review.\n"
        "- Never place if the mandatory post-confirmation positions, open-orders, or portfolio re-read changed relevant state.\n"
        "- Never place if the post-confirmation exact quote, instrument, option-chain proof, or packet-expiry check is missing, stale, changed, or blocked.\n"
        "- If placement outcome is uncertain, query current broker orders first; do not create another logical order.\n"
        f"- Any deliberate retry of this same logical order must reuse ref_id {packet_ref_id}; never auto-retry.\n"
        "- Never request, accept, print, or store passwords, tokens, API keys, MFA codes, cookies, or broker credentials.\n"
        "- Never change account, instrument, side, quantity, order type, or price between review and placement.\n"
        "- Never describe the planning stop as guaranteed or imply that this entry-only packet placed an exit order.\n"
    )


def render_manual_robinhood_review_prompt(
    packet: dict[str, Any],
    *,
    now: datetime | None = None,
) -> str:
    """Render a ready packet only after rechecking integrity and expiry."""
    if isinstance(packet, dict) and packet.get("status") == "manual_review_required":
        integrity = validate_manual_robinhood_review_packet(packet, now=now)
        if integrity.get("ok") is not True:
            error_lines = "\n".join(
                f"- {_prompt_text(row.get('code'))}: {_prompt_text(row.get('message'))}"
                for row in (integrity.get("errors") or [])
                if isinstance(row, dict)
            ) or "- Packet integrity or freshness could not be proven."
            return (
                "# Optedge Manual Robinhood Review\n\n"
                "STATUS: BLOCKED\n\n"
                "DO NOT CALL any Robinhood review or placement tool.\n"
                "Do not schedule, loop, repeat, or automate this packet.\n"
                "Rebuild it from the current Trade Desk and fresh broker state.\n\n"
                f"Packet validation errors:\n{error_lines}\n"
            )
    return _render_manual_robinhood_review_prompt_unchecked(packet)
