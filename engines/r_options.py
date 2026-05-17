"""r/options daily-discussion engine.

The existing wsb_trending engine scrapes r/wallstreetbets sticky.
This engine adds r/options sticky (smaller/smarter subreddit, options-focused)
for a different signal mix.

Free, no auth.
"""
from __future__ import annotations
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Dict, List

import pandas as pd

import sys
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider

log = logging.getLogger("optedge.r_options")

SUBS = ["options", "thetagang", "Optionswheel", "PMTraders"]
MAX_COMMENTS_PER_SUB = 200


def _vader(text: str) -> float:
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        sia = SentimentIntensityAnalyzer()
        return sia.polarity_scores(text)["compound"]
    except Exception:
        return 0.0


def _fetch_sticky_comments(sub: str) -> List[Dict]:
    """Fetch the most recent sticky's comment tree from a subreddit."""
    key = f"r_options:{sub}"
    cached = data_provider.cache_get(key, max_age_sec=1800)
    if cached is not None:
        return cached
    # Get the sticky JSON
    url = f"https://www.reddit.com/r/{sub}/about/sticky.json"
    sess = data_provider.get_session()
    headers = {"User-Agent": "optedge-research/0.1"}
    try:
        r = sess.get(url, timeout=15, headers=headers)
        if r.status_code != 200:
            data_provider.cache_put(key, [])
            return []
        data = r.json()
        # Sticky response = list of 2 entries: [post, comments]
        if not isinstance(data, list) or len(data) < 2:
            data_provider.cache_put(key, [])
            return []
        comments = data[1].get("data", {}).get("children", [])
        out = []
        for c in comments[:MAX_COMMENTS_PER_SUB]:
            body = (c.get("data") or {}).get("body", "")
            if not body or body == "[removed]":
                continue
            out.append({"text": body})
        data_provider.cache_put(key, out)
        return out
    except Exception as e:
        log.debug("r/%s sticky fail: %s", sub, e)
        data_provider.cache_put(key, [])
        return []


def _extract_tickers(text: str, valid_universe: set) -> List[str]:
    """Match potential tickers in text."""
    # Cashtags + standalone all-caps 2-5 letter words
    tags = re.findall(r'\$([A-Z]{1,5})\b', text)
    words = re.findall(r'\b([A-Z]{2,5})\b', text)
    found = set(tags + words)
    return [t for t in found if t in valid_universe]


def run(universe: List[str]) -> pd.DataFrame:
    if not universe:
        return pd.DataFrame()
    universe_set = set(t.upper() for t in universe)
    per_ticker: Dict[str, Dict] = {}
    for sub in SUBS:
        comments = _fetch_sticky_comments(sub)
        for c in comments:
            text = c["text"]
            tickers = _extract_tickers(text, universe_set)
            if not tickers:
                continue
            sent = _vader(text)
            for tk in tickers:
                d = per_ticker.setdefault(tk, {"n": 0, "sent_sum": 0.0})
                d["n"] += 1
                d["sent_sum"] += sent
    if not per_ticker:
        log.info("r/options sticky: no tickers found")
        return pd.DataFrame()
    rows = []
    for tk, d in per_ticker.items():
        if d["n"] < 2:  # filter noise
            continue
        avg_sent = d["sent_sum"] / d["n"]
        rows.append({
            "ticker": tk,
            "r_options_score": max(-1.0, min(1.0, avg_sent * (1 + d["n"] / 20))),
            "r_options_n": d["n"],
            "r_options_avg_sent": avg_sent,
        })
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows).sort_values("r_options_n", ascending=False).reset_index(drop=True)
    log.info("r/options sticky: %d tickers with >=2 mentions", len(out))
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run(["SPY", "QQQ", "AAPL", "TSLA", "NVDA"]))
