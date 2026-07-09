"""Research safety guardrails for live Optedge recommendations.

The guard does two jobs:
  1. Produce a machine-readable warning report from validation metrics.
  2. Downgrade recommendations that are structurally unsafe to trade, such as
     options with spreads wider than the configured threshold.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

MIN_CLOSED_SIGNALS = 500
MAX_DRAWDOWN_LIMIT = -0.20
MAX_OPTION_SPREAD_PCT = 0.15
MODEL_STALE_DAYS = 14
BREAKEVEN_WIN_RATE = 0.50
KEY_ENGINES = {"mispricing", "news", "fundamentals", "earnings", "insider"}


def _load_summary(summary_path: Optional[Path] = None) -> Dict[str, Any]:
    path = summary_path or DATA_DIR / "validation_summary.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _age_days(path: Path) -> Optional[float]:
    if not path.exists():
        return None
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return (datetime.now(timezone.utc) - mtime).total_seconds() / 86400


def _warn(code: str, severity: str, message: str, blocks_trading: bool = False) -> Dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "blocks_trading": bool(blocks_trading),
    }


def build_guard_report(
    validation_summary: Optional[Dict[str, Any]] = None,
    empty_engines: Optional[Iterable[Dict[str, Any]]] = None,
    engine_health: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    summary = validation_summary if validation_summary is not None else _load_summary()
    now = now or datetime.now(timezone.utc)
    warnings: List[Dict[str, Any]] = []

    swing_metrics = summary.get("swing_eligible_after_slippage")
    uses_swing_sample = isinstance(swing_metrics, dict)
    overall = swing_metrics if uses_swing_sample else (summary.get("after_slippage") or summary.get("overall") or {})
    closed = int(
        (summary.get("swing_eligible_closed_positions") or overall.get("n") or 0)
        if uses_swing_sample
        else (summary.get("closed_positions") or overall.get("n") or 0)
    )
    validation_basis = "independent_swing_after_slippage" if uses_swing_sample else "all_closures_after_slippage"
    if closed < MIN_CLOSED_SIGNALS:
        warnings.append(_warn(
            "sample_size",
            "warning",
            f"Only {closed} {validation_basis.replace('_', ' ')} outcomes are available; "
            f"require {MIN_CLOSED_SIGNALS}+ before trusting live sizing.",
            blocks_trading=False,
        ))

    max_dd = overall.get("max_drawdown")
    if max_dd is not None and float(max_dd) < MAX_DRAWDOWN_LIMIT:
        warnings.append(_warn(
            "drawdown",
            "critical",
            f"Validation max drawdown is {float(max_dd) * 100:.1f}%, worse than the -20% research limit.",
            blocks_trading=True,
        ))

    win_rate = overall.get("win_rate")
    if win_rate is not None and float(win_rate) < BREAKEVEN_WIN_RATE:
        warnings.append(_warn(
            "win_rate",
            "warning",
            f"After-slippage win rate is {float(win_rate) * 100:.1f}%, below the simple breakeven threshold.",
            blocks_trading=False,
        ))

    spread_rows = summary.get("spread_buckets") or []
    for row in spread_rows:
        bucket = str(row.get("bucket", ""))
        avg_return = row.get("avg_return")
        if bucket in {"10-15%", "15%+"} and avg_return is not None and float(avg_return) < 0:
            warnings.append(_warn(
                "spread_bucket",
                "warning",
                f"Spread bucket {bucket} has negative average performance after validation.",
                blocks_trading=False,
            ))

    stale_candidates = [ROOT / "config_runtime.py", DATA_DIR / "model_weights.json"]
    ages = [age for age in (_age_days(p) for p in stale_candidates) if age is not None]
    model_age = min(ages) if ages else None
    if model_age is None:
        warnings.append(_warn(
            "model_update_missing",
            "warning",
            "No runtime model update file was found yet.",
            blocks_trading=False,
        ))
    elif model_age >= MODEL_STALE_DAYS:
        warnings.append(_warn(
            "model_stale",
            "warning",
            f"Model weights appear {model_age:.1f} days old; refresh before trusting new signals.",
            blocks_trading=False,
        ))

    failed = []
    for row in empty_engines or []:
        name = str(row.get("engine") or row.get("name") or "")
        if name in KEY_ENGINES:
            failed.append(name)
    if failed:
        warnings.append(_warn(
            "key_data_source_empty",
            "critical",
            f"Key engines returned no data: {', '.join(sorted(set(failed)))}.",
            blocks_trading=True,
        ))

    if engine_health is None:
        try:
            from telemetry.engine_health import load_summary
            engine_health = load_summary()
        except Exception:
            engine_health = {}
    weak_key_engines = []
    for row in (engine_health or {}).get("engines", []):
        name = str(row.get("engine") or "")
        if name not in KEY_ENGINES:
            continue
        health = float(row.get("health_score") or 100)
        hit_rate = float(row.get("hit_rate") or 1)
        if health < 50 or hit_rate < 0.50:
            weak_key_engines.append(name)
    if weak_key_engines:
        warnings.append(_warn(
            "engine_health",
            "warning",
            "Key engines have weak rolling health: "
            + ", ".join(sorted(set(weak_key_engines)))
            + ". Treat affected factors as low-confidence until coverage recovers.",
            blocks_trading=False,
        ))

    status = "blocked" if any(w["blocks_trading"] for w in warnings) else (
        "review" if warnings else "clear"
    )
    return {
        "generated_at": now.isoformat(),
        "status": status,
        "warnings": warnings,
        "closed_signals": closed,
        "validation_basis": validation_basis,
        "model_age_days": model_age,
        "limits": {
            "min_closed_signals": MIN_CLOSED_SIGNALS,
            "max_drawdown": MAX_DRAWDOWN_LIMIT,
            "max_option_spread_pct": MAX_OPTION_SPREAD_PCT,
            "model_stale_days": MODEL_STALE_DAYS,
            "breakeven_win_rate": BREAKEVEN_WIN_RATE,
        },
    }


def apply_to_recommendations(
    options: pd.DataFrame,
    shares: Optional[pd.DataFrame] = None,
    guard_report: Optional[Dict[str, Any]] = None,
) -> Tuple[pd.DataFrame, Optional[pd.DataFrame]]:
    guard_report = guard_report or build_guard_report()
    guard_messages = [w["message"] for w in guard_report.get("warnings", [])]
    status = guard_report.get("status", "clear")

    def _annotate(df: Optional[pd.DataFrame], is_options: bool) -> Optional[pd.DataFrame]:
        if df is None or df.empty:
            return df
        out = df.copy()
        out["research_guard_status"] = status
        if guard_messages:
            out["research_guard_warnings"] = " | ".join(guard_messages[:4])
        else:
            out["research_guard_warnings"] = ""
        if is_options and "spread_pct" in out.columns:
            spread = pd.to_numeric(out["spread_pct"], errors="coerce")
            too_wide = spread > MAX_OPTION_SPREAD_PCT
            if too_wide.any():
                out.loc[too_wide, "research_guard_status"] = "blocked_spread"
                out.loc[too_wide, "trade_status"] = "Watch"
                out.loc[too_wide, "is_actionable"] = False
                if "suggested_contracts" in out.columns:
                    out.loc[too_wide, "suggested_contracts"] = 0
                if "suggested_dollars" in out.columns:
                    out.loc[too_wide, "suggested_dollars"] = 0
                if "actual_dollars" in out.columns:
                    out.loc[too_wide, "actual_dollars"] = 0
                note = f"Blocked: spread is above {MAX_OPTION_SPREAD_PCT * 100:.0f}%."
                out.loc[too_wide, "research_guard_warnings"] = (
                    out.loc[too_wide, "research_guard_warnings"].astype(str).str.strip()
                    + " | "
                    + note
                ).str.strip(" |")
        if status == "blocked":
            out["trade_status"] = "Watch"
            out["is_actionable"] = False
        return out

    return _annotate(options, True), _annotate(shares, False)


def save_guard_report(report: Dict[str, Any], path: Optional[Path] = None) -> Path:
    out = path or DATA_DIR / "research_guard.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return out
