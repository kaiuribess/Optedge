# Purpose: Detect implied-volatility surface anomalies by strike.
"""IV Surface anomaly detection — find strikes where IV deviates from neighbors.

For each ticker × expiry × side (call/put), looks at IV across strikes.
A "normal" smile rises smoothly toward OTM strikes. An ANOMALY is a strike
whose IV sits 2+ std above the local trend — that's unusual demand
(someone bought volatility at that exact strike).

Outputs per ticker:
  - iv_anomaly_max_z: highest absolute deviation across all contracts
  - iv_anomaly_count: how many strikes are >2σ from local trend
  - iv_anomaly_top_strike: which strike has the biggest anomaly
  - iv_anomaly_side: call or put
  - iv_surface_score: signed score (positive = upward IV anomaly = buying pressure)
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

log = logging.getLogger("optedge.iv_surface")


def derive_from_contracts(contracts: pd.DataFrame, min_strikes: int = 6) -> pd.DataFrame:
    """Detect IV-surface anomalies per ticker.

    Strategy: within each (ticker, expiry, side) group, fit a quadratic smile,
    compute residuals, flag strikes where |residual| > 2σ.
    """
    if contracts is None or contracts.empty:
        return pd.DataFrame()
    if not all(c in contracts.columns for c in ["ticker", "expiry", "side", "strike", "iv_market"]):
        return pd.DataFrame()
    c = contracts.copy()
    c = c.dropna(subset=["iv_market", "strike"])
    c = c[c["iv_market"] > 0]
    if c.empty:
        return pd.DataFrame()

    anomaly_records = []
    for (ticker, expiry, side), grp in c.groupby(["ticker", "expiry", "side"]):
        if len(grp) < min_strikes:
            continue
        ivs = grp["iv_market"].values.astype(float)
        strikes = grp["strike"].values.astype(float)
        if len(ivs) < min_strikes:
            continue
        # Fit a quadratic smile: IV = a*K^2 + b*K + c
        try:
            coeffs = np.polyfit(strikes, ivs, deg=2)
            fitted = np.polyval(coeffs, strikes)
            residuals = ivs - fitted
            sigma = float(np.std(residuals))
            if sigma <= 0:
                continue
            z = residuals / sigma
            for i, strike in enumerate(strikes):
                if abs(z[i]) > 2.0:
                    anomaly_records.append(
                        {
                            "ticker": ticker,
                            "expiry": expiry,
                            "side": side,
                            "strike": float(strike),
                            "iv_market": float(ivs[i]),
                            "iv_fitted": float(fitted[i]),
                            "iv_residual": float(residuals[i]),
                            "z": float(z[i]),
                        }
                    )
        except Exception:
            continue

    if not anomaly_records:
        log.info("iv_surface: no anomalies > 2σ detected")
        return pd.DataFrame()

    anom_df = pd.DataFrame(anomaly_records)
    # Per-ticker aggregation: keep the strongest anomaly per ticker
    rows = []
    for ticker, grp in anom_df.groupby("ticker"):
        max_z_idx = grp["z"].abs().idxmax()
        top = grp.loc[max_z_idx]
        # Signed score: positive z + call = bullish demand for upside
        # negative z + call = unusual supply / dump
        # We use the signed z directly (capped)
        score = float(top["z"])
        if top["side"] == "put":
            score = -score  # high IV anomaly on a PUT = hedge demand = bearish
        rows.append(
            {
                "ticker": ticker,
                "iv_anomaly_max_z": round(float(top["z"]), 2),
                "iv_anomaly_count": int(len(grp)),
                "iv_anomaly_top_strike": float(top["strike"]),
                "iv_anomaly_top_side": top["side"],
                "iv_anomaly_top_expiry": top["expiry"],
                "iv_surface_score": round(max(-2.5, min(2.5, score)), 3),
            }
        )
    out = pd.DataFrame(rows)
    if not out.empty:
        log.info(
            "iv_surface: %d tickers with anomalies, top z=%.1f",
            len(out),
            out["iv_anomaly_max_z"].abs().max(),
        )
    return out
