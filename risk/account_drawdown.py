# Purpose: Fail closed on unsafe Robinhood account-equity drawdowns.
"""Pure Robinhood account drawdown ledger and review interlock.

This module never reads or writes files and never contacts a broker.  It turns
already-normalized, read-only broker snapshots into pseudonymous chained equity
observations, validates those chains, and evaluates a versioned capital-
preservation policy for manual review.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

LEDGER_SCHEMA = "optedge_robinhood_account_equity_ledger_v1"
OBSERVATION_SCHEMA = "optedge_robinhood_account_equity_observation_v1"
INTERLOCK_SCHEMA = "optedge_robinhood_account_drawdown_interlock_v1"
BROKER_SNAPSHOT_SCHEMA = "optedge_robinhood_broker_snapshot_v1"

POLICY_VERSION = "robinhood_account_drawdown_v2"
DEFAULT_MAX_AGE_MINUTES = 90.0
MAX_FUTURE_CLOCK_SKEW_SECONDS = 60.0
MIN_BASELINE_OBSERVATIONS = 2
MIN_BASELINE_NY_CALENDAR_DATES = 2
MIN_BASELINE_SPAN_HOURS = 18.0
REDUCE_RISK_DRAWDOWN_FRACTION = 0.05
QUARTER_RISK_DRAWDOWN_FRACTION = 0.08
BLOCK_DRAWDOWN_FRACTION = 0.10
BLOCK_SESSION_LOSS_FRACTION = -0.03
UNEXPLAINED_JUMP_FRACTION = 0.25

GENESIS_HASH = "0" * 64
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
NEW_YORK = ZoneInfo("America/New_York")

_OBSERVATION_HASH_FIELDS = (
    "schema",
    "sequence",
    "account_key",
    "observed_at",
    "equity_dollars",
    "source_snapshot_digest_sha256",
    "previous_observation_hash_sha256",
)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _money(value: float) -> float:
    return round(value + 0.0, 2)


def _ratio(value: float) -> float:
    return round(value + 0.0, 8)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _clean_account_key(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    key = value.strip()
    if key != value or re.fullmatch(r"acct_[0-9a-f]{16}", key) is None:
        return ""
    return key


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None


def source_snapshot_digest(snapshot: Mapping[str, Any] | Any) -> str | None:
    """Digest normalized source state while excluding local normalization time.

    ``normalized_at`` changes when an identical source bundle is normalized
    again.  Excluding only that local processing timestamp makes repeated
    explicit imports idempotent while binding every broker-sourced field,
    including ``generated_at`` and all account-scoped reads.
    """
    if not isinstance(snapshot, Mapping):
        return None
    digest_view = deepcopy(dict(snapshot))
    digest_view.pop("normalized_at", None)
    try:
        return _sha256(digest_view)
    except (TypeError, ValueError):
        return None


def _snapshot_observation_fields(
    snapshot: Mapping[str, Any] | Any,
    account_key: str,
) -> dict[str, Any]:
    if not isinstance(snapshot, Mapping):
        raise ValueError("broker snapshot must be a mapping")
    if snapshot.get("schema") != BROKER_SNAPSHOT_SCHEMA:
        raise ValueError("broker snapshot schema is missing or unsupported")
    if snapshot.get("normalization_blockers"):
        raise ValueError("broker snapshot has unresolved normalization blockers")

    clean_key = _clean_account_key(account_key)
    if not clean_key:
        raise ValueError("account_key must be a non-empty pseudonymous key")
    generated = _parse_timestamp(snapshot.get("generated_at"))
    if generated is None:
        raise ValueError("broker snapshot generated_at must be timezone-aware")

    accounts = snapshot.get("accounts")
    if not isinstance(accounts, list):
        raise ValueError("broker snapshot accounts are missing")
    matches = [
        row for row in accounts if isinstance(row, Mapping) and row.get("account_key") == clean_key
    ]
    if len(matches) != 1:
        raise ValueError("account_key must identify exactly one normalized broker account")
    portfolio = matches[0].get("portfolio")
    if not isinstance(portfolio, Mapping):
        raise ValueError("normalized account portfolio is missing")
    equity = _finite_number(portfolio.get("total_value"))
    if equity is None or equity <= 0:
        raise ValueError("normalized account portfolio total_value must be positive and finite")

    snapshot_digest = source_snapshot_digest(snapshot)
    if snapshot_digest is None:
        raise ValueError("broker snapshot cannot be deterministically digested")
    return {
        "account_key": clean_key,
        "observed_at": _timestamp_text(generated),
        "equity_dollars": _money(equity),
        "source_snapshot_digest_sha256": snapshot_digest,
    }


def eligible_snapshot_account_keys(snapshot: Mapping[str, Any] | Any) -> list[str]:
    """Return unique accounts that contain the minimum observation fields."""
    if not isinstance(snapshot, Mapping) or snapshot.get("normalization_blockers"):
        return []
    if _parse_timestamp(snapshot.get("generated_at")) is None:
        return []
    accounts = snapshot.get("accounts")
    if not isinstance(accounts, list):
        return []
    eligible: list[str] = []
    for row in accounts:
        if not isinstance(row, Mapping):
            continue
        key = _clean_account_key(row.get("account_key"))
        portfolio = row.get("portfolio")
        equity = (
            _finite_number(portfolio.get("total_value")) if isinstance(portfolio, Mapping) else None
        )
        if key and equity is not None and equity > 0 and key not in eligible:
            eligible.append(key)
    return eligible


def _observation_digest(observation: Mapping[str, Any]) -> str:
    return _sha256({field: observation.get(field) for field in _OBSERVATION_HASH_FIELDS})


def _ledger_digest_payload(ledger: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": ledger.get("schema"),
        "account_key": ledger.get("account_key"),
        "observations": ledger.get("observations"),
    }


def _ledger_digest(ledger: Mapping[str, Any]) -> str:
    return _sha256(_ledger_digest_payload(ledger))


def new_equity_ledger(account_key: str) -> dict[str, Any]:
    """Return an empty, sealed single-account equity ledger."""
    clean_key = _clean_account_key(account_key)
    if not clean_key:
        raise ValueError("account_key must be a non-empty pseudonymous key")
    ledger: dict[str, Any] = {
        "schema": LEDGER_SCHEMA,
        "account_key": clean_key,
        "observations": [],
    }
    ledger["ledger_digest_sha256"] = _ledger_digest(ledger)
    return ledger


def validate_equity_ledger(ledger: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Validate schema, single-account scope, time order, and the full hash chain."""
    blockers: list[str] = []
    if not isinstance(ledger, Mapping):
        return {
            "schema": "optedge_robinhood_account_equity_ledger_validation_v1",
            "valid": False,
            "account_key": None,
            "observation_count": 0,
            "ledger_digest_sha256": None,
            "blockers": ["equity ledger must be a mapping"],
        }

    if ledger.get("schema") != LEDGER_SCHEMA:
        blockers.append("equity ledger schema is missing or unsupported")
    account_key = _clean_account_key(ledger.get("account_key"))
    if not account_key:
        blockers.append("equity ledger account_key is missing or malformed")
    observations = ledger.get("observations")
    if not isinstance(observations, list):
        blockers.append("equity ledger observations must be a list")
        observations = []

    previous_hash = GENESIS_HASH
    previous_time: datetime | None = None
    for index, raw in enumerate(observations):
        label = f"equity observation {index + 1}"
        if not isinstance(raw, Mapping):
            blockers.append(f"{label} must be a mapping")
            continue
        if raw.get("schema") != OBSERVATION_SCHEMA:
            blockers.append(f"{label} schema is missing or unsupported")
        sequence = raw.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence != index + 1:
            blockers.append(f"{label} sequence is not contiguous")
        row_key = _clean_account_key(raw.get("account_key"))
        if not row_key or not account_key or row_key != account_key:
            blockers.append(f"{label} does not match the ledger's single account")
        observed_at = _parse_timestamp(raw.get("observed_at"))
        if observed_at is None or raw.get("observed_at") != _timestamp_text(observed_at):
            blockers.append(f"{label} observed_at is not canonical timezone-aware UTC")
        elif previous_time is not None and observed_at <= previous_time:
            blockers.append(f"{label} time is not strictly increasing")
        if observed_at is not None:
            previous_time = observed_at
        equity = _finite_number(raw.get("equity_dollars"))
        if equity is None or equity <= 0:
            blockers.append(f"{label} equity_dollars must be positive and finite")
        if not _is_sha256(raw.get("source_snapshot_digest_sha256")):
            blockers.append(f"{label} source snapshot digest is malformed")
        if raw.get("previous_observation_hash_sha256") != previous_hash:
            blockers.append(f"{label} previous hash does not continue the chain")
        expected_hash = _observation_digest(raw)
        actual_hash = raw.get("observation_hash_sha256")
        if not _is_sha256(actual_hash) or actual_hash != expected_hash:
            blockers.append(f"{label} hash does not match its contents")
        previous_hash = actual_hash if isinstance(actual_hash, str) else ""

    actual_ledger_digest = ledger.get("ledger_digest_sha256")
    expected_ledger_digest = _ledger_digest(ledger)
    if not _is_sha256(actual_ledger_digest) or actual_ledger_digest != expected_ledger_digest:
        blockers.append("equity ledger digest does not match its contents")

    return {
        "schema": "optedge_robinhood_account_equity_ledger_validation_v1",
        "valid": not blockers,
        "account_key": account_key or None,
        "observation_count": len(observations),
        "ledger_digest_sha256": expected_ledger_digest if not blockers else None,
        "blockers": blockers,
    }


def append_snapshot_observation(
    ledger: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any],
    account_key: str,
) -> tuple[dict[str, Any], bool]:
    """Purely append one snapshot observation, or deduplicate an identical one.

    Unsafe existing ledgers, account changes, contradictory equal timestamps,
    and backwards time all raise ``ValueError`` rather than repairing history.
    """
    fields = _snapshot_observation_fields(snapshot, account_key)
    if ledger is None:
        out = new_equity_ledger(fields["account_key"])
    else:
        validation = validate_equity_ledger(ledger)
        if validation.get("valid") is not True:
            raise ValueError(
                "existing equity ledger is unsafe: " + "; ".join(validation.get("blockers") or [])
            )
        out = deepcopy(dict(ledger))
    if out.get("account_key") != fields["account_key"]:
        raise ValueError("snapshot account does not match the equity ledger account")

    observations = out.get("observations")
    assert isinstance(observations, list)
    identity_fields = (
        "account_key",
        "observed_at",
        "equity_dollars",
        "source_snapshot_digest_sha256",
    )
    for existing in observations:
        if isinstance(existing, Mapping) and all(
            existing.get(field) == fields[field] for field in identity_fields
        ):
            return out, False

    new_time = _parse_timestamp(fields["observed_at"])
    assert new_time is not None
    if observations:
        latest = observations[-1]
        latest_time = _parse_timestamp(latest.get("observed_at"))
        assert latest_time is not None
        if new_time < latest_time:
            raise ValueError("snapshot observation time is older than the ledger tail")
        if new_time == latest_time:
            raise ValueError("snapshot observation contradicts the ledger at the same time")
        previous_hash = latest.get("observation_hash_sha256")
        assert isinstance(previous_hash, str)
    else:
        previous_hash = GENESIS_HASH

    observation = {
        "schema": OBSERVATION_SCHEMA,
        "sequence": len(observations) + 1,
        **fields,
        "previous_observation_hash_sha256": previous_hash,
    }
    observation["observation_hash_sha256"] = _observation_digest(observation)
    observations.append(observation)
    out["ledger_digest_sha256"] = _ledger_digest(out)
    return out, True


def _blocked_payload(blockers: list[str], *, account_key: str | None = None) -> dict[str, Any]:
    return {
        "schema": INTERLOCK_SCHEMA,
        "policy_version": POLICY_VERSION,
        "status": "blocked",
        "review_ready": False,
        "allowed": False,
        "account_key": account_key,
        "asof": None,
        "observation_count": 0,
        "baseline_started_at": None,
        "baseline_span_hours": None,
        "baseline_ny_calendar_date_count": 0,
        "current_equity_dollars": None,
        "high_water_equity_dollars": None,
        "high_water_drawdown_fraction": None,
        "ny_session_date": None,
        "ny_session_reference_equity_dollars": None,
        "ny_session_loss_fraction": None,
        "risk_multiplier": 0.0,
        "source_snapshot_digest_sha256": None,
        "ledger_digest_sha256": None,
        "blockers": blockers,
        "policy": _policy_payload(),
        "does_not_place_orders": True,
    }


def _policy_payload(max_age_minutes: float = DEFAULT_MAX_AGE_MINUTES) -> dict[str, Any]:
    return {
        "max_observation_age_minutes": _ratio(max_age_minutes),
        "minimum_baseline_observations": MIN_BASELINE_OBSERVATIONS,
        "minimum_baseline_ny_calendar_dates": MIN_BASELINE_NY_CALENDAR_DATES,
        "minimum_baseline_span_hours": MIN_BASELINE_SPAN_HOURS,
        "half_risk_at_drawdown_fraction": REDUCE_RISK_DRAWDOWN_FRACTION,
        "quarter_risk_at_drawdown_fraction": QUARTER_RISK_DRAWDOWN_FRACTION,
        "block_at_drawdown_fraction": BLOCK_DRAWDOWN_FRACTION,
        "block_at_ny_session_loss_fraction": BLOCK_SESSION_LOSS_FRACTION,
        "block_at_unexplained_adjacent_jump_fraction": UNEXPLAINED_JUMP_FRACTION,
        "missing_or_unsafe_state_policy": "block_new_entries",
        "risk_multiplier_may_increase_risk": False,
    }


def evaluate_account_drawdown(
    ledger: Mapping[str, Any] | Any,
    current_snapshot: Mapping[str, Any] | Any,
    *,
    account_key: str | None = None,
    now: datetime | None = None,
    max_age_minutes: float = DEFAULT_MAX_AGE_MINUTES,
) -> dict[str, Any]:
    """Evaluate one account's high-water and New York-session drawdown.

    The current normalized snapshot must exactly match the latest chained
    observation.  Any uncertainty sets ``risk_multiplier`` to zero and blocks
    a new manual-review packet.
    """
    validation = validate_equity_ledger(ledger)
    clean_key = _clean_account_key(account_key or validation.get("account_key"))
    if validation.get("valid") is not True:
        return _blocked_payload(
            ["Equity ledger: " + blocker for blocker in validation.get("blockers") or []],
            account_key=clean_key or None,
        )
    assert isinstance(ledger, Mapping)
    if not clean_key or clean_key != validation.get("account_key"):
        return _blocked_payload(
            ["requested account does not match the validated single-account equity ledger"],
            account_key=clean_key or None,
        )

    age_limit = _finite_number(max_age_minutes)
    if age_limit is None or age_limit <= 0:
        return _blocked_payload(
            ["max_age_minutes must be positive and finite"],
            account_key=clean_key,
        )
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        return _blocked_payload(
            ["now must be timezone-aware"],
            account_key=clean_key,
        )
    current_time = current_time.astimezone(UTC)

    observations = ledger.get("observations")
    assert isinstance(observations, list)
    blockers: list[str] = []
    if len(observations) < MIN_BASELINE_OBSERVATIONS:
        blockers.append(
            "Account drawdown baseline needs at least two explicit broker observations."
        )

    try:
        current_fields = _snapshot_observation_fields(current_snapshot, clean_key)
    except ValueError as exc:
        current_fields = None
        blockers.append(f"Current broker snapshot: {exc}")

    latest = observations[-1] if observations else None
    if latest is None:
        blockers.append("Equity ledger has no observations.")
    elif current_fields is not None:
        for field in (
            "account_key",
            "observed_at",
            "equity_dollars",
            "source_snapshot_digest_sha256",
        ):
            if latest.get(field) != current_fields.get(field):
                blockers.append(
                    "Current broker snapshot does not match the latest chained equity observation."
                )
                break

    parsed_rows: list[tuple[datetime, float]] = []
    for raw in observations:
        observed_at = _parse_timestamp(raw.get("observed_at"))
        equity = _finite_number(raw.get("equity_dollars"))
        if observed_at is not None and equity is not None and equity > 0:
            parsed_rows.append((observed_at, equity))

    latest_time = parsed_rows[-1][0] if parsed_rows else None
    baseline_started_at = parsed_rows[0][0] if parsed_rows else None
    baseline_span_hours = (
        (latest_time - baseline_started_at).total_seconds() / 3600.0
        if latest_time is not None and baseline_started_at is not None
        else None
    )
    baseline_ny_dates = {observed_at.astimezone(NEW_YORK).date() for observed_at, _ in parsed_rows}
    if baseline_span_hours is None or baseline_span_hours < MIN_BASELINE_SPAN_HOURS - 1e-12:
        blockers.append(
            "Account drawdown baseline must span at least 18 hours before new entries are allowed."
        )
    if len(baseline_ny_dates) < MIN_BASELINE_NY_CALENDAR_DATES:
        blockers.append(
            "Account drawdown baseline must cover at least two New York calendar dates."
        )
    if latest_time is not None:
        age_seconds = (current_time - latest_time).total_seconds()
        if age_seconds < -MAX_FUTURE_CLOCK_SKEW_SECONDS:
            blockers.append("Latest equity observation is materially in the future.")
        elif age_seconds > age_limit * 60.0:
            blockers.append(
                f"Latest equity observation is stale; refresh within {age_limit:g} minutes."
            )

    high_water = max((equity for _, equity in parsed_rows), default=None)
    current_equity = parsed_rows[-1][1] if parsed_rows else None
    drawdown = (
        current_equity / high_water - 1.0
        if current_equity is not None and high_water is not None and high_water > 0
        else None
    )

    ny_session_date = latest_time.astimezone(NEW_YORK).date() if latest_time else None
    session_reference = None
    if ny_session_date is not None and parsed_rows:
        prior_sessions = [
            row for row in parsed_rows[:-1] if row[0].astimezone(NEW_YORK).date() < ny_session_date
        ]
        if prior_sessions:
            session_reference = prior_sessions[-1][1]
        else:
            same_session = [
                row for row in parsed_rows if row[0].astimezone(NEW_YORK).date() == ny_session_date
            ]
            if same_session:
                session_reference = same_session[0][1]
    session_loss = (
        current_equity / session_reference - 1.0
        if current_equity is not None and session_reference is not None and session_reference > 0
        else None
    )

    for index in range(1, len(parsed_rows)):
        previous_equity = parsed_rows[index - 1][1]
        next_equity = parsed_rows[index][1]
        adjacent_change = next_equity / previous_equity - 1.0
        if abs(adjacent_change) >= UNEXPLAINED_JUMP_FRACTION - 1e-12:
            blockers.append(
                "Adjacent account equity changed by at least 25%; possible cash flow requires an explicit rebaseline."
            )
            break

    risk_multiplier = 1.0
    if drawdown is not None:
        loss_magnitude = max(0.0, -drawdown)
        if loss_magnitude >= BLOCK_DRAWDOWN_FRACTION - 1e-12:
            blockers.append("Account high-water drawdown is at least 10%; new entries are blocked.")
        elif loss_magnitude >= QUARTER_RISK_DRAWDOWN_FRACTION - 1e-12:
            risk_multiplier = 0.25
        elif loss_magnitude >= REDUCE_RISK_DRAWDOWN_FRACTION - 1e-12:
            risk_multiplier = 0.5
    if session_loss is not None and session_loss <= BLOCK_SESSION_LOSS_FRACTION + 1e-12:
        blockers.append("New York-session account loss is at least 3%; new entries are blocked.")

    # No blocker may preserve a positive sizing multiplier.  The interlock can
    # only reduce risk and can never authorize more than the caller requested.
    allowed = not blockers
    if not allowed:
        risk_multiplier = 0.0
    risk_multiplier = min(1.0, max(0.0, risk_multiplier))
    status = "blocked" if not allowed else "reduced" if risk_multiplier < 1.0 else "ready"
    return {
        "schema": INTERLOCK_SCHEMA,
        "policy_version": POLICY_VERSION,
        "status": status,
        "review_ready": allowed,
        "allowed": allowed,
        "account_key": clean_key,
        "asof": latest.get("observed_at") if isinstance(latest, Mapping) else None,
        "observation_count": len(observations),
        "baseline_started_at": (
            _timestamp_text(baseline_started_at) if baseline_started_at is not None else None
        ),
        "baseline_span_hours": (
            _ratio(baseline_span_hours) if baseline_span_hours is not None else None
        ),
        "baseline_ny_calendar_date_count": len(baseline_ny_dates),
        "current_equity_dollars": _money(current_equity) if current_equity is not None else None,
        "high_water_equity_dollars": _money(high_water) if high_water is not None else None,
        "high_water_drawdown_fraction": _ratio(drawdown) if drawdown is not None else None,
        "ny_session_date": ny_session_date.isoformat() if ny_session_date is not None else None,
        "ny_session_reference_equity_dollars": (
            _money(session_reference) if session_reference is not None else None
        ),
        "ny_session_loss_fraction": _ratio(session_loss) if session_loss is not None else None,
        "risk_multiplier": risk_multiplier,
        "source_snapshot_digest_sha256": (
            latest.get("source_snapshot_digest_sha256") if isinstance(latest, Mapping) else None
        ),
        "ledger_digest_sha256": validation.get("ledger_digest_sha256"),
        "blockers": blockers,
        "policy": _policy_payload(age_limit),
        "methodology": {
            "high_water_drawdown": "latest_equity / maximum_observed_equity - 1",
            "ny_session_loss": (
                "latest_equity / prior_New_York_session_close_observation - 1; "
                "falls back to first observation in the current NY date"
            ),
            "source_snapshot_digest": "normalized snapshot excluding only normalized_at",
            "cash_flow_policy": "adjacent absolute equity change >=25% blocks for rebaseline",
        },
        "does_not_place_orders": True,
    }
