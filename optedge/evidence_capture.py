# Purpose: Freeze one explicitly checked Robinhood finalist as paper evidence.
"""Manual, append-only capture of an exact broker-checked option candidate."""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from backtest.fixed_horizon import current_evidence_provenance
from optedge.robinhood_finalist import (
    FINALIST_CHECK_FILE,
    FINALIST_CHECK_SCHEMA,
    canonical_digest,
    load_finalist_check_status,
)
from optedge.strategy_profile import (
    LEAPS_SWING_PROFILE,
    SWING_EXECUTION_PROFILE,
)

CAPTURE_SCHEMA = "optedge_manual_evidence_capture_v1"
AUDIT_FILE = "manual_evidence_captures.jsonl"


class EvidenceCaptureError(RuntimeError):
    """A safe fail-closed capture error."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "evidence_capture_failed")
        super().__init__(self.code)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).lower() in {"1", "true", "yes", "y", "trade", "buy", "long"}


def _append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(dict(row), sort_keys=True, default=str) + "\n")


def _atomic_write_parquet(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    os.close(handle)
    try:
        frame.to_parquet(temp_name, index=False)
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _bound_candidate(
    queue: Mapping[str, Any], cycle: Mapping[str, Any], check: Mapping[str, Any]
) -> dict[str, Any]:
    bindings = check.get("source_bindings")
    if not isinstance(bindings, Mapping):
        raise EvidenceCaptureError("finalist_source_binding_missing")
    if bindings.get("queue_digest_sha256") != canonical_digest(queue) or bindings.get(
        "cycle_digest_sha256"
    ) != canonical_digest(cycle):
        raise EvidenceCaptureError("finalist_source_changed")
    candidate_check = check.get("candidate")
    if not isinstance(candidate_check, Mapping):
        raise EvidenceCaptureError("finalist_candidate_missing")
    digest = _text(candidate_check.get("candidate_digest_sha256"))
    rows = queue.get("orders") if isinstance(queue.get("orders"), list) else []
    matches = [
        dict(row) for row in rows if isinstance(row, Mapping) and canonical_digest(row) == digest
    ]
    if len(matches) != 1:
        raise EvidenceCaptureError("finalist_candidate_binding_changed")
    return matches[0]


def _signal_row(
    candidate: Mapping[str, Any], check: Mapping[str, Any], captured_at: datetime
) -> dict[str, Any]:
    quote = check.get("quote") if isinstance(check.get("quote"), Mapping) else {}
    identity = check.get("candidate") if isinstance(check.get("candidate"), Mapping) else {}
    option_type = _text(identity.get("option_type") or candidate.get("option_side")).lower()
    if option_type not in {"call", "put"}:
        raise EvidenceCaptureError("finalist_option_type_invalid")
    ask = _number(quote.get("ask_price"))
    bid = _number(quote.get("bid_price"))
    iv = _number(quote.get("implied_volatility"))
    if ask is None or ask <= 0 or bid is None or bid <= 0 or iv is None or iv <= 0:
        raise EvidenceCaptureError("finalist_evidence_quote_incomplete")
    profile = _text(candidate.get("execution_profile") or SWING_EXECUTION_PROFILE.name).lower()
    lane = _text(candidate.get("strategy_evidence_lane"))
    policy_version = _text(candidate.get("profile_policy_version"))
    if profile == LEAPS_SWING_PROFILE.name:
        if lane != LEAPS_SWING_PROFILE.evidence_lane:
            raise EvidenceCaptureError("leaps_evidence_lane_mismatch")
        if policy_version != LEAPS_SWING_PROFILE.policy_version:
            raise EvidenceCaptureError("leaps_policy_version_mismatch")
        qualified = candidate.get("leaps_execution_ready") is True
        buyer_edge = _number(candidate.get("after_cost_edge_pct"))
    else:
        profile = SWING_EXECUTION_PROFILE.name
        lane = lane or "option_swing_execution"
        policy_version = policy_version or SWING_EXECUTION_PROFILE.strategy_version
        qualified = _truthy(candidate.get("strategy_qualified_pre_guard"))
        buyer_edge = _number(candidate.get("buyer_edge_pct"))
    pricing_edge_ok = buyer_edge is not None and buyer_edge >= 0
    if not qualified or not pricing_edge_ok:
        qualified = False

    expiry = _text(identity.get("expiry") or candidate.get("expiry"))[:10]
    strike = _number(identity.get("strike") or candidate.get("strike"))
    symbol = _text(identity.get("symbol") or candidate.get("symbol")).upper()
    if not symbol or not expiry or strike is None:
        raise EvidenceCaptureError("finalist_identity_incomplete")
    dte = max(0, (pd.Timestamp(expiry).date() - captured_at.date()).days)
    spread = _number(quote.get("spread_fraction"))
    quantity = max(1, int(_number(candidate.get("quantity")) or 1))
    row = {
        "asset": "option",
        "ticker": symbol,
        "symbol": symbol,
        "contract": _text(candidate.get("contract") or identity.get("label")),
        "side": option_type,
        "strike": strike,
        "expiry": expiry,
        "dte": dte,
        "mid": ask,
        "entry_price": ask,
        "bid": bid,
        "ask": ask,
        "spread_pct": spread,
        "iv_market": iv,
        "delta": _number(quote.get("delta")),
        "open_interest": _number(quote.get("open_interest")),
        "volume": _number(quote.get("volume")),
        "is_buy": True,
        "direction": f"long_{option_type}",
        "execution_profile": profile,
        "strategy_evidence_lane": lane,
        "profile_policy_version": policy_version,
        "intended_hold_sessions": candidate.get("planned_hold_sessions")
        or candidate.get("default_hold_sessions"),
        "max_hold_sessions": candidate.get("max_hold_sessions"),
        "buyer_edge_pct": buyer_edge,
        "pricing_edge_ok": pricing_edge_ok,
        "strategy_qualified_pre_guard": qualified,
        "pre_guard_suggested_contracts": quantity,
        "trade_status": "Watch",
        "is_actionable": False,
        "suggested_contracts": 0,
        "research_guard_status": "paper_evidence_only",
        "quote_quality": "live_broker",
        "chain_source": "robinhood_mcp",
        "source_quote_at": quote.get("updated_at"),
        "source_quote_time_basis": "broker_exchange_quote_updated_at",
        "entry_time": captured_at.isoformat(),
        "manual_evidence_capture": True,
        "manual_evidence_capture_schema": CAPTURE_SCHEMA,
        "robinhood_finalist_check_digest_sha256": check.get("artifact_digest_sha256"),
        **current_evidence_provenance(),
    }
    return row


def capture_checked_finalist_evidence(
    *,
    data_dir: Path,
    log_dir: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist one still-fresh, source-bound finalist as immutable paper evidence."""
    data_dir = Path(data_dir)
    log_dir = Path(log_dir)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    current = current.astimezone(UTC)
    raw_check = _read_json(data_dir / FINALIST_CHECK_FILE)
    check = load_finalist_check_status(data_dir, now=current)
    if raw_check.get("schema") != FINALIST_CHECK_SCHEMA or check.get("usable") is not True:
        raise EvidenceCaptureError("fresh_finalist_check_required")
    if check.get("market_check_passed") is not True:
        raise EvidenceCaptureError("finalist_market_check_blocked")
    queue = _read_json(data_dir / "robinhood_agentic_queue.json")
    cycle = _read_json(data_dir / "robinhood_agentic_cycle.json")
    candidate = _bound_candidate(queue, cycle, raw_check)
    row = _signal_row(candidate, raw_check, current)
    digest = _text(raw_check.get("artifact_digest_sha256"))
    if len(digest) != 64:
        raise EvidenceCaptureError("finalist_digest_missing")
    path = log_dir / f"signals_manual_evidence_{digest[:20]}.parquet"
    idempotent = path.exists()
    if not idempotent:
        _atomic_write_parquet(path, pd.DataFrame([row]))
        _append_jsonl(
            data_dir / AUDIT_FILE,
            {
                "schema": CAPTURE_SCHEMA,
                "captured_at": current.isoformat(),
                "signal_file": str(path),
                "symbol": row["symbol"],
                "contract": row["contract"],
                "execution_profile": row["execution_profile"],
                "strategy_evidence_lane": row["strategy_evidence_lane"],
                "strategy_qualified_pre_guard": row["strategy_qualified_pre_guard"],
                "finalist_check_digest_sha256": digest,
                "broker_reads_performed": 0,
                "broker_writes_authorized": 0,
            },
        )
    return {
        "schema": CAPTURE_SCHEMA,
        "ok": True,
        "captured": not idempotent,
        "idempotent": idempotent,
        "signal_file": str(path),
        "symbol": row["symbol"],
        "contract": row["contract"],
        "execution_profile": row["execution_profile"],
        "strategy_evidence_lane": row["strategy_evidence_lane"],
        "strategy_qualified_pre_guard": row["strategy_qualified_pre_guard"],
        "paper_only": True,
        "broker_reads_performed": 0,
        "broker_writes_authorized": 0,
        "does_not_place_orders": True,
        "does_not_preview_orders": True,
        "automatic_retry_enabled": False,
    }


__all__ = [
    "AUDIT_FILE",
    "CAPTURE_SCHEMA",
    "EvidenceCaptureError",
    "capture_checked_finalist_evidence",
]
