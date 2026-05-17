"""Futures engine — equity index, commodity, and crypto futures.

Free via yfinance using continuous-contract symbols (`ES=F`, `NQ=F`, etc.).
For each contract:
  - spot / front-month last
  - 5-day, 20-day, 60-day momentum
  - volatility regime (HV20)
  - 52-week range position
  - directional score: trend × momentum × value-vs-range

Output is bullish-tilted scores in roughly [-2, +2]. The fusion layer ranks
the most bullish (long futures plays) and most bearish (long puts on the
ETF proxy — e.g. SPY puts as a way to express short S&P 500 view).
"""
from __future__ import annotations
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional

import numpy as np
import pandas as pd

import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider
from utils import safe_float

log = logging.getLogger("optedge.futures")


# Default futures universe — wide and free via yfinance
FUTURES = [
    # Equity index
    {"symbol": "ES=F", "name": "S&P 500 E-mini", "etf": "SPY", "kind": "equity"},
    {"symbol": "NQ=F", "name": "Nasdaq-100 E-mini", "etf": "QQQ", "kind": "equity"},
    {"symbol": "YM=F", "name": "Dow E-mini", "etf": "DIA", "kind": "equity"},
    {"symbol": "RTY=F", "name": "Russell 2000 E-mini", "etf": "IWM", "kind": "equity"},
    # Volatility / treasury
    {"symbol": "^VIX", "name": "CBOE Volatility", "etf": "VXX", "kind": "vol"},
    {"symbol": "ZB=F", "name": "30Y Treasury Bond", "etf": "TLT", "kind": "bond"},
    {"symbol": "ZN=F", "name": "10Y Treasury Note", "etf": "IEF", "kind": "bond"},
    # Commodities
    {"symbol": "GC=F", "name": "Gold", "etf": "GLD", "kind": "commodity"},
    {"symbol": "SI=F", "name": "Silver", "etf": "SLV", "kind": "commodity"},
    {"symbol": "CL=F", "name": "Crude Oil WTI", "etf": "USO", "kind": "commodity"},
    {"symbol": "NG=F", "name": "Natural Gas", "etf": "UNG", "kind": "commodity"},
    {"symbol": "HG=F", "name": "Copper", "etf": "CPER", "kind": "commodity"},
    {"symbol": "PL=F", "name": "Platinum", "etf": "PPLT", "kind": "commodity"},
    {"symbol": "ZC=F", "name": "Corn", "etf": "CORN", "kind": "agri"},
    {"symbol": "ZS=F", "name": "Soybeans", "etf": "SOYB", "kind": "agri"},
    {"symbol": "ZW=F", "name": "Wheat", "etf": "WEAT", "kind": "agri"},
    # Currency
    {"symbol": "DX=F", "name": "US Dollar Index", "etf": "UUP", "kind": "fx"},
    # Crypto
    {"symbol": "BTC=F", "name": "Bitcoin", "etf": "IBIT", "kind": "crypto"},
    {"symbol": "ETH=F", "name": "Ether", "etf": "ETHA", "kind": "crypto"},
]


def _hv(close: pd.Series, n: int) -> Optional[float]:
    if close is None or len(close) < n + 1:
        return None
    rets = np.log(close / close.shift(1)).dropna().tail(n)
    if rets.empty:
        return None
    return float(rets.std() * math.sqrt(252))


def _process(meta: Dict[str, Any]) -> Dict[str, Any]:
    sym = meta["symbol"]
    try:
        h = data_provider.get_history(sym, period="1y")
        if h is None or h.empty or "Close" not in h.columns:
            return {**meta, "spot": None, "ret_5d": None, "ret_20d": None,
                    "ret_60d": None, "hv20": None, "range_pos": None,
                    "futures_score": 0.0}
        close = h["Close"].dropna()
        if close.empty:
            return {**meta, "futures_score": 0.0}
        spot = float(close.iloc[-1])
        ret_5d = float(close.iloc[-1] / close.iloc[-6] - 1) if len(close) > 6 else None
        ret_20d = float(close.iloc[-1] / close.iloc[-21] - 1) if len(close) > 21 else None
        ret_60d = float(close.iloc[-1] / close.iloc[-61] - 1) if len(close) > 61 else None
        hv20 = _hv(close, 20)

        # 52-week range position
        last = close.tail(252) if len(close) > 252 else close
        hi, lo = float(last.max()), float(last.min())
        range_pos = (spot - lo) / (hi - lo) if hi > lo else 0.5

        # Directional score
        score = 0.0
        if ret_20d is not None:
            score += max(-1.0, min(1.0, ret_20d * 5))   # +1 at 20%
        if ret_5d is not None:
            score += max(-0.5, min(0.5, ret_5d * 10))   # +0.5 at 5%
        if range_pos < 0.25:
            score += 0.3                                # mean-reversion bias near lows
        elif range_pos > 0.85:
            score -= 0.2                                # caution near highs
        # VIX inverts: low VIX = bullish for equities (handled in fusion via macro),
        # but VIX itself trending up is its own signal. Keep raw direction.

        return {
            **meta,
            "spot": round(spot, 2),
            "ret_5d": round(ret_5d, 4) if ret_5d is not None else None,
            "ret_20d": round(ret_20d, 4) if ret_20d is not None else None,
            "ret_60d": round(ret_60d, 4) if ret_60d is not None else None,
            "hv20": round(hv20, 4) if hv20 else None,
            "range_pos": round(range_pos, 3),
            "futures_score": round(score, 3),
        }
    except Exception as e:
        log.debug("futures fail %s: %s", sym, e)
        return {**meta, "futures_score": 0.0}


def run(max_workers: int = 6) -> pd.DataFrame:
    log.info("futures: %d contracts (parallel, %d workers)", len(FUTURES), max_workers)
    rows = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_process, m): m for m in FUTURES}
        for fut in as_completed(futures):
            try:
                rows.append(fut.result())
            except Exception as e:
                log.debug("futures worker fail: %s", e)
    df = pd.DataFrame(rows).sort_values("futures_score", ascending=False).reset_index(drop=True)
    return df
