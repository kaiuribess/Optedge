# Purpose: Alpha decay tracker.
"""Alpha decay tracker.

Reads logged signals and their realized forward returns at multiple horizons
(1d, 3d, 7d, 14d, 30d) to estimate how long each signal type retains edge.

If a factor has IC > 0 at 1d but IC ~ 0 by 7d, it's a fast-decay factor; we
should weight short-horizon predictors more aggressively for it.

Outputs a DataFrame: factor, horizon, ic, n.
Saves to data/alpha_decay.parquet for the dashboard.
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import List

import pandas as pd

log = logging.getLogger("optedge.alpha_decay")
ROOT = Path(__file__).resolve().parent.parent

FACTOR_COLS = [
    "z_mispricing", "z_iv_rank", "z_sent", "z_fund", "z_insider", "z_macro",
    "z_news", "z_earnings", "z_value", "z_congress", "z_social", "z_analyst",
    "z_uoa", "z_sector_rs", "z_dark_pool", "z_fda", "z_sector_flow",
    "z_short_int", "z_put_call", "z_iv_surface",
]


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
        return pd.DataFrame()
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
        return pd.DataFrame()
    return pd.DataFrame(rows)


def write_alpha_decay(df: pd.DataFrame) -> Path:
    """Write decay table to data/alpha_decay.parquet."""
    out = ROOT / "data" / "alpha_decay.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    return out


def load_alpha_decay() -> pd.DataFrame:
    f = ROOT / "data" / "alpha_decay.parquet"
    if not f.exists():
        return pd.DataFrame()
    try:
        return pd.read_parquet(f)
    except Exception:
        return pd.DataFrame()
