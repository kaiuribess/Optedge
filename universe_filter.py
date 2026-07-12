"""Select a liquid, relevant ticker subset for expensive research engines.

The full universe (~500 tickers + WSB trending) is too large for the slowest
engines (per-ticker SEC fetches, options chain pulls). This module ranks the
universe by a quick liquidity + interest proxy and returns the top-N for the
heavy engines.

Strategy:
  1. Cheap proxy: read cached fundamentals.parquet for market cap (avail every run)
  2. Filter to top-N by market cap (default 300)
  3. Always include the trending tickers supplied by the caller
  4. Always include tickers with prior signal hits (logs/signals_*.parquet)

When no cache exists (first run), passes through full universe.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Set

import pandas as pd

log = logging.getLogger("optedge.uf")
ROOT = Path(__file__).resolve().parent


def _load_recent_market_caps(max_age_hours: int = 48) -> Optional[pd.DataFrame]:
    """Find the most recent fundamentals_*.parquet and return ticker, market_cap."""
    data_dir = ROOT / "data"
    if not data_dir.exists():
        return None
    files = sorted(data_dir.glob("fundamentals_*.parquet"),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        return None
    f = files[0]
    age_hours = (datetime.now().timestamp() - f.stat().st_mtime) / 3600
    if age_hours > max_age_hours:
        return None
    try:
        df = pd.read_parquet(f)
        if "ticker" not in df.columns or "market_cap" not in df.columns:
            return None
        return df[["ticker", "market_cap"]].dropna(subset=["market_cap"])
    except Exception as e:
        log.debug("load fundamentals cache fail: %s", e)
        return None


def _load_prior_signal_tickers(days: int = 7) -> Set[str]:
    """Tickers that have shown up as picks in the past N days — always include."""
    logs_dir = ROOT / "logs"
    if not logs_dir.exists():
        return set()
    cutoff = datetime.now() - timedelta(days=days)
    tickers: Set[str] = set()
    for f in logs_dir.glob("signals_*.parquet"):
        if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            continue
        try:
            df = pd.read_parquet(f, columns=["ticker"])
            tickers.update(df["ticker"].dropna().unique().tolist())
        except Exception:
            continue
    for f in logs_dir.glob("shares_signals_*.parquet"):
        if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            continue
        try:
            df = pd.read_parquet(f, columns=["ticker"])
            tickers.update(df["ticker"].dropna().unique().tolist())
        except Exception:
            continue
    return tickers


def filter_for_heavy_engines(full_universe: List[str], top_n: int = 300,
                              include_trending: List[str] = None,
                              include_priors: bool = True) -> List[str]:
    """Return a subset of the universe optimised for slow per-ticker engines.

    Slot allocation:
      - top_n by market cap (from recent fundamentals cache)
      - ALL WSB trending names
      - ALL prior-signal tickers from past 7 days
    """
    if len(full_universe) <= top_n:
        return full_universe
    keep: Set[str] = set()
    if include_trending:
        keep.update(include_trending)
    if include_priors:
        keep.update(_load_prior_signal_tickers(days=7))
    market_caps = _load_recent_market_caps()
    if market_caps is None or market_caps.empty:
        log.info("universe-filter: no cached market caps — using full universe (%d)",
                 len(full_universe))
        return full_universe
    # Subset to current universe
    market_caps = market_caps[market_caps["ticker"].isin(full_universe)]
    if market_caps.empty:
        return full_universe
    market_caps = market_caps.sort_values("market_cap", ascending=False).head(top_n)
    keep.update(market_caps["ticker"].tolist())
    # Preserve original order for downstream consistency
    out = [t for t in full_universe if t in keep]
    log.info("universe-filter: %d -> %d (incl %d trending, %d priors)",
             len(full_universe), len(out),
             len(include_trending or []),
             len(_load_prior_signal_tickers(days=7)) if include_priors else 0)
    return out
