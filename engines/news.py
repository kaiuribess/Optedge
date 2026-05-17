"""News engine — Google News RSS per ticker.

Free (no API key, no rate limits beyond polite use). Per ticker:
  - fetch headlines from the last 7 days
  - score titles via VADER
  - compute headline_count_24h / 7d, sentiment_now / 7d, news_velocity, Δ
"""
from __future__ import annotations
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any
from xml.etree import ElementTree as ET
from urllib.parse import quote_plus

import pandas as pd
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider
from config import (NEWS_LOOKBACK_DAYS, NEWS_MAX_HEADLINES_PER_TICKER, WORKERS_NEWS,
                    USER_AGENT)

log = logging.getLogger("optedge.news")

_VADER = SentimentIntensityAnalyzer()


def _fetch_rss(ticker: str) -> List[Dict[str, Any]]:
    """Pull Google News RSS for a ticker. Free, public, no key required."""
    cache_key = f"news:{ticker}"
    cached = data_provider.cache_get(cache_key, max_age_sec=3600)  # 1h cache
    if cached is not None:
        return cached

    # Google News RSS — query is `$TICKER stock`. The "$" + uppercase boosts financial relevance.
    query = f"%24{ticker}+stock"
    url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    sess = data_provider.get_session()
    try:
        r = sess.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        if r.status_code != 200:
            log.debug("news %s -> %d", ticker, r.status_code)
            data_provider.cache_put(cache_key, [])
            return []
        root = ET.fromstring(r.text)
        items = []
        for item in root.findall(".//item")[:NEWS_MAX_HEADLINES_PER_TICKER]:
            title = (item.findtext("title") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not title:
                continue
            # Parse RFC 822 date
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(pub_date)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                ts = dt.timestamp()
            except Exception:
                ts = time.time()
            items.append({"title": title, "ts": ts, "link": link})
        data_provider.cache_put(cache_key, items)
        return items
    except Exception as e:
        log.debug("news fetch fail %s: %s", ticker, e)
        return []


def _score_ticker(ticker: str) -> Dict[str, Any]:
    items = _fetch_rss(ticker)
    if not items:
        return {"ticker": ticker, "n_24h": 0, "n_7d": 0,
                "news_sent_24h": 0.0, "news_sent_7d": 0.0,
                "news_delta": 0.0, "news_velocity": 0,
                "top_headline": ""}

    now = time.time()
    cutoff_24h = now - 86400
    cutoff_7d = now - 7 * 86400

    sent_24h, n_24h = [], 0
    sent_7d, n_7d = [], 0
    top_headline = items[0]["title"] if items else ""

    for it in items:
        if it["ts"] < cutoff_7d:
            continue
        score = _VADER.polarity_scores(it["title"])["compound"]
        sent_7d.append(score)
        n_7d += 1
        if it["ts"] >= cutoff_24h:
            sent_24h.append(score)
            n_24h += 1

    avg_24h = sum(sent_24h) / n_24h if n_24h else 0.0
    avg_7d = sum(sent_7d) / n_7d if n_7d else 0.0
    # Velocity: 24h count vs avg daily count over the 7d window
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
    }


def run(universe: List[str], max_workers: int = None) -> pd.DataFrame:
    """Parallel per-ticker news scoring."""
    workers = max_workers or WORKERS_NEWS
    rows = []
    completed = 0
    log.info("news for %d tickers (parallel, %d workers)", len(universe), workers)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_score_ticker, t): t for t in universe}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                rows.append(fut.result())
            except Exception as e:
                log.debug("news fail %s: %s", t, e)
                rows.append({"ticker": t, "n_24h": 0, "n_7d": 0,
                             "news_sent_24h": 0.0, "news_sent_7d": 0.0,
                             "news_delta": 0.0, "news_velocity": 0,
                             "top_headline": ""})
            completed += 1
            if completed % 50 == 0 or completed == len(universe):
                log.info("[%d/%d]", completed, len(universe))
    return pd.DataFrame(rows)
