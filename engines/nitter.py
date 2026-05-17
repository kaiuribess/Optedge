"""Twitter/X social signal — v20.1 (pivoted source).

v20 attempted to scrape Twitter via Nitter mirrors but all 5 instances were
down or rate-limiting at the time of the user's first live run. v20.1
replaces this with the Apewisdom public API which aggregates Twitter +
Reddit + StockTwits + Yahoo mentions into a unified ranking.

Apewisdom is free, no auth, and produces a 24h mention delta + rank delta
which serves as a clean retail-attention momentum signal. Keeps the engine
name "nitter" (and the output column "twitter_score") for compatibility with
v20 dispatch + fusion.

Falls back to Nitter mirrors if Apewisdom is down.

References:
- https://apewisdom.io/api/  (free, no key)
"""
from __future__ import annotations
import logging
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

import sys
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider

log = logging.getLogger("optedge.twitter")

# Apewisdom paginated endpoint — 100 tickers per page, up to ~11 pages
APEWISDOM_BASE = "https://apewisdom.io/api/v1.0/filter/all-stocks"
APEWISDOM_MAX_PAGES = 4  # 400 tickers covers our universe comfortably

# Nitter fallback mirrors — refresh from status.d420.de/api/v1/instances if all dead.
NITTER_MIRRORS = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.fdn.fr",
    "https://nitter.qwik.space",
    "https://nitter.adminforge.de",
    "https://nitter.lunar.icu",
]
NITTER_TIMEOUT = 6


def _fetch_apewisdom_page(page: int) -> List[Dict]:
    """One page of Apewisdom all-stocks ranking."""
    url = f"{APEWISDOM_BASE}/page/{page}"
    key = f"apewisdom:{page}"
    cached = data_provider.cache_get(key, max_age_sec=1800)  # 30 min cache
    if cached is not None:
        return cached
    try:
        sess = data_provider.get_session()
        r = sess.get(url, timeout=20)
        if r.status_code != 200:
            return []
        j = r.json()
        results = j.get("results", [])
        data_provider.cache_put(key, results)
        return results
    except Exception as e:
        log.debug("apewisdom page %d: %s", page, e)
        return []


def _build_from_apewisdom(universe_set: set) -> List[Dict]:
    """Aggregate Apewisdom pages and return per-ticker signal rows."""
    all_results = []
    for p in range(1, APEWISDOM_MAX_PAGES + 1):
        page = _fetch_apewisdom_page(p)
        if not page:
            break
        all_results.extend(page)
        time.sleep(0.3)
    if not all_results:
        return []
    rows = []
    for r in all_results:
        tk = (r.get("ticker") or "").upper()
        if not tk or tk not in universe_set:
            continue
        mentions = int(r.get("mentions") or 0)
        prior = int(r.get("mentions_24h_ago") or 0)
        rank = int(r.get("rank") or 0)
        rank_prior = int(r.get("rank_24h_ago") or 0) or rank
        upvotes = int(r.get("upvotes") or 0)
        sent_raw = r.get("sentiment_score")
        try:
            sent = float(sent_raw) if sent_raw is not None else None
        except Exception:
            sent = None
        # Momentum: mention growth ratio + rank improvement
        # mentions x2 vs yesterday = +0.5; x4 = +1.0; halving = -0.3
        if prior <= 0:
            growth = 1.0 if mentions >= 5 else 0.0
        else:
            ratio = mentions / max(prior, 1)
            growth = max(-0.5, min(1.0, (ratio - 1.0) / 2))
        # Rank improvement (rank 50 -> rank 10 = +40 positions = positive)
        rank_delta = (rank_prior - rank) / max(rank_prior, 1) if rank_prior else 0
        rank_score = max(-0.3, min(0.3, rank_delta))
        # Combine
        score = max(-1.0, min(1.0, growth + rank_score))
        rows.append({
            "ticker": tk,
            "twitter_score": score,
            "twitter_n": mentions,
            "twitter_avg_sent": sent if sent is not None else 0.0,
            "twitter_mentions_24h_ago": prior,
            "twitter_rank": rank,
            "twitter_upvotes": upvotes,
            "twitter_excerpt": "",  # apewisdom doesn't return tweet text
            "twitter_source": "apewisdom",
        })
    return rows


# ---------- Nitter fallback (kept for resilience) ----------
def _vader_score(text: str) -> float:
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        sia = SentimentIntensityAnalyzer()
        return sia.polarity_scores(text)["compound"]
    except Exception:
        return 0.0


def _nitter_fetch(instance: str, ticker: str) -> Optional[List[Dict]]:
    url = f"{instance}/search/rss?f=tweets&q=%24{ticker.upper()}"
    sess = data_provider.get_session()
    try:
        r = sess.get(url, timeout=NITTER_TIMEOUT)
        if r.status_code != 200:
            return None
        items = re.findall(r'<item>(.*?)</item>', r.text, re.DOTALL)
        out = []
        for item in items[:20]:
            t = re.search(r'<title>(.*?)</title>', item, re.DOTALL)
            text = re.sub(r'<[^>]+>', '', t.group(1)) if t else ""
            text = text.replace("&amp;", "&").replace("&quot;", '"').strip()
            if text:
                out.append({"text": text})
        return out
    except Exception:
        return None


def _nitter_fallback(universe_heavy: List[str]) -> List[Dict]:
    """Slow per-ticker Nitter scrape as last resort."""
    rows = []
    targets = universe_heavy[:30]
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _try(tk):
        for inst in NITTER_MIRRORS:
            res = _nitter_fetch(inst, tk)
            if res:
                return res
            time.sleep(0.3)
        return []

    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_try, tk): tk for tk in targets}
        for fut in as_completed(futs):
            tk = futs[fut]
            tweets = fut.result() or []
            if not tweets:
                continue
            scores = [_vader_score(t["text"]) for t in tweets if t.get("text")]
            if not scores:
                continue
            avg = sum(scores) / len(scores)
            rows.append({
                "ticker": tk,
                "twitter_score": max(-1.0, min(1.0, avg)),
                "twitter_n": len(tweets),
                "twitter_avg_sent": avg,
                "twitter_excerpt": tweets[0]["text"][:140],
                "twitter_source": "nitter",
            })
    return rows


def run(universe: List[str]) -> pd.DataFrame:
    if not universe:
        return pd.DataFrame()
    universe_set = {t.upper() for t in universe}

    rows = _build_from_apewisdom(universe_set)
    if rows:
        out = pd.DataFrame(rows)
        log.info("twitter (apewisdom): %d tickers w/ retail-attention momentum", len(out))
        return out

    log.info("apewisdom unavailable — falling back to Nitter mirrors")
    rows = _nitter_fallback(universe)
    if not rows:
        log.info("twitter: both apewisdom AND nitter mirrors failed")
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    log.info("twitter (nitter fallback): %d tickers", len(out))
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run(["AAPL", "TSLA", "NVDA", "MU", "ASTS", "SPY", "GME", "AMC", "MSFT"]))
