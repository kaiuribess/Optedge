# Purpose: Asset-isolated predicted-vs-realized calibration tracking.
"""Asset-isolated predicted-vs-realized calibration tracking.

Calibration is meaningful only when a realized outcome is compared with the
prediction produced for the same asset family.  Options therefore use
``pred_option_return_pct`` while shares and futures use
``pred_stock_return_pct``.  Missing asset-specific predictions are excluded;
the other asset family's prediction is never used as a fallback.

Mixed-asset statistics remain available for descriptive diagnostics, but they
cannot produce a calibration verdict.  This prevents a pooled relationship
between asset groups from hiding poor calibration inside every group (a form
of Simpson's paradox).
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

log = logging.getLogger("optedge.calibration")

_ASSET_ALIASES = {
    "option": "option",
    "options": "option",
    "share": "share",
    "shares": "share",
    "stock": "share",
    "stocks": "share",
    "equity": "share",
    "equities": "share",
    "future": "futures",
    "futures": "futures",
}
_PREDICTION_COLUMN = {
    "option": "pred_option_return_pct",
    "share": "pred_stock_return_pct",
    "futures": "pred_stock_return_pct",
}
_SUPPORTED_ASSETS = tuple(_PREDICTION_COLUMN)


def _normalize_asset(value: Any) -> str | None:
    """Return a canonical supported asset name, or ``None`` when unsupported."""
    if value is None or pd.isna(value):
        return None
    return _ASSET_ALIASES.get(str(value).strip().lower())


def _empty_report(reason: str, **details: Any) -> dict[str, Any]:
    return {
        "bins": pd.DataFrame(),
        "overall": {
            "reason": reason,
            "decision_eligible": False,
            **details,
        },
    }


def calibration_report(
    forward_signals: pd.DataFrame,
    asset: str | None = None,
    n_bins: int = 8,
) -> dict[str, Any]:
    """Bin predictions and compute realized statistics without crossing assets.

    When ``asset`` is supplied, supported aliases are normalized and only that
    asset is evaluated.  Without ``asset``, each row is mapped to the prediction
    column for its own normalized asset.  A report containing more than one
    asset is explicitly descriptive-only; callers must use per-asset reports
    for a decision.
    """
    if forward_signals is None or forward_signals.empty:
        return {"bins": pd.DataFrame(), "overall": {}}
    if n_bins < 1:
        return _empty_report("invalid_bin_count", n_bins=n_bins)

    requested_asset = _normalize_asset(asset) if asset is not None else None
    if asset is not None and requested_asset is None:
        return _empty_report(
            "unsupported_asset",
            requested_asset=str(asset),
            supported_assets=list(_SUPPORTED_ASSETS),
        )

    df = forward_signals.copy()
    n_input = int(len(df))
    if "asset" in df.columns:
        df["_calibration_asset"] = df["asset"].map(_normalize_asset)
    elif requested_asset is not None:
        df["_calibration_asset"] = requested_asset
    else:
        return _empty_report("missing_asset", n_input=n_input)

    unsupported_count = int(df["_calibration_asset"].isna().sum())
    if requested_asset is not None:
        df = df[df["_calibration_asset"] == requested_asset].copy()
    else:
        df = df[df["_calibration_asset"].notna()].copy()

    return_col = (
        "pnl_pct_after_slippage"
        if "pnl_pct_after_slippage" in df.columns
        else "pnl_pct" if "pnl_pct" in df.columns else None
    )
    scope = requested_asset or "mixed"
    if return_col is None:
        return _empty_report(
            "missing_columns",
            asset_scope=scope,
            n_input=n_input,
            excluded_unsupported_asset=unsupported_count,
        )

    # Build one prediction series by explicit row-to-column routing.  A missing
    # value remains missing; no prediction from another asset family can leak in.
    prediction = pd.Series(float("nan"), index=df.index, dtype="float64")
    for canonical_asset, prediction_col in _PREDICTION_COLUMN.items():
        if prediction_col not in df.columns:
            continue
        mask = df["_calibration_asset"].eq(canonical_asset)
        prediction.loc[mask] = pd.to_numeric(
            df.loc[mask, prediction_col], errors="coerce"
        )
    df["_predicted_return_pct"] = prediction
    df[return_col] = pd.to_numeric(df[return_col], errors="coerce")

    missing_prediction_count = int(df["_predicted_return_pct"].isna().sum())
    missing_return_count = int(df[return_col].isna().sum())
    df = df.dropna(subset=["_predicted_return_pct", return_col]).copy()
    assets_evaluated = sorted(df["_calibration_asset"].unique().tolist())
    mixed_scope = len(assets_evaluated) > 1
    report_scope = assets_evaluated[0] if len(assets_evaluated) == 1 else "mixed"

    common_details = {
        "asset_scope": report_scope,
        "assets_evaluated": assets_evaluated,
        "n_input": n_input,
        "n_eligible": int(len(df)),
        "excluded_missing_prediction": missing_prediction_count,
        "excluded_missing_return": missing_return_count,
        "excluded_unsupported_asset": unsupported_count,
        "return_basis": return_col,
    }
    if len(df) < n_bins * 3:
        return _empty_report(
            "insufficient_samples",
            n=int(len(df)),
            **common_details,
        )

    try:
        df["pred_bin"] = pd.qcut(
            df["_predicted_return_pct"], n_bins, duplicates="drop"
        )
    except (TypeError, ValueError):
        return _empty_report("binning_failed", **common_details)

    rows = []
    for prediction_bin, subset in df.groupby("pred_bin", observed=True):
        if subset.empty:
            continue
        pred_mean = float(subset["_predicted_return_pct"].mean())
        realized_mean = float(subset[return_col].mean())
        rows.append(
            {
                "bin": str(prediction_bin),
                "n": int(len(subset)),
                "pred_mean": round(pred_mean, 4),
                "realized_mean": round(realized_mean, 4),
                "realized_std": round(
                    float(subset[return_col].std()) if len(subset) > 1 else 0.0,
                    4,
                ),
                "bias": round(realized_mean - pred_mean, 4),
                "win_rate": round(float((subset[return_col] > 0).mean()), 4),
            }
        )
    bins_df = pd.DataFrame(rows)
    if bins_df.empty:
        return _empty_report("no_bins", **common_details)

    mae = float(bins_df["bias"].abs().mean())
    avg_bias = float(bins_df["bias"].mean())
    try:
        rank_corr_value = df["_predicted_return_pct"].rank().corr(df[return_col].rank())
        rank_corr = float(rank_corr_value) if pd.notna(rank_corr_value) else None
    except (TypeError, ValueError):
        rank_corr = None

    if mixed_scope:
        verdict = (
            "descriptive only: pooled mixed-asset calibration cannot validate any "
            "asset; use the per-asset verdicts"
        )
        verdict_scope = "descriptive_only"
        decision_eligible = False
    else:
        verdict = _verdict(mae, avg_bias, rank_corr)
        verdict_scope = "per_asset"
        decision_eligible = True

    overall = {
        "n_signals": int(len(df)),
        "n_bins": int(len(bins_df)),
        "calibration_mae": mae,
        "avg_bias": avg_bias,
        "rank_correlation": rank_corr,
        "verdict": verdict,
        "verdict_scope": verdict_scope,
        "decision_eligible": decision_eligible,
        **common_details,
    }
    return {"bins": bins_df, "overall": overall}


def _verdict(mae: float, avg_bias: float, rank_corr: float | None) -> str:
    """Plain-English summary of single-asset calibration quality."""
    correlation = rank_corr if rank_corr is not None else 0.0
    if correlation > 0.2 and mae < 0.10:
        return "well-calibrated: predicted ranks reflect realized P&L"
    if correlation > 0.10 and abs(avg_bias) > 0.15:
        if avg_bias > 0:
            return "biased low: predictor under-shoots; realized is bigger than predicted"
        return "biased high: predictor over-shoots; realized is smaller than predicted"
    if correlation < 0.05:
        return (
            "uncorrelated: predicted return has no relationship to realized; "
            "the predictor needs more independent evidence"
        )
    if mae > 0.20:
        return "high error: large per-bin bias; predictor unreliable for sizing"
    return "moderate: predictions weakly informative; gather more independent samples"


def diagnostic_summary(forward_signals: pd.DataFrame) -> dict[str, Any]:
    """Return descriptive pooled statistics plus primary per-asset verdicts."""
    pooled = calibration_report(forward_signals)
    output: dict[str, Any] = {"overall": pooled}
    reports: dict[str, dict[str, Any]] = {}

    if forward_signals is not None and not forward_signals.empty and "asset" in forward_signals:
        normalized_assets = forward_signals["asset"].map(_normalize_asset)
        for canonical_asset in _SUPPORTED_ASSETS:
            if not normalized_assets.eq(canonical_asset).any():
                continue
            report = calibration_report(forward_signals, asset=canonical_asset)
            reports[canonical_asset] = report
            # Preserve the historical direct keys while also exposing a clear map.
            output[canonical_asset] = report

    asset_verdicts: dict[str, str] = {}
    all_well_calibrated = bool(reports)
    evaluated_assets: list[str] = []
    for canonical_asset, report in reports.items():
        overall = report.get("overall", {})
        verdict = overall.get("verdict")
        if verdict:
            evaluated_assets.append(canonical_asset)
            asset_verdicts[canonical_asset] = str(verdict)
            all_well_calibrated = all_well_calibrated and str(verdict).startswith(
                "well-calibrated:"
            )
        else:
            reason = str(overall.get("reason", "unavailable"))
            asset_verdicts[canonical_asset] = reason
            all_well_calibrated = False

    if not reports:
        primary_verdict = "unavailable: no supported assets to calibrate"
    elif all_well_calibrated:
        primary_verdict = "well-calibrated across every evaluated asset"
    else:
        primary_verdict = (
            "not validated across assets: rely only on the individual per-asset reports"
        )

    output["by_asset"] = reports
    output["primary"] = {
        "scope": "per_asset",
        "verdict": primary_verdict,
        "asset_verdicts": asset_verdicts,
        "assets_present": list(reports),
        "assets_evaluated": evaluated_assets,
        "all_assets_well_calibrated": all_well_calibrated,
        "decision_eligible": bool(evaluated_assets),
    }
    return output
