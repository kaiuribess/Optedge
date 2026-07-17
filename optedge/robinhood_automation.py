# Purpose: Guarded Robinhood automation policy, portfolio decisions, and one-shot cycles.
"""Opt-in local automation for the dedicated Robinhood Agentic account.

The controller is intentionally narrow: one dedicated account, one concurrent
option position, one previewed order at a time, no retries, and no Codex task.
It can run in approval-required mode as a read-only decision engine or in an
explicitly armed automatic mode.  Automatic permission never bypasses market,
portfolio, model, broker, or freshness gates.
"""

from __future__ import annotations

import json
import math
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from optedge.robinhood_finalist import canonical_digest
from optedge.robinhood_option_execution import (
    AUTOMATION_AUTHORIZATION_TEXT,
    RobinhoodOptionExecutionError,
)

POLICY_SCHEMA = "optedge_robinhood_automation_policy_v1"
STATE_SCHEMA = "optedge_robinhood_automation_state_v1"
POLICY_FILE = "robinhood_automation_policy.json"
STATE_FILE = "robinhood_automation_state.json"
AUDIT_FILE = "robinhood_automation_audit.jsonl"
KILL_SWITCH_FILE = "agentic_trading_disabled.flag"
ARMING_PHRASE = "ENABLE GUARDED AUTO"
VALID_MODES = frozenset({"off", "approval_required", "automatic"})
DEFAULT_SCAN_INTERVAL_MINUTES = 15
MIN_SCAN_INTERVAL_MINUTES = 5
MAX_SCAN_INTERVAL_MINUTES = 60
DEFAULT_MAX_DAILY_ORDERS = 1
MAX_DAILY_ORDERS = 3
ARM_TTL_HOURS = 8
NEW_YORK = ZoneInfo("America/New_York")


class RobinhoodAutomationError(RuntimeError):
    """Safe categorical failure for the automation controller."""

    def __init__(self, code: str) -> None:
        safe = "".join(
            char if char.isalnum() or char == "_" else "_" for char in str(code).lower()
        )
        self.code = safe.strip("_") or "automation_failed"
        super().__init__(self.code)


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise RobinhoodAutomationError("naive_automation_clock")
    return current.astimezone(UTC)


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def _append_audit(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True, default=str) + "\n")


def _default_policy() -> dict[str, Any]:
    return {
        "schema": POLICY_SCHEMA,
        "mode": "off",
        "account_key": None,
        "scan_interval_minutes": DEFAULT_SCAN_INTERVAL_MINUTES,
        "max_daily_orders": DEFAULT_MAX_DAILY_ORDERS,
        "single_concurrent_position": True,
        "limit_orders_only": True,
        "automatic_retry_enabled": False,
        "requires_broker_preview": True,
        "requires_positive_exact_after_cost_edge": True,
        "automatic_equity_trading_enabled": False,
        "automatic_option_entries_enabled": True,
        "automatic_option_exits_enabled": True,
        "updated_at": None,
    }


def load_automation_policy(data_dir: Path) -> dict[str, Any]:
    policy = _default_policy()
    stored = _read_json(Path(data_dir) / POLICY_FILE)
    if stored.get("schema") == POLICY_SCHEMA:
        for key in policy:
            if key in stored:
                policy[key] = stored[key]
    if policy.get("mode") not in VALID_MODES:
        policy["mode"] = "off"
    interval = _number(policy.get("scan_interval_minutes"))
    policy["scan_interval_minutes"] = int(
        max(
            MIN_SCAN_INTERVAL_MINUTES,
            min(MAX_SCAN_INTERVAL_MINUTES, interval or DEFAULT_SCAN_INTERVAL_MINUTES),
        )
    )
    daily = _number(policy.get("max_daily_orders"))
    policy["max_daily_orders"] = int(
        max(1, min(MAX_DAILY_ORDERS, daily or DEFAULT_MAX_DAILY_ORDERS))
    )
    return policy


def _market_window(current: datetime) -> dict[str, Any]:
    eastern = current.astimezone(NEW_YORK)
    minutes = eastern.hour * 60 + eastern.minute
    weekday = eastern.weekday() < 5
    # Avoid the noisiest open/close windows. Live quote gates remain authoritative.
    inside = weekday and (9 * 60 + 45) <= minutes <= (15 * 60 + 45)
    return {
        "inside": inside,
        "new_york_time": eastern.isoformat(),
        "window": "09:45-15:45 America/New_York",
    }


class RobinhoodAutomationController:
    """Run bounded account-aware option cycles without creating Codex automations."""

    def __init__(
        self,
        *,
        execution_service: Any,
        data_dir: Path,
        snapshot_syncer: Callable[[], Mapping[str, Any]],
        shortlist_builder: Callable[[], Mapping[str, Any]],
        finalist_checker: Callable[[], Mapping[str, Any]],
        research_refresher: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        exit_analyzer: Callable[..., Mapping[str, Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.execution_service = execution_service
        self.data_dir = Path(data_dir)
        self.snapshot_syncer = snapshot_syncer
        self.shortlist_builder = shortlist_builder
        self.finalist_checker = finalist_checker
        self.research_refresher = research_refresher
        self.exit_analyzer = exit_analyzer
        self.clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._session_armed = False
        self._armed_until: datetime | None = None
        self._cycle_running = False

    @property
    def policy_path(self) -> Path:
        return self.data_dir / POLICY_FILE

    @property
    def state_path(self) -> Path:
        return self.data_dir / STATE_FILE

    @property
    def audit_path(self) -> Path:
        return self.data_dir / AUDIT_FILE

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="optedge-robinhood-automation",
                daemon=True,
            )
            self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=3.0)
        with self._lock:
            self._session_armed = False
            self._armed_until = None

    def _armed(self, current: datetime) -> bool:
        with self._lock:
            return bool(
                self._session_armed
                and self._armed_until is not None
                and current <= self._armed_until
            )

    def configure(self, payload: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
        current = _now(now or self.clock())
        mode = str(payload.get("mode") or "off").strip().lower()
        if mode not in VALID_MODES:
            raise RobinhoodAutomationError("automation_mode_invalid")
        account_key = str(payload.get("account_key") or "").strip()
        if mode != "off" and not account_key.startswith("acct_"):
            raise RobinhoodAutomationError("automation_account_required")
        interval = _number(payload.get("scan_interval_minutes"))
        daily = _number(payload.get("max_daily_orders"))
        policy = _default_policy()
        policy.update(
            {
                "mode": mode,
                "account_key": account_key or None,
                "scan_interval_minutes": int(
                    max(
                        MIN_SCAN_INTERVAL_MINUTES,
                        min(
                            MAX_SCAN_INTERVAL_MINUTES,
                            interval or DEFAULT_SCAN_INTERVAL_MINUTES,
                        ),
                    )
                ),
                "max_daily_orders": int(
                    max(1, min(MAX_DAILY_ORDERS, daily or DEFAULT_MAX_DAILY_ORDERS))
                ),
                "updated_at": current.isoformat(),
            }
        )
        armed = False
        if mode == "automatic":
            if (self.data_dir / KILL_SWITCH_FILE).exists():
                raise RobinhoodAutomationError("automation_kill_switch_active")
            if str(payload.get("arming_phrase") or "") != ARMING_PHRASE:
                raise RobinhoodAutomationError("automation_arming_phrase_invalid")
            if payload.get("acknowledge_unattended_trading") is not True:
                raise RobinhoodAutomationError("automation_unattended_risk_not_acknowledged")
            if payload.get("acknowledge_losses_possible") is not True:
                raise RobinhoodAutomationError("automation_loss_risk_not_acknowledged")
            with self._lock:
                self._session_armed = True
                self._armed_until = current + timedelta(hours=ARM_TTL_HOURS)
            armed = True
        else:
            with self._lock:
                self._session_armed = False
                self._armed_until = None
        _atomic_write_json(self.policy_path, policy)
        _append_audit(
            self.audit_path,
            {
                "at": current.isoformat(),
                "event": "policy_configured",
                "mode": mode,
                "account_key": account_key or None,
                "session_armed": armed,
            },
        )
        self._wake.set()
        return self.status(now=current)

    def _daily_state(self, current: datetime) -> dict[str, Any]:
        day = current.astimezone(NEW_YORK).date().isoformat()
        state = _read_json(self.state_path)
        if state.get("schema") != STATE_SCHEMA or state.get("new_york_date") != day:
            return {
                "schema": STATE_SCHEMA,
                "new_york_date": day,
                "orders_sent": 0,
                "attempted_candidate_digests": [],
                "last_cycle": None,
            }
        state.setdefault("orders_sent", 0)
        state.setdefault("attempted_candidate_digests", [])
        return state

    def status(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = _now(now or self.clock())
        policy = load_automation_policy(self.data_dir)
        state = self._daily_state(current)
        armed = self._armed(current) and policy.get("mode") == "automatic"
        kill_switch = (self.data_dir / KILL_SWITCH_FILE).exists()
        with self._lock:
            running = self._cycle_running
            armed_until = self._armed_until.isoformat() if armed and self._armed_until else None
        return {
            "schema": "optedge_robinhood_automation_status_v1",
            "generated_at": current.isoformat(),
            "mode": policy.get("mode"),
            "session_armed": armed,
            "armed_until": armed_until,
            "rearm_required_after_restart": policy.get("mode") == "automatic" and not armed,
            "cycle_running": running,
            "kill_switch": kill_switch,
            "policy": policy,
            "market_window": _market_window(current),
            "today": state,
            "last_cycle": state.get("last_cycle"),
            "automatic_retry_enabled": False,
            "codex_automation_created": False,
            "notes": [
                "Approval-required mode analyzes holdings and candidates but cannot place an order.",
                "Automatic mode must be re-armed after every cockpit restart and expires after eight hours.",
                "Automatic entries require a positive exact-contract after-cost edge plus every normal Optedge and Robinhood gate.",
                "One concurrent option position is allowed; that holding is analyzed before any new entry.",
            ],
        }

    @staticmethod
    def _candidate_edge(report: Mapping[str, Any]) -> float | None:
        candidate = report.get("candidate") if isinstance(report.get("candidate"), Mapping) else {}
        for field in ("after_cost_edge_pct", "net_edge_pct", "buyer_edge_pct"):
            value = _number(candidate.get(field))
            if value is not None:
                return value
        return None

    def _record_cycle(self, current: datetime, result: Mapping[str, Any]) -> None:
        state = self._daily_state(current)
        state["last_cycle"] = dict(result)
        _atomic_write_json(self.state_path, state)
        _append_audit(
            self.audit_path,
            {
                "at": current.isoformat(),
                "event": "cycle_completed",
                "trigger": result.get("trigger"),
                "status": result.get("status"),
                "action": result.get("action"),
                "candidate_digest": result.get("candidate_digest"),
                "order_status": (
                    result.get("order_result", {}).get("status")
                    if isinstance(result.get("order_result"), Mapping)
                    else None
                ),
            },
        )

    def run_once(
        self,
        *,
        trigger: str = "manual",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = _now(now or self.clock())
        policy = load_automation_policy(self.data_dir)
        mode = str(policy.get("mode") or "off")
        if mode == "off":
            raise RobinhoodAutomationError("automation_mode_off")
        if not policy.get("account_key"):
            raise RobinhoodAutomationError("automation_account_required")
        armed = self._armed(current) and mode == "automatic"
        if trigger == "scheduled" and not armed:
            raise RobinhoodAutomationError("automation_not_armed")
        if (self.data_dir / KILL_SWITCH_FILE).exists():
            raise RobinhoodAutomationError("automation_kill_switch_active")
        market = _market_window(current)

        with self._lock:
            if self._cycle_running:
                raise RobinhoodAutomationError("automation_cycle_already_running")
            self._cycle_running = True
        try:
            initial_sync = dict(self.snapshot_syncer())
            initial_analysis = self.execution_service.portfolio_analysis(
                account_key=str(policy["account_key"]),
                now=current,
            )
            research = (
                dict(self.research_refresher(initial_analysis))
                if self.research_refresher is not None
                else {"ok": True, "status": "not_configured"}
            )
            if research.get("ok") is not True:
                result = {
                    "schema": "optedge_robinhood_automation_cycle_v1",
                    "generated_at": current.isoformat(),
                    "trigger": trigger,
                    "mode": mode,
                    "session_armed": armed,
                    "market_window": market,
                    "status": "research_refresh_blocked",
                    "action": "hold",
                    "detail": "Normal Optedge research did not complete cleanly, so no broker action is allowed.",
                    "snapshot": {
                        "ok": initial_sync.get("ok", True),
                        "account_count": initial_sync.get("account_count"),
                        "counts": initial_sync.get("counts") or {},
                    },
                    "portfolio_analysis": initial_analysis,
                    "research_refresh": research,
                    "does_not_use_codex": True,
                    "automatic_retry_enabled": False,
                }
                self._record_cycle(current, result)
                return result

            # A normal Optedge refresh can take long enough to stale broker state.
            # Refresh the clock, arming state, account, and exact quotes afterward.
            current = _now(self.clock())
            market = _market_window(current)
            armed = self._armed(current) and mode == "automatic"
            sync = dict(self.snapshot_syncer())
            analysis = self.execution_service.portfolio_analysis(
                account_key=str(policy["account_key"]),
                now=current,
            )
            exit_bundle = (
                dict(self.exit_analyzer(analysis, now=current))
                if self.exit_analyzer is not None
                else {
                    "portfolio_analysis": {
                        **analysis,
                        "holdings": [
                            {
                                **dict(row),
                                "action": "hold",
                                "auto_exit_eligible": False,
                                "blockers": [
                                    *(row.get("blockers") or []),
                                    "normal Optedge exit analysis is unavailable",
                                ],
                            }
                            for row in analysis.get("holdings", [])
                            if isinstance(row, Mapping)
                        ],
                    },
                    "reviews": [],
                }
            )
            analyzed_portfolio = exit_bundle.get("portfolio_analysis")
            if isinstance(analyzed_portfolio, Mapping):
                analysis = dict(analyzed_portfolio)
            holdings = [
                dict(row)
                for row in analysis.get("holdings", [])
                if isinstance(row, Mapping)
            ]
            exit_candidates = [row for row in holdings if row.get("auto_exit_eligible") is True]
            result: dict[str, Any] = {
                "schema": "optedge_robinhood_automation_cycle_v1",
                "generated_at": current.isoformat(),
                "trigger": trigger,
                "mode": mode,
                "session_armed": armed,
                "market_window": market,
                "snapshot": {
                    "ok": sync.get("ok", True),
                    "account_count": sync.get("account_count"),
                    "counts": sync.get("counts") or {},
                },
                "portfolio_analysis": analysis,
                "research_refresh": research,
                "optedge_exit_analysis": {
                    "reviews": exit_bundle.get("reviews") or [],
                    "research_source": exit_bundle.get("research_source") or {},
                    "broker_snapshot_source": exit_bundle.get("broker_snapshot_source") or {},
                },
                "does_not_use_codex": True,
                "automatic_retry_enabled": False,
            }
            if holdings:
                result.update(
                    {
                        "status": "position_management",
                        "action": "hold",
                        "candidate_count": 0,
                        "detail": "Existing holdings were analyzed before any new entry.",
                    }
                )
                if len(holdings) > 1:
                    result["detail"] = (
                        "Every holding was analyzed, but automatic selling is blocked while "
                        "more than one nonzero position exists. Choose the position manually."
                    )
                    result["action"] = "hold_for_manual_review"
                elif len(exit_candidates) == 1:
                    result["action"] = str(exit_candidates[0].get("action") or "exit_review")
                    result["exit_candidate"] = exit_candidates[0]
                    if armed and market["inside"]:
                        order = self.execution_service.execute_automated_exit(
                            account_key=str(policy["account_key"]),
                            option_id=str(exit_candidates[0].get("option_id") or ""),
                            authorization_text=AUTOMATION_AUTHORIZATION_TEXT,
                            optedge_exit_action=str(
                                exit_candidates[0].get("optedge_exit_action") or ""
                            ),
                            optedge_decision_digest=canonical_digest(
                                {
                                    "holding": exit_candidates[0],
                                    "research_source": exit_bundle.get("research_source") or {},
                                    "broker_snapshot_source": exit_bundle.get("broker_snapshot_source") or {},
                                }
                            ),
                        )
                        result["order_result"] = order
                        result["status"] = "order_sent" if order.get("status") == "exit_order_sent" else "preview_blocked"
                        if order.get("status") == "exit_order_sent":
                            state = self._daily_state(current)
                            state["orders_sent"] = int(state.get("orders_sent") or 0) + 1
                            _atomic_write_json(self.state_path, state)
                self._record_cycle(current, result)
                return result

            shortlist = dict(self.shortlist_builder())
            live = dict(self.finalist_checker())
            reports = [
                dict(row)
                for row in live.get("reports", [])
                if isinstance(row, Mapping)
            ]
            choices = []
            for index, report in enumerate(reports):
                edge = self._candidate_edge(report)
                candidate = (
                    dict(report.get("candidate"))
                    if isinstance(report.get("candidate"), Mapping)
                    else {}
                )
                eligible = bool(
                    report.get("ready_for_manual_review") is True
                    and edge is not None
                    and edge > 0
                )
                choices.append(
                    {
                        "candidate_index": index,
                        "label": candidate.get("label"),
                        "symbol": candidate.get("symbol"),
                        "after_cost_edge_pct": edge,
                        "market_check_passed": report.get("market_check_passed") is True,
                        "eligible": eligible,
                        "blockers": report.get("blockers") or [],
                    }
                )
            eligible_choices = [row for row in choices if row["eligible"]]
            eligible_choices.sort(
                key=lambda row: _number(row.get("after_cost_edge_pct")) or float("-inf"),
                reverse=True,
            )
            result.update(
                {
                    "status": "analysis_complete",
                    "action": "choose_candidate" if eligible_choices else "hold_cash",
                    "shortlist_status": shortlist.get("status"),
                    "candidate_count": len(choices),
                    "eligible_candidate_count": len(eligible_choices),
                    "choices": choices,
                    "detail": (
                        "Eligible candidates are ready for a user choice."
                        if eligible_choices and mode == "approval_required"
                        else "No exact candidate proved positive after-cost edge and every live gate."
                        if not eligible_choices
                        else "The strongest eligible candidate can proceed under the armed policy."
                    ),
                }
            )
            if not armed or not eligible_choices:
                self._record_cycle(current, result)
                return result
            if not market["inside"]:
                result["status"] = "outside_execution_window"
                result["action"] = "hold_cash"
                result["detail"] = "Automatic orders are limited to the guarded regular-hours window."
                self._record_cycle(current, result)
                return result

            chosen = eligible_choices[0]
            report = reports[int(chosen["candidate_index"])]
            candidate = report.get("candidate") if isinstance(report.get("candidate"), Mapping) else {}
            digest = str(candidate.get("candidate_digest_sha256") or canonical_digest(candidate))
            state = self._daily_state(current)
            attempted = [str(value) for value in state.get("attempted_candidate_digests", [])]
            if digest in attempted:
                result["status"] = "candidate_already_attempted"
                result["action"] = "hold_cash"
                result["candidate_digest"] = digest
                self._record_cycle(current, result)
                return result
            if int(state.get("orders_sent") or 0) >= int(policy["max_daily_orders"]):
                result["status"] = "daily_order_limit_reached"
                result["action"] = "hold_cash"
                result["candidate_digest"] = digest
                self._record_cycle(current, result)
                return result

            # Record before the broker call so an ambiguous outcome is never retried automatically.
            attempted.append(digest)
            state["attempted_candidate_digests"] = attempted[-50:]
            state["last_attempt_started_at"] = current.isoformat()
            _atomic_write_json(self.state_path, state)
            order = self.execution_service.execute_automated_entry(
                candidate_index=int(chosen["candidate_index"]),
                account_key=str(policy["account_key"]),
                authorization_text=AUTOMATION_AUTHORIZATION_TEXT,
            )
            if order.get("status") == "order_sent":
                state = self._daily_state(current)
                state["orders_sent"] = int(state.get("orders_sent") or 0) + 1
                _atomic_write_json(self.state_path, state)
            result.update(
                {
                    "status": "order_sent" if order.get("status") == "order_sent" else "preview_blocked",
                    "action": "buy_to_open",
                    "selected_candidate": chosen,
                    "candidate_digest": digest,
                    "order_result": order,
                }
            )
            self._record_cycle(current, result)
            return result
        except (RobinhoodOptionExecutionError, RobinhoodAutomationError):
            raise
        finally:
            with self._lock:
                self._cycle_running = False

    def _loop(self) -> None:
        while not self._stop.is_set():
            current = _now(self.clock())
            policy = load_automation_policy(self.data_dir)
            armed = self._armed(current) and policy.get("mode") == "automatic"
            if armed:
                try:
                    self.run_once(trigger="scheduled", now=current)
                except Exception as exc:
                    code = getattr(exc, "code", "automation_cycle_failed")
                    _append_audit(
                        self.audit_path,
                        {
                            "at": current.isoformat(),
                            "event": "cycle_failed",
                            "trigger": "scheduled",
                            "error_code": str(code),
                            "automatic_retry_enabled": False,
                        },
                    )
            interval = int(policy.get("scan_interval_minutes") or DEFAULT_SCAN_INTERVAL_MINUTES)
            self._wake.wait(timeout=max(60, interval * 60))
            self._wake.clear()


__all__ = [
    "ARMING_PHRASE",
    "AUDIT_FILE",
    "POLICY_FILE",
    "POLICY_SCHEMA",
    "RobinhoodAutomationController",
    "RobinhoodAutomationError",
    "STATE_FILE",
    "STATE_SCHEMA",
    "load_automation_policy",
]
