# Purpose: Normalize read-only Robinhood exports for the cockpit.
"""Normalize read-only Robinhood Agentic/MCP exports for Optedge.

This script does not connect to Robinhood and does not place orders. It turns a
raw JSON bundle of account/portfolio/position/order reads into the local
`data/robinhood_broker_snapshot.json` shape consumed by the cockpit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from risk.account_drawdown import (  # noqa: E402
    append_snapshot_observation,
    eligible_snapshot_account_keys,
    validate_equity_ledger,
)

DATA_DIR = ROOT / "data"
DEFAULT_INPUT = DATA_DIR / "robinhood_mcp_snapshot_raw.json"
DEFAULT_OUTPUT = DATA_DIR / "robinhood_broker_snapshot.json"
EQUITY_LEDGER_DIRNAME = "robinhood_account_equity_ledgers"
SNAPSHOT_SCHEMA = "optedge_robinhood_broker_snapshot_v1"
RAW_BUNDLE_SCHEMA = "optedge_robinhood_mcp_read_bundle_v2"
SAFE_PORTFOLIO_FIELDS = (
    "total_value",
    "market_value",
    "equity",
    "equity_value",
    "extended_hours_equity",
    "adjusted_equity_previous_close",
    "previous_close",
    "cash",
    "buying_power",
    "unleveraged_buying_power",
    "cash_available_for_withdrawal",
)
ROW_HINT_KEYS = {
    "account_number",
    "rhs_account_number",
    "brokerage_account_number",
    "agentic_allowed",
    "option_level",
    "symbol",
    "chain_symbol",
    "underlying_symbol",
    "ticker",
    "option_type",
    "strike_price",
    "expiration_date",
    "quantity",
    "average_buy_price",
    "total_value",
    "cash",
    "buying_power",
    "order_id",
    "state",
}
RAW_ACCOUNT_NUMBER_KEYS = frozenset({
    "account_number",
    "rhs_account_number",
    "brokerage_account_number",
    "account",
})
ACCOUNT_NUMBER_REDACTION = "[redacted-account]"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _float(value: Any, default: float | None = None) -> float | None:
    # ``bool`` is a subclass of ``int`` in Python.  Broker money/quantity
    # fields must never turn JSON true/false into $1/$0.
    if isinstance(value, bool):
        return default
    try:
        if value is None or value == "":
            return default
        out = float(value)
    except Exception:
        return default
    return out if math.isfinite(out) else default


def _first_finite_field(
    raw: dict[str, Any],
    fields: tuple[str, ...],
    default: float | None = None,
) -> float | None:
    for field in fields:
        if field not in raw or raw.get(field) is None or raw.get(field) == "":
            continue
        value = raw.get(field)
        number = None if isinstance(value, bool) else _float(value)
        if number is not None:
            return number
    return default


def _max_finite_field(
    raw: dict[str, Any],
    fields: tuple[str, ...],
) -> float | None:
    values = [
        number
        for field in fields
        if field in raw and raw.get(field) is not None and raw.get(field) != ""
        for number in (
            None
            if isinstance(raw.get(field), bool)
            else _float(raw.get(field)),
        )
        if number is not None
    ]
    return max(values) if values else None


def _numeric_field_issues(
    raw: dict[str, Any],
    fields: tuple[str, ...],
    *,
    label: str,
    require_positive: bool = False,
    reconcile_aliases: bool = False,
) -> list[str]:
    """Reject invalid critical values without rejecting valid quote alternatives."""
    observed: list[tuple[str, float]] = []
    invalid: list[str] = []
    for field in fields:
        if field not in raw or raw.get(field) is None or raw.get(field) == "":
            continue
        value = raw.get(field)
        number = None if isinstance(value, bool) else _float(value)
        if number is None or (require_positive and number <= 0):
            invalid.append(field)
        else:
            observed.append((field, number))
    issues = [f"invalid {label} field(s): {', '.join(invalid)}"] if invalid else []
    if reconcile_aliases and observed:
        reference = observed[0][1]
        if any(
            not math.isclose(number, reference, rel_tol=1e-9, abs_tol=1e-9)
            for _, number in observed[1:]
        ):
            issues.append(
                f"{label} fields disagree: "
                + ", ".join(field for field, _ in observed)
            )
    return issues


def _position_type_issues(
    raw: dict[str, Any],
    *,
    allowed: frozenset[str],
) -> list[str]:
    """Reject explicit unknown or contradictory long/short aliases."""
    observed: list[tuple[str, str]] = []
    invalid: list[str] = []
    for field in ("position_type", "type"):
        if field not in raw or raw.get(field) is None or raw.get(field) == "":
            continue
        value = _text(raw.get(field)).lower()
        if value not in allowed:
            invalid.append(field)
        else:
            observed.append((field, value))
    issues = [f"invalid position-type field(s): {', '.join(invalid)}"] if invalid else []
    if observed and any(value != observed[0][1] for _, value in observed[1:]):
        issues.append(
            "position-type fields disagree: "
            + ", ".join(field for field, _ in observed)
        )
    return issues


def _position_sign_issues(
    raw: dict[str, Any],
    *,
    unsigned_quantity_fields: tuple[str, ...],
) -> list[str]:
    """Reject sign/direction contradictions while allowing unsigned shorts."""
    directions = {
        _text(raw.get(field)).lower()
        for field in ("position_type", "type")
        if field in raw
        and raw.get(field) not in (None, "")
        and _text(raw.get(field)).lower() in {"long", "short"}
    }
    if len(directions) != 1:
        return []
    direction = next(iter(directions))
    signed = _first_finite_field(raw, ("signed_quantity",))
    if signed is not None and (
        (direction == "long" and signed < 0)
        or (direction == "short" and signed > 0)
    ):
        return ["signed quantity contradicts explicit position type"]
    if direction == "long":
        for field in unsigned_quantity_fields:
            value = _first_finite_field(raw, (field,))
            if value is not None and value < 0:
                return [f"negative {field} contradicts explicit long position type"]
    return []


def _position_numeric_issues(
    raw: dict[str, Any],
    quantity_fields: tuple[str, ...],
    *,
    pending_fields: tuple[str, ...] = (),
) -> list[str]:
    """Return fail-closed issues before lossy numeric normalization.

    Position quantities determine both duplicate exposure and the total-open
    capital cap.  Invalid values must not collapse to zero, and contradictory
    aliases must not be reduced to whichever field happens to be read first.
    """
    issues: list[str] = []
    observed: list[tuple[str, float]] = []
    invalid: list[str] = []
    for field in quantity_fields:
        if field not in raw or raw.get(field) is None or raw.get(field) == "":
            continue
        value = raw.get(field)
        number = None if isinstance(value, bool) else _float(value)
        if number is None:
            invalid.append(field)
        else:
            observed.append((field, number))
    if invalid:
        issues.append(f"invalid quantity field(s): {', '.join(invalid)}")
    if not observed and not invalid:
        issues.append("quantity is missing")
    if observed:
        reference = abs(observed[0][1])
        if any(
            not math.isclose(abs(number), reference, rel_tol=1e-9, abs_tol=1e-9)
            for _, number in observed[1:]
        ):
            issues.append(
                "quantity fields disagree: "
                + ", ".join(field for field, _ in observed)
            )

    invalid_pending: list[str] = []
    for field in pending_fields:
        if field not in raw or raw.get(field) is None or raw.get(field) == "":
            continue
        value = raw.get(field)
        if isinstance(value, bool) or _float(value) is None:
            invalid_pending.append(field)
    if invalid_pending:
        issues.append(
            f"invalid pending quantity field(s): {', '.join(invalid_pending)}"
        )
    return issues


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    raw = _text(value).lower()
    if raw in {"true", "1", "yes", "y"}:
        return True
    if raw in {"false", "0", "no", "n"}:
        return False
    return None


def _clean(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _supplied_account_numbers(value: Any) -> tuple[str, ...]:
    """Collect explicit raw account identifiers without guessing from other numbers."""
    found: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if str(key).strip().lower() in RAW_ACCOUNT_NUMBER_KEYS:
                    if isinstance(item, (str, int)) and not isinstance(item, bool):
                        candidate = _text(item)
                        if candidate:
                            found.add(candidate)
                visit(item)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(value)
    return tuple(sorted(found, key=lambda item: (-len(item), item)))


def _redact_account_number_text(text: str, account_numbers: tuple[str, ...]) -> str:
    """Redact exact account tokens while leaving unrelated numeric substrings intact."""
    redacted = text
    for account_number in account_numbers:
        pattern = re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(account_number)}(?![A-Za-z0-9])"
        )
        redacted = pattern.sub(ACCOUNT_NUMBER_REDACTION, redacted)
    return redacted


def _redact_supplied_account_numbers(
    value: Any,
    account_numbers: tuple[str, ...],
) -> Any:
    """Recursively redact raw account identifiers from public string keys and values."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            clean_key = _redact_account_number_text(str(key), account_numbers)
            if clean_key in redacted:
                raise ValueError(
                    "account-number redaction would create duplicate public snapshot keys"
                )
            redacted[clean_key] = _redact_supplied_account_numbers(item, account_numbers)
        return redacted
    if isinstance(value, list):
        return [
            _redact_supplied_account_numbers(item, account_numbers)
            for item in value
        ]
    if isinstance(value, str):
        return _redact_account_number_text(value, account_numbers)
    return value


def _assert_account_numbers_absent(
    value: Any,
    account_numbers: tuple[str, ...],
) -> None:
    """Fail closed if any full raw account identifier remains in public data."""
    leaks: set[str] = set()

    def inspect(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                inspect(str(key))
                inspect(item)
        elif isinstance(node, list):
            for item in node:
                inspect(item)
        elif isinstance(node, str):
            leaks.update(
                account_number
                for account_number in account_numbers
                if account_number in node
            )

    inspect(value)
    if leaks:
        raise ValueError(
            "normalized broker snapshot still contains a supplied raw account identifier"
        )


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_clean(payload), indent=2, sort_keys=True), encoding="utf-8")


def _write_json_atomic(path: Path, payload: Any) -> None:
    """Atomically replace one local ledger after its complete chain is sealed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    encoded = json.dumps(_clean(payload), indent=2, sort_keys=True)
    with temp.open("w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def default_account_equity_ledger_dir(data_dir: Path) -> Path:
    """Return a durable default without leaking test state outside custom dirs.

    The repository's real ``data/`` directory uses per-user application state
    so a checkout cleanup cannot silently erase the drawdown high-water mark.
    Explicit temporary or custom data directories stay self-contained.
    ``OPTEDGE_STATE_DIR`` is an explicit override for the real-data ledger
    directory.
    """
    candidate = Path(data_dir).expanduser()
    if candidate.resolve(strict=False) != DATA_DIR.resolve(strict=False):
        return candidate / EQUITY_LEDGER_DIRNAME

    override = os.environ.get("OPTEDGE_STATE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        base = (
            Path(local_app_data).expanduser()
            if local_app_data
            else Path.home() / "AppData" / "Local"
        )
        return base / "Optedge" / "risk"
    xdg_state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    base = (
        Path(xdg_state_home).expanduser()
        if xdg_state_home
        else Path.home() / ".local" / "state"
    )
    return base / "optedge" / "risk"


def account_equity_ledger_path(ledger_dir: Path, account_key: str) -> Path:
    """Map a pseudonymous account key to a non-identifying stable filename."""
    digest = hashlib.sha256(
        f"optedge-account-equity-ledger-v1|{account_key}".encode()
    ).hexdigest()
    return Path(ledger_dir) / f"account_{digest[:16]}.json"


def account_equity_ledger_backup_path(ledger_path: Path) -> Path:
    """Return the stable sidecar used to detect deletion and rollback."""
    path = Path(ledger_path)
    return path.with_name(f"{path.name}.bak")


def _load_consistent_account_equity_ledger(
    path: Path,
    *,
    allow_backup_lag: bool,
) -> dict[str, Any] | None:
    """Load a ledger whose primary and sidecar form one safe history.

    A write interrupted after replacing the primary can leave a valid backup
    that is a strict prefix.  Only the explicit append path may accept and
    repair that condition.  Review reads require the two durable copies to
    agree exactly.
    """
    backup_path = account_equity_ledger_backup_path(path)
    if not path.exists():
        if backup_path.exists():
            raise ValueError(
                "equity ledger primary is missing while its .bak exists; "
                "restore the primary or explicitly rebaseline"
            )
        return None

    primary = _read_json(path)
    primary_validation = validate_equity_ledger(primary)
    if primary_validation.get("valid") is not True:
        raise ValueError(
            "existing equity ledger is unsafe: "
            + "; ".join(primary_validation.get("blockers") or [])
        )
    if not backup_path.exists():
        observations = primary.get("observations")
        if isinstance(observations, list) and len(observations) >= 2:
            raise ValueError(
                "equity ledger .bak is missing for a multi-observation chain; "
                "restore the sidecar or explicitly rebaseline"
            )
        return primary

    backup = _read_json(backup_path)
    backup_validation = validate_equity_ledger(backup)
    if backup_validation.get("valid") is not True:
        raise ValueError(
            "equity ledger .bak is unsafe: "
            + "; ".join(backup_validation.get("blockers") or [])
        )

    primary_observations = primary.get("observations")
    backup_observations = backup.get("observations")
    assert isinstance(primary_observations, list)
    assert isinstance(backup_observations, list)
    backup_is_primary_prefix = (
        primary.get("account_key") == backup.get("account_key")
        and len(backup_observations) <= len(primary_observations)
        and primary_observations[: len(backup_observations)] == backup_observations
    )
    if not backup_is_primary_prefix:
        raise ValueError(
            "equity ledger .bak proves primary rollback or divergent history; "
            "restore the newest valid chain or explicitly rebaseline"
        )
    if len(backup_observations) < len(primary_observations) and not allow_backup_lag:
        raise ValueError(
            "equity ledger .bak lags the primary; rerun explicit normalization "
            "to reseal durable state before review"
        )
    return primary


def load_consistent_account_equity_ledger(path: Path) -> dict[str, Any] | None:
    """Fail-closed loader for account-loss evaluation and manual review."""
    return _load_consistent_account_equity_ledger(path, allow_backup_lag=False)


def _write_equity_ledger_with_backup(
    path: Path,
    previous: dict[str, Any] | None,
    ledger: dict[str, Any],
) -> None:
    """Atomically replace the primary and leave a newest-chain sidecar.

    The previous primary is written first so an interrupted primary replace is
    recoverable.  After the primary succeeds, the sidecar is advanced to the
    same newest chain.  In the normal state both copies therefore carry the
    latest tail, so replacing only the primary with its immediately preceding
    version is detectable instead of looking like a valid backup restore.
    """
    if previous is not None:
        _write_json_atomic(account_equity_ledger_backup_path(path), previous)
    _write_json_atomic(path, ledger)
    if previous is not None:
        _write_json_atomic(account_equity_ledger_backup_path(path), ledger)


def append_account_equity_ledgers(
    snapshot: dict[str, Any],
    ledger_dir: Path,
) -> dict[str, Any]:
    """Persist chained observations after an explicit normalized snapshot write.

    One file is maintained per pseudonymous account so every hash chain remains
    single-account.  Invalid or incomplete account state is never guessed and
    never overwrites an existing ledger.
    """
    appended = 0
    deduplicated = 0
    blockers: list[str] = []
    account_keys = eligible_snapshot_account_keys(snapshot)
    if not account_keys:
        blockers.append(
            "No account has a timezone-aware source time, pseudonymous key, and positive portfolio total_value."
        )
    for account_key in account_keys:
        path = account_equity_ledger_path(ledger_dir, account_key)
        try:
            existing = _load_consistent_account_equity_ledger(
                path,
                allow_backup_lag=True,
            )
            ledger, was_appended = append_snapshot_observation(
                existing,
                snapshot,
                account_key,
            )
            if was_appended:
                _write_equity_ledger_with_backup(path, existing, ledger)
            elif existing is not None:
                # A retry after an interrupted final sidecar replace can safely
                # reseal the already-validated primary without inventing data.
                backup_path = account_equity_ledger_backup_path(path)
                backup = _read_json(backup_path) if backup_path.exists() else None
                if backup != existing:
                    _write_json_atomic(backup_path, existing)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            blockers.append(f"{account_key}: {exc}")
            continue
        if was_appended:
            appended += 1
        else:
            deduplicated += 1
    return {
        "schema": "optedge_robinhood_account_equity_ledger_update_v1",
        "status": "updated" if appended and not blockers else "unchanged" if not blockers else "blocked",
        "eligible_account_count": len(account_keys),
        "observations_appended": appended,
        "identical_observations_deduplicated": deduplicated,
        "blockers": blockers,
        "does_not_place_orders": True,
    }


def _unwrap_rows(value: Any, preferred_keys: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Accept direct lists, paginated API shapes, or keyed account maps."""
    if value is None:
        return []
    if isinstance(value, list):
        rows: list[dict[str, Any]] = []
        page_keys = preferred_keys + (
            "results", "items", "accounts", "positions", "orders",
            "option_positions", "equity_positions", "instruments",
        )
        for row in value:
            if not isinstance(row, dict):
                continue
            is_mcp_page = isinstance(row.get("data"), dict) or any(
                isinstance(row.get(key), list) for key in page_keys
            )
            if is_mcp_page:
                rows.extend(_unwrap_rows(row, preferred_keys=preferred_keys))
            else:
                rows.append(row)
        return rows
    if isinstance(value, dict):
        for key in preferred_keys + (
            "results",
            "items",
            "accounts",
            "positions",
            "orders",
            "option_positions",
            "equity_positions",
            "data",
        ):
            rows = value.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        data_payload = value.get("data")
        if isinstance(data_payload, dict):
            nested_rows = _unwrap_rows(data_payload, preferred_keys=preferred_keys)
            if nested_rows:
                return [
                    {
                        **{
                            k: value[k]
                            for k in ("account_number", "rhs_account_number", "brokerage_account_number")
                            if value.get(k)
                        },
                        **row,
                    }
                    for row in nested_rows
                ]
            collection_keys = preferred_keys + (
                "results",
                "items",
                "accounts",
                "positions",
                "orders",
                "option_positions",
                "equity_positions",
                "instruments",
            )
            if any(isinstance(data_payload.get(key), list) for key in collection_keys):
                # A decoded MCP page with an explicit empty collection has zero rows;
                # the page metadata itself is not a position/order/instrument.
                return []
            payload_copy = dict(data_payload)
            for k in ("account_number", "rhs_account_number", "brokerage_account_number"):
                if value.get(k):
                    payload_copy.setdefault(k, value.get(k))
            return [payload_copy]
        if any(key in value for key in ROW_HINT_KEYS):
            return [value]
        rows: list[dict[str, Any]] = []
        for account_key, maybe_rows in value.items():
            if not isinstance(maybe_rows, (list, dict)):
                continue
            for row in _unwrap_rows(maybe_rows, preferred_keys=preferred_keys):
                copy = dict(row)
                copy.setdefault("account_number", account_key)
                rows.append(copy)
        if rows:
            return rows
        return [value]
    return []


def _account_number(raw: dict[str, Any], fallback: str = "") -> str:
    for key in (
        "account_number",
        "rhs_account_number",
        "brokerage_account_number",
        "account",
    ):
        text = _text(raw.get(key))
        if text:
            return text
    return fallback


def _account_mask(account_number: str, raw: dict[str, Any]) -> str:
    if account_number:
        return f"...{account_number[-4:]}"
    explicit = _text(raw.get("account_mask") or raw.get("mask"))
    if explicit:
        return f"...{explicit[-4:]}"
    return ""


def _account_key(account_number: str, raw: dict[str, Any]) -> str:
    """Return a stable non-secret pseudonymous key without persisting the account number."""
    explicit = _text(raw.get("account_key"))
    if not account_number and (
        explicit.startswith("acct_")
        and len(explicit) == 21
        and all(char in "0123456789abcdef" for char in explicit[5:])
    ):
        return explicit
    basis = account_number or "|".join([
        _text(raw.get("account_mask") or raw.get("mask")),
        _text(raw.get("label") or raw.get("nickname") or raw.get("name")),
        _text(raw.get("brokerage_account_type") or raw.get("type")),
    ])
    if not basis.strip("|"):
        return ""
    digest = hashlib.sha256(f"optedge-robinhood-account-v1|{basis}".encode()).hexdigest()
    return f"acct_{digest[:16]}"


def _sanitize_portfolio(raw: Any) -> dict[str, Any]:
    """Keep only numeric portfolio totals needed by the local readiness view."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key in SAFE_PORTFOLIO_FIELDS:
        value = raw.get(key)
        if isinstance(value, dict):
            nested = value
            value = None
            for nested_key in (key, "amount", "value"):
                candidate = nested.get(nested_key)
                if candidate not in (None, ""):
                    value = candidate
                    break
        number = _float(value)
        if number is not None:
            out[key] = number
    return out


def _conservative_buying_power(*values: Any) -> float | None:
    """Return the smallest explicit broker buying-power figure, never cash."""
    numbers = [number for number in (_float(value) for value in values) if number is not None]
    return min(numbers) if numbers else None


def _normalize_account(raw: dict[str, Any], fallback_number: str = "") -> dict[str, Any]:
    account_number = _account_number(raw, fallback_number)
    agentic_allowed = _bool(raw.get("agentic_allowed"))
    option_level = _text(
        raw.get("option_level")
        or raw.get("options_level")
        or raw.get("option_approval_level")
        or raw.get("account_option_level")
    )
    buying_power_raw = raw.get("buying_power")
    if isinstance(buying_power_raw, dict):
        buying_power_value = _conservative_buying_power(
            buying_power_raw.get("buying_power"),
            buying_power_raw.get("unleveraged_buying_power"),
        )
    else:
        buying_power_value = _conservative_buying_power(
            buying_power_raw,
            raw.get("unleveraged_buying_power"),
        )
    state = _text(raw.get("state") or raw.get("status"))
    deactivated = _bool(raw.get("deactivated"))
    if not state and deactivated is not None:
        state = "deactivated" if deactivated else "active"
    return {
        "account_key": _account_key(account_number, raw),
        "account_mask": _account_mask(account_number, raw),
        "label": _text(raw.get("label") or raw.get("nickname") or raw.get("name")),
        "brokerage_account_type": _text(raw.get("brokerage_account_type") or raw.get("type")),
        "state": state,
        "agentic_allowed": agentic_allowed if agentic_allowed is not None else False,
        "option_level": option_level,
        "buying_power": _float(buying_power_value),
        "portfolio": _sanitize_portfolio(raw.get("portfolio")),
        "equity_positions": [],
        "option_positions": [],
        "equity_orders": [],
        "option_orders": [],
    }


def _find_symbol(raw: dict[str, Any]) -> str:
    return _text(
        raw.get("symbol")
        or raw.get("chain_symbol")
        or raw.get("underlying_symbol")
        or raw.get("ticker")
        or raw.get("ticker_or_symbol")
        or raw.get("instrument_symbol")
    ).upper()


def _option_side(raw: dict[str, Any]) -> str:
    text = _text(
        raw.get("option_type")
        or raw.get("side")
        or raw.get("right")
        or raw.get("type")
    ).lower()
    if text.startswith("c"):
        return "call"
    if text.startswith("p"):
        return "put"
    return text


def _option_right(raw: dict[str, Any]) -> str:
    """Return call/put without confusing execution side buy/sell for contract right."""
    text = _text(raw.get("option_type") or raw.get("right")).lower()
    if not text:
        candidate = _text(raw.get("type")).lower()
        text = candidate if candidate in {"call", "put", "c", "p"} else ""
    if text.startswith("c"):
        return "call"
    if text.startswith("p"):
        return "put"
    return ""


def _expiration(raw: dict[str, Any]) -> str:
    return _text(
        raw.get("expiration_date")
        or raw.get("expiry")
        or raw.get("expiration")
        or raw.get("lastTradeDateOrContractMonth")
    )[:10]


def _normalize_equity_position(raw: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
    quantity = _first_finite_field(
        raw,
        ("signed_quantity", "quantity", "shares", "qty"),
        0.0,
    ) or 0.0
    position_type = _text(raw.get("position_type") or raw.get("type")).lower()
    if position_type not in {"long", "short", "boxed", "empty"}:
        position_type = "short" if quantity < 0 else "long" if quantity > 0 else "empty"
    avg = _float(raw.get("average_buy_price") or raw.get("average_price") or raw.get("avg_price"))
    current = _max_finite_field(
        raw,
        ("current_price", "mark_price", "last_price"),
    )
    market_value = _first_finite_field(raw, ("market_value",))
    return {
        "symbol": _find_symbol(raw),
        "quantity": quantity,
        "signed_quantity": quantity,
        "position_type": position_type,
        "average_buy_price": avg,
        "average_price": avg,
        "current_price": current,
        "market_value": (
            market_value
            if market_value not in (None, 0)
            else quantity * current
            if current is not None
            else None
        ),
        "account_mask": account.get("account_mask"),
        "account_key": account.get("account_key"),
        "account_label": account.get("label"),
        "account_agentic_allowed": account.get("agentic_allowed"),
    }


def _normalize_option_position(raw: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
    quantity = _first_finite_field(
        raw,
        ("signed_quantity", "quantity", "contracts", "qty"),
        0.0,
    ) or 0.0
    avg = _float(raw.get("average_price") or raw.get("avg_price") or raw.get("average_buy_price"))
    current = _max_finite_field(
        raw,
        ("current_price", "mark_price", "last_price", "adjusted_mark_price"),
    )
    option_type = _option_right(raw)
    raw_position_type = _text(raw.get("position_type") or raw.get("type")).lower()
    position_type = raw_position_type if raw_position_type in {"long", "short"} else ""
    signed_quantity = -abs(quantity) if position_type == "short" else abs(quantity)
    return {
        "symbol": _find_symbol(raw),
        "chain_symbol": _find_symbol(raw),
        "option_type": option_type,
        "side": option_type,
        "position_type": position_type,
        "strike_price": _float(raw.get("strike_price") or raw.get("strike")),
        "expiration_date": _expiration(raw),
        "quantity": quantity,
        "signed_quantity": signed_quantity,
        "state": _text(raw.get("state") or raw.get("status") or raw.get("position_state")),
        "average_price": avg,
        "current_price": current,
        "mark_price": current,
        "bid_price": _max_finite_field(raw, ("bid_price", "bid")),
        "ask_price": _max_finite_field(raw, ("ask_price", "ask")),
        "instrument_id": _text(raw.get("instrument_id") or raw.get("option_id") or raw.get("id")),
        "instrument_state": _text(raw.get("option_state") or raw.get("instrument_state")),
        "tradability": _text(raw.get("tradability")),
        "underlying_type": _text(raw.get("underlying_type")),
        "chain_id": _text(raw.get("chain_id")),
        "trade_value_multiplier": _first_finite_field(
            raw,
            ("trade_value_multiplier", "multiplier"),
        ),
        "pending_buy_quantity": _float(raw.get("pending_buy_quantity")),
        "pending_sell_quantity": _float(raw.get("pending_sell_quantity")),
        "pending_exercise_quantity": _float(raw.get("pending_exercise_quantity")),
        "pending_assignment_quantity": _float(raw.get("pending_assignment_quantity")),
        "pending_expiration_quantity": _float(raw.get("pending_expiration_quantity")),
        "account_label": account.get("label") or account.get("account_mask"),
        "account_key": account.get("account_key"),
        "account_agentic_allowed": account.get("agentic_allowed"),
        "account_option_level": account.get("option_level"),
    }


def _normalize_order(raw: dict[str, Any], account: dict[str, Any], asset: str) -> dict[str, Any]:
    raw_legs = raw.get("legs")
    legs = raw_legs if isinstance(raw_legs, list) else []
    option_legs = [leg for leg in legs if isinstance(leg, dict)]
    exact_single_leg = (
        asset != "option"
        or (len(legs) == 1 and isinstance(legs[0], dict))
    )
    leg = option_legs[0] if exact_single_leg and option_legs else {}
    row = {
        "asset": asset,
        "order_id": _text(raw.get("order_id") or raw.get("id")),
        "symbol": _find_symbol(raw) or _find_symbol(leg),
        "state": _text(raw.get("state") or raw.get("status")),
        "side": _text(raw.get("side") or leg.get("side")),
        "position_effect": _text(raw.get("position_effect") or leg.get("position_effect")),
        "quantity": raw.get("quantity"),
        "price": raw.get("price") or raw.get("limit_price"),
        "created_at": raw.get("created_at") or raw.get("created_at_utc"),
        "placed_agent": raw.get("placed_agent") or raw.get("source"),
        "account_mask": account.get("account_mask"),
        "account_key": account.get("account_key"),
    }
    if asset == "option":
        contract_identity_status = (
            "exact_single_leg"
            if exact_single_leg
            else "unresolved_malformed_legs"
            if any(not isinstance(value, dict) for value in legs)
            else "unresolved_multi_leg"
            if len(legs) > 1
            else "unresolved_missing_leg"
        )
        row.update({
            "chain_symbol": _find_symbol(raw) or _find_symbol(leg),
            "option_type": _option_right(leg) or _option_right(raw) if exact_single_leg else "",
            "expiration_date": _expiration(leg) or _expiration(raw) if exact_single_leg else "",
            "strike_price": (
                _float(
                    leg.get("strike_price")
                    or leg.get("strike")
                    or raw.get("strike_price")
                    or raw.get("strike")
                )
                if exact_single_leg
                else None
            ),
            "option_id": (
                _text(leg.get("option_id") or leg.get("instrument_id") or raw.get("option_id"))
                if exact_single_leg
                else ""
            ),
            "leg_count": len(legs),
            "contract_identity_status": contract_identity_status,
            "contract_identity_blocker": (
                "Nonterminal option orders require exactly one leg for exact duplicate-exposure checks."
                if not exact_single_leg
                else ""
            ),
            "pending_quantity": raw.get("pending_quantity"),
            "processed_quantity": raw.get("processed_quantity"),
        })
    return row


def _pick_account(accounts: dict[str, dict[str, Any]], account_number: str, fallback: str) -> dict[str, Any]:
    key = account_number or fallback
    if key not in accounts:
        accounts[key] = _normalize_account({"account_number": key}, key)
    return accounts[key]


def _merge_portfolio(account: dict[str, Any], raw: dict[str, Any]) -> None:
    portfolio = _sanitize_portfolio(raw)
    buying_power = raw.get("buying_power")
    if isinstance(buying_power, dict):
        nested = buying_power
        leveraged = _float(nested.get("buying_power"))
        unleveraged = _float(nested.get("unleveraged_buying_power"))
        if leveraged is not None:
            portfolio["buying_power"] = leveraged
        if unleveraged is not None:
            portfolio["unleveraged_buying_power"] = unleveraged
        buying_power = _conservative_buying_power(leveraged, unleveraged)
    else:
        buying_power = _conservative_buying_power(
            buying_power,
            raw.get("unleveraged_buying_power"),
        )
    account["portfolio"] = portfolio
    if buying_power is not None:
        existing = _float(account.get("buying_power"))
        account["buying_power"] = min(existing, buying_power) if existing is not None else buying_power


def _option_instrument_lookup(*sources: Any) -> dict[str, dict[str, Any]]:
    """Index direct instrument rows or one/more complete MCP result pages."""
    lookup: dict[str, dict[str, Any]] = {}
    pending = list(sources)
    while pending:
        source = pending.pop()
        if isinstance(source, list):
            pending.extend(source)
            continue
        if not isinstance(source, dict):
            continue
        instrument_id = _text(
            source.get("id") or source.get("option_id") or source.get("instrument_id")
        )
        if instrument_id and (
            source.get("chain_symbol")
            or source.get("strike_price") is not None
            or source.get("expiration_date")
        ):
            lookup[instrument_id] = source
            continue
        nested_found = False
        for key in ("data", "instruments", "results", "items"):
            child = source.get(key)
            if isinstance(child, (dict, list)):
                pending.append(child)
                nested_found = True
        if not nested_found:
            pending.extend(value for value in source.values() if isinstance(value, (dict, list)))
    return lookup


def _page_next(page: dict[str, Any]) -> str:
    data = page.get("data") if isinstance(page.get("data"), dict) else page
    return _text(data.get("next")) if isinstance(data, dict) else ""


def _page_request_cursor(page: dict[str, Any]) -> str:
    for key in ("request_cursor", "cursor"):
        value = _text(page.get(key))
        if value:
            return value
    for key in ("request", "request_args", "arguments"):
        request = page.get(key)
        if isinstance(request, dict):
            value = _text(request.get("cursor"))
            if value:
                return value
    return ""


def _cursor_from_next(next_url: str) -> str:
    try:
        return _text((parse_qs(urlparse(next_url).query).get("cursor") or [""])[0])
    except Exception:
        return ""


def _decoded_response_shape_issue(
    response: Any,
    *,
    collection_key: str | None = None,
) -> str:
    """Require the exact decoded MCP data envelope for a read response."""
    raw_pages = response if isinstance(response, list) else [response]
    if not raw_pages:
        return "page list is empty"
    if any(not isinstance(page, dict) for page in raw_pages):
        return "page list contains a non-object entry"
    for index, page in enumerate(raw_pages, start=1):
        data = page.get("data")
        if not isinstance(data, dict):
            return f"page {index} is missing its decoded data object"
        if collection_key:
            collection = data.get(collection_key)
            if not isinstance(collection, list):
                return f"page {index} data.{collection_key} must be a list"
            if any(not isinstance(row, dict) for row in collection):
                return f"page {index} data.{collection_key} contains a non-object entry"
    return ""


def _pagination_capture_issue(
    response: Any,
    *,
    require_explicit_next: bool = False,
) -> str:
    """Validate an ordered capture of one or more decoded MCP pages.

    Positions, orders, and instrument reads are paginated contracts. Their
    capture is only provably complete when every decoded page retains an
    explicit ``data.next`` field and the final value is JSON null.
    """
    raw_pages = response if isinstance(response, list) else [response]
    if isinstance(response, list) and any(not isinstance(page, dict) for page in raw_pages):
        return "page list contains a non-object entry"
    pages = [page for page in raw_pages if isinstance(page, dict)]
    if not pages:
        return ""
    if require_explicit_next:
        for index, page in enumerate(pages, start=1):
            data = page.get("data")
            if not isinstance(data, dict):
                return f"page {index} is missing its decoded data object"
            if "next" not in data:
                return f"page {index} is missing explicit data.next"
    for index, page in enumerate(pages[:-1]):
        data = page.get("data") if isinstance(page.get("data"), dict) else page
        next_value = data.get("next") if isinstance(data, dict) else None
        next_url = _text(next_value)
        if next_value is None or not next_url:
            return "a captured page terminates before the final page"
        expected_cursor = _cursor_from_next(next_url)
        actual_cursor = _page_request_cursor(pages[index + 1])
        if expected_cursor:
            if not actual_cursor:
                return "follow-up page is missing request.cursor linkage metadata"
            if expected_cursor != actual_cursor:
                return "captured page cursor linkage is out of order"
    final_data = (
        pages[-1].get("data")
        if isinstance(pages[-1].get("data"), dict)
        else pages[-1]
    )
    final_next = final_data.get("next") if isinstance(final_data, dict) else None
    if require_explicit_next and final_next is not None:
        return "final data.next is non-null"
    if not require_explicit_next and _text(final_next):
        return "final data.next is non-null"
    return ""


def _enrich_option_contract(
    raw: dict[str, Any],
    instruments: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Join position/order rows to exact contract metadata by option UUID."""
    out = dict(raw)
    option_id = _text(raw.get("option_id") or raw.get("instrument_id"))
    instrument = instruments.get(option_id) if option_id else None
    if not isinstance(instrument, dict):
        return out
    if not _text(out.get("option_id")):
        out["option_id"] = option_id
    if not _find_symbol(out):
        out["chain_symbol"] = instrument.get("chain_symbol") or instrument.get("symbol")
    if not _option_right(out):
        out["option_type"] = instrument.get("type") or instrument.get("option_type")
    if _float(out.get("strike_price") or out.get("strike")) is None:
        out["strike_price"] = instrument.get("strike_price") or instrument.get("strike")
    if not _expiration(out):
        out["expiration_date"] = instrument.get("expiration_date") or instrument.get("expiry")
    if not _text(out.get("option_state")):
        out["option_state"] = instrument.get("state")
    if not _text(out.get("tradability")):
        out["tradability"] = instrument.get("tradability")
    if not _text(out.get("underlying_type")):
        out["underlying_type"] = instrument.get("underlying_type")
    if not _text(out.get("chain_id")):
        out["chain_id"] = instrument.get("chain_id")
    return out


def _enrich_option_order(
    raw: dict[str, Any],
    instruments: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    out = dict(raw)
    legs = raw.get("legs") if isinstance(raw.get("legs"), list) else []
    out["legs"] = [
        _enrich_option_contract(leg, instruments) if isinstance(leg, dict) else leg
        for leg in legs
    ]
    return _enrich_option_contract(out, instruments)


def normalize_broker_snapshot(
    raw: Any,
    *,
    generated_at: str | None = None,
    account_number: str = "",
) -> dict[str, Any]:
    """Return a cockpit-compatible broker snapshot from a flexible raw bundle."""
    bundle = raw if isinstance(raw, dict) else {"accounts": raw}
    fallback_account = _text(account_number)
    is_v2_bundle = _text(bundle.get("schema")) == RAW_BUNDLE_SCHEMA
    normalization_blockers: list[str] = []
    if is_v2_bundle:
        accounts_shape_issue = _decoded_response_shape_issue(
            bundle.get("get_accounts"),
            collection_key="accounts",
        )
        if accounts_shape_issue:
            normalization_blockers.append(
                f"get_accounts capture has an invalid decoded shape: {accounts_shape_issue}."
            )
    account_rows = _unwrap_rows(bundle.get("accounts") or bundle.get("get_accounts"), ("accounts", "results"))
    if is_v2_bundle and not account_rows:
        normalization_blockers.append(
            f"{RAW_BUNDLE_SCHEMA} requires the complete get_accounts result before account-scoped reads."
        )
    if is_v2_bundle and account_rows:
        account_identities = [_account_number(row) for row in account_rows]
        blank_identity_count = sum(1 for value in account_identities if not value)
        if blank_identity_count:
            normalization_blockers.append(
                "get_accounts capture contains "
                f"{blank_identity_count} row(s) without a stable account identity."
            )
        nonempty_identities = [value for value in account_identities if value]
        if len(nonempty_identities) != len(set(nonempty_identities)):
            normalization_blockers.append(
                "get_accounts capture contains duplicate account identities; "
                "account scope is ambiguous."
            )
    accounts: dict[str, dict[str, Any]] = {}
    for row in account_rows:
        raw_account_number = _account_number(row, fallback_account)
        acct = _normalize_account(row, fallback_account)
        key = raw_account_number or fallback_account or acct.get("account_mask") or "account"
        accounts[str(key)] = acct
    if not accounts:
        key = fallback_account or "snapshot"
        accounts[key] = _normalize_account({"account_number": key}, key)
    known_account_keys = list(accounts)
    scoped_value = bundle.get("account_snapshots") or bundle.get("account_reads")
    account_scopes = _unwrap_rows(scoped_value, ("account_snapshots", "account_reads")) if scoped_value else []
    if is_v2_bundle and not account_scopes:
        normalization_blockers.append(
            f"{RAW_BUNDLE_SCHEMA} requires non-empty account_snapshots with an account_number on every wrapper."
        )
    if is_v2_bundle:
        pagination_issue = _pagination_capture_issue(bundle.get("get_accounts"))
        if pagination_issue:
            normalization_blockers.append(f"get_accounts capture is incomplete: {pagination_issue}.")

    if is_v2_bundle and account_scopes:
        required_scoped_reads = (
            "get_portfolio",
            "get_equity_positions",
            "get_option_positions",
            "get_equity_orders",
            "get_option_orders",
        )
        scoped_collection_keys = {
            "get_equity_positions": "positions",
            "get_option_positions": "positions",
            "get_equity_orders": "orders",
            "get_option_orders": "orders",
            "get_option_instruments": "instruments",
        }
        forbidden_top_level_reads = [
            section for section in required_scoped_reads if section in bundle
        ]
        if forbidden_top_level_reads:
            normalization_blockers.append(
                "V2 account-scoped read section(s) must not appear at the top level when "
                "account_snapshots wrappers exist: "
                + ", ".join(forbidden_top_level_reads)
                + "."
            )
        scoped_account_numbers = [
            _account_number(scope) for scope in account_scopes if _account_number(scope)
        ]
        missing_account_scope_count = sum(
            1 for key in known_account_keys if key not in scoped_account_numbers
        )
        if missing_account_scope_count:
            normalization_blockers.append(
                f"account_snapshots is missing complete scoped reads for "
                f"{missing_account_scope_count} get_accounts account(s)."
            )
        if len(scoped_account_numbers) != len(set(scoped_account_numbers)):
            normalization_blockers.append(
                "account_snapshots contains duplicate account_number wrappers; account scope is ambiguous."
            )
        unknown_scope_count = sum(
            1 for account_key in scoped_account_numbers if account_key not in known_account_keys
        )
        if unknown_scope_count:
            normalization_blockers.append(
                f"account_snapshots contains {unknown_scope_count} wrapper(s) not present in get_accounts."
            )
        for scope in account_scopes:
            missing_sections = [
                section
                for section in required_scoped_reads
                if section not in scope
            ]
            if missing_sections:
                normalization_blockers.append(
                    "An account_snapshots wrapper is missing required scoped read section(s): "
                    + ", ".join(missing_sections)
                    + "."
                )
            for section in (*required_scoped_reads, "get_option_instruments"):
                if section in scope:
                    shape_issue = _decoded_response_shape_issue(
                        scope.get(section),
                        collection_key=scoped_collection_keys.get(section),
                    )
                    if shape_issue:
                        normalization_blockers.append(
                            f"{section} capture has an invalid decoded shape: {shape_issue}."
                        )
                        continue
                    pagination_issue = _pagination_capture_issue(
                        scope.get(section),
                        require_explicit_next=section in scoped_collection_keys,
                    )
                    if pagination_issue:
                        normalization_blockers.append(
                            f"{section} capture is incomplete: {pagination_issue}."
                        )
    if is_v2_bundle:
        if "get_option_instruments" in bundle:
            shape_issue = _decoded_response_shape_issue(
                bundle.get("get_option_instruments"),
                collection_key="instruments",
            )
            if shape_issue:
                normalization_blockers.append(
                    "get_option_instruments capture has an invalid decoded shape: "
                    f"{shape_issue}."
                )
            else:
                pagination_issue = _pagination_capture_issue(
                    bundle.get("get_option_instruments"),
                    require_explicit_next=True,
                )
                if pagination_issue:
                    normalization_blockers.append(
                        f"get_option_instruments capture is incomplete: {pagination_issue}."
                    )

    instrument_sources = [
        bundle.get("option_instruments"),
        bundle.get("get_option_instruments"),
    ]
    for scope in account_scopes:
        instrument_sources.extend([
            scope.get("option_instruments"),
            scope.get("get_option_instruments"),
        ])
    instruments = _option_instrument_lookup(*instrument_sources)

    def resolve_account(row: dict[str, Any], forced_account: str, section: str) -> str:
        row_account = _account_number(row)
        if forced_account and row_account and row_account != forced_account:
            normalization_blockers.append(
                f"A {section} row conflicts with its account_snapshots wrapper; account scope is untrusted."
            )
            return "unscoped"
        explicit = forced_account or row_account
        if explicit:
            return explicit
        if fallback_account:
            return fallback_account
        if len(known_account_keys) == 1:
            return str(known_account_keys[0])
        normalization_blockers.append(
            f"{section} rows are not account-scoped; wrap the tool response with its request account_number."
        )
        return "unscoped"

    def merge_portfolio_source(raw_value: Any, forced_account: str = "") -> None:
        for raw_portfolio in _unwrap_rows(raw_value):
            acct_num = resolve_account(raw_portfolio, forced_account, "portfolio")
            _merge_portfolio(_pick_account(accounts, acct_num, "unscoped"), raw_portfolio)

    def attach_rows(
        raw_value: Any,
        attr: str,
        normalizer: Any,
        asset: str | None = None,
        *,
        forced_account: str = "",
        option_contracts: bool = False,
    ) -> None:
        for row_index, raw_row in enumerate(_unwrap_rows(raw_value), start=1):
            numeric_issues: list[str] = []
            if attr == "equity_positions":
                numeric_issues = _position_numeric_issues(
                    raw_row,
                    ("signed_quantity", "quantity", "shares", "qty"),
                )
                numeric_issues.extend(
                    _position_type_issues(
                        raw_row,
                        allowed=frozenset({"long", "short", "boxed", "empty"}),
                    )
                )
                numeric_issues.extend(
                    _position_sign_issues(
                        raw_row,
                        unsigned_quantity_fields=("quantity", "shares", "qty"),
                    )
                )
                numeric_issues.extend(
                    _numeric_field_issues(
                        raw_row,
                        ("current_price", "mark_price", "last_price"),
                        label="current price",
                        require_positive=True,
                    )
                )
                numeric_issues.extend(
                    _numeric_field_issues(
                        raw_row,
                        ("market_value",),
                        label="market value",
                    )
                )
            elif attr == "option_positions":
                numeric_issues = _position_numeric_issues(
                    raw_row,
                    ("signed_quantity", "quantity", "contracts", "qty"),
                    pending_fields=(
                        "pending_buy_quantity",
                        "pending_sell_quantity",
                        "pending_exercise_quantity",
                        "pending_assignment_quantity",
                        "pending_expiration_quantity",
                    ),
                )
                numeric_issues.extend(
                    _position_type_issues(
                        raw_row,
                        allowed=frozenset({"long", "short"}),
                    )
                )
                numeric_issues.extend(
                    _position_sign_issues(
                        raw_row,
                        unsigned_quantity_fields=("quantity", "contracts", "qty"),
                    )
                )
                numeric_issues.extend(
                    _numeric_field_issues(
                        raw_row,
                        (
                            "current_price",
                            "mark_price",
                            "last_price",
                            "adjusted_mark_price",
                        ),
                        label="current mark",
                        require_positive=True,
                    )
                )
                numeric_issues.extend(
                    _numeric_field_issues(
                        raw_row,
                        ("ask_price", "ask"),
                        label="ask price",
                        require_positive=True,
                        reconcile_aliases=True,
                    )
                )
                numeric_issues.extend(
                    _numeric_field_issues(
                        raw_row,
                        ("trade_value_multiplier", "multiplier"),
                        label="trade-value multiplier",
                        require_positive=True,
                        reconcile_aliases=True,
                    )
                )
            normalization_blockers.extend(
                f"{attr} row {row_index} is unsafe: {issue}."
                for issue in numeric_issues
            )
            acct_num = resolve_account(raw_row, forced_account, attr)
            account = _pick_account(accounts, acct_num, "unscoped")
            row = dict(raw_row)
            if option_contracts:
                row = (
                    _enrich_option_order(row, instruments)
                    if asset == "option"
                    else _enrich_option_contract(row, instruments)
                )
            if asset:
                normalized = normalizer(row, account, asset)
            else:
                normalized = normalizer(row, account)
            if numeric_issues:
                # Keep the malformed row visible but make its canonical
                # quantity unusable. This remains fail-closed even if a caller
                # accidentally strips the snapshot-level blocker list.
                normalized["quantity"] = None
                normalized["signed_quantity"] = None
                normalized["position_validation_errors"] = list(numeric_issues)
            account[attr].append(normalized)

    def attach_account_scope(scope: dict[str, Any]) -> None:
        scoped_account = _account_number(scope)
        if not scoped_account:
            normalization_blockers.append(
                "An account_snapshots wrapper is missing account_number; its reads cannot prove same-account readiness."
            )
            scoped_account = resolve_account(scope, "", "account_snapshots")
        _pick_account(accounts, scoped_account, "unscoped")
        merge_portfolio_source(
            scope.get("portfolio") or scope.get("get_portfolio"),
            scoped_account,
        )
        attach_rows(
            scope.get("equity_positions") or scope.get("get_equity_positions"),
            "equity_positions", _normalize_equity_position,
            forced_account=scoped_account,
        )
        attach_rows(
            scope.get("option_positions") or scope.get("get_option_positions"),
            "option_positions", _normalize_option_position,
            forced_account=scoped_account, option_contracts=True,
        )
        attach_rows(
            scope.get("equity_orders") or scope.get("get_equity_orders"),
            "equity_orders", _normalize_order, "equity",
            forced_account=scoped_account,
        )
        attach_rows(
            scope.get("option_orders") or scope.get("get_option_orders"),
            "option_orders", _normalize_order, "option",
            forced_account=scoped_account, option_contracts=True,
        )

    if account_scopes:
        for scope in account_scopes:
            attach_account_scope(scope)
    else:
        merge_portfolio_source(
            bundle.get("portfolio") or bundle.get("portfolios") or bundle.get("get_portfolio")
        )
        attach_rows(
            bundle.get("equity_positions")
            or bundle.get("stock_positions")
            or bundle.get("get_equity_positions"),
            "equity_positions", _normalize_equity_position,
        )
        attach_rows(
            bundle.get("option_positions")
            or bundle.get("options_positions")
            or bundle.get("get_option_positions"),
            "option_positions", _normalize_option_position,
            option_contracts=True,
        )
        attach_rows(
            bundle.get("equity_orders") or bundle.get("get_equity_orders"),
            "equity_orders", _normalize_order, "equity",
        )
        attach_rows(
            bundle.get("option_orders") or bundle.get("get_option_orders"),
            "option_orders", _normalize_order, "option",
            option_contracts=True,
        )

    account_list = list(accounts.values())
    option_positions = [pos for account in account_list for pos in account.get("option_positions", [])]
    equity_positions = [pos for account in account_list for pos in account.get("equity_positions", [])]
    option_orders = [row for account in account_list for row in account.get("option_orders", [])]
    equity_orders = [row for account in account_list for row in account.get("equity_orders", [])]
    missing_option_contract_count = sum(
        1 for row in option_positions
        if _float(row.get("quantity"), 0.0) != 0
        and (
            not _find_symbol(row)
            or _option_right(row) not in {"call", "put"}
            or _float(row.get("strike_price")) is None
            or not _expiration(row)
        )
    )
    if missing_option_contract_count:
        normalization_blockers.append(
            f"{missing_option_contract_count} open option position(s) lack exact instrument metadata; "
            "include get_option_instruments results keyed by option_id."
        )
    normalization_blockers = list(dict.fromkeys(normalization_blockers))
    return {
        "schema": SNAPSHOT_SCHEMA,
        "generated_at": generated_at or _text(bundle.get("generated_at") or bundle.get("collected_at")) or None,
        "normalized_at": _now(),
        "source": "read_only_robinhood_agentic_mcp_export",
        "raw_bundle_schema": _text(bundle.get("schema")) or "legacy_flexible_bundle",
        "does_not_place_orders": True,
        "normalization_blockers": normalization_blockers,
        "accounts": account_list,
        "option_positions": option_positions,
        "equity_positions": equity_positions,
        "option_orders": option_orders,
        "equity_orders": equity_orders,
        "counts": {
            "accounts": len(account_list),
            "option_positions": len(option_positions),
            "equity_positions": len(equity_positions),
            "option_orders": len(option_orders),
            "equity_orders": len(equity_orders),
            "missing_option_contracts": missing_option_contract_count,
        },
        "notes": [
            "Normalized from read-only broker/account data for local reconciliation.",
            "This file is not broker confirmation and cannot place, cancel, or replace orders.",
            f"For multiple accounts, use {RAW_BUNDLE_SCHEMA} account_snapshots wrappers so every read remains account-scoped.",
        ],
    }


def persist_broker_snapshot_bundle(
    raw_bundle: dict[str, Any],
    *,
    output_path: Path = DEFAULT_OUTPUT,
    ledger_dir: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Persist one in-memory V2 read bundle without ever writing its raw form.

    The normalized public snapshot is recursively scrubbed and verified before
    an atomic replace.  Pseudonymous account-equity ledgers are updated only
    after that replace succeeds.  This helper performs no broker connection or
    order action and deliberately accepts only the account-scoped V2 bundle.
    """
    if not isinstance(raw_bundle, dict) or _text(raw_bundle.get("schema")) != RAW_BUNDLE_SCHEMA:
        raise ValueError(f"raw_bundle must use schema {RAW_BUNDLE_SCHEMA}")

    account_numbers = _supplied_account_numbers(raw_bundle)
    snapshot = normalize_broker_snapshot(
        raw_bundle,
        generated_at=generated_at,
    )
    public_snapshot = _redact_supplied_account_numbers(snapshot, account_numbers)
    _assert_account_numbers_absent(public_snapshot, account_numbers)

    destination = Path(output_path)
    resolved_ledger_dir = (
        Path(ledger_dir)
        if ledger_dir is not None
        else default_account_equity_ledger_dir(destination.parent)
    )
    _write_json_atomic(destination, public_snapshot)
    ledger_update = append_account_equity_ledgers(
        public_snapshot,
        resolved_ledger_dir,
    )
    return {
        "schema": "optedge_robinhood_broker_snapshot_persistence_v1",
        "output": str(destination),
        "equity_ledger_dir": str(resolved_ledger_dir),
        "snapshot": public_snapshot,
        "equity_ledger_update": ledger_update,
        "raw_bundle_written": False,
        "does_not_place_orders": True,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Normalize a read-only Robinhood snapshot for Optedge.")
    ap.add_argument("--input", default=str(DEFAULT_INPUT), help="Raw JSON bundle to normalize.")
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Normalized broker snapshot output path.")
    ap.add_argument(
        "--equity-ledger-dir",
        default="",
        help=(
            "Directory for chained pseudonymous account-equity ledgers. "
            "Defaults to durable per-user state for the repository data directory, "
            f"or {EQUITY_LEDGER_DIRNAME}/ beside a custom --output."
        ),
    )
    ap.add_argument("--account-number", default="", help="Fallback account number when raw rows omit it.")
    ap.add_argument("--dry-run", action="store_true", help="Print summary without writing the output file.")
    args = ap.parse_args(argv)

    input_path = Path(args.input)
    output_path = Path(args.output)
    ledger_dir = (
        Path(args.equity_ledger_dir)
        if str(args.equity_ledger_dir).strip()
        else default_account_equity_ledger_dir(output_path.parent)
    )
    raw = _read_json(input_path)
    snapshot = normalize_broker_snapshot(raw, account_number=args.account_number)
    summary = {
        "input": str(input_path),
        "output": str(output_path),
        "equity_ledger_dir": str(ledger_dir),
        "dry_run": bool(args.dry_run),
        **snapshot["counts"],
    }
    if not args.dry_run:
        # Ledger state is a consequence of a successfully persisted explicit
        # normalization.  Dry runs and failed snapshot writes cannot mutate it.
        _write_json(output_path, snapshot)
        ledger_update = append_account_equity_ledgers(snapshot, ledger_dir)
        summary["equity_ledger_update"] = ledger_update
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
