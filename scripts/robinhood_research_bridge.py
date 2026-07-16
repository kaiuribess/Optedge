# Purpose: Queue and cache read-only Robinhood market research.
"""Read-only Robinhood research cache for interactive ticker and option lookups.

The local cockpit cannot call the authenticated Robinhood MCP server directly.
It writes a bounded request queue instead. A connected Codex heartbeat can use
read-only market-data tools, merge normalized results into the snapshot, and
the regular lookup path consumes that cache with explicit freshness labels.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.option_history import contract_key  # noqa: E402, I001

DATA_DIR = ROOT / "data"
REQUESTS_PATH = DATA_DIR / "robinhood_research_requests.json"
PROMPT_PATH = DATA_DIR / "robinhood_research_prompt.md"
SNAPSHOT_PATH = DATA_DIR / "robinhood_research_snapshot.json"
COVERAGE_PATH = DATA_DIR / "robinhood_research_coverage.json"

REQUEST_SCHEMA = "optedge_robinhood_research_requests_v1"
SNAPSHOT_SCHEMA = "optedge_robinhood_research_snapshot_v1"
COVERAGE_SCHEMA = "optedge_robinhood_research_coverage_v1"
FRESH_MINUTES = 20.0
AGING_MINUTES = 90.0


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


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else default
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    temp.replace(path)


def _parse_time(value: Any) -> pd.Timestamp | None:
    parsed = pd.to_datetime(value, errors="coerce", utc=True)
    return None if pd.isna(parsed) else parsed


def _age_minutes(value: Any, asof: datetime | None = None) -> float | None:
    parsed = _parse_time(value)
    if parsed is None:
        return None
    now = pd.Timestamp(asof or datetime.now(UTC))
    return max(0.0, (now - parsed).total_seconds() / 60.0)


def freshness(value: Any, asof: datetime | None = None) -> str:
    age = _age_minutes(value, asof)
    if age is None:
        return "unknown"
    if age <= FRESH_MINUTES:
        return "fresh"
    if age <= AGING_MINUTES:
        return "aging"
    return "stale"


def _option_request_key(symbol: str, request: dict[str, Any] | None) -> str:
    if not request or str(request.get("asset") or "").lower() != "option":
        return ""
    return contract_key(
        symbol,
        request.get("expiry"),
        request.get("side"),
        request.get("strike"),
    )


def request_id(symbol: str, request: dict[str, Any] | None = None) -> str:
    option_key = _option_request_key(symbol, request)
    return f"option:{option_key}" if option_key else f"equity:{symbol.strip().upper()}"


def _records(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("records")
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    if isinstance(rows, dict):
        return [row for row in rows.values() if isinstance(row, dict)]
    return []


def merge_snapshot_payload(
    existing: Any,
    incoming: Any,
    *,
    asof: datetime | None = None,
) -> dict[str, Any]:
    merged: dict[str, dict[str, Any]] = {}
    for payload in (existing, incoming):
        for raw in _records(payload):
            symbol = _text(raw.get("symbol")).upper()
            rid = _text(raw.get("request_id"))
            if not rid and symbol:
                rid = request_id(symbol, raw.get("option_request"))
            if not rid or not symbol:
                continue
            prior = merged.get(rid, {})
            combined = {**prior, **raw, "request_id": rid, "symbol": symbol}
            collected = _text(combined.get("collected_at") or combined.get("generated_at"))
            combined["collected_at"] = collected or (asof or datetime.now(UTC)).isoformat()
            merged[rid] = combined
    now = asof or datetime.now(UTC)
    rows = sorted(
        merged.values(),
        key=lambda row: (_text(row.get("symbol")), _text(row.get("request_id"))),
    )
    return {
        "schema": SNAPSHOT_SCHEMA,
        "generated_at": now.isoformat(),
        "source": "robinhood_mcp_read_only",
        "records": rows,
    }


def merge_snapshot_file(
    incoming_path: Path,
    snapshot_path: Path = SNAPSHOT_PATH,
) -> dict[str, Any]:
    payload = merge_snapshot_payload(
        _read_json(snapshot_path, {}),
        _read_json(incoming_path, {}),
    )
    _write_json(snapshot_path, payload)
    return payload


def load_snapshot(path: Path = SNAPSHOT_PATH) -> dict[str, Any]:
    payload = _read_json(path, {})
    return (
        merge_snapshot_payload({}, payload)
        if payload
        else {
            "schema": SNAPSHOT_SCHEMA,
            "generated_at": None,
            "source": "robinhood_mcp_read_only",
            "records": [],
        }
    )


def _matching_records(
    symbol: str,
    option_request: dict[str, Any] | None,
    snapshot_path: Path,
) -> list[dict[str, Any]]:
    clean = symbol.strip().upper()
    option_key = _option_request_key(clean, option_request)
    rows = [
        row
        for row in _records(load_snapshot(snapshot_path))
        if _text(row.get("symbol")).upper() == clean
    ]
    if option_key:
        exact = [
            row
            for row in rows
            if _option_request_key(clean, row.get("option_request")) == option_key
        ]
        if exact:
            rows = exact + [row for row in rows if row not in exact]
    return sorted(
        rows,
        key=lambda row: _parse_time(row.get("collected_at")) or pd.Timestamp.min.tz_localize("UTC"),
        reverse=True,
    )


def _current_equity_price(quote: dict[str, Any]) -> tuple[float | None, str, str]:
    candidates = []
    regular = _number(quote.get("last_trade_price"))
    regular_time = _parse_time(quote.get("venue_last_trade_time"))
    if regular is not None and regular > 0:
        candidates.append((regular_time, regular, "regular"))
    extended = _number(quote.get("last_non_reg_trade_price"))
    extended_time = _parse_time(quote.get("venue_last_non_reg_trade_time"))
    if extended is not None and extended > 0:
        candidates.append((extended_time, extended, "extended"))
    if not candidates:
        return None, "unknown", ""
    candidates.sort(
        key=lambda item: item[0] or pd.Timestamp.min.tz_localize("UTC"),
        reverse=True,
    )
    timestamp, price, session = candidates[0]
    return price, session, timestamp.isoformat() if timestamp is not None else ""


def _spread(bid: Any, ask: Any) -> tuple[float | None, float | None]:
    bid_value = _number(bid)
    ask_value = _number(ask)
    if bid_value is None or ask_value is None or bid_value <= 0 or ask_value < bid_value:
        return None, None
    mid = (bid_value + ask_value) / 2.0
    return mid, (ask_value - bid_value) / mid if mid > 0 else None


def _history_metrics(history: dict[str, Any]) -> dict[str, Any]:
    bars = [
        bar
        for bar in (history.get("bars") or [])
        if isinstance(bar, dict) and not bool(bar.get("interpolated", False))
    ]
    closes = [
        _number(bar.get("close_price"))
        for bar in bars
        if _number(bar.get("close_price")) is not None
    ]
    if not closes:
        return {"broker_history_rows": 0}

    def period_return(lookback: int) -> float | None:
        if len(closes) <= lookback or closes[-lookback - 1] in {None, 0}:
            return None
        return closes[-1] / closes[-lookback - 1] - 1.0

    return {
        "broker_history_rows": len(closes),
        "broker_ret_5d": period_return(5),
        "broker_ret_20d": period_return(20),
        "broker_ret_60d": period_return(60),
        "broker_history_last_date": _text(bars[-1].get("begins_at"))[:10],
    }


def _upcoming_earnings(rows: list[dict[str, Any]], asof: datetime) -> dict[str, Any]:
    today = asof.date()
    upcoming = []
    for row in rows:
        report = row.get("report") if isinstance(row.get("report"), dict) else {}
        report_date = pd.to_datetime(report.get("date"), errors="coerce")
        if pd.isna(report_date) or report_date.date() < today:
            continue
        upcoming.append((report_date.date(), row, report))
    if not upcoming:
        return {}
    report_date, row, report = min(upcoming, key=lambda item: item[0])
    eps = row.get("eps") if isinstance(row.get("eps"), dict) else {}
    return {
        "next_earnings_date": report_date.isoformat(),
        "days_to_earnings": (report_date - today).days,
        "earnings_timing": report.get("timing"),
        "earnings_verified": bool(report.get("verified")),
        "earnings_eps_estimate": _number(eps.get("estimate")),
    }


def flatten_equity_record(
    record: dict[str, Any],
    *,
    asof: datetime | None = None,
) -> dict[str, Any]:
    now = asof or datetime.now(UTC)
    quote_result = (
        record.get("equity_quote") if isinstance(record.get("equity_quote"), dict) else {}
    )
    quote = (
        quote_result.get("quote") if isinstance(quote_result.get("quote"), dict) else quote_result
    )
    official_close = (
        quote_result.get("close") if isinstance(quote_result.get("close"), dict) else {}
    )
    fundamentals = (
        record.get("fundamentals") if isinstance(record.get("fundamentals"), dict) else {}
    )
    earnings = record.get("earnings") if isinstance(record.get("earnings"), list) else []
    history = record.get("equity_history") if isinstance(record.get("equity_history"), dict) else {}
    price, session, quote_time = _current_equity_price(quote)
    mid, spread_pct = _spread(quote.get("bid_price"), quote.get("ask_price"))
    collected_at = record.get("collected_at")
    data_time = quote_time or collected_at
    row = {
        "symbol": _text(record.get("symbol")).upper(),
        "current_price": price,
        "price_session": session,
        "quote_updated_at": quote_time,
        "bid_price": _number(quote.get("bid_price")),
        "ask_price": _number(quote.get("ask_price")),
        "mid_price": mid,
        "spread_pct": spread_pct,
        "official_close": _number(official_close.get("price")),
        "official_close_date": official_close.get("date"),
        "official_close_interpolated": official_close.get("interpolated"),
        "market_cap": _number(fundamentals.get("market_cap")),
        "pe_ratio": _number(fundamentals.get("pe_ratio")),
        "pb_ratio": _number(fundamentals.get("pb_ratio")),
        "sector": fundamentals.get("sector"),
        "industry": fundamentals.get("industry"),
        "volume": _number(fundamentals.get("volume")),
        "average_volume_30d": _number(fundamentals.get("average_volume_30_days")),
        "high_52w": _number(fundamentals.get("high_52_weeks")),
        "low_52w": _number(fundamentals.get("low_52_weeks")),
        "float_shares": _number(fundamentals.get("float")),
        "financial_status": fundamentals.get("financial_status_description"),
        "quote_age_min": _age_minutes(quote_time, now),
        "collection_age_min": _age_minutes(collected_at, now),
        "snapshot_age_min": _age_minutes(data_time, now),
        "snapshot_freshness": freshness(data_time, now),
        "source": "robinhood_mcp_read_only",
    }
    row.update(_history_metrics(history))
    row.update(_upcoming_earnings(earnings, now))
    return row


def flatten_option_contract(
    raw: dict[str, Any],
    collected_at: Any,
    *,
    asof: datetime | None = None,
) -> dict[str, Any]:
    now = asof or datetime.now(UTC)
    instrument = raw.get("instrument") if isinstance(raw.get("instrument"), dict) else {}
    quote_result = raw.get("quote") if isinstance(raw.get("quote"), dict) else {}
    quote = (
        quote_result.get("quote") if isinstance(quote_result.get("quote"), dict) else quote_result
    )
    official_close = (
        quote_result.get("close") if isinstance(quote_result.get("close"), dict) else {}
    )
    bid = _number(quote.get("bid_price"))
    ask = _number(quote.get("ask_price"))
    mid, spread_pct = _spread(bid, ask)
    expiry = _text(instrument.get("expiration_date"))
    expiry_date = pd.to_datetime(expiry, errors="coerce")
    dte = None if pd.isna(expiry_date) else (expiry_date.date() - now.date()).days
    quote_time = quote.get("updated_at")
    data_time = quote_time or collected_at
    return {
        "symbol": _text(instrument.get("chain_symbol")).upper(),
        "instrument_id": _text(instrument.get("id") or quote.get("instrument_id")),
        "side": _text(instrument.get("type")).lower(),
        "strike": _number(instrument.get("strike_price")),
        "expiry": expiry,
        "dte": dte,
        "state": instrument.get("state"),
        "tradability": instrument.get("tradability"),
        "sellout_datetime": instrument.get("sellout_datetime"),
        "mark_price": _number(quote.get("mark_price")),
        "bid_price": bid,
        "ask_price": ask,
        "mid_price": mid,
        "spread_pct": spread_pct,
        "bid_size": quote.get("bid_size"),
        "ask_size": quote.get("ask_size"),
        "volume": quote.get("volume"),
        "open_interest": quote.get("open_interest"),
        "implied_volatility": _number(quote.get("implied_volatility")),
        "delta": _number(quote.get("delta")),
        "gamma": _number(quote.get("gamma")),
        "theta": _number(quote.get("theta")),
        "vega": _number(quote.get("vega")),
        "chance_of_profit_long": _number(quote.get("chance_of_profit_long")),
        "break_even_price": _number(quote.get("break_even_price")),
        "low_fill_rate_buy_price": _number(quote.get("low_fill_rate_buy_price")),
        "high_fill_rate_buy_price": _number(quote.get("high_fill_rate_buy_price")),
        "official_close": _number(official_close.get("price")),
        "official_close_date": official_close.get("date"),
        "quote_updated_at": quote_time,
        "quote_age_min": _age_minutes(quote_time, now),
        "collection_age_min": _age_minutes(collected_at, now),
        "snapshot_age_min": _age_minutes(data_time, now),
        "snapshot_freshness": freshness(data_time, now),
        "source": "robinhood_mcp_read_only",
    }


def lookup_sections(
    symbol: str,
    option_request: dict[str, Any] | None = None,
    *,
    data_dir: Path = DATA_DIR,
    asof: datetime | None = None,
) -> dict[str, list[dict[str, Any]]]:
    rows = _matching_records(
        symbol,
        option_request,
        data_dir / SNAPSHOT_PATH.name,
    )
    if not rows:
        return {"robinhood_research": [], "robinhood_option_quotes": []}
    equity = [flatten_equity_record(rows[0], asof=asof)]
    option_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    requested_key = _option_request_key(symbol, option_request)
    for record in rows:
        for raw in record.get("option_contracts") or []:
            if not isinstance(raw, dict):
                continue
            flattened = flatten_option_contract(
                raw,
                record.get("collected_at"),
                asof=asof,
            )
            key = contract_key(
                flattened.get("symbol"),
                flattened.get("expiry"),
                flattened.get("side"),
                flattened.get("strike"),
            )
            if requested_key and key != requested_key:
                continue
            if not key or key in seen:
                continue
            seen.add(key)
            option_rows.append(flattened)
    return {
        "robinhood_research": equity,
        "robinhood_option_quotes": option_rows,
    }


def _latest_record_for_request(
    rid: str,
    snapshot_path: Path,
) -> dict[str, Any] | None:
    matches = [
        row for row in _records(load_snapshot(snapshot_path)) if row.get("request_id") == rid
    ]
    if not matches:
        return None
    return max(
        matches,
        key=lambda row: _parse_time(row.get("collected_at")) or pd.Timestamp.min.tz_localize("UTC"),
    )


def build_request(
    query: str,
    symbol: str,
    option_request: dict[str, Any] | None = None,
    *,
    asof: datetime | None = None,
) -> dict[str, Any]:
    now = asof or datetime.now(UTC)
    clean = symbol.strip().upper()
    option_key = _option_request_key(clean, option_request)
    return {
        "request_id": request_id(clean, option_request),
        "query": query,
        "symbol": clean,
        "asset": "option" if option_key else "equity",
        "option_request": option_request if option_key else None,
        "requested_at": now.isoformat(),
        "history_start_time": (now - timedelta(days=190)).isoformat(),
        "history_end_time": now.isoformat(),
        "priority": 0 if option_key else 1,
        "status": "pending",
        "read_only": True,
        "tools": [
            "get_equity_quotes",
            "get_equity_fundamentals",
            "get_equity_historicals",
            "get_earnings_results",
            "get_option_chains",
        ]
        + (
            ["get_option_instruments", "get_option_quotes", "get_option_historicals"]
            if option_key
            else []
        ),
    }


def request_prompt(packet: dict[str, Any]) -> str:
    return f"""# Robinhood Lookup Research Refresh

Process up to 5 pending rows from `data/{REQUESTS_PATH.name}` using read-only Robinhood tools only.

For every request, collect the exact symbol's equity quote, official close, fundamentals, six months of split-adjusted regular-session day bars, earnings results, and option-chain metadata. For an option request, resolve the exact expiry/strike/side with `get_option_instruments`, then collect `get_option_quotes` and recent `get_option_historicals` for that UUID.

Merge one record per request into `data/{SNAPSHOT_PATH.name}` using schema `{SNAPSHOT_SCHEMA}`. Each record contains `request_id`, `query`, `symbol`, `option_request`, `collected_at`, `equity_quote`, `fundamentals`, `earnings`, `equity_history`, `option_chain`, and `option_contracts`. Store the matching result objects, not tool guides. Preserve existing records and never copy account numbers, positions, orders, credentials, or unrelated symbols.

After merging, run `python scripts/robinhood_research_bridge.py --status`.

Do not call order, cancel, watchlist-write, scanner-write, or any other broker mutation tool. This cache is research evidence and never proves a fill.

Pending requests: {int(packet.get("pending_count") or 0)}.
"""


def coverage(
    packet: dict[str, Any],
    *,
    snapshot_path: Path = SNAPSHOT_PATH,
    asof: datetime | None = None,
) -> dict[str, Any]:
    now = asof or datetime.now(UTC)
    records = _records(load_snapshot(snapshot_path))
    freshness_counts: dict[str, int] = {}
    for row in records:
        label = freshness(row.get("collected_at"), now)
        freshness_counts[label] = freshness_counts.get(label, 0) + 1
    return {
        "schema": COVERAGE_SCHEMA,
        "generated_at": now.isoformat(),
        "source": "robinhood_mcp_read_only",
        "request_count": len(packet.get("requests") or []),
        "pending_requests": int(packet.get("pending_count") or 0),
        "cached_records": len(records),
        "cached_symbols": len(
            {_text(row.get("symbol")).upper() for row in records if row.get("symbol")}
        ),
        "freshness_counts": freshness_counts,
        "notes": [
            "This cache contains market research only, never broker orders or credentials.",
            "Live labels require a recent upstream quote timestamp and fresh collection time.",
            "Stale or missing broker research cannot make a setup actionable.",
        ],
    }


def queue_request(
    query: str,
    symbol: str,
    option_request: dict[str, Any] | None = None,
    *,
    data_dir: Path = DATA_DIR,
    asof: datetime | None = None,
    max_requests: int = 25,
) -> dict[str, Any]:
    now = asof or datetime.now(UTC)
    requests_path = data_dir / REQUESTS_PATH.name
    snapshot_path = data_dir / SNAPSHOT_PATH.name
    packet = _read_json(requests_path, {})
    existing = {
        _text(row.get("request_id")): row
        for row in (packet.get("requests") or [])
        if isinstance(row, dict) and _text(row.get("request_id"))
    }
    row = build_request(query, symbol, option_request, asof=now)
    cached = _latest_record_for_request(row["request_id"], snapshot_path)
    cache_freshness = freshness(cached.get("collected_at"), now) if cached else "missing"
    row["status"] = "satisfied" if cache_freshness == "fresh" else "pending"
    row["cache_freshness"] = cache_freshness
    existing[row["request_id"]] = row
    requests = sorted(
        existing.values(),
        key=lambda item: (
            0 if item.get("status") == "pending" else 1,
            int(item.get("priority") or 0),
            -(
                _parse_time(item.get("requested_at")) or pd.Timestamp.min.tz_localize("UTC")
            ).timestamp(),
        ),
    )[: max(1, int(max_requests))]
    output = {
        "schema": REQUEST_SCHEMA,
        "generated_at": now.isoformat(),
        "read_only": True,
        "request_count": len(requests),
        "pending_count": sum(1 for item in requests if item.get("status") == "pending"),
        "requests": requests,
    }
    _write_json(requests_path, output)
    (data_dir / PROMPT_PATH.name).write_text(request_prompt(output), encoding="utf-8")
    _write_json(
        data_dir / COVERAGE_PATH.name,
        coverage(output, snapshot_path=snapshot_path, asof=now),
    )
    return row


def refresh_status(
    *,
    data_dir: Path = DATA_DIR,
    asof: datetime | None = None,
) -> dict[str, Any]:
    now = asof or datetime.now(UTC)
    requests_path = data_dir / REQUESTS_PATH.name
    snapshot_path = data_dir / SNAPSHOT_PATH.name
    packet = _read_json(requests_path, {})
    requests = []
    for raw in packet.get("requests") or []:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        cached = _latest_record_for_request(_text(row.get("request_id")), snapshot_path)
        cache_freshness = freshness(cached.get("collected_at"), now) if cached else "missing"
        row["cache_freshness"] = cache_freshness
        row["status"] = "satisfied" if cache_freshness == "fresh" else "pending"
        requests.append(row)
    packet = {
        "schema": REQUEST_SCHEMA,
        "generated_at": now.isoformat(),
        "read_only": True,
        "request_count": len(requests),
        "pending_count": sum(1 for row in requests if row.get("status") == "pending"),
        "requests": requests,
    }
    _write_json(requests_path, packet)
    (data_dir / PROMPT_PATH.name).write_text(request_prompt(packet), encoding="utf-8")
    report = coverage(packet, snapshot_path=snapshot_path, asof=now)
    _write_json(data_dir / COVERAGE_PATH.name, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manage the read-only Robinhood lookup research cache"
    )
    parser.add_argument("--queue", help="Queue a ticker or company lookup")
    parser.add_argument("--ingest", type=Path, help="Merge a normalized read-only snapshot JSON")
    parser.add_argument("--status", action="store_true", help="Refresh and print cache status")
    args = parser.parse_args()
    if args.ingest:
        merge_snapshot_file(args.ingest)
    if args.queue:
        from scripts.symbol_resolver import resolve_symbol

        resolved = resolve_symbol(args.queue)
        queue_request(
            args.queue,
            _text(resolved.get("symbol") or args.queue).upper(),
            resolved.get("request"),
        )
    report = refresh_status()
    if args.status or args.queue or args.ingest:
        print(json.dumps(report, indent=2))
        print(f"Requests: {REQUESTS_PATH}")
        print(f"Snapshot: {SNAPSHOT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
