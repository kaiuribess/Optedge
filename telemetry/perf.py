# Purpose: Per-engine performance tracking.
"""Per-engine performance tracking.

Records latency, success/fail status, row count per engine per run.
Writes to data/engine_perf.parquet for rolling p50/p95/p99 stats.

Usage in run.py:
    with track("mispricing") as t:
        result = mispricing.run(universe)
        t.set_rows(len(result))
"""
from __future__ import annotations
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

log = logging.getLogger("optedge.perf")
ROOT = Path(__file__).resolve().parent.parent
PERF_LOG = ROOT / "data" / "engine_perf.parquet"
MAX_LOG_ROWS = 5000  # Keep rolling buffer


class EngineTimer:
    def __init__(self, name: str):
        self.name = name
        self.start = None
        self.elapsed = 0.0
        self.rows = 0
        self.ok = True
        self.error = ""

    def set_rows(self, n: int):
        self.rows = int(n) if n is not None else 0

    def set_error(self, e):
        self.ok = False
        self.error = str(e)[:200]


@contextmanager
def track(name: str):
    t = EngineTimer(name)
    t.start = time.time()
    try:
        yield t
    except Exception as e:
        t.set_error(e)
        raise
    finally:
        t.elapsed = time.time() - t.start
        _append_log(t)


def _append_log(t: EngineTimer):
    """Append a single row to the perf log parquet."""
    try:
        PERF_LOG.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "engine": t.name,
            "elapsed_sec": round(t.elapsed, 3),
            "rows": t.rows,
            "ok": t.ok,
            "error": t.error,
        }
        if PERF_LOG.exists():
            try:
                prev = pd.read_parquet(PERF_LOG)
                if len(prev) >= MAX_LOG_ROWS:
                    prev = prev.tail(MAX_LOG_ROWS - 1)
                df = pd.concat([prev, pd.DataFrame([row])], ignore_index=True)
            except Exception:
                df = pd.DataFrame([row])
        else:
            df = pd.DataFrame([row])
        df.to_parquet(PERF_LOG, index=False)
    except Exception as e:
        log.debug("perf log append fail: %s", e)


def summary(engine: Optional[str] = None, last_n: int = 30) -> Dict:
    """Return p50/p95/p99 latency, success rate over last N runs."""
    if not PERF_LOG.exists():
        return {}
    try:
        df = pd.read_parquet(PERF_LOG)
        if df.empty:
            return {}
        if engine:
            df = df[df["engine"] == engine]
        if df.empty:
            return {}
        df = df.tail(last_n * 50)  # over-fetch then group
        out = {}
        for eng, sub in df.groupby("engine"):
            sub = sub.tail(last_n)
            elapsed = pd.to_numeric(sub["elapsed_sec"], errors="coerce").dropna()
            if elapsed.empty:
                continue
            out[eng] = {
                "n": int(len(sub)),
                "p50": float(elapsed.quantile(0.5)),
                "p95": float(elapsed.quantile(0.95)),
                "p99": float(elapsed.quantile(0.99)),
                "mean": float(elapsed.mean()),
                "ok_rate": float(sub["ok"].mean()),
                "avg_rows": float(pd.to_numeric(sub["rows"], errors="coerce").fillna(0).mean()),
            }
        return out
    except Exception as e:
        log.debug("perf summary fail: %s", e)
        return {}


def latest_run_summary() -> Dict:
    """Return per-engine times from the most recent run only."""
    if not PERF_LOG.exists():
        return {}
    try:
        df = pd.read_parquet(PERF_LOG)
        if df.empty:
            return {}
        # Group by 1-minute buckets, take latest bucket
        df["ts_dt"] = pd.to_datetime(df["ts"], errors="coerce", utc=True)
        df = df.dropna(subset=["ts_dt"]).sort_values("ts_dt", ascending=False)
        if df.empty:
            return {}
        cutoff = df["ts_dt"].iloc[0] - pd.Timedelta(minutes=10)
        recent = df[df["ts_dt"] >= cutoff]
        result = {}
        for _, row in recent.iterrows():
            eng = row["engine"]
            if eng not in result:
                result[eng] = {
                    "elapsed_sec": float(row.get("elapsed_sec", 0)),
                    "rows": int(row.get("rows", 0)),
                    "ok": bool(row.get("ok", True)),
                }
        return result
    except Exception as e:
        log.debug("perf latest fail: %s", e)
        return {}
