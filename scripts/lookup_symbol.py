"""Local Optedge ticker lookup.

This does not call a broker or paid API. It reads the latest generated Optedge
snapshots and open-position state, then writes a compact ticker report.
"""
from __future__ import annotations

import argparse
import html
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

SNAPSHOTS = {
    "options": ("top_options_*.parquet", "ticker"),
    "shares": ("top_shares_*.parquet", "ticker"),
    "value": ("top_value_*.parquet", "ticker"),
    "futures": ("top_futures_*.parquet", "symbol"),
}

OPEN_FILES = {
    "open_options": ("open_positions.json", "ticker"),
    "open_shares": ("open_share_positions.json", "ticker"),
    "open_futures": ("open_futures_positions.json", "symbol"),
}

DISPLAY_COLUMNS = {
    "options": [
        "ticker", "side", "strike", "expiry", "dte", "mid", "spot", "confidence",
        "rank_score", "fused_score", "trade_status", "suggested_contracts",
        "stop_price", "target_price", "spread_pct", "ev_pct", "net_edge_pct",
        "top_headline",
    ],
    "shares": [
        "ticker", "spot", "confidence", "rank_score", "fused_score", "trade_status",
        "suggested_dollars", "stop_price", "target_price", "ev_pct", "top_headline",
    ],
    "value": [
        "ticker", "value_score", "value_bucket", "pe", "fcf_yield", "earnings_yield",
        "insider_score", "top_headline",
    ],
    "futures": [
        "symbol", "name", "direction", "contract", "using_micro", "futures_score",
        "rank_score", "trade_status", "suggested_contracts", "entry_price",
        "stop_price", "target_price", "risk_dollars", "reward_dollars",
    ],
    "open_options": [
        "ticker", "side", "strike", "expiry", "entry_time", "entry_price",
        "current_mid", "unrealized_pct", "trade_status", "stop_price", "target_price",
        "last_reprice_source",
    ],
    "open_shares": [
        "ticker", "entry_time", "entry_price", "current_price", "unrealized_pct",
        "trade_status", "stop_price", "target_price", "latest_exit_action",
    ],
    "open_futures": [
        "symbol", "direction", "entry_time", "entry_price", "current_price",
        "pnl_pct", "pnl_dollars", "trade_status", "stop_price", "target_price",
        "latest_exit_action",
    ],
}


def _latest_file(data_dir: Path, pattern: str) -> Path | None:
    files = [p for p in data_dir.glob(pattern) if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: (p.stat().st_mtime, p.name))


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


def _read_json_rows(path: Path) -> pd.DataFrame:
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return pd.DataFrame()
    if not isinstance(rows, list) or not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


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


def _frame_records(df: pd.DataFrame, section: str) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    cols = [c for c in DISPLAY_COLUMNS.get(section, []) if c in df.columns]
    if not cols:
        cols = list(df.columns[:20])
    records = []
    for _, row in df[cols].head(100).iterrows():
        records.append({str(k): _clean_value(v) for k, v in row.to_dict().items()})
    return records


def _match(df: pd.DataFrame, column: str, query: str) -> pd.DataFrame:
    if df is None or df.empty or column not in df.columns:
        return pd.DataFrame()
    q = query.strip().upper()
    values = df[column].astype(str).str.upper().str.strip()
    return df[values == q].copy()


def lookup_symbol(query: str, data_dir: Path = DATA_DIR) -> dict[str, Any]:
    q = query.strip().upper()
    generated_at = datetime.now(timezone.utc).isoformat()
    sections: dict[str, list[dict[str, Any]]] = {}
    sources: dict[str, str | None] = {}

    for section, (pattern, column) in SNAPSHOTS.items():
        path = _latest_file(data_dir, pattern)
        sources[section] = path.name if path else None
        sections[section] = _frame_records(_match(_read_parquet(path), column, q), section)

    for section, (filename, column) in OPEN_FILES.items():
        path = data_dir / filename
        sources[section] = filename if path.exists() else None
        sections[section] = _frame_records(_match(_read_json_rows(path), column, q), section)

    total_hits = sum(len(rows) for rows in sections.values())
    return {
        "generated_at": generated_at,
        "query": q,
        "total_hits": total_hits,
        "sources": sources,
        "sections": sections,
        "notes": [
            "Lookup uses latest local Optedge snapshots only.",
            "Run a fresh scan with --universe TICKER if the ticker is missing or stale.",
            "This is research output only, not an order or financial advice.",
        ],
    }


def _render_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p class='muted'>No matching rows.</p>"
    columns = list(dict.fromkeys(k for row in rows for k in row.keys()))
    head = "".join(f"<th>{html.escape(c)}</th>" for c in columns)
    body = []
    for row in rows:
        cells = []
        for col in columns:
            value = row.get(col)
            text = "-" if value is None else str(value)
            cells.append(f"<td>{html.escape(text[:220])}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"<div class='table-wrap'><table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"


def render_html(report: dict[str, Any]) -> str:
    q = html.escape(str(report.get("query", "")))
    sections = report.get("sections", {})
    parts = []
    for name, rows in sections.items():
        parts.append(
            f"<section><h2>{html.escape(name.replace('_', ' ').title())} "
            f"<span>{len(rows)}</span></h2>{_render_table(rows)}</section>"
        )
    notes = "".join(f"<li>{html.escape(str(n))}</li>" for n in report.get("notes", []))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Optedge Lookup - {q}</title>
<style>
body {{ margin:0; background:#090b10; color:#e5e7eb; font-family:Inter,Segoe UI,Arial,sans-serif; }}
.wrap {{ max-width:1280px; margin:0 auto; padding:28px 18px 60px; }}
header {{ display:flex; justify-content:space-between; gap:16px; align-items:flex-end; border-bottom:1px solid #1f2937; padding-bottom:16px; }}
h1 {{ margin:0; font-size:28px; }}
.muted, li {{ color:#94a3b8; }}
.pill {{ border:1px solid #334155; background:#111827; border-radius:999px; padding:6px 10px; font-size:12px; }}
section {{ margin-top:20px; border:1px solid #1f2937; border-radius:8px; background:#0f172a; padding:14px; }}
h2 {{ margin:0 0 12px; font-size:15px; }}
h2 span {{ color:#38bdf8; font-family:monospace; }}
.table-wrap {{ overflow:auto; }}
table {{ width:100%; border-collapse:collapse; font-size:12px; }}
th, td {{ padding:8px 10px; border-bottom:1px solid #1f2937; text-align:left; vertical-align:top; }}
th {{ color:#94a3b8; text-transform:uppercase; font-size:10px; letter-spacing:.4px; }}
</style>
</head>
<body><div class="wrap">
<header><div><h1>Optedge Lookup: {q}</h1><div class="muted">Latest local scan snapshot</div></div><div class="pill">{report.get('total_hits', 0)} hits</div></header>
<ul>{notes}</ul>
{''.join(parts)}
</div></body></html>"""


def save_lookup(report: dict[str, Any], data_dir: Path = DATA_DIR) -> dict[str, Path]:
    data_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in report["query"] if ch.isalnum() or ch in {"_", "-", "="}) or "lookup"
    json_path = data_dir / f"lookup_{safe}.json"
    html_path = data_dir / f"lookup_{safe}.html"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    return {"json": json_path, "html": html_path}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Look up one ticker/symbol in latest local Optedge outputs.")
    ap.add_argument("symbol", help="Ticker or futures symbol to inspect, e.g. NVDA, TSLA, CL=F")
    ap.add_argument("--data-dir", default=str(DATA_DIR))
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args(argv)

    report = lookup_symbol(args.symbol, Path(args.data_dir))
    paths = save_lookup(report, Path(args.data_dir))
    print(json.dumps(report, indent=2, default=str) if args.json_only else f"Lookup report: {paths['html']}\nLookup JSON: {paths['json']}\nHits: {report['total_hits']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
