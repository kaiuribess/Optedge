"""Predicted vs realized calibration tracking.

For each logged signal, we now have BOTH:
  - pred_option_return_pct (what the predictor said at log time)
  - pnl_pct (realized via forward.py re-pricing)

This module bins predictions into deciles and computes the realized mean per bin.
A WELL-CALIBRATED predictor has:
  - mean_realized ≈ mean_predicted within each bin (low bias)
  - tight std per bin (consistent)

When calibration is OFF (e.g., +5% predicted → +1.5% realized) it tells us
either the Lasso coefs need scaling, or some factor weights are wrong.

The output gets surfaced on the dashboard as a calibration panel so user can
see WHEN to trust pred_return and when to discount it.
"""
from __future__ import annotations
import logging
from typing import Dict, Any, Optional

import numpy as np
import pandas as pd

log = logging.getLogger("optedge.calibration")


def calibration_report(forward_signals: pd.DataFrame, asset: Optional[str] = None,
                        n_bins: int = 8) -> Dict[str, Any]:
    """Bin predicted return into deciles, compute realized stats per bin."""
    if forward_signals is None or forward_signals.empty:
        return {"bins": pd.DataFrame(), "overall": {}}

    df = forward_signals.copy()
    if asset:
        df = df[df["asset"] == asset]
    needed = {"pnl_pct"}
    pred_col = None
    for cand in ("pred_option_return_pct", "pred_stock_return_pct"):
        if cand in df.columns:
            pred_col = cand
            break
    if pred_col is None or not needed.issubset(df.columns):
        return {"bins": pd.DataFrame(), "overall": {"reason": "missing_columns"}}

    df = df.dropna(subset=[pred_col, "pnl_pct"])
    if len(df) < n_bins * 3:        # need enough rows per bin
        return {"bins": pd.DataFrame(), "overall": {"reason": "insufficient_samples", "n": len(df)}}

    try:
        df["pred_bin"] = pd.qcut(df[pred_col], n_bins, duplicates="drop")
    except Exception:
        return {"bins": pd.DataFrame(), "overall": {"reason": "binning_failed"}}

    rows = []
    for b, sub in df.groupby("pred_bin", observed=True):
        if sub.empty:
            continue
        pred_mean = float(sub[pred_col].mean())
        realized_mean = float(sub["pnl_pct"].mean())
        rows.append({
            "bin": str(b),
            "n": int(len(sub)),
            "pred_mean": round(pred_mean, 4),
            "realized_mean": round(realized_mean, 4),
            "realized_std": round(float(sub["pnl_pct"].std()) if len(sub) > 1 else 0.0, 4),
            "bias": round(realized_mean - pred_mean, 4),         # >0 = predictor under-shooting
            "win_rate": round(float((sub["pnl_pct"] > 0).mean()), 4),
        })
    bins_df = pd.DataFrame(rows)

    # Overall calibration error: avg |bias| across bins
    if not bins_df.empty:
        mae = float(bins_df["bias"].abs().mean())
        # Sign of overall systematic bias
        avg_bias = float(bins_df["bias"].mean())
        # Correlation: how well do predicted ranks correlate with realized ranks?
        try:
            rank_corr = float(df[pred_col].rank().corr(df["pnl_pct"].rank()))
        except Exception:
            rank_corr = None
        overall = {
            "n_signals": int(len(df)),
            "n_bins": int(len(bins_df)),
            "calibration_mae": mae,
            "avg_bias": avg_bias,
            "rank_correlation": rank_corr,
            "verdict": _verdict(mae, avg_bias, rank_corr),
        }
    else:
        overall = {"reason": "no_bins"}
    return {"bins": bins_df, "overall": overall}


def _verdict(mae: float, avg_bias: float, rank_corr: Optional[float]) -> str:
    """Plain-English summary of calibration quality."""
    rc = rank_corr if rank_corr is not None else 0
    if rc > 0.2 and mae < 0.10:
        return "well-calibrated: predicted ranks reflect realized P&L"
    if rc > 0.10 and abs(avg_bias) > 0.15:
        if avg_bias > 0:
            return "biased low: predictor under-shoots; realized is bigger than predicted"
        return "biased high: predictor over-shoots; realized is smaller than predicted"
    if rc < 0.05:
        return "uncorrelated: predicted return has no relationship to realized — Lasso needs more data or weights are off"
    if mae > 0.20:
        return "high error: large per-bin bias; predictor unreliable for sizing"
    return "moderate: predictions weakly informative; refit will improve with more samples"


def diagnostic_summary(forward_signals: pd.DataFrame) -> Dict[str, Any]:
    """Multi-asset calibration: separate reports for options, shares, futures."""
    out = {"overall": calibration_report(forward_signals)}
    if "asset" in forward_signals.columns:
        for asset in ("option", "shares", "futures"):
            sub = forward_signals[forward_signals["asset"] == asset]
            if len(sub) >= 20:
                out[asset] = calibration_report(sub, asset=asset)
    return out
