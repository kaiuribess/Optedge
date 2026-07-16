# Purpose: Score option volume-to-open-interest imbalances.
"""Unusual Options Activity (UOA) — per-ticker signal.

For each ticker, looks across all its tradeable contracts and finds the
strongest volume / open-interest ratio. Ratio > 2 = "unusual" — smart money
is loading up TODAY at that strike.

We surface:
  - uoa_max_ratio: strongest single contract's volume/OI ratio
  - uoa_call_ratio: same restricted to calls only (bullish flow)
  - uoa_put_ratio:  same restricted to puts only (bearish flow / hedge demand)
  - uoa_score: signed score = call_ratio - put_ratio, log-scaled

The score is side-aligned at fusion time (boosts calls when call flow is heavy,
boosts puts when put flow is heavy).

Data source: yfinance chains we already pull in the mispricing engine —
this is essentially free.
"""

from __future__ import annotations

import logging
import math
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("optedge.uoa")


def derive_from_contracts(contracts: pd.DataFrame) -> pd.DataFrame:
    """Produce per-ticker UOA stats from the enriched contracts DataFrame.

    Expected columns: ticker, side, open_interest, volume.
    """
    if contracts is None or contracts.empty:
        return pd.DataFrame(
            columns=[
                "ticker",
                "uoa_score",
                "uoa_max_ratio",
                "uoa_call_ratio",
                "uoa_put_ratio",
                "uoa_call_top_strike",
                "uoa_put_top_strike",
            ]
        )
    c = contracts.copy()
    # Compute ratio (cap denominator at 1 to avoid divide-by-zero)
    c["uoa_ratio"] = c["volume"].fillna(0) / c["open_interest"].clip(lower=1)
    # Only consider contracts with at least some volume and OI for a meaningful ratio
    c = c[(c["volume"].fillna(0) >= 50) & (c["open_interest"].fillna(0) >= 50)]
    if c.empty:
        return pd.DataFrame()

    rows = []
    for ticker, sub in c.groupby("ticker"):
        calls = sub[sub["side"] == "call"]
        puts = sub[sub["side"] == "put"]
        # Top single-contract ratio per side
        call_top = calls["uoa_ratio"].max() if not calls.empty else 0.0
        put_top = puts["uoa_ratio"].max() if not puts.empty else 0.0
        call_strike = (
            calls.loc[calls["uoa_ratio"].idxmax(), "strike"]
            if not calls.empty and call_top > 0
            else None
        )
        put_strike = (
            puts.loc[puts["uoa_ratio"].idxmax(), "strike"]
            if not puts.empty and put_top > 0
            else None
        )
        # Per-ticker score: log-scaled signed difference. ±1 around ratio 2, ±2 around 5.
        signed = call_top - put_top
        score = math.copysign(math.log1p(abs(signed)), signed)
        max_ratio = max(call_top, put_top)
        rows.append(
            {
                "ticker": ticker,
                "uoa_score": round(float(score), 3),
                "uoa_max_ratio": round(float(max_ratio), 2),
                "uoa_call_ratio": round(float(call_top), 2),
                "uoa_put_ratio": round(float(put_top), 2),
                "uoa_call_top_strike": float(call_strike) if call_strike is not None else None,
                "uoa_put_top_strike": float(put_strike) if put_strike is not None else None,
            }
        )
    df = pd.DataFrame(rows)
    log.info(
        "UOA computed: %d tickers, top ratio %.2fx",
        len(df),
        df["uoa_max_ratio"].max() if not df.empty else 0,
    )
    return df
