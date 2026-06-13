"""Shared data provider — gives every engine a consistent, hardened way
to fetch data. Wraps yfinance with curl_cffi browser fingerprinting (defeats
most rate limiting), adds disk caching, and exposes a uniform API.

If you have a Polygon.io free API key (set POLYGON_API_KEY env var), this
module will prefer Polygon for options chains and prices — Polygon's free
tier is more reliable than yfinance and doesn't get rate-limited the same way.

Free no-key history fallbacks are layered behind Yahoo/yfinance so the app can
keep repricing and backtesting when Yahoo throttles. These fallbacks are not
treated as live quotes.
"""
from __future__ import annotations
import logging
import os
import time
import json
import hashlib
import io
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
        if not h.empty:
            out = h.reset_index()
            out["Date"] = pd.to_datetime(out["Date"]).dt.strftime("%Y-%m-%d")
            cache_put(key, out.to_dict("records"))
            return h
    except Exception as e:
        log.debug("yfinance history fail %s: %s", ticker, e)

    # --- Public Nasdaq historical endpoint. No key, not a live quote. ---
    h = _nasdaq_history(ticker, period, interval)
    if not h.empty:
        out = h.reset_index()
        if "Date" in out.columns:
            out["Date"] = pd.to_datetime(out["Date"]).dt.strftime("%Y-%m-%d")
        cache_put(key, out.to_dict("records"))
        return h

    # --- Final no-key fallback: Stooq public CSV. Not live quotes. ---
    h = _stooq_history(ticker, period, interval)
    if not h.empty:
        out = h.reset_index()
        if "Date" in out.columns:
            out["Date"] = pd.to_datetime(out["Date"]).dt.strftime("%Y-%m-%d")
        cache_put(key, out.to_dict("records"))
        return h

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


_NASDAQ_ETF_HINTS = {
    "SPY", "QQQ", "IWM", "DIA", "VXX", "UVXY", "TLT", "IEF", "HYG", "LQD",
    "XLF", "XLK", "XLE", "XLV", "XLY", "XLI", "XLP", "XLB", "XLU", "XLRE",
    "IBIT", "GBTC", "ETHA",
}

_NASDAQ_INDEX_MAP = {
    "^IXIC": "COMP",
    "^NDX": "NDX",
    "^GSPC": "SPX",
    "^SPX": "SPX",
    "^DJI": "DJI",
    "^RUT": "RUT",
}


def _nasdaq_symbol_and_assetclasses(ticker: str) -> tuple[str | None, list[str]]:
    raw = str(ticker or "").strip().upper()
    if not raw or raw.endswith("=F") or raw.endswith("=X"):
        return None, []
    if raw in _NASDAQ_INDEX_MAP:
        return _NASDAQ_INDEX_MAP[raw], ["index"]
    if raw.startswith("^"):
        return None, []
    symbol = raw.replace("-", ".")
    if not symbol.replace(".", "").isalnum():
        return None, []
    if symbol in _NASDAQ_ETF_HINTS:
        return symbol, ["etf", "stocks"]
    return symbol, ["stocks", "etf"]


def _nasdaq_clean_number(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return float("nan")
    text = text.replace("$", "").replace(",", "")
    text = text.replace("N/A", "").strip()
    try:
        return float(text)
    except ValueError:
        return float("nan")


def _nasdaq_history(ticker: str, period: str, interval: str) -> pd.DataFrame:
    if str(interval or "1d").strip().lower() not in {"1d", "d", "daily"}:
        return pd.DataFrame()
    symbol, assetclasses = _nasdaq_symbol_and_assetclasses(ticker)
    if not symbol:
        return pd.DataFrame()

    import urllib.parse
    import urllib.request

    today = datetime.now(timezone.utc).date()
    start_ts = _period_start(period)
    if start_ts is None:
        start_date = today - timedelta(days=3650)
    else:
        start_date = start_ts.date()
    params_base = {
        "fromdate": start_date.isoformat(),
        "todate": today.isoformat(),
        "limit": "9999",
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Origin": "https://www.nasdaq.com",
        "Referer": "https://www.nasdaq.com/",
    }
    for assetclass in assetclasses:
        params = urllib.parse.urlencode({**params_base, "assetclass": assetclass})
        safe_symbol = urllib.parse.quote(symbol, safe="")
        req = urllib.request.Request(
            f"https://api.nasdaq.com/api/quote/{safe_symbol}/historical?{params}",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except Exception as e:
            log.debug("nasdaq history %s/%s: %s", ticker, assetclass, e)
            continue
        rows = (
            ((payload or {}).get("data") or {})
            .get("tradesTable", {})
            .get("rows", [])
        )
        if not rows:
            continue
        parsed = []
        for row in rows:
            try:
                dt = pd.to_datetime(row.get("date"), format="%m/%d/%Y", errors="coerce", utc=True)
            except Exception:
                dt = pd.NaT
            if pd.isna(dt):
                continue
            parsed.append({
                "Date": dt,
                "Open": _nasdaq_clean_number(row.get("open")),
                "High": _nasdaq_clean_number(row.get("high")),
                "Low": _nasdaq_clean_number(row.get("low")),
                "Close": _nasdaq_clean_number(row.get("close")),
                "Volume": _nasdaq_clean_number(row.get("volume")),
            })
        df = pd.DataFrame(parsed)
        if df.empty:
            continue
        df = df.dropna(subset=["Close"]).set_index("Date").sort_index()
        if not df.empty:
            return df[["Open", "High", "Low", "Close", "Volume"]]
    return pd.DataFrame()


_STOOQ_INDEX_MAP = {
    "^GSPC": "^spx",
    "^SPX": "^spx",
    "^DJI": "^dji",
    "^IXIC": "^ndq",
    "^NDX": "^ndx",
    "^RUT": "^rut",
    "^VIX": "^vix",
}

_STOOQ_FUTURES_MAP = {
    "ES=F": "es.f",
    "NQ=F": "nq.f",
    "YM=F": "ym.f",
    "RTY=F": "rty.f",
    "CL=F": "cl.f",
    "NG=F": "ng.f",
    "GC=F": "gc.f",
    "SI=F": "si.f",
    "HG=F": "hg.f",
    "ZW=F": "zw.f",
    "ZC=F": "zc.f",
    "ZS=F": "zs.f",
    "ZB=F": "zb.f",
    "ZN=F": "zn.f",
    "DX=F": "dx.f",
}


def _stooq_symbol(ticker: str) -> str | None:
    raw = str(ticker or "").strip().upper()
    if not raw:
        return None
    if raw in _STOOQ_INDEX_MAP:
        return _STOOQ_INDEX_MAP[raw]
    if raw in _STOOQ_FUTURES_MAP:
        return _STOOQ_FUTURES_MAP[raw]
    if raw.startswith("^") or raw.endswith("=F") or raw.endswith("=X"):
        return None
    normalized = raw.replace("-", ".")
    if not normalized.replace(".", "").isalnum():
        return None
    return f"{normalized.lower()}.us"


def _period_start(period: str) -> pd.Timestamp | None:
    text = str(period or "").strip().lower()
    now = pd.Timestamp.now(tz="UTC").normalize()
    if text in {"", "max"}:
        return None
    if text == "ytd":
        return pd.Timestamp(year=now.year, month=1, day=1, tz="UTC")
    units = {"d": "days", "mo": "months", "y": "years"}
    for suffix, unit in units.items():
        if not text.endswith(suffix):
            continue
        try:
            n = int(text[: -len(suffix)])
        except ValueError:
            return None
        if n <= 0:
            return None
        if unit == "days":
            return now - pd.Timedelta(days=n)
        if unit == "months":
            return now - pd.DateOffset(months=n)
        if unit == "years":
            return now - pd.DateOffset(years=n)
    return None


def _stooq_interval(interval: str) -> str | None:
    text = str(interval or "1d").strip().lower()
    if text in {"1d", "d", "daily"}:
        return "d"
    if text in {"1wk", "1w", "wk", "weekly"}:
        return "w"
    if text in {"1mo", "1m", "mo", "monthly"}:
        return "m"
    return None


def _stooq_history(ticker: str, period: str, interval: str) -> pd.DataFrame:
    stooq_symbol = _stooq_symbol(ticker)
    stooq_interval = _stooq_interval(interval)
    if not stooq_symbol or not stooq_interval:
        return pd.DataFrame()

    import urllib.parse
    import urllib.request

    params = urllib.parse.urlencode({"s": stooq_symbol, "i": stooq_interval})
    req = urllib.request.Request(
        f"https://stooq.com/q/d/l/?{params}",
        headers={"User-Agent": "Mozilla/5.0 (compatible; Optedge/1.0)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.debug("stooq history %s/%s: %s", ticker, stooq_symbol, e)
        return pd.DataFrame()

    if not raw.strip() or raw.lower().startswith("no data"):
        return pd.DataFrame()
    try:
        df = pd.read_csv(io.StringIO(raw))
    except Exception as e:
        log.debug("stooq parse %s/%s: %s", ticker, stooq_symbol, e)
        return pd.DataFrame()
    if df.empty or "Date" not in df.columns or "Close" not in df.columns:
        return pd.DataFrame()

    rename = {col: col.title() for col in df.columns}
    df = df.rename(columns=rename)
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce", utc=True)
    df = df.dropna(subset=["Date", "Close"]).set_index("Date").sort_index()
    if df.empty:
        return pd.DataFrame()

    start = _period_start(period)
    if start is not None:
        df = df[df.index >= start]
    cols = [col for col in ("Open", "High", "Low", "Close", "Volume") if col in df.columns]
    return df[cols]


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
