# Purpose: Preview and place one exact Robinhood option order after two explicit clicks.
"""Single-use Robinhood option execution capability.

This is deliberately not an autopilot.  It can preview one fully gated Optedge
finalist and issue one short-lived, in-memory confirmation token.  Placement
consumes that token before making exactly one fixed ``place_option_order`` call;
there is no retry, polling loop, scheduler, or persisted account identifier.
"""

from __future__ import annotations

import hashlib
import math
import re
import secrets
import threading
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from optedge.robinhood_finalist import (
    RobinhoodFinalistCheckError,
    canonical_digest,
    check_best_option_finalist,
)
from optedge.robinhood_mcp import sanitize_public_data

CONFIRMATION_TTL_SECONDS = 60
MAX_ACCOUNT_RISK_FRACTION = 0.01
MAX_OPTION_QUOTE_AGE_SECONDS = 120
MAX_AUTOMATED_EXIT_SPREAD_FRACTION = 0.15
AUTOMATED_TAKE_PROFIT_FRACTION = 0.35
AUTOMATED_HARD_LOSS_FRACTION = -0.35
AUTOMATED_EXPIRY_EXIT_DAYS = 7
AUTOMATION_AUTHORIZATION_TEXT = "ARMED_GUARDED_AUTOMATION"
TERMINAL_ORDER_STATES = frozenset(
    {"cancelled", "canceled", "filled", "rejected", "failed", "expired", "voided"}
)


class RobinhoodOptionExecutionError(RuntimeError):
    """Safe categorical failure for the local execution desk."""

    def __init__(self, code: str) -> None:
        safe = "".join(char if char.isalnum() or char == "_" else "_" for char in str(code).lower())
        self.code = safe.strip("_") or "option_execution_failed"
        super().__init__(self.code)


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise RobinhoodOptionExecutionError("naive_execution_clock")
    return current.astimezone(UTC)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _truth(value: Any) -> bool:
    return value is True


def _data(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not isinstance(value.get("data"), Mapping):
        raise RobinhoodOptionExecutionError("broker_result_invalid")
    return dict(value["data"])


def _rows(value: Any, *keys: str) -> list[dict[str, Any]]:
    data = _data(value)
    for key in keys:
        raw = data.get(key)
        if isinstance(raw, list):
            return [dict(row) for row in raw if isinstance(row, Mapping)]
    return []


def _account_key(account_number: str) -> str:
    digest = hashlib.sha256(
        f"optedge-robinhood-account-v1|{account_number.strip()}".encode()
    ).hexdigest()
    return f"acct_{digest[:16]}"


def _masked_account(account_number: str) -> str:
    clean = account_number.strip()
    return f"••••{clean[-4:]}" if len(clean) >= 4 else "••••"


def _option_level(account: Mapping[str, Any]) -> int:
    for field in ("options_level", "option_trading_level", "option_level"):
        raw = account.get(field)
        value = _number(raw)
        if value is not None:
            return int(value)
        match = re.fullmatch(r"option_level_([0-9]+)", _text(raw).lower())
        if match:
            return int(match.group(1))
    return 0


def _portfolio_values(value: Any) -> dict[str, Any]:
    data = _data(value)
    portfolio = data.get("portfolio")
    return dict(portfolio) if isinstance(portfolio, Mapping) else data


def _portfolio_number(portfolio: Mapping[str, Any], field: str) -> float | None:
    """Read current Robinhood portfolio numbers across flat and nested schemas."""
    raw = portfolio.get(field)
    if isinstance(raw, Mapping):
        raw = raw.get(field) or raw.get("amount") or raw.get("value")
    return _number(raw)


def _portfolio_capacity(portfolio: Mapping[str, Any]) -> tuple[float | None, float | None]:
    total_value = _portfolio_number(portfolio, "total_value") or _portfolio_number(
        portfolio, "equity"
    )
    nested = portfolio.get("buying_power")
    nested = dict(nested) if isinstance(nested, Mapping) else {}
    buying_power = _number(nested.get("buying_power")) or _portfolio_number(
        portfolio, "buying_power"
    )
    unleveraged = _number(nested.get("unleveraged_buying_power")) or _portfolio_number(
        portfolio, "unleveraged_buying_power"
    )
    available_values = [value for value in (buying_power, unleveraged) if value is not None]
    return total_value, min(available_values) if available_values else None


def _parse_timestamp(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _identifier_arguments(schema: Mapping[str, Any], option_id: str) -> dict[str, Any]:
    properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
    for field in ("option_ids", "instrument_ids", "ids"):
        if field in properties:
            return {field: [option_id]}
    for field in ("option_id", "instrument_id", "id"):
        if field in properties:
            return {field: option_id}
    raise RobinhoodOptionExecutionError("option_quote_schema_unsupported")


def _position_option_id(row: Mapping[str, Any]) -> str:
    return _text(row.get("option_id") or row.get("instrument_id") or row.get("id"))


def _position_symbol(row: Mapping[str, Any]) -> str:
    return _text(row.get("chain_symbol") or row.get("symbol") or row.get("ticker")).upper()


def _position_quantity(row: Mapping[str, Any]) -> float | None:
    return _number(row.get("quantity"))


def _pending_option_quantity(row: Mapping[str, Any]) -> float:
    return sum(
        abs(_number(row.get(field)) or 0.0)
        for field in (
            "pending_buy_quantity",
            "pending_sell_quantity",
            "pending_exercise_quantity",
            "pending_assignment_quantity",
            "pending_expiration_quantity",
        )
    )


def _quote_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    nested = row.get("quote")
    return dict(nested) if isinstance(nested, Mapping) else dict(row)


def _has_more(value: Any) -> bool:
    data = _data(value)
    return data.get("next") not in (None, "") or data.get("next_cursor") not in (None, "")


def _order_state(row: Mapping[str, Any]) -> str:
    return _text(row.get("state") or row.get("status")).lower()


class RobinhoodOptionExecutionService:
    """Keep account choices and confirmation capabilities only in process memory."""

    def __init__(self, manager: Any, *, data_dir: Path) -> None:
        self.manager = manager
        self.data_dir = Path(data_dir)
        self._lock = threading.RLock()
        self._accounts: dict[str, str] = {}
        self._confirmations: dict[str, dict[str, Any]] = {}

    def clear(self) -> None:
        """Discard every ephemeral account mapping and confirmation capability."""
        with self._lock:
            self._accounts.clear()
            self._confirmations.clear()

    def account_choices(self) -> dict[str, Any]:
        result = self.manager.call_read_tool("get_accounts", {}, timeout_seconds=12)
        accounts = _rows(result, "accounts", "results")
        choices: list[dict[str, Any]] = []
        ephemeral: dict[str, str] = {}
        for account in accounts:
            number = _text(
                account.get("account_number")
                or account.get("rhs_account_number")
                or account.get("brokerage_account_number")
            )
            if not number:
                continue
            key = _account_key(number)
            ephemeral[key] = number
            agentic = _truth(account.get("agentic_allowed"))
            active = account.get("active") is not False and _text(account.get("state")).lower() not in {
                "closed",
                "disabled",
                "inactive",
            }
            level = _option_level(account)
            choices.append(
                {
                    "account_key": key,
                    "label": f"{'Agentic' if agentic else 'Standard'} {_masked_account(number)}",
                    "mask": _masked_account(number),
                    "agentic_allowed": agentic,
                    "active": active,
                    "options_level": level,
                    "eligible_for_live_options": bool(agentic and active and level >= 2),
                }
            )
        with self._lock:
            self._accounts = ephemeral
        return {
            "accounts": choices,
            "account_count": len(choices),
            "raw_account_numbers_exposed": False,
            "account_data_persisted": False,
        }

    def _selected_account(self, account_key: str) -> tuple[str, dict[str, Any]]:
        listing = self.account_choices()
        choice = next(
            (row for row in listing["accounts"] if row.get("account_key") == account_key),
            None,
        )
        with self._lock:
            number = self._accounts.get(account_key)
        if not isinstance(choice, dict) or not number:
            raise RobinhoodOptionExecutionError("account_choice_invalid")
        if choice.get("eligible_for_live_options") is not True:
            raise RobinhoodOptionExecutionError("agentic_options_account_required")
        return number, choice

    def _preflight_account(self, account_number: str, report: Mapping[str, Any]) -> dict[str, Any]:
        arguments = {"account_number": account_number}
        portfolio_result = self.manager.call_read_tool(
            "get_portfolio", arguments, timeout_seconds=12
        )
        portfolio = _portfolio_values(portfolio_result)
        total_value, available = _portfolio_capacity(portfolio)
        quote = report.get("quote") if isinstance(report.get("quote"), Mapping) else {}
        candidate = report.get("candidate") if isinstance(report.get("candidate"), Mapping) else {}
        quantity = int(_number(candidate.get("quantity_cap")) or 0)
        ask = _number(quote.get("ask_price"))
        debit = quantity * ask * 100 if quantity > 0 and ask is not None else None
        if total_value is None or total_value <= 0 or available is None or available < 0:
            raise RobinhoodOptionExecutionError("portfolio_capacity_invalid")
        if debit is None or debit <= 0:
            raise RobinhoodOptionExecutionError("option_debit_invalid")
        if debit > available + 0.01:
            raise RobinhoodOptionExecutionError("insufficient_buying_power")
        if debit > total_value * MAX_ACCOUNT_RISK_FRACTION + 0.01:
            raise RobinhoodOptionExecutionError("option_debit_exceeds_account_risk_cap")

        snapshots: dict[str, Any] = {}
        for tool, keys in (
            ("get_option_positions", ("positions", "option_positions")),
            ("get_option_orders", ("orders", "option_orders")),
            ("get_equity_positions", ("positions", "equity_positions")),
            ("get_equity_orders", ("orders", "equity_orders")),
        ):
            value = self.manager.call_read_tool(tool, arguments, timeout_seconds=12)
            if _has_more(value):
                raise RobinhoodOptionExecutionError("broker_preflight_pagination_incomplete")
            snapshots[tool] = _rows(value, *keys)
        working = [
            row
            for tool in ("get_option_orders", "get_equity_orders")
            for row in snapshots[tool]
            if _order_state(row) not in TERMINAL_ORDER_STATES
        ]
        if working:
            raise RobinhoodOptionExecutionError("working_broker_order_exists")
        for tool in ("get_option_positions", "get_equity_positions"):
            for row in snapshots[tool]:
                quantity = _number(row.get("quantity"))
                if quantity is None:
                    raise RobinhoodOptionExecutionError("broker_position_quantity_invalid")
                if abs(quantity) > 1e-12:
                    raise RobinhoodOptionExecutionError("direct_option_placement_requires_flat_account")
        return {
            "total_value": round(total_value, 2),
            "conservative_buying_power": round(available, 2),
            "estimated_debit": round(debit, 2),
            "risk_cap": round(total_value * MAX_ACCOUNT_RISK_FRACTION, 2),
        }

    def portfolio_analysis(
        self,
        *,
        account_key: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Read one live Agentic account and classify every nonzero holding once."""
        current = _now(now)
        account_number, account = self._selected_account(account_key)
        arguments = {"account_number": account_number}
        portfolio = _portfolio_values(
            self.manager.call_read_tool("get_portfolio", arguments, timeout_seconds=12)
        )
        total_value, available = _portfolio_capacity(portfolio)
        if total_value is None or total_value <= 0 or available is None or available < 0:
            raise RobinhoodOptionExecutionError("portfolio_capacity_invalid")

        snapshots: dict[str, list[dict[str, Any]]] = {}
        for tool, keys in (
            ("get_option_positions", ("positions", "option_positions")),
            ("get_option_orders", ("orders", "option_orders")),
            ("get_equity_positions", ("positions", "equity_positions")),
            ("get_equity_orders", ("orders", "equity_orders")),
        ):
            value = self.manager.call_read_tool(tool, arguments, timeout_seconds=12)
            if _has_more(value):
                raise RobinhoodOptionExecutionError("broker_preflight_pagination_incomplete")
            snapshots[tool] = _rows(value, *keys)

        working_orders = [
            row
            for tool in ("get_option_orders", "get_equity_orders")
            for row in snapshots[tool]
            if _order_state(row) not in TERMINAL_ORDER_STATES
        ]
        option_positions = [
            row
            for row in snapshots["get_option_positions"]
            if (_position_quantity(row) is not None and abs(_position_quantity(row) or 0.0) > 1e-12)
        ]
        equity_positions = [
            row
            for row in snapshots["get_equity_positions"]
            if (_position_quantity(row) is not None and abs(_position_quantity(row) or 0.0) > 1e-12)
        ]

        quote_schema = None
        holdings: list[dict[str, Any]] = []
        for raw in option_positions:
            quantity = _position_quantity(raw)
            option_id = _position_option_id(raw)
            symbol = _position_symbol(raw)
            position_type = _text(raw.get("type") or raw.get("position_type")).lower()
            expiry_text = _text(raw.get("expiration_date") or raw.get("expiry"))[:10]
            try:
                expiry = datetime.fromisoformat(expiry_text).date()
            except ValueError:
                expiry = None
            dte = (expiry - current.date()).days if expiry is not None else None
            quote: dict[str, Any] = {}
            if option_id:
                quote_schema = quote_schema or self.manager.read_tool_input_schema(
                    "get_option_quotes"
                )
                quote_result = self.manager.call_read_tool(
                    "get_option_quotes",
                    _identifier_arguments(quote_schema, option_id),
                    timeout_seconds=12,
                )
                if _has_more(quote_result):
                    raise RobinhoodOptionExecutionError("option_quote_pagination_incomplete")
                quote_rows = _rows(quote_result, "quotes", "results")
                matching_quotes = []
                for row in quote_rows:
                    payload = _quote_payload(row)
                    found_id = _text(
                        payload.get("option_id")
                        or payload.get("instrument_id")
                        or payload.get("id")
                    )
                    if found_id == option_id:
                        matching_quotes.append(payload)
                if len(matching_quotes) == 1:
                    quote = matching_quotes[0]

            bid = _number(quote.get("bid_price") or quote.get("bid"))
            ask = _number(quote.get("ask_price") or quote.get("ask"))
            mark = _number(
                quote.get("mark_price")
                or quote.get("adjusted_mark_price")
                or quote.get("mark")
            )
            quote_at = _parse_timestamp(quote.get("updated_at") or quote.get("quote_at"))
            quote_age_seconds = (
                (current - quote_at).total_seconds() if quote_at is not None else None
            )
            spread = (
                (ask - bid) / ((ask + bid) / 2.0)
                if bid is not None and bid > 0 and ask is not None and ask >= bid
                else None
            )
            average_price_contract = _number(raw.get("average_price"))
            mark_contract = mark * 100.0 if mark is not None else None
            pnl_fraction = (
                (mark_contract - average_price_contract) / average_price_contract
                if mark_contract is not None
                and average_price_contract is not None
                and average_price_contract > 0
                else None
            )
            pending_quantity = _pending_option_quantity(raw)
            exact_identity = bool(option_id and symbol and expiry_text)
            quote_ready = bool(
                quote_age_seconds is not None
                and -5 <= quote_age_seconds <= MAX_OPTION_QUOTE_AGE_SECONDS
                and bid is not None
                and bid > 0
                and ask is not None
                and ask >= bid
                and spread is not None
                and spread <= MAX_AUTOMATED_EXIT_SPREAD_FRACTION
            )
            signals: list[str] = []
            if pnl_fraction is not None and pnl_fraction >= AUTOMATED_TAKE_PROFIT_FRACTION:
                signals.append("profit_target")
            if pnl_fraction is not None and pnl_fraction <= AUTOMATED_HARD_LOSS_FRACTION:
                signals.append("hard_loss_limit")
            if dte is not None and dte <= AUTOMATED_EXPIRY_EXIT_DAYS:
                signals.append("expiration_risk")
            broker_close_ready = bool(
                exact_identity
                and quote_ready
                and position_type == "long"
                and quantity is not None
                and quantity > 0
                and math.isclose(quantity, round(quantity), abs_tol=1e-9)
                and pending_quantity <= 1e-12
                and not working_orders
            )
            action = (
                "take_profit"
                if "profit_target" in signals
                else "expiration_exit"
                if "expiration_risk" in signals
                else "risk_exit"
                if "hard_loss_limit" in signals
                else "hold"
            )
            blockers: list[str] = []
            if not exact_identity:
                blockers.append("exact option identity is incomplete")
            if not quote_ready:
                blockers.append("fresh executable Robinhood bid/ask is unavailable")
            if pending_quantity > 1e-12:
                blockers.append("position has a pending broker transition")
            if working_orders:
                blockers.append("account has a working order")
            if position_type != "long":
                blockers.append("only long option positions are supported")
            holdings.append(
                {
                    "asset": "option",
                    "symbol": symbol,
                    "option_id": option_id or None,
                    "position_type": position_type or None,
                    "expiry": expiry_text or None,
                    "quantity": int(round(quantity)) if quantity is not None else None,
                    "average_price_per_contract": (
                        round(average_price_contract, 2)
                        if average_price_contract is not None
                        else None
                    ),
                    "bid": round(bid, 4) if bid is not None else None,
                    "ask": round(ask, 4) if ask is not None else None,
                    "mark": round(mark, 4) if mark is not None else None,
                    "spread_fraction": round(spread, 6) if spread is not None else None,
                    "quote_age_seconds": (
                        round(quote_age_seconds, 1) if quote_age_seconds is not None else None
                    ),
                    "dte": dte,
                    "unrealized_return_fraction": (
                        round(pnl_fraction, 6) if pnl_fraction is not None else None
                    ),
                    "action": "hold",
                    "signals": [],
                    "broker_reference_action": action,
                    "broker_reference_signals": signals,
                    "broker_close_ready": broker_close_ready,
                    "auto_exit_eligible": False,
                    "blockers": blockers,
                }
            )

        for raw in equity_positions:
            holdings.append(
                {
                    "asset": "equity",
                    "symbol": _position_symbol(raw),
                    "quantity": _position_quantity(raw),
                    "action": "analyze_only",
                    "signals": [],
                    "auto_exit_eligible": False,
                    "blockers": [
                        "automatic equity exits are not enabled in the options automation lane"
                    ],
                }
            )

        analysis = {
            "schema": "optedge_robinhood_portfolio_analysis_v1",
            "generated_at": current.isoformat(),
            "account": account,
            "total_value": round(total_value, 2),
            "conservative_buying_power": round(available, 2),
            "option_position_count": len(option_positions),
            "equity_position_count": len(equity_positions),
            "working_order_count": len(working_orders),
            "new_option_entry_allowed": not option_positions
            and not equity_positions
            and not working_orders,
            "holdings": holdings,
            "automatic_exit_candidate_count": 0,
            "does_not_place_orders": True,
        }
        return sanitize_public_data(analysis, account_numbers=[account_number])

    def review(
        self,
        *,
        candidate_index: int,
        account_key: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = _now(now)
        try:
            report = check_best_option_finalist(
                self.manager,
                data_dir=self.data_dir,
                now=current,
                write=True,
                candidate_index=candidate_index,
            )
        except RobinhoodFinalistCheckError as exc:
            raise RobinhoodOptionExecutionError(exc.code) from exc
        if report.get("ready_for_manual_review") is not True:
            raise RobinhoodOptionExecutionError("optedge_or_robinhood_gate_blocked")
        account_number, account = self._selected_account(account_key)
        capacity = self._preflight_account(account_number, report)
        candidate = report["candidate"]
        quote = report["quote"]
        contract = report["contract"]
        quantity = int(candidate["quantity_cap"])
        price = min(float(quote["ask_price"]), float(quote["limit_cap"]))
        review_arguments = {
            "account_number": account_number,
            "chain_symbol": candidate["symbol"],
            "underlying_type": "equity",
            "legs": [
                {
                    "option_id": contract["option_id"],
                    "side": "buy",
                    "position_effect": "open",
                    "ratio_quantity": 1,
                }
            ],
            "quantity": str(quantity),
            "type": "limit",
            "price": f"{price:.2f}",
            "time_in_force": "gfd",
            "market_hours": "regular_hours",
        }
        preview = self.manager.call_review_tool(
            "review_option_order", review_arguments, timeout_seconds=20
        )
        sanitized_preview = sanitize_public_data(preview, account_numbers=[account_number])
        preview_payload = (
            dict(preview.get("data"))
            if isinstance(preview, Mapping) and isinstance(preview.get("data"), Mapping)
            else dict(preview)
            if isinstance(preview, Mapping)
            else {}
        )
        checks_present = "order_checks" in preview_payload
        checks = preview_payload.get("order_checks")
        if not checks_present or checks not in ([], {}):
            return {
                "status": "preview_blocked",
                "confirmation_required": False,
                "preview": sanitized_preview,
                "blockers": [
                    "Robinhood order checks were missing or returned conditions that must be resolved first."
                ],
                "account": account,
                "capacity": capacity,
            }

        place_arguments = {
            "account_number": account_number,
            "legs": review_arguments["legs"],
            "quantity": review_arguments["quantity"],
            "type": review_arguments["type"],
            "price": review_arguments["price"],
            "time_in_force": review_arguments["time_in_force"],
            "market_hours": review_arguments["market_hours"],
            "ref_id": str(uuid.uuid4()),
        }
        token = secrets.token_urlsafe(32)
        confirmation = {
            "expires_at": current + timedelta(seconds=CONFIRMATION_TTL_SECONDS),
            "candidate_index": candidate_index,
            "candidate_digest": candidate["candidate_digest_sha256"],
            "contract_id": contract["option_id"],
            "report_digest": report["artifact_digest_sha256"],
            "account_key": account_key,
            "account_number": account_number,
            "arguments": place_arguments,
        }
        with self._lock:
            self._confirmations.clear()
            self._confirmations[token] = confirmation
        return {
            "status": "preview_ready",
            "confirmation_required": True,
            "confirmation_token": token,
            "expires_at": confirmation["expires_at"].isoformat(),
            "preview": sanitized_preview,
            "account": account,
            "capacity": capacity,
            "order": {
                "contract": candidate["label"],
                "side": "buy to open",
                "quantity": quantity,
                "limit_price": round(price, 2),
                "maximum_debit": round(quantity * price * 100, 2),
            },
            "automatic_retry_enabled": False,
            "background_polling_enabled": False,
        }

    def place(
        self,
        *,
        confirmation_token: str,
        confirmation_text: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = _now(now)
        if confirmation_text != "PLACE":
            raise RobinhoodOptionExecutionError("explicit_confirmation_required")
        with self._lock:
            confirmation = self._confirmations.pop(confirmation_token, None)
        if not isinstance(confirmation, dict):
            raise RobinhoodOptionExecutionError("confirmation_invalid_or_consumed")
        if current > confirmation["expires_at"]:
            raise RobinhoodOptionExecutionError("confirmation_expired")

        try:
            latest = check_best_option_finalist(
                self.manager,
                data_dir=self.data_dir,
                now=current,
                write=True,
                candidate_index=int(confirmation["candidate_index"]),
            )
        except RobinhoodFinalistCheckError as exc:
            raise RobinhoodOptionExecutionError(exc.code) from exc
        if latest.get("ready_for_manual_review") is not True:
            raise RobinhoodOptionExecutionError("final_revalidation_blocked")
        if latest.get("candidate", {}).get("candidate_digest_sha256") != confirmation.get(
            "candidate_digest"
        ):
            raise RobinhoodOptionExecutionError("candidate_changed_after_preview")
        if latest.get("contract", {}).get("option_id") != confirmation.get("contract_id"):
            raise RobinhoodOptionExecutionError("contract_changed_after_preview")
        account_number, _ = self._selected_account(str(confirmation.get("account_key") or ""))
        if account_number != confirmation.get("account_number"):
            raise RobinhoodOptionExecutionError("account_changed_after_preview")
        self._preflight_account(account_number, latest)
        arguments = dict(confirmation["arguments"])
        current_ask = _number(latest.get("quote", {}).get("ask_price"))
        limit_price = _number(arguments.get("price"))
        if current_ask is None or limit_price is None or current_ask > limit_price + 1e-9:
            raise RobinhoodOptionExecutionError("live_ask_above_previewed_limit")

        result = self.manager.place_confirmed_option_order(arguments, timeout_seconds=20)
        public = sanitize_public_data(result, account_numbers=[confirmation["account_number"]])
        return {
            "status": "order_sent",
            "order": public,
            "logical_order_ref": canonical_digest(arguments)[:16],
            "confirmation_consumed": True,
            "automatic_retry_enabled": False,
            "retry_performed": False,
            "background_polling_enabled": False,
        }

    def execute_automated_entry(
        self,
        *,
        candidate_index: int,
        account_key: str,
        authorization_text: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Review and place one exact entry after a separately armed policy authorizes it."""
        if authorization_text != AUTOMATION_AUTHORIZATION_TEXT:
            raise RobinhoodOptionExecutionError("automation_authorization_required")
        preview = self.review(
            candidate_index=candidate_index,
            account_key=account_key,
            now=now,
        )
        if preview.get("status") != "preview_ready":
            return {
                **preview,
                "automation_authorized": True,
                "order_placed": False,
            }
        placed = self.place(
            confirmation_token=str(preview.get("confirmation_token") or ""),
            confirmation_text="PLACE",
            now=now,
        )
        return {
            **placed,
            "automation_authorized": True,
            "broker_preview_completed": True,
            "per_order_click_required": False,
        }

    def execute_automated_exit(
        self,
        *,
        account_key: str,
        option_id: str,
        authorization_text: str,
        optedge_exit_action: str,
        optedge_decision_digest: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Sell one exact long option once when a hard exit rule is live and fresh."""
        current = _now(now)
        if authorization_text != AUTOMATION_AUTHORIZATION_TEXT:
            raise RobinhoodOptionExecutionError("automation_authorization_required")
        if optedge_exit_action not in {"hard_stop", "hard_target", "close_early"}:
            raise RobinhoodOptionExecutionError("optedge_exit_action_required")
        if len(_text(optedge_decision_digest)) != 64:
            raise RobinhoodOptionExecutionError("optedge_exit_decision_digest_required")
        analysis = self.portfolio_analysis(account_key=account_key, now=current)
        matches = [
            row
            for row in analysis.get("holdings", [])
            if isinstance(row, Mapping) and _text(row.get("option_id")) == _text(option_id)
        ]
        if len(matches) != 1:
            raise RobinhoodOptionExecutionError("automatic_exit_position_not_unique")
        position = dict(matches[0])
        if position.get("broker_close_ready") is not True:
            raise RobinhoodOptionExecutionError("automatic_exit_broker_gate_blocked")
        quantity_value = _number(position.get("quantity"))
        bid = _number(position.get("bid"))
        if (
            quantity_value is None
            or quantity_value <= 0
            or not math.isclose(quantity_value, round(quantity_value), abs_tol=1e-9)
            or bid is None
            or bid <= 0
        ):
            raise RobinhoodOptionExecutionError("automatic_exit_order_invalid")

        account_number, account = self._selected_account(account_key)
        review_arguments = {
            "account_number": account_number,
            "chain_symbol": _text(position.get("symbol")),
            "underlying_type": "equity",
            "legs": [
                {
                    "option_id": _text(option_id),
                    "side": "sell",
                    "position_effect": "close",
                    "ratio_quantity": 1,
                }
            ],
            "quantity": str(int(round(quantity_value))),
            "type": "limit",
            "price": f"{bid:.2f}",
            "time_in_force": "gfd",
            "market_hours": "regular_hours",
        }
        preview = self.manager.call_review_tool(
            "review_option_order", review_arguments, timeout_seconds=20
        )
        preview_payload = (
            dict(preview.get("data"))
            if isinstance(preview, Mapping) and isinstance(preview.get("data"), Mapping)
            else dict(preview)
            if isinstance(preview, Mapping)
            else {}
        )
        checks_present = "order_checks" in preview_payload
        checks = preview_payload.get("order_checks")
        if not checks_present or checks not in ([], {}):
            return {
                "status": "preview_blocked",
                "automation_authorized": True,
                "order_placed": False,
                "preview": sanitize_public_data(preview, account_numbers=[account_number]),
                "account": account,
                "blockers": [
                    "Robinhood order checks were missing or returned conditions that must be resolved first."
                ],
            }

        final_current = _now() if now is None else current
        latest = self.portfolio_analysis(account_key=account_key, now=final_current)
        latest_matches = [
            row
            for row in latest.get("holdings", [])
            if isinstance(row, Mapping) and _text(row.get("option_id")) == _text(option_id)
        ]
        if len(latest_matches) != 1 or latest_matches[0].get("broker_close_ready") is not True:
            raise RobinhoodOptionExecutionError("automatic_exit_final_revalidation_blocked")
        latest_quantity = _number(latest_matches[0].get("quantity"))
        latest_bid = _number(latest_matches[0].get("bid"))
        if (
            latest_quantity is None
            or not math.isclose(latest_quantity, quantity_value, abs_tol=1e-9)
            or latest_bid is None
            or latest_bid + 1e-9 < bid
        ):
            raise RobinhoodOptionExecutionError("automatic_exit_changed_after_preview")

        place_arguments = {
            "account_number": account_number,
            "legs": review_arguments["legs"],
            "quantity": review_arguments["quantity"],
            "type": review_arguments["type"],
            "price": review_arguments["price"],
            "time_in_force": review_arguments["time_in_force"],
            "market_hours": review_arguments["market_hours"],
            "ref_id": str(uuid.uuid4()),
        }
        result = self.manager.place_confirmed_option_order(place_arguments, timeout_seconds=20)
        return {
            "status": "exit_order_sent",
            "automation_authorized": True,
            "broker_preview_completed": True,
            "order": sanitize_public_data(result, account_numbers=[account_number]),
            "position": position,
            "optedge_exit_action": optedge_exit_action,
            "optedge_decision_digest": optedge_decision_digest,
            "logical_order_ref": canonical_digest(place_arguments)[:16],
            "automatic_retry_enabled": False,
            "retry_performed": False,
            "background_polling_enabled": False,
        }
