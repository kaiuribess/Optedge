# Purpose: Verify asset-aware, after-cost prediction calibration.
from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.calibration import calibration_report, diagnostic_summary


def test_calibration_prefers_after_slippage_returns():
    predicted = np.linspace(-0.16, 0.16, 48)
    frame = pd.DataFrame({
        "asset": "option",
        "pred_option_return_pct": predicted,
        "pnl_pct": predicted + 0.50,
        "pnl_pct_after_slippage": predicted,
    })

    report = calibration_report(frame, asset="option", n_bins=8)

    assert report["overall"]["return_basis"] == "pnl_pct_after_slippage"
    assert report["overall"]["calibration_mae"] < 0.001


def test_mixed_asset_calibration_uses_the_matching_prediction_column():
    option_pred = np.linspace(-0.20, 0.20, 32)
    share_pred = np.linspace(-0.08, 0.08, 32)
    option_rows = pd.DataFrame({
        "asset": "option",
        "pred_option_return_pct": option_pred,
        "pred_stock_return_pct": np.nan,
        "pnl_pct_after_slippage": option_pred,
    })
    share_rows = pd.DataFrame({
        "asset": "share",
        "pred_option_return_pct": np.nan,
        "pred_stock_return_pct": share_pred,
        "pnl_pct_after_slippage": share_pred,
    })

    report = diagnostic_summary(pd.concat([option_rows, share_rows], ignore_index=True))

    assert "option" in report
    assert "share" in report
    assert report["option"]["overall"]["n_signals"] == 32
    assert report["share"]["overall"]["n_signals"] == 32
    assert report["overall"]["overall"]["n_signals"] == 64
    assert report["overall"]["overall"]["decision_eligible"] is False
    assert report["overall"]["overall"]["verdict_scope"] == "descriptive_only"
    assert report["primary"]["scope"] == "per_asset"


def test_asset_aliases_are_normalized_before_prediction_selection():
    predicted = np.linspace(-0.12, 0.12, 32)
    frame = pd.DataFrame({
        "asset": [" Stocks "] * 16 + ["EQUITIES"] * 16,
        "pred_stock_return_pct": predicted,
        # A tempting but deliberately wrong option prediction must be ignored.
        "pred_option_return_pct": predicted[::-1] + 5.0,
        "pnl_pct_after_slippage": predicted,
    })

    report = calibration_report(frame, asset="shares", n_bins=8)

    assert report["overall"]["asset_scope"] == "share"
    assert report["overall"]["assets_evaluated"] == ["share"]
    assert report["overall"]["n_signals"] == 32
    assert report["overall"]["calibration_mae"] < 0.001


def test_rows_missing_asset_specific_prediction_are_excluded_without_fallback():
    option_pred = np.linspace(-0.15, 0.15, 24)
    share_pred = np.linspace(-0.08, 0.08, 24)
    valid_options = pd.DataFrame({
        "asset": "options",
        "pred_option_return_pct": option_pred,
        "pred_stock_return_pct": np.nan,
        "pnl_pct_after_slippage": option_pred,
    })
    invalid_options = pd.DataFrame({
        "asset": "option",
        "pred_option_return_pct": np.nan,
        "pred_stock_return_pct": np.linspace(0.50, 0.60, 6),
        "pnl_pct_after_slippage": np.linspace(0.50, 0.60, 6),
    })
    valid_shares = pd.DataFrame({
        "asset": "stock",
        "pred_option_return_pct": np.nan,
        "pred_stock_return_pct": share_pred,
        "pnl_pct_after_slippage": share_pred,
    })
    invalid_shares = pd.DataFrame({
        "asset": "shares",
        "pred_option_return_pct": np.linspace(-0.60, -0.50, 5),
        "pred_stock_return_pct": np.nan,
        "pnl_pct_after_slippage": np.linspace(-0.60, -0.50, 5),
    })
    frame = pd.concat(
        [valid_options, invalid_options, valid_shares, invalid_shares],
        ignore_index=True,
    )

    option_report = calibration_report(frame, asset="option", n_bins=8)
    share_report = calibration_report(frame, asset="share", n_bins=8)
    pooled_report = calibration_report(frame, n_bins=8)

    assert option_report["overall"]["n_signals"] == 24
    assert option_report["overall"]["excluded_missing_prediction"] == 6
    assert share_report["overall"]["n_signals"] == 24
    assert share_report["overall"]["excluded_missing_prediction"] == 5
    assert pooled_report["overall"]["n_signals"] == 48
    assert pooled_report["overall"]["excluded_missing_prediction"] == 11


def test_futures_use_stock_prediction_and_never_option_prediction():
    futures_pred = np.linspace(-0.10, 0.10, 24)
    valid = pd.DataFrame({
        "asset": "future",
        "pred_stock_return_pct": futures_pred,
        "pred_option_return_pct": futures_pred[::-1] + 2.0,
        "pnl_pct_after_slippage": futures_pred,
    })
    wrong_column_only = pd.DataFrame({
        "asset": "futures",
        "pred_stock_return_pct": np.nan,
        "pred_option_return_pct": np.linspace(0.40, 0.50, 4),
        "pnl_pct_after_slippage": np.linspace(0.40, 0.50, 4),
    })

    report = calibration_report(
        pd.concat([valid, wrong_column_only], ignore_index=True),
        asset="futures",
        n_bins=8,
    )

    assert report["overall"]["asset_scope"] == "futures"
    assert report["overall"]["n_signals"] == 24
    assert report["overall"]["excluded_missing_prediction"] == 4
    assert report["overall"]["calibration_mae"] < 0.001


def test_pooled_simpsons_paradox_cannot_claim_well_calibrated():
    # Within each asset the predictor is perfectly reversed, but the large gap
    # between asset groups creates an apparently strong positive pooled rank.
    option_pred = np.linspace(-0.22, -0.18, 32)
    share_pred = np.linspace(0.18, 0.22, 32)
    option_rows = pd.DataFrame({
        "asset": "option",
        "pred_option_return_pct": option_pred,
        "pred_stock_return_pct": np.nan,
        "pnl_pct_after_slippage": option_pred[::-1],
    })
    share_rows = pd.DataFrame({
        "asset": "share",
        "pred_option_return_pct": np.nan,
        "pred_stock_return_pct": share_pred,
        "pnl_pct_after_slippage": share_pred[::-1],
    })

    report = diagnostic_summary(pd.concat([option_rows, share_rows], ignore_index=True))

    pooled = report["overall"]["overall"]
    assert pooled["rank_correlation"] > 0.20
    assert pooled["calibration_mae"] < 0.10
    assert pooled["verdict"].startswith("descriptive only:")
    assert pooled["decision_eligible"] is False
    assert not report["option"]["overall"]["verdict"].startswith("well-calibrated:")
    assert not report["share"]["overall"]["verdict"].startswith("well-calibrated:")
    assert report["primary"]["all_assets_well_calibrated"] is False
    assert report["primary"]["verdict"].startswith("not validated")


def test_unknown_or_missing_assets_cannot_borrow_any_prediction_column():
    frame = pd.DataFrame({
        "asset": ["crypto"] * 32,
        "pred_option_return_pct": np.linspace(-0.1, 0.1, 32),
        "pred_stock_return_pct": np.linspace(-0.1, 0.1, 32),
        "pnl_pct_after_slippage": np.linspace(-0.1, 0.1, 32),
    })

    report = calibration_report(frame)

    assert report["overall"]["reason"] == "insufficient_samples"
    assert report["overall"]["n"] == 0
    assert report["overall"]["excluded_unsupported_asset"] == 32
