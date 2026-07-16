# Purpose: Earnings engine — yfinance calendar.
"""Earnings engine — yfinance calendar.

Per ticker:
  - next earnings date + days until
  - last-quarter EPS surprise (actual vs estimate)
  - earnings_score: positive when an imminent catalyst aligns with a directional view

The signal feeds the fusion layer as a directional boost — bullish factors get
amplified when earnings is 7-21 days out (catalyst window), dampened when
earnings is < 3 days out (IV crush risk).
"""

from __future__ import annotations

import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider  # noqa: E402
from config import EARNINGS_LOOKAHEAD_DAYS, WORKERS_EARNINGS  # noqa: E402

log = logging.getLogger("optedge.earnings")


def _fetch(ticker: str) -> dict[str, Any]:
    """Pull earnings details from yfinance with caching."""
    cache_key = f"earnings:{ticker}"
    cached = data_provider.cache_get(cache_key, max_age_sec=43200)  # 12h cache
    if cached is not None:
        return cached

    try:
        tk = data_provider.yf_ticker(ticker)
        info = getattr(tk, "info", {}) or {}

        # Next earnings date
        ts = info.get("earningsTimestamp") or info.get("earningsTimestampStart")
        next_date = None
        if ts:
            try:
                next_date = datetime.fromtimestamp(ts, tz=UTC).date().isoformat()
            except Exception:
                pass

        # EPS estimate / actual
        eps_est = info.get("earningsAverage") or info.get("epsCurrentYear")
        eps_actual = info.get("trailingEps")

        # Last-quarter surprise via earnings_history (when available)
        last_surprise = None
        try:
            hist = tk.earnings_history
            if hist is not None and not hist.empty:
                last_row = hist.iloc[-1]
                actual = last_row.get("epsActual")
                est = last_row.get("epsEstimate")
                if actual is not None and est is not None and est != 0:
                    last_surprise = float((actual - est) / abs(est))
        except Exception:
            pass

        out = {
            "ticker": ticker,
            "next_earnings_date": next_date,
            "eps_est": eps_est,
            "eps_actual": eps_actual,
            "last_eps_surprise_pct": last_surprise,
        }
        data_provider.cache_put(cache_key, out)
        return out
    except Exception as e:
        log.debug("earnings fetch fail %s: %s", ticker, e)
        return {
            "ticker": ticker,
            "next_earnings_date": None,
            "eps_est": None,
            "eps_actual": None,
            "last_eps_surprise_pct": None,
        }


def _score(row: dict[str, Any]) -> float:
    """Earnings catalyst score in roughly [-1, +1].

    +1: imminent earnings + recent positive surprise (bullish catalyst window)
    -1: imminent earnings + recent negative surprise (bearish catalyst window)
     0: no upcoming earnings or distant
    """
    next_date = row.get("next_earnings_date")
    if not next_date:
        return 0.0
    try:
        edate = datetime.fromisoformat(next_date).replace(tzinfo=UTC)
    except Exception:
        return 0.0
    dte = (edate - datetime.now(UTC)).days
    if dte < 0 or dte > EARNINGS_LOOKAHEAD_DAYS:
        return 0.0

    # Catalyst window strongest 7-21 days out, weaker either side
    if dte < 3:
        window_weight = 0.3  # too close to earnings — IV crush dominates
    elif dte < 7:
        window_weight = 0.6
    elif dte < 21:
        window_weight = 1.0  # sweet spot
    else:
        window_weight = 0.5

    surprise = row.get("last_eps_surprise_pct")
    direction = 0.0
    if surprise is not None:
        # Recent beats persist via PEAD; recent misses hurt
        direction = max(-1.0, min(1.0, surprise * 5))  # 20% surprise → ±1

    return round(window_weight * direction, 3)


def _process_ticker(t: str) -> dict[str, Any]:
    fetched = _fetch(t)
    fetched["earnings_score"] = _score(fetched)
    next_date = fetched.get("next_earnings_date")
    if next_date:
        try:
            edate = datetime.fromisoformat(next_date).replace(tzinfo=UTC)
            fetched["days_to_earnings"] = (edate - datetime.now(UTC)).days
        except Exception:
            fetched["days_to_earnings"] = None
    else:
        fetched["days_to_earnings"] = None

    # Post-earnings window flag: True if last earnings was within the trailing
    # 7 days. Used by fusion to boost the news_aligned signal — fresh post-call
    # commentary captures the transcript story without us scraping transcripts directly.
    fetched["post_earnings_window"] = False
    try:
        # Many fields aren't populated; fallback to inferring from next_date in the past
        if fetched.get("days_to_earnings") is not None and -7 <= fetched["days_to_earnings"] <= 0:
            fetched["post_earnings_window"] = True
    except Exception:
        pass
    return fetched


def run(universe: list[str], max_workers: int = None) -> pd.DataFrame:
    workers = max_workers or WORKERS_EARNINGS
    rows = []
    completed = 0
    log.info("earnings for %d tickers (parallel, %d workers)", len(universe), workers)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_process_ticker, t): t for t in universe}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                rows.append(fut.result())
            except Exception as e:
                log.debug("earnings fail %s: %s", t, e)
            completed += 1
            if completed % 50 == 0 or completed == len(universe):
                log.info("[%d/%d]", completed, len(universe))
    return pd.DataFrame(rows)
