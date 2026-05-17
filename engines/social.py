"""Social signal engine — beyond Reddit.

Two free sources:
  1. StockTwits per-ticker stream (api.stocktwits.com, no auth, 200/hr limit).
     Pulls 30 most recent messages per ticker. Bullish/bearish tags often present.
  2. Donald Trump Truth Social (mastodon-compatible API).
     Trump posts can move markets — caches tickers mentioned + sentiment score.
     Falls back to Wayback / mirror sites if Truth Social is unreachable.

Each source contributes to a single per-ticker `social_score` that gets
fused with the rest of the multi-factor stack.
"""
from __future__ import annotations
import logging
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

import requests
import pandas as pd
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider
from config import USER_AGENT

log = logging.getLogger("optedge.social")
_VADER = SentimentIntensityAnalyzer()

STOCKTWITS_URL = "https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
TRUTH_SOCIAL_API = "https://truthsocial.com/api/v1/accounts/{id}/statuses?limit={limit}"
TRUMP_TRUTHSOCIAL_ID = "107780257626128497"   # @realDonaldTrump
TRUMP_MIRROR_URL = "https://trumpstruth.org/?s=&limit=30"   # fallback mirror

# Company name → ticker map for politicians/news posts that don't use cashtags.
# Conservative list — only ambiguous-free names.
COMPANY_TO_TICKER = {
    # Mega caps
    "apple": "AAPL", "microsoft": "MSFT", "nvidia": "NVDA", "amazon": "AMZN",
    "google": "GOOGL", "alphabet": "GOOGL", "meta": "META", "facebook": "META",
    "tesla": "TSLA", "netflix": "NFLX", "oracle": "ORCL",
    # Large caps
    "boeing": "BA", "lockheed": "LMT", "raytheon": "RTX", "northrop": "NOC",
    "general dynamics": "GD", "general motors": "GM", "ford": "F",
    "exxon": "XOM", "chevron": "CVX", "conoco": "COP",
    "jpmorgan": "JPM", "goldman": "GS", "morgan stanley": "MS",
    "bank of america": "BAC", "wells fargo": "WFC", "citi": "C",
    "walmart": "WMT", "costco": "COST", "target": "TGT",
    "home depot": "HD", "lowe": "LOW", "starbucks": "SBUX",
    "mcdonald": "MCD", "coca-cola": "KO", "pepsi": "PEP",
    "pfizer": "PFE", "moderna": "MRNA", "merck": "MRK", "lilly": "LLY",
    "johnson & johnson": "JNJ", "j&j": "JNJ", "unitedhealth": "UNH",
    "disney": "DIS", "comcast": "CMCSA",
    "intel": "INTC", "amd": "AMD", "qualcomm": "QCOM", "broadcom": "AVGO",
    "micron": "MU", "tsmc": "TSM", "asml": "ASML",
    # Speculative / Trump-relevant
    "trump media": "DJT", "djt": "DJT", "truth social": "DJT",
    "palantir": "PLTR", "robinhood": "HOOD", "coinbase": "COIN",
    "rivian": "RIVN", "lucid": "LCID",
    "bitcoin": "IBIT", "btc": "IBIT",
    "ethereum": "ETHA", "eth": "ETHA",
    "salesforce": "CRM", "snowflake": "SNOW", "shopify": "SHOP",
    # ETFs people mention
    "spy": "SPY", "qqq": "QQQ", "russell": "IWM",
}
_CASHTAG_RE = re.compile(r"\$([A-Z]{1,5})\b")


def _extract_tickers_from_text(text: str, valid: set) -> List[str]:
    if not text:
        return []
    found = set()
    for m in _CASHTAG_RE.finditer(text):
        sym = m.group(1).upper()
        if sym in valid:
            found.add(sym)
    # Company-name fallback — case-insensitive substring match
    low = text.lower()
    for name, tk in COMPANY_TO_TICKER.items():
        if name in low and tk in valid:
            found.add(tk)
    return list(found)


# -------- StockTwits ---------------------------------------------------
def _fetch_stocktwits(ticker: str) -> List[Dict[str, Any]]:
    """One ticker's recent stream — 30 messages from public StockTwits API."""
    cache_key = f"stocktwits:{ticker}"
    cached = data_provider.cache_get(cache_key, max_age_sec=900)
    if cached is not None:
        return cached
    try:
        sess = data_provider.get_session()
        url = STOCKTWITS_URL.format(ticker=ticker)
        r = sess.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        if r.status_code != 200:
            log.debug("stocktwits %s -> %d", ticker, r.status_code)
            data_provider.cache_put(cache_key, [])
            return []
        msgs = r.json().get("messages", []) or []
        # Trim — keep only what we need
        slim = []
        for m in msgs:
            slim.append({
                "body": (m.get("body") or "")[:500],
                "ts": m.get("created_at"),
                "sentiment": (m.get("entities") or {}).get("sentiment", {}).get("basic")
                              if isinstance((m.get("entities") or {}).get("sentiment"), dict)
                              else None,
                "ups": m.get("likes", {}).get("total", 0) if isinstance(m.get("likes"), dict) else 0,
            })
        data_provider.cache_put(cache_key, slim)
        return slim
    except Exception as e:
        log.debug("stocktwits %s error: %s", ticker, e)
        return []


def _score_stocktwits_for_tickers(tickers: List[str], max_workers: int = 6) -> Dict[str, Dict[str, Any]]:
    """Pull StockTwits for each ticker and aggregate sentiment + mention count."""
    out: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_fetch_stocktwits, t): t for t in tickers}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                msgs = fut.result()
            except Exception:
                msgs = []
            if not msgs:
                continue
            scores, n, n_bull, n_bear = [], 0, 0, 0
            for m in msgs:
                txt = m.get("body") or ""
                if not txt:
                    continue
                # Use explicit Bullish/Bearish tags when set; otherwise VADER
                tag = (m.get("sentiment") or "").lower()
                if tag == "bullish":
                    scores.append(0.6)
                    n_bull += 1
                elif tag == "bearish":
                    scores.append(-0.6)
                    n_bear += 1
                else:
                    scores.append(_VADER.polarity_scores(txt)["compound"])
                n += 1
            if n == 0:
                continue
            avg = sum(scores) / n
            out[t] = {
                "stocktwits_n": n,
                "stocktwits_avg_sent": round(avg, 3),
                "stocktwits_n_bull": n_bull,
                "stocktwits_n_bear": n_bear,
            }
    return out


# -------- Trump Truth Social ------------------------------------------
def _fetch_trump_posts(limit: int = 20) -> List[Dict[str, Any]]:
    """Fetch Trump's recent Truth Social posts. Falls back to mirror if blocked."""
    cache_key = "social:trump_posts"
    cached = data_provider.cache_get(cache_key, max_age_sec=1800)
    if cached is not None:
        return cached

    sess = data_provider.get_session()
    # 1. Direct Truth Social Mastodon-compatible API
    try:
        url = TRUTH_SOCIAL_API.format(id=TRUMP_TRUTHSOCIAL_ID, limit=limit)
        r = sess.get(url, headers={"User-Agent": USER_AGENT,
                                   "Accept": "application/json"}, timeout=15)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and data:
                posts = []
                for p in data:
                    posts.append({
                        "body": _strip_html(p.get("content") or ""),
                        "ts": p.get("created_at"),
                        "url": p.get("url"),
                        "favourites": p.get("favourites_count", 0),
                        "reblogs": p.get("reblogs_count", 0),
                    })
                data_provider.cache_put(cache_key, posts)
                log.info("trump truth social: %d posts via direct API", len(posts))
                return posts
    except Exception as e:
        log.debug("truth social direct API failed: %s", e)

    # 2. Mirror site fallback (trumpstruth.org has scrapable Trump posts)
    try:
        r = sess.get(TRUMP_MIRROR_URL,
                     headers={"User-Agent": USER_AGENT}, timeout=20)
        if r.status_code == 200:
            posts = _parse_trumpstruth_html(r.text, limit=limit)
            if posts:
                data_provider.cache_put(cache_key, posts)
                log.info("trump truth social: %d posts via mirror", len(posts))
                return posts
    except Exception as e:
        log.debug("trumpstruth.org fallback failed: %s", e)

    log.warning("could not reach Trump posts via any source")
    return []


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"<[^>]+>", " ", s).replace("&amp;", "&").replace("&nbsp;", " ").strip()


def _parse_trumpstruth_html(html_text: str, limit: int = 30) -> List[Dict[str, Any]]:
    """Parse trumpstruth.org's post listing HTML.

    Multiple parsing strategies in order of preference. trumpstruth.org's
    layout has shifted over time so we try BS4 with several CSS selectors,
    falling back to aggressive text extraction.
    """
    if not html_text:
        return []

    posts: List[Dict[str, Any]] = []

    # Strategy 1: BeautifulSoup with multiple selectors
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_text, "html.parser")
        # Try several selectors that match common post-listing layouts
        candidates = (
            soup.select("article")
            or soup.select("div.status, div.post, div.truth, div.entry")
            or soup.select("[class*='status'], [class*='truth-card']")
            or soup.find_all(lambda tag: tag.name == "div" and
                              tag.get("class") and
                              any("post" in c.lower() or "card" in c.lower()
                                  for c in tag.get("class", [])))
        )
        for el in candidates[:limit * 3]:   # over-fetch then filter
            # Try to find the post body inside the candidate
            body_el = (el.select_one(".status-body, .post-body, .truth-body, p.body")
                       or el.find("p"))
            body = body_el.get_text(" ", strip=True) if body_el else el.get_text(" ", strip=True)
            body = body.strip()
            # Filter junk: nav links, very short text, etc.
            if len(body) < 20 or body.lower().startswith(("home", "log in", "sign up")):
                continue
            # Try to find a timestamp
            ts_el = el.find("time") or el.select_one("[datetime]")
            ts = (ts_el.get("datetime") if ts_el and ts_el.has_attr("datetime")
                  else (ts_el.get_text(strip=True) if ts_el else None))
            posts.append({"body": body[:1000], "ts": ts,
                          "favourites": 0, "reblogs": 0})
            if len(posts) >= limit:
                break
        if posts:
            return posts
    except ImportError:
        log.debug("BeautifulSoup not installed — using regex fallback")
    except Exception as e:
        log.debug("BS4 trumpstruth parse failed: %s", e)

    # Strategy 2: regex fallback over <p> tags with substantial text
    p_re = re.compile(r'<p[^>]*>(.*?)</p>', re.DOTALL)
    for m in p_re.finditer(html_text):
        body = _strip_html(m.group(1)).strip()
        if len(body) < 30 or len(body) > 1000:
            continue
        # Skip obvious navigation/footer text
        low = body.lower()
        if any(kw in low for kw in ("copyright", "privacy", "follow us", "sign up")):
            continue
        posts.append({"body": body, "ts": None, "favourites": 0, "reblogs": 0})
        if len(posts) >= limit:
            break

    return posts


def _score_trump_for_universe(universe: List[str]) -> Dict[str, Dict[str, Any]]:
    """Per-ticker contribution from Trump posts in the lookback window."""
    posts = _fetch_trump_posts(limit=30)
    if not posts:
        return {}
    valid = set(t.upper() for t in universe)
    out: Dict[str, Dict[str, Any]] = {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)   # 2-week lookback
    for p in posts:
        ts = None
        if p.get("ts"):
            try:
                ts = datetime.fromisoformat(p["ts"].replace("Z", "+00:00"))
            except Exception:
                ts = None
        if ts and ts < cutoff:
            continue
        body = p.get("body") or ""
        if not body:
            continue
        tickers = _extract_tickers_from_text(body, valid)
        if not tickers:
            continue
        sent = _VADER.polarity_scores(body)["compound"]
        engagement_boost = 1 + math.log1p((p.get("favourites") or 0) + (p.get("reblogs") or 0)) / 10
        # Weight Trump posts heavily — they can move markets
        weight = 1.0 * engagement_boost
        for t in set(tickers):
            d = out.setdefault(t, {"trump_n": 0, "trump_sent_sum": 0.0,
                                   "trump_weight_sum": 0.0, "trump_excerpt": ""})
            d["trump_n"] += 1
            d["trump_sent_sum"] += sent * weight
            d["trump_weight_sum"] += weight
            if not d["trump_excerpt"] and len(body) > 30:
                d["trump_excerpt"] = body[:140]
    # Finalize per-ticker
    for t, d in out.items():
        d["trump_avg_sent"] = (d["trump_sent_sum"] / d["trump_weight_sum"]) if d["trump_weight_sum"] else 0
    return out


# -------- Public API --------------------------------------------------
def run(universe: List[str], top_st_tickers: int = 30) -> pd.DataFrame:
    """Aggregate StockTwits + Trump Truth Social into per-ticker social_score.

    StockTwits is queried only for the top N tickers (by alphabetical order, but
    ideally by mention count from sentiment engine — we use universe order as a proxy).
    """
    valid = [t for t in universe if t and t.isalpha() and len(t) <= 5]
    # Limit StockTwits queries — it's 200 req/hour, we do 30 per run
    st_tickers = valid[:top_st_tickers]
    st_data = _score_stocktwits_for_tickers(st_tickers)
    trump_data = _score_trump_for_universe(valid)

    rows = []
    all_tickers = set(st_data.keys()) | set(trump_data.keys())
    for t in all_tickers:
        st = st_data.get(t, {})
        tr = trump_data.get(t, {})

        # StockTwits component: signed by avg sentiment, scaled by mention count
        st_n = st.get("stocktwits_n", 0)
        st_avg = st.get("stocktwits_avg_sent", 0.0)
        st_component = st_avg * math.log1p(st_n) * 0.5

        # Trump component: signed by avg sentiment, weighted heavily
        tr_n = tr.get("trump_n", 0)
        tr_avg = tr.get("trump_avg_sent", 0.0)
        tr_component = tr_avg * math.log1p(tr_n) * 1.2

        social_score = round(st_component + tr_component, 3)
        rows.append({
            "ticker": t,
            "social_score": social_score,
            "stocktwits_n": st_n,
            "stocktwits_avg_sent": st_avg,
            "stocktwits_n_bull": st.get("stocktwits_n_bull", 0),
            "stocktwits_n_bear": st.get("stocktwits_n_bear", 0),
            "trump_n": tr_n,
            "trump_avg_sent": tr_avg,
            "trump_excerpt": tr.get("trump_excerpt", ""),
        })
    log.info("social engine: %d tickers (st covers %d, trump %d)",
             len(rows), len(st_data), len(trump_data))
    return pd.DataFrame(rows)
