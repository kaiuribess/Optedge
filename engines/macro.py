"""Macro / economic context engine — uses data_provider for hardened sessions.

VIX, ^TNX (10y yield), ^IRX (3m yield), SPY 3M return — all via yfinance.
Optional FRED for CPI/UNRATE if FRED_API_KEY env var is set.
"""
from __future__ import annotations
import logging
import os
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone

import pandas as pd
import requests

import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider
from config import VIX_RISK_OFF, VIX_RISK_ON

log = logging.getLogger("optedge.macro")


def _last_close(ticker: str) -> Optional[float]:
    h = data_provider.get_history(ticker, period="5d", cache_age=3600)
    if h.empty:
        return None
    try:
        return float(h["Close"].iloc[-1])
    except Exception:
        return None


def _get_fred_key() -> str:
    """Pull from env first, then keys.py."""
    key = os.environ.get("FRED_API_KEY", "")
    if key:
        return key
    try:
        from keys import FRED_API_KEY
        return FRED_API_KEY
    except Exception:
        return ""


def _fred_obs(series_id: str, limit: int = 1) -> List[Dict[str, str]]:
    """Fetch latest N observations for a FRED series. Cached 6h."""
    key = _get_fred_key()
    if not key:
        return []
    cache_key = f"fred:{series_id}:{limit}"
    cached = data_provider.cache_get(cache_key, max_age_sec=6 * 3600)
    if cached is not None:
        return cached
    try:
        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {"series_id": series_id, "api_key": key, "file_type": "json",
                  "sort_order": "desc", "limit": limit}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        obs = r.json().get("observations", [])
        data_provider.cache_put(cache_key, obs)
        return obs
    except Exception as e:
        log.debug("FRED %s failed: %s", series_id, e)
        return []


def _fred_latest(series_id: str) -> Optional[float]:
    obs = _fred_obs(series_id, limit=1)
    if not obs:
        return None
    try:
        return float(obs[0]["value"])
    except (ValueError, KeyError):
        return None


def _fred_yoy(series_id: str) -> Optional[float]:
    """Year-over-year change of a FRED series (e.g., CPI inflation rate)."""
    obs = _fred_obs(series_id, limit=13)   # 13 months gets us YoY for monthly series
    if len(obs) < 13:
        return None
    try:
        latest = float(obs[0]["value"])
        year_ago = float(obs[12]["value"])
        if year_ago == 0:
            return None
        return (latest / year_ago) - 1
    except (ValueError, KeyError):
        return None


def run() -> Dict[str, Any]:
    vix = _last_close("^VIX")
    tnx = _last_close("^TNX")        # 10Y yield ×10
    irx = _last_close("^IRX")        # 3M yield ×10

    spy_3m = None
    h = data_provider.get_history("SPY", period="3mo", cache_age=3600)
    if not h.empty and len(h) > 2:
        try:
            spy_3m = float(h["Close"].iloc[-1] / h["Close"].iloc[0] - 1)
        except Exception:
            pass

    slope = None
    if tnx is not None and irx is not None:
        slope = (tnx - irx) / 10.0

    # Rich FRED enrichments (when API key is set)
    cpi_yoy = _fred_yoy("CPIAUCSL")              # CPI year-over-year inflation
    unrate = _fred_latest("UNRATE")               # Unemployment rate
    fed_funds = _fred_latest("DFF")               # Federal funds rate
    initial_claims = _fred_latest("ICSA")         # Initial jobless claims (weekly)
    hy_spread = _fred_latest("BAMLH0A0HYM2")      # High yield spread (recession indicator)
    industrial_prod_yoy = _fred_yoy("INDPRO")     # Industrial production YoY
    retail_sales_yoy = _fred_yoy("RSAFS")          # Retail sales YoY
    m2_yoy = _fred_yoy("M2SL")                     # M2 money supply YoY (liquidity)
    # Curve risk: 10Y-3M spread (negative = recession warning)
    t10y3m = _fred_latest("T10Y3M")

    regime = "neutral"
    tilt = 0.0

    # VIX-based core
    if vix is not None:
        if vix >= VIX_RISK_OFF:
            regime = "risk_off"; tilt -= 0.5
        elif vix <= VIX_RISK_ON:
            regime = "risk_on"; tilt += 0.4
    # Yield curve from yfinance fallback
    if slope is not None:
        if slope < 0: tilt -= 0.2
        elif slope > 1.5: tilt += 0.1
    if spy_3m is not None:
        tilt += max(-0.3, min(0.3, spy_3m * 2))

    # FRED-enriched signals (each adds a small tilt)
    # 1. Inflation regime: high CPI YoY = bearish (Fed has to stay restrictive)
    if cpi_yoy is not None:
        if cpi_yoy > 0.04: tilt -= 0.10        # CPI > 4% YoY
        elif cpi_yoy < 0.02: tilt += 0.05      # CPI < 2% (Fed cuts likely)
    # 2. Jobless claims: rising = bearish leading indicator
    if initial_claims is not None and initial_claims > 250_000:
        tilt -= 0.05
    # 3. High yield spread widening = risk-off
    if hy_spread is not None:
        if hy_spread > 5.0: tilt -= 0.15       # HY spread > 500bps = stress
        elif hy_spread < 3.0: tilt += 0.08     # HY < 300bps = healthy credit
    # 4. T10Y3M curve inversion (recession lead time ~12 months)
    if t10y3m is not None and t10y3m < -0.5:
        tilt -= 0.10
    # 5. M2 expansion = liquidity tailwind
    if m2_yoy is not None and m2_yoy > 0.06:
        tilt += 0.05
    # 6. Industrial production declining = recession nearby
    if industrial_prod_yoy is not None and industrial_prod_yoy < -0.02:
        tilt -= 0.08

    tilt = max(-1.0, min(1.0, tilt))
    if tilt > 0.3:    regime = "risk_on"
    elif tilt < -0.3: regime = "risk_off"

    if vix is None and slope is None and spy_3m is None:
        log.warning("No live macro data — falling back to neutral defaults")
        return {
            "asof": datetime.now(timezone.utc).isoformat(),
            "vix": None, "yield_10y": None, "yield_3m": None,
            "yield_curve_slope": None, "spy_3m_return": None,
            "cpi_yoy": cpi_yoy, "unrate": unrate, "fed_funds": fed_funds,
            "initial_claims": initial_claims, "hy_spread": hy_spread,
            "industrial_prod_yoy": industrial_prod_yoy,
            "retail_sales_yoy": retail_sales_yoy, "m2_yoy": m2_yoy,
            "t10y3m": t10y3m,
            "regime": "neutral", "macro_tilt": 0.0,
        }

    return {
        "asof": datetime.now(timezone.utc).isoformat(),
        "vix": vix,
        "yield_10y": tnx / 10.0 if tnx else None,
        "yield_3m": irx / 10.0 if irx else None,
        "yield_curve_slope": slope,
        "spy_3m_return": spy_3m,
        # Rich FRED data
        "cpi_yoy": cpi_yoy,
        "unrate": unrate,
        "fed_funds": fed_funds,
        "initial_claims": initial_claims,
        "hy_spread": hy_spread,
        "industrial_prod_yoy": industrial_prod_yoy,
        "retail_sales_yoy": retail_sales_yoy,
        "m2_yoy": m2_yoy,
        "t10y3m": t10y3m,
        "regime": regime,
        "macro_tilt": tilt,
    }
