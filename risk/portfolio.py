"""Pure, fail-closed broker portfolio exposure controls.

The functions in this module only evaluate an already-normalized broker snapshot.
They never read files, contact a broker, or place/cancel an order.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

TERMINAL_ORDER_STATES = frozenset(
    {
        "cancelled",
        "canceled",
        "expired",
        "failed",
        "filled",
        "partially_filled_rest_cancelled",
        "rejected",
        "voided",
    }
)
PENDING_OPTION_POSITION_FIELDS = (
    "pending_buy_quantity",
    "pending_sell_quantity",
    "pending_exercise_quantity",
    "pending_assignment_quantity",
    "pending_expiration_quantity",
)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive_number(value: Any) -> float | None:
    number = _finite_number(value)
    return number if number is not None and number > 0 else None


def _money(value: float) -> float:
    return round(value + 0.0, 2)


def _ratio(value: float) -> float:
    return round(value + 0.0, 6)


def _asof_date(value: Any) -> date | None:
    if value is None:
        return datetime.now(UTC).date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            if len(text) == 10:
                return date.fromisoformat(text)
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def _row_quantity(
    row: Mapping[str, Any], fields: tuple[str, ...]
) -> tuple[float | None, str | None]:
    """Return one reconciled quantity, rejecting missing or contradictory fields."""
    observed: list[tuple[str, float]] = []
    invalid: list[str] = []
    for field in fields:
        if field not in row or row.get(field) is None or row.get(field) == "":
            continue
        number = _finite_number(row.get(field))
        if number is None:
            invalid.append(field)
        else:
            observed.append((field, number))
    if invalid:
        return None, f"invalid quantity field(s): {', '.join(invalid)}"
    if not observed:
        return None, "quantity is missing"
    reference = abs(observed[0][1])
    if any(not math.isclose(abs(number), reference, abs_tol=1e-9) for _, number in observed[1:]):
        return None, "quantity fields disagree"
    if any(number < 0 for _, number in observed):
        return -reference, None
    signed = next((number for field, number in observed if field == "signed_quantity"), None)
    return (signed if signed is not None else observed[0][1]), None


def _order_quantity(row: Mapping[str, Any]) -> tuple[float | None, str | None]:
    """Return the largest order quantity observed across total/pending fields."""
    values: list[float] = []
    invalid: list[str] = []
    for field in ("quantity", "pending_quantity"):
        if field not in row or row.get(field) is None or row.get(field) == "":
            continue
        number = _finite_number(row.get(field))
        if number is None:
            invalid.append(field)
        else:
            values.append(abs(number))
    if invalid:
        return None, f"invalid quantity field(s): {', '.join(invalid)}"
    if not values:
        return None, "quantity is missing"
    return max(values), None


def _account_scope(row: Mapping[str, Any]) -> str:
    return str(row.get("account_key") or "").strip()


def _position_label(asset: str, row: Mapping[str, Any], index: int) -> str:
    symbol = str(row.get("symbol") or row.get("chain_symbol") or "").strip().upper()
    return f"{asset} position {index + 1}" + (f" ({symbol})" if symbol else "")


def _append_blocker(blockers: list[str], message: str) -> None:
    if message not in blockers:
        blockers.append(message)


def _pending_option_position_error(row: Mapping[str, Any]) -> str | None:
    """Reject any unresolved option-position transition before exposure math."""
    pending: list[str] = []
    invalid: list[str] = []
    for field in PENDING_OPTION_POSITION_FIELDS:
        if field not in row or row.get(field) is None or row.get(field) == "":
            continue
        value = _finite_number(row.get(field))
        if value is None:
            invalid.append(field)
        elif not math.isclose(value, 0.0, abs_tol=1e-12):
            pending.append(field)
    if invalid:
        return f"invalid pending quantity field(s): {', '.join(invalid)}"
    if pending:
        return f"unresolved pending position transition(s): {', '.join(pending)}"
    return None


def summarize_broker_account_capital_at_risk(
    snapshot: Mapping[str, Any] | Any,
    account_key: str,
    asof: date | datetime | str | None = None,
) -> dict[str, Any]:
    """Conservatively summarize open long exposure for exactly one broker account.

    A usable result requires a complete, normalized snapshot with exact account
    scoping. Any short/ambiguous position, stale nonzero expired option, missing
    option mark, non-standard option multiplier, or same-account working order
    blocks the result instead of estimating through uncertainty.
    """
    blockers: list[str] = []
    asof_day = _asof_date(asof)
    clean_account_key = str(account_key or "").strip()
    if asof_day is None:
        blockers.append("asof must be a valid date or ISO-8601 datetime")
    if not clean_account_key:
        blockers.append("account_key is required")
    if not isinstance(snapshot, Mapping):
        blockers.append("broker snapshot must be a mapping")
        snapshot = {}

    normalization_blockers = snapshot.get("normalization_blockers")
    if normalization_blockers:
        blockers.append("broker snapshot has unresolved normalization blockers")

    accounts = snapshot.get("accounts")
    if not isinstance(accounts, list):
        blockers.append("broker snapshot accounts are missing")
        accounts = []
    matching_accounts = [
        row
        for row in accounts
        if isinstance(row, Mapping) and _account_scope(row) == clean_account_key
    ]
    if clean_account_key and len(matching_accounts) != 1:
        blockers.append("account_key must identify exactly one normalized broker account")

    collections: dict[str, list[Any]] = {}
    for field in ("option_positions", "equity_positions", "option_orders", "equity_orders"):
        rows = snapshot.get(field)
        if not isinstance(rows, list):
            blockers.append(f"broker snapshot {field} are missing")
            rows = []
        collections[field] = rows

    # Any nonterminal order may change exposure between calculation and review.
    working_order_count = 0
    for asset, field in (("option", "option_orders"), ("equity", "equity_orders")):
        for index, raw_row in enumerate(collections[field]):
            if not isinstance(raw_row, Mapping):
                _append_blocker(blockers, f"{asset} order {index + 1} is malformed and unscoped")
                continue
            state = str(raw_row.get("state") or raw_row.get("status") or "").strip().lower()
            if state in TERMINAL_ORDER_STATES:
                continue
            scope = _account_scope(raw_row)
            if scope and scope != clean_account_key:
                continue
            quantity, quantity_error = _order_quantity(raw_row)
            if quantity_error:
                _append_blocker(
                    blockers,
                    f"{asset} order {index + 1} is ambiguous: {quantity_error}",
                )
                continue
            if quantity is not None and math.isclose(quantity, 0.0, abs_tol=1e-12):
                continue
            if not scope:
                _append_blocker(
                    blockers,
                    f"nonterminal {asset} order {index + 1} is not account-scoped",
                )
                continue
            working_order_count += 1
            _append_blocker(
                blockers,
                f"same-account nonterminal {asset} order {index + 1} must resolve before exposure is recomputed",
            )

    option_total = 0.0
    equity_total = 0.0
    option_count = 0
    equity_count = 0
    position_rows: list[dict[str, Any]] = []

    for index, raw_row in enumerate(collections["option_positions"]):
        if not isinstance(raw_row, Mapping):
            _append_blocker(blockers, f"option position {index + 1} is malformed and unscoped")
            continue
        scope = _account_scope(raw_row)
        if scope and scope != clean_account_key:
            continue
        pending_error = _pending_option_position_error(raw_row)
        if pending_error:
            _append_blocker(
                blockers,
                f"option position {index + 1} is ambiguous: {pending_error}",
            )
            continue
        quantity, quantity_error = _row_quantity(
            raw_row, ("signed_quantity", "quantity", "contracts", "qty")
        )
        label = _position_label("option", raw_row, index)
        if quantity_error:
            _append_blocker(blockers, f"{label} is ambiguous: {quantity_error}")
            continue
        assert quantity is not None
        if math.isclose(quantity, 0.0, abs_tol=1e-12):
            continue
        if not scope:
            _append_blocker(blockers, f"{label} is not account-scoped")
            continue

        position_type = str(raw_row.get("position_type") or "").strip().lower()
        if quantity < 0 or position_type == "short":
            _append_blocker(blockers, f"{label} is short; maximum loss is not bounded by premium")
            continue
        if position_type != "long":
            _append_blocker(blockers, f"{label} has ambiguous long/short direction")
            continue
        if not math.isclose(quantity, round(quantity), abs_tol=1e-9):
            _append_blocker(blockers, f"{label} has a non-whole contract quantity")
            continue

        symbol = str(raw_row.get("symbol") or raw_row.get("chain_symbol") or "").strip().upper()
        option_type = str(raw_row.get("option_type") or raw_row.get("side") or "").strip().lower()
        strike = _positive_number(raw_row.get("strike_price") or raw_row.get("strike"))
        if not symbol or option_type not in {"call", "put"} or strike is None:
            _append_blocker(blockers, f"{label} lacks exact option contract identity")
            continue

        expiry_text = str(raw_row.get("expiration_date") or raw_row.get("expiry") or "").strip()
        try:
            expiry = date.fromisoformat(expiry_text) if len(expiry_text) == 10 else None
        except ValueError:
            expiry = None
        if expiry is None or expiry.isoformat() != expiry_text:
            _append_blocker(blockers, f"{label} has an invalid expiration date")
            continue
        if asof_day is not None and expiry < asof_day:
            _append_blocker(blockers, f"{label} is expired but still has nonzero broker quantity")
            continue

        multiplier = _finite_number(
            raw_row.get("trade_value_multiplier")
            if raw_row.get("trade_value_multiplier") is not None
            else raw_row.get("multiplier")
        )
        if multiplier is None or not math.isclose(multiplier, 100.0, abs_tol=1e-9):
            _append_blocker(blockers, f"{label} does not prove the standard 100-share multiplier")
            continue

        mark_candidates = [
            number
            for number in (
                _positive_number(raw_row.get("mark_price")),
                _positive_number(raw_row.get("current_price")),
            )
            if number is not None
        ]
        if not mark_candidates:
            _append_blocker(blockers, f"{label} is missing a valid current mark")
            continue
        ask = _positive_number(raw_row.get("ask_price") or raw_row.get("ask"))
        conservative_price = max([*mark_candidates, *([ask] if ask is not None else [])])
        exposure = abs(quantity) * 100.0 * conservative_price
        option_total += exposure
        option_count += 1
        position_rows.append(
            {
                "asset": "option",
                "symbol": symbol,
                "option_type": option_type,
                "strike_price": strike,
                "expiration_date": expiry_text,
                "quantity": abs(quantity),
                "price_basis": "max_valid_ask_mark_or_current",
                "conservative_price": _money(conservative_price),
                "capital_at_risk_dollars": _money(exposure),
            }
        )

    for index, raw_row in enumerate(collections["equity_positions"]):
        if not isinstance(raw_row, Mapping):
            _append_blocker(blockers, f"equity position {index + 1} is malformed and unscoped")
            continue
        scope = _account_scope(raw_row)
        if scope and scope != clean_account_key:
            continue
        quantity, quantity_error = _row_quantity(
            raw_row, ("signed_quantity", "quantity", "shares", "qty")
        )
        label = _position_label("equity", raw_row, index)
        if quantity_error:
            _append_blocker(blockers, f"{label} is ambiguous: {quantity_error}")
            continue
        assert quantity is not None
        if math.isclose(quantity, 0.0, abs_tol=1e-12):
            continue
        if not scope:
            _append_blocker(blockers, f"{label} is not account-scoped")
            continue

        position_type = str(raw_row.get("position_type") or "").strip().lower()
        if quantity < 0 or position_type in {"short", "boxed"}:
            _append_blocker(blockers, f"{label} is short or boxed; maximum loss is ambiguous")
            continue
        if position_type != "long":
            _append_blocker(blockers, f"{label} has ambiguous long/short direction")
            continue
        symbol = str(raw_row.get("symbol") or "").strip().upper()
        if not symbol:
            _append_blocker(blockers, f"{label} is missing its symbol")
            continue

        market_value = _finite_number(raw_row.get("market_value"))
        market_exposure = (
            abs(market_value)
            if market_value is not None and not math.isclose(market_value, 0.0, abs_tol=1e-12)
            else None
        )
        price_candidates = [
            number
            for number in (
                _positive_number(raw_row.get("mark_price")),
                _positive_number(raw_row.get("current_price")),
                _positive_number(raw_row.get("last_price")),
            )
            if number is not None
        ]
        conservative_price = max(price_candidates) if price_candidates else None
        quantity_exposure = (
            abs(quantity) * conservative_price if conservative_price is not None else None
        )
        if market_exposure is None and quantity_exposure is None:
            _append_blocker(blockers, f"{label} is missing market value and a valid current price")
            continue
        if market_exposure is not None and quantity_exposure is not None:
            reconciliation_tolerance = max(
                1.0,
                max(market_exposure, quantity_exposure) * 0.05,
            )
            if abs(market_exposure - quantity_exposure) > reconciliation_tolerance:
                _append_blocker(
                    blockers,
                    f"{label} market value does not reconcile with quantity times current price",
                )
                continue
            exposure = max(market_exposure, quantity_exposure)
            conservative_price = exposure / abs(quantity)
            price_basis = "max_reconciled_market_value_or_quantity_times_current_price"
        elif market_exposure is not None:
            exposure = market_exposure
            conservative_price = exposure / abs(quantity)
            price_basis = "absolute_market_value"
        else:
            assert quantity_exposure is not None and conservative_price is not None
            exposure = quantity_exposure
            price_basis = "absolute_quantity_times_current_price"
        equity_total += exposure
        equity_count += 1
        position_rows.append(
            {
                "asset": "equity",
                "symbol": symbol,
                "quantity": abs(quantity),
                "price_basis": price_basis,
                "conservative_price": _money(conservative_price),
                "capital_at_risk_dollars": _money(exposure),
            }
        )

    observed_total = option_total + equity_total
    eligible = not blockers
    return {
        "schema": "optedge_broker_portfolio_exposure_v1",
        "status": "ready" if eligible else "blocked",
        "eligible": eligible,
        "account_key": clean_account_key or None,
        "asof": asof_day.isoformat() if asof_day is not None else None,
        "capital_at_risk_dollars": _money(observed_total) if eligible else None,
        "observed_capital_at_risk_dollars": _money(observed_total),
        "option_capital_at_risk_dollars": _money(option_total),
        "equity_capital_at_risk_dollars": _money(equity_total),
        "position_count": option_count + equity_count,
        "option_position_count": option_count,
        "equity_position_count": equity_count,
        "same_account_nonterminal_order_count": working_order_count,
        "positions": position_rows,
        "blockers": blockers,
        "methodology": {
            "scope": "same_account_normalized_broker_rows_only",
            "long_option": "quantity x 100 x max(valid ask, mark/current)",
            "long_equity": (
                "max(reconciled abs market value, abs quantity x current price); "
                "material disagreement blocks"
            ),
            "working_orders": "any nonzero same-account nonterminal order blocks",
        },
    }


def _trade_plan_capital_at_risk(trade_plan: Mapping[str, Any]) -> tuple[float | None, str | None]:
    errors = trade_plan.get("errors")
    if errors:
        return None, "trade_plan has validation errors"
    validation = (
        trade_plan.get("validation") if isinstance(trade_plan.get("validation"), Mapping) else {}
    )
    if validation.get("ok") is not True or validation.get("errors"):
        return None, "trade_plan has validation errors"
    if trade_plan.get("ready") is False or trade_plan.get("valid") is False:
        return None, "trade_plan is not ready"
    if trade_plan.get("is_actionable") is not True:
        return None, "trade_plan is not actionable"
    status = str(trade_plan.get("status") or "").strip().lower()
    if status not in {"ready", "ready_for_manual_review"}:
        return None, "trade_plan is not ready for manual review"
    risk = trade_plan.get("risk") if isinstance(trade_plan.get("risk"), Mapping) else {}
    order = trade_plan.get("order") if isinstance(trade_plan.get("order"), Mapping) else {}
    if risk.get("max_loss_is_unbounded") is not False:
        return None, "trade_plan maximum loss is unbounded"

    inputs = trade_plan.get("inputs") if isinstance(trade_plan.get("inputs"), Mapping) else {}
    asset = (
        str(trade_plan.get("asset") or order.get("asset") or inputs.get("asset")).strip().lower()
    )
    if asset in {"share", "shares", "equity", "stock"}:
        candidates = (
            risk.get("full_share_notional_at_risk_dollars"),
            order.get("estimated_notional_dollars"),
        )
    elif asset in {"option", "options", "long_option"}:
        candidates = (
            order.get("estimated_debit_dollars"),
            risk.get("planned_max_loss_dollars"),
            risk.get("full_option_debit_at_risk_dollars"),
        )
    else:
        # Valid plans still expose a conservative maximum-loss/notional field.
        candidates = (
            risk.get("full_share_notional_at_risk_dollars"),
            order.get("estimated_debit_dollars"),
            order.get("estimated_notional_dollars"),
            risk.get("planned_max_loss_dollars"),
        )
    values = [_positive_number(value) for value in candidates]
    values = [value for value in values if value is not None]
    if not values:
        return None, "trade_plan does not contain positive capital at risk"
    if any(not math.isclose(value, values[0], abs_tol=0.011) for value in values[1:]):
        return None, "trade_plan capital-at-risk fields do not reconcile"
    return values[0], None


def evaluate_post_trade_portfolio(
    exposure_summary: Mapping[str, Any] | Any,
    trade_plan: Mapping[str, Any] | None = None,
    *,
    proposed_capital_at_risk: float | None = None,
    assumed_equity: float | None,
    live_equity: float | None,
    allocation_fraction: float | None,
) -> dict[str, Any]:
    """Evaluate a proposed long trade against the total-open allocation cap.

    Pass either a validated ``trade_plan`` or an explicit
    ``proposed_capital_at_risk``. The cap always uses the lower of assumed and
    live same-account equity so the review attestation cannot inflate capacity.
    """
    blockers: list[str] = []
    if not isinstance(exposure_summary, Mapping):
        blockers.append("exposure_summary must be a mapping")
        exposure_summary = {}
    if exposure_summary.get("status") != "ready" or exposure_summary.get("eligible") is not True:
        blockers.append("broker exposure summary is blocked")
    existing = _finite_number(exposure_summary.get("capital_at_risk_dollars"))
    if existing is None or existing < 0:
        blockers.append("broker exposure summary lacks usable capital at risk")

    if trade_plan is not None and proposed_capital_at_risk is not None:
        blockers.append("provide either trade_plan or proposed_capital_at_risk, not both")
        proposed = None
    elif trade_plan is not None:
        if not isinstance(trade_plan, Mapping):
            blockers.append("trade_plan must be a mapping")
            proposed = None
        else:
            proposed, plan_error = _trade_plan_capital_at_risk(trade_plan)
            if plan_error:
                blockers.append(plan_error)
    else:
        proposed = _positive_number(proposed_capital_at_risk)
        if proposed is None:
            blockers.append("proposed_capital_at_risk must be positive and finite")

    assumed = _positive_number(assumed_equity)
    live = _positive_number(live_equity)
    allocation = _positive_number(allocation_fraction)
    if assumed is None:
        blockers.append("assumed_equity must be positive and finite")
    if live is None:
        blockers.append("live_equity must be positive and finite")
    if allocation is None or allocation > 1:
        blockers.append("allocation_fraction must be greater than 0 and no greater than 1")

    equity_basis = min(assumed, live) if assumed is not None and live is not None else None
    allocation_cap = (
        equity_basis * allocation
        if equity_basis is not None and allocation is not None and allocation <= 1
        else None
    )
    post_trade = existing + proposed if existing is not None and proposed is not None else None
    headroom_before = (
        allocation_cap - existing if allocation_cap is not None and existing is not None else None
    )
    headroom_after = (
        allocation_cap - post_trade
        if allocation_cap is not None and post_trade is not None
        else None
    )
    if (
        allocation_cap is not None
        and post_trade is not None
        and post_trade > allocation_cap + 0.005
    ):
        blockers.append("post-trade broker capital at risk exceeds the total-open allocation cap")

    allowed = not blockers
    return {
        "schema": "optedge_post_trade_portfolio_gate_v1",
        "status": "allowed" if allowed else "blocked",
        "allowed": allowed,
        "account_key": exposure_summary.get("account_key"),
        "asof": exposure_summary.get("asof"),
        "exposure_schema": exposure_summary.get("schema"),
        "position_count": exposure_summary.get("position_count"),
        "same_account_nonterminal_order_count": exposure_summary.get(
            "same_account_nonterminal_order_count"
        ),
        "equity_basis_method": "min_assumed_and_live_same_account_equity",
        "assumed_equity_dollars": _money(assumed) if assumed is not None else None,
        "live_equity_dollars": _money(live) if live is not None else None,
        "equity_basis_dollars": _money(equity_basis) if equity_basis is not None else None,
        "allocation_fraction": _ratio(allocation) if allocation is not None else None,
        "allocation_cap_dollars": _money(allocation_cap) if allocation_cap is not None else None,
        "current_capital_at_risk_dollars": _money(existing) if existing is not None else None,
        "proposed_capital_at_risk_dollars": _money(proposed) if proposed is not None else None,
        "post_trade_capital_at_risk_dollars": _money(post_trade)
        if post_trade is not None
        else None,
        "headroom_before_trade_dollars": _money(headroom_before)
        if headroom_before is not None
        else None,
        "headroom_after_trade_dollars": _money(headroom_after)
        if headroom_after is not None
        else None,
        "utilization_before": (
            _ratio(existing / allocation_cap)
            if existing is not None and allocation_cap is not None and allocation_cap > 0
            else None
        ),
        "utilization_after": (
            _ratio(post_trade / allocation_cap)
            if post_trade is not None and allocation_cap is not None and allocation_cap > 0
            else None
        ),
        "blockers": blockers,
    }
