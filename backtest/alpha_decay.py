# Purpose: Preserve an exploratory alpha-decay diagnostic for compatibility.
"""Exploratory alpha-decay diagnostic retained for compatibility.

Reads logged signals and their realized forward returns at multiple horizons
(1d, 3d, 7d, 14d, 30d) to estimate how long each signal type retains edge.

The helper does not enforce the current fixed-horizon provenance, cost,
coverage, or independence policy. Its output is therefore diagnostic-only: it
cannot promote weights, clear Edge Lab, or authorize live review.

``write_alpha_decay`` can persist a labeled local artifact for explicit
inspection. No current dashboard or execution gate consumes that artifact.
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import List

import pandas as pd

log = logging.getLogger("optedge.alpha_decay")
ROOT = Path(__file__).resolve().parent.parent

EVIDENCE_STATUS = "diagnostic_only_exploratory_alpha_decay"
ELIGIBLE_FOR_MODEL_PROMOTION = False
ELIGIBLE_FOR_LIVE_REVIEW = False

FACTOR_COLS = [
    "z_mispricing", "z_iv_rank", "z_sent", "z_fund", "z_insider", "z_macro",
    "z_news", "z_earnings", "z_value", "z_congress", "z_social", "z_analyst",
    "z_uoa", "z_sector_rs", "z_dark_pool", "z_fda", "z_sector_flow",
    "z_short_int", "z_put_call", "z_iv_surface",
]


def _labeled(frame: pd.DataFrame | None = None) -> pd.DataFrame:
    """Return a copy carrying explicit diagnostic-only evidence labels."""
    out = frame.copy() if isinstance(frame, pd.DataFrame) else pd.DataFrame()
    out["evidence_status"] = EVIDENCE_STATUS
    out["eligible_for_model_promotion"] = ELIGIBLE_FOR_MODEL_PROMOTION
    out["eligible_for_live_review"] = ELIGIBLE_FOR_LIVE_REVIEW
    return out


def _safe_corr(x: pd.Series, y: pd.Series) -> float:
    try:
        x, y = pd.Series(x).astype(float), pd.Series(y).astype(float)
        mask = x.notna() & y.notna()
        if mask.sum() < 8:
            return float("nan")
        return float(x[mask].corr(y[mask], method="spearman"))
    except Exception:
        return float("nan")


def compute_alpha_decay(signals_df: pd.DataFrame, horizons: List[int] = None) -> pd.DataFrame:
    """For each factor + horizon, compute IC vs realised return at that horizon."""
    if horizons is None:
        horizons = [1, 3, 7, 14, 30]
    if signals_df is None or signals_df.empty:
        return _labeled()
    # We expect columns: ticker, log_time, factor scores, AND realized_pnl_pct_{h}d
    rows = []
    for h in horizons:
        col = f"realized_pnl_pct_{h}d"
        if col not in signals_df.columns:
            # Some logs may use 'pnl_pct' for a single horizon — try fallback
            if h == 7 and "pnl_pct" in signals_df.columns:
                col = "pnl_pct"
            else:
                continue
        for f in FACTOR_COLS:
            if f not in signals_df.columns:
                continue
            ic = _safe_corr(signals_df[f], signals_df[col])
            n = int((signals_df[f].notna() & signals_df[col].notna()).sum())
            if n >= 8:
                rows.append({"factor": f.replace("z_", ""), "horizon": h, "ic": ic, "n": n})
    if not rows:
        return _labeled()
    return _labeled(pd.DataFrame(rows))


def write_alpha_decay(df: pd.DataFrame) -> Path:
    """Write a labeled diagnostic table to ``data/alpha_decay.parquet``."""
    out = ROOT / "data" / "alpha_decay.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    _labeled(df).to_parquet(out, index=False)
    return out


def load_alpha_decay() -> pd.DataFrame:
    """Load the local diagnostic while enforcing its evidence labels."""
    f = ROOT / "data" / "alpha_decay.parquet"
    if not f.exists():
        return _labeled()
    try:
        return _labeled(pd.read_parquet(f))
    except Exception:
        return _labeled()
