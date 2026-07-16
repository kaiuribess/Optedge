# Purpose: WSB-trending ticker discovery.
"""WSB-trending ticker discovery.

Scans Reddit hot/rising/top across r/wallstreetbets, r/options, r/stocks for
the past 24h, extracts ticker mentions, ranks them by (mentions × log(ups+1)),
and returns the top N. Add these to the universe at runtime so the screener
follows what retail is actually trading TODAY.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider  # noqa: E402
from config import USER_AGENT  # noqa: E402

log = logging.getLogger("optedge.wsb")

TRENDING_SUBS = [
    "wallstreetbets",
    "options",
    "stocks",
    "investing",
    "smallstreetbets",
    "pennystocks",
]
SORTS = ["hot", "rising", "new"]

# Regex catches both $TICKER and bare TICKER (1-5 caps)
_TICKER_RE = re.compile(r"\$([A-Z]{1,5})\b|\b([A-Z]{2,5})\b")
_STOPWORDS = {
    # Generic
    "USA",
    "USD",
    "EUR",
    "GDP",
    "CPI",
    "FOMC",
    "FED",
    "SEC",
    "ETF",
    "IPO",
    "API",
    "EOM",
    "TLDR",
    "FYI",
    "IMO",
    "IIRC",
    "DD",
    "DM",
    "PR",
    "HQ",
    "AKA",
    "NSFW",
    "AMA",
    "TIL",
    "CEO",
    "CFO",
    "COO",
    "CTO",
    "VP",
    # Trading slang that matches pattern
    "FOMO",
    "ATM",
    "ITM",
    "OTM",
    "IV",
    "EOD",
    "AH",
    "PM",
    "EPS",
    "YOLO",
    "WSB",
    "HFT",
    "GTFO",
    "LOL",
    "LMAO",
    "WTF",
    "OMG",
    "TBD",
    "AF",
    # Common false positives
    "GO",
    "OF",
    "ON",
    "AT",
    "OR",
    "AND",
    "BUY",
    "SELL",
    "HOLD",
    "LONG",
    "PUT",
    "CALL",
    "NEW",
    "OLD",
    "BIG",
    "ALL",
    "ANY",
}


def _is_valid_ticker(sym: str, valid_set: set[str]) -> bool:
    if not sym or sym in _STOPWORDS:
        return False
    if valid_set and sym not in valid_set:
        return False
    return True


def _extract_tickers(text: str, valid_set: set[str]) -> list[str]:
    if not text:
        return []
    out = []
    for m in _TICKER_RE.finditer(text):
        sym = (m.group(1) or m.group(2) or "").upper()
        if _is_valid_ticker(sym, valid_set):
            out.append(sym)
    return out


def _fetch_listing(sub: str, sort: str, limit: int = 100) -> list[dict]:
    sess = data_provider.get_session()
    url = f"https://www.reddit.com/r/{sub}/{sort}.json?limit={limit}"
    try:
        r = sess.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        if r.status_code != 200:
            log.debug("reddit %s/%s -> %d", sub, sort, r.status_code)
            return []
        return [c["data"] for c in r.json().get("data", {}).get("children", [])]
    except Exception as e:
        log.debug("reddit fetch %s/%s: %s", sub, sort, e)
        return []


def _fetch_comments(sub: str, limit: int = 100) -> list[dict]:
    """Get the most recent comments from a sub. Captures the daily discussion
    thread + comments on hot posts + 'WSB chat'-style talk.

    Reddit's /comments.json endpoint sorts newest-first across the whole sub,
    so we get a real-time pulse of what's being said right now.
    """
    sess = data_provider.get_session()
    url = f"https://www.reddit.com/r/{sub}/comments.json?limit={limit}&sort=new"
    try:
        r = sess.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        if r.status_code != 200:
            log.debug("reddit %s/comments -> %d", sub, r.status_code)
            return []
        return [c["data"] for c in r.json().get("data", {}).get("children", [])]
    except Exception as e:
        log.debug("reddit comments fetch %s: %s", sub, e)
        return []


def _fetch_sticky_comments(sub: str, limit: int = 500) -> list[dict]:
    """Find the daily discussion sticky and pull all its comments in one fetch.

    Reddit's about/sticky.json returns the pinned post (whatever its name is
    today: "What Are Your Moves Today", "Daily Discussion", "After Hours
    Discussion", etc.) — we follow the permalink and pull up to 500 comments.
    """
    sess = data_provider.get_session()
    sticky_url = f"https://www.reddit.com/r/{sub}/about/sticky.json"
    try:
        r = sess.get(sticky_url, headers={"User-Agent": USER_AGENT}, timeout=15)
        if r.status_code != 200:
            log.debug("reddit %s sticky lookup -> %d", sub, r.status_code)
            return []
        # /about/sticky.json returns the post + comments tree directly
        data = r.json()
        if isinstance(data, list) and len(data) >= 2:
            # Standard Reddit thread format: [post listing, comments listing]
            comments_listing = data[1]
        elif isinstance(data, dict):
            comments_listing = data
        else:
            return []
        out = []

        # Recursively flatten the comment tree
        def _walk(node):
            if not isinstance(node, dict):
                return
            children = node.get("data", {}).get("children", [])
            for c in children:
                if c.get("kind") == "t1":
                    cd = c.get("data", {})
                    out.append(cd)
                    # Replies
                    replies = cd.get("replies")
                    if isinstance(replies, dict):
                        _walk(replies)

        _walk(comments_listing)
        # Cap at limit
        if limit and len(out) > limit:
            out = out[:limit]
        log.debug("sticky thread %s yielded %d comments", sub, len(out))
        return out
    except Exception as e:
        log.debug("reddit sticky fetch %s: %s", sub, e)
        return []


def get_trending(valid_universe: list[str], top_n: int = 50, min_mentions: int = 3) -> list[str]:
    """Return up to `top_n` tickers from WSB-style subs that exceed `min_mentions`.

    Restricts matches to `valid_universe` so we don't surface random false
    positives. If you want to discover NEW tickers (not already in your list),
    pass an empty set as valid_universe and trust the regex.
    """
    valid_set = set(t.upper() for t in valid_universe)
    score: dict[str, float] = defaultdict(float)
    mentions: dict[str, int] = defaultdict(int)

    import math

    n_posts = 0
    n_comments = 0
    for sub in TRENDING_SUBS:
        # 1. Posts (titles + selftext) — slower-moving, higher signal-per-mention
        for sort in SORTS:
            posts = _fetch_listing(sub, sort, 100)
            for p in posts:
                text = " ".join([p.get("title") or "", p.get("selftext") or ""])
                ups = max(1, p.get("ups") or 0)
                num_comments = max(1, p.get("num_comments") or 0)
                tickers = _extract_tickers(text, valid_set)
                weight = math.log1p(ups) + 0.3 * math.log1p(num_comments)
                for t in set(tickers):
                    score[t] += weight
                    mentions[t] += 1
                n_posts += 1
            time.sleep(0.4)
        # 2. Comments — the "WSB chat" / daily discussion / hot-post replies.
        # Higher volume, noisier per-message, weighted at 0.3× a post.
        comments = _fetch_comments(sub, 100)
        for c in comments:
            text = c.get("body") or ""
            ups = max(1, c.get("ups") or 0)
            tickers = _extract_tickers(text, valid_set)
            weight = 0.3 * math.log1p(ups)
            for t in set(tickers):
                score[t] += weight
                mentions[t] += 1
            n_comments += 1
        time.sleep(0.4)

        # 3. Sticky deep scan — the daily discussion thread (whatever it's named)
        sticky_comments = _fetch_sticky_comments(sub, 500)
        for c in sticky_comments:
            text = c.get("body") or ""
            ups = max(1, c.get("ups") or 0)
            tickers = _extract_tickers(text, valid_set)
            weight = 0.3 * math.log1p(ups)
            for t in set(tickers):
                score[t] += weight
                mentions[t] += 1
            n_comments += 1
        time.sleep(0.4)

    log.info("wsb scan: %d posts + %d comments (incl sticky) processed", n_posts, n_comments)
    ranked = sorted(score.items(), key=lambda kv: kv[1], reverse=True)
    out = [t for t, s in ranked if mentions[t] >= min_mentions][:top_n]
    log.info("wsb trending (top %d): %s", len(out), out[:20])
    return out


def get_trending_with_metadata(
    valid_universe: list[str], top_n: int = 50, min_mentions: int = 3
) -> list[dict]:
    """Same as get_trending but returns score/mention metadata."""
    valid_set = set(t.upper() for t in valid_universe)
    score: dict[str, float] = defaultdict(float)
    mentions: dict[str, int] = defaultdict(int)
    total_ups: dict[str, int] = defaultdict(int)

    import math

    n_posts = 0
    n_comments = 0
    for sub in TRENDING_SUBS:
        for sort in SORTS:
            posts = _fetch_listing(sub, sort, 100)
            for p in posts:
                text = " ".join([p.get("title") or "", p.get("selftext") or ""])
                ups = max(1, p.get("ups") or 0)
                num_comments = max(1, p.get("num_comments") or 0)
                tickers = _extract_tickers(text, valid_set)
                weight = math.log1p(ups) + 0.3 * math.log1p(num_comments)
                for t in set(tickers):
                    score[t] += weight
                    mentions[t] += 1
                    total_ups[t] += ups
                n_posts += 1
            time.sleep(0.4)
        # Add comments + sticky deep scan for fresh "WSB chat" signal
        comments = _fetch_comments(sub, 100)
        for c in comments:
            text = c.get("body") or ""
            ups = max(1, c.get("ups") or 0)
            tickers = _extract_tickers(text, valid_set)
            weight = 0.3 * math.log1p(ups)
            for t in set(tickers):
                score[t] += weight
                mentions[t] += 1
                total_ups[t] += ups
            n_comments += 1
        sticky_comments = _fetch_sticky_comments(sub, 500)
        for c in sticky_comments:
            text = c.get("body") or ""
            ups = max(1, c.get("ups") or 0)
            tickers = _extract_tickers(text, valid_set)
            weight = 0.3 * math.log1p(ups)
            for t in set(tickers):
                score[t] += weight
                mentions[t] += 1
                total_ups[t] += ups
            n_comments += 1
        time.sleep(0.4)

    log.info("wsb metadata scan: %d posts + %d comments (incl sticky)", n_posts, n_comments)
    ranked = sorted(score.items(), key=lambda kv: kv[1], reverse=True)
    out = []
    for t, s in ranked:
        if mentions[t] < min_mentions:
            continue
        out.append({"ticker": t, "score": s, "mentions": mentions[t], "ups": total_ups[t]})
        if len(out) >= top_n:
            break
    return out
