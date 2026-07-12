"""Finnhub API client — shared by congress, insider, and analyst engines.

Free tier: 60 req/min. We cache aggressively (24h) since most of these
data sources update slowly.
"""
from __future__ import annotations
import logging
import time
import sys
from pathlib import Path
from typing import Dict, Any, Optional

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider

try:
    from keys import FINNHUB_API_KEY
except Exception:
    import os
    FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")

log = logging.getLogger("optedge.finnhub")
BASE = "https://finnhub.io/api/v1"


def _ratelimit_sleep():
    """Free tier is 60/min. Distribute across workers — 1/sec is safe."""
    time.sleep(1.0)


def get(endpoint: str, params: Dict[str, Any] = None,
        cache_ttl: int = 86400, force: bool = False) -> Optional[Dict[str, Any]]:
    """Generic Finnhub GET with disk cache + retry."""
    if not FINNHUB_API_KEY:
        return None
    params = dict(params or {})
    params["token"] = FINNHUB_API_KEY
    cache_key = f"finnhub:{endpoint}:{':'.join(f'{k}={v}' for k, v in sorted(params.items()) if k != 'token')}"
    if not force:
        cached = data_provider.cache_get(cache_key, max_age_sec=cache_ttl)
        if cached is not None:
            return cached
    try:
        r = requests.get(f"{BASE}{endpoint}", params=params, timeout=15)
        if r.status_code == 429:
            log.debug("finnhub rate-limited at %s — backing off 5s", endpoint)
            time.sleep(5)
            r = requests.get(f"{BASE}{endpoint}", params=params, timeout=15)
        if r.status_code != 200:
            log.debug("finnhub %s -> %d", endpoint, r.status_code)
            return None
        data = r.json()
        data_provider.cache_put(cache_key, data)
        return data
    except Exception as e:
        log.debug("finnhub %s error: %s", endpoint, e)
        return None
