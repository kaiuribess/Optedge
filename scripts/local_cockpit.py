"""Free local Optedge cockpit server.

This is a lightweight browser UI for existing Optedge artifacts. It does not
place trades, does not store broker credentials, and does not require paid
dashboard services.
"""
from __future__ import annotations

import argparse
import json
import math
import mimetypes
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
from scripts.research_jobs import (
    create_job, job_dashboard_path, list_jobs, read_job, read_job_log,
)


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


def build_summary(data_dir: Path = DATA_DIR) -> dict[str, Any]:
    validation = _read_json(data_dir / "validation_summary.json")
    aging = _read_json(data_dir / "position_aging_summary.json")
    open_counts = {
        "options": _count_json_rows(data_dir / "open_positions.json"),
        "shares": _count_json_rows(data_dir / "open_share_positions.json"),
        "futures": _count_json_rows(data_dir / "open_futures_positions.json"),
    }
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
    <h2 style="margin:0 0 8px;font-size:18px">Symbol lookup</h2>
    <div class="muted">Search the latest local scan snapshots and open positions. For a new symbol, run a focused scan first.</div>
    <div class="search">
      <input id="symbol" placeholder="Type ticker, company, or option idea, e.g. Nvidia, TSLA, AAPL 20260618 C 200" autocomplete="off">
      <div class="search-actions">
        <button class="btn" type="button" id="lookup">Lookup</button>
        <button class="btn" type="button" id="run-symbol">Run focused scan</button>
      </div>
    </div>
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
function table(rows, clickRows=false) {
  if (!rows || rows.length === 0) return '<div class="empty">No matching rows.</div>';
  const cols = [...new Set(rows.flatMap(r => Object.keys(r)))];
  const head = cols.map(c => `<th>${escHtml(c)}</th>`).join('');
  const body = rows.map(r => {
    const sym = clickRows ? rowSymbol(r) : '';
    const attrs = sym ? ` class="clickable-row" data-symbol="${escAttr(sym)}"` : '';
    return `<tr${attrs}>${cols.map(c => `<td>${cell(r[c])}</td>`).join('')}</tr>`;
  }).join('');
  return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
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
async function loadSummary() {
  const res = await fetch('/api/summary');
  const data = await res.json();
  $('asof').textContent = new Date(data.generated_at).toLocaleString();
  $('open-options').textContent = data.open_counts.options;
  $('open-shares').textContent = data.open_counts.shares;
  $('open-futures').textContent = data.open_counts.futures;
  $('total-open').textContent = data.total_open;
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
      $('job-log').textContent = (data.lines || []).join('\n') || 'No log output yet.';
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
async function lookup() {
  const symbol = $('symbol').value.trim();
  if (!symbol) return;
  $('lookup-status').textContent = 'Searching local artifacts...';
  $('lookup-results').innerHTML = '';
  const res = await fetch('/api/lookup?symbol=' + encodeURIComponent(symbol));
  const data = await res.json();
  const resolved = data.lookup_symbol && data.lookup_symbol !== data.query ? ` (${data.lookup_symbol})` : '';
  $('lookup-status').textContent = `${data.total_hits} hit(s) for ${data.query}${resolved}.`;
  $('lookup-results').innerHTML = Object.entries(data.sections).map(([name, rows]) => {
    return `<div class="section"><h3><span>${name.replaceAll('_', ' ')}</span><span>${rows.length}</span></h3>${table(rows)}</div>`;
  }).join('');
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
$('refresh').addEventListener('click', loadSummary);
$('explorer-load').addEventListener('click', loadExplorer);
$('explorer-query').addEventListener('keydown', (e) => { if (e.key === 'Enter') loadExplorer(); });
loadSummary().catch(err => { $('asof').textContent = 'Status failed'; console.error(err); });
loadJobs().catch(err => console.error(err));
loadExplorer().catch(err => { $('explorer-status-text').textContent = 'Explorer failed'; console.error(err); });
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
        if parsed.path == "/api/lookup":
            symbol = parse_qs(parsed.query).get("symbol", [""])[0]
            if not symbol.strip():
                self._send_json({"error": "symbol is required"}, status=400)
                return
            self._send_json(lookup_symbol(symbol, self.data_dir))
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
        if parsed.path != "/api/run-symbol":
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
        query = str(body.get("query") or "").strip()
        if not query:
            self._send_json({"ok": False, "error": "query is required"}, status=400)
            return
        mode = str(body.get("mode") or "full").strip().lower()
        scan_args = ["--minimal"] if mode == "quick" else []
        if body.get("aggressive"):
            scan_args.append("--aggressive")
        try:
            bankroll = float(body.get("bankroll") or 0)
        except Exception:
            bankroll = 0.0
        if bankroll > 0:
            scan_args.extend(["--bankroll", str(bankroll)])
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
