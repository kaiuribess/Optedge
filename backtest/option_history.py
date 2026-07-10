"""Read-only Robinhood option-history cache and request queue.

The authenticated Robinhood MCP connector is available to Codex, not to the
local Python process. This module defines a small, auditable handoff: Optedge
queues exact contracts that need history, a connected agent records normalized
read-only bars, and fixed-horizon validation consumes those bars when present.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
REQUESTS_PATH = DATA_DIR / "robinhood_option_history_requests.json"
PROMPT_PATH = DATA_DIR / "robinhood_option_history_prompt.md"
SNAPSHOT_PATH = DATA_DIR / "robinhood_option_history_snapshot.json"
COVERAGE_PATH = DATA_DIR / "robinhood_option_history_coverage.json"

REQUEST_SCHEMA = "optedge_robinhood_option_history_requests_v1"
SNAPSHOT_SCHEMA = "optedge_robinhood_option_history_snapshot_v1"
COVERAGE_SCHEMA = "optedge_robinhood_option_history_coverage_v1"
OCC_PATTERN = re.compile(r"^\s*([A-Z0-9.]+)\s+(\d{6})([CP])(\d{8})\s*$", re.I)


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "null"} else text


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).lower() in {"1", "true", "yes", "y", "trade", "buy", "long"}


def _first_text(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _first_number(*values: Any) -> float | None:
    for value in values:
        number = _float(value)
        if number is not None:
            return number
    return None


def normalize_expiry(value: Any) -> str:
    parsed = pd.to_datetime(value, errors="coerce", utc=True)
    return "" if pd.isna(parsed) else parsed.date().isoformat()


def normalize_side(value: Any) -> str:
    text = _text(value).lower()
    if text in {"c", "call", "calls"}:
        return "call"
    if text in {"p", "put", "puts"}:
        return "put"
    return ""


def normalize_strike(value: Any) -> str:
    number = _float(value)
    if number is None or number <= 0:
        return ""
    return f"{number:.4f}".rstrip("0").rstrip(".")


def contract_key(symbol: Any, expiry: Any, side: Any, strike: Any) -> str:
    symbol_text = _text(symbol).upper()
    expiry_text = normalize_expiry(expiry)
    side_text = normalize_side(side)
    strike_text = normalize_strike(strike)
    if not all((symbol_text, expiry_text, side_text, strike_text)):
        return ""
    return "|".join((symbol_text, expiry_text, side_text, strike_text))


def contract_key_from_row(row: Any) -> str:
    getter = row.get if hasattr(row, "get") else lambda _key: None
    return contract_key(
        _first_text(getter("ticker"), getter("symbol"), getter("chain_symbol")),
        _first_text(getter("expiry"), getter("expiration_date")),
        _first_text(getter("side"), getter("option_side"), getter("type")),
        _first_number(getter("strike"), getter("strike_price")),
    )


def parse_occ_symbol(value: Any) -> dict[str, Any]:
    match = OCC_PATTERN.match(_text(value).upper())
    if not match:
        return {}
    root, compact_date, right, raw_strike = match.groups()
    expiry = datetime.strptime(compact_date, "%y%m%d").date().isoformat()
    return {
        "symbol": root,
        "expiry": expiry,
        "side": "call" if right == "C" else "put",
        "strike": int(raw_strike) / 1000.0,
    }


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    temp.replace(path)


def _records_from_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    contracts = payload.get("contracts")
    if isinstance(contracts, list):
        return [row for row in contracts if isinstance(row, dict)]
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        return [row for row in data["results"] if isinstance(row, dict)]
    responses = payload.get("responses")
    if isinstance(responses, list):
        rows: list[dict[str, Any]] = []
        for response in responses:
            rows.extend(_records_from_payload(response))
        return rows
    return []


def normalize_contract_record(raw: dict[str, Any]) -> dict[str, Any] | None:
    occ = parse_occ_symbol(raw.get("occ_symbol"))
    symbol = _first_text(
        raw.get("symbol"), raw.get("chain_symbol"), occ.get("symbol"),
    ).upper()
    expiry = normalize_expiry(
        _first_text(raw.get("expiry"), raw.get("expiration_date"), occ.get("expiry"))
    )
    side = normalize_side(_first_text(raw.get("side"), raw.get("type"), occ.get("side")))
    strike = _first_number(raw.get("strike"), raw.get("strike_price"), occ.get("strike"))
    key = contract_key(symbol, expiry, side, strike)
    if not key:
        return None

    bars: dict[str, dict[str, Any]] = {}
    for bar in raw.get("bars") or []:
        if not isinstance(bar, dict):
            continue
        begins_at = _text(bar.get("begins_at"))
        close = _float(bar.get("close_price") if "close_price" in bar else bar.get("close"))
        if not begins_at or close is None or close < 0:
            continue
        bars[begins_at] = {
            "begins_at": begins_at,
            "open_price": _float(bar.get("open_price") if "open_price" in bar else bar.get("open")),
            "high_price": _float(bar.get("high_price") if "high_price" in bar else bar.get("high")),
            "low_price": _float(bar.get("low_price") if "low_price" in bar else bar.get("low")),
            "close_price": close,
            "session": _text(bar.get("session") or "reg"),
            "interpolated": bool(bar.get("interpolated", False)),
        }
    return {
        "contract_key": key,
        "symbol": symbol,
        "expiry": expiry,
        "side": side,
        "strike": strike,
        "instrument_id": _text(raw.get("instrument_id") or raw.get("id")),
        "occ_symbol": _text(raw.get("occ_symbol")),
        "state": _text(raw.get("state")),
        "tradability": _text(raw.get("tradability")),
        "interval": _text(raw.get("interval") or "day"),
        "bounds": _text(raw.get("bounds") or "regular"),
        "bars": sorted(bars.values(), key=lambda row: row["begins_at"]),
    }


def merge_snapshot_payload(existing: Any, incoming: Any, *, asof: datetime | None = None) -> dict[str, Any]:
    merged: dict[str, dict[str, Any]] = {}
    for payload in (existing, incoming):
        for raw in _records_from_payload(payload):
            row = normalize_contract_record(raw)
            if not row:
                continue
            key = row["contract_key"]
            prior = merged.get(key, {})
            bar_map: dict[str, dict[str, Any]] = {}
            for bar in (prior.get("bars") or []) + (row.get("bars") or []):
                begins_at = _text(bar.get("begins_at"))
                if not begins_at:
                    continue
                existing_bar = bar_map.get(begins_at)
                if (
                    existing_bar is not None
                    and not bool(existing_bar.get("interpolated", False))
                    and bool(bar.get("interpolated", False))
                ):
                    continue
                bar_map[begins_at] = bar
            combined = {**prior, **{k: v for k, v in row.items() if v not in ("", None, [])}}
            combined["bars"] = sorted(bar_map.values(), key=lambda bar: bar["begins_at"])
            merged[key] = combined
    now = asof or datetime.now(UTC)
    return {
        "schema": SNAPSHOT_SCHEMA,
        "generated_at": now.isoformat(),
        "source": "robinhood_mcp_read_only",
        "contracts": [merged[key] for key in sorted(merged)],
    }


def merge_snapshot_file(incoming_path: Path, snapshot_path: Path = SNAPSHOT_PATH) -> dict[str, Any]:
    existing = _read_json(snapshot_path, {})
    incoming = _read_json(incoming_path, {})
    payload = merge_snapshot_payload(existing, incoming)
    _write_json(snapshot_path, payload)
    return payload


def load_option_histories(path: Path = SNAPSHOT_PATH) -> dict[str, dict[str, Any]]:
    payload = _read_json(path, {})
    histories: dict[str, dict[str, Any]] = {}
    for raw in _records_from_payload(payload):
        row = normalize_contract_record(raw)
        if not row:
            continue
        frame_rows = []
        for bar in row.get("bars") or []:
            timestamp = pd.to_datetime(bar.get("begins_at"), errors="coerce", utc=True)
            close = _float(bar.get("close_price"))
            if pd.isna(timestamp) or close is None or close < 0:
                continue
            frame_rows.append({
                "timestamp": timestamp,
                "Close": close,
                "interpolated": bool(bar.get("interpolated", False)),
                "_session_date": timestamp.date(),
            })
        frame = pd.DataFrame(frame_rows)
        if not frame.empty:
            frame = frame.sort_values("timestamp").set_index("timestamp")
            frame = frame.drop_duplicates("_session_date", keep="last")
        histories[row["contract_key"]] = {**row, "history": frame}
    return histories


def observed_option_close(record: dict[str, Any] | None, target_date: date) -> tuple[float, dict[str, Any]] | None:
    if not isinstance(record, dict):
        return None
    history = record.get("history")
    if not isinstance(history, pd.DataFrame) or history.empty:
        return None
    rows = history[
        (history["_session_date"] == target_date)
        & ~history["interpolated"].fillna(False).astype(bool)
    ]
    if rows.empty:
        return None
    price = _float(rows.iloc[-1].get("Close"))
    if price is None or price < 0:
        return None
    return price, {
        "option_instrument_id": record.get("instrument_id"),
        "occ_symbol": record.get("occ_symbol"),
        "option_bar_interpolated": False,
        "option_bar_date": target_date.isoformat(),
    }


def _last_required_date(asof: datetime) -> date:
    return (pd.Timestamp(asof.date()) - pd.offsets.BDay(1)).date()


def build_requests(
    signals: pd.DataFrame,
    *,
    snapshot_path: Path = SNAPSHOT_PATH,
    asof: datetime | None = None,
    max_requests: int = 50,
) -> dict[str, Any]:
    now = asof or datetime.now(UTC)
    if signals is None or signals.empty:
        option_rows = pd.DataFrame()
    else:
        option_rows = signals.copy()
        if "asset" in option_rows.columns:
            option_rows = option_rows[
                option_rows["asset"].fillna("").astype(str).str.lower().eq("option")
            ].copy()
        else:
            sides = option_rows.get("side", pd.Series("", index=option_rows.index))
            option_rows = option_rows[sides.fillna("").astype(str).str.lower().isin({"call", "put"})].copy()
    histories = load_option_histories(snapshot_path)
    required_end = _last_required_date(now)
    grouped: dict[str, dict[str, Any]] = {}
    for raw in option_rows.to_dict(orient="records"):
        key = contract_key_from_row(raw)
        if not key:
            continue
        symbol, expiry, side, strike = key.split("|")
        entry = pd.to_datetime(raw.get("entry_time") or raw.get("log_time"), errors="coerce", utc=True)
        if pd.isna(entry):
            continue
        cached = histories.get(key)
        cached_frame = cached.get("history") if cached else None
        cached_dates = (
            list(cached_frame.loc[
                ~cached_frame["interpolated"].fillna(False).astype(bool), "_session_date"
            ])
            if isinstance(cached_frame, pd.DataFrame) and not cached_frame.empty else []
        )
        expiry_date = pd.Timestamp(expiry).date()
        contract_required_end = min(required_end, expiry_date)
        cache_complete = bool(
            cached_dates
            and min(cached_dates) <= entry.date()
            and max(cached_dates) >= contract_required_end
        )
        if cache_complete:
            continue
        strategy = _truthy(raw.get("strategy_qualified_pre_guard"))
        executable = _truthy(raw.get("is_actionable")) or _text(raw.get("trade_status")).lower() == "trade"
        directional = _float(raw.get("buyer_edge_pct")) is not None
        priority = 0 if strategy else 1 if executable else 2 if directional else 3
        item = grouped.setdefault(key, {
            "request_id": key,
            "contract_key": key,
            "symbol": symbol,
            "expiry": expiry,
            "side": side,
            "strike": float(strike),
            "state": "active" if expiry >= now.date().isoformat() else "expired",
            "start_time": entry.date().isoformat() + "T00:00:00Z",
            "end_time": (
                now.isoformat().replace("+00:00", "Z")
                if expiry_date >= now.date()
                else expiry_date.isoformat() + "T23:59:59Z"
            ),
            "interval": "day",
            "bounds": "regular",
            "priority": priority,
            "signal_count": 0,
            "latest_entry_time": entry.isoformat(),
            "cached_through": max(cached_dates).isoformat() if cached_dates else None,
        })
        item["signal_count"] += 1
        item["priority"] = min(item["priority"], priority)
        item["start_time"] = min(item["start_time"], entry.date().isoformat() + "T00:00:00Z")
        item["latest_entry_time"] = max(item["latest_entry_time"], entry.isoformat())
    def request_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
        latest = pd.to_datetime(row.get("latest_entry_time"), errors="coerce", utc=True)
        recency = -latest.timestamp() if not pd.isna(latest) else 0.0
        return row["priority"], -row["signal_count"], recency, row["contract_key"]

    requests = sorted(
        grouped.values(),
        key=request_sort_key,
    )[: max(0, int(max_requests))]
    return {
        "schema": REQUEST_SCHEMA,
        "generated_at": now.isoformat(),
        "read_only": True,
        "max_requests": max(0, int(max_requests)),
        "request_count": len(requests),
        "requests": requests,
        "instructions": [
            "Resolve each exact contract with get_option_instruments.",
            "Fetch regular-session day bars with get_option_historicals.",
            "Ignore interpolated bars for validation.",
            "Merge results into robinhood_option_history_snapshot.json; never place an order.",
        ],
    }


def request_prompt(packet: dict[str, Any]) -> str:
    count = int(packet.get("request_count") or 0)
    return f"""# Robinhood Option History Refresh

This is a read-only market-data task for {count} exact option contract(s).

1. Read `data/robinhood_option_history_requests.json`.
2. For each request, call `get_option_instruments` with its symbol, expiry, strike, side, and state.
3. Call `get_option_historicals` for the resolved instrument UUID using the requested time range, `interval=day`, and `bounds=regular`.
4. Write or merge normalized contract records into `data/robinhood_option_history_snapshot.json` using schema `{SNAPSHOT_SCHEMA}`.
5. Preserve instrument_id, OCC symbol, exact contract fields, and every returned bar including the interpolated flag.
6. Run `python scripts/refresh_robinhood_option_history.py --status` to refresh coverage.

Do not call review, place, cancel, watchlist, scanner-write, or any other broker-write tool. These bars are validation evidence, not proof of a fill.
"""


def build_coverage(
    *,
    snapshot_path: Path = SNAPSHOT_PATH,
    requests: dict[str, Any] | None = None,
    outcomes: pd.DataFrame | None = None,
    asof: datetime | None = None,
) -> dict[str, Any]:
    now = asof or datetime.now(UTC)
    histories = load_option_histories(snapshot_path)
    bars = sum(
        int((~record["history"].get("interpolated", pd.Series(dtype=bool)).fillna(False)).sum())
        for record in histories.values()
        if isinstance(record.get("history"), pd.DataFrame) and not record["history"].empty
    )
    option_outcomes = pd.DataFrame()
    if outcomes is not None and not outcomes.empty and "asset" in outcomes.columns:
        option_outcomes = outcomes[outcomes["asset"].astype(str).str.lower().eq("option")].copy()
        if "is_scored" in option_outcomes.columns:
            option_outcomes = option_outcomes[option_outcomes["is_scored"].fillna(False).astype(bool)]
    quality = (
        option_outcomes.get("outcome_quality", pd.Series(dtype=str)).fillna("unknown").value_counts().to_dict()
        if not option_outcomes.empty else {}
    )
    observed = int(quality.get("broker_market_observed", 0))
    total = int(len(option_outcomes))
    return {
        "schema": COVERAGE_SCHEMA,
        "generated_at": now.isoformat(),
        "source": "robinhood_mcp_read_only",
        "cached_contracts": len(histories),
        "non_interpolated_bars": bars,
        "pending_requests": int((requests or {}).get("request_count") or 0),
        "option_outcomes": total,
        "broker_observed_option_outcomes": observed,
        "modeled_option_outcomes": int(quality.get("modeled_option_proxy", 0)),
        "broker_observed_coverage_pct": observed / total if total else None,
        "quality_counts": quality,
        "notes": [
            "Observed means a Robinhood regular-session option trade bar matched the exact target date.",
            "Interpolated bars are excluded. Missing exact bars fall back to the labeled pricing proxy.",
            "Historical trade bars are market observations, not evidence that Optedge received a fill.",
        ],
    }


def refresh_artifacts(
    signals: pd.DataFrame,
    *,
    outcomes: pd.DataFrame | None = None,
    data_dir: Path = DATA_DIR,
    max_requests: int = 50,
    asof: datetime | None = None,
) -> dict[str, Any]:
    requests_path = data_dir / REQUESTS_PATH.name
    prompt_path = data_dir / PROMPT_PATH.name
    snapshot_path = data_dir / SNAPSHOT_PATH.name
    coverage_path = data_dir / COVERAGE_PATH.name
    packet = build_requests(
        signals, snapshot_path=snapshot_path, asof=asof, max_requests=max_requests,
    )
    _write_json(requests_path, packet)
    prompt_path.write_text(request_prompt(packet), encoding="utf-8")
    coverage = build_coverage(
        snapshot_path=snapshot_path, requests=packet, outcomes=outcomes, asof=asof,
    )
    _write_json(coverage_path, coverage)
    return {"requests": packet, "coverage": coverage}


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh read-only Robinhood option-history artifacts")
    parser.add_argument("--ingest", type=Path, help="Merge a normalized MCP response JSON into the cache")
    parser.add_argument("--max-requests", type=int, default=50)
    parser.add_argument("--status", action="store_true", help="Print coverage after refreshing artifacts")
    args = parser.parse_args()
    if args.ingest:
        merge_snapshot_file(args.ingest)
    from backtest.fixed_horizon import OUTCOMES_PATH
    from backtest.forward import _load_all_logs

    signals = _load_all_logs()
    outcomes = pd.read_parquet(OUTCOMES_PATH) if OUTCOMES_PATH.exists() else pd.DataFrame()
    result = refresh_artifacts(signals, outcomes=outcomes, max_requests=args.max_requests)
    if args.status or not args.ingest:
        print(json.dumps(result["coverage"], indent=2))
        print(f"Requests: {REQUESTS_PATH}")
        print(f"Prompt: {PROMPT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
