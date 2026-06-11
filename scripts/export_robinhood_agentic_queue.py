"""Build an option-only Robinhood Agentic Trading handoff queue.

This script does not connect to Robinhood, does not store credentials, and does
not place orders. It creates a strict execution candidate file and a companion
prompt for a Robinhood MCP/Codex agent to double-check before any order.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from scripts.export_external_paper_track import build_external_orders

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

QUEUE_JSON = "robinhood_agentic_queue.json"
PROMPT_MD = "robinhood_agentic_prompt.md"
KILL_SWITCH = "agentic_trading_disabled.flag"

DEFAULT_ACCOUNT_BUDGET = 500.0
DEFAULT_MAX_ORDERS = 2
DEFAULT_MAX_CANDIDATES = 5
DEFAULT_MIN_CONFIDENCE = 55.0
DEFAULT_MAX_SPREAD_PCT = 0.15
DEFAULT_LIMIT_BUFFER_PCT = 0.08
DEFAULT_MIN_DTE = 180


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
        out = float(value)
    except Exception:
        return default
    return out if math.isfinite(out) else default


def _int(value: Any, default: int = 0) -> int:
    try:
        if value is None or pd.isna(value):
            return default
        return int(float(value))
    except Exception:
        return default


def _round_option_price(value: float) -> float:
    return round(max(0.01, value), 2)


def _asof_date(generated_at: str | None) -> pd.Timestamp:
    if generated_at:
        try:
            return pd.to_datetime(generated_at, utc=True).normalize()
        except Exception:
            pass
    return pd.Timestamp.now(tz="UTC").normalize()


def _dte(expiry: Any, generated_at: str | None) -> int | None:
    text = _text(expiry)
    if not text:
        return None
    try:
        expiry_date = pd.to_datetime(text, utc=True).normalize()
    except Exception:
        return None
    return int((expiry_date - _asof_date(generated_at)).days)


def _default_max_total_premium(account_budget: float) -> float:
    return round(min(account_budget * 0.50, 250.0), 2)


def _default_max_premium_per_order(account_budget: float) -> float:
    return round(min(account_budget * 0.30, 150.0), 2)


def _candidate_score(row: dict[str, Any]) -> float:
    rank = _float(row.get("rank_score"), default=0.0)
    fused = _float(row.get("fused_score"), default=0.0)
    confidence = _float(row.get("confidence"), default=0.0) / 100.0
    reward = _float(row.get("reward_dollars"), default=0.0)
    risk = _float(row.get("risk_dollars"), default=0.0)
    rr_bonus = min(reward / risk, 5.0) * 0.05 if risk > 0 else 0.0
    return max(rank, fused) + 0.25 * confidence + rr_bonus


def _rejection(
    row: dict[str, Any],
    reasons: list[str],
) -> dict[str, Any]:
    return {
        "ticker": _text(row.get("ticker_or_symbol")),
        "contract": _text(row.get("contract")),
        "option_side": _text(row.get("option_side")),
        "strike": row.get("strike"),
        "expiry": row.get("expiry"),
        "entry_price": row.get("entry_price"),
        "confidence": row.get("confidence"),
        "rank_score": row.get("rank_score"),
        "reasons": reasons,
    }


def _order_from_row(
    row: dict[str, Any],
    quantity: int,
    limit_buffer_pct: float,
    max_spread_pct: float,
) -> dict[str, Any]:
    entry = _float(row.get("entry_price"))
    limit_price = _round_option_price(entry * (1.0 + limit_buffer_pct))
    premium = round(entry * quantity * 100.0, 2)
    dte_value = _dte(row.get("expiry"), row.get("generated_at"))
    return {
        "asset": "option",
        "symbol": _text(row.get("ticker_or_symbol")).upper(),
        "action": "BUY_TO_OPEN",
        "order_type": "limit",
        "time_in_force": "day",
        "quantity": quantity,
        "contract": _text(row.get("contract")),
        "option_side": _text(row.get("option_side")).lower(),
        "strike": row.get("strike"),
        "expiry": row.get("expiry"),
        "dte": dte_value,
        "direction": _text(row.get("direction")),
        "reference_entry_price": entry,
        "max_limit_price": limit_price,
        "estimated_premium_dollars": premium,
        "stop_price_reference": row.get("stop_price"),
        "target_price_reference": row.get("target_price"),
        "confidence": row.get("confidence"),
        "rank_score": row.get("rank_score"),
        "fused_score": row.get("fused_score"),
        "risk_dollars_reference": row.get("risk_dollars"),
        "reward_dollars_reference": row.get("reward_dollars"),
        "trade_status": row.get("trade_status"),
        "max_allowed_spread_pct": max_spread_pct,
        "agent_instruction": (
            "Before placing this order, verify exact contract, current bid/ask/mid, spread, "
            "buying power, no duplicate exposure, and no breaking news invalidating the setup."
        ),
    }


def build_queue_from_candidates(
    candidates: pd.DataFrame,
    account_budget: float = DEFAULT_ACCOUNT_BUDGET,
    max_orders: int = DEFAULT_MAX_ORDERS,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    max_total_premium: float | None = None,
    max_premium_per_order: float | None = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    max_spread_pct: float = DEFAULT_MAX_SPREAD_PCT,
    limit_buffer_pct: float = DEFAULT_LIMIT_BUFFER_PCT,
    min_dte: int = DEFAULT_MIN_DTE,
    generated_at: str | None = None,
    kill_switch_present: bool = False,
) -> dict[str, Any]:
    """Return a loss-capped option execution queue for an external agent."""
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    account_budget = max(0.0, float(account_budget))
    max_orders = max(0, int(max_orders))
    max_candidates = max(0, int(max_candidates))
    min_dte = max(0, int(min_dte))
    max_total_premium = (
        _default_max_total_premium(account_budget)
        if max_total_premium is None
        else max(0.0, float(max_total_premium))
    )
    max_premium_per_order = (
        _default_max_premium_per_order(account_budget)
        if max_premium_per_order is None
        else max(0.0, float(max_premium_per_order))
    )

    orders: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    total_premium = 0.0
    rows = []
    if candidates is not None and not candidates.empty:
        rows = [
            {str(k): v for k, v in row.items()}
            for row in candidates.to_dict(orient="records")
        ]
        rows = sorted(rows, key=_candidate_score, reverse=True)

    if kill_switch_present:
        for row in rows:
            rejected.append(_rejection(row, ["kill switch file is present"]))

    for row in rows:
        if kill_switch_present:
            continue
        reasons: list[str] = []
        if _text(row.get("asset")).lower() != "option":
            reasons.append("not an option candidate")
        if _text(row.get("reason_excluded")):
            reasons.append(_text(row.get("reason_excluded")))
        if _text(row.get("trade_status")).lower() in {"watch", "skip", "blocked"}:
            reasons.append(f"trade_status is {row.get('trade_status')}")
        if _text(row.get("action")).upper() != "BUY_TO_OPEN":
            reasons.append("only BUY_TO_OPEN options are allowed")
        if _text(row.get("option_side")).lower() not in {"call", "put"}:
            reasons.append("missing option side")
        if not _text(row.get("ticker_or_symbol")):
            reasons.append("missing ticker")
        if not _text(row.get("expiry")):
            reasons.append("missing expiry")
        if _text(row.get("strike")) == "":
            reasons.append("missing strike")

        dte = _dte(row.get("expiry"), generated_at)
        entry = _float(row.get("entry_price"))
        confidence = _float(row.get("confidence"))
        spread = _float(row.get("spread_pct"), default=0.0)
        suggested_qty = _int(row.get("quantity") or row.get("suggested_contracts"))
        stop = _float(row.get("stop_price"))
        target = _float(row.get("target_price"))
        if entry <= 0:
            reasons.append("missing entry price")
        if suggested_qty <= 0:
            reasons.append("suggested quantity <= 0")
        if confidence < min_confidence:
            reasons.append(f"confidence below {min_confidence:g}")
        if dte is None:
            reasons.append("missing/invalid expiry date")
        elif dte < min_dte:
            reasons.append(f"dte below {min_dte}")
        if spread > max_spread_pct:
            reasons.append(f"spread above {max_spread_pct:.0%}")
        if stop <= 0:
            reasons.append("missing stop reference")
        if target <= 0:
            reasons.append("missing target reference")

        qty = 0
        if entry > 0:
            qty_by_order = math.floor(max_premium_per_order / (entry * 100.0))
            qty = min(suggested_qty, qty_by_order)
            if qty <= 0 and not reasons:
                reasons.append("premium cap leaves no buyable contracts")
        if len(orders) >= max_candidates and not reasons:
            reasons.append("max candidate count reached")

        if reasons:
            rejected.append(_rejection(row, reasons))
            continue

        order = _order_from_row(row, qty, limit_buffer_pct, max_spread_pct)
        orders.append(order)
        total_premium = round(total_premium + _float(order["estimated_premium_dollars"]), 2)

    status = "disabled" if kill_switch_present else "ready" if orders else "empty"
    return {
        "generated_at": generated_at,
        "schema": "optedge_robinhood_agentic_options_queue_v1",
        "status": status,
        "mode": "options_only_loss_capped",
        "does_not_place_orders": True,
        "account_budget": round(account_budget, 2),
        "max_orders": max_orders,
        "max_orders_to_submit": max_orders,
        "max_candidates": max_candidates,
        "max_total_premium": round(max_total_premium, 2),
        "max_premium_per_order": round(max_premium_per_order, 2),
        "min_confidence": min_confidence,
        "min_dte": min_dte,
        "max_spread_pct": max_spread_pct,
        "limit_buffer_pct": limit_buffer_pct,
        "estimated_total_candidate_premium": round(total_premium, 2),
        "kill_switch_file": str(DATA_DIR / KILL_SWITCH),
        "orders": orders,
        "rejected": rejected,
        "required_agent_checks": [
            "Use only the dedicated Robinhood Agentic account.",
            "Verify current buying power before every order.",
            "Verify the exact option contract in Robinhood: symbol, expiry, strike, call/put.",
            "Fetch current bid/ask/mid and skip if spread exceeds max_spread_pct.",
            "Use BUY_TO_OPEN limit DAY orders only; never use market orders.",
            "Do not exceed max_limit_price, max_orders, or max_total_premium.",
            "Skip if a same-symbol same-direction option position is already open.",
            "Do a quick current-news/catalyst sanity check before submitting.",
            "If any check is unclear, skip the order and record the reason.",
        ],
    }


def render_agent_prompt(queue: dict[str, Any]) -> str:
    orders = queue.get("orders") or []
    lines = [
        "# Optedge Robinhood Agentic Options Queue",
        "",
        "This is a handoff file for a Robinhood MCP/Codex trading agent.",
        "It is not an order ticket and Optedge has not placed any trades.",
        "",
        "## Hard Rules",
        "- Trade only in the dedicated Robinhood Agentic account.",
        "- Options only. No shares, crypto, futures, margin, or market orders.",
        "- Long-dated options only. Skip contracts below the queue minimum DTE.",
        "- Use BUY_TO_OPEN limit DAY orders only.",
        "- Do not exceed any max_limit_price in the queue.",
        "- Treat these as candidates. Submit at most max_orders_to_submit.",
        "- Do not exceed the queue max_orders_to_submit or max_total_premium.",
        "- Prefer contracts with at least the queue min_dte remaining.",
        "- Skip everything if the queue status is not ready.",
        "- Skip everything if the kill-switch file exists locally.",
        "- Double-check current Robinhood quotes and current news before submitting.",
        "- If any check is unclear, skip the order and record the reason.",
        "",
        "## Queue Summary",
        f"- Generated: {queue.get('generated_at')}",
        f"- Status: {queue.get('status')}",
        f"- Account budget: ${queue.get('account_budget')}",
        f"- Max total premium: ${queue.get('max_total_premium')}",
        f"- Max premium per order: ${queue.get('max_premium_per_order')}",
        f"- Minimum DTE: {queue.get('min_dte')}",
        f"- Max orders to submit: {queue.get('max_orders_to_submit')}",
        f"- Candidate orders: {len(orders)}",
        "",
        "## Required Double Checks",
    ]
    lines.extend(f"- {check}" for check in queue.get("required_agent_checks", []))
    lines.extend(["", "## Candidate Orders"])
    if not orders:
        lines.append("No candidate orders passed the queue filters.")
    for idx, order in enumerate(orders, start=1):
        lines.extend([
            f"### {idx}. {order['symbol']} {order['option_side'].upper()} "
            f"{order['strike']} {order['expiry']}",
            f"- Contract label: {order['contract']}",
            f"- Quantity: {order['quantity']}",
            f"- DTE: {order.get('dte')}",
            f"- Max limit price: {order['max_limit_price']}",
            f"- Estimated premium: ${order['estimated_premium_dollars']}",
            f"- Confidence: {order.get('confidence')}",
            f"- Rank score: {order.get('rank_score')}",
            f"- Stop reference: {order.get('stop_price_reference')}",
            f"- Target reference: {order.get('target_price_reference')}",
            "",
        ])
    lines.extend([
        "## Agent Output Required",
        "After reviewing, report each order as submitted or skipped with the exact reason.",
        "Do not claim execution unless Robinhood confirms the order.",
        "",
    ])
    return "\n".join(lines)


def build_robinhood_queue(
    data_dir: Path = DATA_DIR,
    account_budget: float = DEFAULT_ACCOUNT_BUDGET,
    max_orders: int = DEFAULT_MAX_ORDERS,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    max_total_premium: float | None = None,
    max_premium_per_order: float | None = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    max_spread_pct: float = DEFAULT_MAX_SPREAD_PCT,
    limit_buffer_pct: float = DEFAULT_LIMIT_BUFFER_PCT,
    min_dte: int = DEFAULT_MIN_DTE,
    query: str = "",
) -> dict[str, Any]:
    data_dir = Path(data_dir)
    candidates = build_external_orders(
        data_dir=data_dir,
        max_new=max(max_candidates * 4, max_orders * 4, max_candidates),
        max_open=30,
        max_options=max(max_candidates * 4, max_candidates),
        asset="option",
        dry_run=False,
        query=query,
    )
    return build_queue_from_candidates(
        candidates,
        account_budget=account_budget,
        max_orders=max_orders,
        max_candidates=max_candidates,
        max_total_premium=max_total_premium,
        max_premium_per_order=max_premium_per_order,
        min_confidence=min_confidence,
        max_spread_pct=max_spread_pct,
        limit_buffer_pct=limit_buffer_pct,
        min_dte=min_dte,
        kill_switch_present=(data_dir / KILL_SWITCH).exists(),
    )


def write_outputs(queue: dict[str, Any], data_dir: Path = DATA_DIR) -> tuple[Path, Path]:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    queue_path = data_dir / QUEUE_JSON
    prompt_path = data_dir / PROMPT_MD
    queue_path.write_text(json.dumps(queue, indent=2, default=str), encoding="utf-8")
    prompt_path.write_text(render_agent_prompt(queue), encoding="utf-8")
    return queue_path, prompt_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Export Robinhood agentic options queue")
    parser.add_argument("--account-budget", type=float, default=DEFAULT_ACCOUNT_BUDGET)
    parser.add_argument("--max-orders", type=int, default=DEFAULT_MAX_ORDERS)
    parser.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES)
    parser.add_argument("--max-total-premium", type=float, default=None)
    parser.add_argument("--max-premium-per-order", type=float, default=None)
    parser.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE)
    parser.add_argument("--max-spread-pct", type=float, default=DEFAULT_MAX_SPREAD_PCT)
    parser.add_argument("--limit-buffer-pct", type=float, default=DEFAULT_LIMIT_BUFFER_PCT)
    parser.add_argument("--min-dte", type=int, default=DEFAULT_MIN_DTE)
    parser.add_argument("--query", default="", help="Optional ticker or contract filter")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    queue = build_robinhood_queue(
        account_budget=args.account_budget,
        max_orders=args.max_orders,
        max_candidates=args.max_candidates,
        max_total_premium=args.max_total_premium,
        max_premium_per_order=args.max_premium_per_order,
        min_confidence=args.min_confidence,
        max_spread_pct=args.max_spread_pct,
        limit_buffer_pct=args.limit_buffer_pct,
        min_dte=args.min_dte,
        query=args.query,
    )
    if args.dry_run:
        print(json.dumps(queue, indent=2, default=str))
        print(render_agent_prompt(queue))
        return 0
    queue_path, prompt_path = write_outputs(queue)
    print(f"Robinhood agentic queue: {queue_path}")
    print(f"Robinhood agentic prompt: {prompt_path}")
    print(f"Selected option orders: {len(queue.get('orders') or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
