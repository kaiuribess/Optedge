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
        total_value = _number(portfolio.get("total_value") or portfolio.get("equity"))
        buying_power = _number(portfolio.get("buying_power"))
        unleveraged = _number(portfolio.get("unleveraged_buying_power"))
        available = min(
            value for value in (buying_power, unleveraged) if value is not None
        ) if buying_power is not None or unleveraged is not None else None
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
