"""Normalize read-only Robinhood Agentic/MCP exports for Optedge.

This script does not connect to Robinhood and does not place orders. It turns a
raw JSON bundle of account/portfolio/position/order reads into the local
`data/robinhood_broker_snapshot.json` shape consumed by the cockpit.
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

DATA_DIR = ROOT / "data"
DEFAULT_INPUT = DATA_DIR / "robinhood_mcp_snapshot_raw.json"
DEFAULT_OUTPUT = DATA_DIR / "robinhood_broker_snapshot.json"
SNAPSHOT_SCHEMA = "optedge_robinhood_broker_snapshot_v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        out = float(value)
    except Exception:
        return default
    return out if math.isfinite(out) else default


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


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_clean(payload), indent=2, sort_keys=True), encoding="utf-8")


def _unwrap_rows(value: Any, preferred_keys: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Accept direct lists, paginated API shapes, or keyed account maps."""
    if value is None:
        return []
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
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
        "id",
    ):
        text = _text(raw.get(key))
        if text:
            return text
    return fallback


def _account_mask(account_number: str, raw: dict[str, Any]) -> str:
    explicit = _text(raw.get("account_mask") or raw.get("mask"))
    if explicit:
        return explicit
    if not account_number:
        return ""
    return f"...{account_number[-4:]}" if len(account_number) > 4 else account_number


def _normalize_account(raw: dict[str, Any], fallback_number: str = "") -> dict[str, Any]:
    account_number = _account_number(raw, fallback_number)
    agentic_allowed = _bool(raw.get("agentic_allowed"))
    option_level = _text(
        raw.get("option_level")
        or raw.get("options_level")
        or raw.get("option_approval_level")
        or raw.get("account_option_level")
    )
    return {
        "account_number": account_number,
        "account_mask": _account_mask(account_number, raw),
        "label": _text(raw.get("label") or raw.get("nickname") or raw.get("name")),
        "brokerage_account_type": _text(raw.get("brokerage_account_type") or raw.get("type")),
        "agentic_allowed": agentic_allowed if agentic_allowed is not None else False,
        "option_level": option_level,
        "buying_power": _float(raw.get("buying_power") or raw.get("cash_available_for_withdrawal")),
        "portfolio": raw.get("portfolio") if isinstance(raw.get("portfolio"), dict) else {},
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


def _expiration(raw: dict[str, Any]) -> str:
    return _text(
        raw.get("expiration_date")
        or raw.get("expiry")
        or raw.get("expiration")
        or raw.get("lastTradeDateOrContractMonth")
    )[:10]


def _normalize_equity_position(raw: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
    quantity = _float(raw.get("quantity") or raw.get("shares") or raw.get("qty"), 0.0) or 0.0
    avg = _float(raw.get("average_buy_price") or raw.get("average_price") or raw.get("avg_price"))
    current = _float(raw.get("current_price") or raw.get("mark_price") or raw.get("last_price"))
    return {
        "symbol": _find_symbol(raw),
        "quantity": quantity,
        "average_buy_price": avg,
        "average_price": avg,
        "current_price": current,
        "market_value": _float(raw.get("market_value")) or (quantity * current if current is not None else None),
        "account_mask": account.get("account_mask"),
        "account_label": account.get("label"),
        "account_agentic_allowed": account.get("agentic_allowed"),
    }


def _normalize_option_position(raw: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
    quantity = _float(raw.get("quantity") or raw.get("contracts") or raw.get("qty"), 0.0) or 0.0
    avg = _float(raw.get("average_price") or raw.get("avg_price") or raw.get("average_buy_price"))
    current = _float(raw.get("current_price") or raw.get("mark_price") or raw.get("last_price") or raw.get("adjusted_mark_price"))
    option_type = _option_side(raw)
    return {
        "symbol": _find_symbol(raw),
        "chain_symbol": _find_symbol(raw),
        "option_type": option_type,
        "side": option_type,
        "strike_price": _float(raw.get("strike_price") or raw.get("strike")),
        "expiration_date": _expiration(raw),
        "quantity": quantity,
        "average_price": avg,
        "current_price": current,
        "mark_price": current,
        "bid_price": _float(raw.get("bid_price") or raw.get("bid")),
        "ask_price": _float(raw.get("ask_price") or raw.get("ask")),
        "instrument_id": _text(raw.get("instrument_id") or raw.get("option_id") or raw.get("id")),
        "account_label": account.get("label") or account.get("account_mask"),
        "account_agentic_allowed": account.get("agentic_allowed"),
        "account_option_level": account.get("option_level"),
    }


def _normalize_order(raw: dict[str, Any], account: dict[str, Any], asset: str) -> dict[str, Any]:
    return {
        "asset": asset,
        "order_id": _text(raw.get("order_id") or raw.get("id")),
        "symbol": _find_symbol(raw),
        "state": _text(raw.get("state") or raw.get("status")),
        "side": _text(raw.get("side")),
        "quantity": raw.get("quantity"),
        "price": raw.get("price") or raw.get("limit_price"),
        "created_at": raw.get("created_at") or raw.get("created_at_utc"),
        "placed_agent": raw.get("placed_agent") or raw.get("source"),
        "account_mask": account.get("account_mask"),
    }


def _pick_account(accounts: dict[str, dict[str, Any]], account_number: str, fallback: str) -> dict[str, Any]:
    key = account_number or fallback
    if key not in accounts:
        accounts[key] = _normalize_account({"account_number": key}, key)
    return accounts[key]


def _merge_portfolio(account: dict[str, Any], raw: dict[str, Any]) -> None:
    account["portfolio"] = raw
    for key in ("buying_power", "cash", "cash_available_for_withdrawal"):
        if account.get("buying_power") is None and raw.get(key) is not None:
            account["buying_power"] = _float(raw.get(key))


def normalize_broker_snapshot(
    raw: Any,
    *,
    generated_at: str | None = None,
    account_number: str = "",
) -> dict[str, Any]:
    """Return a cockpit-compatible broker snapshot from a flexible raw bundle."""
    if isinstance(raw, dict) and raw.get("schema") == SNAPSHOT_SCHEMA:
        copy = dict(raw)
        copy.setdefault("generated_at", generated_at or _now())
        return copy

    bundle = raw if isinstance(raw, dict) else {"accounts": raw}
    fallback_account = _text(account_number)
    account_rows = _unwrap_rows(bundle.get("accounts") or bundle.get("get_accounts"), ("accounts", "results"))
    accounts: dict[str, dict[str, Any]] = {}
    for row in account_rows:
        acct = _normalize_account(row, fallback_account)
        key = acct.get("account_number") or fallback_account or acct.get("account_mask") or "account"
        accounts[str(key)] = acct
    if not accounts:
        key = fallback_account or "snapshot"
        accounts[key] = _normalize_account({"account_number": key}, key)

    for raw_portfolio in _unwrap_rows(bundle.get("portfolio") or bundle.get("portfolios") or bundle.get("get_portfolio")):
        acct_num = _account_number(raw_portfolio, fallback_account)
        _merge_portfolio(_pick_account(accounts, acct_num, fallback_account or "snapshot"), raw_portfolio)

    def attach_rows(raw_value: Any, attr: str, normalizer: Any, asset: str | None = None) -> None:
        for row in _unwrap_rows(raw_value):
            acct_num = _account_number(row, fallback_account)
            account = _pick_account(accounts, acct_num, fallback_account or "snapshot")
            if asset:
                account[attr].append(normalizer(row, account, asset))
            else:
                account[attr].append(normalizer(row, account))

    attach_rows(
        bundle.get("equity_positions")
        or bundle.get("stock_positions")
        or bundle.get("get_equity_positions"),
        "equity_positions",
        _normalize_equity_position,
    )
    attach_rows(
        bundle.get("option_positions")
        or bundle.get("options_positions")
        or bundle.get("get_option_positions"),
        "option_positions",
        _normalize_option_position,
    )
    attach_rows(
        bundle.get("equity_orders") or bundle.get("get_equity_orders"),
        "equity_orders",
        _normalize_order,
        "equity",
    )
    attach_rows(
        bundle.get("option_orders") or bundle.get("get_option_orders"),
        "option_orders",
        _normalize_order,
        "option",
    )

    account_list = list(accounts.values())
    option_positions = [pos for account in account_list for pos in account.get("option_positions", [])]
    equity_positions = [pos for account in account_list for pos in account.get("equity_positions", [])]
    option_orders = [row for account in account_list for row in account.get("option_orders", [])]
    equity_orders = [row for account in account_list for row in account.get("equity_orders", [])]
    return {
        "schema": SNAPSHOT_SCHEMA,
        "generated_at": generated_at or _now(),
        "source": "read_only_robinhood_agentic_mcp_export",
        "does_not_place_orders": True,
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
        },
        "notes": [
            "Normalized from read-only broker/account data for local reconciliation.",
            "This file is not broker confirmation and cannot place, cancel, or replace orders.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Normalize a read-only Robinhood snapshot for Optedge.")
    ap.add_argument("--input", default=str(DEFAULT_INPUT), help="Raw JSON bundle to normalize.")
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Normalized broker snapshot output path.")
    ap.add_argument("--account-number", default="", help="Fallback account number when raw rows omit it.")
    ap.add_argument("--dry-run", action="store_true", help="Print summary without writing the output file.")
    args = ap.parse_args(argv)

    input_path = Path(args.input)
    output_path = Path(args.output)
    raw = _read_json(input_path)
    snapshot = normalize_broker_snapshot(raw, account_number=args.account_number)
    summary = {
        "input": str(input_path),
        "output": str(output_path),
        "dry_run": bool(args.dry_run),
        **snapshot["counts"],
    }
    if not args.dry_run:
        _write_json(output_path, snapshot)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
