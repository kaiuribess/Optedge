# Purpose: Yield curve PCA factors engine.
"""Yield curve PCA factors engine.

Extracts the classic principal components of the US Treasury yield curve:
  - Level: parallel movement in rates
  - Slope: 2s10s steepening/flattening
  - Curvature: belly of the curve

Free source stack:
  - FRED API when configured
  - Keyless FRED public CSV fallback
  - Official Treasury XML yield-curve feed when FRED is thin/unavailable
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List
from xml.etree import ElementTree as ET

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider
from engines.fred_public import fred_csv_history

log = logging.getLogger("optedge.yield_curve")

CURVE_SERIES = {
    "3m": "DGS3MO",
    "6m": "DGS6MO",
    "1y": "DGS1",
    "2y": "DGS2",
    "5y": "DGS5",
    "10y": "DGS10",
    "30y": "DGS30",
}

TREASURY_XML_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
)
TREASURY_XML_FIELDS = {
    "3m": "BC_3MONTH",
    "6m": "BC_6MONTH",
    "1y": "BC_1YEAR",
    "2y": "BC_2YEAR",
    "5y": "BC_5YEAR",
    "10y": "BC_10YEAR",
    "30y": "BC_30YEAR",
}
TREASURY_D_NS = "{http://schemas.microsoft.com/ado/2007/08/dataservices}"
TREASURY_M_NS = "{http://schemas.microsoft.com/ado/2007/08/dataservices/metadata}"

LEVEL_BENEFICIARIES = [
    "XLF", "JPM", "BAC", "WFC", "C", "USB", "PNC",
    "MET", "PRU", "AFL", "ALL", "TRV", "CB", "AIG",
]
SLOPE_STEEP_BENEFICIARIES = ["XLF", "JPM", "BAC", "MS", "GS"]
LEVEL_HARMED = ["XLU", "XLRE", "AMT", "PLD", "O", "TLT"]


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
        return fred_csv_history(series_id, days=days, cache_hours=12)
    cache_key = f"fred_curve:{series_id}"
    cached = data_provider.cache_get(cache_key, max_age_sec=12 * 3600)
    if cached is not None:
        return cached
    today = datetime.utcnow().strftime("%Y-%m-%d")
    start = (datetime.utcnow() - timedelta(days=days * 2)).strftime("%Y-%m-%d")
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": start,
        "observation_end": today,
        "sort_order": "desc",
        "limit": days,
    }
    try:
        import requests

        response = requests.get(url, params=params, timeout=15)
        if response.status_code != 200:
            return []
        obs = response.json().get("observations", [])
        out = []
        for row in obs:
            try:
                out.append({"date": row["date"], "value": float(row["value"])})
            except (ValueError, TypeError):
                continue
        data_provider.cache_put(cache_key, out)
        return out
    except Exception as exc:
        log.debug("FRED curve %s: %s", series_id, exc)
        return fred_csv_history(series_id, days=days, cache_hours=12)


def _build_fred_curve_panel(days: int = 60) -> pd.DataFrame:
    """Return a FRED-backed DataFrame indexed by date, columns = tenor yields."""
    data = {}
    for tenor, series_id in CURVE_SERIES.items():
        rows = _fred_series_history(series_id, days)
        if rows:
            data[tenor] = {row["date"]: row["value"] for row in rows}
    if not data:
        return pd.DataFrame()
    return pd.DataFrame(data).sort_index().tail(days).dropna(how="any")


def _parse_treasury_xml_curve(xml_text: str) -> List[Dict[str, float | str]]:
    """Parse the Treasury OData XML daily par yield-curve feed."""
    root = ET.fromstring(xml_text)
    rows: List[Dict[str, float | str]] = []
    for props in root.findall(f".//{TREASURY_M_NS}properties"):
        date_text = props.findtext(f"{TREASURY_D_NS}NEW_DATE")
        if not date_text:
            continue
        row: Dict[str, float | str] = {"date": date_text[:10]}
        for tenor, field in TREASURY_XML_FIELDS.items():
            value_text = props.findtext(f"{TREASURY_D_NS}{field}")
            try:
                row[tenor] = float(value_text)
            except (TypeError, ValueError):
                row = {}
                break
        if row:
            rows.append(row)
    return rows


def _treasury_year_curve_rows(year: int) -> List[Dict[str, float | str]]:
    cache_key = f"treasury_yield_curve_xml:{year}"
    cached = data_provider.cache_get(cache_key, max_age_sec=12 * 3600)
    if cached is not None:
        return cached
    try:
        session = data_provider.get_session()
        response = session.get(
            TREASURY_XML_URL,
            params={"data": "daily_treasury_yield_curve", "field_tdr_date_value": str(year)},
            timeout=20,
        )
        if response.status_code != 200:
            log.debug("Treasury yield XML %s -> %s", year, response.status_code)
            data_provider.cache_put(cache_key, [])
            return []
        rows = _parse_treasury_xml_curve(response.text)
        data_provider.cache_put(cache_key, rows)
        return rows
    except Exception as exc:
        log.debug("Treasury yield XML %s failed: %s", year, exc)
        return []


def _build_treasury_curve_panel(days: int = 60) -> pd.DataFrame:
    """Return official Treasury XML curve panel when FRED is unavailable."""
    year = datetime.now(timezone.utc).year
    rows: List[Dict[str, float | str]] = []
    for offset in range(3):
        rows.extend(_treasury_year_curve_rows(year - offset))
        if len(rows) >= days:
            break
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).drop_duplicates(subset=["date"]).set_index("date").sort_index()
    return df[list(CURVE_SERIES.keys())].tail(days).dropna(how="any")


def _build_curve_panel(days: int = 60) -> tuple[pd.DataFrame, str]:
    """Return curve panel plus source label."""
    fred_df = _build_fred_curve_panel(days)
    if len(fred_df) >= 20:
        return fred_df, "fred"
    treasury_df = _build_treasury_curve_panel(days)
    if len(treasury_df) >= 20:
        return treasury_df, "treasury_xml"
    return fred_df if not fred_df.empty else treasury_df, "insufficient"


def compute_pca_factors() -> Dict:
    """Run PCA on the curve panel. Returns level/slope/curvature changes."""
    df, source = _build_curve_panel(days=60)
    if df.empty or len(df) < 20:
        return {
            "level": 0.0,
            "slope": 0.0,
            "curvature": 0.0,
            "level_chg_5d": 0.0,
            "slope_chg_5d": 0.0,
            "curvature_chg_5d": 0.0,
            "n_obs": len(df),
            "curve_source": source,
        }

    diffs = df.diff().dropna()
    if diffs.empty:
        return {
            "level": 0.0,
            "slope": 0.0,
            "curvature": 0.0,
            "n_obs": len(df),
            "curve_source": source,
        }

    cov = diffs.cov().values
    eigvals, eigvecs = np.linalg.eigh(cov)
    order = np.argsort(eigvals)[::-1]
    eigvecs = eigvecs[:, order]
    recent_5 = diffs.tail(5).values
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
        "curve_source": source,
    }


def run(universe: List[str]) -> pd.DataFrame:
    """Broadcast curve factor scores to affected ticker buckets."""
    state = compute_pca_factors()
    if state.get("n_obs", 0) < 20:
        log.info("yield_curve PCA: insufficient data (n=%d)", state.get("n_obs", 0))
        return pd.DataFrame()
    log.info(
        "yield_curve PCA: source=%s 10y=%.2f 2s10s=%+.0fbp curve_chg_5d level=%+.3f slope=%+.3f",
        state.get("curve_source", "unknown"),
        state["ten_year"],
        state["slope"] * 100,
        state["level_chg_5d"],
        state["slope_chg_5d"],
    )

    rows = []
    level_score_pos = max(-1.0, min(1.0, state["level_chg_5d"] * 5))
    slope_score_pos = max(-1.0, min(1.0, state["slope_chg_5d"] * 5))
    for ticker in LEVEL_BENEFICIARIES:
        rows.append({
            "ticker": ticker,
            "curve_score": (level_score_pos + slope_score_pos) / 2,
            "curve_factor": "level+slope",
            "curve_source": state.get("curve_source", "unknown"),
        })
    for ticker in LEVEL_HARMED:
        if any(row["ticker"] == ticker for row in rows):
            continue
        rows.append({
            "ticker": ticker,
            "curve_score": -level_score_pos,
            "curve_factor": "duration_hurt",
            "curve_source": state.get("curve_source", "unknown"),
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).reset_index(drop=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(compute_pca_factors())
