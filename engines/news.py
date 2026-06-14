"""News engine - free RSS headlines per ticker.

Free, no-key source stack:
  - Google News RSS as the primary ticker headline feed
  - Yahoo Finance RSS as a backup/supplement when Google is thin or blocked

Per ticker, the engine scores recent titles with VADER and emits headline
counts, sentiment, velocity, and source metadata for dashboard transparency.
"""
from __future__ import annotations

import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List
from xml.etree import ElementTree as ET

import pandas as pd
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider
from config import NEWS_MAX_HEADLINES_PER_TICKER, USER_AGENT, WORKERS_NEWS

log = logging.getLogger("optedge.news")

_VADER = SentimentIntensityAnalyzer()
_NEWS_CACHE_SEC = 3600


def _empty_row(ticker: str) -> Dict[str, Any]:
    return {
        "ticker": ticker,
        "n_24h": 0,
        "n_7d": 0,
        "news_sent_24h": 0.0,
        "news_sent_7d": 0.0,
        "news_delta": 0.0,
        "news_velocity": 0,
        "top_headline": "",
        "news_source": "",
        "news_provider_count": 0,
    }


def _parse_rss_items(xml_text: str, provider: str) -> List[Dict[str, Any]]:
    """Parse a small RSS feed into the common headline shape."""
    root = ET.fromstring(xml_text)
    items: List[Dict[str, Any]] = []
    for item in root.findall(".//item")[:NEWS_MAX_HEADLINES_PER_TICKER]:
        title = (item.findtext("title") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title:
            continue
        try:
            dt = parsedate_to_datetime(pub_date)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ts = dt.timestamp()
        except Exception:
            ts = time.time()
        items.append({"title": title, "ts": ts, "link": link, "provider": provider})
    return items


def _fetch_provider_rss(ticker: str, provider: str, url: str) -> List[Dict[str, Any]]:
    cache_key = f"news:{provider}:{ticker.upper()}"
    cached = data_provider.cache_get(cache_key, max_age_sec=_NEWS_CACHE_SEC)
    if cached is not None:
        return cached

    sess = data_provider.get_session()
    try:
        response = sess.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        if response.status_code != 200:
            log.debug("news %s %s -> %d", provider, ticker, response.status_code)
            data_provider.cache_put(cache_key, [])
            return []
        items = _parse_rss_items(response.text, provider)
        data_provider.cache_put(cache_key, items)
        return items
    except Exception as exc:
        log.debug("news fetch fail %s %s: %s", provider, ticker, exc)
        return []


def _dedupe_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
    for item in sorted(items, key=lambda row: float(row.get("ts") or 0), reverse=True):
        key = (
            (item.get("title") or "").strip().lower(),
            (item.get("link") or "").strip().lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= NEWS_MAX_HEADLINES_PER_TICKER:
            break
    return out


def _fetch_google_rss(ticker: str) -> List[Dict[str, Any]]:
    query = f"%24{ticker.upper()}+stock"
    url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    return _fetch_provider_rss(ticker, "google_news", url)


def _fetch_yahoo_rss(ticker: str) -> List[Dict[str, Any]]:
    symbol = ticker.upper()
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"
    return _fetch_provider_rss(ticker, "yahoo_finance_rss", url)


def _fetch_rss(ticker: str) -> List[Dict[str, Any]]:
    """Pull free public RSS headlines, using Yahoo Finance as a no-key backup."""
    cache_key = f"news:v2:{ticker.upper()}"
    cached = data_provider.cache_get(cache_key, max_age_sec=_NEWS_CACHE_SEC)
    if cached is not None:
        return cached

    items = _fetch_google_rss(ticker)
    if len(items) < min(3, NEWS_MAX_HEADLINES_PER_TICKER):
        items = _dedupe_items(items + _fetch_yahoo_rss(ticker))
    data_provider.cache_put(cache_key, items)
    return items


def _score_ticker(ticker: str) -> Dict[str, Any]:
    items = _fetch_rss(ticker)
    if not items:
        return _empty_row(ticker)

    now = time.time()
    cutoff_24h = now - 86400
    cutoff_7d = now - 7 * 86400

    sent_24h, n_24h = [], 0
    sent_7d, n_7d = [], 0
    top_headline = str(items[0].get("title") or "")
    providers = sorted({str(item.get("provider") or "unknown") for item in items})

    for item in items:
        if float(item.get("ts") or 0) < cutoff_7d:
            continue
        score = _VADER.polarity_scores(str(item.get("title") or ""))["compound"]
        sent_7d.append(score)
        n_7d += 1
        if float(item.get("ts") or 0) >= cutoff_24h:
            sent_24h.append(score)
            n_24h += 1

    avg_24h = sum(sent_24h) / n_24h if n_24h else 0.0
    avg_7d = sum(sent_7d) / n_7d if n_7d else 0.0
    daily_avg = n_7d / 7.0 if n_7d else 0.0
    velocity = n_24h - daily_avg

    return {
        "ticker": ticker,
        "n_24h": n_24h,
        "n_7d": n_7d,
        "news_sent_24h": round(avg_24h, 3),
        "news_sent_7d": round(avg_7d, 3),
        "news_delta": round(avg_24h - avg_7d, 3),
        "news_velocity": round(velocity, 1),
        "top_headline": top_headline[:140],
        "news_source": "+".join(providers),
        "news_provider_count": len(providers),
    }


def run(universe: List[str], max_workers: int | None = None) -> pd.DataFrame:
    """Parallel per-ticker news scoring."""
    workers = max_workers or WORKERS_NEWS
    rows = []
    completed = 0
    log.info("news for %d tickers (parallel, %d workers)", len(universe), workers)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_score_ticker, ticker): ticker for ticker in universe}
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                rows.append(future.result())
            except Exception as exc:
                log.debug("news fail %s: %s", ticker, exc)
                rows.append(_empty_row(ticker))
            completed += 1
            if completed % 50 == 0 or completed == len(universe):
                log.info("[%d/%d]", completed, len(universe))
    return pd.DataFrame(rows)
