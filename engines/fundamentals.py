# Purpose: Fundamentals engine — yfinance-based, with caching.
"""Fundamentals engine — yfinance-based, with caching.

Per ticker: revenue growth, gross/operating margin, EPS trend, P/E, EV/EBITDA,
P/S, FCF yield, next earnings date, classification.
"""

from __future__ import annotations

import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider  # noqa: E402
from config import WORKERS_FUNDAMENTALS  # noqa: E402

log = logging.getLogger("optedge.fundamentals")


def _classify(rev_growth, op_margin, pe) -> str:
    if op_margin is not None and op_margin < 0:
        if pe is None or pe < 0:
            return "distressed"
        return "speculative"
    if rev_growth is not None and rev_growth > 0.20:
        return "growth"
    if pe is not None and 0 < pe < 15:
        return "value"
    if pe is None or pe < 0:
        return "speculative"
    return "core"


def _fund_score(rev_growth, op_margin, pe, ps, fcf_yield) -> float:
    """Bullish-tilt score in roughly [-2, +2]."""
    s = 0.0
    if rev_growth is not None:
        s += max(-1.0, min(1.0, rev_growth / 0.30))
    if op_margin is not None:
        s += max(-1.0, min(1.0, op_margin / 0.20))
    if pe is not None and pe > 0:
        s += max(-1.0, min(1.0, (20 - pe) / 20))
    if fcf_yield is not None:
        s += max(-1.0, min(1.0, fcf_yield / 0.08))
    return s


def _per_ticker(t: str) -> dict[str, Any]:
    info = data_provider.get_fundamentals(t)
    rev_growth = info.get("revenueGrowth")
    gross_margin = info.get("grossMargins")
    op_margin = info.get("operatingMargins")
    pe = info.get("trailingPE")
    fwd_pe = info.get("forwardPE")
    ps = info.get("priceToSalesTrailing12Months")
    ev_ebitda = info.get("enterpriseToEbitda")
    fcf = info.get("freeCashflow")
    mcap = info.get("marketCap")
    fcf_yield = (fcf / mcap) if (fcf and mcap and mcap > 0) else None
    earnings_ts = info.get("earningsTimestamp") or info.get("earningsTimestampStart")
    earnings_date = None
    if earnings_ts:
        try:
            earnings_date = pd.to_datetime(earnings_ts, unit="s").date().isoformat()
        except Exception:
            earnings_date = None

    return {
        "ticker": t,
        "rev_growth": rev_growth,
        "gross_margin": gross_margin,
        "op_margin": op_margin,
        "pe": pe,
        "fwd_pe": fwd_pe,
        "ps": ps,
        "ev_ebitda": ev_ebitda,
        "fcf_yield": fcf_yield,
        "market_cap": mcap,
        "earnings_date": earnings_date,
        "classification": _classify(rev_growth, op_margin, pe),
        "fund_score": _fund_score(rev_growth, op_margin, pe, ps, fcf_yield),
    }


def run(universe: list[str], max_workers: int = None) -> pd.DataFrame:
    """Parallel per-ticker processing. yfinance fundamentals are cached for 24h,
    so re-runs are much faster than the first run."""
    workers = max_workers or WORKERS_FUNDAMENTALS
    rows = []
    completed = 0
    log.info("fundamentals for %d tickers (parallel, %d workers)", len(universe), workers)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_per_ticker, t): t for t in universe}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                rows.append(fut.result())
            except Exception as e:
                log.debug("fundamentals fail %s: %s", t, e)
            completed += 1
            if completed % 50 == 0 or completed == len(universe):
                log.info("[%d/%d]", completed, len(universe))
    return pd.DataFrame(rows)
