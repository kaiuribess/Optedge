# Purpose: Investment-grade / High-yield credit spread engine.
"""Investment-grade / High-yield credit spread engine.

Tracks the spread between HY (high-yield/junk) and IG (investment-grade) bonds.
Widening spreads signal credit stress and predict equity weakness — particularly
in cyclicals, banks, and small caps.

Uses FRED's BAMLH0A0HYM2 (HY OAS) and BAMLC0A0CM (IG OAS). The DIVERGENCE
between them (HY-IG spread) is the cleaner stress indicator than either alone.

Free. Uses the FRED API when a key is configured and the public FRED CSV
endpoint as a keyless fallback.
"""
from __future__ import annotations
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List

import pandas as pd

import sys
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider
from engines.fred_public import fred_csv_history

log = logging.getLogger("optedge.credit_spread")

# Tickers most sensitive to credit stress
CYCLICAL_BUCKET = ["XLF", "XLI", "XLY", "XHB", "XRT", "IWM"]
SMALL_CAP_BUCKET = ["IWM", "IJR", "SLY"]
LEVERAGED_NAMES = ["KMI", "WMB", "ET", "WBA", "F", "GM", "CCL", "RCL", "NCLH",
                   "DAL", "AAL", "UAL", "LUV"]


def _get_fred_key() -> str:
    key = os.environ.get("FRED_API_KEY", "")
    if key:
        return key
    try:
        from keys import FRED_API_KEY
        return FRED_API_KEY
    except Exception:
        return ""


def _fred_history(series_id: str, days: int = 30) -> List[Dict]:
    key = _get_fred_key()
    if not key:
        return fred_csv_history(series_id, days=days, cache_hours=12)
    cache_key = f"fred_credit:{series_id}:{days}"
    cached = data_provider.cache_get(cache_key, max_age_sec=12 * 3600)
    if cached is not None:
        return cached
    today = datetime.utcnow().strftime("%Y-%m-%d")
    start = (datetime.utcnow() - timedelta(days=days * 2)).strftime("%Y-%m-%d")
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id, "api_key": key, "file_type": "json",
        "observation_start": start, "observation_end": today,
        "sort_order": "desc", "limit": days + 5,
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
            except Exception:
                continue
        data_provider.cache_put(cache_key, out)
        return out
    except Exception as e:
        log.debug("FRED credit %s: %s", series_id, e)
        return fred_csv_history(series_id, days=days, cache_hours=12)


def compute_credit_state() -> Dict:
    hy = _fred_history("BAMLH0A0HYM2", days=30)  # ICE BofA HY OAS (PERCENT)
    ig = _fred_history("BAMLC0A0CM", days=30)    # ICE BofA IG OAS (PERCENT)
    if not hy or not ig:
        return {"hy_oas": None, "ig_oas": None, "hy_ig_spread": None,
                "hy_chg_5d": 0.0, "ig_chg_5d": 0.0, "spread_chg_5d": 0.0,
                "stress_score": 0.0}
    # v20.1 BUG FIX: FRED BAMLH0A0HYM2 / BAMLC0A0CM are in PERCENT (e.g.
    # 2.95 = 2.95% = 295bp). We store in basis points throughout.
    hy_latest_bp = hy[0]["value"] * 100
    ig_latest_bp = ig[0]["value"] * 100
    spread_bp = hy_latest_bp - ig_latest_bp
    if len(hy) >= 6 and len(ig) >= 6:
        hy_5d_ago_bp = hy[5]["value"] * 100
        ig_5d_ago_bp = ig[5]["value"] * 100
        hy_chg_bp = hy_latest_bp - hy_5d_ago_bp
        ig_chg_bp = ig_latest_bp - ig_5d_ago_bp
        spread_chg_bp = (hy_latest_bp - ig_latest_bp) - (hy_5d_ago_bp - ig_5d_ago_bp)
    else:
        hy_chg_bp = ig_chg_bp = spread_chg_bp = 0.0
    # Stress score: 50bp widening of HY-IG spread over 5 days = -1.0
    stress = -max(-1.0, min(1.0, spread_chg_bp / 50.0))
    return {
        "hy_oas": hy_latest_bp, "ig_oas": ig_latest_bp, "hy_ig_spread": spread_bp,
        "hy_chg_5d": hy_chg_bp, "ig_chg_5d": ig_chg_bp,
        "spread_chg_5d": spread_chg_bp, "stress_score": stress,
    }


def run(universe: List[str]) -> pd.DataFrame:
    state = compute_credit_state()
    if state["hy_oas"] is None:
        log.info("credit_spread: no FRED data (FRED key missing?)")
        return pd.DataFrame()
    stress = state["stress_score"]
    log.info("credit spread: HY=%.0fbp IG=%.0fbp spread=%.0fbp 5d=%+0.1fbp stress=%+.2f",
             state["hy_oas"], state["ig_oas"],
             state["hy_ig_spread"], state["spread_chg_5d"], stress)
    rows = []
    affected = set(CYCLICAL_BUCKET + SMALL_CAP_BUCKET + LEVERAGED_NAMES)
    for tk in affected:
        rows.append({
            "ticker": tk,
            "credit_score": stress,
            "credit_hy_oas": state["hy_oas"],
            "credit_spread_chg_5d": state["spread_chg_5d"],
        })
    return pd.DataFrame(rows)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(compute_credit_state())
