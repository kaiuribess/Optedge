# Purpose: Build one complete, user-triggered Robinhood broker snapshot in memory.
"""Fail-closed direct Robinhood account snapshot synchronization.

The service performs one bounded read pass after an explicit cockpit action.
Raw account identifiers exist only in memory long enough to scope the official
MCP calls and normalize the result.  Only the recursively redacted snapshot and
pseudonymous equity ledger are persisted.  There is no scheduler, polling,
retry, review, or placement call in this module.
"""

from __future__ import annotations

import copy
import math
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from scripts.normalize_robinhood_broker_snapshot import (
    RAW_BUNDLE_SCHEMA,
    default_account_equity_ledger_dir,
    persist_broker_snapshot_bundle,
)

DIRECT_SNAPSHOT_SYNC_SCHEMA = "optedge_robinhood_direct_snapshot_sync_v1"
OMITTED_NEXT_TERMINAL_PROOF_SCHEMA = "optedge_official_mcp_omitted_null_terminal_v1"
MAX_ACCOUNTS = 10
MAX_PAGES_PER_READ = 50
MAX_OPTION_INSTRUMENT_IDS = 250
MAX_TOTAL_MANAGER_CALLS = 120
MAX_TOTAL_CAPTURED_PAGES = 100
SNAPSHOT_SYNC_DEADLINE_SECONDS = 120.0
MAX_READ_CALL_SECONDS = 20.0

_SNAPSHOT_SYNC_LOCK = threading.Lock()


class RobinhoodSnapshotSyncError(RuntimeError):
    """A direct-sync failure containing only a stable categorical code."""

    def __init__(self, code: str) -> None:
        safe = str(code or "snapshot_sync_failed").strip().lower()
        if not safe.replace("_", "").isalnum():
            safe = "snapshot_sync_failed"
        self.code = safe
        super().__init__(safe)


class _SnapshotSyncBudget:
    """Process-local limits shared by every read in one explicit sync."""

    def __init__(self, monotonic: Callable[[], float]) -> None:
        self._monotonic = monotonic
        self.started_at = self._read_clock()
        self.deadline = self.started_at + SNAPSHOT_SYNC_DEADLINE_SECONDS
        self.manager_call_count = 0
        self.captured_page_count = 0

    def _read_clock(self) -> float:
        try:
            value = float(self._monotonic())
        except (TypeError, ValueError, OverflowError) as exc:
            raise RobinhoodSnapshotSyncError("snapshot_monotonic_clock_invalid") from exc
        if not math.isfinite(value):
            raise RobinhoodSnapshotSyncError("snapshot_monotonic_clock_invalid")
        return value

    def remaining_seconds(self) -> float:
        remaining = self.deadline - self._read_clock()
        if remaining <= 0:
            raise RobinhoodSnapshotSyncError("snapshot_sync_deadline_exceeded")
        return remaining

    def consume_manager_call(self) -> None:
        self.remaining_seconds()
        if self.manager_call_count >= MAX_TOTAL_MANAGER_CALLS:
            raise RobinhoodSnapshotSyncError("snapshot_call_budget_exceeded")
        self.manager_call_count += 1

    def consume_page(self) -> None:
        self.remaining_seconds()
        if self.captured_page_count >= MAX_TOTAL_CAPTURED_PAGES:
            raise RobinhoodSnapshotSyncError("snapshot_page_budget_exceeded")
        self.captured_page_count += 1

    def read_timeout_seconds(self) -> float:
        return min(MAX_READ_CALL_SECONDS, self.remaining_seconds())


def _read_tool_schema(
    manager: Any,
    tool_name: str,
    *,
    budget: _SnapshotSyncBudget,
    schema_cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    cached = schema_cache.get(tool_name)
    if cached is not None:
        return cached
    budget.consume_manager_call()
    schema = manager.read_tool_input_schema(tool_name)
    budget.remaining_seconds()
    if not isinstance(schema, Mapping):
        raise RobinhoodSnapshotSyncError("tool_schema_invalid")
    captured = copy.deepcopy(dict(schema))
    schema_cache[tool_name] = captured
    return captured


def _mapping(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RobinhoodSnapshotSyncError(code)
    return dict(value)


def _data_envelope(value: Any, code: str) -> dict[str, Any]:
    result = _mapping(value, code)
    return _mapping(result.get("data"), code)


def _schema_accepts(schema: Mapping[str, Any], field: str) -> bool:
    properties = schema.get("properties")
    return (isinstance(properties, Mapping) and field in properties) or schema.get(
        "additionalProperties", True
    ) is not False


def _field_schema(schema: Mapping[str, Any], field: str) -> dict[str, Any]:
    properties = schema.get("properties")
    if isinstance(properties, Mapping) and isinstance(properties.get(field), Mapping):
        return dict(properties[field])
    return {}


def _schema_types(schema: Mapping[str, Any]) -> set[str]:
    types: set[str] = set()
    raw_type = schema.get("type")
    if isinstance(raw_type, str):
        types.add(raw_type)
    elif isinstance(raw_type, list):
        types.update(str(value) for value in raw_type if isinstance(value, str))
    for key in ("anyOf", "oneOf"):
        rows = schema.get(key)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, Mapping):
                    types.update(_schema_types(row))
    return types


def _cursor_from_next(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 8192:
        raise RobinhoodSnapshotSyncError("pagination_cursor_invalid")
    cursors = parse_qs(urlparse(text).query, keep_blank_values=True).get("cursor", [])
    if len(cursors) != 1 or not cursors[0] or len(cursors[0]) > 4096:
        raise RobinhoodSnapshotSyncError("pagination_cursor_invalid")
    return cursors[0]


def _omitted_next_is_proven_terminal(
    schema: Mapping[str, Any],
    page: Mapping[str, Any],
) -> bool:
    """Accept the official MCP's omitted-null terminal page contract only.

    Robinhood's current Agentic MCP may omit ``data.next`` when it would be
    null.  That omission is safe to treat as terminal only when the closed
    input schema explicitly defines a string pagination cursor sourced from
    the prior response's next URL and the tool returned its guide metadata.
    Unknown or open-ended schemas still fail closed.
    """

    if schema.get("additionalProperties") is not False:
        return False
    cursor_schema = _field_schema(schema, "cursor")
    if "string" not in _schema_types(cursor_schema):
        return False
    description = " ".join(str(cursor_schema.get("description") or "").lower().split())
    if not all(
        phrase in description for phrase in ("pagination cursor", "prior response", "next url")
    ):
        return False
    guide = page.get("guide")
    return isinstance(guide, str) and bool(guide.strip()) and len(guide) <= 20000


def _read_collection_pages(
    manager: Any,
    tool_name: str,
    *,
    collection_key: str,
    base_arguments: dict[str, Any],
    require_explicit_next: bool,
    budget: _SnapshotSyncBudget,
    schema_cache: dict[str, dict[str, Any]],
) -> dict[str, Any] | list[dict[str, Any]]:
    schema = _read_tool_schema(
        manager,
        tool_name,
        budget=budget,
        schema_cache=schema_cache,
    )
    for field in base_arguments:
        if not _schema_accepts(schema, field):
            raise RobinhoodSnapshotSyncError("tool_schema_changed")

    pages: list[dict[str, Any]] = []
    seen_cursors: set[str] = set()
    cursor_used: str | None = None
    for _ in range(MAX_PAGES_PER_READ):
        arguments = dict(base_arguments)
        if cursor_used is not None:
            if not _schema_accepts(schema, "cursor"):
                raise RobinhoodSnapshotSyncError("pagination_schema_changed")
            arguments["cursor"] = cursor_used
        budget.consume_page()
        budget.consume_manager_call()
        page = _mapping(
            manager.call_read_tool(
                tool_name,
                arguments,
                timeout_seconds=budget.read_timeout_seconds(),
            ),
            "tool_result_invalid",
        )
        budget.remaining_seconds()
        data = _data_envelope(page, "tool_result_invalid")
        rows = data.get(collection_key)
        if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
            raise RobinhoodSnapshotSyncError("tool_result_shape_changed")
        omitted_next_terminal = (
            require_explicit_next
            and "next" not in data
            and _omitted_next_is_proven_terminal(schema, page)
        )
        if require_explicit_next and "next" not in data and not omitted_next_terminal:
            raise RobinhoodSnapshotSyncError("pagination_proof_missing")

        captured = copy.deepcopy(page)
        if omitted_next_terminal:
            captured["_optedge_pagination"] = {
                "schema": OMITTED_NEXT_TERMINAL_PROOF_SCHEMA,
                "tool": tool_name,
                "terminal": True,
            }
        if cursor_used is not None:
            captured["request"] = {"cursor": cursor_used}
        pages.append(captured)
        next_value = data.get("next")
        if next_value in (None, ""):
            return pages[0] if len(pages) == 1 else pages
        next_cursor = _cursor_from_next(next_value)
        if next_cursor in seen_cursors:
            raise RobinhoodSnapshotSyncError("pagination_cursor_cycle")
        seen_cursors.add(next_cursor)
        cursor_used = next_cursor
    raise RobinhoodSnapshotSyncError("pagination_page_limit")


def _collection_rows(
    captured: dict[str, Any] | list[dict[str, Any]],
    collection_key: str,
) -> list[dict[str, Any]]:
    pages = captured if isinstance(captured, list) else [captured]
    rows: list[dict[str, Any]] = []
    for page in pages:
        values = _data_envelope(page, "tool_result_invalid").get(collection_key)
        if not isinstance(values, list):
            raise RobinhoodSnapshotSyncError("tool_result_shape_changed")
        rows.extend(dict(value) for value in values if isinstance(value, Mapping))
    return rows


def _account_numbers(accounts_capture: Any) -> list[str]:
    rows = _collection_rows(accounts_capture, "accounts")
    if not rows or len(rows) > MAX_ACCOUNTS:
        raise RobinhoodSnapshotSyncError(
            "account_limit_exceeded" if rows else "no_accounts_returned"
        )
    numbers: list[str] = []
    for row in rows:
        number = str(row.get("account_number") or "").strip()
        if not number:
            raise RobinhoodSnapshotSyncError("account_identity_missing")
        if number in numbers:
            raise RobinhoodSnapshotSyncError("account_identity_duplicate")
        numbers.append(number)
    return numbers


def _portfolio_read(
    manager: Any,
    account_number: str,
    *,
    budget: _SnapshotSyncBudget,
    schema_cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    schema = _read_tool_schema(
        manager,
        "get_portfolio",
        budget=budget,
        schema_cache=schema_cache,
    )
    if not _schema_accepts(schema, "account_number"):
        raise RobinhoodSnapshotSyncError("tool_schema_changed")
    budget.consume_manager_call()
    result = _mapping(
        manager.call_read_tool(
            "get_portfolio",
            {"account_number": account_number},
            timeout_seconds=budget.read_timeout_seconds(),
        ),
        "tool_result_invalid",
    )
    budget.remaining_seconds()
    _data_envelope(result, "tool_result_shape_changed")
    return copy.deepcopy(result)


def _option_ids_from_rows(rows: list[dict[str, Any]]) -> set[str]:
    found: set[str] = set()

    def add(value: Any) -> None:
        option_id = str(value or "").strip()
        if option_id:
            if len(option_id) > 256:
                raise RobinhoodSnapshotSyncError("option_instrument_id_invalid")
            found.add(option_id)

    for row in rows:
        add(row.get("option_id") or row.get("option_instrument_id"))
        legs = row.get("legs")
        if isinstance(legs, list):
            for leg in legs:
                if isinstance(leg, Mapping):
                    add(leg.get("option_id") or leg.get("option_instrument_id"))
    return found


def _instrument_arguments(schema: Mapping[str, Any], option_ids: list[str]) -> dict[str, Any]:
    properties = schema.get("properties")
    property_names = set(properties) if isinstance(properties, Mapping) else set()
    for field in ("ids", "option_ids"):
        if field not in property_names:
            continue
        types = _schema_types(_field_schema(schema, field))
        if "array" in types:
            return {field: option_ids}
        if "string" in types or not types:
            return {field: ",".join(option_ids)}
    if len(option_ids) == 1:
        for field in ("id", "option_id"):
            if field in property_names:
                return {field: option_ids[0]}
    if not property_names and schema.get("additionalProperties", True) is not False:
        return {"ids": ",".join(option_ids)}
    raise RobinhoodSnapshotSyncError("option_instrument_schema_changed")


def _sync_robinhood_broker_snapshot(
    manager: Any,
    *,
    data_dir: Path,
    now: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    budget = _SnapshotSyncBudget(monotonic)
    schema_cache: dict[str, dict[str, Any]] = {}
    budget.consume_manager_call()
    status = manager.status()
    budget.remaining_seconds()
    if not isinstance(status, Mapping) or str(status.get("connection_state")) not in {
        "connected",
        "connected_limited",
    }:
        raise RobinhoodSnapshotSyncError("robinhood_not_connected")

    accounts_capture = _read_collection_pages(
        manager,
        "get_accounts",
        collection_key="accounts",
        base_arguments={},
        require_explicit_next=False,
        budget=budget,
        schema_cache=schema_cache,
    )
    account_numbers = _account_numbers(accounts_capture)
    account_snapshots: list[dict[str, Any]] = []
    all_option_ids: set[str] = set()
    scoped_collections = (
        ("get_equity_positions", "positions"),
        ("get_option_positions", "positions"),
        ("get_equity_orders", "orders"),
        ("get_option_orders", "orders"),
    )
    for account_number in account_numbers:
        scope: dict[str, Any] = {
            "account_number": account_number,
            "get_portfolio": _portfolio_read(
                manager,
                account_number,
                budget=budget,
                schema_cache=schema_cache,
            ),
        }
        for tool_name, collection_key in scoped_collections:
            captured = _read_collection_pages(
                manager,
                tool_name,
                collection_key=collection_key,
                base_arguments={"account_number": account_number},
                require_explicit_next=True,
                budget=budget,
                schema_cache=schema_cache,
            )
            scope[tool_name] = captured
            if tool_name in {"get_option_positions", "get_option_orders"}:
                all_option_ids.update(
                    _option_ids_from_rows(_collection_rows(captured, collection_key))
                )
        account_snapshots.append(scope)

    if len(all_option_ids) > MAX_OPTION_INSTRUMENT_IDS:
        raise RobinhoodSnapshotSyncError("option_instrument_limit_exceeded")

    raw_bundle: dict[str, Any] = {
        "schema": RAW_BUNDLE_SCHEMA,
        "get_accounts": accounts_capture,
        "account_snapshots": account_snapshots,
    }
    if all_option_ids:
        option_ids = sorted(all_option_ids)
        instrument_schema = _read_tool_schema(
            manager,
            "get_option_instruments",
            budget=budget,
            schema_cache=schema_cache,
        )
        instrument_arguments = _instrument_arguments(instrument_schema, option_ids)
        raw_bundle["get_option_instruments"] = _read_collection_pages(
            manager,
            "get_option_instruments",
            collection_key="instruments",
            base_arguments=instrument_arguments,
            require_explicit_next=True,
            budget=budget,
            schema_cache=schema_cache,
        )

    budget.remaining_seconds()
    clock = now or (lambda: datetime.now(UTC))
    finished_at = clock()
    if finished_at.tzinfo is None or finished_at.utcoffset() is None:
        raise RobinhoodSnapshotSyncError("snapshot_clock_invalid")
    generated_at = finished_at.astimezone(UTC).isoformat()
    raw_bundle["generated_at"] = generated_at
    budget.remaining_seconds()

    destination = Path(data_dir) / "robinhood_broker_snapshot.json"
    persistence = persist_broker_snapshot_bundle(
        raw_bundle,
        output_path=destination,
        ledger_dir=default_account_equity_ledger_dir(Path(data_dir)),
        generated_at=generated_at,
    )
    snapshot = persistence.get("snapshot") if isinstance(persistence, Mapping) else {}
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    blockers = snapshot.get("normalization_blockers")
    blocker_count = len(blockers) if isinstance(blockers, list) else 0
    counts = snapshot.get("counts") if isinstance(snapshot.get("counts"), Mapping) else {}
    return {
        "schema": DIRECT_SNAPSHOT_SYNC_SCHEMA,
        "ok": True,
        "snapshot_ready": blocker_count == 0,
        "generated_at": generated_at,
        "account_count": len(account_numbers),
        "option_instrument_count": len(all_option_ids),
        "normalization_blocker_count": blocker_count,
        "counts": dict(counts),
        "output": str(destination),
        "raw_bundle_written": False,
        "account_numbers_persisted": False,
        "does_not_review_orders": True,
        "does_not_place_orders": True,
        "automatic_retry_enabled": False,
        "background_polling_enabled": False,
        "manager_call_count": budget.manager_call_count,
        "captured_page_count": budget.captured_page_count,
        "sync_deadline_seconds": SNAPSHOT_SYNC_DEADLINE_SECONDS,
    }


def sync_robinhood_broker_snapshot(
    manager: Any,
    *,
    data_dir: Path,
    now: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Perform one single-flight account read pass and persist redacted state."""
    if not _SNAPSHOT_SYNC_LOCK.acquire(blocking=False):
        raise RobinhoodSnapshotSyncError("sync_already_active")
    try:
        return _sync_robinhood_broker_snapshot(
            manager,
            data_dir=data_dir,
            now=now,
            monotonic=monotonic,
        )
    finally:
        _SNAPSHOT_SYNC_LOCK.release()


__all__ = [
    "DIRECT_SNAPSHOT_SYNC_SCHEMA",
    "RobinhoodSnapshotSyncError",
    "sync_robinhood_broker_snapshot",
]
