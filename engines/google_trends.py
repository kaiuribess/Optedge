"""Google Trends + Wikipedia pageviews search-interest engine — v20.2.

PRIMARY  : pytrends (unofficial Google Trends scrape). Same as v20.1.
           Rate-limited HARD (429 after ~20 queries) — known limitation.
FALLBACK : Wikipedia pageviews REST API. Free, no key, no rate-limit issue.
           Per-article daily views over a 30d window. We resolve the article
           name from yfinance Ticker.info `longName`, falling back to the
           ticker symbol itself if no company name is cached.

Retail attention is a real (if noisy) short-horizon signal — particularly
for meme/momentum names. Either source produces the same kind of "recent
spike vs longer baseline" momentum score, so fusion weights can stay put.
"""
from __future__ import annotations
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

import sys
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider

log = logging.getLogger("optedge.gtrends")

MAX_TICKERS_PER_RUN = 60   # rate-limit safety (pytrends path)
BATCH_SIZE = 5             # pytrends limit per request
WIKI_MAX_WORKERS = 6
WIKI_UA = "optedge-research/0.2 (research@optedge.local)"


# ---------------------------------------------------------------------------
# PRIMARY: pytrends (unchanged from v20.1)
# ---------------------------------------------------------------------------
def _build_payload(pytrends, terms: List[str]) -> pd.DataFrame:
    try:
        pytrends.build_payload(terms, cat=0, timeframe="today 3-m", geo="US", gprop="")
        df = pytrends.interest_over_time()
        if df is None or df.empty:
            return pd.DataFrame()
        if "isPartial" in df.columns:
            df = df.drop(columns="isPartial")
        return df
    except Exception as e:
        log.debug("gtrends batch %s fail: %s", terms, e)
        return pd.DataFrame()


def _score_pytrends_series(series: pd.Series) -> float:
    if series is None or len(series) < 10:
        return 0.0
    recent = series.tail(7).mean()
    baseline = series.mean()
    if baseline <= 0:
        return 0.0
    ratio = recent / baseline
    if ratio >= 3.0: return 1.0
    if ratio >= 2.0: return 0.7
    if ratio >= 1.5: return 0.4
    if ratio >= 1.2: return 0.2
    if ratio < 0.5:  return -0.3
    return 0.0


def _run_pytrends(tickers: List[str]) -> Dict[str, float]:
    try:
        from pytrends.request import TrendReq
    except ImportError:
        log.info("gtrends: pytrends not installed (pip install pytrends) — using Wikipedia fallback")
        return {}
    try:
        pytrends = TrendReq(hl="en-US", tz=300, timeout=(10, 25))
    except Exception as e:
        log.info("gtrends: TrendReq init fail (%s) — using Wikipedia fallback", e)
        return {}

    results: Dict[str, float] = {}
    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        cache_key = f"gtrends:{','.join(batch)}"
        cached = data_provider.cache_get(cache_key, max_age_sec=6 * 3600)
        if cached is not None:
            results.update(cached)
            continue
        df = _build_payload(pytrends, batch)
        if df.empty:
            time.sleep(2.0)
            continue
        batch_scores = {}
        for tk in batch:
            if tk in df.columns:
                batch_scores[tk] = _score_pytrends_series(df[tk])
        results.update(batch_scores)
        if batch_scores:
            data_provider.cache_put(cache_key, batch_scores)
        time.sleep(1.5)
    return results


# ---------------------------------------------------------------------------
# FALLBACK: Wikipedia pageviews REST API
# ---------------------------------------------------------------------------
def _ticker_to_article(ticker: str) -> Optional[str]:
    """Resolve ticker -> Wikipedia article slug. Uses yfinance longName when
    available, else falls back to '<ticker>_(company)'-style guess."""
    key = f"wiki_article:{ticker}"
    cached = data_provider.cache_get(key, max_age_sec=30 * 86400)
    if cached:
        return cached
    article: Optional[str] = None
    try:
        tk = data_provider.yf_ticker(ticker)
        info = getattr(tk, "info", {}) or {}
        name = info.get("longName") or info.get("shortName")
        if name:
            # Strip common suffixes that don't appear in WP titles
            for sfx in (", Inc.", ", Inc", " Inc.", " Inc", " Corp.", " Corp",
                        " Corporation", ", Ltd.", " Ltd.", " Ltd", " plc",
                        " Holdings", " Group", " Company"):
                if name.endswith(sfx):
                    name = name[:-len(sfx)]
                    break
            article = name.strip().replace(" ", "_")
    except Exception:
        pass
    if not article:
        article = ticker
    data_provider.cache_put(key, article)
    return article


def _wiki_pageviews(article: str, days: int = 30) -> Optional[List[int]]:
    """Return list of daily pageviews for the article, most-recent last."""
    cache_key = f"wiki_pv:{article}:{days}"
    cached = data_provider.cache_get(cache_key, max_age_sec=6 * 3600)
    if cached is not None:
        return cached
    end = datetime.utcnow().date() - timedelta(days=1)
    start = end - timedelta(days=days)
    url = (
        "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
        f"en.wikipedia/all-access/user/{article}/daily/"
        f"{start.strftime('%Y%m%d')}/{end.strftime('%Y%m%d')}"
    )
    sess = data_provider.get_session()
    try:
        r = sess.get(url, headers={"User-Agent": WIKI_UA}, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        views = [int(item.get("views") or 0) for item in (data.get("items") or [])]
        if not views:
            return None
        data_provider.cache_put(cache_key, views)
        return views
    except Exception as e:
        log.debug("wiki pageviews %s: %s", article, e)
        return None


def _score_wiki_views(views: List[int]) -> float:
    """Score: trailing-7-day mean vs full-window mean. Same shape as pytrends."""
    if not views or len(views) < 10:
        return 0.0
    recent = sum(views[-7:]) / 7
    baseline = sum(views) / len(views)
    if baseline <= 0:
        return 0.0
    ratio = recent / baseline
    if ratio >= 3.0: return 1.0
    if ratio >= 2.0: return 0.7
    if ratio >= 1.5: return 0.4
    if ratio >= 1.2: return 0.2
    if ratio < 0.5:  return -0.3
    return 0.0


def _run_wiki(tickers: List[str]) -> Dict[str, float]:
    results: Dict[str, float] = {}
    def _one(tk: str):
        article = _ticker_to_article(tk)
        if not article:
            return tk, 0.0
        views = _wiki_pageviews(article)
        if not views:
            # Try the bare ticker if the longName-derived article missed
            if article != tk:
                views = _wiki_pageviews(tk)
            if not views:
                return tk, 0.0
        return tk, _score_wiki_views(views)
    with ThreadPoolExecutor(max_workers=WIKI_MAX_WORKERS) as ex:
        futs = [ex.submit(_one, tk) for tk in tickers]
        for fut in as_completed(futs):
            try:
                tk, s = fut.result()
                if abs(s) > 1e-3:
                    results[tk] = s
            except Exception:
                continue
    return results


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def run(universe: List[str]) -> pd.DataFrame:
    if not universe:
        return pd.DataFrame()
    tickers = list(dict.fromkeys(universe))[:MAX_TICKERS_PER_RUN]

    # Primary
    results = _run_pytrends(tickers)
    source = "pytrends"

    # Fallback: Wikipedia for any tickers not covered
    missing = [tk for tk in tickers if tk not in results]
    if missing:
        wiki_scores = _run_wiki(missing)
        if wiki_scores:
            results.update(wiki_scores)
            source = "pytrends+wiki" if "pytrends" in source else "wiki"
            if not any(tk for tk in tickers if tk in results and tk not in wiki_scores):
                source = "wiki"

    if not results:
        log.info("gtrends: no results from pytrends or Wikipedia")
        return pd.DataFrame()
    rows = [{"ticker": tk, "gtrends_score": score, "gtrends_term": tk,
             "gtrends_source": source}
            for tk, score in results.items() if abs(score) > 1e-3]
    out = pd.DataFrame(rows)
    log.info("gtrends(%s): %d tickers with search/attention momentum",
             source, len(out))
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run(["AAPL", "MSFT", "NVDA", "TSLA", "GME", "AMC"]))
