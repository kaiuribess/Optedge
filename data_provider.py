"""Shared data provider — gives every engine a consistent, hardened way
to fetch data. Wraps yfinance with curl_cffi browser fingerprinting (defeats
most rate limiting), adds disk caching, and exposes a uniform API.

If you have a Polygon.io free API key (set POLYGON_API_KEY env var), this
module will prefer Polygon for options chains and prices — Polygon's free
tier is more reliable than yfinance and doesn't get rate-limited the same way.
"""
from __future__ import annotations
import logging
import os
import time
import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

import pandas as pd

log = logging.getLogger("optedge.provider")

CACHE_DIR = Path(__file__).resolve().parent / "data" / "_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
RAM_CACHE_ENABLED = os.environ.get("OPTEDGE_RAM_CACHE", "1").strip().lower() not in {
    "0", "false", "no", "off",
}
try:
    RAM_CACHE_MAX_ITEMS = max(100, int(os.environ.get("OPTEDGE_RAM_CACHE_MAX_ITEMS", "5000")))
except ValueError:
    RAM_CACHE_MAX_ITEMS = 5000
_RAM_CACHE: Dict[str, tuple[float, Any]] = {}


# -------- Disk cache (light) -----------------------------------------
def _cache_path(key: str) -> Path:
    h = hashlib.sha1(key.encode()).hexdigest()[:16]
    return CACHE_DIR / f"{h}.json"


def cache_get(key: str, max_age_sec: int = 900) -> Optional[Any]:
    if RAM_CACHE_ENABLED:
        entry = _RAM_CACHE.get(key)
        if entry is not None:
            ts, value = entry
            if time.time() - ts <= max_age_sec:
                return value
            _RAM_CACHE.pop(key, None)
    fp = _cache_path(key)
    if not fp.exists():
        return None
    age = time.time() - fp.stat().st_mtime
    if age > max_age_sec:
        return None
    try:
        value = json.loads(fp.read_text())
        if RAM_CACHE_ENABLED:
            _cache_put_ram(key, value)
        return value
    except Exception:
        return None


def cache_put(key: str, value: Any) -> None:
    if RAM_CACHE_ENABLED:
        _cache_put_ram(key, value)
    fp = _cache_path(key)
    try:
        fp.write_text(json.dumps(value, default=str))
    except Exception as e:
        log.debug("cache_put fail: %s", e)


def _cache_put_ram(key: str, value: Any) -> None:
    _RAM_CACHE[key] = (time.time(), value)
    if len(_RAM_CACHE) > RAM_CACHE_MAX_ITEMS:
        oldest = sorted(_RAM_CACHE, key=lambda k: _RAM_CACHE[k][0])[: max(1, len(_RAM_CACHE) // 10)]
        for old_key in oldest:
            _RAM_CACHE.pop(old_key, None)


def cache_stats() -> Dict[str, Any]:
    return {
        "ram_cache_enabled": RAM_CACHE_ENABLED,
        "ram_cache_items": len(_RAM_CACHE),
        "ram_cache_max_items": RAM_CACHE_MAX_ITEMS,
        "disk_cache_dir": str(CACHE_DIR),
    }


def configure_ram_cache(enabled: bool | None = None, max_items: int | None = None) -> Dict[str, Any]:
    global RAM_CACHE_ENABLED, RAM_CACHE_MAX_ITEMS
    if enabled is not None:
        RAM_CACHE_ENABLED = bool(enabled)
        if not RAM_CACHE_ENABLED:
            _RAM_CACHE.clear()
    if max_items is not None:
        RAM_CACHE_MAX_ITEMS = max(100, int(max_items))
        if len(_RAM_CACHE) > RAM_CACHE_MAX_ITEMS:
            for key in sorted(_RAM_CACHE, key=lambda k: _RAM_CACHE[k][0])[: len(_RAM_CACHE) - RAM_CACHE_MAX_ITEMS]:
                _RAM_CACHE.pop(key, None)
    return cache_stats()


# -------- Session factory ---------------------------------------------
_SESSION = None


def get_session():
    """Return a session that uses curl_cffi if available (better at defeating
    rate limiting), else a plain requests session."""
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    try:
        from curl_cffi import requests as creq
        _SESSION = creq.Session(impersonate="chrome120", timeout=30)
        log.debug("using curl_cffi session (chrome120 impersonation)")
    except ImportError:
        import requests
        _SESSION = requests.Session()
        _SESSION.headers["User-Agent"] = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/16.6 Safari/605.1.15"
        )
        log.debug("using plain requests session (curl_cffi unavailable)")
    return _SESSION


# -------- yfinance wrapper --------------------------------------------
def yf_ticker(ticker: str):
    """Return a yfinance Ticker bound to our hardened session."""
    import yfinance as yf
    sess = get_session()
    try:
        return yf.Ticker(ticker, session=sess)
    except TypeError:
        # Older yfinance versions don't accept session kwarg
        return yf.Ticker(ticker)


# -------- Public unified API ------------------------------------------
def get_history(ticker: str, period: str = "1y", interval: str = "1d",
                cache_age: int = 3600) -> pd.DataFrame:
    """Get price history with caching. Returns empty DataFrame on failure.

    v20.3: tries Yahoo v8 chart API directly first (free, no key, no crumb
    needed — bypasses yfinance's heavyweight throttle). Falls back to
    yfinance library if v8 fails.
    """
    key = f"history:{ticker}:{period}:{interval}"
    cached = cache_get(key, cache_age)
    if cached is not None:
        return pd.DataFrame(cached)

    # --- Primary: Yahoo v8 chart endpoint (direct, no library overhead) ---
    h = _yahoo_v8_history(ticker, period, interval)
    if not h.empty:
        out = h.reset_index()
        if "Date" in out.columns:
            out["Date"] = pd.to_datetime(out["Date"]).dt.strftime("%Y-%m-%d")
        cache_put(key, out.to_dict("records"))
        return h

    # --- Fallback: yfinance library (existing path) ---
    try:
        tk = yf_ticker(ticker)
        h = tk.history(period=period, interval=interval)
        if h.empty:
            return pd.DataFrame()
        out = h.reset_index()
        out["Date"] = pd.to_datetime(out["Date"]).dt.strftime("%Y-%m-%d")
        cache_put(key, out.to_dict("records"))
        return h
    except Exception as e:
        log.debug("get_history fail %s: %s", ticker, e)
        return pd.DataFrame()


def _yahoo_v8_history(ticker: str, period: str, interval: str) -> pd.DataFrame:
    """Direct Yahoo v8 chart API call. No crumb required for /v8/finance/chart.
    Uses stdlib urllib (not curl_cffi) since the chrome impersonation triggers
    Yahoo's per-fingerprint rate limit."""
    import urllib.request, urllib.parse, json as _json
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {"range": period, "interval": interval}
    req = urllib.request.Request(
        url + "?" + urllib.parse.urlencode(params),
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            payload = _json.loads(r.read())
    except Exception as e:
        log.debug("yahoo v8 %s: %s", ticker, e)
        return pd.DataFrame()
    try:
        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            return pd.DataFrame()
        res = result[0]
        ts = res.get("timestamp") or []
        quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        opens  = quote.get("open")  or []
        highs  = quote.get("high")  or []
        lows   = quote.get("low")   or []
        closes = quote.get("close") or []
        vols   = quote.get("volume") or []
        if not ts or not closes:
            return pd.DataFrame()
        df = pd.DataFrame({
            "Date":   [datetime.fromtimestamp(t, tz=timezone.utc) for t in ts],
            "Open":   opens,
            "High":   highs,
            "Low":    lows,
            "Close":  closes,
            "Volume": vols,
        })
        df = df.set_index("Date")
        df = df.dropna(subset=["Close"])
        return df
    except Exception as e:
        log.debug("yahoo v8 parse %s: %s", ticker, e)
        return pd.DataFrame()


def get_options_chain(ticker: str, cache_age: int = 600) -> Dict[str, Any]:
    """Get full option chain across all expirations — v20.2 multi-source.

    v20.2: this function now delegates to chain_provider.fetch_chain() which
    tries CBOE -> NASDAQ -> yfinance in order. CBOE is keyless and returns
    every expiration + Greeks in one HTTP call, dramatically improving
    coverage and latency vs. the old yfinance-only path. If chain_provider
    is unavailable for any reason, this function falls back to the legacy
    yfinance implementation below so behavior degrades gracefully.

    Returns:
      {"spot": float, "div_yield": float, "expirations": List[str],
       "chains": Dict[expiry, DataFrame], "source": str}
      or empty dict on failure.
    """
    try:
        from chain_provider import fetch_chain as _multi
        blob = _multi(ticker, cache_age=cache_age)
        if blob:
            return blob
    except Exception as e:
        log.debug("chain_provider unavailable for %s: %s — using yfinance legacy",
                  ticker, e)

    # Legacy yfinance path — preserved as the final safety net
    key = f"chain:{ticker}"
    cached = cache_get(key, cache_age)
    if cached:
        chains = {exp: pd.DataFrame(rows) for exp, rows in cached["chains"].items()}
        return {**cached, "chains": chains}

    try:
        tk = yf_ticker(ticker)
        h = tk.history(period="5d")
        spot = float(h["Close"].iloc[-1]) if not h.empty else None
        if not spot:
            return {}
        info = getattr(tk, "info", {}) or {}
        dy = info.get("dividendYield") or 0.0
        div_yield = dy if dy is not None and dy < 1 else (dy or 0) / 100.0

        expirations = tk.options or []
        chains = {}
        for exp in expirations:
            try:
                opt = tk.option_chain(exp)
                df_calls = opt.calls.copy(); df_calls["side"] = "call"
                df_puts = opt.puts.copy(); df_puts["side"] = "put"
                chains[exp] = pd.concat([df_calls, df_puts], ignore_index=True)
            except Exception:
                continue
            time.sleep(0.2)  # be polite

        out = {
            "spot": spot,
            "div_yield": div_yield,
            "expirations": expirations,
            "chains": chains,
            "source": "yfinance",
        }
        cache_put(key, {**out, "chains": {k: v.to_dict("records") for k, v in chains.items()}})
        return out
    except Exception as e:
        log.debug("get_options_chain fail %s: %s", ticker, e)
        return {}


def get_fundamentals(ticker: str, cache_age: int = 86400) -> Dict[str, Any]:
    key = f"fundamentals:{ticker}"
    cached = cache_get(key, cache_age)
    if cached is not None:
        return cached
    try:
        tk = yf_ticker(ticker)
        info = getattr(tk, "info", {}) or {}
        # Pick out the fields we actually use
        out = {
            "revenueGrowth": info.get("revenueGrowth"),
            "grossMargins": info.get("grossMargins"),
            "operatingMargins": info.get("operatingMargins"),
            "trailingPE": info.get("trailingPE"),
            "forwardPE": info.get("forwardPE"),
            "priceToSalesTrailing12Months": info.get("priceToSalesTrailing12Months"),
            "enterpriseToEbitda": info.get("enterpriseToEbitda"),
            "freeCashflow": info.get("freeCashflow"),
            "marketCap": info.get("marketCap"),
            "dividendYield": info.get("dividendYield"),
            "earningsTimestamp": info.get("earningsTimestamp"),
            "earningsTimestampStart": info.get("earningsTimestampStart"),
        }
        cache_put(key, out)
        return out
    except Exception as e:
        log.debug("get_fundamentals fail %s: %s", ticker, e)
        return {}


def get_short_info(ticker: str, cache_age: int = 86400) -> Dict[str, Any]:
    """v20.2: fetch short-interest fields from yfinance Ticker.info.

    These fields were intentionally stripped from get_fundamentals' cache so
    short_interest.py was reading None for every value. Cached separately so
    the fundamentals cache schema doesn't shift (would invalidate ~500 files)."""
    key = f"short_info:{ticker}"
    cached = cache_get(key, cache_age)
    if cached is not None:
        return cached
    try:
        tk = yf_ticker(ticker)
        info = getattr(tk, "info", {}) or {}
        out = {
            "shortPercentOfFloat":   info.get("shortPercentOfFloat"),
            "shortRatio":            info.get("shortRatio"),
            "sharesShort":           info.get("sharesShort"),
            "sharesShortPriorMonth": info.get("sharesShortPriorMonth"),
            "dateShortInterest":     info.get("dateShortInterest"),
            "floatShares":           info.get("floatShares"),
        }
        # Only cache when at least one field came back populated — avoids
        # poisoning the cache when yfinance silently 401s the info call
        if any(v is not None for v in out.values()):
            cache_put(key, out)
        return out
    except Exception as e:
        log.debug("get_short_info fail %s: %s", ticker, e)
        return {}


# -------- Health -----------------------------------------------------
def status() -> Dict[str, Any]:
    """Read .optedge_status.json (written by setup_check.py)."""
    fp = Path(__file__).resolve().parent / ".optedge_status.json"
    if not fp.exists():
        return {}
    try:
        return json.loads(fp.read_text())
    except Exception:
        return {}
