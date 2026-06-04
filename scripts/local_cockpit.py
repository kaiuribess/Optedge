"""Free local Optedge cockpit server.

This is a lightweight browser UI for existing Optedge artifacts. It does not
place trades, does not store broker credentials, and does not require paid
dashboard services.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from scripts.lookup_symbol import DATA_DIR, ROOT, lookup_symbol, render_html
from scripts.research_jobs import create_job, list_jobs, read_job, read_job_log


ARTIFACTS = {
    "latest-dashboard": ("dashboard_*.html", "text/html; charset=utf-8"),
    "validation-report": ("validation_report.html", "text/html; charset=utf-8"),
    "validation-summary": ("validation_summary.json", "application/json; charset=utf-8"),
    "factor-ic": ("factor_ic_summary.json", "application/json; charset=utf-8"),
    "position-aging": ("position_aging_summary.json", "application/json; charset=utf-8"),
    "equity-curve": ("equity_curve.png", "image/png"),
    "external-paper-orders": ("external_paper_orders.csv", "text/csv; charset=utf-8"),
}


def _latest_file(data_dir: Path, pattern: str) -> Path | None:
    files = [p for p in data_dir.glob(pattern) if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: (p.stat().st_mtime, p.name))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _count_json_rows(path: Path) -> int:
    rows = _read_json(path)
    return len(rows) if isinstance(rows, list) else 0


def artifact_path(name: str, data_dir: Path = DATA_DIR) -> Path | None:
    spec = ARTIFACTS.get(name)
    if spec is None:
        return None
    pattern, _ = spec
    if "*" in pattern:
        return _latest_file(data_dir, pattern)
    path = data_dir / pattern
    return path if path.exists() and path.is_file() else None


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
input { width:100%; background:var(--panel2); color:var(--text); border:1px solid var(--border); border-radius:8px; padding:12px 14px; font-size:15px; }
input:focus { outline:none; border-color:var(--accent); }
.search-actions { display:flex; gap:8px; flex-wrap:wrap; }
.status { margin-top:8px; font-size:12px; color:var(--muted); min-height:18px; }
.sections { display:grid; grid-template-columns:1fr; gap:12px; margin-top:14px; }
.section { border:1px solid var(--border); border-radius:8px; background:#0b1220; overflow:hidden; }
.section h3 { margin:0; padding:12px 14px; font-size:14px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; }
.table-wrap { overflow:auto; }
table { width:100%; border-collapse:collapse; font-size:12px; }
th, td { padding:8px 10px; border-bottom:1px solid #1d2938; text-align:left; vertical-align:top; }
th { color:var(--muted); text-transform:uppercase; font-size:10px; letter-spacing:.4px; }
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
    <h2 style="margin:0 0 8px;font-size:18px">Symbol lookup</h2>
    <div class="muted">Search the latest local scan snapshots and open positions. For a new symbol, run a focused scan first.</div>
    <div class="search">
      <input id="symbol" placeholder="Type ticker, company, or option idea, e.g. Nvidia, TSLA, AAPL 20260618 C 200" autocomplete="off">
      <div class="search-actions">
        <button class="btn" type="button" id="lookup">Lookup</button>
        <button class="btn" type="button" id="run-symbol">Run focused scan</button>
      </div>
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
function cell(v) { return v === null || v === undefined || v === '' ? '-' : String(v).slice(0, 220); }
function table(rows) {
  if (!rows || rows.length === 0) return '<div class="empty">No matching rows.</div>';
  const cols = [...new Set(rows.flatMap(r => Object.keys(r)))];
  const head = cols.map(c => `<th>${c}</th>`).join('');
  const body = rows.map(r => `<tr>${cols.map(c => `<td>${cell(r[c])}</td>`).join('')}</tr>`).join('');
  return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
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
  const dash = job.dashboard_path ? `<a class="btn" href="/artifact/latest-dashboard" target="_blank">Dashboard</a>` : '';
  const req = job.request ? ` | ${job.request.side} ${job.request.expiry} ${job.request.strike}` : '';
  return `<div class="job"><div><code>${job.symbol || job.query}</code> <span class="${jobClass(job.status)}">${job.status}</span><small>${job.name || job.query || ''}${req} ${job.updated_at || ''}</small></div><div>${dash}<button class="btn job-log-btn" type="button" data-job="${job.job_id}">Log</button></div></div>`;
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
}
async function lookup() {
  const symbol = $('symbol').value.trim();
  if (!symbol) return;
  $('lookup-status').textContent = 'Searching local artifacts...';
  $('lookup-results').innerHTML = '';
  const res = await fetch('/api/lookup?symbol=' + encodeURIComponent(symbol));
  const data = await res.json();
  $('lookup-status').textContent = `${data.total_hits} hit(s) for ${data.query}.`;
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
    body: JSON.stringify({query})
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
loadSummary().catch(err => { $('asof').textContent = 'Status failed'; console.error(err); });
loadJobs().catch(err => console.error(err));
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
        result = create_job(query, self.data_dir, launch=True)
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
