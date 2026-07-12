"""Cache hit rate + freshness tracking.

Monkey-patches data_provider.cache_get + cache_put to track hit/miss counts
per cache key prefix. Exposed via dashboard panel.
"""
from __future__ import annotations
import logging
import time
from collections import defaultdict
from typing import Dict

log = logging.getLogger("optedge.cache_stats")

_HITS: Dict[str, int] = defaultdict(int)
_MISSES: Dict[str, int] = defaultdict(int)
_LAST_HIT_TS: Dict[str, float] = {}


def _prefix_of(key: str) -> str:
    # Cache keys look like "yf_ticker:NVDA" / "fred:DGS10:1" / "13f_data:1234..."
    return key.split(":", 1)[0]


def record_hit(key: str):
    p = _prefix_of(key)
    _HITS[p] += 1
    _LAST_HIT_TS[p] = time.time()


def record_miss(key: str):
    p = _prefix_of(key)
    _MISSES[p] += 1


def install_hooks():
    """Wrap data_provider's cache_get / cache_put to track usage."""
    try:
        import data_provider
    except ImportError:
        return False
    if getattr(data_provider, "_cache_stats_installed", False):
        return True
    orig_get = data_provider.cache_get
    orig_put = data_provider.cache_put

    def wrapped_get(key, max_age_sec=900):
        result = orig_get(key, max_age_sec)
        if result is not None:
            record_hit(key)
        else:
            record_miss(key)
        return result

    def wrapped_put(key, value):
        return orig_put(key, value)

    data_provider.cache_get = wrapped_get
    data_provider.cache_put = wrapped_put
    data_provider._cache_stats_installed = True
    return True


def summary() -> Dict:
    """Aggregate hit/miss counts per prefix."""
    out = {}
    all_keys = set(_HITS) | set(_MISSES)
    for p in all_keys:
        h = _HITS.get(p, 0)
        m = _MISSES.get(p, 0)
        total = h + m
        if total == 0:
            continue
        out[p] = {
            "hits": h,
            "misses": m,
            "hit_rate": h / total if total else 0.0,
            "total": total,
        }
    return out


def freshness_summary() -> Dict:
    """Return age (seconds since last hit) per prefix.

    Useful as a 'data freshness' indicator on the dashboard.
    """
    now = time.time()
    out = {}
    for p, ts in _LAST_HIT_TS.items():
        out[p] = {"last_hit_age_sec": now - ts}
    return out
