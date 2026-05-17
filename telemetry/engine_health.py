"""Rolling engine health registry.

Each scan records per-engine row counts, runtime, and failure state. The
dashboard and research guard can use this to spot engines that are persistently
empty or slow instead of treating every run as a fresh mystery.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
HEALTH_JSON = DATA_DIR / "engine_health.json"
HEALTH_JSONL = DATA_DIR / "engine_health_history.jsonl"
MAX_HISTORY_ROWS = 1000


def _read_history() -> List[Dict[str, Any]]:
    if not HEALTH_JSONL.exists():
        return []
    rows = []
    try:
        for line in HEALTH_JSONL.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except Exception:
        return []
    return rows[-MAX_HISTORY_ROWS:]


def record(timings: Dict[str, Dict[str, Any]], empty_engines: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    """Append one run of engine telemetry and rewrite the compact summary."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    empty_names = {str(e.get("name")) for e in (empty_engines or [])}
    run_id = datetime.now(timezone.utc).isoformat()
    rows = []
    for name, data in (timings or {}).items():
        rows.append({
            "ts": run_id,
            "engine": name,
            "ok": bool(data.get("ok", False)),
            "rows": int(data.get("rows") or 0),
            "elapsed": float(data.get("elapsed") or 0.0),
            "empty": name in empty_names or int(data.get("rows") or 0) == 0,
            "error": data.get("error"),
        })
    if rows:
        with HEALTH_JSONL.open("a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, default=str) + "\n")
    history = _read_history()
    summary = summarize(history)
    HEALTH_JSON.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary


def summarize(rows: List[Dict[str, Any]] | None = None, window: int = 20) -> Dict[str, Any]:
    rows = rows if rows is not None else _read_history()
    if not rows:
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "engines": []}
    df = pd.DataFrame(rows)
    engines = []
    for name, sub in df.groupby("engine"):
        recent = sub.tail(window)
        runs = int(len(recent))
        ok_rate = float(recent["ok"].mean()) if runs else 0.0
        hit_rate = float((recent["rows"] > 0).mean()) if runs else 0.0
        empty_rate = float(recent["empty"].mean()) if runs else 0.0
        avg_rows = float(pd.to_numeric(recent["rows"], errors="coerce").fillna(0).mean())
        avg_elapsed = float(pd.to_numeric(recent["elapsed"], errors="coerce").fillna(0).mean())
        score = max(0.0, min(100.0, 100.0 * (0.55 * ok_rate + 0.35 * hit_rate + 0.10 * (1.0 - min(avg_elapsed / 300.0, 1.0)))))
        engines.append({
            "engine": str(name),
            "runs": runs,
            "health_score": round(score, 1),
            "ok_rate": round(ok_rate, 3),
            "hit_rate": round(hit_rate, 3),
            "empty_rate": round(empty_rate, 3),
            "avg_rows": round(avg_rows, 1),
            "avg_elapsed": round(avg_elapsed, 2),
            "last_rows": int(sub.iloc[-1].get("rows") or 0),
            "last_elapsed": float(sub.iloc[-1].get("elapsed") or 0.0),
            "last_ok": bool(sub.iloc[-1].get("ok", False)),
        })
    engines.sort(key=lambda r: (r["health_score"], r["engine"]))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": window,
        "engines": engines,
    }


def load_summary() -> Dict[str, Any]:
    if HEALTH_JSON.exists():
        try:
            return json.loads(HEALTH_JSON.read_text(encoding="utf-8"))
        except Exception:
            pass
    return summarize()
