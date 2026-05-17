"""Analyst Recommendations engine — Finnhub-powered.

For each ticker, pulls:
  - Latest month buy/hold/sell distribution
  - Month-over-month change in strong-buy count (momentum signal)
  - Net analyst score: weighted sum where strongBuy=+2, buy=+1, hold=0,
    sell=-1, strongSell=-2 / total = average sentiment

The "analyst momentum" signal is what we care about most: when strong-buy
count rises month-over-month, that often precedes price movement (analyst
herding effect — once one upgrades, others follow within a few weeks).
"""
from __future__ import annotations
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional

import pandas as pd

import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engines import finnhub_provider as fh

log = logging.getLogger("optedge.analyst")


def _score_one(symbol: str) -> Optional[Dict[str, Any]]:
    """Per-ticker analyst score + momentum."""
    data = fh.get("/stock/recommendation", {"symbol": symbol}, cache_ttl=86400)
    if not data or not isinstance(data, list) or not data:
        return None

    latest = data[0]   # most recent month
    sb = int(latest.get("strongBuy") or 0)
    b = int(latest.get("buy") or 0)
    h = int(latest.get("hold") or 0)
    s = int(latest.get("sell") or 0)
    ss = int(latest.get("strongSell") or 0)
    total = sb + b + h + s + ss
    if total == 0:
        return None

    # Average analyst sentiment: -2 to +2
    avg_score = (sb * 2 + b * 1 + h * 0 + s * -1 + ss * -2) / total

    # Momentum: change in strong buys + buys vs previous month
    momentum = 0
    if len(data) >= 2:
        prev = data[1]
        prev_bullish = int(prev.get("strongBuy") or 0) + int(prev.get("buy") or 0)
        curr_bullish = sb + b
        momentum = curr_bullish - prev_bullish

    # Net analyst factor: avg sentiment + momentum boost
    analyst_score = avg_score + 0.1 * momentum  # 1 added analyst = +0.1 score
    # Clip to [-3, +3]
    analyst_score = max(-3.0, min(3.0, analyst_score))

    return {
        "ticker": symbol,
        "analyst_score": round(analyst_score, 3),
        "analyst_strong_buy": sb,
        "analyst_buy": b,
        "analyst_hold": h,
        "analyst_sell": s,
        "analyst_strong_sell": ss,
        "analyst_total": total,
        "analyst_avg": round(avg_score, 3),
        "analyst_momentum": momentum,
        "analyst_period": latest.get("period"),
    }


def run(universe: List[str], top_n: int = 80, max_workers: int = 6) -> pd.DataFrame:
    """Pull analyst recommendations for top N tickers in the universe.

    Capped at top_n because Finnhub's free tier is 60/min and we want
    headroom for other engines hitting Finnhub.
    """
    targets = list(dict.fromkeys(universe))[:top_n]
    rows = []
    completed = 0
    log.info("analyst: %d tickers (parallel, %d workers)", len(targets), max_workers)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_score_one, t): t for t in targets}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                r = fut.result()
                if r:
                    rows.append(r)
            except Exception as e:
                log.debug("analyst fail %s: %s", t, e)
            completed += 1
            if completed % 20 == 0 or completed == len(targets):
                log.info("[%d/%d]", completed, len(targets))
    df = pd.DataFrame(rows)
    if not df.empty:
        log.info("analyst: %d tickers had data", len(df))
    return df
