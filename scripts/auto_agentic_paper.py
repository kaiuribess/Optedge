"""Auto-take Robinhood Agentic queue entries in a local paper book.

This module intentionally does not call a broker API. It converts the latest
Optedge Robinhood agentic queue/cycle into:

- local paper option positions that are opened automatically when gates pass
- live-ready order tickets that require an explicit broker-side confirmation
- JSONL audit rows for every paper entry, duplicate skip, or blocked gate
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.export_robinhood_agentic_queue import (  # noqa: E402
    CYCLE_JSON,
    DECISION_LOG_JSONL,
    KILL_SWITCH,
    QUEUE_JSON,
    append_agent_decision,
    robinhood_mcp_option_review_plan,
)

DATA_DIR = ROOT / "data"
PAPER_POSITIONS_JSON = "agentic_paper_positions.json"
PAPER_ORDERS_JSONL = "agentic_paper_orders.jsonl"
LIVE_ORDER_TICKETS_JSON = "robinhood_live_order_tickets.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except Exception:
        return default
    return out if math.isfinite(out) else default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str, sort_keys=True), encoding="utf-8")


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str, sort_keys=True) + "\n")


def _contract_key(row: dict[str, Any]) -> str:
    symbol = _text(row.get("symbol") or row.get("ticker") or row.get("ticker_or_symbol")).upper()
    side = _text(row.get("option_side") or row.get("side")).lower()
    expiry = _text(row.get("expiry"))
    strike = _text(row.get("strike"))
    return "|".join([symbol, side, expiry, strike])


def _direction_key(row: dict[str, Any]) -> str:
    symbol = _text(row.get("symbol") or row.get("ticker") or row.get("ticker_or_symbol")).upper()
    direction = _text(row.get("direction")).lower()
    side = _text(row.get("option_side") or row.get("side")).lower()
    return "|".join([symbol, direction or side])


def _position_keys(positions: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    contract_keys: set[str] = set()
    direction_keys: set[str] = set()
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        if _text(pos.get("status") or pos.get("trade_status")).lower() in {"closed", "cancelled"}:
            continue
        contract_key = _contract_key(pos)
        direction_key = _direction_key(pos)
        if contract_key.strip("|"):
            contract_keys.add(contract_key)
        if direction_key.strip("|"):
            direction_keys.add(direction_key)
    return contract_keys, direction_keys


def _optedge_open_option_keys(data_dir: Path) -> tuple[set[str], set[str]]:
    raw = _read_json(data_dir / "open_positions.json", [])
    if not isinstance(raw, list):
        return set(), set()
    normalized: list[dict[str, Any]] = []
    for pos in raw:
        if not isinstance(pos, dict):
            continue
        normalized.append({
            "symbol": pos.get("ticker") or pos.get("symbol"),
            "option_side": pos.get("side") or pos.get("option_side"),
            "expiry": pos.get("expiry"),
            "strike": pos.get("strike"),
            "direction": pos.get("direction") or (
                f"long_{_text(pos.get('side') or pos.get('option_side')).lower()}"
                if _text(pos.get("side") or pos.get("option_side"))
                else ""
            ),
            "trade_status": pos.get("trade_status") or "open",
        })
    return _position_keys(normalized)


def _candidate_source(cycle: dict[str, Any], queue: dict[str, Any], allow_blocked_paper: bool) -> tuple[list[dict[str, Any]], str]:
    entry_gate = cycle.get("entry_gate") if isinstance(cycle.get("entry_gate"), dict) else {}
    if entry_gate.get("new_entries_allowed_after_live_checks"):
        rows = cycle.get("entry_candidates") if isinstance(cycle.get("entry_candidates"), list) else []
        return [row for row in rows if isinstance(row, dict)], "entry_candidates"
    if allow_blocked_paper:
        review_rows = (
            cycle.get("review_only_entry_candidates")
            if isinstance(cycle.get("review_only_entry_candidates"), list)
            else []
        )
        if review_rows:
            return [row for row in review_rows if isinstance(row, dict)], "review_only_entry_candidates"
        queue_rows = queue.get("orders") if isinstance(queue.get("orders"), list) else []
        return [row for row in queue_rows if isinstance(row, dict)], "queue_orders_override"
    return [], "blocked_by_entry_gate"


def _ticket_from_order(
    order: dict[str, Any],
    generated_at: str,
    gate_status: str,
    source: str,
) -> dict[str, Any]:
    limit_price = _float(order.get("max_limit_price") or order.get("reference_entry_price"))
    quantity = _int(order.get("quantity"), 0)
    ticket = {
        "generated_at": generated_at,
        "schema": "optedge_robinhood_live_order_ticket_v1",
        "status": "requires_explicit_confirmation",
        "broker": "robinhood",
        "asset": "option",
        "action": "BUY_TO_OPEN",
        "order_type": "limit",
        "time_in_force": "day",
        "symbol": _text(order.get("symbol") or order.get("ticker_or_symbol")).upper(),
        "contract": _text(order.get("contract")),
        "option_side": _text(order.get("option_side")).lower(),
        "strike": order.get("strike"),
        "expiry": order.get("expiry"),
        "quantity": quantity,
        "limit_price": round(limit_price, 2),
        "estimated_premium_dollars": round(limit_price * quantity * 100.0, 2),
        "max_limit_price": order.get("max_limit_price"),
        "reference_entry_price": order.get("reference_entry_price"),
        "stop_price_reference": order.get("stop_price_reference"),
        "target_price_reference": order.get("target_price_reference"),
        "confidence": order.get("confidence"),
        "rank_score": order.get("rank_score"),
        "fused_score": order.get("fused_score"),
        "swing_fit_score": order.get("swing_fit_score"),
        "swing_fit_label": order.get("swing_fit_label"),
        "entry_gate_status": gate_status,
        "candidate_source": source,
        "live_submit_allowed_by_this_script": False,
        "broker_mcp_review_supported": True,
        "broker_mcp_place_supported_after_explicit_confirmation": True,
        "confirmation_required": True,
        "notes": [
            "This is a live-ready ticket only; this script does not submit broker orders.",
            "Use robinhood_mcp_review_plan to review the exact single-leg limit order before any live place call.",
            "Before live submission, verify Robinhood buying power, exact contract, bid/ask/mid, spread, and current news.",
        ],
    }
    ticket["robinhood_mcp_review_plan"] = (
        order.get("robinhood_mcp_review_plan")
        if isinstance(order.get("robinhood_mcp_review_plan"), dict)
        else robinhood_mcp_option_review_plan(ticket)
    )
    return ticket


def _paper_position_from_order(
    order: dict[str, Any],
    generated_at: str,
    source: str,
    allow_blocked_paper: bool,
) -> dict[str, Any]:
    symbol = _text(order.get("symbol") or order.get("ticker_or_symbol")).upper()
    limit_price = _float(order.get("max_limit_price") or order.get("reference_entry_price"))
    ref_price = _float(order.get("reference_entry_price") or limit_price)
    quantity = max(1, _int(order.get("quantity"), 1))
    paper_id = "paper-" + "-".join([
        generated_at.replace(":", "").replace("+", "Z"),
        symbol,
        _text(order.get("expiry")).replace("-", ""),
        _text(order.get("option_side")).lower(),
        _text(order.get("strike")).replace(".", "p"),
    ])
    return {
        "paper_position_id": paper_id,
        "schema": "optedge_agentic_paper_position_v1",
        "status": "open",
        "opened_at": generated_at,
        "asset": "option",
        "broker_route": "local_paper_only",
        "symbol": symbol,
        "ticker_or_symbol": symbol,
        "action": "BUY_TO_OPEN",
        "direction": order.get("direction"),
        "contract": order.get("contract"),
        "option_side": _text(order.get("option_side")).lower(),
        "strike": order.get("strike"),
        "expiry": order.get("expiry"),
        "quantity": quantity,
        "entry_price": ref_price,
        "paper_limit_price": limit_price,
        "estimated_premium_dollars": round(ref_price * quantity * 100.0, 2),
        "stop_price_reference": order.get("stop_price_reference"),
        "target_price_reference": order.get("target_price_reference"),
        "confidence": order.get("confidence"),
        "rank_score": order.get("rank_score"),
        "fused_score": order.get("fused_score"),
        "swing_fit_score": order.get("swing_fit_score"),
        "swing_fit_label": order.get("swing_fit_label"),
        "cboe_activity_volume": order.get("cboe_activity_volume"),
        "candidate_source": source,
        "paper_override_validation_gate": bool(allow_blocked_paper and source != "entry_candidates"),
        "notes": [
            "Opened automatically in Optedge local paper tracking only.",
            "No live broker order was placed by this script.",
        ],
    }


def process_agentic_paper(
    data_dir: Path = DATA_DIR,
    max_orders: int | None = None,
    allow_blocked_paper: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create paper positions and live-ready tickets from latest agentic queue."""
    data_dir = Path(data_dir)
    generated_at = _now()
    queue = _read_json(data_dir / QUEUE_JSON, {})
    cycle = _read_json(data_dir / CYCLE_JSON, {})
    if not isinstance(queue, dict):
        queue = {}
    if not isinstance(cycle, dict):
        cycle = {}

    kill_switch = (data_dir / KILL_SWITCH).exists()
    entry_gate = cycle.get("entry_gate") if isinstance(cycle.get("entry_gate"), dict) else {}
    gate_status = _text(entry_gate.get("status")) or "unknown"
    blockers: list[str] = []
    if kill_switch:
        blockers.append("kill-switch file is present")
    if queue.get("status") != "ready":
        blockers.append(f"queue status is {queue.get('status') or 'missing'}")
    if cycle.get("hard_pause"):
        blockers.extend(
            _text(reason)
            for reason in cycle.get("hard_pause_reasons", [])
            if _text(reason)
        )
    if not cycle:
        blockers.append("agentic cycle file is missing or malformed")

    candidates, source = _candidate_source(cycle, queue, allow_blocked_paper=allow_blocked_paper)
    limit = max(0, int(max_orders if max_orders is not None else queue.get("max_orders_to_submit") or 0))
    if limit <= 0:
        limit = len(candidates)

    tickets = [
        _ticket_from_order(order, generated_at, gate_status, source)
        for order in candidates[:limit]
    ]

    existing_paper = _read_json(data_dir / PAPER_POSITIONS_JSON, [])
    if not isinstance(existing_paper, list):
        existing_paper = []
    paper_contract_keys, paper_direction_keys = _position_keys(existing_paper)
    optedge_contract_keys, optedge_direction_keys = _optedge_open_option_keys(data_dir)

    opened: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    if blockers:
        for order in candidates[:limit]:
            skipped.append({
                "timestamp": generated_at,
                "schema": "optedge_agentic_paper_order_v1",
                "action": "blocked",
                "symbol": _text(order.get("symbol") or order.get("ticker_or_symbol")).upper(),
                "contract": order.get("contract"),
                "reasons": blockers,
                "candidate_source": source,
            })
    else:
        for order in candidates[:limit]:
            contract_key = _contract_key(order)
            direction_key = _direction_key(order)
            reasons: list[str] = []
            if contract_key in paper_contract_keys:
                reasons.append("exact contract already open in local paper book")
            if direction_key in paper_direction_keys:
                reasons.append("same symbol/direction already open in local paper book")
            if contract_key in optedge_contract_keys:
                reasons.append("exact contract already open in Optedge open_positions.json")
            if direction_key in optedge_direction_keys:
                reasons.append("same symbol/direction already open in Optedge open_positions.json")
            if reasons:
                skipped.append({
                    "timestamp": generated_at,
                    "schema": "optedge_agentic_paper_order_v1",
                    "action": "skipped_duplicate",
                    "symbol": _text(order.get("symbol") or order.get("ticker_or_symbol")).upper(),
                    "contract": order.get("contract"),
                    "reasons": reasons,
                    "candidate_source": source,
                })
                continue
            position = _paper_position_from_order(order, generated_at, source, allow_blocked_paper)
            opened.append(position)
            paper_contract_keys.add(contract_key)
            paper_direction_keys.add(direction_key)

    output = {
        "generated_at": generated_at,
        "schema": "optedge_agentic_paper_execution_result_v1",
        "ok": True,
        "dry_run": dry_run,
        "data_dir": str(data_dir),
        "candidate_source": source,
        "queue_status": queue.get("status"),
        "entry_gate_status": gate_status,
        "allow_blocked_paper": allow_blocked_paper,
        "live_broker_orders_submitted": 0,
        "live_submit_supported": False,
        "broker_mcp_review_supported": True,
        "live_submit_note": "This script does not submit real broker orders. It writes live-ready tickets that can be reviewed with Robinhood MCP and still require explicit confirmation.",
        "candidate_count": len(candidates),
        "ticket_count": len(tickets),
        "opened_paper_count": len(opened),
        "skipped_count": len(skipped),
        "blockers": blockers,
        "files": {
            "paper_positions": str(data_dir / PAPER_POSITIONS_JSON),
            "paper_orders": str(data_dir / PAPER_ORDERS_JSONL),
            "live_order_tickets": str(data_dir / LIVE_ORDER_TICKETS_JSON),
            "decision_log": str(data_dir / DECISION_LOG_JSONL),
        },
        "live_order_tickets": tickets,
        "opened_paper_positions": opened,
        "skipped": skipped,
    }

    if dry_run:
        return output

    if tickets:
        _write_json(data_dir / LIVE_ORDER_TICKETS_JSON, {
            "generated_at": generated_at,
            "schema": "optedge_robinhood_live_order_tickets_v1",
            "live_submit_supported": False,
            "broker_mcp_review_supported": True,
            "confirmation_required": True,
            "tickets": tickets,
        })

    if opened:
        updated_positions = existing_paper + opened
        _write_json(data_dir / PAPER_POSITIONS_JSON, updated_positions)
        for row in opened:
            order_row = {
                "timestamp": generated_at,
                "schema": "optedge_agentic_paper_order_v1",
                "action": "paper_submitted",
                "paper_position_id": row.get("paper_position_id"),
                "symbol": row.get("symbol"),
                "contract": row.get("contract"),
                "option_side": row.get("option_side"),
                "strike": row.get("strike"),
                "expiry": row.get("expiry"),
                "quantity": row.get("quantity"),
                "limit_price": row.get("paper_limit_price"),
                "estimated_premium_dollars": row.get("estimated_premium_dollars"),
                "candidate_source": source,
                "paper_override_validation_gate": row.get("paper_override_validation_gate"),
            }
            _append_jsonl(data_dir / PAPER_ORDERS_JSONL, order_row)
            append_agent_decision(
                {
                    "decision": "reviewed",
                    "symbol": row.get("symbol"),
                    "contract": row.get("contract"),
                    "option_side": row.get("option_side"),
                    "strike": row.get("strike"),
                    "expiry": row.get("expiry"),
                    "quantity": row.get("quantity"),
                    "limit_price": row.get("paper_limit_price"),
                    "estimated_premium_dollars": row.get("estimated_premium_dollars"),
                    "entry_gate_status": gate_status,
                    "source": "auto_agentic_paper",
                    "reason": "local paper order opened automatically; no live broker order submitted",
                },
                data_dir=data_dir,
                generated_at=generated_at,
            )

    for row in skipped:
        _append_jsonl(data_dir / PAPER_ORDERS_JSONL, row)

    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Auto-open Optedge Robinhood queue entries in local paper tracking")
    parser.add_argument("--data-dir", default=str(DATA_DIR), help="Optedge data directory")
    parser.add_argument("--max-orders", type=int, default=None, help="Maximum paper orders to open this run")
    parser.add_argument(
        "--allow-blocked-paper",
        action="store_true",
        help="Allow local paper entries even when validation blocks live fresh entries",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing files")
    args = parser.parse_args(argv)
    result = process_agentic_paper(
        data_dir=Path(args.data_dir),
        max_orders=args.max_orders,
        allow_blocked_paper=args.allow_blocked_paper,
        dry_run=args.dry_run,
    )
    print(json.dumps({
        "ok": result["ok"],
        "dry_run": result["dry_run"],
        "candidate_source": result["candidate_source"],
        "candidate_count": result["candidate_count"],
        "opened_paper_count": result["opened_paper_count"],
        "ticket_count": result["ticket_count"],
        "skipped_count": result["skipped_count"],
        "live_broker_orders_submitted": result["live_broker_orders_submitted"],
        "blockers": result["blockers"],
        "files": result["files"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
