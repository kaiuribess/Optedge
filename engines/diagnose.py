# Purpose: Diagnose current signal, evidence, and learned-weight artifacts.
"""Inspect the current Optedge research-evidence pipeline without changing it.

This compatibility utility inventories local signal logs, fixed-horizon
outcomes, Edge Lab status, and learned-weight metadata. It performs no broker
action, network replay, model refit, or artifact write.

Run it directly from a source checkout:

    python -m engines.diagnose

The supported interactive routes remain ``python run.py --forward`` for
forward telemetry, ``python -m backtest.fixed_horizon`` for fixed-session
settlement, and the local cockpit for the combined evidence view.
"""
from __future__ import annotations

import glob
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Dict

import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
LOGS_DIR = ROOT / "logs"


def _summarize_log_files(pattern: str) -> Dict[str, Any]:
    """Inventory local signal-log files that match ``pattern``."""
    files = sorted(glob.glob(str(LOGS_DIR / pattern)))
    if not files:
        return {
            "n_files": 0,
            "n_rows": 0,
            "newest": None,
            "oldest": None,
            "newest_age_hours": None,
            "oldest_age_hours": None,
            "size_kb": 0.0,
            "read_errors": 0,
        }

    n_rows = 0
    size = 0
    read_errors = 0
    for file_name in files:
        try:
            n_rows += len(pd.read_parquet(file_name))
            size += os.path.getsize(file_name)
        except Exception:
            read_errors += 1

    newest = max(files, key=os.path.getmtime)
    oldest = min(files, key=os.path.getmtime)
    now = datetime.now(UTC).timestamp()
    return {
        "n_files": len(files),
        "n_rows": n_rows,
        "newest": Path(newest).name,
        "oldest": Path(oldest).name,
        "newest_age_hours": max(0.0, (now - os.path.getmtime(newest)) / 3600),
        "oldest_age_hours": max(0.0, (now - os.path.getmtime(oldest)) / 3600),
        "size_kb": round(size / 1024, 1),
        "read_errors": read_errors,
    }


def _artifact_status(path: Path) -> Dict[str, Any]:
    """Return read-only metadata for a local JSON or Parquet artifact."""
    if not path.exists():
        return {
            "path": str(path),
            "exists": False,
            "rows": None,
            "age_hours": None,
            "read_error": None,
        }

    age_hours = max(
        0.0,
        (datetime.now(UTC).timestamp() - path.stat().st_mtime) / 3600,
    )
    rows = None
    read_error = None
    try:
        if path.suffix.lower() == ".parquet":
            rows = len(pd.read_parquet(path))
        elif path.suffix.lower() == ".json":
            json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        read_error = str(exc)[:160]
    return {
        "path": str(path),
        "exists": True,
        "rows": rows,
        "age_hours": round(age_hours, 2),
        "read_error": read_error,
    }


def _current_pipeline_status() -> Dict[str, bool]:
    """Confirm that the supported evidence functions are importable."""
    try:
        from backtest.edge_lab import build_edge_lab
        from backtest.fixed_horizon import run_fixed_horizon_test
        from backtest.forward import run_forward_test
        from backtest.track import (
            log_signals,
            log_signals_futures,
            log_signals_shares,
        )
    except Exception:
        return {
            "forward_telemetry": False,
            "fixed_horizon_settlement": False,
            "edge_lab": False,
            "option_signal_logging": False,
            "share_signal_logging": False,
            "futures_signal_logging": False,
        }
    return {
        "forward_telemetry": callable(run_forward_test),
        "fixed_horizon_settlement": callable(run_fixed_horizon_test),
        "edge_lab": callable(build_edge_lab),
        "option_signal_logging": callable(log_signals),
        "share_signal_logging": callable(log_signals_shares),
        "futures_signal_logging": callable(log_signals_futures),
    }


def _trace_replay_one(
    row: pd.Series,
    replay_fn: Callable[[pd.Series], Dict[str, Any] | None],
) -> Dict[str, Any]:
    """Compatibility helper that captures one caller-supplied replay result."""
    ticker = row.get("ticker") or row.get("symbol") or "?"
    try:
        result = replay_fn(row)
    except Exception as exc:
        return {"ticker": ticker, "status": "ERROR", "reason": str(exc)[:160]}
    if result is None:
        return {
            "ticker": ticker,
            "status": "UNRESOLVED",
            "reason": "the supplied replay function returned no outcome",
        }
    return {
        "ticker": ticker,
        "status": "OK",
        "outcome": result.get("outcome"),
        "pnl_pct": result.get("pnl_pct"),
    }


def _print_log_inventory() -> None:
    print("\n[2] Local signal history")
    print("-" * 72)
    for pattern, label in (
        ("signals_*.parquet", "options"),
        ("shares_signals_*.parquet", "shares"),
        ("futures_signals_*.parquet", "futures"),
    ):
        summary = _summarize_log_files(pattern)
        if not summary["n_files"]:
            print(f"  {label:<10} no local log files")
            continue
        print(
            f"  {label:<10} files={summary['n_files']:<4} "
            f"rows={summary['n_rows']:<7} newest={summary['newest']} "
            f"age={summary['newest_age_hours']:.1f}h "
            f"read_errors={summary['read_errors']}"
        )


def _print_evidence_artifacts() -> None:
    from backtest.fixed_horizon import OUTCOMES_PATH, SUMMARY_PATH

    print("\n[3] Fixed-horizon evidence artifacts")
    print("-" * 72)
    for label, path in (
        ("outcomes", OUTCOMES_PATH),
        ("summary", SUMMARY_PATH),
    ):
        status = _artifact_status(path)
        if not status["exists"]:
            print(f"  {label:<10} missing: {path}")
            continue
        detail = f"rows={status['rows']} " if status["rows"] is not None else ""
        detail += f"age={status['age_hours']:.1f}h"
        if status["read_error"]:
            detail += f" read_error={status['read_error']}"
        print(f"  {label:<10} {detail}")


def _print_edge_status() -> None:
    print("\n[4] Edge Lab")
    print("-" * 72)
    try:
        from backtest.edge_lab import build_edge_lab

        report = build_edge_lab(DATA_DIR, now=datetime.now(UTC))
    except Exception as exc:
        print(f"  unavailable: {str(exc)[:180]}")
        return
    print(f"  status: {report.get('label') or report.get('status') or 'unknown'}")
    print(f"  live-capital eligible: {bool(report.get('live_capital_eligible'))}")
    if report.get("validated_assets"):
        print(f"  validated assets: {', '.join(report['validated_assets'])}")
    if report.get("primary_blocker"):
        print(f"  primary blocker: {report['primary_blocker']}")


def _print_weight_status() -> None:
    from engines.learning import list_bucket_status

    print("\n[5] Runtime weight metadata")
    print("-" * 72)
    for row in list_bucket_status():
        fitted = str(row.get("fitted_at") or "-").split("T")[0]
        print(
            f"  {row.get('bucket', '?'):<22} source={row.get('source', '-'):<18} "
            f"n={int(row.get('n_samples') or 0):<6} fitted={fitted}"
        )


def diagnose() -> None:
    """Print a read-only diagnostic of the current evidence pipeline."""
    print("=" * 72)
    print("Optedge current evidence-pipeline diagnostic")
    print("=" * 72)
    print("This report is diagnostic only and cannot promote a model or authorize a trade.")

    print("\n[1] Supported pipeline routes")
    print("-" * 72)
    pipeline = _current_pipeline_status()
    for name, ready in pipeline.items():
        print(f"  {name.replace('_', ' '):<30} {'ready' if ready else 'unavailable'}")

    _print_log_inventory()
    _print_evidence_artifacts()
    _print_edge_status()
    _print_weight_status()

    print("\nInterpretation")
    print("-" * 72)
    print("  Current-mark forward results are monitoring telemetry only.")
    print("  Fixed-horizon, policy-bound evidence feeds Edge Lab eligibility.")
    print("  Any missing, stale, malformed, or adverse evidence remains blocking.")


if __name__ == "__main__":
    diagnose()
