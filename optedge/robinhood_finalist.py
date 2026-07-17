# Purpose: Rank Robinhood chains and verify exact Optedge finalists with fresh broker data.
"""Bounded, read-only Robinhood option-chain ranking and finalist verification.

The normal Optedge research pipeline remains the source of candidate selection.
This module can enumerate the eligible 3m+ Robinhood chains for a bounded set of
Optedge ticker theses, run the surviving contracts through the existing pricing
models, and recheck the best few quotes.  It also performs the final market-data
check for unchanged execution-queue candidates.  It exposes no generic MCP tool
surface, order preview, or order-placement method.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import tempfile
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import numpy as np
from scipy.stats import norm

from config import RISK_FREE_RATE_DEFAULT
from optedge.strategy_profile import (
    DISCOVERY_PROFILE,
    LEAPS_SWING_PROFILE,
    SWING_EXECUTION_OPTION_UNDERLYING_TYPE,
    SWING_EXECUTION_PROFILE,
    is_known_index_option_symbol,
)
from pricing_models import all_models_vec, ensemble_theo_vec, load_weights

FINALIST_CHECK_SCHEMA = "optedge_robinhood_option_finalist_check_v1"
FINALIST_CHECK_FILE = "robinhood_finalist_check.json"
FINALIST_BATCH_SCHEMA = "optedge_robinhood_option_finalist_batch_v1"
FINALIST_BATCH_FILE = "robinhood_finalist_batch.json"
TICKER_EDGE_SCAN_SCHEMA = "optedge_robinhood_ticker_edge_scan_v1"
TICKER_EDGE_SCAN_FILE = "robinhood_ticker_edge_scan.json"
FULL_CHAIN_EDGE_SCAN_SCHEMA = "optedge_robinhood_full_chain_edge_scan_v1"
FULL_CHAIN_EDGE_SCAN_FILE = "robinhood_full_chain_edge_scan.json"
MAX_FINALIST_BATCH_SIZE = 10
QUEUE_SCHEMA = "optedge_robinhood_agentic_options_queue_v1"
CYCLE_SCHEMA = "optedge_robinhood_agentic_cycle_v1"
MAX_QUOTE_AGE_SECONDS = 120.0
MAX_SOURCE_AGE_MINUTES = 45.0
MAX_PAGES_PER_TOOL = 8
MAX_ROWS_PER_TOOL = 4000
MAX_EXACT_INSTRUMENT_ROWS_WITHOUT_NEXT = 4
MAX_MATCHING_CHAINS = 6
MAX_OPERATION_SECONDS = 30.0
READ_TIMEOUT_SECONDS = 12.0
FULL_CHAIN_MAX_OPERATION_SECONDS = 120.0
FULL_CHAIN_MAX_EXPIRATIONS_PER_TICKER = 32
FULL_CHAIN_EXPIRATIONS_PER_INSTRUMENT_READ = 6
FULL_CHAIN_MAX_PAGES_PER_INSTRUMENT_READ = 16
FULL_CHAIN_MAX_INSTRUMENTS_PER_TICKER = 4000
FULL_CHAIN_MAX_TOTAL_INSTRUMENTS = 12000
FULL_CHAIN_QUOTE_BATCH_SIZE = 200
FULL_CHAIN_FINALISTS_PER_TICKER = 3
FULL_CHAIN_RESULT_LIMIT = 25
FULL_CHAIN_MIN_DTE = 90
FULL_CHAIN_MAX_DTE = 900
FULL_CHAIN_MIN_ABS_DELTA = 0.55
FULL_CHAIN_MAX_ABS_DELTA = 0.80
FULL_CHAIN_MAX_SPREAD_FRACTION = 0.10
FULL_CHAIN_MIN_OPEN_INTEREST = 100
FULL_CHAIN_LEAPS_MIN_OPEN_INTEREST = 250
FULL_CHAIN_MIN_DAILY_VOLUME = 10
FULL_CHAIN_MAX_PREMIUM_DOLLARS = 500.0


class RobinhoodFinalistCheckError(RuntimeError):
    """Safe public failure code for one bounded finalist check."""

    def __init__(self, code: str) -> None:
        safe = re.sub(r"[^a-z0-9_]+", "_", str(code or "").strip().lower()).strip("_")
        self.code = safe or "finalist_check_failed"
        super().__init__(self.code)


def canonical_digest(value: Any) -> str:
    """Return the stable SHA-256 binding used by the finalist artifact."""
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    if number is None or not math.isclose(number, round(number), abs_tol=1e-9):
        return None
    return int(round(number))


def _parse_timestamp(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        result = datetime.fromisoformat(text)
    except ValueError:
        return None
    if result.tzinfo is None or result.utcoffset() is None:
        return None
    return result.astimezone(UTC)


def _utc_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise RobinhoodFinalistCheckError("naive_check_clock")
    return current.astimezone(UTC)


def _mapping(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RobinhoodFinalistCheckError(code)
    return dict(value)


def _data_envelope(value: Any) -> dict[str, Any]:
    result = _mapping(value, "tool_result_invalid")
    return _mapping(result.get("data"), "tool_result_invalid")


def _schema_properties(schema: Mapping[str, Any]) -> dict[str, Any]:
    raw = schema.get("properties")
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): value for key, value in raw.items()}


def _schema_types(schema: Mapping[str, Any]) -> set[str]:
    found: set[str] = set()
    value = schema.get("type")
    if isinstance(value, str):
        found.add(value)
    elif isinstance(value, list):
        found.update(str(item) for item in value if isinstance(item, str))
    for key in ("anyOf", "oneOf"):
        rows = schema.get(key)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, Mapping):
                    found.update(_schema_types(row))
    return found


def _field_schema(schema: Mapping[str, Any], field: str) -> dict[str, Any]:
    value = _schema_properties(schema).get(field)
    return dict(value) if isinstance(value, Mapping) else {}


def _accepts_field(schema: Mapping[str, Any], field: str) -> bool:
    return (
        field in _schema_properties(schema) or schema.get("additionalProperties", True) is not False
    )


def _first_field(schema: Mapping[str, Any], names: tuple[str, ...]) -> str | None:
    properties = _schema_properties(schema)
    for name in names:
        if name in properties:
            return name
    if schema.get("additionalProperties", True) is not False:
        return names[0]
    return None


def _encoded(schema: Mapping[str, Any], field: str, value: Any) -> Any:
    field_types = _schema_types(_field_schema(schema, field))
    if "array" in field_types:
        return value if isinstance(value, list) else [value]
    if isinstance(value, list):
        if len(value) != 1:
            return ",".join(str(item) for item in value)
        value = value[0]
    if "number" in field_types or "integer" in field_types:
        number = _number(value)
        if number is None:
            raise RobinhoodFinalistCheckError("tool_schema_changed")
        return int(number) if "integer" in field_types else number
    return str(value)


def _validate_required_arguments(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> None:
    required = schema.get("required")
    if required is None:
        return
    if not isinstance(required, list) or any(not isinstance(field, str) for field in required):
        raise RobinhoodFinalistCheckError("tool_schema_changed")
    if any(field not in arguments for field in required if field != "cursor"):
        raise RobinhoodFinalistCheckError("tool_schema_changed")


def _chain_arguments(schema: Mapping[str, Any], symbol: str) -> dict[str, Any]:
    field = _first_field(schema, ("underlying_symbol", "symbol", "chain_symbol"))
    if field is None:
        raise RobinhoodFinalistCheckError("option_chain_schema_changed")
    arguments = {field: _encoded(schema, field, symbol)}
    _validate_required_arguments(schema, arguments)
    return arguments


def _instrument_arguments(
    schema: Mapping[str, Any],
    *,
    chain_id: str,
    symbol: str,
    expiry: str,
    strike: float,
    option_type: str,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    chain_field = _first_field(
        schema,
        ("chain_id", "chain_ids", "chain_symbol", "underlying_symbol", "symbol"),
    )
    if chain_field is None:
        raise RobinhoodFinalistCheckError("option_instrument_schema_changed")
    chain_value: Any = chain_id if "id" in chain_field else symbol
    arguments[chain_field] = _encoded(schema, chain_field, chain_value)
    for names, value in (
        (("expiration_dates", "expiration_date", "expirations", "expiry"), expiry),
        (("strike_price", "strike_prices", "strike"), strike),
        (("type", "option_type", "side"), option_type),
    ):
        field = _first_field(schema, names)
        if field is not None:
            arguments[field] = _encoded(schema, field, value)
    for field, value in (("state", "active"), ("tradability", "tradable")):
        if field in _schema_properties(schema):
            arguments[field] = _encoded(schema, field, value)
    _validate_required_arguments(schema, arguments)
    return arguments


def _quote_arguments(schema: Mapping[str, Any], option_id: str) -> dict[str, Any]:
    field = _first_field(
        schema,
        ("ids", "option_ids", "instrument_ids", "id", "option_id", "instrument_id"),
    )
    if field is None:
        raise RobinhoodFinalistCheckError("option_quote_schema_changed")
    arguments = {field: _encoded(schema, field, option_id)}
    _validate_required_arguments(schema, arguments)
    return arguments


def _equity_quote_arguments(schema: Mapping[str, Any], symbols: list[str]) -> dict[str, Any]:
    field = _first_field(schema, ("symbols", "symbol"))
    if field is None:
        raise RobinhoodFinalistCheckError("equity_quote_schema_changed")
    arguments = {field: _encoded(schema, field, symbols)}
    _validate_required_arguments(schema, arguments)
    return arguments


def _cursor_from_next(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("cursor")
    text = _text(value)
    if not text or len(text) > 8192:
        raise RobinhoodFinalistCheckError("pagination_cursor_invalid")
    parsed = urlparse(text)
    if parsed.scheme or parsed.query:
        cursors = parse_qs(parsed.query, keep_blank_values=True).get("cursor", [])
        if len(cursors) != 1 or not cursors[0]:
            raise RobinhoodFinalistCheckError("pagination_cursor_invalid")
        text = cursors[0]
    if len(text) > 4096 or any(char in text for char in "\r\n"):
        raise RobinhoodFinalistCheckError("pagination_cursor_invalid")
    return text


def _proves_exact_instrument_terminal_page(
    tool_name: str,
    *,
    base_arguments: Mapping[str, Any],
    schema: Mapping[str, Any],
    cursor: str | None,
    values: list[Any],
) -> bool:
    """Allow Robinhood's cursorless terminal response only for one exact scope."""
    properties = _schema_properties(schema)
    required_fields = {
        "chain_id",
        "expiration_dates",
        "strike_price",
        "type",
        "state",
        "tradability",
    }
    if (
        tool_name != "get_option_instruments"
        or cursor is not None
        or schema.get("additionalProperties") is not False
        or "cursor" not in properties
        or not required_fields <= set(base_arguments)
        or len(values) > MAX_EXACT_INSTRUMENT_ROWS_WITHOUT_NEXT
        or any(not _text(base_arguments.get(field)) for field in required_fields)
    ):
        return False
    expected_strike = _number(base_arguments.get("strike_price"))
    if expected_strike is None:
        return False
    expected_chain = _text(base_arguments.get("chain_id"))
    expected_expiry = _text(base_arguments.get("expiration_dates"))[:10]
    expected_type = _text(base_arguments.get("type")).lower()
    expected_state = _text(base_arguments.get("state")).lower()
    expected_tradability = _text(base_arguments.get("tradability")).lower()
    for raw in values:
        if not isinstance(raw, Mapping):
            return False
        strike = _number(raw.get("strike_price") or raw.get("strike"))
        if (
            _text(raw.get("chain_id")) != expected_chain
            or _text(raw.get("expiration_date"))[:10] != expected_expiry
            or _text(raw.get("type") or raw.get("option_type")).lower() != expected_type
            or _text(raw.get("state")).lower() != expected_state
            or _text(raw.get("tradability")).lower() != expected_tradability
            or strike is None
            or not math.isclose(strike, expected_strike, abs_tol=1e-4)
        ):
            return False
    return True


def _collection_rows(
    manager: Any,
    tool_name: str,
    *,
    collection_key: str | tuple[str, ...],
    base_arguments: dict[str, Any],
    schema: Mapping[str, Any],
    deadline: float,
    max_pages: int = MAX_PAGES_PER_TOOL,
    allow_omitted_next_as_terminal: bool = False,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_cursors: set[str] = set()
    cursor: str | None = None
    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or max_pages < 1:
        raise RobinhoodFinalistCheckError("pagination_page_limit_invalid")
    for _ in range(max_pages):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RobinhoodFinalistCheckError("finalist_check_timeout")
        arguments = dict(base_arguments)
        if cursor is not None:
            if not _accepts_field(schema, "cursor"):
                raise RobinhoodFinalistCheckError("pagination_schema_changed")
            arguments["cursor"] = cursor
        result = manager.call_read_tool(
            tool_name,
            arguments,
            timeout_seconds=min(READ_TIMEOUT_SECONDS, remaining),
        )
        data = _data_envelope(result)
        collection_keys = (
            (collection_key,) if isinstance(collection_key, str) else tuple(collection_key)
        )
        present_keys = [key for key in collection_keys if key in data]
        if len(present_keys) != 1:
            raise RobinhoodFinalistCheckError("tool_result_shape_changed")
        values = data.get(present_keys[0])
        if not isinstance(values, list) or any(not isinstance(row, Mapping) for row in values):
            raise RobinhoodFinalistCheckError("tool_result_shape_changed")
        if "next" not in data:
            # Robinhood's non-paginated option-chain and quote tools currently
            # omit ``next`` entirely.  Generic reads remain strict; the full-chain
            # caller opts in only after it has narrowed a request to exact IDs or
            # bounded chain/expiry/type batches.
            if _accepts_field(schema, "cursor"):
                exact_terminal = _proves_exact_instrument_terminal_page(
                    tool_name,
                    base_arguments=base_arguments,
                    schema=schema,
                    cursor=cursor,
                    values=values,
                )
                if not exact_terminal and not allow_omitted_next_as_terminal:
                    raise RobinhoodFinalistCheckError("pagination_proof_missing")
            rows.extend(dict(row) for row in values)
            if len(rows) > MAX_ROWS_PER_TOOL:
                raise RobinhoodFinalistCheckError("tool_result_limit_exceeded")
            return rows
        rows.extend(dict(row) for row in values)
        if len(rows) > MAX_ROWS_PER_TOOL:
            raise RobinhoodFinalistCheckError("tool_result_limit_exceeded")
        next_value = data.get("next")
        if next_value in (None, ""):
            return rows
        cursor = _cursor_from_next(next_value)
        if cursor in seen_cursors:
            raise RobinhoodFinalistCheckError("pagination_cursor_cycle")
        seen_cursors.add(cursor)
    raise RobinhoodFinalistCheckError("pagination_page_limit")


def _option_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    option_type = _text(row.get("option_side") or row.get("option_type") or row.get("type")).lower()
    if option_type.startswith("c"):
        option_type = "call"
    elif option_type.startswith("p"):
        option_type = "put"
    return {
        "asset": "option",
        "symbol": _text(
            row.get("symbol") or row.get("ticker") or row.get("ticker_or_symbol")
        ).upper(),
        "option_type": option_type,
        "strike": _number(row.get("strike") or row.get("strike_price")),
        "expiry": _text(row.get("expiry") or row.get("expiration_date"))[:10],
        "underlying_type": _text(row.get("underlying_type")).lower(),
    }


def _identity_key(row: Mapping[str, Any]) -> tuple[str, str, float | None, str]:
    identity = _option_identity(row)
    strike = identity["strike"]
    return (
        str(identity["symbol"]),
        str(identity["expiry"]),
        round(float(strike), 4) if strike is not None else None,
        str(identity["option_type"]),
    )


def _candidate_label(identity: Mapping[str, Any]) -> str:
    strike = identity.get("strike")
    strike_text = f"{float(strike):g}" if strike is not None else "?"
    side = "C" if identity.get("option_type") == "call" else "P"
    return f"{identity.get('symbol')} {identity.get('expiry')} {side} {strike_text}"


def _chain_expiries(row: Mapping[str, Any]) -> list[str]:
    values = row.get("expiration_dates") or row.get("expirations") or []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    return [_text(value)[:10] for value in values if _text(value)]


def _quote_row(row: Mapping[str, Any]) -> dict[str, Any]:
    nested = row.get("quote")
    return dict(nested) if isinstance(nested, Mapping) else dict(row)


def _standard_chain_blockers(chain: Mapping[str, Any], symbol: str) -> list[str]:
    blockers: list[str] = []
    if chain.get("can_open_position") is not True:
        blockers.append("Robinhood reports that this chain cannot open new positions.")
    if chain.get("cash_component") is not None:
        blockers.append("The live chain has a cash component and may be an adjusted contract.")
    multiplier = _number(chain.get("trade_value_multiplier") or chain.get("multiplier"))
    if multiplier is None or not math.isclose(multiplier, 100.0, abs_tol=1e-9):
        blockers.append("The live chain does not prove a standard 100-share multiplier.")
    underlyings = chain.get("underlying_instruments")
    if not isinstance(underlyings, list) or not underlyings:
        blockers.append("The live chain does not identify its underlying instrument.")
    else:
        chain_symbol = _text(chain.get("symbol") or chain.get("chain_symbol")).upper()
        symbols = {
            _text(row.get("symbol")).upper()
            for row in underlyings
            if isinstance(row, Mapping) and _text(row.get("symbol"))
        }
        stable_references = [
            row
            for row in underlyings
            if isinstance(row, Mapping)
            and any(_text(row.get(field)) for field in ("id", "instrument_id", "instrument", "url"))
        ]
        exact_reference_binding = bool(
            chain_symbol == symbol and len(underlyings) == 1 and len(stable_references) == 1
        )
        if symbol not in symbols and not exact_reference_binding:
            blockers.append("The live chain does not prove the exact equity underlying.")
    return blockers


def _liquidity_thresholds(candidate: Mapping[str, Any]) -> tuple[int, int]:
    profile = _text(candidate.get("execution_profile")).lower()
    if profile == LEAPS_SWING_PROFILE.name:
        return LEAPS_SWING_PROFILE.min_open_interest, LEAPS_SWING_PROFILE.min_daily_volume
    return DISCOVERY_PROFILE.min_open_interest, DISCOVERY_PROFILE.min_daily_volume


def _planner_candidate(
    candidate: Mapping[str, Any],
    *,
    identity: Mapping[str, Any],
    quote: Mapping[str, Any],
    generated_at: str,
    queue_generated_at: Any,
    market_passed: bool,
) -> dict[str, Any]:
    entry = _number(quote.get("ask_price"))
    profile = _text(candidate.get("execution_profile") or SWING_EXECUTION_PROFILE.name).lower()
    if profile == LEAPS_SWING_PROFILE.name and entry is not None:
        stop = round(entry * (1.0 - LEAPS_SWING_PROFILE.stop_loss_fraction), 2)
        target = round(entry * (1.0 + LEAPS_SWING_PROFILE.target_gain_fraction), 2)
    else:
        stop = _number(candidate.get("stop_price_reference") or candidate.get("stop_price"))
        target = _number(candidate.get("target_price_reference") or candidate.get("target_price"))
    stable = {
        "asset": "option",
        "symbol": identity.get("symbol"),
        "direction": "long",
        "option_type": identity.get("option_type"),
        "strike": identity.get("strike"),
        "expiry": identity.get("expiry"),
        "underlying_type": identity.get("underlying_type"),
        "entry_price": entry,
        "stop_price": stop,
        "target_price": target,
        "max_units": _integer(candidate.get("quantity")),
        "source_file": "robinhood_agentic_queue.json",
        "source_generated_at": queue_generated_at,
        "source_artifact_at": queue_generated_at,
        "source_artifact_time_basis": "generated_at",
        "source_quote_at": quote.get("updated_at"),
        "source_quote_time_basis": "broker_exchange_quote_updated_at",
        "quote_quality": "live_broker",
        "data_delay": "real_time",
        "bid": _number(quote.get("bid_price")),
        "ask": _number(quote.get("ask_price")),
        "mid": _number(quote.get("mark_price")),
        "spread_pct": _number(quote.get("spread_fraction")),
    }
    fingerprint = canonical_digest(stable)[:24]
    return {
        **stable,
        "identity_label": _candidate_label(identity),
        "contract": _candidate_label(identity),
        "contract_multiplier": 100,
        "execution_profile": profile,
        "strategy_evidence_lane": candidate.get("strategy_evidence_lane"),
        "profile_policy_version": candidate.get("profile_policy_version"),
        "planned_hold_sessions": candidate.get("planned_hold_sessions"),
        "default_hold_sessions": candidate.get("default_hold_sessions"),
        "candidate_fingerprint": fingerprint,
        "live_check_generated_at": generated_at,
        "plan_ready": bool(
            market_passed
            and entry is not None
            and entry > 0
            and stop is not None
            and stop > 0
            and target is not None
            and target > entry
        ),
        "blockers": [] if market_passed else ["The Robinhood finalist market check did not pass."],
    }


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False)
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


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _safe_blocked_report(
    *,
    now: datetime,
    identity: Mapping[str, Any],
    candidate: Mapping[str, Any],
    queue: Mapping[str, Any],
    cycle: Mapping[str, Any],
    candidate_lane: str,
    local_entry_allowed: bool,
    blockers: list[str],
    warnings: list[str] | None = None,
    calls: list[str] | None = None,
    contract: Mapping[str, Any] | None = None,
    quote: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    generated_at = now.isoformat()
    expires_at = (now + timedelta(seconds=MAX_QUOTE_AGE_SECONDS)).isoformat()
    report = {
        "schema": FINALIST_CHECK_SCHEMA,
        "generated_at": generated_at,
        "expires_at": expires_at,
        "status": "passed" if not blockers else "blocked",
        "market_check_passed": not blockers,
        "ready_for_manual_review": bool(not blockers and local_entry_allowed),
        "candidate_lane": candidate_lane,
        "local_entry_gate_allowed": bool(local_entry_allowed),
        "candidate": {
            **dict(identity),
            "label": _candidate_label(identity),
            "candidate_digest_sha256": canonical_digest(candidate),
            "quantity_cap": _integer(candidate.get("quantity")),
            "limit_cap": _number(candidate.get("max_limit_price") or candidate.get("limit_price")),
            "execution_profile": candidate.get("execution_profile"),
        },
        "source_bindings": {
            "queue_schema": queue.get("schema"),
            "queue_generated_at": queue.get("generated_at"),
            "queue_digest_sha256": canonical_digest(queue),
            "cycle_schema": cycle.get("schema"),
            "cycle_generated_at": cycle.get("generated_at"),
            "cycle_digest_sha256": canonical_digest(cycle),
        },
        "contract": dict(contract or {}),
        "quote": dict(quote or {}),
        "checks": {
            "exact_contract_identity": not any(
                "identity" in value.lower() or "contract" in value.lower() for value in blockers
            ),
            "fresh_quote": not any(
                "stale" in value.lower() or "timestamp" in value.lower() for value in blockers
            ),
            "spread_within_cap": not any("spread" in value.lower() for value in blockers),
            "ask_within_limit_cap": not any(
                "limit" in value.lower() or "ask" in value.lower() and "cap" in value.lower()
                for value in blockers
            ),
            "liquidity_within_profile": not any(
                "open interest" in value.lower() or "volume" in value.lower() for value in blockers
            ),
            "standard_contract_proven": not any(
                "standard" in value.lower()
                or "underlying" in value.lower()
                or "cash component" in value.lower()
                for value in blockers
            ),
        },
        "blockers": list(dict.fromkeys(value for value in blockers if value)),
        "warnings": list(dict.fromkeys(value for value in (warnings or []) if value)),
        "broker_read_calls": list(calls or []),
        "read_stage_count": len(calls or []),
        "does_not_place_orders": True,
        "does_not_preview_orders": True,
        "automatic_retry_enabled": False,
        "background_polling_enabled": False,
        "broker_writes_authorized": 0,
    }
    report["planner_candidate"] = _planner_candidate(
        candidate,
        identity=identity,
        quote=report["quote"],
        generated_at=generated_at,
        queue_generated_at=queue.get("generated_at"),
        market_passed=not report["blockers"],
    )
    report["artifact_digest_sha256"] = canonical_digest(
        {key: value for key, value in report.items() if key != "artifact_digest_sha256"}
    )
    return report


def _source_age_minutes(value: Any, now: datetime) -> float | None:
    timestamp = _parse_timestamp(value)
    return (now - timestamp).total_seconds() / 60.0 if timestamp is not None else None


def _select_current_finalist(
    queue: Mapping[str, Any],
    cycle: Mapping[str, Any],
    now: datetime,
    candidate_index: int = 0,
) -> tuple[dict[str, Any], str, bool]:
    if queue.get("schema") != QUEUE_SCHEMA or cycle.get("schema") != CYCLE_SCHEMA:
        raise RobinhoodFinalistCheckError("finalist_source_schema_invalid")
    for label, source in (("queue", queue), ("cycle", cycle)):
        age = _source_age_minutes(source.get("generated_at"), now)
        if age is None or age < -1 or age > MAX_SOURCE_AGE_MINUTES:
            raise RobinhoodFinalistCheckError(f"{label}_stale_or_invalid")
    orders = queue.get("orders")
    if (
        not isinstance(candidate_index, int)
        or isinstance(candidate_index, bool)
        or candidate_index < 0
        or candidate_index >= MAX_FINALIST_BATCH_SIZE
    ):
        raise RobinhoodFinalistCheckError("finalist_index_invalid")
    if (
        not isinstance(orders, list)
        or candidate_index >= len(orders)
        or not isinstance(orders[candidate_index], Mapping)
    ):
        raise RobinhoodFinalistCheckError("no_option_finalist")
    candidate = dict(orders[candidate_index])
    identity = _option_identity(candidate)
    if (
        identity.get("symbol") == ""
        or identity.get("option_type") not in {"call", "put"}
        or identity.get("strike") is None
        or identity.get("expiry") == ""
        or identity.get("underlying_type") != SWING_EXECUTION_OPTION_UNDERLYING_TYPE
        or is_known_index_option_symbol(identity.get("symbol"))
        or re.search(r"\d", str(identity.get("symbol") or ""))
    ):
        raise RobinhoodFinalistCheckError("finalist_identity_invalid")
    if _text(candidate.get("action")).upper() != "BUY_TO_OPEN":
        raise RobinhoodFinalistCheckError("finalist_action_invalid")
    wanted_key = _identity_key(candidate)
    wanted_digest = canonical_digest(candidate)
    matches: list[tuple[str, dict[str, Any]]] = []
    for lane in ("manual_review_candidates", "review_only_entry_candidates"):
        rows = cycle.get(lane)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if (
                isinstance(row, Mapping)
                and _identity_key(row) == wanted_key
                and canonical_digest(row) == wanted_digest
            ):
                matches.append((lane, dict(row)))
    if len(matches) != 1:
        raise RobinhoodFinalistCheckError("finalist_cycle_binding_invalid")
    lane = matches[0][0]
    entry_gate = cycle.get("entry_gate") if isinstance(cycle.get("entry_gate"), Mapping) else {}
    local_entry_allowed = bool(
        lane == "manual_review_candidates"
        and entry_gate.get("new_entries_allowed_after_live_checks") is True
    )
    return candidate, lane, local_entry_allowed


def check_best_option_finalist(
    manager: Any,
    *,
    data_dir: Path,
    now: datetime | None = None,
    write: bool = True,
    candidate_index: int = 0,
    source_queue: Mapping[str, Any] | None = None,
    source_cycle: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Check one of the first ten Optedge option finalists through Robinhood once."""
    current = _utc_now(now)
    data_dir = Path(data_dir)
    if (source_queue is None) != (source_cycle is None):
        raise RobinhoodFinalistCheckError("finalist_source_override_incomplete")
    queue = (
        dict(source_queue)
        if source_queue is not None
        else _read_json(data_dir / "robinhood_agentic_queue.json")
    )
    cycle = (
        dict(source_cycle)
        if source_cycle is not None
        else _read_json(data_dir / "robinhood_agentic_cycle.json")
    )
    candidate, candidate_lane, local_entry_allowed = _select_current_finalist(
        queue,
        cycle,
        current,
        candidate_index,
    )
    identity = _option_identity(candidate)
    symbol = str(identity["symbol"])
    expiry = str(identity["expiry"])
    strike = float(identity["strike"])
    option_type = str(identity["option_type"])
    profile = _text(candidate.get("execution_profile") or SWING_EXECUTION_PROFILE.name).lower()
    spread_cap = _number(candidate.get("max_allowed_spread_pct"))
    profile_cap = (
        LEAPS_SWING_PROFILE.max_spread_pct
        if profile == LEAPS_SWING_PROFILE.name
        else SWING_EXECUTION_PROFILE.max_option_spread_pct
    )
    spread_cap = min(spread_cap, profile_cap) if spread_cap and spread_cap > 0 else profile_cap
    limit_cap = _number(candidate.get("max_limit_price") or candidate.get("limit_price"))
    if limit_cap is None or limit_cap <= 0:
        raise RobinhoodFinalistCheckError("finalist_limit_cap_invalid")

    deadline = time.monotonic() + MAX_OPERATION_SECONDS
    calls: list[str] = []
    chain_schema = manager.read_tool_input_schema("get_option_chains")
    chains = _collection_rows(
        manager,
        "get_option_chains",
        collection_key="chains",
        base_arguments=_chain_arguments(chain_schema, symbol),
        schema=chain_schema,
        deadline=deadline,
    )
    calls.append("get_option_chains")
    matching_chains = [
        row
        for row in chains
        if _text(row.get("symbol") or row.get("chain_symbol")).upper() == symbol
        and expiry in _chain_expiries(row)
    ]
    unique_chain_ids = {_text(row.get("id") or row.get("chain_id")) for row in matching_chains}
    unique_chain_ids.discard("")
    blockers: list[str] = []
    warnings: list[str] = []
    if not matching_chains or not unique_chain_ids:
        blockers.append("Robinhood returned no exact option chain containing the planned expiry.")
    if len(unique_chain_ids) > MAX_MATCHING_CHAINS:
        blockers.append(
            "Robinhood returned too many matching chains to verify safely in one bounded check."
        )

    all_instruments: list[dict[str, Any]] = []
    if not blockers:
        instrument_schema = manager.read_tool_input_schema("get_option_instruments")
        for chain_id in sorted(unique_chain_ids):
            rows = _collection_rows(
                manager,
                "get_option_instruments",
                collection_key="instruments",
                base_arguments=_instrument_arguments(
                    instrument_schema,
                    chain_id=chain_id,
                    symbol=symbol,
                    expiry=expiry,
                    strike=strike,
                    option_type=option_type,
                ),
                schema=instrument_schema,
                deadline=deadline,
            )
            calls.append("get_option_instruments")
            all_instruments.extend(rows)
    matches = [
        row
        for row in all_instruments
        if _text(row.get("chain_symbol")).upper() == symbol
        and _text(row.get("expiration_date"))[:10] == expiry
        and _text(row.get("type") or row.get("option_type")).lower() == option_type
        and _number(row.get("strike_price") or row.get("strike")) is not None
        and math.isclose(
            float(_number(row.get("strike_price") or row.get("strike")) or 0.0),
            strike,
            abs_tol=1e-4,
        )
        and _text(row.get("underlying_type")).lower() == SWING_EXECUTION_OPTION_UNDERLYING_TYPE
        and _text(row.get("state")).lower() == "active"
        and _text(row.get("tradability")).lower() == "tradable"
        and _text(row.get("chain_id")) in unique_chain_ids
    ]
    if not blockers and len(matches) != 1:
        blockers.append(
            "Robinhood did not return exactly one active, tradable contract for the planned identity."
        )

    instrument = matches[0] if len(matches) == 1 else {}
    option_id = _text(instrument.get("id") or instrument.get("option_id"))
    selected_chain = next(
        (
            row
            for row in matching_chains
            if _text(row.get("id") or row.get("chain_id")) == _text(instrument.get("chain_id"))
        ),
        {},
    )
    if instrument and not option_id:
        blockers.append("The exact Robinhood option instrument has no stable identifier.")
    if instrument and not selected_chain:
        blockers.append("The exact instrument does not link back to one complete Robinhood chain.")
    if selected_chain:
        blockers.extend(_standard_chain_blockers(selected_chain, symbol))

    quote: dict[str, Any] = {}
    if option_id and not blockers:
        quote_schema = manager.read_tool_input_schema("get_option_quotes")
        quote_rows = _collection_rows(
            manager,
            "get_option_quotes",
            collection_key=("quotes", "results"),
            base_arguments=_quote_arguments(quote_schema, option_id),
            schema=quote_schema,
            deadline=deadline,
        )
        calls.append("get_option_quotes")
        exact_quotes = [
            _quote_row(row)
            for row in quote_rows
            if _text(_quote_row(row).get("instrument_id") or _quote_row(row).get("option_id"))
            == option_id
        ]
        if len(exact_quotes) != 1:
            blockers.append(
                "Robinhood did not return exactly one quote for the resolved option ID."
            )
        else:
            raw_quote = exact_quotes[0]
            bid = _number(raw_quote.get("bid_price") or raw_quote.get("bid"))
            ask = _number(raw_quote.get("ask_price") or raw_quote.get("ask"))
            mark = _number(
                raw_quote.get("mark_price")
                or raw_quote.get("adjusted_mark_price")
                or raw_quote.get("mark")
            )
            quote_at = _parse_timestamp(raw_quote.get("updated_at") or raw_quote.get("quote_at"))
            age_seconds = (current - quote_at).total_seconds() if quote_at is not None else None
            spread = (
                (ask - bid) / ((ask + bid) / 2.0)
                if bid is not None and bid > 0 and ask is not None and ask >= bid
                else None
            )
            open_interest = _integer(
                raw_quote.get("open_interest") or raw_quote.get("openInterest")
            )
            volume = _integer(raw_quote.get("volume") or raw_quote.get("daily_volume"))
            min_open_interest, min_volume = _liquidity_thresholds(candidate)
            if quote_at is None or age_seconds is None:
                blockers.append("The Robinhood option quote timestamp is missing or invalid.")
            elif age_seconds < -5:
                blockers.append(
                    "The Robinhood option quote timestamp is implausibly in the future."
                )
            elif age_seconds > MAX_QUOTE_AGE_SECONDS:
                blockers.append(
                    f"The Robinhood option quote is stale ({age_seconds:.0f} seconds old)."
                )
            if bid is None or bid <= 0 or ask is None or ask < bid:
                blockers.append("The Robinhood bid/ask is missing, zero, or crossed.")
            elif spread is None or spread > spread_cap + 1e-12:
                blockers.append(
                    f"The Robinhood bid/ask spread exceeds the {spread_cap:.1%} profile cap."
                )
            if ask is None or ask > limit_cap + 1e-9:
                blockers.append(
                    f"The live Robinhood ask exceeds the frozen ${limit_cap:.2f} candidate limit cap."
                )
            if open_interest is None or open_interest < min_open_interest:
                blockers.append(
                    f"Live open interest is below the {min_open_interest:,} profile minimum or unavailable."
                )
            if volume is None or volume < min_volume:
                blockers.append(
                    f"Live daily volume is below the {min_volume:,} profile minimum or unavailable."
                )
            delta = _number(raw_quote.get("delta"))
            if profile == LEAPS_SWING_PROFILE.name:
                if delta is None or not (
                    LEAPS_SWING_PROFILE.min_abs_delta
                    <= abs(delta)
                    <= LEAPS_SWING_PROFILE.max_abs_delta
                ):
                    blockers.append(
                        "The live LEAPS delta is outside the dedicated profile range or unavailable."
                    )
            previous_ask = _number(candidate.get("source_ask") or candidate.get("ask"))
            if previous_ask and ask is not None and previous_ask > 0:
                change_fraction = (ask - previous_ask) / previous_ask
                if abs(change_fraction) >= 0.10:
                    warnings.append(
                        f"The live ask changed {change_fraction:+.1%} from the research quote."
                    )
            quote = {
                "instrument_id": option_id,
                "updated_at": quote_at.isoformat() if quote_at is not None else None,
                "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
                "bid_price": bid,
                "ask_price": ask,
                "mark_price": mark,
                "spread_fraction": round(spread, 6) if spread is not None else None,
                "spread_cap": spread_cap,
                "limit_cap": limit_cap,
                "open_interest": open_interest,
                "volume": volume,
                "delta": delta,
                "gamma": _number(raw_quote.get("gamma")),
                "theta": _number(raw_quote.get("theta")),
                "vega": _number(raw_quote.get("vega")),
                "implied_volatility": _number(raw_quote.get("implied_volatility")),
                "break_even_price": _number(raw_quote.get("break_even_price")),
                "high_fill_rate_buy_price": _number(raw_quote.get("high_fill_rate_buy_price")),
                "quote_quality": "live_broker",
                "data_delay": "real_time",
            }

    contract = {
        "option_id": option_id or None,
        "chain_id": instrument.get("chain_id") if instrument else None,
        "chain_symbol": instrument.get("chain_symbol") if instrument else None,
        "underlying_type": instrument.get("underlying_type") if instrument else None,
        "expiration_date": instrument.get("expiration_date") if instrument else None,
        "strike_price": _number(instrument.get("strike_price")) if instrument else None,
        "type": instrument.get("type") if instrument else None,
        "state": instrument.get("state") if instrument else None,
        "tradability": instrument.get("tradability") if instrument else None,
        "can_open_position": selected_chain.get("can_open_position") if selected_chain else None,
        "trade_value_multiplier": _number(
            selected_chain.get("trade_value_multiplier") if selected_chain else None
        ),
        "cash_component_is_null": (
            selected_chain.get("cash_component") is None if selected_chain else False
        ),
    }
    report = _safe_blocked_report(
        now=current,
        identity=identity,
        candidate=candidate,
        queue=queue,
        cycle=cycle,
        candidate_lane=candidate_lane,
        local_entry_allowed=local_entry_allowed,
        blockers=blockers,
        warnings=warnings,
        calls=calls,
        contract=contract,
        quote=quote,
    )
    report["candidate_index"] = candidate_index
    report["artifact_digest_sha256"] = canonical_digest(
        {key: value for key, value in report.items() if key != "artifact_digest_sha256"}
    )
    if write:
        _atomic_write_json(data_dir / FINALIST_CHECK_FILE, report)
    return report


def check_top_option_finalists(
    manager: Any,
    *,
    data_dir: Path,
    limit: int = MAX_FINALIST_BATCH_SIZE,
    now: datetime | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """Live-check up to ten ranked Optedge candidates once, without broker writes."""
    current = _utc_now(now)
    data_dir = Path(data_dir)
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise RobinhoodFinalistCheckError("finalist_batch_limit_invalid")
    limit = max(1, min(limit, MAX_FINALIST_BATCH_SIZE))
    queue = _read_json(data_dir / "robinhood_agentic_queue.json")
    orders = queue.get("orders")
    if not isinstance(orders, list) or not orders:
        raise RobinhoodFinalistCheckError("no_option_finalist")

    reports: list[dict[str, Any]] = []
    for candidate_index in range(min(limit, len(orders))):
        try:
            report = check_best_option_finalist(
                manager,
                data_dir=data_dir,
                now=current,
                write=False,
                candidate_index=candidate_index,
            )
        except RobinhoodFinalistCheckError as exc:
            candidate = orders[candidate_index] if isinstance(orders[candidate_index], Mapping) else {}
            identity = _option_identity(candidate)
            report = {
                "schema": FINALIST_CHECK_SCHEMA,
                "candidate_index": candidate_index,
                "generated_at": current.isoformat(),
                "expires_at": (current + timedelta(seconds=MAX_QUOTE_AGE_SECONDS)).isoformat(),
                "status": "blocked",
                "market_check_passed": False,
                "ready_for_manual_review": False,
                "candidate": {
                    **identity,
                    "label": _candidate_label(identity),
                    "candidate_digest_sha256": canonical_digest(candidate),
                },
                "contract": {},
                "quote": {},
                "blockers": [f"Robinhood live check stopped safely ({exc.code})."],
                "warnings": [],
                "does_not_place_orders": True,
                "does_not_preview_orders": True,
                "automatic_retry_enabled": False,
                "background_polling_enabled": False,
                "broker_writes_authorized": 0,
            }
            report["artifact_digest_sha256"] = canonical_digest(report)
        reports.append(report)

    result = {
        "schema": FINALIST_BATCH_SCHEMA,
        "generated_at": current.isoformat(),
        "expires_at": (current + timedelta(seconds=MAX_QUOTE_AGE_SECONDS)).isoformat(),
        "requested_limit": limit,
        "candidate_count": len(reports),
        "market_passed_count": sum(row.get("market_check_passed") is True for row in reports),
        "review_ready_count": sum(row.get("ready_for_manual_review") is True for row in reports),
        "reports": reports,
        "one_shot": True,
        "does_not_place_orders": True,
        "does_not_preview_orders": True,
        "automatic_retry_enabled": False,
        "background_polling_enabled": False,
        "broker_writes_authorized": 0,
    }
    result["artifact_digest_sha256"] = canonical_digest(result)
    if write:
        _atomic_write_json(data_dir / FINALIST_BATCH_FILE, result)
    return result


def _ticker_scan_contract_candidate(row: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one provider-ranked 3m+ contract for an exact Robinhood read."""
    raw = dict(row)
    raw["option_side"] = raw.get("option_side") or raw.get("side")
    identity = _option_identity(raw)
    ask = _number(row.get("ask"))
    mid = _number(row.get("mid"))
    reference = ask if ask is not None and ask > 0 else mid
    if reference is None or reference <= 0:
        raise RobinhoodFinalistCheckError("ticker_scan_price_missing")
    execution_profile = _text(row.get("execution_profile") or SWING_EXECUTION_PROFILE.name)
    return {
        **raw,
        "asset": "option",
        "symbol": identity.get("symbol"),
        "ticker_or_symbol": identity.get("symbol"),
        "action": "BUY_TO_OPEN",
        "order_type": "limit",
        "time_in_force": "day",
        "quantity": 1,
        "option_side": identity.get("option_type"),
        "underlying_type": identity.get("underlying_type"),
        "strike": identity.get("strike"),
        "expiry": identity.get("expiry"),
        "contract": _candidate_label(identity),
        "reference_entry_price": round(reference, 4),
        "source_bid": _number(row.get("bid")),
        "source_ask": ask,
        "max_limit_price": round(reference * 1.08, 2),
        "max_allowed_spread_pct": (
            LEAPS_SWING_PROFILE.max_spread_pct
            if execution_profile == LEAPS_SWING_PROFILE.name
            else SWING_EXECUTION_PROFILE.max_option_spread_pct
        ),
        "execution_profile": execution_profile,
    }


def _ticker_scan_empty_report(
    ticker: Mapping[str, Any],
    *,
    current: datetime,
    reason: str,
) -> dict[str, Any]:
    symbol = _text(ticker.get("symbol")).upper()
    return {
        "schema": FINALIST_CHECK_SCHEMA,
        "generated_at": current.isoformat(),
        "expires_at": (current + timedelta(seconds=MAX_QUOTE_AGE_SECONDS)).isoformat(),
        "status": "no_contract",
        "market_check_passed": False,
        "edge_check_passed": False,
        "ready_for_manual_review": False,
        "candidate": {
            "asset": "option",
            "symbol": symbol,
            "label": f"{symbol} - no qualifying 3m+ contract",
        },
        "ticker_thesis": {
            "source": ticker.get("source"),
            "score": ticker.get("score"),
            "reason": ticker.get("reason"),
        },
        "contract": {},
        "quote": {},
        "research_edge": {
            "after_cost_edge_pct": None,
            "status": "unproven",
            "exact_contract_required": True,
        },
        "blockers": [reason],
        "warnings": [],
        "does_not_place_orders": True,
        "does_not_preview_orders": True,
        "automatic_retry_enabled": False,
        "background_polling_enabled": False,
        "broker_writes_authorized": 0,
    }


def _full_chain_instrument_argument_sets(
    schema: Mapping[str, Any],
    *,
    chain_id: str,
    symbol: str,
    expiries: list[str],
    preferred_option_type: str | None = None,
) -> list[dict[str, Any]]:
    """Build the narrowest schema-proven full-chain instrument reads."""
    properties = _schema_properties(schema)
    required = schema.get("required") or []
    if not isinstance(required, list) or any(not isinstance(field, str) for field in required):
        raise RobinhoodFinalistCheckError("option_instrument_schema_changed")
    if any(field in required for field in ("strike", "strike_price", "strike_prices")):
        raise RobinhoodFinalistCheckError("full_chain_schema_requires_strike")

    chain_field = _first_field(
        schema,
        ("chain_id", "chain_ids", "chain_symbol", "underlying_symbol", "symbol"),
    )
    expiry_field = _first_field(
        schema,
        ("expiration_dates", "expiration_date", "expirations", "expiry"),
    )
    if chain_field is None or expiry_field is None:
        raise RobinhoodFinalistCheckError("option_instrument_schema_changed")
    chain_value: Any = chain_id if "id" in chain_field else symbol
    type_field = next(
        (field for field in ("type", "option_type", "side") if field in properties),
        None,
    )
    normalized_preference = _full_chain_option_type(preferred_option_type)
    option_types: list[str | None]
    if type_field is None:
        option_types = [None]
    elif normalized_preference in {"call", "put"}:
        option_types = [normalized_preference]
    else:
        # Split calls and puts even when type is optional.  Robinhood's live
        # instrument endpoint paginates broad chain reads, while these narrow
        # reads stay bounded and can be proven complete by the returned cursor.
        option_types = ["call", "put"]

    argument_sets: list[dict[str, Any]] = []
    for start in range(0, len(expiries), FULL_CHAIN_EXPIRATIONS_PER_INSTRUMENT_READ):
        expiry_batch = expiries[start : start + FULL_CHAIN_EXPIRATIONS_PER_INSTRUMENT_READ]
        if not expiry_batch:
            continue
        base = {
            chain_field: _encoded(schema, chain_field, chain_value),
            expiry_field: _encoded(schema, expiry_field, expiry_batch),
        }
        for field, value in (("state", "active"), ("tradability", "tradable")):
            if field in properties:
                base[field] = _encoded(schema, field, value)
        for option_type in option_types:
            arguments = dict(base)
            if type_field is not None and option_type is not None:
                arguments[type_field] = _encoded(schema, type_field, option_type)
            _validate_required_arguments(schema, arguments)
            argument_sets.append(arguments)
    return argument_sets


def _full_chain_quote_rows(
    manager: Any,
    option_ids: list[str],
    *,
    schema: Mapping[str, Any],
    deadline: float,
) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    calls = 0
    for start in range(0, len(option_ids), FULL_CHAIN_QUOTE_BATCH_SIZE):
        batch = option_ids[start : start + FULL_CHAIN_QUOTE_BATCH_SIZE]
        if not batch:
            continue
        arguments = _quote_arguments(schema, batch)
        rows.extend(
            _collection_rows(
                manager,
                "get_option_quotes",
                collection_key=("quotes", "results"),
                base_arguments=arguments,
                schema=schema,
                deadline=deadline,
                allow_omitted_next_as_terminal=True,
            )
        )
        calls += 1
    return rows, calls


def _full_chain_live_equity_spots(
    manager: Any,
    symbols: list[str],
    *,
    schema: Mapping[str, Any],
    deadline: float,
) -> dict[str, float]:
    if not symbols:
        return {}
    arguments = _equity_quote_arguments(schema, symbols)
    rows = _collection_rows(
        manager,
        "get_equity_quotes",
        collection_key=("quotes", "results"),
        base_arguments=arguments,
        schema=schema,
        deadline=deadline,
        allow_omitted_next_as_terminal=True,
    )
    spots: dict[str, float] = {}
    for row in rows:
        quote = _quote_row(row)
        symbol = _text(quote.get("symbol")).upper()
        bid = _number(quote.get("bid_price") or quote.get("bid"))
        ask = _number(quote.get("ask_price") or quote.get("ask"))
        last = _number(
            quote.get("last_non_reg_trade_price")
            or quote.get("last_trade_price")
            or quote.get("last_price")
        )
        spot = (
            (bid + ask) / 2.0
            if bid is not None and ask is not None and bid > 0 and ask >= bid
            else last
        )
        if symbol and spot is not None and spot > 0 and quote.get("state") != "inactive":
            spots[symbol] = float(spot)
    return spots


def _full_chain_quote_identity(row: Mapping[str, Any]) -> str:
    quote = _quote_row(row)
    return _text(
        quote.get("instrument_id")
        or quote.get("option_id")
        or quote.get("id")
        or row.get("instrument_id")
        or row.get("option_id")
        or row.get("id")
    )


def _full_chain_option_type(value: Any) -> str:
    option_type = _text(value).lower()
    if option_type.startswith("c"):
        return "call"
    if option_type.startswith("p"):
        return "put"
    return ""


def _full_chain_quote_record(
    instrument: Mapping[str, Any],
    quote_envelope: Mapping[str, Any],
    *,
    symbol: str,
    current: datetime,
) -> dict[str, Any]:
    quote = _quote_row(quote_envelope)
    expiry = _text(instrument.get("expiration_date") or instrument.get("expiry"))[:10]
    try:
        expiry_date = datetime.fromisoformat(expiry).date()
    except ValueError:
        expiry_date = None
    dte = (expiry_date - current.date()).days if expiry_date is not None else None
    bid = _number(quote.get("bid_price") or quote.get("bid"))
    ask = _number(quote.get("ask_price") or quote.get("ask"))
    mark = _number(quote.get("mark_price") or quote.get("adjusted_mark_price"))
    if mark is None and bid is not None and ask is not None and ask >= bid:
        mark = (bid + ask) / 2.0
    spread = (
        (ask - bid) / mark
        if bid is not None and ask is not None and mark is not None and mark > 0 and ask >= bid
        else None
    )
    updated_at = (
        quote.get("updated_at")
        or quote.get("quote_updated_at")
        or quote.get("timestamp")
    )
    updated = _parse_timestamp(updated_at)
    age_seconds = (current - updated).total_seconds() if updated is not None else None
    quote_is_fresh = bool(
        age_seconds is not None and -5 <= age_seconds <= MAX_QUOTE_AGE_SECONDS
    )
    return {
        "option_id": _text(instrument.get("id") or instrument.get("option_id")),
        "symbol": symbol,
        "expiry": expiry,
        "dte": dte,
        "option_type": _full_chain_option_type(
            instrument.get("type") or instrument.get("option_type") or instrument.get("side")
        ),
        "strike": _number(instrument.get("strike_price") or instrument.get("strike")),
        "underlying_type": _text(instrument.get("underlying_type") or "equity").lower(),
        "state": _text(instrument.get("state")).lower(),
        "tradability": _text(instrument.get("tradability")).lower(),
        "bid": bid,
        "ask": ask,
        "mid": mark,
        "spread_fraction": spread,
        "premium_dollars": ask * 100.0 if ask is not None else None,
        "open_interest": _integer(quote.get("open_interest")),
        "volume": _integer(quote.get("volume")),
        "delta": _number(quote.get("delta")),
        "gamma": _number(quote.get("gamma")),
        "theta": _number(quote.get("theta")),
        "vega": _number(quote.get("vega")),
        "implied_volatility": _number(
            quote.get("implied_volatility") or quote.get("impliedVolatility")
        ),
        "quote_updated_at": _text(updated_at),
        "quote_age_seconds": age_seconds,
        "quote_is_fresh": quote_is_fresh,
        "chain_source": "robinhood_mcp",
        "quote_quality": "live_broker",
        "data_delay": "real_time",
    }


def _full_chain_hard_filter_reasons(
    row: Mapping[str, Any],
    *,
    require_fresh_quote: bool = True,
) -> list[str]:
    reasons: list[str] = []
    dte = _integer(row.get("dte"))
    delta = _number(row.get("delta"))
    bid = _number(row.get("bid"))
    ask = _number(row.get("ask"))
    spread = _number(row.get("spread_fraction"))
    premium = _number(row.get("premium_dollars"))
    open_interest = _integer(row.get("open_interest"))
    volume = _integer(row.get("volume"))
    quote_age = _number(row.get("quote_age_seconds"))
    option_type = _full_chain_option_type(row.get("option_type"))
    strike = _number(row.get("strike"))
    if row.get("state") not in {"", "active"}:
        reasons.append("inactive contract")
    if row.get("tradability") not in {"", "tradable"}:
        reasons.append("untradable contract")
    if row.get("underlying_type") != SWING_EXECUTION_OPTION_UNDERLYING_TYPE:
        reasons.append("non-equity underlying")
    if option_type not in {"call", "put"} or strike is None or strike <= 0:
        reasons.append("invalid contract identity")
    if dte is None or not FULL_CHAIN_MIN_DTE <= dte <= FULL_CHAIN_MAX_DTE:
        reasons.append("outside 90-900 DTE")
    if bid is None or ask is None or bid <= 0 or ask <= bid:
        reasons.append("missing two-sided quote")
    if spread is None or spread > FULL_CHAIN_MAX_SPREAD_FRACTION:
        reasons.append("spread above 10%")
    if premium is None or premium <= 0 or premium > FULL_CHAIN_MAX_PREMIUM_DOLLARS:
        reasons.append("premium outside $500 budget")
    if delta is None or not FULL_CHAIN_MIN_ABS_DELTA <= abs(delta) <= FULL_CHAIN_MAX_ABS_DELTA:
        reasons.append("delta outside 0.55-0.80")
    minimum_oi = (
        FULL_CHAIN_LEAPS_MIN_OPEN_INTEREST
        if dte is not None and dte >= LEAPS_SWING_PROFILE.option_min_dte
        else FULL_CHAIN_MIN_OPEN_INTEREST
    )
    if open_interest is None or open_interest < minimum_oi:
        reasons.append(f"open interest below {minimum_oi}")
    if volume is None or volume < FULL_CHAIN_MIN_DAILY_VOLUME:
        reasons.append("daily volume below 10")
    if require_fresh_quote and (
        quote_age is None or quote_age < -5 or quote_age > MAX_QUOTE_AGE_SECONDS
    ):
        reasons.append("quote is missing or stale")
    return reasons


def _theoretical_profit_probability(
    *,
    spot: float,
    strike: float,
    ask: float,
    years: float,
    sigma: float,
    dividend_yield: float,
    option_type: str,
) -> float:
    breakeven = strike + ask if option_type == "call" else strike - ask
    if spot <= 0 or breakeven <= 0 or years <= 0 or sigma <= 0:
        return 0.0
    z = (
        math.log(spot / breakeven)
        + (RISK_FREE_RATE_DEFAULT - dividend_yield - 0.5 * sigma**2) * years
    ) / (sigma * math.sqrt(years))
    probability = norm.cdf(z) if option_type == "call" else norm.cdf(-z)
    return float(max(0.0, min(1.0, probability)))


def _full_chain_price_rows(
    rows: list[dict[str, Any]],
    *,
    context: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if not rows:
        return []
    spot = _number(context.get("spot"))
    spot_is_live = context.get("spot_source") == "robinhood_equity_quote"
    supplied_fair_vol = _number(context.get("fair_vol"))
    confidence_raw = _number(context.get("confidence"))
    confidence = (
        max(0.0, min(1.0, confidence_raw / 100.0 if confidence_raw > 1 else confidence_raw))
        if confidence_raw is not None
        else 0.0
    )
    preferred_side = _full_chain_option_type(
        context.get("option_side") or context.get("side") or context.get("direction")
    )
    if spot is None or spot <= 0:
        for row in rows:
            row.update(
                {
                    "theoretical_ev_status": "unproven",
                    "pricing_blocker": "Normal Optedge spot context is unavailable.",
                    "conservative_positive_edge": False,
                    "ranking_score": -999.0,
                }
            )
        return rows

    observed_ivs = [
        value
        for value in (_number(row.get("implied_volatility")) for row in rows)
        if value is not None and 0.05 <= value <= 3.0
    ]
    fair_vol_independent = supplied_fair_vol is not None and 0.05 <= supplied_fair_vol <= 3.0
    fair_vol = (
        supplied_fair_vol
        if fair_vol_independent
        else float(np.median(observed_ivs))
        if observed_ivs
        else None
    )
    if fair_vol is None:
        for row in rows:
            row.update(
                {
                    "theoretical_ev_status": "unproven",
                    "pricing_blocker": "No valid fair-volatility input is available.",
                    "conservative_positive_edge": False,
                    "ranking_score": -999.0,
                }
            )
        return rows

    dividend_yield = _number(context.get("dividend_yield")) or 0.0
    strikes = np.array([float(row["strike"]) for row in rows], dtype=float)
    years = np.array([max(float(row["dte"]) / 365.25, 1 / 365.25) for row in rows])
    spots = np.full(len(rows), spot, dtype=float)
    sigmas = np.full(len(rows), fair_vol, dtype=float)
    dividends = np.full(len(rows), dividend_yield, dtype=float)
    call_mask = np.array([row["option_type"] == "call" for row in rows], dtype=bool)
    models = all_models_vec(
        spots,
        strikes,
        years,
        RISK_FREE_RATE_DEFAULT,
        sigmas,
        dividends,
        call_mask,
        crr_steps=80,
        models={"bs", "crr", "bjs"},
    )
    ensemble = ensemble_theo_vec(models, load_weights(str(context.get("regime") or "normal")))
    model_stack = np.stack([models[name] for name in ("bs", "crr", "bjs")], axis=1)
    model_low = np.min(model_stack, axis=1)
    model_high = np.max(model_stack, axis=1)
    model_std = np.std(model_stack, axis=1)

    research_guard_status = _text(context.get("research_guard_status")).lower()
    days_to_earnings = _number(context.get("days_to_earnings"))
    event_risk = days_to_earnings is not None and 0 <= days_to_earnings <= 7
    for index, row in enumerate(rows):
        ask = float(row["ask"])
        bid = float(row["bid"])
        spread_dollars = ask - bid
        theoretical_value = float(ensemble[index])
        theoretical_ev = theoretical_value / ask - 1.0
        after_cost_ev = (theoretical_value - spread_dollars / 2.0) / ask - 1.0
        lower_bound_ev = (float(model_low[index]) - spread_dollars / 2.0) / ask - 1.0
        confidence_adjusted = (
            min(after_cost_ev * confidence, lower_bound_ev)
            if after_cost_ev > 0
            else after_cost_ev
        )
        disagreement = (
            float(model_std[index]) / theoretical_value if theoretical_value > 0 else 1.0
        )
        profit_probability = _theoretical_profit_probability(
            spot=spot,
            strike=float(row["strike"]),
            ask=ask,
            years=float(years[index]),
            sigma=fair_vol,
            dividend_yield=dividend_yield,
            option_type=str(row["option_type"]),
        )
        thesis_aligned = bool(preferred_side and row["option_type"] == preferred_side)
        conservative_positive = bool(
            fair_vol_independent
            and spot_is_live
            and research_guard_status == "passed"
            and row.get("quote_is_fresh") is True
            and thesis_aligned
            and not event_risk
            and theoretical_ev > 0
            and after_cost_ev > 0
            and lower_bound_ev > 0
            and confidence_adjusted > 0
        )
        liquidity_score = max(0.0, 1.0 - float(row["spread_fraction"]) / 0.10)
        model_agreement = max(0.0, 1.0 - min(disagreement, 1.0))
        ranking_score = 100.0 * (
            0.55 * max(-0.50, min(confidence_adjusted, 0.50))
            + 0.20 * profit_probability
            + 0.10 * confidence
            + 0.10 * liquidity_score
            + 0.05 * model_agreement
        )
        status = (
            "positive_conservative"
            if conservative_positive
            else "positive_theoretical_only"
            if theoretical_ev > 0
            else "negative"
        )
        row.update(
            {
                "spot": round(spot, 4),
                "spot_source": context.get("spot_source") or "normal_optedge_snapshot",
                "fair_vol": round(fair_vol, 6),
                "fair_vol_source": (
                    context.get("fair_vol_source") or "normal_optedge_snapshot"
                    if fair_vol_independent
                    else "robinhood_iv_surface_proxy"
                ),
                "fair_vol_independent": fair_vol_independent,
                "thesis_side": preferred_side or None,
                "thesis_aligned": thesis_aligned,
                "thesis_confidence": round(confidence, 4),
                "research_guard_status": research_guard_status or "unknown",
                "event_risk_blocked": event_risk,
                "theoretical_value": round(theoretical_value, 6),
                "theoretical_ev_pct": round(theoretical_ev, 6),
                "after_cost_ev_pct": round(after_cost_ev, 6),
                "ev_lower_bound_pct": round(lower_bound_ev, 6),
                "confidence_adjusted_after_cost_ev_pct": round(confidence_adjusted, 6),
                "theoretical_profit_probability": round(profit_probability, 6),
                "model_disagreement_pct": round(disagreement, 6),
                "theoretical_ev_status": status,
                "conservative_positive_edge": conservative_positive,
                "ranking_score": round(ranking_score, 4),
                "theoretical_models": {
                    "black_scholes": round(float(models["bs"][index]), 6),
                    "crr": round(float(models["crr"][index]), 6),
                    "bjerksund_stensland": round(float(models["bjs"][index]), 6),
                    "low": round(float(model_low[index]), 6),
                    "high": round(float(model_high[index]), 6),
                },
                "pricing_blocker": (
                    "A Robinhood-IV surface proxy is not independent valuation evidence."
                    if not fair_vol_independent
                    else "A fresh Robinhood underlying quote is required for conservative EV."
                    if not spot_is_live
                    else "The normal Optedge research guard has not passed."
                    if research_guard_status != "passed"
                    else "The option quote is stale; theoretical EV is research-only."
                    if row.get("quote_is_fresh") is not True
                    else "The contract opposes the normal Optedge thesis."
                    if not thesis_aligned
                    else "Near-term earnings/event risk blocks the conservative lane."
                    if event_risk
                    else None
                ),
            }
        )
    return rows


def check_full_chain_option_edges(
    manager: Any,
    *,
    data_dir: Path,
    ticker_candidates: list[Mapping[str, Any]],
    pricing_context: Mapping[str, Mapping[str, Any]] | None = None,
    limit: int = MAX_FINALIST_BATCH_SIZE,
    now: datetime | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """Enumerate eligible Robinhood chains, price survivors, and recheck finalists.

    This remains a research-only read lane.  A positive result cannot bypass the
    normal Optedge queue, validation, account, exposure, or order-review gates.
    """
    current = _utc_now(now)
    data_dir = Path(data_dir)
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise RobinhoodFinalistCheckError("ticker_scan_limit_invalid")
    limit = max(1, min(limit, MAX_FINALIST_BATCH_SIZE))
    context_by_symbol = {
        _text(symbol).upper(): dict(value)
        for symbol, value in (pricing_context or {}).items()
        if _text(symbol) and isinstance(value, Mapping)
    }
    tickers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in ticker_candidates:
        if not isinstance(raw, Mapping):
            continue
        symbol = _text(raw.get("symbol")).upper()
        if not symbol or symbol in seen or is_known_index_option_symbol(symbol):
            continue
        seen.add(symbol)
        tickers.append(dict(raw))
        if len(tickers) >= limit:
            break
    if not tickers:
        raise RobinhoodFinalistCheckError("no_ticker_scan_candidates")

    deadline = time.monotonic() + FULL_CHAIN_MAX_OPERATION_SECONDS
    chain_schema = manager.read_tool_input_schema("get_option_chains")
    instrument_schema = manager.read_tool_input_schema("get_option_instruments")
    quote_schema = manager.read_tool_input_schema("get_option_quotes")
    equity_quote_schema = manager.read_tool_input_schema("get_equity_quotes")
    all_survivors: list[dict[str, Any]] = []
    ticker_summaries: list[dict[str, Any]] = []
    rejection_counts: dict[str, int] = {}
    total_instruments = 0
    total_quotes = 0
    broker_read_calls = 0
    live_spots = _full_chain_live_equity_spots(
        manager,
        [_text(ticker.get("symbol")).upper() for ticker in tickers],
        schema=equity_quote_schema,
        deadline=deadline,
    )
    broker_read_calls += 1
    for symbol, spot in live_spots.items():
        context_by_symbol.setdefault(symbol, {})["spot"] = spot
        context_by_symbol[symbol]["spot_source"] = "robinhood_equity_quote"

    for ticker_rank, ticker in enumerate(tickers, start=1):
        if time.monotonic() >= deadline:
            raise RobinhoodFinalistCheckError("full_chain_scan_timeout")
        symbol = _text(ticker.get("symbol")).upper()
        summary = {
            "symbol": symbol,
            "ticker_rank": ticker_rank,
            "ticker_source": ticker.get("source"),
            "ticker_score": ticker.get("score"),
            "status": "blocked",
            "chains": 0,
            "expirations": 0,
            "instruments": 0,
            "quotes": 0,
            "hard_filter_survivors": 0,
            "priced_survivors": 0,
            "finalists": 0,
            "warnings": [],
        }
        try:
            chains = _collection_rows(
                manager,
                "get_option_chains",
                collection_key="chains",
                base_arguments=_chain_arguments(chain_schema, symbol),
                schema=chain_schema,
                deadline=deadline,
            )
            broker_read_calls += 1
            matching_chains = [
                row
                for row in chains
                if _text(row.get("symbol") or row.get("chain_symbol")).upper() == symbol
            ][:MAX_MATCHING_CHAINS]
            summary["chains"] = len(matching_chains)
            instrument_rows: list[dict[str, Any]] = []
            expiration_set: set[str] = set()
            for chain in matching_chains:
                blockers = _standard_chain_blockers(chain, symbol)
                if blockers:
                    summary["warnings"].extend(blockers)
                    continue
                eligible_expiries: list[str] = []
                for expiry in _chain_expiries(chain):
                    try:
                        dte = (datetime.fromisoformat(expiry).date() - current.date()).days
                    except ValueError:
                        continue
                    if FULL_CHAIN_MIN_DTE <= dte <= FULL_CHAIN_MAX_DTE:
                        eligible_expiries.append(expiry)
                eligible_expiries = sorted(set(eligible_expiries))[
                    :FULL_CHAIN_MAX_EXPIRATIONS_PER_TICKER
                ]
                expiration_set.update(eligible_expiries)
                if not eligible_expiries:
                    continue
                chain_id = _text(chain.get("id") or chain.get("chain_id"))
                if not chain_id:
                    summary["warnings"].append("Robinhood chain ID is missing.")
                    continue
                for arguments in _full_chain_instrument_argument_sets(
                    instrument_schema,
                    chain_id=chain_id,
                    symbol=symbol,
                    expiries=eligible_expiries,
                    preferred_option_type=(
                        context_by_symbol.get(symbol, {}).get("option_side")
                        or ticker.get("option_side")
                    ),
                ):
                    instrument_rows.extend(
                        _collection_rows(
                            manager,
                            "get_option_instruments",
                            collection_key="instruments",
                            base_arguments=arguments,
                            schema=instrument_schema,
                            deadline=deadline,
                            max_pages=FULL_CHAIN_MAX_PAGES_PER_INSTRUMENT_READ,
                            allow_omitted_next_as_terminal=True,
                        )
                    )
                    broker_read_calls += 1
            summary["expirations"] = len(expiration_set)
            unique_instruments: dict[str, dict[str, Any]] = {}
            for row in instrument_rows:
                option_id = _text(row.get("id") or row.get("option_id"))
                if option_id:
                    unique_instruments.setdefault(option_id, dict(row))
            if len(unique_instruments) > FULL_CHAIN_MAX_INSTRUMENTS_PER_TICKER:
                raise RobinhoodFinalistCheckError("full_chain_instrument_limit_exceeded")
            if total_instruments + len(unique_instruments) > FULL_CHAIN_MAX_TOTAL_INSTRUMENTS:
                raise RobinhoodFinalistCheckError("full_chain_total_limit_exceeded")
            summary["instruments"] = len(unique_instruments)
            total_instruments += len(unique_instruments)
            option_ids = list(unique_instruments)
            quote_rows, quote_calls = _full_chain_quote_rows(
                manager,
                option_ids,
                schema=quote_schema,
                deadline=deadline,
            )
            broker_read_calls += quote_calls
            quote_by_id = {
                option_id: dict(row)
                for row in quote_rows
                if (option_id := _full_chain_quote_identity(row))
            }
            summary["quotes"] = len(quote_by_id)
            total_quotes += len(quote_by_id)
            survivors: list[dict[str, Any]] = []
            local_rejections: dict[str, int] = {}
            for option_id, instrument in unique_instruments.items():
                quote_envelope = quote_by_id.get(option_id)
                if quote_envelope is None:
                    reasons = ["quote missing for instrument"]
                else:
                    record = _full_chain_quote_record(
                        instrument,
                        quote_envelope,
                        symbol=symbol,
                        current=current,
                    )
                    reasons = _full_chain_hard_filter_reasons(
                        record,
                        require_fresh_quote=False,
                    )
                if reasons:
                    for reason in set(reasons):
                        local_rejections[reason] = local_rejections.get(reason, 0) + 1
                        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                    continue
                record.update(
                    {
                        "ticker_rank": ticker_rank,
                        "ticker_source": ticker.get("source"),
                        "ticker_score": ticker.get("score"),
                        "ticker_reason": ticker.get("reason"),
                    }
                )
                survivors.append(record)
            summary["hard_filter_survivors"] = len(survivors)
            summary["top_rejections"] = [
                {"reason": reason, "count": count}
                for reason, count in sorted(
                    local_rejections.items(), key=lambda item: (-item[1], item[0])
                )[:5]
            ]
            ticker_context = dict(context_by_symbol.get(symbol, {}))
            priced = _full_chain_price_rows(survivors, context=ticker_context)
            summary["priced_survivors"] = len(priced)
            summary["status"] = "priced" if priced else "no_survivors"
            all_survivors.extend(priced)
        except RobinhoodFinalistCheckError as exc:
            summary["error_code"] = exc.code
            summary["warnings"].append(f"Robinhood full-chain read stopped safely ({exc.code}).")
        ticker_summaries.append(summary)

    first_pass = sorted(
        all_survivors,
        key=lambda row: (
            row.get("conservative_positive_edge") is True,
            _number(row.get("ranking_score")) or -999.0,
            _number(row.get("confidence_adjusted_after_cost_ev_pct")) or -999.0,
            _number(row.get("theoretical_profit_probability")) or 0.0,
        ),
        reverse=True,
    )
    finalist_ids: list[str] = []
    finalist_rows: list[dict[str, Any]] = []
    per_symbol: dict[str, int] = {}
    for row in first_pass:
        symbol = _text(row.get("symbol")).upper()
        if per_symbol.get(symbol, 0) >= FULL_CHAIN_FINALISTS_PER_TICKER:
            continue
        per_symbol[symbol] = per_symbol.get(symbol, 0) + 1
        finalist_rows.append(copy.deepcopy(row))
        finalist_ids.append(_text(row.get("option_id")))

    if finalist_ids:
        recheck_rows, recheck_calls = _full_chain_quote_rows(
            manager,
            finalist_ids,
            schema=quote_schema,
            deadline=deadline,
        )
        broker_read_calls += recheck_calls
        recheck_by_id = {
            option_id: dict(row)
            for row in recheck_rows
            if (option_id := _full_chain_quote_identity(row))
        }
        refreshed_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for row in finalist_rows:
            option_id = _text(row.get("option_id"))
            quote_envelope = recheck_by_id.get(option_id)
            if quote_envelope is None:
                row["exact_recheck_passed"] = False
                row["recheck_blockers"] = ["Exact Robinhood quote recheck returned no row."]
                refreshed_by_symbol.setdefault(_text(row.get("symbol")).upper(), []).append(row)
                continue
            instrument = {
                "id": option_id,
                "expiration_date": row.get("expiry"),
                "type": row.get("option_type"),
                "strike_price": row.get("strike"),
                "underlying_type": row.get("underlying_type"),
                "state": row.get("state"),
                "tradability": row.get("tradability"),
            }
            refreshed = _full_chain_quote_record(
                instrument,
                quote_envelope,
                symbol=_text(row.get("symbol")).upper(),
                current=current,
            )
            refreshed.update(
                {
                    key: value
                    for key, value in row.items()
                    if key
                    not in {
                        "bid",
                        "ask",
                        "mid",
                        "spread_fraction",
                        "premium_dollars",
                        "open_interest",
                        "volume",
                        "delta",
                        "gamma",
                        "theta",
                        "vega",
                        "implied_volatility",
                        "quote_updated_at",
                        "quote_age_seconds",
                        "quote_is_fresh",
                    }
                }
            )
            blockers = _full_chain_hard_filter_reasons(refreshed)
            refreshed["exact_recheck_passed"] = not blockers
            refreshed["recheck_blockers"] = blockers
            refreshed_by_symbol.setdefault(refreshed["symbol"], []).append(refreshed)
        finalist_rows = []
        for symbol, rows in refreshed_by_symbol.items():
            finalist_rows.extend(
                _full_chain_price_rows(rows, context=context_by_symbol.get(symbol, {}))
            )

    finalist_rows = sorted(
        finalist_rows,
        key=lambda row: (
            row.get("exact_recheck_passed") is True,
            row.get("conservative_positive_edge") is True,
            _number(row.get("ranking_score")) or -999.0,
        ),
        reverse=True,
    )
    for index, row in enumerate(finalist_rows, start=1):
        row["overall_rank"] = index
        row["contract"] = _candidate_label(
            {
                "symbol": row.get("symbol"),
                "expiry": row.get("expiry"),
                "option_type": row.get("option_type"),
                "strike": row.get("strike"),
            }
        )
    displayed = finalist_rows[:FULL_CHAIN_RESULT_LIMIT]
    conservative_positive_count = sum(
        row.get("exact_recheck_passed") is True
        and row.get("conservative_positive_edge") is True
        for row in displayed
    )
    decision = "research_candidates_available" if conservative_positive_count else "no_trade"
    for summary in ticker_summaries:
        symbol = summary["symbol"]
        summary["finalists"] = sum(row.get("symbol") == symbol for row in displayed)

    result = {
        "schema": FULL_CHAIN_EDGE_SCAN_SCHEMA,
        "generated_at": current.isoformat(),
        "expires_at": (current + timedelta(seconds=MAX_QUOTE_AGE_SECONDS)).isoformat(),
        "decision": decision,
        "decision_label": (
            "Conservative research finalists found; normal Optedge gates still apply."
            if conservative_positive_count
            else "No trade - cash beats every rechecked contract under the conservative EV test."
        ),
        "requested_tickers": limit,
        "ticker_count": len(tickers),
        "total_instruments": total_instruments,
        "total_option_quotes": total_quotes,
        "hard_filter_survivor_count": len(all_survivors),
        "rechecked_finalist_count": len(displayed),
        "conservative_positive_count": conservative_positive_count,
        "theoretical_positive_count": sum(
            (_number(row.get("theoretical_ev_pct")) or -1.0) > 0 for row in displayed
        ),
        "broker_read_calls": broker_read_calls,
        "ticker_summaries": ticker_summaries,
        "rejection_counts": dict(
            sorted(rejection_counts.items(), key=lambda item: (-item[1], item[0]))
        ),
        "results": displayed,
        "filters": {
            "dte": [FULL_CHAIN_MIN_DTE, FULL_CHAIN_MAX_DTE],
            "absolute_delta": [FULL_CHAIN_MIN_ABS_DELTA, FULL_CHAIN_MAX_ABS_DELTA],
            "max_spread_fraction": FULL_CHAIN_MAX_SPREAD_FRACTION,
            "min_open_interest": FULL_CHAIN_MIN_OPEN_INTEREST,
            "leaps_min_open_interest": FULL_CHAIN_LEAPS_MIN_OPEN_INTEREST,
            "min_daily_volume": FULL_CHAIN_MIN_DAILY_VOLUME,
            "max_premium_dollars": FULL_CHAIN_MAX_PREMIUM_DOLLARS,
            "finalists_per_ticker": FULL_CHAIN_FINALISTS_PER_TICKER,
        },
        "pricing_checks": [
            "Black-Scholes theoretical value",
            "CRR American theoretical value",
            "Bjerksund-Stensland American theoretical value",
            "model disagreement and low-model bound",
            "entry and estimated exit spread cost",
            "Optedge confidence shrinkage",
            "theoretical expiration profit probability",
            "normal Optedge thesis direction",
            "near-term earnings/event risk",
        ],
        "one_shot": True,
        "research_only": True,
        "no_trade_is_valid": True,
        "does_not_promote_candidates": True,
        "does_not_place_orders": True,
        "does_not_preview_orders": True,
        "automatic_retry_enabled": False,
        "background_polling_enabled": False,
        "broker_writes_authorized": 0,
    }
    result["artifact_digest_sha256"] = canonical_digest(result)
    if write:
        _atomic_write_json(data_dir / FULL_CHAIN_EDGE_SCAN_FILE, result)
    return result


def check_top_ticker_option_edges(
    manager: Any,
    *,
    data_dir: Path,
    ticker_candidates: list[Mapping[str, Any]],
    contract_rows: list[Mapping[str, Any]],
    limit: int = MAX_FINALIST_BATCH_SIZE,
    now: datetime | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """Check one exact 3m+ contract for each of ten Optedge ticker ideas.

    This is a read-only discovery lane.  It can confirm Robinhood contract and
    quote quality, and it preserves any model-estimated after-cost edge already
    attached to that exact provider row.  It never promotes a discovered row to
    the execution queue or clears the normal validation gate.
    """
    current = _utc_now(now)
    data_dir = Path(data_dir)
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise RobinhoodFinalistCheckError("ticker_scan_limit_invalid")
    limit = max(1, min(limit, MAX_FINALIST_BATCH_SIZE))

    tickers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in ticker_candidates:
        if not isinstance(raw, Mapping):
            continue
        symbol = _text(raw.get("symbol")).upper()
        if not symbol or symbol in seen or is_known_index_option_symbol(symbol):
            continue
        seen.add(symbol)
        tickers.append(dict(raw))
        if len(tickers) >= limit:
            break
    if not tickers:
        raise RobinhoodFinalistCheckError("no_ticker_scan_candidates")

    best_by_symbol: dict[str, Mapping[str, Any]] = {}
    for row in contract_rows:
        if not isinstance(row, Mapping):
            continue
        symbol = _text(row.get("symbol") or row.get("ticker")).upper()
        if symbol in seen and symbol not in best_by_symbol:
            best_by_symbol[symbol] = row

    normalized: list[dict[str, Any]] = []
    index_by_symbol: dict[str, int] = {}
    normalization_errors: dict[str, str] = {}
    for ticker in tickers:
        symbol = _text(ticker.get("symbol")).upper()
        row = best_by_symbol.get(symbol)
        if row is None:
            continue
        try:
            candidate = _ticker_scan_contract_candidate(row)
        except RobinhoodFinalistCheckError as exc:
            normalization_errors[symbol] = exc.code
            continue
        index_by_symbol[symbol] = len(normalized)
        normalized.append(candidate)

    source_queue = {
        "schema": QUEUE_SCHEMA,
        "generated_at": current.isoformat(),
        "execution_enabled": False,
        "max_orders_to_submit": 0,
        "does_not_place_orders": True,
        "orders": copy.deepcopy(normalized),
    }
    source_cycle = {
        "schema": CYCLE_SCHEMA,
        "generated_at": current.isoformat(),
        "auto_submit_allowed": False,
        "does_not_place_orders": True,
        "entry_gate": {"new_entries_allowed_after_live_checks": False},
        "manual_review_candidates": [],
        "review_only_entry_candidates": copy.deepcopy(normalized),
    }

    reports: list[dict[str, Any]] = []
    for ticker in tickers:
        symbol = _text(ticker.get("symbol")).upper()
        row = best_by_symbol.get(symbol)
        candidate_index = index_by_symbol.get(symbol)
        if row is None:
            reports.append(
                _ticker_scan_empty_report(
                    ticker,
                    current=current,
                    reason="The provider scan found no qualifying 3m+ contract for this ticker.",
                )
            )
            continue
        if candidate_index is None:
            reports.append(
                _ticker_scan_empty_report(
                    ticker,
                    current=current,
                    reason=(
                        "The exact contract could not be normalized safely "
                        f"({normalization_errors.get(symbol, 'ticker_scan_contract_invalid')})."
                    ),
                )
            )
            continue
        try:
            report = check_best_option_finalist(
                manager,
                data_dir=data_dir,
                now=current,
                write=False,
                candidate_index=candidate_index,
                source_queue=source_queue,
                source_cycle=source_cycle,
            )
        except RobinhoodFinalistCheckError as exc:
            report = _ticker_scan_empty_report(
                ticker,
                current=current,
                reason=f"Robinhood exact-contract check stopped safely ({exc.code}).",
            )
        edge = _number(row.get("after_cost_edge_pct"))
        edge_status = "positive" if edge is not None and edge > 0 else "negative" if edge is not None else "unproven"
        report["ticker_thesis"] = {
            "source": ticker.get("source"),
            "score": ticker.get("score"),
            "reason": ticker.get("reason"),
            "candidate_rank": ticker.get("candidate_rank"),
        }
        report["research_edge"] = {
            "after_cost_edge_pct": edge,
            "status": edge_status,
            "exact_contract_required": True,
            "source": row.get("chain_source") or row.get("batch_source"),
            "quote_quality": row.get("quote_quality") or row.get("batch_quote_quality"),
            "contract_quality_score": _number(row.get("contract_quality_score")),
            "swing_fit_score": _number(row.get("swing_fit_score")),
        }
        report["edge_check_passed"] = bool(
            report.get("market_check_passed") is True and edge is not None and edge > 0
        )
        report["ready_for_manual_review"] = False
        report["candidate_lane"] = "ticker_research_scan"
        report["local_entry_gate_allowed"] = False
        report["artifact_digest_sha256"] = canonical_digest(
            {key: value for key, value in report.items() if key != "artifact_digest_sha256"}
        )
        reports.append(report)

    result = {
        "schema": TICKER_EDGE_SCAN_SCHEMA,
        "generated_at": current.isoformat(),
        "expires_at": (current + timedelta(seconds=MAX_QUOTE_AGE_SECONDS)).isoformat(),
        "requested_limit": limit,
        "ticker_count": len(tickers),
        "contract_candidate_count": len(normalized),
        "market_passed_count": sum(row.get("market_check_passed") is True for row in reports),
        "positive_edge_count": sum(
            (row.get("research_edge") or {}).get("status") == "positive" for row in reports
        ),
        "live_edge_count": sum(row.get("edge_check_passed") is True for row in reports),
        "review_ready_count": 0,
        "reports": reports,
        "one_shot": True,
        "research_only": True,
        "does_not_promote_candidates": True,
        "does_not_place_orders": True,
        "does_not_preview_orders": True,
        "automatic_retry_enabled": False,
        "background_polling_enabled": False,
        "broker_writes_authorized": 0,
    }
    result["artifact_digest_sha256"] = canonical_digest(result)
    if write:
        _atomic_write_json(data_dir / TICKER_EDGE_SCAN_FILE, result)
    return result


def load_finalist_check_status(
    data_dir: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Load the last public finalist artifact and mark expiration explicitly."""
    current = _utc_now(now)
    report = _read_json(Path(data_dir) / FINALIST_CHECK_FILE)
    if report.get("schema") != FINALIST_CHECK_SCHEMA:
        return {
            "schema": FINALIST_CHECK_SCHEMA,
            "status": "missing",
            "usable": False,
            "market_check_passed": False,
            "does_not_place_orders": True,
        }
    generated = _parse_timestamp(report.get("generated_at"))
    expires = _parse_timestamp(report.get("expires_at"))
    age_seconds = (current - generated).total_seconds() if generated is not None else None
    usable = bool(
        report.get("market_check_passed") is True
        and expires is not None
        and current <= expires
        and age_seconds is not None
        and -5 <= age_seconds <= MAX_QUOTE_AGE_SECONDS
    )
    public = copy.deepcopy(report)
    public["age_seconds"] = round(age_seconds, 3) if age_seconds is not None else None
    public["usable"] = usable
    if not usable and report.get("status") == "passed":
        public["status"] = "expired"
        public["ready_for_manual_review"] = False
        planner = public.get("planner_candidate")
        if isinstance(planner, dict):
            planner["plan_ready"] = False
            planner["blockers"] = ["The saved Robinhood finalist check expired; run it again."]
    return public


def apply_finalist_check_to_sources(
    plan: Mapping[str, Any],
    cycle: Mapping[str, Any],
    queue: Mapping[str, Any],
    check: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Overlay one still-bound live quote onto in-memory queue/cycle copies."""
    current = _utc_now(now)
    cycle_copy = copy.deepcopy(dict(cycle))
    queue_copy = copy.deepcopy(dict(queue))
    status = {
        "schema": FINALIST_CHECK_SCHEMA,
        "applied": False,
        "reason": "finalist_check_missing_or_invalid",
        "generated_at": check.get("generated_at"),
    }
    if check.get("schema") != FINALIST_CHECK_SCHEMA or check.get("market_check_passed") is not True:
        return cycle_copy, queue_copy, status
    expires = _parse_timestamp(check.get("expires_at"))
    generated = _parse_timestamp(check.get("generated_at"))
    if (
        expires is None
        or generated is None
        or current > expires
        or (current - generated).total_seconds() > MAX_QUOTE_AGE_SECONDS
        or (current - generated).total_seconds() < -5
    ):
        status["reason"] = "finalist_check_expired"
        return cycle_copy, queue_copy, status
    bindings = (
        check.get("source_bindings") if isinstance(check.get("source_bindings"), Mapping) else {}
    )
    if bindings.get("queue_digest_sha256") != canonical_digest(queue) or bindings.get(
        "cycle_digest_sha256"
    ) != canonical_digest(cycle):
        status["reason"] = "finalist_check_source_changed"
        return cycle_copy, queue_copy, status
    candidate_check = check.get("candidate") if isinstance(check.get("candidate"), Mapping) else {}
    quote = check.get("quote") if isinstance(check.get("quote"), Mapping) else {}
    contract = check.get("contract") if isinstance(check.get("contract"), Mapping) else {}
    order = plan.get("order") if isinstance(plan.get("order"), Mapping) else {}
    plan_key = _identity_key(
        {
            "symbol": order.get("symbol"),
            "expiry": order.get("expiry"),
            "strike": order.get("strike"),
            "option_type": order.get("option_type"),
            "underlying_type": order.get("underlying_type"),
        }
    )
    if plan_key != _identity_key(candidate_check):
        status["reason"] = "finalist_check_identity_changed"
        return cycle_copy, queue_copy, status

    overlay = {
        "source_quote_at": quote.get("updated_at"),
        "source_quote_time_basis": "broker_exchange_quote_updated_at",
        "source_bid": quote.get("bid_price"),
        "source_ask": quote.get("ask_price"),
        "source_spread_pct": quote.get("spread_fraction"),
        "quote_quality": "live_broker",
        "data_delay": "real_time",
        "chain_source": "robinhood_mcp",
        "option_id": contract.get("option_id"),
        "open_interest": quote.get("open_interest"),
        "volume": quote.get("volume"),
        "delta": quote.get("delta"),
        "fresh_robinhood_quote_required": False,
        "research_quote_warnings": [],
        "robinhood_finalist_check_generated_at": check.get("generated_at"),
        "robinhood_finalist_check_digest_sha256": check.get("artifact_digest_sha256"),
    }
    wanted_digest = candidate_check.get("candidate_digest_sha256")
    queue_matches = 0
    cycle_matches = 0
    orders = queue_copy.get("orders")
    if isinstance(orders, list):
        for index, row in enumerate(orders):
            if (
                isinstance(row, Mapping)
                and _identity_key(row) == plan_key
                and canonical_digest(row) == wanted_digest
            ):
                orders[index] = {**dict(row), **overlay}
                queue_matches += 1
    for lane in ("manual_review_candidates", "review_only_entry_candidates"):
        rows = cycle_copy.get(lane)
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if (
                isinstance(row, Mapping)
                and _identity_key(row) == plan_key
                and canonical_digest(row) == wanted_digest
            ):
                rows[index] = {**dict(row), **overlay}
                cycle_matches += 1
    if queue_matches != 1 or cycle_matches != 1:
        status["reason"] = "finalist_check_candidate_binding_changed"
        return copy.deepcopy(dict(cycle)), copy.deepcopy(dict(queue)), status
    status.update(
        {
            "applied": True,
            "reason": "live_robinhood_quote_applied",
            "artifact_digest_sha256": check.get("artifact_digest_sha256"),
            "option_id": contract.get("option_id"),
            "quote_updated_at": quote.get("updated_at"),
            "quote_age_seconds": quote.get("age_seconds"),
            "bid_price": quote.get("bid_price"),
            "ask_price": quote.get("ask_price"),
            "spread_fraction": quote.get("spread_fraction"),
        }
    )
    return cycle_copy, queue_copy, status


__all__ = [
    "FINALIST_CHECK_FILE",
    "FINALIST_CHECK_SCHEMA",
    "FULL_CHAIN_EDGE_SCAN_FILE",
    "FULL_CHAIN_EDGE_SCAN_SCHEMA",
    "MAX_QUOTE_AGE_SECONDS",
    "TICKER_EDGE_SCAN_FILE",
    "TICKER_EDGE_SCAN_SCHEMA",
    "RobinhoodFinalistCheckError",
    "apply_finalist_check_to_sources",
    "canonical_digest",
    "check_best_option_finalist",
    "check_full_chain_option_edges",
    "check_top_option_finalists",
    "check_top_ticker_option_edges",
    "load_finalist_check_status",
]
