# Purpose: Record loop health and enforce memory and cache limits.
"""Record whole-day process reliability and bound local cache growth.

Per-iter health record so silent degradation over a 13-iter market day is
visible. Writes to `telemetry/health.parquet` (or `.jsonl` fallback when no
parquet engine available). Rolling window of 500 rows.

Tracked per iter: timestamp, runtime, RSS memory, cache size, error string.
Pre-iter check forces GC + cache prune when RSS is too high.
"""
from __future__ import annotations
import gc
import json
import logging
import os
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import psutil

log = logging.getLogger("optedge.health")

ROOT = Path(__file__).resolve().parent.parent
HEALTH_FILE = ROOT / "telemetry" / "health.parquet"
HEALTH_JSONL = ROOT / "telemetry" / "health.jsonl"
MAX_ROWS = 500


def _mem_rss_mb() -> Optional[float]:
    """Return this process's resident memory in MiB, or ``None`` on OS error."""
    try:
        return float(psutil.Process(os.getpid()).memory_info().rss) / (1024 * 1024)
    except Exception:
        return None


def _cache_size_mb() -> float:
    cache_dir = ROOT / "data" / "_cache"
    if not cache_dir.exists():
        return 0.0
    total = 0
    for p in cache_dir.glob("**/*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except Exception:
            continue
    return total / (1024 * 1024)


def prune_cache(max_mb: float = 250.0) -> int:
    """Drop oldest 50% of cache files when total cache exceeds max_mb."""
    cache_dir = ROOT / "data" / "_cache"
    if not cache_dir.exists():
        return 0
    files: List[Path] = [p for p in cache_dir.glob("**/*") if p.is_file()]
    total_mb = sum(p.stat().st_size for p in files) / (1024 * 1024)
    if total_mb < max_mb:
        return 0
    files.sort(key=lambda p: p.stat().st_mtime)
    drop_count = len(files) // 2
    removed = 0
    for p in files[:drop_count]:
        try:
            p.unlink()
            removed += 1
        except Exception:
            continue
    log.info("health: pruned %d cache files (was %.0fMB, target %.0fMB)",
             removed, total_mb, max_mb)
    return removed


def _trim_health_file() -> None:
    if not HEALTH_FILE.exists():
        return
    try:
        df = pd.read_parquet(HEALTH_FILE)
        if len(df) > MAX_ROWS:
            df = df.tail(MAX_ROWS)
            df.to_parquet(HEALTH_FILE, index=False)
    except Exception as e:
        log.debug("health trim fail: %s", e)


def record(record_data: Dict[str, Any]) -> None:
    """Append one row to telemetry/health.parquet (or .jsonl fallback)."""
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "py_version": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "mem_rss_mb": _mem_rss_mb(),
        "cache_mb": _cache_size_mb(),
    }
    row.update(record_data)
    try:
        if HEALTH_FILE.exists():
            df = pd.read_parquet(HEALTH_FILE)
            df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
        else:
            df = pd.DataFrame([row])
        df.to_parquet(HEALTH_FILE, index=False)
        _trim_health_file()
        return
    except Exception as e:
        log.debug("health parquet write failed (%s), falling back to JSONL", e)
    try:
        with HEALTH_JSONL.open("a") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception as e:
        log.debug("health jsonl write failed: %s", e)


def assert_can_continue(min_free_mem_mb: float = 200.0) -> bool:
    """Pre-iter check: enough memory, cache not absurd."""
    rss = _mem_rss_mb()
    if rss is not None and rss > 6000:
        log.warning("health: high RSS %.0fMB — forcing GC + cache prune", rss)
        gc.collect()
        prune_cache(max_mb=200.0)
        rss = _mem_rss_mb()
        if rss is not None and rss > 8000:
            log.error("health: RSS still %.0fMB after prune — consider restarting", rss)
    return True


def summary() -> Dict[str, Any]:
    """Read health (.parquet or .jsonl) and return last-N-iter summary."""
    df = None
    if HEALTH_FILE.exists():
        try:
            df = pd.read_parquet(HEALTH_FILE)
        except Exception:
            df = None
    if (df is None or df.empty) and HEALTH_JSONL.exists():
        try:
            rows = [json.loads(line) for line in HEALTH_JSONL.read_text().splitlines() if line.strip()]
            df = pd.DataFrame(rows[-MAX_ROWS:])
        except Exception:
            df = None
    if df is None or df.empty:
        return {}
    last = df.iloc[-1].to_dict()
    last5 = df.tail(5)
    return {
        "iters_today": len(df),
        "last_iter_at": last.get("ts"),
        "last_iter_seconds": last.get("iter_seconds"),
        "mem_rss_mb": last.get("mem_rss_mb"),
        "cache_mb": last.get("cache_mb"),
        "mean_iter_seconds_5": float(last5["iter_seconds"].mean())
                                if "iter_seconds" in last5.columns else None,
        "engine_failures_5": (int(last5["failure_count"].sum())
                              if "failure_count" in last5.columns else 0),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    record({"iter_seconds": 42.0, "iteration": 1, "contracts": 2500})
    print(json.dumps(summary(), indent=2, default=str))
