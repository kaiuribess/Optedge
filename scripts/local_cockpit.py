"""Free local Optedge cockpit server.

This is a lightweight browser UI for existing Optedge artifacts. It does not
place trades, does not store broker credentials, and does not require paid
dashboard services.
"""
from __future__ import annotations

import argparse
import binascii
import json
import math
import mimetypes
import struct
import sys
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pandas as pd

ROOT_BOOTSTRAP = Path(__file__).resolve().parent.parent
if str(ROOT_BOOTSTRAP) not in sys.path:
    sys.path.insert(0, str(ROOT_BOOTSTRAP))

from scripts.lookup_symbol import DATA_DIR, ROOT, lookup_symbol, render_html
from scripts.export_external_paper_track import build_external_orders, write_outputs
from scripts.research_jobs import (
    create_job, job_dashboard_path, list_jobs, read_job, read_job_log,
)
from scripts.symbol_resolver import COMMON_ALIASES, resolve_symbol


ARTIFACTS = {
    "latest-dashboard": ("dashboard_*.html", "text/html; charset=utf-8"),
    "validation-report": ("validation_report.html", "text/html; charset=utf-8"),
    "validation-summary": ("validation_summary.json", "application/json; charset=utf-8"),
    "factor-ic": ("factor_ic_summary.json", "application/json; charset=utf-8"),
    "position-aging": ("position_aging_summary.json", "application/json; charset=utf-8"),
    "equity-curve": ("equity_curve.png", "image/png"),
    "external-paper-orders": ("external_paper_orders.csv", "text/csv; charset=utf-8"),
}

OPPORTUNITY_SPECS = {
    "option": {
        "pattern": "top_options_*.parquet",
        "label": "Options",
        "symbol_col": "ticker",
        "columns": [
            "asset", "actionable", "ticker", "side", "strike", "expiry", "dte", "mid", "spot",
            "confidence", "rank_score", "fused_score", "trade_status",
            "suggested_contracts", "spread_pct", "ev_pct", "net_edge_pct",
            "stop_price", "target_price", "top_headline",
        ],
    },
    "share": {
        "pattern": "top_shares_*.parquet",
        "label": "Shares",
        "symbol_col": "ticker",
        "columns": [
            "asset", "actionable", "ticker", "spot", "confidence", "rank_score", "fused_score",
            "trade_status", "suggested_dollars", "ev_pct", "stop_price",
            "target_price", "top_headline",
        ],
    },
    "futures": {
        "pattern": "top_futures_*.parquet",
        "label": "Futures",
        "symbol_col": "symbol",
        "columns": [
            "asset", "actionable", "symbol", "name", "direction", "contract", "using_micro",
            "futures_score", "rank_score", "confidence", "trade_status",
            "suggested_contracts", "entry_price", "stop_price", "target_price",
            "risk_dollars", "reward_dollars", "ret_20d", "hv20", "range_pos",
            "top_headline",
        ],
    },
    "value": {
        "pattern": "top_value_*.parquet",
        "label": "Value",
        "symbol_col": "ticker",
        "columns": [
            "asset", "actionable", "ticker", "value_score", "value_bucket", "pe", "fcf_yield",
            "earnings_yield", "rev_growth", "op_margin", "insider_score",
            "n_buys", "n_sells", "top_headline",
        ],
    },
}

POSITION_FILES = {
    "option": "open_positions.json",
    "share": "open_share_positions.json",
    "futures": "open_futures_positions.json",
}

WATCHLIST_FILENAME = "cockpit_watchlist.json"


def _latest_file(data_dir: Path, pattern: str) -> Path | None:
    files = [p for p in data_dir.glob(pattern) if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: (p.stat().st_mtime, p.name))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _count_json_rows(path: Path) -> int:
    rows = _read_json(path)
    return len(rows) if isinstance(rows, list) else 0


def _direct_open_counts(data_dir: Path) -> dict[str, int]:
    return {
        "options": _count_json_rows(data_dir / "open_positions.json"),
        "shares": _count_json_rows(data_dir / "open_share_positions.json"),
        "futures": _count_json_rows(data_dir / "open_futures_positions.json"),
    }


def _read_parquet(path: Path | None) -> pd.DataFrame:
    if path is None:
        return pd.DataFrame()
    try:
        df = pd.read_parquet(path)
    except Exception:
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out["_source_file"] = path.name
    return out


def _file_meta(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    stat = path.stat()
    modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
    age_minutes = max(0.0, (datetime.now(timezone.utc) - modified).total_seconds() / 60.0)
    return {
        "name": path.name,
        "path": str(path),
        "size_bytes": int(stat.st_size),
        "modified_at": modified.isoformat(),
        "age_minutes": round(age_minutes, 1),
    }


def _png_validation_error(path: Path | None) -> str | None:
    """Return a short error if a PNG is missing/corrupt, otherwise None."""
    if path is None or not path.exists() or not path.is_file():
        return "missing"
    try:
        data = path.read_bytes()
    except Exception as exc:
        return f"could not read: {exc}"
    if len(data) < 12 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "not a PNG"
    offset = 8
    saw_iend = False
    while offset + 8 <= len(data):
        try:
            length = struct.unpack(">I", data[offset:offset + 4])[0]
        except Exception:
            return "invalid chunk length"
        chunk_type = data[offset + 4:offset + 8]
        offset += 8
        chunk_end = offset + length
        crc_end = chunk_end + 4
        if crc_end > len(data):
            return f"truncated {chunk_type.decode('ascii', errors='replace')} chunk"
        chunk = data[offset:chunk_end]
        expected = struct.unpack(">I", data[chunk_end:crc_end])[0]
        actual = binascii.crc32(chunk_type + chunk) & 0xFFFFFFFF
        if actual != expected:
            name = chunk_type.decode("ascii", errors="replace")
            return f"{name} CRC mismatch"
        offset = crc_end
        if chunk_type == b"IEND":
            saw_iend = True
            break
    if not saw_iend:
        return "missing IEND chunk"
    return None


def _clean_value(value: Any) -> Any:
    try:
        if value is None or pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except Exception:
        return default
    return out if math.isfinite(out) else default


def _position_identity(row: dict[str, Any]) -> tuple:
    pid = row.get("position_id")
    if pid:
        return ("id", str(pid))
    return (
        str(row.get("asset") or ""),
        str(row.get("ticker") or row.get("symbol") or row.get("ticker_or_symbol") or ""),
        str(row.get("side") or row.get("direction") or row.get("side_or_direction") or ""),
        str(row.get("strike") or row.get("contract") or row.get("strike_or_contract") or ""),
        str(row.get("expiry") or ""),
        str(row.get("entry_time") or ""),
        str(row.get("entry_price") or ""),
    )


def _dedupe_position_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = _position_identity(row)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _watchlist_file(data_dir: Path = DATA_DIR) -> Path:
    return Path(data_dir) / WATCHLIST_FILENAME


def _safe_id(value: Any) -> str:
    text = str(value or "").strip()
    safe = "".join(ch if ch.isalnum() or ch in {"_", "-", "=", "."} else "_" for ch in text)
    return safe[:96] or "item"


def _watchlist_entry_id(resolution: dict[str, Any], query: str) -> str:
    symbol = str(resolution.get("symbol") or query).upper()
    request = resolution.get("request") or {}
    if request:
        raw = (
            f"{symbol}_{request.get('side','')}_{request.get('expiry','')}_"
            f"{request.get('strike','')}"
        )
    else:
        raw = symbol
    return _safe_id(raw)


def _watchlist_lookup_query(entry: dict[str, Any]) -> str:
    if entry.get("request"):
        return str(entry.get("query") or entry.get("symbol") or "").strip()
    return str(entry.get("symbol") or entry.get("query") or "").strip()


def _enrich_watchlist_entry(entry: dict[str, Any], data_dir: Path) -> dict[str, Any]:
    out = dict(entry)
    query = _watchlist_lookup_query(entry)
    if not query:
        return out
    try:
        report = lookup_symbol(query, data_dir)
        brief = report.get("brief") or {}
        best = brief.get("best_idea") or {}
        open_pos = brief.get("open_positions") or {}
        out.update({
            "local_hits": _clean_value(report.get("total_hits")),
            "best_idea": _clean_value(best.get("label")),
            "best_status": _clean_value(best.get("trade_status")),
            "best_confidence": _clean_value(best.get("confidence")),
            "best_score": _clean_value(best.get("score")),
            "open_count": _clean_value(open_pos.get("count")),
            "avg_unrealized_pct": _clean_value(open_pos.get("avg_unrealized_pct")),
            "max_exit_pressure": _clean_value(open_pos.get("max_exit_pressure")),
            "warning_count": len(brief.get("risk_warnings") or []),
            "last_enriched_at": _now_iso(),
        })
    except Exception as exc:
        out["enrichment_error"] = str(exc)[:180]
    return out


def load_watchlist(data_dir: Path = DATA_DIR, enrich: bool = False) -> dict[str, Any]:
    rows = _read_json(_watchlist_file(data_dir))
    if not isinstance(rows, list):
        rows = []
    cleaned: list[dict[str, Any]] = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id = str(row.get("id") or "").strip()
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        cleaned.append(row)
    entries = [_enrich_watchlist_entry(row, Path(data_dir)) for row in cleaned] if enrich else cleaned
    return {
        "generated_at": _now_iso(),
        "count": len(entries),
        "enriched": enrich,
        "entries": entries,
        "path": str(_watchlist_file(data_dir)),
        "notes": [
            "Watchlist entries are local research targets only.",
            "Enriched watchlists read the latest local scan snapshots and open positions.",
            "Run all launches focused scans for resolved symbols; no trades are placed.",
        ],
    }


def _save_watchlist(entries: list[dict[str, Any]], data_dir: Path = DATA_DIR) -> None:
    path = _watchlist_file(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2, default=str), encoding="utf-8")


def add_watchlist_query(query: str, data_dir: Path = DATA_DIR) -> dict[str, Any]:
    clean = str(query or "").strip()
    if not clean:
        return {"ok": False, "error": "query is required"}
    resolution = resolve_symbol(clean)
    if not resolution.get("symbol"):
        return {
            "ok": False,
            "error": resolution.get("error") or "could not resolve symbol",
            "resolution": resolution,
        }
    current = load_watchlist(data_dir)["entries"]
    item_id = _watchlist_entry_id(resolution, clean)
    now = _now_iso()
    entry = {
        "id": item_id,
        "query": clean,
        "symbol": str(resolution.get("symbol") or "").upper(),
        "name": resolution.get("name"),
        "source": resolution.get("source"),
        "request": resolution.get("request"),
        "resolution": resolution,
        "added_at": now,
        "updated_at": now,
    }
    replaced = False
    for idx, row in enumerate(current):
        if row.get("id") == item_id:
            entry["added_at"] = row.get("added_at") or now
            current[idx] = entry
            replaced = True
            break
    if not replaced:
        current.append(entry)
    _save_watchlist(current, data_dir)
    return {"ok": True, "entry": entry, "updated_existing": replaced, **load_watchlist(data_dir)}


def remove_watchlist_entry(entry_id: str, data_dir: Path = DATA_DIR) -> dict[str, Any]:
    clean_id = str(entry_id or "").strip()
    current = load_watchlist(data_dir)["entries"]
    remaining = [row for row in current if str(row.get("id")) != clean_id]
    removed = len(remaining) != len(current)
    _save_watchlist(remaining, data_dir)
    return {"ok": removed, "removed": removed, **load_watchlist(data_dir)}


def _scan_args_from_controls(mode: str = "full", bankroll: Any = None,
                             aggressive: bool = False) -> list[str]:
    scan_args = ["--minimal"] if str(mode or "full").strip().lower() == "quick" else []
    if aggressive:
        scan_args.append("--aggressive")
    try:
        bankroll_float = float(bankroll or 0)
    except Exception:
        bankroll_float = 0.0
    if bankroll_float > 0:
        scan_args.extend(["--bankroll", str(bankroll_float)])
    return scan_args


def run_watchlist_scans(
    data_dir: Path = DATA_DIR,
    mode: str = "full",
    bankroll: Any = None,
    aggressive: bool = False,
    launch: bool = True,
) -> dict[str, Any]:
    entries = load_watchlist(data_dir)["entries"]
    scan_args = _scan_args_from_controls(mode, bankroll, aggressive)
    jobs = []
    for entry in entries[:25]:
        query = str(entry.get("query") or entry.get("symbol") or "").strip()
        if not query:
            continue
        jobs.append(create_job(
            query,
            data_dir,
            launch=launch,
            extra_scan_args=scan_args,
            scan_mode=str(mode or "full"),
        ))
    return {
        "ok": True,
        "count": len(jobs),
        "jobs": jobs,
        "scan_args": scan_args,
        "launched": launch,
    }


def _position_label(row: dict[str, Any]) -> str:
    symbol = str(row.get("ticker") or row.get("symbol") or "-").upper()
    side = str(row.get("side") or row.get("direction") or "").upper()
    strike = row.get("strike")
    expiry = str(row.get("expiry") or "")
    contract = str(row.get("contract") or "")
    if strike not in (None, "", "-") and expiry:
        try:
            strike_txt = f"{float(strike):g}"
        except Exception:
            strike_txt = str(strike)
        return f"{symbol} {side[:1]} {strike_txt} {expiry[-5:]}"
    if contract:
        return f"{symbol} {side} {contract}".strip()
    return f"{symbol} {side}".strip()


def _position_age_days(row: dict[str, Any]) -> float:
    if row.get("age_days") is not None:
        return _float_value(row.get("age_days"))
    entry = row.get("entry_time")
    if not entry:
        return 0.0
    try:
        ts = pd.to_datetime(entry, errors="coerce", utc=True)
        if pd.isna(ts):
            return 0.0
        return max(0.0, float((pd.Timestamp.now(tz="UTC") - ts).total_seconds() / 86400.0))
    except Exception:
        return 0.0


def _position_pnl_pct(row: dict[str, Any]) -> float:
    for col in ("unrealized_pct", "current_pnl_pct", "pnl_pct"):
        if row.get(col) is not None:
            return _float_value(row.get(col))
    current = row.get("current_mid", row.get("current_price"))
    if current is None:
        current = row.get("last_price")
    entry = _float_value(row.get("entry_price"))
    cur = _float_value(current)
    return (cur - entry) / entry if entry > 0 and cur > 0 else 0.0


def _normalize_position(row: dict[str, Any], asset: str) -> dict[str, Any]:
    out = dict(row)
    out.setdefault("asset", asset)
    symbol = out.get("ticker") or out.get("symbol") or "-"
    current_price = out.get("current_mid", out.get("current_price", out.get("last_price")))
    pnl_pct = _position_pnl_pct(out)
    exit_pressure = _float_value(out.get("latest_exit_pressure"), default=0.0)
    reprice_failed = _float_value(out.get("reprice_failed_count"), default=0.0)
    attention = exit_pressure >= 60 or reprice_failed >= 2 or pnl_pct <= -0.30
    return {
        "asset": out.get("asset"),
        "position_label": _position_label(out),
        "ticker_or_symbol": str(symbol).upper(),
        "side_or_direction": out.get("side") or out.get("direction") or out.get("asset"),
        "strike_or_contract": out.get("strike", out.get("contract")),
        "expiry": out.get("expiry"),
        "trade_status": out.get("trade_status") or "Open",
        "entry_time": out.get("entry_time"),
        "age_days": round(_position_age_days(out), 2),
        "entry_price": _clean_value(out.get("entry_price")),
        "current_price": _clean_value(current_price),
        "pnl_pct": pnl_pct,
        "pnl_dollars": _clean_value(out.get("pnl_dollars")),
        "confidence": _clean_value(out.get("confidence")),
        "latest_exit_pressure": _clean_value(out.get("latest_exit_pressure")),
        "latest_exit_action": out.get("latest_exit_action"),
        "stop_price": _clean_value(out.get("stop_price")),
        "target_price": _clean_value(out.get("target_price")),
        "reprice_failed_count": _clean_value(out.get("reprice_failed_count")),
        "research_guard_status": out.get("research_guard_status"),
        "attention": attention,
    }


def _position_sort_key(row: dict[str, Any]) -> tuple:
    return (
        1 if row.get("attention") else 0,
        _float_value(row.get("latest_exit_pressure")),
        abs(_float_value(row.get("pnl_pct"))),
        _float_value(row.get("age_days")),
    )


def _opportunity_score(row: pd.Series) -> float:
    for col in ("rank_score", "fused_score", "futures_score", "value_score"):
        if col in row:
            score = _float_value(row.get(col), default=math.nan)
            if math.isfinite(score):
                return score
    return _float_value(row.get("confidence"), default=0.0) / 100.0


def _is_actionable(row: pd.Series) -> bool:
    status = str(row.get("trade_status") or "").strip().lower()
    if status in {"watch", "skip", "blocked"}:
        return False
    asset = str(row.get("asset") or "").lower()
    if asset == "option":
        return _float_value(row.get("suggested_contracts")) > 0
    if asset == "futures":
        return _float_value(row.get("suggested_contracts")) > 0
    if asset == "share":
        return _float_value(row.get("suggested_dollars")) > 0
    return True


def _opportunity_records(df: pd.DataFrame, asset: str, limit: int) -> list[dict[str, Any]]:
    if df.empty:
        return []
    spec = OPPORTUNITY_SPECS[asset]
    cols = [c for c in spec["columns"] if c in df.columns]
    if "asset" not in cols:
        cols.insert(0, "asset")
    records: list[dict[str, Any]] = []
    for _, row in df[cols].head(limit).iterrows():
        records.append({str(k): _clean_value(v) for k, v in row.to_dict().items()})
    return records


def _suggestion_text(row: dict[str, Any]) -> str:
    return " ".join(str(row.get(key) or "") for key in ("symbol", "label", "name", "query", "source")).lower()


def _add_suggestion(
    rows: list[dict[str, Any]],
    seen: set[tuple[str, str, str]],
    symbol: Any,
    label: str,
    kind: str,
    source: str,
    query: str | None = None,
    name: Any = None,
    score: Any = None,
    trade_status: Any = None,
) -> None:
    clean_symbol = str(symbol or "").strip().upper()
    clean_label = str(label or clean_symbol).strip()
    clean_query = str(query or clean_symbol).strip()
    if not clean_symbol or not clean_query:
        return
    key = (kind, clean_symbol, clean_query.upper())
    if key in seen:
        return
    seen.add(key)
    rows.append({
        "symbol": clean_symbol,
        "label": clean_label,
        "kind": kind,
        "source": source,
        "query": clean_query,
        "name": _clean_value(name),
        "score": _clean_value(score),
        "trade_status": _clean_value(trade_status),
    })


def _option_query_from_row(row: pd.Series) -> str:
    ticker = str(row.get("ticker") or "").strip().upper()
    expiry = str(row.get("expiry") or "").strip()
    side_raw = str(row.get("side") or "").strip().upper()
    side = "C" if side_raw.startswith("C") else "P" if side_raw.startswith("P") else side_raw[:1]
    strike = row.get("strike")
    if not ticker or not expiry or not side or strike in (None, ""):
        return ticker
    try:
        strike_text = f"{float(strike):g}"
    except Exception:
        strike_text = str(strike)
    return f"{ticker} {expiry} {side} {strike_text}"


def build_symbol_suggestions(
    data_dir: Path = DATA_DIR,
    query: str = "",
    limit: int = 16,
) -> dict[str, Any]:
    """Suggest local tickers/contracts from current Optedge artifacts."""
    query_norm = str(query or "").strip().lower()
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for alias, (symbol, name) in sorted(COMMON_ALIASES.items()):
        _add_suggestion(
            rows, seen, symbol, f"{symbol} - {name}", "alias", "built-in aliases",
            query=symbol, name=name, score=0.25,
        )
        if alias != symbol.lower():
            _add_suggestion(
                rows, seen, symbol, f"{alias.title()} -> {symbol}", "alias",
                "built-in aliases", query=alias, name=name, score=0.2,
            )

    for asset_name, spec in OPPORTUNITY_SPECS.items():
        df = _read_parquet(_latest_file(data_dir, spec["pattern"]))
        if df.empty:
            continue
        symbol_col = str(spec["symbol_col"])
        if symbol_col not in df.columns:
            continue
        for _, row in df.head(600).iterrows():
            symbol = row.get(symbol_col)
            status = row.get("trade_status")
            score = _opportunity_score(row)
            if asset_name == "option":
                option_query = _option_query_from_row(row)
                side = str(row.get("side") or "").upper()[:1]
                label = f"{symbol} {side} {row.get('strike', '-')} {row.get('expiry', '-')}"
                _add_suggestion(
                    rows, seen, symbol, label, "option", "latest options",
                    query=option_query, score=score, trade_status=status,
                )
            elif asset_name == "futures":
                name = row.get("name")
                direction = str(row.get("direction") or "").upper()
                contract = str(row.get("contract") or "")
                label = f"{symbol} {direction} {contract}".strip()
                if name:
                    label = f"{label} - {name}"
                _add_suggestion(
                    rows, seen, symbol, label, "futures", "latest futures",
                    query=str(symbol or ""), name=name, score=score, trade_status=status,
                )
            else:
                label = f"{symbol} {asset_name}"
                _add_suggestion(
                    rows, seen, symbol, label, asset_name, f"latest {asset_name}",
                    query=str(symbol or ""), score=score, trade_status=status,
                )

    for asset_name, filename in POSITION_FILES.items():
        raw = _read_json(data_dir / filename)
        if not isinstance(raw, list):
            continue
        for item in raw[:800]:
            if not isinstance(item, dict):
                continue
            normalized = _normalize_position(item, asset_name)
            symbol = normalized.get("ticker_or_symbol")
            label = normalized.get("position_label") or str(symbol or "")
            _add_suggestion(
                rows, seen, symbol, label, f"open_{asset_name}",
                "open positions", query=str(symbol or ""), score=0.5,
                trade_status=normalized.get("trade_status"),
            )

    for item in load_watchlist(data_dir).get("entries", []):
        _add_suggestion(
            rows, seen, item.get("symbol"), str(item.get("query") or item.get("symbol") or ""),
            "watchlist", "research watchlist", query=str(item.get("query") or item.get("symbol") or ""),
            name=item.get("name"), score=0.75,
        )

    if query_norm:
        rows = [row for row in rows if query_norm in _suggestion_text(row)]

    def sort_key(row: dict[str, Any]) -> tuple[int, int, float, str]:
        text = _suggestion_text(row)
        symbol = str(row.get("symbol") or "").lower()
        prefix = 1 if query_norm and (symbol.startswith(query_norm) or text.startswith(query_norm)) else 0
        exact = 1 if query_norm and (symbol == query_norm or str(row.get("query") or "").lower() == query_norm) else 0
        return (exact, prefix, _float_value(row.get("score")), str(row.get("symbol") or ""))

    rows = sorted(rows, key=sort_key, reverse=True)[:limit]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "query": query,
        "count": len(rows),
        "rows": rows,
        "notes": [
            "Suggestions are built from local scan snapshots, open positions, watchlist entries, and built-in aliases.",
            "Selecting a suggestion only fills or runs local research; it does not place trades.",
        ],
    }


def _records_from_frame(df: pd.DataFrame, limit: int = 100) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    records: list[dict[str, Any]] = []
    for _, row in df.head(limit).iterrows():
        records.append({str(k): _clean_value(v) for k, v in row.to_dict().items()})
    return records


def build_paper_candidates(
    data_dir: Path = DATA_DIR,
    max_new: int = 5,
    max_open: int = 30,
    include_watch: bool = False,
    allow_zero_size_placeholder: bool = False,
    asset: str = "all",
    dry_run: bool = False,
    write: bool = False,
) -> dict[str, Any]:
    """Build or write the compact external paper tracking candidate list."""
    df = build_external_orders(
        data_dir=data_dir,
        max_new=max_new,
        max_open=max_open,
        include_watch=include_watch,
        allow_zero_size_placeholder=allow_zero_size_placeholder,
        asset=asset,
        dry_run=dry_run,
    )
    paths: dict[str, str] = {}
    if write and not dry_run:
        csv_path, json_path = write_outputs(df, data_dir)
        paths = {"csv": str(csv_path), "json": str(json_path)}
    selected_count = 0
    excluded_count = 0
    if not df.empty and "reason_excluded" in df.columns:
        excluded_mask = df["reason_excluded"].astype(str).str.len() > 0
        excluded_count = int(excluded_mask.sum())
        selected_count = int((~excluded_mask).sum())
    else:
        selected_count = int(len(df))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "asset": asset,
        "max_new": max_new,
        "max_open": max_open,
        "include_watch": include_watch,
        "allow_zero_size_placeholder": allow_zero_size_placeholder,
        "dry_run": dry_run,
        "wrote_files": bool(paths),
        "paths": paths,
        "count": int(len(df)),
        "selected_count": selected_count,
        "excluded_count": excluded_count,
        "rows": _records_from_frame(df, limit=150),
        "notes": [
            "External paper candidates are a small filtered subset, not every internal signal.",
            "This creates manual paper-tracking files only; no trades are placed.",
            "Dry-run review includes rejected rows and exclusion reasons.",
        ],
    }


def build_opportunities(
    data_dir: Path = DATA_DIR,
    asset: str = "all",
    query: str = "",
    status: str = "all",
    min_confidence: float = 0.0,
    limit: int = 80,
) -> dict[str, Any]:
    selected = list(OPPORTUNITY_SPECS) if asset == "all" else [asset]
    query_norm = str(query or "").strip().upper()
    status_norm = str(status or "all").strip().lower()
    rows: list[pd.DataFrame] = []
    sources: dict[str, str | None] = {}

    for asset_name in selected:
        spec = OPPORTUNITY_SPECS.get(asset_name)
        if spec is None:
            continue
        path = _latest_file(data_dir, spec["pattern"])
        sources[asset_name] = path.name if path else None
        df = _read_parquet(path)
        if df.empty:
            continue
        out = df.copy()
        out["asset"] = asset_name
        out["actionable"] = out.apply(_is_actionable, axis=1)
        out["_opportunity_score"] = out.apply(_opportunity_score, axis=1)
        if "confidence" in out.columns:
            out = out[pd.to_numeric(out["confidence"], errors="coerce").fillna(0.0) >= min_confidence]
        elif min_confidence > 0:
            out = out.iloc[0:0]
        if query_norm:
            symbol_col = str(spec["symbol_col"])
            symbol_match = (
                out[symbol_col].astype(str).str.upper().str.contains(query_norm, na=False, regex=False)
                if symbol_col in out.columns else pd.Series(False, index=out.index)
            )
            headline_match = (
                out["top_headline"].astype(str).str.upper().str.contains(query_norm, na=False, regex=False)
                if "top_headline" in out.columns else pd.Series(False, index=out.index)
            )
            out = out[symbol_match | headline_match]
        if status_norm == "actionable":
            out = out[out["actionable"]]
        elif status_norm != "all" and "trade_status" in out.columns:
            out = out[out["trade_status"].astype(str).str.lower() == status_norm]
        rows.append(out)

    if rows:
        combined = pd.concat(rows, ignore_index=True, sort=False)
        combined = combined.sort_values("_opportunity_score", ascending=False, kind="mergesort")
    else:
        combined = pd.DataFrame()

    records = []
    for asset_name in OPPORTUNITY_SPECS:
        part = combined[combined["asset"] == asset_name] if "asset" in combined.columns else pd.DataFrame()
        records.extend(_opportunity_records(part, asset_name, limit))
    records = sorted(records, key=lambda r: _float_value(r.get("rank_score") or r.get("fused_score") or r.get("futures_score") or r.get("value_score")), reverse=True)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "asset": asset,
        "query": query,
        "status": status,
        "min_confidence": min_confidence,
        "count": len(records[:limit]),
        "sources": sources,
        "rows": records[:limit],
        "notes": [
            "Explorer reads the latest local top_* parquet snapshots.",
            "Actionable excludes Watch/Skip where sizing fields are present.",
            "This is research output only; no orders are placed.",
        ],
    }


def build_positions(
    data_dir: Path = DATA_DIR,
    asset: str = "all",
    query: str = "",
    status: str = "all",
    limit: int = 250,
) -> dict[str, Any]:
    selected = list(POSITION_FILES) if asset == "all" else [asset]
    query_norm = str(query or "").strip().upper()
    status_norm = str(status or "all").strip().lower()
    rows: list[dict[str, Any]] = []
    sources: dict[str, str | None] = {}

    for asset_name in selected:
        filename = POSITION_FILES.get(asset_name)
        if not filename:
            continue
        path = data_dir / filename
        raw = _read_json(path)
        sources[asset_name] = filename if path.exists() else None
        if not isinstance(raw, list):
            continue
        for item in raw:
            if isinstance(item, dict):
                rows.append(_normalize_position(item, asset_name))

    rows = _dedupe_position_rows(rows)
    if query_norm:
        rows = [
            row for row in rows
            if query_norm in str(row.get("ticker_or_symbol") or "").upper()
            or query_norm in str(row.get("position_label") or "").upper()
        ]
    if status_norm == "attention":
        rows = [row for row in rows if row.get("attention")]
    elif status_norm != "all":
        rows = [
            row for row in rows
            if str(row.get("trade_status") or "").strip().lower() == status_norm
            or str(row.get("latest_exit_action") or "").strip().lower() == status_norm
        ]
    rows = sorted(rows, key=_position_sort_key, reverse=True)
    limited = rows[:limit]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "asset": asset,
        "query": query,
        "status": status,
        "count": len(limited),
        "total_before_limit": len(rows),
        "sources": sources,
        "rows": limited,
        "notes": [
            "Position monitor reads current open position state only.",
            "Attention means high exit pressure, repeated repricing trouble, or a large unrealized drawdown.",
            "This is research output only; no orders are placed.",
        ],
    }


def artifact_path(name: str, data_dir: Path = DATA_DIR) -> Path | None:
    spec = ARTIFACTS.get(name)
    if spec is None:
        return None
    pattern, _ = spec
    if "*" in pattern:
        return _latest_file(data_dir, pattern)
    path = data_dir / pattern
    return path if path.exists() and path.is_file() else None


def _int_param(value: str | None, default: int, low: int, high: int) -> int:
    try:
        out = int(float(value or default))
    except Exception:
        return default
    return max(low, min(high, out))


def _float_param(value: str | None, default: float, low: float, high: float) -> float:
    try:
        out = float(value or default)
    except Exception:
        return default
    if not math.isfinite(out):
        return default
    return max(low, min(high, out))


def _bool_param(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "on"}


def _health_check(level: str, label: str, detail: str) -> dict[str, str]:
    return {"level": level, "label": label, "detail": detail}


def _health_status(checks: list[dict[str, str]]) -> str:
    order = {"ok": 0, "warn": 1, "bad": 2}
    worst = max((order.get(row.get("level", "ok"), 0) for row in checks), default=0)
    return {0: "ok", 1: "warn", 2: "bad"}[worst]


def _count_duplicate_open_positions(data_dir: Path) -> tuple[int, int]:
    raw_count = 0
    deduped_count = 0
    for filename in POSITION_FILES.values():
        rows = _read_json(data_dir / filename)
        if not isinstance(rows, list):
            continue
        dict_rows = [row for row in rows if isinstance(row, dict)]
        raw_count += len(dict_rows)
        deduped_count += len(_dedupe_position_rows(dict_rows))
    return raw_count, deduped_count


def build_data_health(data_dir: Path = DATA_DIR) -> dict[str, Any]:
    """Check whether the dashboard-facing artifacts agree with current state."""
    open_counts = _direct_open_counts(data_dir)
    total_open = sum(open_counts.values())
    validation = _read_json(data_dir / "validation_summary.json")
    aging = _read_json(data_dir / "position_aging_summary.json")
    checks: list[dict[str, str]] = []

    if isinstance(validation, dict):
        reported_open = int(_float_value(validation.get("open_positions")))
        if reported_open == total_open:
            checks.append(_health_check(
                "ok", "Validation open count",
                f"Validation and current open files both show {total_open} open position(s).",
            ))
        else:
            checks.append(_health_check(
                "bad", "Validation open count mismatch",
                f"Validation shows {reported_open}, but current open files show {total_open}.",
            ))
        asset_map = {"option": "options", "share": "shares", "futures": "futures"}
        assets = validation.get("assets") if isinstance(validation.get("assets"), dict) else {}
        for asset_name, count_key in asset_map.items():
            reported = assets.get(asset_name, {}) if isinstance(assets.get(asset_name), dict) else {}
            reported_count = int(_float_value(reported.get("open_positions")))
            direct_count = open_counts[count_key]
            if reported_count != direct_count:
                checks.append(_health_check(
                    "warn", f"{asset_name} open count mismatch",
                    f"Validation shows {reported_count}; current {count_key} state shows {direct_count}.",
                ))
    else:
        checks.append(_health_check(
            "warn", "Validation summary missing",
            "Run python run.py --validation-report to refresh validation_summary.json.",
        ))

    if isinstance(aging, dict):
        aging_open = int(_float_value(aging.get("open_count")))
        level = "ok" if aging_open == total_open else "warn"
        detail = (
            f"Position aging and current open files both show {total_open} open position(s)."
            if aging_open == total_open
            else f"Position aging shows {aging_open}, but current open files show {total_open}."
        )
        checks.append(_health_check(level, "Position aging count", detail))
    else:
        checks.append(_health_check(
            "warn", "Position aging missing",
            "position_aging_summary.json was not found or could not be read.",
        ))

    raw_open, deduped_open = _count_duplicate_open_positions(data_dir)
    duplicate_count = max(0, raw_open - deduped_open)
    if duplicate_count:
        checks.append(_health_check(
            "warn", "Duplicate open positions",
            f"{duplicate_count} duplicate open position row(s) were detected across lifecycle files.",
        ))
    else:
        checks.append(_health_check(
            "ok", "Duplicate open positions",
            f"No duplicate open rows detected across {raw_open} current position row(s).",
        ))

    equity_curve = artifact_path("equity-curve", data_dir)
    png_error = _png_validation_error(equity_curve)
    if png_error is None:
        checks.append(_health_check("ok", "Equity curve image", "equity_curve.png passed PNG integrity checks."))
    elif png_error == "missing":
        checks.append(_health_check("warn", "Equity curve image missing", "No equity curve image is available yet."))
    else:
        checks.append(_health_check("bad", "Equity curve image corrupt", f"equity_curve.png appears invalid: {png_error}."))

    latest_dashboard = artifact_path("latest-dashboard", data_dir)
    validation_path = artifact_path("validation-summary", data_dir)
    dashboard_meta = _file_meta(latest_dashboard)
    validation_meta = _file_meta(validation_path)
    if dashboard_meta:
        if _float_value(dashboard_meta.get("age_minutes")) > 24 * 60:
            checks.append(_health_check(
                "warn", "Dashboard is old",
                f"Latest dashboard is {dashboard_meta['age_minutes']} minutes old.",
            ))
        else:
            checks.append(_health_check("ok", "Dashboard freshness", f"Latest dashboard: {dashboard_meta['name']}."))
    else:
        checks.append(_health_check("warn", "Dashboard missing", "No dashboard_*.html file was found."))

    if dashboard_meta and validation_meta:
        dash_mtime = Path(dashboard_meta["path"]).stat().st_mtime
        val_mtime = Path(validation_meta["path"]).stat().st_mtime
        if dash_mtime - val_mtime > 3600:
            checks.append(_health_check(
                "warn", "Validation older than dashboard",
                "validation_summary.json is more than 60 minutes older than the latest dashboard.",
            ))

    snapshot_meta: dict[str, Any] = {}
    for asset_name, pattern in {
        "options": "top_options_*.parquet",
        "shares": "top_shares_*.parquet",
        "futures": "top_futures_*.parquet",
        "value": "top_value_*.parquet",
    }.items():
        meta = _file_meta(_latest_file(data_dir, pattern))
        snapshot_meta[asset_name] = meta
        if meta is None:
            checks.append(_health_check("warn", f"{asset_name} snapshot missing", f"No {pattern} file was found."))
        elif _float_value(meta.get("age_minutes")) > 24 * 60:
            checks.append(_health_check("warn", f"{asset_name} snapshot old", f"{meta['name']} is more than 24 hours old."))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": _health_status(checks),
        "open_counts": open_counts,
        "total_open": total_open,
        "duplicate_open_rows": duplicate_count,
        "checks": checks,
        "artifacts": {
            "dashboard": dashboard_meta,
            "validation_summary": validation_meta,
            "validation_report": _file_meta(artifact_path("validation-report", data_dir)),
            "equity_curve": _file_meta(equity_curve),
        },
        "snapshots": snapshot_meta,
        "notes": [
            "Data health reads the same local files used by the cockpit.",
            "Open-position counts come directly from current open position JSON files.",
            "Warnings mean review the artifacts before trusting the displayed analytics.",
        ],
    }


def build_summary(data_dir: Path = DATA_DIR) -> dict[str, Any]:
    validation = _read_json(data_dir / "validation_summary.json")
    aging = _read_json(data_dir / "position_aging_summary.json")
    open_counts = _direct_open_counts(data_dir)
    data_health = build_data_health(data_dir)
    latest = {
        "dashboard": artifact_path("latest-dashboard", data_dir),
        "validation_report": artifact_path("validation-report", data_dir),
        "external_paper_orders": artifact_path("external-paper-orders", data_dir),
        "equity_curve": artifact_path("equity-curve", data_dir),
    }
    snapshots = {
        "options": _latest_file(data_dir, "top_options_*.parquet"),
        "shares": _latest_file(data_dir, "top_shares_*.parquet"),
        "value": _latest_file(data_dir, "top_value_*.parquet"),
        "futures": _latest_file(data_dir, "top_futures_*.parquet"),
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_dir": str(data_dir),
        "open_counts": open_counts,
        "total_open": sum(open_counts.values()),
        "validation": validation if isinstance(validation, dict) else {},
        "position_aging": aging if isinstance(aging, dict) else {},
        "data_health": data_health,
        "latest_artifacts": {k: (str(v) if v else None) for k, v in latest.items()},
        "snapshots": {k: (v.name if v else None) for k, v in snapshots.items()},
        "notes": [
            "This cockpit reads local Optedge artifacts only.",
            "Search uses the latest scan snapshots and open position files.",
            "No trades are placed from this UI.",
        ],
    }


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, indent=2, default=str).encode("utf-8")


def render_cockpit_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Optedge Local Cockpit</title>
<style>
:root { color-scheme: dark; --bg:#080b10; --panel:#0f172a; --panel2:#111827; --border:#223044; --text:#e5e7eb; --muted:#94a3b8; --accent:#38bdf8; --good:#10b981; --warn:#f59e0b; --bad:#ef4444; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font-family:Inter,Segoe UI,Arial,sans-serif; }
.wrap { max-width:1280px; margin:0 auto; padding:24px 16px 72px; }
header { display:flex; justify-content:space-between; gap:16px; align-items:flex-end; border-bottom:1px solid var(--border); padding-bottom:16px; }
h1 { margin:0; font-size:28px; font-weight:650; }
.muted { color:var(--muted); }
.grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:18px 0; }
.tile, .panel { border:1px solid var(--border); background:var(--panel); border-radius:8px; padding:14px; }
.tile span { display:block; color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.5px; }
.tile strong { display:block; font-size:26px; margin-top:6px; }
.actions { display:flex; flex-wrap:wrap; gap:8px; margin:16px 0; }
a, button { color:var(--text); }
.btn { display:inline-flex; align-items:center; gap:8px; border:1px solid var(--border); background:var(--panel2); border-radius:999px; padding:8px 12px; text-decoration:none; font-size:13px; cursor:pointer; }
.btn:hover { border-color:var(--accent); }
.search { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:10px; margin-top:10px; }
.scan-controls { display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; align-items:center; }
input, select { background:var(--panel2); color:var(--text); border:1px solid var(--border); border-radius:8px; padding:12px 14px; font-size:15px; }
input { width:100%; }
input:focus, select:focus { outline:none; border-color:var(--accent); }
.check { display:inline-flex; align-items:center; gap:6px; color:var(--muted); font-size:13px; }
.check input { width:auto; }
.search-actions { display:flex; gap:8px; flex-wrap:wrap; }
.status { margin-top:8px; font-size:12px; color:var(--muted); min-height:18px; }
.sections { display:grid; grid-template-columns:1fr; gap:12px; margin-top:14px; }
.section { border:1px solid var(--border); border-radius:8px; background:#0b1220; overflow:hidden; }
.section h3 { margin:0; padding:12px 14px; font-size:14px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; }
.brief-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:8px; }
.brief-tile { border:1px solid var(--border); background:#0b1220; border-radius:8px; padding:10px; }
.brief-tile span { display:block; color:var(--muted); font-size:10px; text-transform:uppercase; letter-spacing:.4px; }
.brief-tile strong { display:block; margin-top:5px; font-size:14px; }
.brief-cols { display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:10px; margin-top:10px; }
.brief-list { border:1px solid var(--border); background:#0b1220; border-radius:8px; padding:10px; }
.brief-list h4 { margin:0 0 8px; font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.4px; }
.brief-list ul { margin:0; padding-left:18px; color:#cbd5e1; font-size:12px; }
.table-wrap { overflow:auto; }
table { width:100%; border-collapse:collapse; font-size:12px; }
th, td { padding:8px 10px; border-bottom:1px solid #1d2938; text-align:left; vertical-align:top; }
th { color:var(--muted); text-transform:uppercase; font-size:10px; letter-spacing:.4px; }
tr.clickable-row { cursor:pointer; }
tr.clickable-row:hover { background:#111c31; }
.empty { padding:14px; color:var(--muted); font-style:italic; }
.risk { border-left:4px solid var(--warn); }
.job-list { display:grid; gap:8px; margin-top:10px; }
.job { display:flex; justify-content:space-between; gap:10px; align-items:center; border:1px solid var(--border); background:#0b1220; border-radius:8px; padding:10px 12px; font-size:13px; }
.job code { color:var(--accent); }
.job small { color:var(--muted); display:block; margin-top:3px; }
.logbox { display:none; white-space:pre-wrap; overflow:auto; max-height:280px; border:1px solid var(--border); background:#050812; border-radius:8px; padding:12px; margin-top:10px; font:12px/1.45 Consolas,monospace; color:#cbd5e1; }
.logbox.active { display:block; }
.suggestions { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; min-height:30px; }
.suggestion { border:1px solid var(--border); background:#0b1220; border-radius:999px; padding:7px 10px; cursor:pointer; font-size:12px; color:var(--text); }
.suggestion:hover { border-color:var(--accent); }
.suggestion span { color:var(--muted); margin-left:6px; }
.good { color:var(--good); } .warn { color:var(--warn); } .bad { color:var(--bad); }
@media (max-width:900px) { header { align-items:flex-start; flex-direction:column; } .grid { grid-template-columns:repeat(2,minmax(0,1fr)); } .search { grid-template-columns:1fr; } }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Optedge Local Cockpit</h1>
      <div class="muted">Interactive local research view. No broker execution.</div>
    </div>
    <div class="muted" id="asof">Loading...</div>
  </header>
  <div class="grid">
    <div class="tile"><span>Open options</span><strong id="open-options">-</strong></div>
    <div class="tile"><span>Open shares</span><strong id="open-shares">-</strong></div>
    <div class="tile"><span>Open futures</span><strong id="open-futures">-</strong></div>
    <div class="tile risk"><span>Total open</span><strong id="total-open">-</strong></div>
    <div class="tile"><span>Data health</span><strong id="data-health">-</strong></div>
  </div>
  <div class="actions">
    <a class="btn" href="/artifact/latest-dashboard" target="_blank">Latest dashboard</a>
    <a class="btn" href="/artifact/validation-report" target="_blank">Validation report</a>
    <a class="btn" href="/artifact/validation-summary" target="_blank">Validation JSON</a>
    <a class="btn" href="/artifact/equity-curve" target="_blank">Equity curve</a>
    <a class="btn" href="/artifact/external-paper-orders" target="_blank">Paper orders</a>
    <button class="btn" type="button" id="refresh">Refresh status</button>
  </div>
  <section class="panel">
    <h2 style="margin:0 0 8px;font-size:18px">Data health</h2>
    <div class="muted">Checks whether validation, open positions, snapshots, and images line up before you trust the screen.</div>
    <div class="section" style="margin-top:12px"><div id="health-results"></div></div>
  </section>
  <section class="panel">
    <h2 style="margin:0 0 8px;font-size:18px">Open position monitor</h2>
    <div class="muted">Review current lifecycle positions across options, shares, and futures. Click a row to look it up.</div>
    <div class="scan-controls">
      <select id="positions-asset" aria-label="Position asset">
        <option value="all">All assets</option>
        <option value="option">Options</option>
        <option value="share">Shares</option>
        <option value="futures">Futures</option>
      </select>
      <select id="positions-status" aria-label="Position status">
        <option value="all">All statuses</option>
        <option value="attention">Needs attention</option>
        <option value="trade">Trade</option>
        <option value="watch">Watch</option>
        <option value="hold">Hold exit action</option>
        <option value="tighten_stop">Tighten-stop action</option>
        <option value="close_early">Close-early action</option>
      </select>
      <input id="positions-query" placeholder="Filter ticker/contract">
      <button class="btn" type="button" id="positions-load">Apply filters</button>
    </div>
    <div class="status" id="positions-status-text"></div>
    <div class="section" style="margin-top:12px"><div id="positions-results" class="table-wrap"></div></div>
  </section>
  <section class="panel">
    <h2 style="margin:0 0 8px;font-size:18px">Opportunity explorer</h2>
    <div class="muted">Filter the latest ranked options, shares, futures, and value ideas. Click a row to look it up.</div>
    <div class="scan-controls">
      <select id="explorer-asset" aria-label="Explorer asset">
        <option value="all">All assets</option>
        <option value="option">Options</option>
        <option value="share">Shares</option>
        <option value="futures">Futures</option>
        <option value="value">Value</option>
      </select>
      <select id="explorer-status" aria-label="Explorer status">
        <option value="all">All statuses</option>
        <option value="actionable">Actionable only</option>
        <option value="trade">Trade</option>
        <option value="watch">Watch</option>
        <option value="skip">Skip</option>
      </select>
      <input id="explorer-query" placeholder="Filter ticker/headline">
      <input id="explorer-confidence" type="number" min="0" max="100" step="1" placeholder="Min confidence">
      <button class="btn" type="button" id="explorer-load">Apply filters</button>
    </div>
    <div class="status" id="explorer-status-text"></div>
    <div class="section" style="margin-top:12px"><div id="explorer-results" class="table-wrap"></div></div>
  </section>
  <section class="panel">
    <h2 style="margin:0 0 8px;font-size:18px">External paper candidates</h2>
    <div class="muted">Create a small executable candidate list for manual paper tracking. This is intentionally filtered down from the full internal signal stream.</div>
    <div class="scan-controls">
      <select id="paper-asset" aria-label="Paper asset">
        <option value="all">All assets</option>
        <option value="option">Options</option>
        <option value="share">Shares</option>
        <option value="futures">Futures</option>
      </select>
      <input id="paper-max-new" type="number" min="1" max="30" step="1" value="5" aria-label="Max new orders">
      <input id="paper-max-open" type="number" min="1" max="200" step="1" value="30" aria-label="Max open positions">
      <label class="check"><input id="paper-include-watch" type="checkbox"> include Watch</label>
      <label class="check"><input id="paper-zero-size" type="checkbox"> allow zero-size placeholders</label>
      <label class="check"><input id="paper-dry-run" type="checkbox"> review exclusions</label>
      <button class="btn" type="button" id="paper-preview">Preview candidates</button>
      <button class="btn" type="button" id="paper-export">Write export files</button>
    </div>
    <div class="status" id="paper-status-text"></div>
    <div class="section" style="margin-top:12px"><div id="paper-results" class="table-wrap"></div></div>
  </section>
  <section class="panel">
    <h2 style="margin:0 0 8px;font-size:18px">Research watchlist</h2>
    <div class="muted">Save tickers, company names, futures, or option requests you want checked again. Run focused scans from the list when you are ready.</div>
    <div class="search">
      <input id="watchlist-query" placeholder="Add symbol/company/option, e.g. Apple, natural gas, NVDA 20260618 C 200" autocomplete="off">
      <div class="search-actions">
        <button class="btn" type="button" id="watchlist-add">Add</button>
        <button class="btn" type="button" id="watchlist-run">Run watchlist scans</button>
      </div>
    </div>
    <div class="suggestions" id="watchlist-suggestions"></div>
    <div class="scan-controls">
      <select id="watchlist-scan-mode" aria-label="Watchlist scan mode">
        <option value="full">Full scan</option>
        <option value="quick">Quick scan</option>
      </select>
      <input id="watchlist-bankroll" type="number" min="1" step="100" placeholder="Bankroll override">
      <label class="check"><input id="watchlist-aggressive" type="checkbox"> aggressive sizing</label>
    </div>
    <div class="status" id="watchlist-status-text"></div>
    <div class="section" style="margin-top:12px"><div id="watchlist-results" class="table-wrap"></div></div>
  </section>
  <section class="panel">
    <h2 style="margin:0 0 8px;font-size:18px">Symbol lookup</h2>
    <div class="muted">Search the latest local scan snapshots and open positions. For a new symbol, run a focused scan first.</div>
    <div class="search">
      <input id="symbol" placeholder="Type ticker, company, or option idea, e.g. Nvidia, TSLA, AAPL 20260618 C 200" autocomplete="off">
      <div class="search-actions">
        <button class="btn" type="button" id="lookup">Lookup</button>
        <button class="btn" type="button" id="run-symbol">Run focused scan</button>
      </div>
    </div>
    <div class="suggestions" id="symbol-suggestions"></div>
    <div class="scan-controls">
      <select id="scan-mode" aria-label="Scan mode">
        <option value="full">Full scan</option>
        <option value="quick">Quick scan</option>
      </select>
      <input id="scan-bankroll" type="number" min="1" step="100" placeholder="Bankroll override">
      <label class="check"><input id="scan-aggressive" type="checkbox"> aggressive sizing</label>
    </div>
    <div class="status" id="lookup-status"></div>
    <div class="sections" id="lookup-results"></div>
  </section>
  <section class="panel">
    <h2 style="margin:0 0 8px;font-size:18px">Focused scan jobs</h2>
    <div class="muted">Runs started from this cockpit use <code>python run.py --universe SYMBOL --no-open</code> in the background.</div>
    <div class="job-list" id="jobs"></div>
    <pre class="logbox" id="job-log"></pre>
  </section>
  <section class="panel">
    <h2 style="margin:0 0 8px;font-size:18px">System notes</h2>
    <ul class="muted" id="notes"></ul>
  </section>
</div>
<script>
const $ = (id) => document.getElementById(id);
function escHtml(v) { return String(v || '').replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll("'", '&#39;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'); }
function cell(v) { return v === null || v === undefined || v === '' ? '-' : escHtml(String(v).slice(0, 220)); }
function escAttr(v) { return escHtml(v); }
function rowSymbol(r) { return r.ticker || r.symbol || ''; }
function rowLookupSymbol(r) { return r.ticker || r.symbol || r.ticker_or_symbol || ''; }
function pct(v) {
  const n = Number(v);
  return Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : '-';
}
function briefHtml(brief) {
  if (!brief) return '';
  const idea = brief.best_idea || {};
  const open = brief.open_positions || {};
  const val = brief.validation || {};
  const source = brief.resolution_source || '-';
  const resolvedFrom = brief.resolved_from || '';
  const resolvedText = source + (resolvedFrom ? ' from ' + resolvedFrom : '');
  const list = (rows) => (rows && rows.length ? rows.slice(0, 5).map(x => `<li>${escHtml(x.factor)} <b>${cell(x.value)}</b></li>`).join('') : '<li>None surfaced</li>');
  const warnings = (brief.risk_warnings && brief.risk_warnings.length)
    ? brief.risk_warnings.slice(0, 5).map(w => `<li>${escHtml(w)}</li>`).join('')
    : '<li>No local warnings found</li>';
  return `<div class="section"><h3><span>Research brief</span><span>${escHtml(brief.symbol || '')}</span></h3>
    <div style="padding:12px">
      <div class="brief-grid">
        <div class="brief-tile"><span>Best local idea</span><strong>${escHtml(idea.label || 'None')}</strong></div>
        <div class="brief-tile"><span>Status</span><strong>${escHtml(idea.trade_status || '-')}</strong></div>
        <div class="brief-tile"><span>Resolved via</span><strong>${escHtml(resolvedText)}</strong></div>
        <div class="brief-tile"><span>Open exposure</span><strong>${cell(open.count || 0)}</strong></div>
        <div class="brief-tile"><span>Avg unrealized</span><strong>${pct(open.avg_unrealized_pct)}</strong></div>
        <div class="brief-tile"><span>Validation win rate</span><strong>${pct(val.win_rate)}</strong></div>
        <div class="brief-tile"><span>Validation avg return</span><strong>${pct(val.avg_return)}</strong></div>
      </div>
      <div class="brief-cols">
        <div class="brief-list"><h4>Positive factors</h4><ul>${list(brief.top_positive_factors)}</ul></div>
        <div class="brief-list"><h4>Negative factors</h4><ul>${list(brief.top_negative_factors)}</ul></div>
        <div class="brief-list"><h4>Warnings</h4><ul>${warnings}</ul></div>
      </div>
    </div>
  </div>`;
}
function table(rows, clickRows=false) {
  if (!rows || rows.length === 0) return '<div class="empty">No matching rows.</div>';
  const cols = [...new Set(rows.flatMap(r => Object.keys(r)))];
  const head = cols.map(c => `<th>${escHtml(c)}</th>`).join('');
  const body = rows.map(r => {
    const sym = clickRows ? rowLookupSymbol(r) : '';
    const attrs = sym ? ` class="clickable-row" data-symbol="${escAttr(sym)}"` : '';
    return `<tr${attrs}>${cols.map(c => `<td>${cell(r[c])}</td>`).join('')}</tr>`;
  }).join('');
  return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}
function healthClass(level) {
  if (level === 'bad') return 'bad';
  if (level === 'warn') return 'warn';
  return 'good';
}
function healthTable(health) {
  const checks = (health && health.checks) || [];
  if (!checks.length) return '<div class="empty">No health checks available.</div>';
  const body = checks.map(c => `<tr><td><strong class="${healthClass(c.level)}">${cell(c.level)}</strong></td><td>${cell(c.label)}</td><td>${cell(c.detail)}</td></tr>`).join('');
  return `<div class="table-wrap"><table><thead><tr><th>Status</th><th>Check</th><th>Detail</th></tr></thead><tbody>${body}</tbody></table></div>`;
}
function suggestionHtml(rows) {
  if (!rows || rows.length === 0) return '';
  return rows.slice(0, 10).map(r => `<button class="suggestion" type="button" data-query="${escAttr(r.query || r.symbol || '')}"><b>${cell(r.symbol)}</b><span>${cell(r.kind)} - ${cell(r.source)}</span></button>`).join('');
}
let suggestionTimer = null;
async function loadSuggestions(inputId, targetId, autoLookup=false) {
  const q = $(inputId).value.trim();
  const target = $(targetId);
  if (!target) return;
  if (q.length < 1) {
    target.innerHTML = '';
    return;
  }
  const res = await fetch('/api/suggestions?query=' + encodeURIComponent(q) + '&limit=10');
  const data = await res.json();
  target.innerHTML = suggestionHtml(data.rows || []);
  target.querySelectorAll('.suggestion').forEach(btn => {
    btn.addEventListener('click', async () => {
      $(inputId).value = btn.dataset.query || '';
      target.innerHTML = '';
      if (autoLookup) await lookup();
    });
  });
}
function scheduleSuggestions(inputId, targetId, autoLookup=false) {
  clearTimeout(suggestionTimer);
  suggestionTimer = setTimeout(() => {
    loadSuggestions(inputId, targetId, autoLookup).catch(() => {});
  }, 160);
}
function watchlistTable(rows) {
  if (!rows || rows.length === 0) return '<div class="empty">No saved research targets yet.</div>';
  const body = rows.map(r => {
    const request = r.request ? `${r.request.side || ''} ${r.request.expiry || ''} ${r.request.strike || ''}` : '';
    return `<tr>
      <td><button class="btn watch-lookup-btn" type="button" data-query="${escAttr(r.query || r.symbol || '')}">Lookup</button></td>
      <td><strong>${cell(r.symbol)}</strong></td>
      <td>${cell(r.query)}</td>
      <td>${cell(r.best_idea || '-')}</td>
      <td>${cell(r.best_status || '-')}</td>
      <td>${cell(r.best_confidence || '-')}</td>
      <td>${cell(r.open_count || 0)}</td>
      <td>${pct(r.avg_unrealized_pct)}</td>
      <td>${cell(r.warning_count || 0)}</td>
      <td>${cell(request)}</td>
      <td><button class="btn watch-remove-btn" type="button" data-id="${escAttr(r.id)}">Remove</button></td>
    </tr>`;
  }).join('');
  return `<div class="table-wrap"><table><thead><tr>
    <th></th><th>Symbol</th><th>Query</th><th>Best local idea</th><th>Status</th><th>Conf</th><th>Open</th><th>Avg open P&amp;L</th><th>Warnings</th><th>Request</th><th></th>
  </tr></thead><tbody>${body}</tbody></table></div>`;
}
function wireClickableRows(root=document) {
  root.querySelectorAll('.clickable-row').forEach(row => {
    row.addEventListener('click', async () => {
      $('symbol').value = row.dataset.symbol || '';
      await lookup();
      window.location.hash = 'lookup';
    });
  });
}
function wireWatchlistRows() {
  document.querySelectorAll('.watch-lookup-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      $('symbol').value = btn.dataset.query || '';
      await lookup();
    });
  });
  document.querySelectorAll('.watch-remove-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      await removeWatchlist(btn.dataset.id || '');
    });
  });
}
async function loadSummary() {
  const res = await fetch('/api/summary');
  const data = await res.json();
  $('asof').textContent = new Date(data.generated_at).toLocaleString();
  $('open-options').textContent = data.open_counts.options;
  $('open-shares').textContent = data.open_counts.shares;
  $('open-futures').textContent = data.open_counts.futures;
  $('total-open').textContent = data.total_open;
  $('data-health').textContent = (data.data_health && data.data_health.status) || '-';
  $('data-health').className = healthClass((data.data_health && data.data_health.status) || 'ok');
  $('health-results').innerHTML = healthTable(data.data_health);
  $('notes').innerHTML = (data.notes || []).map(n => `<li>${n}</li>`).join('');
}
function jobClass(status) {
  if (status === 'completed') return 'good';
  if (status === 'failed') return 'bad';
  if (status === 'running') return 'warn';
  return '';
}
function jobHtml(job) {
  const dash = job.dashboard_path ? `<a class="btn" href="/job-dashboard?id=${encodeURIComponent(job.job_id)}" target="_blank">Dashboard</a>` : '';
  const match = job.request ? `<button class="btn job-match-btn" type="button" data-query="${escAttr(job.query)}">Match</button>` : '';
  const req = job.request ? ` | ${job.request.side} ${job.request.expiry} ${job.request.strike}` : '';
  const mode = job.scan_mode ? ` | ${job.scan_mode}` : '';
  return `<div class="job"><div><code>${job.symbol || job.query}</code> <span class="${jobClass(job.status)}">${job.status}</span><small>${job.name || job.query || ''}${req}${mode} ${job.updated_at || ''}</small></div><div>${dash}${match}<button class="btn job-log-btn" type="button" data-job="${job.job_id}">Log</button></div></div>`;
}
async function loadJobs() {
  const res = await fetch('/api/jobs');
  const data = await res.json();
  $('jobs').innerHTML = (data.jobs || []).map(jobHtml).join('') || '<div class="empty">No focused scan jobs yet.</div>';
  document.querySelectorAll('.job-log-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const id = btn.dataset.job;
      const res = await fetch('/api/job-log?id=' + encodeURIComponent(id));
      const data = await res.json();
      $('job-log').textContent = (data.lines || []).join('\\n') || 'No log output yet.';
      $('job-log').classList.add('active');
    });
  });
  document.querySelectorAll('.job-match-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      $('symbol').value = btn.dataset.query || '';
      await lookup();
    });
  });
}
async function loadExplorer() {
  $('explorer-status-text').textContent = 'Loading ranked opportunities...';
  const params = new URLSearchParams({
    asset: $('explorer-asset').value,
    status: $('explorer-status').value,
    query: $('explorer-query').value.trim(),
    min_confidence: $('explorer-confidence').value || '0',
    limit: '80'
  });
  const res = await fetch('/api/opportunities?' + params.toString());
  const data = await res.json();
  $('explorer-status-text').textContent = `${data.count || 0} latest local opportunity row(s).`;
  $('explorer-results').innerHTML = table(data.rows || [], true);
  wireClickableRows($('explorer-results'));
}
async function loadWatchlist() {
  const res = await fetch('/api/watchlist?enrich=1');
  const data = await res.json();
  const suffix = data.enriched ? ' with latest local context' : '';
  $('watchlist-status-text').textContent = `${data.count || 0} saved research target(s)${suffix}.`;
  $('watchlist-results').innerHTML = watchlistTable(data.entries || []);
  wireWatchlistRows();
}
async function addWatchlist() {
  const query = $('watchlist-query').value.trim() || $('symbol').value.trim();
  if (!query) return;
  $('watchlist-status-text').textContent = 'Resolving and saving watchlist target...';
  const res = await fetch('/api/watchlist-add', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ query })
  });
  const data = await res.json();
  if (!res.ok || data.ok === false) {
    $('watchlist-status-text').textContent = 'Could not add: ' + (data.error || 'unknown error');
    return;
  }
  $('watchlist-query').value = '';
  $('watchlist-status-text').textContent = `${data.entry.symbol} saved to watchlist.`;
  await loadWatchlist();
}
async function removeWatchlist(id) {
  if (!id) return;
  const res = await fetch('/api/watchlist-remove', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ id })
  });
  const data = await res.json();
  $('watchlist-status-text').textContent = data.removed ? 'Removed watchlist target.' : 'Target was not found.';
  await loadWatchlist();
}
async function runWatchlist() {
  $('watchlist-status-text').textContent = 'Starting focused scans for saved targets...';
  const res = await fetch('/api/watchlist-run', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      mode: $('watchlist-scan-mode').value,
      bankroll: $('watchlist-bankroll').value,
      aggressive: $('watchlist-aggressive').checked
    })
  });
  const data = await res.json();
  if (!res.ok || data.ok === false) {
    $('watchlist-status-text').textContent = 'Could not run watchlist: ' + (data.error || 'unknown error');
    return;
  }
  $('watchlist-status-text').textContent = `Started ${data.count || 0} focused scan job(s).`;
  await loadJobs();
}
async function loadPaperCandidates(write=false) {
  $('paper-status-text').textContent = write ? 'Writing export files...' : 'Building paper candidate preview...';
  const dryRun = $('paper-dry-run').checked;
  const payload = {
    asset: $('paper-asset').value,
    max_new: $('paper-max-new').value || 5,
    max_open: $('paper-max-open').value || 30,
    include_watch: $('paper-include-watch').checked,
    allow_zero_size_placeholder: $('paper-zero-size').checked,
    dry_run: dryRun
  };
  const res = write
    ? await fetch('/api/export-paper', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      })
    : await fetch('/api/paper-candidates?' + new URLSearchParams(payload).toString());
  const data = await res.json();
  if (!res.ok || data.error) {
    $('paper-status-text').textContent = 'Paper candidate export failed: ' + (data.error || 'unknown error');
    return;
  }
  const fileNote = data.wrote_files ? ` Files written: ${data.paths.csv || ''}` : '';
  const dryNote = data.dry_run ? `, ${data.excluded_count || 0} excluded rows reviewed` : '';
  $('paper-status-text').textContent = `${data.selected_count || 0} selected candidate(s)${dryNote}.${fileNote}`;
  $('paper-results').innerHTML = table(data.rows || [], true);
  wireClickableRows($('paper-results'));
}
async function loadPositions() {
  $('positions-status-text').textContent = 'Loading open positions...';
  const params = new URLSearchParams({
    asset: $('positions-asset').value,
    status: $('positions-status').value,
    query: $('positions-query').value.trim(),
    limit: '250'
  });
  const res = await fetch('/api/positions?' + params.toString());
  const data = await res.json();
  $('positions-status-text').textContent = `${data.count || 0} open position row(s).`;
  $('positions-results').innerHTML = table(data.rows || [], true);
  wireClickableRows($('positions-results'));
}
async function lookup() {
  const symbol = $('symbol').value.trim();
  if (!symbol) return;
  $('lookup-status').textContent = 'Searching local artifacts...';
  $('lookup-results').innerHTML = '';
  const res = await fetch('/api/lookup?symbol=' + encodeURIComponent(symbol));
  const data = await res.json();
  const resolved = data.lookup_symbol && data.lookup_symbol !== data.query ? ` (${data.lookup_symbol})` : '';
  $('lookup-status').textContent = `${data.total_hits} hit(s) for ${data.query}${resolved}.`;
  const brief = briefHtml(data.brief);
  const sections = Object.entries(data.sections).map(([name, rows]) => {
    return `<div class="section"><h3><span>${name.replaceAll('_', ' ')}</span><span>${rows.length}</span></h3>${table(rows)}</div>`;
  }).join('');
  $('lookup-results').innerHTML = brief + sections;
}
async function runSymbol() {
  const query = $('symbol').value.trim();
  if (!query) return;
  $('lookup-status').textContent = 'Resolving symbol and starting focused scan...';
  const res = await fetch('/api/run-symbol', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      query,
      mode: $('scan-mode').value,
      bankroll: $('scan-bankroll').value,
      aggressive: $('scan-aggressive').checked
    })
  });
  const data = await res.json();
  if (!res.ok || data.ok === false) {
    $('lookup-status').textContent = 'Could not start scan: ' + (data.error || 'unknown error');
    return;
  }
  $('lookup-status').textContent = `Started focused scan for ${data.symbol}. You can keep using the cockpit while it runs.`;
  await loadJobs();
}
$('lookup').addEventListener('click', lookup);
$('run-symbol').addEventListener('click', runSymbol);
$('symbol').addEventListener('keydown', (e) => { if (e.key === 'Enter') lookup(); });
$('symbol').addEventListener('input', () => scheduleSuggestions('symbol', 'symbol-suggestions', true));
$('refresh').addEventListener('click', loadSummary);
$('positions-load').addEventListener('click', loadPositions);
$('positions-query').addEventListener('keydown', (e) => { if (e.key === 'Enter') loadPositions(); });
$('explorer-load').addEventListener('click', loadExplorer);
$('explorer-query').addEventListener('keydown', (e) => { if (e.key === 'Enter') loadExplorer(); });
$('paper-preview').addEventListener('click', () => loadPaperCandidates(false));
$('paper-export').addEventListener('click', () => loadPaperCandidates(true));
$('watchlist-add').addEventListener('click', addWatchlist);
$('watchlist-run').addEventListener('click', runWatchlist);
$('watchlist-query').addEventListener('keydown', (e) => { if (e.key === 'Enter') addWatchlist(); });
$('watchlist-query').addEventListener('input', () => scheduleSuggestions('watchlist-query', 'watchlist-suggestions', false));
loadSummary().catch(err => { $('asof').textContent = 'Status failed'; console.error(err); });
loadJobs().catch(err => console.error(err));
loadPositions().catch(err => { $('positions-status-text').textContent = 'Position monitor failed'; console.error(err); });
loadExplorer().catch(err => { $('explorer-status-text').textContent = 'Explorer failed'; console.error(err); });
loadPaperCandidates(false).catch(err => { $('paper-status-text').textContent = 'Paper candidate preview failed'; console.error(err); });
loadWatchlist().catch(err => { $('watchlist-status-text').textContent = 'Watchlist failed'; console.error(err); });
setInterval(() => { loadJobs().catch(() => {}); }, 5000);
</script>
</body>
</html>"""


class CockpitHandler(BaseHTTPRequestHandler):
    data_dir = DATA_DIR

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj: Any, status: int = 200) -> None:
        self._send(status, _json_bytes(obj), "application/json; charset=utf-8")

    def _send_file(self, path: Path, content_type: str | None = None) -> None:
        if not path.exists() or not path.is_file():
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        if content_type is None:
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._send(200, path.read_bytes(), content_type)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, render_cockpit_html().encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/summary":
            self._send_json(build_summary(self.data_dir))
            return
        if parsed.path == "/api/data-health":
            self._send_json(build_data_health(self.data_dir))
            return
        if parsed.path == "/api/lookup":
            symbol = parse_qs(parsed.query).get("symbol", [""])[0]
            if not symbol.strip():
                self._send_json({"error": "symbol is required"}, status=400)
                return
            self._send_json(lookup_symbol(symbol, self.data_dir))
            return
        if parsed.path == "/api/suggestions":
            params = parse_qs(parsed.query)
            query = params.get("query", [""])[0]
            limit = _int_param(params.get("limit", ["16"])[0], 16, 1, 50)
            self._send_json(build_symbol_suggestions(self.data_dir, query=query, limit=limit))
            return
        if parsed.path == "/api/opportunities":
            params = parse_qs(parsed.query)
            asset = params.get("asset", ["all"])[0].strip().lower()
            if asset not in {"all", *OPPORTUNITY_SPECS.keys()}:
                self._send_json({"error": "invalid asset"}, status=400)
                return
            status = params.get("status", ["all"])[0].strip().lower()
            query = params.get("query", [""])[0]
            min_conf = _float_param(params.get("min_confidence", ["0"])[0], 0.0, 0.0, 100.0)
            limit = _int_param(params.get("limit", ["80"])[0], 80, 1, 250)
            self._send_json(build_opportunities(
                self.data_dir, asset=asset, query=query, status=status,
                min_confidence=min_conf, limit=limit,
            ))
            return
        if parsed.path == "/api/positions":
            params = parse_qs(parsed.query)
            asset = params.get("asset", ["all"])[0].strip().lower()
            if asset not in {"all", *POSITION_FILES.keys()}:
                self._send_json({"error": "invalid asset"}, status=400)
                return
            status = params.get("status", ["all"])[0].strip().lower()
            query = params.get("query", [""])[0]
            limit = _int_param(params.get("limit", ["250"])[0], 250, 1, 500)
            self._send_json(build_positions(
                self.data_dir, asset=asset, query=query, status=status, limit=limit,
            ))
            return
        if parsed.path == "/api/paper-candidates":
            params = parse_qs(parsed.query)
            asset = params.get("asset", ["all"])[0].strip().lower()
            if asset not in {"all", "option", "share", "futures"}:
                self._send_json({"error": "invalid asset"}, status=400)
                return
            max_new = _int_param(params.get("max_new", ["5"])[0], 5, 1, 30)
            max_open = _int_param(params.get("max_open", ["30"])[0], 30, 1, 200)
            include_watch = _bool_param(params.get("include_watch", ["false"])[0])
            allow_zero = _bool_param(params.get("allow_zero_size_placeholder", ["false"])[0])
            dry_run = _bool_param(params.get("dry_run", ["false"])[0])
            self._send_json(build_paper_candidates(
                self.data_dir,
                max_new=max_new,
                max_open=max_open,
                include_watch=include_watch,
                allow_zero_size_placeholder=allow_zero,
                asset=asset,
                dry_run=dry_run,
                write=False,
            ))
            return
        if parsed.path == "/api/watchlist":
            params = parse_qs(parsed.query)
            enrich = _bool_param(params.get("enrich", ["false"])[0])
            self._send_json(load_watchlist(self.data_dir, enrich=enrich))
            return
        if parsed.path == "/api/jobs":
            self._send_json({"jobs": list_jobs(self.data_dir)})
            return
        if parsed.path == "/api/job":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            job = read_job(job_id, self.data_dir) if job_id else None
            if not job:
                self._send_json({"error": "job not found"}, status=404)
                return
            self._send_json(job)
            return
        if parsed.path == "/api/job-log":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            if not job_id:
                self._send_json({"error": "id is required"}, status=400)
                return
            self._send_json(read_job_log(job_id, self.data_dir))
            return
        if parsed.path == "/lookup":
            symbol = parse_qs(parsed.query).get("symbol", [""])[0]
            if not symbol.strip():
                self._send(400, b"symbol is required", "text/plain; charset=utf-8")
                return
            self._send(200, render_html(lookup_symbol(symbol, self.data_dir)).encode("utf-8"),
                       "text/html; charset=utf-8")
            return
        if parsed.path == "/job-dashboard":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            path = job_dashboard_path(job_id, self.data_dir) if job_id else None
            if path is None:
                self._send(404, b"Job dashboard not found", "text/plain; charset=utf-8")
                return
            self._send_file(path, "text/html; charset=utf-8")
            return
        if parsed.path.startswith("/artifact/"):
            name = parsed.path.rsplit("/", 1)[-1]
            path = artifact_path(name, self.data_dir)
            if path is None:
                self._send(404, b"Artifact not found", "text/plain; charset=utf-8")
                return
            content_type = ARTIFACTS.get(name, ("", None))[1]
            self._send_file(path, content_type)
            return
        self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        if parsed.path not in {
            "/api/run-symbol", "/api/export-paper",
            "/api/watchlist-add", "/api/watchlist-remove", "/api/watchlist-run",
        }:
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except Exception:
            length = 0
        raw = self.rfile.read(min(length, 2000)) if length > 0 else b"{}"
        try:
            body = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            body = {}
        if parsed.path == "/api/watchlist-add":
            result = add_watchlist_query(str(body.get("query") or ""), self.data_dir)
            self._send_json(result, status=200 if result.get("ok") else 400)
            return
        if parsed.path == "/api/watchlist-remove":
            result = remove_watchlist_entry(str(body.get("id") or ""), self.data_dir)
            self._send_json(result, status=200 if result.get("ok") else 404)
            return
        if parsed.path == "/api/watchlist-run":
            result = run_watchlist_scans(
                self.data_dir,
                mode=str(body.get("mode") or "full"),
                bankroll=body.get("bankroll"),
                aggressive=_bool_param(body.get("aggressive"), False),
                launch=True,
            )
            self._send_json(result)
            return
        if parsed.path == "/api/export-paper":
            asset = str(body.get("asset") or "all").strip().lower()
            if asset not in {"all", "option", "share", "futures"}:
                self._send_json({"error": "invalid asset"}, status=400)
                return
            dry_run = _bool_param(body.get("dry_run"), False)
            report = build_paper_candidates(
                self.data_dir,
                max_new=_int_param(str(body.get("max_new") or "5"), 5, 1, 30),
                max_open=_int_param(str(body.get("max_open") or "30"), 30, 1, 200),
                include_watch=_bool_param(body.get("include_watch"), False),
                allow_zero_size_placeholder=_bool_param(body.get("allow_zero_size_placeholder"), False),
                asset=asset,
                dry_run=dry_run,
                write=not dry_run,
            )
            self._send_json(report)
            return
        query = str(body.get("query") or "").strip()
        if not query:
            self._send_json({"ok": False, "error": "query is required"}, status=400)
            return
        mode = str(body.get("mode") or "full").strip().lower()
        scan_args = _scan_args_from_controls(
            mode,
            body.get("bankroll"),
            _bool_param(body.get("aggressive"), False),
        )
        result = create_job(query, self.data_dir, launch=True,
                            extra_scan_args=scan_args, scan_mode=mode or "full")
        self._send_json(result, status=200 if result.get("ok") else 400)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def run_server(host: str = "127.0.0.1", port: int = 8765,
               data_dir: Path = DATA_DIR, open_browser: bool = True) -> None:
    handler = type("OptedgeCockpitHandler", (CockpitHandler,), {"data_dir": data_dir})
    server = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}"
    print(f"Optedge cockpit: {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nCockpit stopped.")
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the free local Optedge interactive cockpit.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--data-dir", default=str(DATA_DIR))
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args(argv)
    run_server(args.host, args.port, Path(args.data_dir), open_browser=not args.no_open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
