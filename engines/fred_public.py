"""Keyless FRED CSV helper.

FRED's official JSON API is best when a key is configured, but the public graph
CSV endpoint is enough for current macro context when no key is available.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from io import StringIO
import logging
from typing import Any

import pandas as pd

import data_provider

log = logging.getLogger("optedge.fred_public")

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


def fred_csv_history(series_id: str, days: int = 90, cache_hours: int = 12) -> list[dict[str, Any]]:
    """Fetch recent observations from FRED's public CSV endpoint.

    Returns newest-first rows shaped like the official FRED API fallback:
    ``{"date": "YYYY-MM-DD", "value": float}``.
    """
    series = str(series_id or "").strip().upper()
    if not series:
        return []
    days = max(1, int(days or 1))
    cache_key = f"fred_public_csv:{series}:{days}"
    cached = data_provider.cache_get(cache_key, max_age_sec=max(60, cache_hours * 3600))
    if cached is not None:
        return cached

    start = (datetime.utcnow() - timedelta(days=days * 3 + 14)).strftime("%Y-%m-%d")
    try:
        session = data_provider.get_session()
        resp = session.get(FRED_CSV_URL, params={"id": series, "cosd": start}, timeout=15)
        if getattr(resp, "status_code", 0) != 200:
            log.debug("FRED CSV %s returned %s", series, getattr(resp, "status_code", "unknown"))
            return []
        df = pd.read_csv(StringIO(resp.text))
        if "observation_date" not in df.columns or series not in df.columns:
            log.debug("FRED CSV %s unexpected columns: %s", series, list(df.columns))
            return []
        df[series] = pd.to_numeric(df[series].replace(".", pd.NA), errors="coerce")
        df = df.dropna(subset=[series]).tail(days)
        out = [
            {"date": str(row["observation_date"]), "value": float(row[series])}
            for _, row in df.iloc[::-1].iterrows()
        ]
        data_provider.cache_put(cache_key, out)
        return out
    except Exception as exc:
        log.debug("FRED CSV %s failed: %s", series, exc)
        return []
