"""Yield curve PCA factors engine.

Extracts the classic 3 principal components of the US Treasury yield curve:
  - Level   (parallel shift in rates)
  - Slope   (2s10s steepening/flattening)
  - Curvature (belly of the curve)

These factors explain ~99% of curve movement and drive bank/insurer/REIT/
financial-sector returns asymmetrically.

Free, uses FRED API (key required — same key as macro engine).
"""
from __future__ import annotations
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

import sys
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider

log = logging.getLogger("optedge.yield_curve")

# FRED series for the curve. Daily.
CURVE_SERIES = {
    "3m":  "DGS3MO",
    "6m":  "DGS6MO",
    "1y":  "DGS1",
    "2y":  "DGS2",
    "5y":  "DGS5",
    "10y": "DGS10",
    "30y": "DGS30",
}

# Sector buckets that respond to curve factors
LEVEL_BENEFICIARIES = ["XLF", "JPM", "BAC", "WFC", "C", "USB", "PNC",
                       "MET", "PRU", "AFL", "ALL", "TRV", "CB", "AIG"]
SLOPE_STEEP_BENEFICIARIES = ["XLF", "JPM", "BAC", "MS", "GS"]  # Banks like steeper curves
LEVEL_HARMED = ["XLU", "XLRE", "AMT", "PLD", "O", "TLT"]  # Long-duration / dividend / bonds


def _get_fred_key() -> str:
    key = os.environ.get("FRED_API_KEY", "")
    if key:
        return key
    try:
        from keys import FRED_API_KEY
        return FRED_API_KEY
    except Exception:
        return ""


def _fred_series_history(series_id: str, days: int = 90) -> List[Dict]:
    """Fetch daily history for a FRED series."""
    api_key = _get_fred_key()
    if not api_key:
        return []
    cache_key = f"fred_curve:{series_id}"
    cached = data_provider.cache_get(cache_key, max_age_sec=12 * 3600)
    if cached is not None:
        return cached
    today = datetime.utcnow().strftime("%Y-%m-%d")
    start = (datetime.utcnow() - timedelta(days=days * 2)).strftime("%Y-%m-%d")
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id, "api_key": api_key, "file_type": "json",
        "observation_start": start, "observation_end": today,
        "sort_order": "desc", "limit": days,
    }
    try:
        import requests
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 200:
            return []
        obs = r.json().get("observations", [])
        out = []
        for o in obs:
            try:
                v = float(o["value"])
                out.append({"date": o["date"], "value": v})
            except (ValueError, TypeError):
                continue
        data_provider.cache_put(cache_key, out)
        return out
    except Exception as e:
        log.debug("FRED curve %s: %s", series_id, e)
        return []


def _build_curve_panel(days: int = 60) -> pd.DataFrame:
    """Return DataFrame indexed by date, columns = tenor yields."""
    data = {}
    for tenor, sid in CURVE_SERIES.items():
        rows = _fred_series_history(sid, days)
        if rows:
            data[tenor] = {r["date"]: r["value"] for r in rows}
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data).sort_index().tail(days).dropna(how="any")
    return df


def compute_pca_factors() -> Dict:
    """Run PCA on the curve panel. Returns level/slope/curvature changes."""
    df = _build_curve_panel(days=60)
    if df.empty or len(df) < 20:
        return {"level": 0.0, "slope": 0.0, "curvature": 0.0,
                "level_chg_5d": 0.0, "slope_chg_5d": 0.0,
                "curvature_chg_5d": 0.0, "n_obs": len(df)}
    # Compute daily changes
    diffs = df.diff().dropna()
    if diffs.empty:
        return {"level": 0.0, "slope": 0.0, "curvature": 0.0,
                "n_obs": len(df)}
    # PCA: simple eigendecomposition of covariance matrix
    cov = diffs.cov().values
    eigvals, eigvecs = np.linalg.eigh(cov)
    # Sort descending
    order = np.argsort(eigvals)[::-1]
    eigvecs = eigvecs[:, order]
    # Project last 5 daily changes onto top 3 PCs
    recent_5 = diffs.tail(5).values  # shape (5, n_tenors)
    pcs_5d = recent_5.sum(axis=0) @ eigvecs[:, :3]
    return {
        "level": float(df.iloc[-1].mean()),
        "slope": float(df["10y"].iloc[-1] - df["2y"].iloc[-1]),
        "curvature": float(2 * df["5y"].iloc[-1] - df["2y"].iloc[-1] - df["10y"].iloc[-1]),
        "level_chg_5d": float(pcs_5d[0]),
        "slope_chg_5d": float(pcs_5d[1]),
        "curvature_chg_5d": float(pcs_5d[2]),
        "n_obs": len(df),
        "ten_year": float(df["10y"].iloc[-1]),
        "two_year": float(df["2y"].iloc[-1]),
    }


def run(universe: List[str]) -> pd.DataFrame:
    """Broadcast curve factor scores to affected ticker buckets."""
    state = compute_pca_factors()
    if state.get("n_obs", 0) < 20:
        log.info("yield_curve PCA: insufficient FRED data (n=%d)", state.get("n_obs", 0))
        return pd.DataFrame()
    log.info("yield_curve PCA: 10y=%.2f 2s10s=%+.0fbp curve_chg_5d level=%+.3f slope=%+.3f",
             state["ten_year"], state["slope"] * 100,
             state["level_chg_5d"], state["slope_chg_5d"])

    rows = []
    # Rising yields (positive level change) help banks/insurers (re-pricing assets)
    level_score_pos = max(-1.0, min(1.0, state["level_chg_5d"] * 5))
    # Steepening (positive slope change) helps banks (NIM expansion)
    slope_score_pos = max(-1.0, min(1.0, state["slope_chg_5d"] * 5))
    for tk in LEVEL_BENEFICIARIES:
        rows.append({
            "ticker": tk,
            "curve_score": (level_score_pos + slope_score_pos) / 2,
            "curve_factor": "level+slope",
        })
    # Long-duration / bond proxies get the opposite sign of level
    for tk in LEVEL_HARMED:
        if any(r["ticker"] == tk for r in rows):
            continue
        rows.append({
            "ticker": tk,
            "curve_score": -level_score_pos,
            "curve_factor": "duration_hurt",
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).reset_index(drop=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(compute_pca_factors())
