"""Pure trade sizing and approval-gated Robinhood review packets.

This module deliberately has no filesystem, network, broker, credential, clock,
or automation dependency.  It turns explicit risk inputs into a deterministic
whole-unit trade plan, then converts an actionable plan into manual Robinhood
MCP review instructions.  A review packet never places an order.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any

TRADE_PLAN_SCHEMA = "optedge_trade_plan_v1"
ACCOUNT_LIMITS_SCHEMA = "optedge_account_limits_v1"
ROBINHOOD_EQUITY_REVIEW_SCHEMA = "optedge_robinhood_equity_review_plan_v1"
ROBINHOOD_OPTION_REVIEW_SCHEMA = "optedge_robinhood_option_review_plan_v1"
MANUAL_REVIEW_PACKET_SCHEMA = "optedge_manual_robinhood_review_packet_v1"

ACCOUNT_NUMBER_PLACEHOLDER = "<explicit_user_confirmed_account_number>"
OPTION_ID_PLACEHOLDER = "<option_id_from_get_option_instruments>"
REF_ID_PLACEHOLDER = "<fresh_uuid_generated_once_after_exact_confirmation>"
SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")

__all__ = [
    "calculate_account_limits",
    "size_share_trade",
    "size_long_option_trade",
    "build_robinhood_equity_review_plan",
    "build_robinhood_option_review_plan",
    "build_manual_robinhood_review_packet",
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

    if risk is not None and risk > 1:
        errors.append(_issue("risk_fraction_above_one", "risk_fraction", "risk_fraction cannot exceed 1.0."))
    elif risk is not None and risk > 0.02:
        warnings.append(
            _issue(
                "elevated_risk_fraction",
                "risk_fraction",
                "Risk per trade is above 2% of account equity; verify this aggressive setting deliberately.",
            )
        )
    if allocation is not None and allocation > 1:
        errors.append(
            _issue("allocation_fraction_above_one", "allocation_fraction", "allocation_fraction cannot exceed 1.0.")
        )
    elif allocation is not None and allocation > 0.25:
        warnings.append(
            _issue(
                "concentrated_allocation_fraction",
                "allocation_fraction",
                "Maximum allocation is above 25% of account equity; review concentration risk.",
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
    if not trade_plan.get("is_actionable"):
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
        allocation_cap = _number(risk.get("allocation_cap_dollars"))
        if allocation_cap is None or expected_notional > allocation_cap + 0.01:
            errors.append(
                _issue(
                    "equity_notional_exceeds_allocation_cap",
                    "allocation_cap_dollars",
                    "Share notional must fit inside the allocation cap.",
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
            "Use a limit order only; never change the reviewed fields during placement.",
            "Do not schedule, repeat, or automatically retry this order.",
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
                "Manual Robinhood review supports standard 100x contracts only; adjusted deliverables require a fresh broker-derived plan.",
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
            "Resolve the exact option_id through get_option_chains and get_option_instruments.",
            "Require the live get_option_instruments underlying_type to exactly match the packet; stop on any mismatch.",
            "Verify the chain trade_value_multiplier matches the local contract multiplier; rebuild the plan if it differs.",
            "Call review_option_order first and show quotes, fees, collateral, and every broker alert verbatim.",
            "Ask the user to confirm the exact reviewed account, contract, side, quantity, type, and price.",
            "Do not call place_option_order without that exact confirmation.",
            "Use a single-leg buy-to-open limit order only; never change reviewed fields during placement.",
            "Do not schedule, repeat, or automatically retry this order.",
        ],
        "validation": _validation([], []),
    }


def _packet_id(trade_plan: dict[str, Any], snapshot_id: str | None) -> str:
    canonical = json.dumps(
        {"snapshot_id": snapshot_id, "trade_plan": trade_plan},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "manual-review-" + hashlib.sha256(canonical).hexdigest()[:16]


def build_manual_robinhood_review_packet(
    trade_plan: dict[str, Any],
    *,
    snapshot_id: str | None = None,
    issued_at: str | None = None,
    expires_at: str | None = None,
    external_blockers: list[str] | None = None,
) -> dict[str, Any]:
    """Package a trade plan for one manual, approval-gated broker review."""
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

    blocker_messages = [
        _prompt_text(value)
        for value in (external_blockers or [])
        if _prompt_text(value)
    ]
    if blocker_messages:
        review_plan = _blocked_review_plan(
            str(review_plan.get("schema") or "optedge_robinhood_blocked_review_plan_v1"),
            str(asset or "unknown"),
            str(review_plan.get("review_tool") or "unknown"),
            str(review_plan.get("place_tool_after_explicit_confirmation") or "unknown"),
            [
                _issue("external_review_gate_blocked", "review_gate", message)
                for message in blocker_messages
            ],
        )

    order = trade_plan.get("order") if isinstance(trade_plan, dict) and isinstance(trade_plan.get("order"), dict) else {}
    risk = trade_plan.get("risk") if isinstance(trade_plan, dict) and isinstance(trade_plan.get("risk"), dict) else {}
    assumptions = (
        trade_plan.get("account_assumptions")
        if isinstance(trade_plan, dict) and isinstance(trade_plan.get("account_assumptions"), dict)
        else {}
    )
    review_constraints = (
        trade_plan.get("review_constraints")
        if isinstance(trade_plan, dict) and isinstance(trade_plan.get("review_constraints"), dict)
        else {}
    )
    ready = bool(review_plan.get("review_allowed"))
    packet = {
        "schema": MANUAL_REVIEW_PACKET_SCHEMA,
        "packet_id": _packet_id(trade_plan if isinstance(trade_plan, dict) else {}, snapshot_id),
        "snapshot_id": snapshot_id,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "broker": "robinhood",
        "status": "manual_review_required" if ready else "blocked",
        "does_not_place_orders": True,
        "automation_allowed": False,
        "repeat_orders_allowed": False,
        "contains_credentials": False,
        "requires_explicit_user_confirmation": True,
        "external_review_gate_blockers": blocker_messages,
        "trade_plan": trade_plan,
        "review_constraints": review_constraints,
        "review_plan": review_plan,
        "confirmation_summary": {
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
            "full_share_notional_at_risk_dollars": risk.get("full_share_notional_at_risk_dollars"),
            "full_option_debit_at_risk_dollars": risk.get("full_option_debit_at_risk_dollars"),
            "max_loss_is_unbounded": bool(risk.get("max_loss_is_unbounded")),
            "stop_is_not_broker_order": bool(risk.get("stop_is_not_broker_order", True)),
        },
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
            "limit_price_may_increase": False,
        },
    }
    packet["prompt"] = render_manual_robinhood_review_prompt(packet)
    return packet


def render_manual_robinhood_review_prompt(packet: dict[str, Any]) -> str:
    """Render strict instructions for one manual Codex/Robinhood review."""
    review = packet.get("review_plan") if isinstance(packet.get("review_plan"), dict) else {}
    summary = packet.get("confirmation_summary") if isinstance(packet.get("confirmation_summary"), dict) else {}
    constraints = packet.get("review_constraints") if isinstance(packet.get("review_constraints"), dict) else {}
    account_constraints = constraints.get("account") if isinstance(constraints.get("account"), dict) else {}
    quote_constraints = constraints.get("quote") if isinstance(constraints.get("quote"), dict) else {}
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
        f"- Allocation fraction: {allocation_fraction:.2%}\n"
        if assumed_equity is not None and risk_fraction is not None and allocation_fraction is not None
        else ""
    )
    quote_policy_lines = (
        f"- Live quote maximum age: {max_quote_age_seconds} seconds\n"
        f"- Maximum live bid/ask spread: {max_spread_fraction:.2%}\n"
        "- Bid and ask must both be positive; ask must be at least bid; the packet limit may never increase\n"
        if max_quote_age_seconds > 0 and max_spread_fraction is not None
        else ""
    )
    asset = _prompt_text(review.get("asset"))
    if asset == "option":
        live_risk_rule = (
            "For the chosen account, require full option debit <= total_value x risk_fraction, "
            "full option debit <= total_value x allocation_fraction, and full option debit <= conservative buying power."
        )
        live_quote_rule = (
            "Call get_option_quotes for the resolved option_id. Require quote.updated_at no older than the packet's "
            "maximum quote age, bid_price > 0, ask_price >= bid_price, and "
            "(ask_price - bid_price) / ((ask_price + bid_price) / 2) <= the packet spread cap."
        )
    else:
        live_risk_rule = (
            "For the chosen account, require planned stop loss <= total_value x risk_fraction, full share notional "
            "<= total_value x allocation_fraction, and order notional <= conservative buying power."
        )
        live_quote_rule = (
            "Call get_equity_quotes for the exact symbol. Require venue_bid_time and venue_ask_time no older than the "
            "packet's maximum quote age, bid_price > 0, ask_price >= bid_price, and "
            "(ask_price - bid_price) / ((ask_price + bid_price) / 2) <= the packet spread cap."
        )
    packet_id = _prompt_text(packet.get("packet_id"))
    issued_at = _prompt_text(packet.get("issued_at") or "not recorded")
    expires_at = _prompt_text(packet.get("expires_at") or "not recorded")
    review_template = json.dumps(review.get("review_arguments_template"), indent=2, sort_keys=True)
    lookup_template = json.dumps(review.get("contract_lookup"), indent=2, sort_keys=True)
    return (
        "# Optedge Manual Robinhood Review\n\n"
        "MANUAL, ONE-ORDER WORKFLOW ONLY. This packet never authorizes automation.\n\n"
        "## Packet identity\n"
        f"- Packet: {packet_id}\n"
        f"- Issued: {issued_at}\n"
        f"- Expires: {expires_at}\n"
        "- If the expiry is missing or has passed, stop. Recalculate from a fresh Optedge and broker snapshot.\n\n"
        "## Exact local plan\n"
        f"- Instrument: {order_label}\n"
        f"- Intent: {_prompt_text(summary.get('intent'))}\n"
        f"- Side: {_prompt_text(summary.get('side'))}\n"
        f"- Quantity: {_prompt_text(summary.get('quantity'))}\n"
        f"- Entry order: {_prompt_text(summary.get('order_type'))} at ${float(summary.get('limit_price')):.2f}\n"
        f"{stop_target_lines}{stop_line}{max_loss_line}{notional_line}{debit_line}{multiplier_line}"
        f"{account_assumption_lines}{quote_policy_lines}"
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
        "1. Use get_accounts and have the user choose or clearly identify the account. Never default an account.\n"
        "2. Call get_portfolio for that exact account. Use total_value as live equity and the smaller of buying_power and unleveraged_buying_power as conservative buying power. Require the same account to be active, agentic_allowed, sufficiently funded, and options-approved when applicable.\n"
        f"   {live_risk_rule}\n"
        + (
            f"   STOP if planner equity exceeds live total_value by more than max($1, {max_equity_overstatement:.2%} of live total_value).\n"
            if max_equity_overstatement is not None
            else ""
        )
        + f"3. Perform the read-only preflight with: {preflight}. If the same position exposure or logical working order already exists, STOP and do not review or place another order.\n"
        f"4. Resolve the exact active/tradable instrument. Require its underlying_type to exactly match the packet. {live_quote_rule} If any field or timestamp is missing or the underlying type differs, STOP. If the live ask is above the packet limit, STOP and rebuild; never raise the limit.\n"
        f"5. Call {review_tool} FIRST with the review template. Never send a placeholder account number or option_id.\n"
        "6. Present the complete broker preview, compliance quote disclosure, alerts, fees, collateral, and estimated cost exactly as returned.\n"
        "7. Ask the user to confirm the exact reviewed account, instrument, side, quantity, type, and limit price.\n"
        f"8. Only after that confirmation, call {place_tool} once with unchanged reviewed fields and one fresh ref_id.\n"
        "9. Report the broker order ID and state. Submission is not a fill.\n\n"
        "## Hard prohibitions\n"
        "- No scheduled task, recurring Codex message, heartbeat, loop, batch, or automatic placement.\n"
        "- Never place or repeat an order without a new exact confirmation after review.\n"
        "- If placement outcome is uncertain, query current broker orders first; do not create another logical order.\n"
        "- Never request, accept, print, or store passwords, tokens, API keys, MFA codes, cookies, or broker credentials.\n"
        "- Never change account, instrument, side, quantity, order type, or price between review and placement.\n"
        "- Never describe the planning stop as guaranteed or imply that this entry-only packet placed an exit order.\n"
    )
