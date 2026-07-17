# Purpose: Bounded, exact-contract Robinhood option-history collection.
"""Collect a small batch of exact option histories through the official MCP.

This workflow is intentionally operator-triggered and read-only.  It resolves
each requested option identity, reads daily regular-session history, and only
then atomically merges the complete batch into the local validation cache.
There are no retries, background loops, review calls, or placement calls.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backtest import option_history
from optedge.robinhood_finalist import (
    RobinhoodFinalistCheckError,
    _chain_arguments,
    _collection_rows,
    _encoded,
    _first_field,
    _instrument_arguments,
    _number,
    _schema_properties,
    _text,
    _validate_required_arguments,
)

SYNC_SCHEMA = "optedge_robinhood_option_history_sync_v1"
DEFAULT_MAX_REQUESTS = 5
HARD_MAX_REQUESTS = 10
MAX_OPERATION_SECONDS = 75.0
READ_TIMEOUT_SECONDS = 12.0


class RobinhoodOptionHistorySyncError(RuntimeError):
    """A fail-closed history-sync error with a safe categorical code."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "option_history_sync_failed")
        super().__init__(self.code)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _duration_days(request: Mapping[str, Any]) -> int:
    start = datetime.fromisoformat(_text(request.get("start_time")).replace("Z", "+00:00"))
    end = datetime.fromisoformat(_text(request.get("end_time")).replace("Z", "+00:00"))
    return max(1, math.ceil((end - start).total_seconds() / 86_400))


def _span_value(schema: Mapping[str, Any], field: str, request: Mapping[str, Any]) -> str:
    field_schema = _schema_properties(schema).get(field)
    allowed = (
        [str(value) for value in field_schema.get("enum", [])]
        if isinstance(field_schema, Mapping) and isinstance(field_schema.get("enum"), list)
        else []
    )
    days = _duration_days(request)
    capacities = {
        "day": 1,
        "week": 7,
        "month": 31,
        "3month": 93,
        "3months": 93,
        "year": 366,
        "5year": 1830,
        "5years": 1830,
        "all": 100_000,
    }
    if allowed:
        viable = [value for value in allowed if capacities.get(value.lower(), 0) >= days]
        if not viable:
            raise RobinhoodOptionHistorySyncError("historical_span_insufficient")
        return min(viable, key=lambda value: capacities.get(value.lower(), 100_000))
    if days <= 31:
        return "month"
    if days <= 93:
        return "3month"
    return "year"


def _historical_arguments(
    schema: Mapping[str, Any], option_id: str, request: Mapping[str, Any]
) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    identity_field = _first_field(
        schema,
        ("ids", "option_ids", "instrument_ids", "id", "option_id", "instrument_id"),
    )
    if identity_field is None:
        raise RobinhoodOptionHistorySyncError("historical_schema_changed")
    arguments[identity_field] = _encoded(schema, identity_field, option_id)
    properties = _schema_properties(schema)
    for names, value in (
        (("interval",), request.get("interval") or "day"),
        (("bounds",), request.get("bounds") or "regular"),
        (("start_time", "start", "from"), request.get("start_time")),
        (("end_time", "end", "to"), request.get("end_time")),
    ):
        field = next((name for name in names if name in properties), None)
        if field is not None and value not in (None, ""):
            arguments[field] = _encoded(schema, field, value)
    span_field = next((name for name in ("span", "range") if name in properties), None)
    if span_field is not None:
        arguments[span_field] = _encoded(
            schema,
            span_field,
            _span_value(schema, span_field, request),
        )
    try:
        _validate_required_arguments(schema, arguments)
    except RobinhoodFinalistCheckError as exc:
        raise RobinhoodOptionHistorySyncError("historical_schema_changed") from exc
    return arguments


def _history_bars(result: Any, option_id: str, request: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(result, Mapping) or not isinstance(result.get("data"), Mapping):
        raise RobinhoodOptionHistorySyncError("historical_result_invalid")
    data = dict(result["data"])
    present = [key for key in ("historicals", "results", "data_points") if key in data]
    if len(present) != 1 or not isinstance(data[present[0]], list):
        raise RobinhoodOptionHistorySyncError("historical_result_shape_changed")
    if data.get("next") not in (None, ""):
        raise RobinhoodOptionHistorySyncError("historical_pagination_unsupported")
    values = data[present[0]]
    if any(not isinstance(value, Mapping) for value in values):
        raise RobinhoodOptionHistorySyncError("historical_result_shape_changed")

    bars: list[Mapping[str, Any]]
    if present[0] == "data_points":
        bars = list(values)
    else:
        nested = [
            value
            for value in values
            if _text(value.get("instrument_id") or value.get("option_id") or value.get("id"))
            == option_id
        ]
        if not nested and len(values) == 1 and isinstance(values[0].get("data_points"), list):
            nested = list(values)
        if len(nested) == 1 and isinstance(nested[0].get("data_points"), list):
            bars = nested[0]["data_points"]
        elif nested and all("begins_at" in value for value in nested):
            bars = nested
        else:
            raise RobinhoodOptionHistorySyncError("historical_identity_unproven")

    start = datetime.fromisoformat(_text(request.get("start_time")).replace("Z", "+00:00"))
    end = datetime.fromisoformat(_text(request.get("end_time")).replace("Z", "+00:00"))
    normalized: list[dict[str, Any]] = []
    for raw in bars:
        if not isinstance(raw, Mapping):
            raise RobinhoodOptionHistorySyncError("historical_result_shape_changed")
        try:
            begins = datetime.fromisoformat(_text(raw.get("begins_at")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if begins.tzinfo is None:
            begins = begins.replace(tzinfo=UTC)
        begins = begins.astimezone(UTC)
        if begins < start.astimezone(UTC) or begins > end.astimezone(UTC):
            continue
        close = _number(raw.get("close_price") if "close_price" in raw else raw.get("close"))
        if close is None or close < 0:
            continue
        normalized.append(
            {
                "begins_at": begins.isoformat().replace("+00:00", "Z"),
                "open_price": _number(
                    raw.get("open_price") if "open_price" in raw else raw.get("open")
                ),
                "high_price": _number(
                    raw.get("high_price") if "high_price" in raw else raw.get("high")
                ),
                "low_price": _number(
                    raw.get("low_price") if "low_price" in raw else raw.get("low")
                ),
                "close_price": close,
                "session": _text(raw.get("session") or "reg"),
                "interpolated": bool(raw.get("interpolated", False)),
            }
        )
    if not normalized:
        raise RobinhoodOptionHistorySyncError("historical_bars_missing")
    return sorted(normalized, key=lambda row: row["begins_at"])


def _exact_instrument(
    manager: Any,
    request: Mapping[str, Any],
    *,
    deadline: float,
) -> tuple[dict[str, Any], list[str]]:
    symbol = _text(request.get("symbol")).upper()
    expiry = _text(request.get("expiry"))[:10]
    side = _text(request.get("side")).lower()
    strike = _number(request.get("strike"))
    if not symbol or not expiry or side not in {"call", "put"} or strike is None:
        raise RobinhoodOptionHistorySyncError("history_request_invalid")

    chain_schema = manager.read_tool_input_schema("get_option_chains")
    chains = _collection_rows(
        manager,
        "get_option_chains",
        collection_key="chains",
        base_arguments=_chain_arguments(chain_schema, symbol),
        schema=chain_schema,
        deadline=deadline,
    )
    chain_ids = {
        _text(row.get("id") or row.get("chain_id"))
        for row in chains
        if _text(row.get("symbol") or row.get("chain_symbol")).upper() == symbol
        and expiry in [str(value)[:10] for value in (row.get("expiration_dates") or [])]
    }
    chain_ids.discard("")
    if not chain_ids or len(chain_ids) > 2:
        raise RobinhoodOptionHistorySyncError("exact_chain_not_found")

    instrument_schema = manager.read_tool_input_schema("get_option_instruments")
    rows: list[dict[str, Any]] = []
    for chain_id in sorted(chain_ids):
        try:
            arguments = _instrument_arguments(
                instrument_schema,
                chain_id=chain_id,
                symbol=symbol,
                expiry=expiry,
                strike=strike,
                option_type=side,
            )
        except RobinhoodFinalistCheckError as exc:
            raise RobinhoodOptionHistorySyncError("instrument_schema_changed") from exc
        rows.extend(
            _collection_rows(
                manager,
                "get_option_instruments",
                collection_key="instruments",
                base_arguments=arguments,
                schema=instrument_schema,
                deadline=deadline,
            )
        )
    matches = [
        row
        for row in rows
        if _text(row.get("chain_symbol")).upper() == symbol
        and _text(row.get("expiration_date"))[:10] == expiry
        and _text(row.get("type") or row.get("option_type")).lower() == side
        and _number(row.get("strike_price") or row.get("strike")) is not None
        and math.isclose(
            float(_number(row.get("strike_price") or row.get("strike")) or 0),
            strike,
            abs_tol=1e-4,
        )
        and _text(row.get("state")).lower() == "active"
        and _text(row.get("tradability")).lower() == "tradable"
        and _text(row.get("chain_id")) in chain_ids
    ]
    if len(matches) != 1:
        raise RobinhoodOptionHistorySyncError("exact_instrument_ambiguous")
    option_id = _text(matches[0].get("id") or matches[0].get("option_id"))
    if not option_id:
        raise RobinhoodOptionHistorySyncError("option_instrument_id_missing")
    return matches[0], ["get_option_chains", "get_option_instruments"]


def sync_robinhood_option_histories(
    manager: Any,
    *,
    data_dir: Path,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run one all-or-nothing, read-only option-history batch."""
    data_dir = Path(data_dir)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    current = current.astimezone(UTC)
    limit = min(HARD_MAX_REQUESTS, max(1, int(max_requests)))
    request_path = data_dir / option_history.REQUESTS_PATH.name
    packet = _read_json(request_path)
    if packet.get("schema") != option_history.REQUEST_SCHEMA:
        raise RobinhoodOptionHistorySyncError("history_request_packet_missing")
    requests = [
        row
        for row in (packet.get("requests") or [])
        if isinstance(row, Mapping) and _text(row.get("state")).lower() == "active"
    ][:limit]
    if not requests:
        raise RobinhoodOptionHistorySyncError("no_active_history_requests")

    deadline = time.monotonic() + MAX_OPERATION_SECONDS
    contracts: list[dict[str, Any]] = []
    calls: list[str] = []
    completed: list[str] = []
    for request in requests:
        if time.monotonic() >= deadline:
            raise RobinhoodOptionHistorySyncError("history_sync_timeout")
        try:
            instrument, resolution_calls = _exact_instrument(
                manager,
                request,
                deadline=deadline,
            )
        except RobinhoodFinalistCheckError as exc:
            raise RobinhoodOptionHistorySyncError(exc.code) from exc
        calls.extend(resolution_calls)
        option_id = _text(instrument.get("id") or instrument.get("option_id"))
        history_schema = manager.read_tool_input_schema("get_option_historicals")
        arguments = _historical_arguments(history_schema, option_id, request)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RobinhoodOptionHistorySyncError("history_sync_timeout")
        result = manager.call_read_tool(
            "get_option_historicals",
            arguments,
            timeout_seconds=min(READ_TIMEOUT_SECONDS, remaining),
        )
        calls.append("get_option_historicals")
        bars = _history_bars(result, option_id, request)
        contracts.append(
            {
                "symbol": _text(request.get("symbol")).upper(),
                "expiry": _text(request.get("expiry"))[:10],
                "side": _text(request.get("side")).lower(),
                "strike": _number(request.get("strike")),
                "instrument_id": option_id,
                "occ_symbol": _text(instrument.get("occ_symbol")),
                "state": _text(instrument.get("state")),
                "tradability": _text(instrument.get("tradability")),
                "interval": "day",
                "bounds": "regular",
                "bars": bars,
            }
        )
        completed.append(_text(request.get("request_id") or request.get("contract_key")))

    snapshot_path = data_dir / option_history.SNAPSHOT_PATH.name
    existing = _read_json(snapshot_path)
    incoming = {
        "schema": option_history.SNAPSHOT_SCHEMA,
        "generated_at": current.isoformat(),
        "source": "robinhood_mcp_read_only",
        "contracts": contracts,
    }
    merged = option_history.merge_snapshot_payload(existing, incoming, asof=current)
    _atomic_write_json(snapshot_path, merged)
    return {
        "schema": SYNC_SCHEMA,
        "ok": True,
        "generated_at": current.isoformat(),
        "requested_count": len(requests),
        "completed_count": len(completed),
        "completed_request_ids": completed,
        "contract_count": len(contracts),
        "bar_count": sum(len(row.get("bars") or []) for row in contracts),
        "broker_read_call_count": len(calls),
        "broker_read_calls": calls,
        "snapshot_contract_count": len(merged.get("contracts") or []),
        "snapshot_written": True,
        "does_not_place_orders": True,
        "does_not_preview_orders": True,
        "automatic_retry_enabled": False,
        "background_polling_enabled": False,
        "broker_writes_authorized": 0,
    }


__all__ = [
    "DEFAULT_MAX_REQUESTS",
    "HARD_MAX_REQUESTS",
    "RobinhoodOptionHistorySyncError",
    "SYNC_SCHEMA",
    "sync_robinhood_option_histories",
]
