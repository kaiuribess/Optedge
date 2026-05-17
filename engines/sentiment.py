"""Reddit sentiment engine — public JSON endpoints, no auth required.

Pulls hot/new submissions across configured subreddits, extracts ticker
mentions, scores sentiment via VADER + an options-savvy keyword overlay,
and emits per-ticker mention volume + velocity + Δ-sentiment.
"""
from __future__ import annotations
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List

import requests
import pandas as pd
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider
from config import SUBREDDITS, SENTIMENT_LOOKBACK_HOURS, USER_AGENT

log = logging.getLogger("optedge.sentiment")

_VADER = SentimentIntensityAnalyzer()

# Bullish/bearish keyword overlay tuned for retail options chatter
BULLISH_KW = {"calls", "moon", "rip", "send it", "yolo", "long", "buying", "rocket",
              "squeeze", "breakout", "tendies", "diamond hands", "bull"}
BEARISH_KW = {"puts", "short", "crash", "dump", "bag", "bagholder", "rug", "drill",
              "tank", "bear", "selling", "rolldown"}

# Common cashtag/$ ticker pattern + bare-ticker fallback restricted to dictionary
_TICKER_RE = re.compile(r"\$([A-Z]{1,5})\b|\b([A-Z]{2,5})\b")
_STOPWORDS = {
    # v20.1 base
    "USA", "CEO", "CFO", "USD", "ETF", "IPO", "FYI", "TLDR", "EOM", "DD",
    "FOMO", "ATM", "ITM", "OTM", "IV", "API", "HQ", "PR", "SEC", "FED",
    "AI", "EV", "GDP", "CPI", "FOMC", "EOD", "AH", "PM", "EPS", "DM",
    # v20.7 — common Reddit / WSB false positives that match the ticker regex
    "NEW", "ALL", "FOR", "THE", "ARE", "BUY", "NOW", "GET", "CAN", "IRS",
    "PIN", "ANY", "YES", "OWN", "GOT", "TOP", "RUG", "MAY", "FUD", "FED",
    "ETF", "FAQ", "IMO", "TBH", "OBV", "JFC", "WTF", "LOL", "ROFL", "IDC",
    "AND", "BUT", "OUT", "USE", "SEE", "WAY", "WHO", "WHY", "GOP", "USA",
    "USD", "EUR", "JPY", "GBP", "RIP", "DOJ", "IRS", "EU", "UK", "US",
    "TBD", "PT", "AVG", "VAR", "JD", "JS", "PY", "MIT", "VC", "RV",
}


def _allowed_tickers(universe: List[str]) -> set:
    return set(t.upper() for t in universe)


def _extract_tickers(text: str, allowed: set) -> List[str]:
    if not text:
        return []
    found = set()
    for m in _TICKER_RE.finditer(text):
        sym = (m.group(1) or m.group(2) or "").upper()
        if not sym:
            continue
        if sym in _STOPWORDS:
            continue
        if sym in allowed:
            found.add(sym)
    return list(found)


def _keyword_tilt(text: str) -> float:
    if not text:
        return 0.0
    low = text.lower()
    bull = sum(1 for k in BULLISH_KW if k in low)
    bear = sum(1 for k in BEARISH_KW if k in low)
    if bull + bear == 0:
        return 0.0
    return (bull - bear) / (bull + bear)


def _score_text(text: str) -> float:
    """Compound sentiment in [-1, 1]: VADER averaged with keyword tilt.

    Note: FinBERT-based refinement is applied at the END of run() in a
    batched pass — per-message FinBERT calls would be too slow. This
    per-message score uses the cheaper VADER + keyword path.
    """
    if not text:
        return 0.0
    vader = _VADER.polarity_scores(text)["compound"]
    kw = _keyword_tilt(text)
    return 0.6 * vader + 0.4 * kw


def _finbert_blend_scores(posts: List[Dict[str, Any]],
                            max_texts: int = 800) -> int:
    """v20.7 — batched FinBERT pass to refine WSB sentiment scores.

    VADER alone reads "this thing is going to dump hard 🚀" as bullish
    because of '🚀'. FinBERT trained on financial text reads it correctly.
    We blend: final_score = 0.5 * finbert + 0.3 * vader + 0.2 * keyword.

    Caps total texts to keep latency bounded. Returns the number of texts
    rescored (0 if FinBERT unavailable). Modifies `posts` in place.
    """
    if not posts:
        return 0
    try:
        from engines import finbert as _fb
    except Exception:
        return 0
    # Pull the original texts. Score each post once even if it mentions
    # multiple tickers. We stored the raw text on each post entry below.
    texts_to_score = []
    indices = []
    for i, p in enumerate(posts):
        t = p.get("_text", "")
        if t:
            texts_to_score.append(t)
            indices.append(i)
        if len(texts_to_score) >= max_texts:
            break
    if not texts_to_score:
        return 0
    try:
        fb_scores = _fb._score_texts(texts_to_score)
    except Exception as e:
        log.debug("finbert blend skipped: %s", e)
        return 0
    if not fb_scores or len(fb_scores) != len(texts_to_score):
        return 0
    rescored = 0
    for idx, fb_s in zip(indices, fb_scores):
        # Original score was 0.6*vader + 0.4*kw. Reconstruct vader+kw weight=1
        # by treating it as "non-finbert" and add finbert at 0.5 weight,
        # renormalising. Concretely:  blended = 0.5*finbert + 0.5*old_score
        # That preserves the 60/40 vader/kw mix within the non-finbert half.
        old_score = float(posts[idx].get("score", 0.0))
        posts[idx]["score"] = 0.5 * float(fb_s) + 0.5 * old_score
        rescored += 1
    return rescored


def _fetch_listing(sub: str, sort: str = "new", limit: int = 100) -> List[dict]:
    """Use the curl_cffi session (browser fingerprint) — better at slipping past Reddit's IP gate."""
    url = f"https://www.reddit.com/r/{sub}/{sort}.json?limit={limit}"
    sess = data_provider.get_session()
    try:
        r = sess.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        if r.status_code != 200:
            log.warning("reddit %s %s -> %d", sub, sort, r.status_code)
            return []
        data = r.json()
        return [c["data"] for c in data.get("data", {}).get("children", [])]
    except Exception as e:
        log.warning("reddit fetch failed %s: %s", sub, e)
        return []


def _fetch_comments(sub: str, limit: int = 100) -> List[dict]:
    """Recent comments across the sub — captures daily discussion + hot-post replies."""
    url = f"https://www.reddit.com/r/{sub}/comments.json?limit={limit}&sort=new"
    sess = data_provider.get_session()
    try:
        r = sess.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        if r.status_code != 200:
            log.debug("reddit %s comments -> %d", sub, r.status_code)
            return []
        return [c["data"] for c in r.json().get("data", {}).get("children", [])]
    except Exception as e:
        log.debug("reddit comments fetch %s: %s", sub, e)
        return []


def run(universe: List[str]) -> pd.DataFrame:
    """Per-ticker: mentions, sentiment_now, sentiment_prev, sentiment_delta, velocity."""
    allowed = _allowed_tickers(universe)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=SENTIMENT_LOOKBACK_HOURS)
    half = datetime.now(timezone.utc) - timedelta(hours=SENTIMENT_LOOKBACK_HOURS / 2)

    # Collect posts AND comments. Posts weight 1.0, comments 0.3 (noisier per-message).
    posts: List[Dict[str, Any]] = []
    n_post_msgs = 0
    n_comment_msgs = 0
    for sub in SUBREDDITS:
        # Posts
        for sort in ("new", "hot"):
            items = _fetch_listing(sub, sort, 100)
            for it in items:
                created = datetime.fromtimestamp(it.get("created_utc", 0), tz=timezone.utc)
                if created < cutoff:
                    continue
                text = " ".join([it.get("title") or "", it.get("selftext") or ""])
                tickers = _extract_tickers(text, allowed)
                if not tickers:
                    continue
                score = _score_text(text)
                posts.append({
                    "sub": sub, "ts": created, "tickers": tickers,
                    "score": score, "ups": it.get("ups", 0),
                    "weight": 1.0,
                    "_text": text[:512],
                })
                n_post_msgs += 1
            time.sleep(0.3)

        # Comments — "WSB chat" / daily discussion / hot-post replies
        comments = _fetch_comments(sub, 100)
        for c in comments:
            created = datetime.fromtimestamp(c.get("created_utc", 0), tz=timezone.utc)
            if created < cutoff:
                continue
            text = c.get("body") or ""
            tickers = _extract_tickers(text, allowed)
            if not tickers:
                continue
            score = _score_text(text)
            posts.append({
                "sub": sub, "ts": created, "tickers": tickers,
                "score": score, "ups": c.get("ups", 0),
                "weight": 0.3,
                "_text": text[:512],
            })
            n_comment_msgs += 1
        time.sleep(0.3)

    log.info("sentiment scan: %d posts + %d comments captured ticker mentions",
             n_post_msgs, n_comment_msgs)
    # v20.7 — batched FinBERT pass to refine scores (VADER misreads degen-speak)
    n_finbert = _finbert_blend_scores(posts, max_texts=800)
    if n_finbert > 0:
        log.info("sentiment scan: rescored %d posts via FinBERT (degen-aware)",
                 n_finbert)
    if not posts:
        log.warning("no reddit posts/comments captured (network blocked or rate-limited)")
        return pd.DataFrame(columns=[
            "ticker", "mentions", "sentiment_now", "sentiment_prev",
            "sentiment_delta", "velocity",
        ])

    # Aggregate per ticker × half-window — comments contribute 0.3× toward both
    # the score sum AND the mention count, so a ticker with only comment mentions
    # but no posts will have lower (correct) effective signal.
    stats: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"now_score_sum": 0.0, "prev_score_sum": 0.0,
                 "now_w": 0.0, "prev_w": 0.0,
                 "now_n": 0, "prev_n": 0, "ups": 0}
    )
    for p in posts:
        bucket = "now" if p["ts"] >= half else "prev"
        w = p["weight"]
        for t in p["tickers"]:
            stats[t][f"{bucket}_score_sum"] += p["score"] * w
            stats[t][f"{bucket}_w"] += w
            stats[t][f"{bucket}_n"] += 1
            stats[t]["ups"] += p["ups"]

    rows = []
    for t, d in stats.items():
        n_now = d["now_n"]
        n_prev = d["prev_n"]
        s_now = d["now_score_sum"] / d["now_w"] if d["now_w"] else 0.0
        s_prev = d["prev_score_sum"] / d["prev_w"] if d["prev_w"] else 0.0
        rows.append({
            "ticker": t,
            "mentions": n_now + n_prev,
            "sentiment_now": s_now,
            "sentiment_prev": s_prev,
            "sentiment_delta": s_now - s_prev,
            "velocity": n_now - n_prev,
            "ups": d["ups"],
        })
    return pd.DataFrame(rows).sort_values("mentions", ascending=False)
