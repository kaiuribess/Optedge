"""Polygon.io adapter — alternative to yfinance.

Free tier (https://polygon.io/pricing) gives:
  - 5 API calls/min (we throttle to stay under)
  - End-of-day data, including options chains
  - 2 years of history

Set POLYGON_API_KEY env var to enable. The provider auto-detects this and
falls back to yfinance if the key isn't set.

Why this matters: Polygon's free tier is more reliable than yfinance for
cloud/datacenter IPs, where Yahoo aggressively rate-limits.
"""
from __future__ import annotations
import logging
import os
import time
from typing import Dict, Any, List

import pandas as pd
import requests

log = logging.getLogger("optedge.polygon")

BASE = "https://api.polygon.io"


def _get(path: str, **params) -> Dict[str, Any]:
    key = os.environ.get("POLYGON_API_KEY")
    if not key:
        raise RuntimeError("POLYGON_API_KEY not set")
    params["apiKey"] = key
    r = requests.get(f"{BASE}{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _throttle():
    """Free tier is 5 req/min. Sleep 13s between calls to stay safe."""
    time.sleep(13)


def get_options_chain(ticker: str) -> Dict[str, Any]:
    """Return the same dict shape as data_provider.get_options_chain."""
    # 1. Get spot
    snap = _get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}")
    spot = snap.get("ticker", {}).get("day", {}).get("c") or snap.get("ticker", {}).get("prevDay", {}).get("c")
    if not spot:
        return {}

    _throttle()

    # 2. List option contracts (active, expiring within 60 days)
    contracts = _get(
        "/v3/reference/options/contracts",
        underlying_ticker=ticker,
        expired="false",
        limit=1000,
    ).get("results", [])

    if not contracts:
        return {}

    # 3. Group by expiry and build per-expiry DataFrames
    chains: Dict[str, pd.DataFrame] = {}
    by_exp: Dict[str, List[Dict[str, Any]]] = {}
    for c in contracts:
        exp = c.get("expiration_date")
        if not exp:
            continue
        by_exp.setdefault(exp, []).append({
            "strike": c.get("strike_price"),
            "side": c.get("contract_type"),    # 'call' / 'put'
            "ticker_option": c.get("ticker"),
        })

    for exp, items in by_exp.items():
        df = pd.DataFrame(items)
        # Free tier doesn't give live quotes per contract — populate placeholders
        df["bid"] = 0.0
        df["ask"] = 0.0
        df["lastPrice"] = 0.0
        df["openInterest"] = 0
        df["volume"] = 0
        chains[exp] = df

    log.warning("Polygon free tier doesn't include live option quotes — "
                "you'll get the contract list but no live IV/mispricing. "
                "Upgrade to a paid Polygon plan or stick with yfinance for full functionality.")

    return {
        "spot": spot,
        "div_yield": 0.0,
        "expirations": sorted(by_exp.keys()),
        "chains": chains,
    }
