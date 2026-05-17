"""SEC 8-K buyback announcement scanner — v20.1.

Pulls recent 8-K filings from EDGAR Full-Text Search and flags any mentioning
a share-repurchase authorization.

v20.1 fix: EFTS returns tickers in `display_names` field (formatted as
"Company Name (TICKER, TICKER-WT) (CIK 0001234567)"), NOT in the `tickers`
field which is usually empty. Parser updated.
"""
from __future__ import annotations
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Set

import pandas as pd

import sys
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider

log = logging.getLogger("optedge.buybacks")

SEC_HEADERS = {
    "User-Agent": "optedge-research/0.1 (research@optedge.local)",
    "Accept": "application/json, text/plain, */*",
}

BUYBACK_PHRASES = [
    "repurchase program",
    "share repurchase",
    "stock repurchase",
    "buyback program",
    "repurchase authorization",
]

# Extract tickers from display_names like:
#   "Apple Inc.  (AAPL, AAPL-PA)  (CIK 0000320193)"
#   "Vistance Networks, Inc.  (VISN)  (CIK 0001517228)"
# We want the first paren group (which holds tickers), skipping the CIK group.
_TICKER_PAREN_RE = re.compile(r'\(([A-Z][A-Z0-9\-,\s\.]*)\)\s*\(CIK')


def _extract_tickers(display_names) -> Set[str]:
    """Pull ticker symbols from EFTS display_names list."""
    tickers: Set[str] = set()
    if not display_names:
        return tickers
    if isinstance(display_names, str):
        display_names = [display_names]
    for dn in display_names:
        if not isinstance(dn, str):
            continue
        m = _TICKER_PAREN_RE.search(dn)
        if not m:
            continue
        # Inside, split on comma; first token of each is the ticker
        raw = m.group(1)
        for part in raw.split(","):
            tk = part.strip().split("-")[0].split(".")[0].strip()
            if tk and tk.isalpha() and 1 <= len(tk) <= 5:
                tickers.add(tk)
    return tickers


def _edgar_search(query: str, days_back: int = 14) -> List[Dict]:
    """EDGAR full-text search for 8-K filings containing `query`."""
    date_to = datetime.utcnow().strftime("%Y-%m-%d")
    date_from = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    url = (
        f"https://efts.sec.gov/LATEST/search-index?q=%22{query.replace(' ', '+')}%22"
        f"&dateRange=custom&startdt={date_from}&enddt={date_to}&forms=8-K"
    )
    key = f"edgar_buyback:{query}:{date_from}"
    cached = data_provider.cache_get(key, max_age_sec=6 * 3600)
    if cached is not None:
        return cached
    try:
        import requests
        r = requests.get(url, headers=SEC_HEADERS, timeout=20)
        if r.status_code != 200:
            log.debug("efts %s -> %d", query, r.status_code)
            return []
        data = r.json()
        hits = data.get("hits", {}).get("hits", [])
        out = []
        for h in hits[:100]:
            s = h.get("_source", {})
            out.append({
                "display_names": s.get("display_names", []),
                "tickers": s.get("tickers", []),
                "file_date": s.get("file_date", ""),
                "form": s.get("form", ""),
                "ciks": s.get("ciks", []),
            })
        data_provider.cache_put(key, out)
        return out
    except Exception as e:
        log.debug("efts %s: %s", query, e)
        return []


def run(universe: List[str]) -> pd.DataFrame:
    if not universe:
        return pd.DataFrame()
    universe_set = {t.upper() for t in universe}

    all_filings = []
    for phrase in BUYBACK_PHRASES:
        hits = _edgar_search(phrase, days_back=14)
        all_filings.extend(hits)
    if not all_filings:
        log.info("buybacks: no 8-K mentions found (EFTS empty or blocked)")
        return pd.DataFrame()

    # Dedup by (ticker, date) and aggregate per ticker
    seen = set()
    ticker_scores: Dict[str, Dict] = {}
    n_hits_seen = len(all_filings)
    n_matched = 0
    for f in all_filings:
        # Prefer EFTS tickers field if non-empty, else parse from display_names
        tickers = set(t.upper() for t in (f.get("tickers") or []) if isinstance(t, str))
        if not tickers:
            tickers = _extract_tickers(f.get("display_names"))
        date = f.get("file_date", "")
        for tk in tickers:
            if tk not in universe_set:
                continue
            n_matched += 1
            k = (tk, date)
            if k in seen:
                continue
            seen.add(k)
            entry = ticker_scores.setdefault(tk, {
                "buyback_score": 0.0, "buyback_dates": [], "n_mentions": 0,
            })
            entry["buyback_score"] = max(entry["buyback_score"], 0.5)
            entry["buyback_dates"].append(date)
            entry["n_mentions"] += 1
            # Multiple distinct filing dates suggests REAL active buyback campaign
            if len(set(entry["buyback_dates"])) >= 2:
                entry["buyback_score"] = min(1.0, entry["buyback_score"] + 0.3)
    if not ticker_scores:
        log.info("buybacks: %d hits, 0 matched universe (parsed %d filings, "
                 "no overlap)", n_hits_seen, n_hits_seen)
        return pd.DataFrame()
    rows = [{
        "ticker": tk,
        "buyback_score": d["buyback_score"],
        "buyback_date_latest": max(d["buyback_dates"]) if d["buyback_dates"] else "",
        "buyback_n_filings": d["n_mentions"],
    } for tk, d in ticker_scores.items()]
    out = pd.DataFrame(rows).sort_values("buyback_score", ascending=False).reset_index(drop=True)
    log.info("buybacks: %d hits -> %d tickers matched universe", n_hits_seen, len(out))
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run(["AAPL", "MSFT", "NVDA", "META", "GOOGL", "JPM", "AVGO", "ORCL", "AUB"]))
