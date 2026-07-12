# Purpose: Put/Call Ratio engine — derived from chains we already pull.
"""Put/Call Ratio engine — derived from chains we already pull.

For each ticker, computes:
  - pc_vol_ratio: total put volume / total call volume across all contracts
  - pc_oi_ratio:  total put OI / total call OI

Interpretation (note: contrarian at extremes):
  - pc_vol > 1.5 = heavy put buying = bearish positioning, OFTEN contrarian bullish
  - pc_vol < 0.5 = heavy call buying = bullish positioning, can be contrarian bearish
  - 0.7-1.2 = neutral

We score it as a CONTRARIAN signal: extreme P/C → bullish bias (fade the crowd).
This is well-documented behavior; retail piles into puts at bottoms, calls at tops.
"""
from __future__ import annotations
import logging
import math

import pandas as pd

log = logging.getLogger("optedge.put_call")


def _score(pc_vol: float, pc_oi: float) -> float:
    """Contrarian score: high P/C = bullish (crowd is wrong at extremes).

    Returns score in roughly [-1.5, +1.5].
    """
    if pc_vol is None or pd.isna(pc_vol):
        return 0.0
    # Center on 1.0 (balanced). Above = bearish crowd → contrarian bullish.
    deviation = pc_vol - 1.0
    # Map: P/C of 2.5 → +1.5 (very bullish contrarian); P/C of 0.3 → -1.0
    score = math.copysign(math.log1p(abs(deviation)) * 1.5, deviation)
    # Confirm with OI: if both vol AND oi are high P/C, signal is stronger
    if pc_oi is not None and not pd.isna(pc_oi):
        oi_deviation = pc_oi - 1.0
        if (deviation > 0 and oi_deviation > 0) or (deviation < 0 and oi_deviation < 0):
            score *= 1.2          # confirmation amplifies
    return round(max(-1.5, min(1.5, score)), 3)


def derive_from_contracts(contracts: pd.DataFrame) -> pd.DataFrame:
    """Compute put/call ratios per ticker from already-fetched chains."""
    if contracts is None or contracts.empty:
        return pd.DataFrame(columns=["ticker", "pc_vol_ratio", "pc_oi_ratio", "pc_score"])
    c = contracts.copy()
    rows = []
    for ticker, sub in c.groupby("ticker"):
        calls = sub[sub["side"] == "call"]
        puts = sub[sub["side"] == "put"]
        c_vol = float(calls["volume"].fillna(0).sum())
        p_vol = float(puts["volume"].fillna(0).sum())
        c_oi = float(calls["open_interest"].fillna(0).sum())
        p_oi = float(puts["open_interest"].fillna(0).sum())
        # Need at least some flow on both sides for a meaningful ratio
        if c_vol < 100 or p_vol < 100:
            continue
        pc_vol = round(p_vol / max(c_vol, 1), 3)
        pc_oi = round(p_oi / max(c_oi, 1), 3) if c_oi >= 100 else None
        rows.append({
            "ticker": ticker,
            "pc_vol_ratio": pc_vol,
            "pc_oi_ratio": pc_oi,
            "pc_score": _score(pc_vol, pc_oi),
            "pc_call_vol": int(c_vol),
            "pc_put_vol": int(p_vol),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        log.info("put_call: %d tickers with measurable P/C, median pc_vol=%.2f",
                 len(df), df["pc_vol_ratio"].median())
    return df
