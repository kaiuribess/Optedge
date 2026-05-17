"""Earnings whisper engine — v20.2 (layered sources).

History:
  v20  : scraped earningswhispers.com — broke when site went JS-rendered.
  v20.1: pivoted to Finnhub /stock/price-target — works if user has set
         FINNHUB_API_KEY, silently disabled otherwise (most users).
  v20.2: Finnhub stays as the PRIMARY signal source when a key is set.
         Falls back to yfinance Ticker.info `targetMeanPrice` (free, keyless,
         already cached) so the factor works without any API key. Both paths
         compute the same target-vs-spot gap.

Intuition (unchanged):
  - Mean target far above spot  -> "high bar" setup; beat = squeeze.
  - Mean target below spot      -> market priced past analysts; bearish setup.
  - Mean target slightly above  -> sandbagged / low bar; positive beat skew.

Filter: only score tickers with earnings inside the next 14 days (or in the
past 2 days for post-print drift). Outside that window no row is emitted —
matches the engine's original v20 intent of focusing on the earnings catalyst.
"""
from __future__ import annotations
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any

import pandas as pd

import sys
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider

log = logging.getLogger("optedge.whisper")

EARNINGS_WINDOW_DAYS_BEFORE = 14
EARNINGS_WINDOW_DAYS_AFTER  = 2


# ---------------------------------------------------------------------------
# Primary path: Finnhub price-target (v20.1 behavior, preserved)
# ---------------------------------------------------------------------------
def _get_finnhub_key() -> str:
    key = os.environ.get("FINNHUB_API_KEY", "")
    if key:
        return key
    try:
        from keys import FINNHUB_API_KEY
        return FINNHUB_API_KEY
    except Exception:
        return ""


def _fetch_finnhub_price_target(ticker: str) -> Optional[Dict]:
    key = _get_finnhub_key()
    if not key:
        return None
    cache_key = f"finnhub_pt:{ticker}"
    cached = data_provider.cache_get(cache_key, max_age_sec=24 * 3600)
    if cached is not None:
        return cached
    url = "https://finnhub.io/api/v1/stock/price-target"
    try:
        sess = data_provider.get_session()
        r = sess.get(url, params={"symbol": ticker, "token": key}, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        if not data or not isinstance(data, dict) or not data.get("targetMean"):
            return None
        data_provider.cache_put(cache_key, data)
        return data
    except Exception as e:
        log.debug("finnhub price-target %s: %s", ticker, e)
        return None


# ---------------------------------------------------------------------------
# Fallback path: yfinance Ticker.info targetMeanPrice (no key, no quota)
# ---------------------------------------------------------------------------
def _fetch_yf_targets(ticker: str) -> Dict[str, Any]:
    """Pull analyst targets + earnings timestamp from yfinance Ticker.info.
    Cached separately from the fundamentals cache so we don't disturb existing
    cached data."""
    key = f"whisper_yf:{ticker}"
    cached = data_provider.cache_get(key, max_age_sec=12 * 3600)
    if cached is not None:
        return cached
    try:
        tk = data_provider.yf_ticker(ticker)
        info = getattr(tk, "info", {}) or {}
    except Exception as e:
        log.debug("whisper yf %s: %s", ticker, e)
        return {}
    out = {
        "targetMeanPrice":         info.get("targetMeanPrice"),
        "targetMedianPrice":       info.get("targetMedianPrice"),
        "numberOfAnalystOpinions": info.get("numberOfAnalystOpinions"),
        "earningsTimestamp":       info.get("earningsTimestamp"),
        "earningsTimestampStart":  info.get("earningsTimestampStart"),
        "currentPrice":            info.get("currentPrice"),
        "regularMarketPrice":      info.get("regularMarketPrice"),
    }
    if any(v is not None for v in out.values()):
        data_provider.cache_put(key, out)
    return out


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _spot_price(ticker: str, yf_info: Dict[str, Any]) -> Optional[float]:
    for k in ("currentPrice", "regularMarketPrice"):
        v = yf_info.get(k)
        if v and v > 0:
            return float(v)
    try:
        h = data_provider.get_history(ticker, period="5d", cache_age=3600)
        if h.empty:
            return None
        return float(h["Close"].iloc[-1])
    except Exception:
        return None


def _within_earnings_window(yf_info: Dict[str, Any]) -> Optional[int]:
    """Signed days-to-earnings if inside the catalyst window, else None.
    Returns None when no earnings timestamp is available (don't emit a row)."""
    ts = yf_info.get("earningsTimestamp") or yf_info.get("earningsTimestampStart")
    if not ts:
        return None
    try:
        et = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except Exception:
        return None
    days = (et - datetime.now(tz=timezone.utc)).days
    if days > EARNINGS_WINDOW_DAYS_BEFORE:
        return None
    if days < -EARNINGS_WINDOW_DAYS_AFTER:
        return None
    return days


def _score_gap(gap_pct: float) -> float:
    if gap_pct > 0.30:
        return 0.5
    if gap_pct > 0.15:
        return 0.3
    if gap_pct < -0.05:
        return -0.2
    if gap_pct < 0.05:
        return 0.1
    return 0.0


def _process(ticker: str) -> Optional[Dict[str, Any]]:
    # Always need yfinance info for spot + earnings window
    yf_info = _fetch_yf_targets(ticker)
    days_to_earn = _within_earnings_window(yf_info)
    if days_to_earn is None:
        return None
    spot = _spot_price(ticker, yf_info)
    if not spot or spot <= 0:
        return None

    # Primary: Finnhub (if key present)
    source = "yfinance"
    target_mean: Optional[float] = None
    n_analysts: Optional[int] = None
    pt = _fetch_finnhub_price_target(ticker)
    if pt and pt.get("targetMean"):
        try:
            target_mean = float(pt.get("targetMean") or 0)
            n_analysts = pt.get("numberAnalysts") or pt.get("lastUpdated") or None
            if target_mean > 0:
                source = "finnhub"
        except (TypeError, ValueError):
            target_mean = None

    # Fallback: yfinance analyst target
    if not target_mean:
        try:
            tm = yf_info.get("targetMeanPrice")
            if tm and float(tm) > 0:
                target_mean = float(tm)
                n_analysts = yf_info.get("numberOfAnalystOpinions")
                source = "yfinance"
        except (TypeError, ValueError):
            pass

    if not target_mean or target_mean <= 0:
        return None

    gap_pct = (target_mean - spot) / spot
    return {
        "ticker": ticker,
        "whisper_score": _score_gap(gap_pct),
        "whisper_eps": None,
        "whisper_consensus": None,
        "whisper_gap_pct": gap_pct,
        "whisper_target_mean": target_mean,
        "whisper_spot": spot,
        "whisper_days_to_earnings": days_to_earn,
        "whisper_n_analysts": n_analysts,
        "whisper_source": source,
        "whisper_report_date": "",
    }


def run(universe: List[str], earnings_df: Optional[pd.DataFrame] = None,
        max_tickers: int = 80, max_workers: int = 6) -> pd.DataFrame:
    """Whisper-proxy: analyst-target-vs-spot gap for tickers near earnings.

    Layered: Finnhub primary when key is set, yfinance targetMeanPrice fallback.
    `earnings_df` accepted for API compat (unused; we read earnings dates from
    yfinance Ticker.info directly).
    """
    if not universe:
        return pd.DataFrame()
    targets = list(dict.fromkeys(universe))[:max_tickers]
    rows: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_process, tk): tk for tk in targets}
        for fut in as_completed(futs):
            try:
                r = fut.result()
            except Exception:
                r = None
            if r:
                rows.append(r)
    if not rows:
        log.info("whisper: 0 tickers in earnings window with analyst targets")
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    src_counts = out["whisper_source"].value_counts().to_dict()
    log.info("whisper: %d tickers in earnings window (-2d..+14d) (sources=%s)",
             len(out), src_counts)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run(["NVDA", "AAPL", "MSFT", "META", "TSLA", "AMZN", "GOOGL"]))
