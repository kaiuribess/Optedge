# Purpose: Bind live Robinhood holdings to normal Optedge lifecycle exit decisions.
"""Fail-closed adapter from broker holdings to Optedge's existing exit engine."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from backtest.exit_rules import compute_exit_pressure

MAX_SNAPSHOT_AGE_MINUTES = 10
MAX_RESEARCH_AGE_MINUTES = 45
AUTOMATIC_EXIT_ACTIONS = frozenset({"hard_stop", "hard_target", "close_early"})


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _timestamp(value: Any) -> datetime | None:
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


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def _identity(row: Mapping[str, Any]) -> tuple[str, str, float, str] | None:
    symbol = _text(row.get("ticker") or row.get("chain_symbol") or row.get("symbol")).upper()
    side = _text(row.get("side") or row.get("option_type")).lower()
    strike = _number(row.get("strike") or row.get("strike_price"))
    expiry = _text(row.get("expiry") or row.get("expiration_date"))[:10]
    if not symbol or side not in {"call", "put"} or strike is None or not expiry:
        return None
    return symbol, side, round(strike, 4), expiry


def _latest_ranked_options(data_dir: Path, current: datetime) -> tuple[pd.DataFrame, dict[str, Any]]:
    paths = sorted(Path(data_dir).glob("ranked_options_*.parquet"), reverse=True)
    if not paths:
        return pd.DataFrame(), {"available": False, "reason": "ranked_options_missing"}
    path = paths[0]
    try:
        age_minutes = max(0.0, (current.timestamp() - path.stat().st_mtime) / 60.0)
    except OSError:
        return pd.DataFrame(), {"available": False, "reason": "ranked_options_unreadable"}
    if age_minutes > MAX_RESEARCH_AGE_MINUTES:
        return pd.DataFrame(), {
            "available": False,
            "reason": "ranked_options_stale",
            "age_minutes": round(age_minutes, 2),
        }
    try:
        frame = pd.read_parquet(path)
    except Exception:
        return pd.DataFrame(), {
            "available": False,
            "reason": "ranked_options_invalid",
            "age_minutes": round(age_minutes, 2),
        }
    return frame, {
        "available": True,
        "source": path.name,
        "age_minutes": round(age_minutes, 2),
    }


def _signal_for_identity(frame: pd.DataFrame, identity: tuple[str, str, float, str]) -> dict[str, Any] | None:
    if frame.empty:
        return None
    for _, series in frame.iterrows():
        row = series.to_dict()
        if _identity(row) == identity:
            return row
    return None


def _lifecycle_match(
    data_dir: Path,
    identity: tuple[str, str, float, str],
    current: datetime,
) -> tuple[dict[str, Any] | None, str, list[str]]:
    open_rows = _read_json(Path(data_dir) / "open_positions.json", [])
    if not isinstance(open_rows, list):
        open_rows = []
    matches = [dict(row) for row in open_rows if isinstance(row, Mapping) and _identity(row) == identity]
    if len(matches) == 1:
        return matches[0], "open_positions.json", []
    if len(matches) > 1:
        return None, "open_positions.json", ["multiple exact Optedge lifecycle rows matched"]

    closed_rows = _read_json(Path(data_dir) / "closed_positions.json", [])
    if not isinstance(closed_rows, list):
        closed_rows = []
    terminal: list[tuple[datetime, dict[str, Any]]] = []
    for raw in closed_rows:
        if not isinstance(raw, Mapping) or _identity(raw) != identity:
            continue
        exit_at = _timestamp(raw.get("exit_time"))
        reason = _text(raw.get("exit_reason")).lower()
        if exit_at is None or reason not in {"hard_stop", "hard_target", "dynamic_exit"}:
            continue
        age_minutes = (current - exit_at).total_seconds() / 60.0
        if 0 <= age_minutes <= MAX_RESEARCH_AGE_MINUTES:
            terminal.append((exit_at, dict(raw)))
    if terminal:
        terminal.sort(key=lambda item: item[0], reverse=True)
        return terminal[0][1], "closed_positions.json", []
    return None, "none", ["broker contract has no exact fresh Optedge lifecycle match"]


def _snapshot_positions(
    data_dir: Path,
    *,
    account_key: str,
    current: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    snapshot = _read_json(Path(data_dir) / "robinhood_broker_snapshot.json", {})
    if not isinstance(snapshot, Mapping):
        return [], {"available": False, "reason": "broker_snapshot_invalid"}
    generated = _timestamp(snapshot.get("generated_at") or snapshot.get("source_generated_at"))
    if generated is None:
        return [], {"available": False, "reason": "broker_snapshot_timestamp_missing"}
    age_minutes = (current - generated).total_seconds() / 60.0
    if age_minutes < -1 or age_minutes > MAX_SNAPSHOT_AGE_MINUTES:
        return [], {
            "available": False,
            "reason": "broker_snapshot_stale",
            "age_minutes": round(age_minutes, 2),
        }
    rows = snapshot.get("option_positions")
    if not isinstance(rows, list):
        rows = []
    positions = [
        dict(row)
        for row in rows
        if isinstance(row, Mapping)
        and _text(row.get("account_key")) == account_key
        and abs(_number(row.get("quantity")) or 0.0) > 1e-12
    ]
    return positions, {
        "available": True,
        "source": "robinhood_broker_snapshot.json",
        "age_minutes": round(max(0.0, age_minutes), 2),
    }


def analyze_robinhood_holdings_with_optedge(
    portfolio_analysis: Mapping[str, Any],
    *,
    data_dir: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Require exact broker/lifecycle identity before normal Optedge can authorize an exit."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    account = portfolio_analysis.get("account")
    account_key = _text(account.get("account_key")) if isinstance(account, Mapping) else ""
    snapshot_positions, snapshot_source = _snapshot_positions(
        Path(data_dir), account_key=account_key, current=current
    )
    ranked, research_source = _latest_ranked_options(Path(data_dir), current)
    holdings = [
        dict(row)
        for row in portfolio_analysis.get("holdings", [])
        if isinstance(row, Mapping)
    ]
    reviews: list[dict[str, Any]] = []
    enriched: list[dict[str, Any]] = []

    for holding in holdings:
        if holding.get("asset") != "option":
            holding["optedge_exit_action"] = "manual_only"
            holding["auto_exit_eligible"] = False
            enriched.append(holding)
            continue
        option_id = _text(holding.get("option_id"))
        exact_broker = [
            row
            for row in snapshot_positions
            if _text(row.get("instrument_id") or row.get("option_id")) == option_id
        ]
        blockers: list[str] = []
        if not snapshot_source.get("available"):
            blockers.append(_text(snapshot_source.get("reason")) or "broker snapshot unavailable")
        if len(exact_broker) != 1:
            blockers.append("exact normalized broker position was not unique")
        broker_row = exact_broker[0] if len(exact_broker) == 1 else {}
        identity = _identity(broker_row)
        if identity is None:
            blockers.append("broker option identity is incomplete")

        lifecycle = None
        lifecycle_source = "none"
        if identity is not None:
            lifecycle, lifecycle_source, lifecycle_blockers = _lifecycle_match(
                Path(data_dir), identity, current
            )
            blockers.extend(lifecycle_blockers)

        signal = _signal_for_identity(ranked, identity) if identity is not None else None
        mark = _number(holding.get("mark"))
        broker_entry = _number(holding.get("average_price_per_contract"))
        broker_entry = broker_entry / 100.0 if broker_entry is not None else None
        projected = dict(lifecycle or {})
        entry = _number(projected.get("entry_price")) or broker_entry
        if entry is None or entry <= 0 or mark is None or mark <= 0:
            blockers.append("entry or current option price is invalid")
        age_days = None
        entry_at = _timestamp(projected.get("entry_time"))
        if entry_at is not None:
            age_days = max(0.0, (current - entry_at).total_seconds() / 86400.0)
        projected.update(
            {
                "ticker": identity[0] if identity else holding.get("symbol"),
                "side": identity[1] if identity else None,
                "strike": identity[2] if identity else None,
                "expiry": identity[3] if identity else holding.get("expiry"),
                "entry_price": entry,
                "current_mid": mark,
                "current_price": mark,
                "unrealized_pct": ((mark - entry) / entry) if entry and mark else None,
                "age_days": age_days,
                "spread_pct": holding.get("spread_fraction"),
            }
        )

        review = compute_exit_pressure(projected, signal, asset="option") if lifecycle else {}
        action = _text(review.get("action") or "hold").lower()
        stop = _number(projected.get("stop_price"))
        target = _number(projected.get("target_price"))
        terminal_reason = _text(projected.get("exit_reason")).lower()
        if stop is not None and mark is not None and mark <= stop:
            action = "hard_stop"
        elif target is not None and mark is not None and mark >= target:
            action = "hard_target"
        elif terminal_reason == "dynamic_exit":
            action = "close_early"

        if action == "close_early" and (signal is None or not research_source.get("available")):
            blockers.append("dynamic exit lacks a fresh exact ranked-options thesis")
        broker_ready = holding.get("broker_close_ready") is True
        if not broker_ready:
            blockers.append("Robinhood close quote or order state is not execution-ready")
        automatic_exit_allowed = bool(
            lifecycle
            and identity is not None
            and action in AUTOMATIC_EXIT_ACTIONS
            and broker_ready
            and not blockers
        )
        public_review = {
            "option_id": option_id or None,
            "identity": {
                "symbol": identity[0] if identity else None,
                "option_type": identity[1] if identity else None,
                "strike": identity[2] if identity else None,
                "expiry": identity[3] if identity else None,
            },
            "action": action,
            "exit_pressure": review.get("exit_pressure"),
            "reasons": review.get("reasons") or [],
            "policy_version": review.get("policy_version"),
            "used_learned_policy": review.get("used_learned_policy"),
            "lifecycle_source": lifecycle_source,
            "current_signal_matched": signal is not None,
            "automatic_exit_allowed": automatic_exit_allowed,
            "blockers": list(dict.fromkeys(blockers)),
        }
        holding.update(
            {
                "option_type": public_review["identity"]["option_type"],
                "strike": public_review["identity"]["strike"],
                "optedge_exit_action": action,
                "optedge_exit_pressure": review.get("exit_pressure"),
                "optedge_exit_reasons": review.get("reasons") or [],
                "optedge_lifecycle_source": lifecycle_source,
                "action": action if automatic_exit_allowed else "hold",
                "signals": review.get("reasons") or [],
                "auto_exit_eligible": automatic_exit_allowed,
                "blockers": list(dict.fromkeys([*(holding.get("blockers") or []), *blockers])),
            }
        )
        reviews.append(public_review)
        enriched.append(holding)

    analysis = dict(portfolio_analysis)
    analysis["holdings"] = enriched
    analysis["automatic_exit_candidate_count"] = sum(
        row.get("auto_exit_eligible") is True for row in enriched
    )
    analysis["exit_decision_source"] = "backtest.exit_rules.compute_exit_pressure"
    return {
        "schema": "optedge_robinhood_exit_analysis_v1",
        "generated_at": current.isoformat(),
        "portfolio_analysis": analysis,
        "reviews": reviews,
        "broker_snapshot_source": snapshot_source,
        "research_source": research_source,
        "automatic_exit_actions": sorted(AUTOMATIC_EXIT_ACTIONS),
    }


__all__ = [
    "AUTOMATIC_EXIT_ACTIONS",
    "analyze_robinhood_holdings_with_optedge",
]
