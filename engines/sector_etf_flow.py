# Purpose: Estimate sector momentum from ETF price and volume.
"""Sector ETF flow — which sectors are seeing institutional inflows?

Approach: use price momentum of the sector ETF itself + volume vs 20d avg.
We can't get direct fund-flow data on the free tier, but ETF price action
is a strong proxy — when XLF is +5% in a week on heavy volume, money is
flowing in. When XLE drops -3% on heavy volume, money is leaving.

Output per sector ETF:
  ret_5d, ret_20d, vol_vs_avg (current day vol / 20d avg vol)
  flow_score: signed score combining momentum + volume thrust

This gets joined to each ticker via SECTOR_MAP, so a ticker in a hot sector
gets its rank boosted.
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

log = logging.getLogger("optedge.sector_flow")

# Sector ETFs we care about
SECTOR_ETFS = [
    "XLK",
    "XLF",
    "XLE",
    "XLV",
    "XLY",
    "XLP",
    "XLI",
    "XLB",
    "XLRE",
    "XLU",
    "XLC",
    "SPY",
    "QQQ",
    "IWM",
    "DIA",
    "ARKK",
]


def _sector_stats(etf: str) -> dict[str, Any] | None:
    """Compute momentum + volume thrust for one ETF."""
    h = data_provider.get_history(etf, period="3mo")
    if h is None or h.empty or len(h) < 22:
        return None
    closes = h["Close"]
    vols = h["Volume"]
    last_close = float(closes.iloc[-1])
    ret_5d = (last_close / float(closes.iloc[-6])) - 1 if len(closes) > 6 else 0.0
    ret_20d = (last_close / float(closes.iloc[-21])) - 1 if len(closes) > 21 else 0.0
    avg_vol_20 = float(vols.iloc[-21:-1].mean()) if len(vols) > 21 else 1
    last_vol = float(vols.iloc[-1])
    vol_thrust = last_vol / max(avg_vol_20, 1)
    # Flow score = momentum × (1 + vol_thrust_excess clamped)
    excess_vol = max(0, min(2.0, vol_thrust - 1.0))  # cap at 2x normal
    flow_score = (ret_5d * 3 + ret_20d * 1.5) * (1 + excess_vol * 0.5)
    return {
        "sector_etf": etf,
        "ret_5d": round(ret_5d, 4),
        "ret_20d": round(ret_20d, 4),
        "vol_thrust": round(vol_thrust, 2),
        "flow_score": round(flow_score, 4),
    }


def run() -> pd.DataFrame:
    """Compute flow score per sector ETF (returns a small DataFrame).

    Use sector_flow_score_for(ticker) to join into per-ticker fusion data.
    """
    rows = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_sector_stats, etf): etf for etf in SECTOR_ETFS}
        for fut in as_completed(futs):
            try:
                r = fut.result()
                if r:
                    rows.append(r)
            except Exception:
                pass
    df = pd.DataFrame(rows)
    if not df.empty:
        log.info(
            "sector_flow: %d ETFs, top flow: %s",
            len(df),
            df.sort_values("flow_score", ascending=False).iloc[0]["sector_etf"],
        )
    return df


def per_ticker_score(universe: list[str], sector_flow: pd.DataFrame) -> pd.DataFrame:
    """Map each ticker → its sector ETF's flow_score."""
    if sector_flow is None or sector_flow.empty:
        return pd.DataFrame()
    try:
        from engines.sector_rs import SECTOR_MAP
    except Exception:
        return pd.DataFrame()
    flow_map = dict(zip(sector_flow["sector_etf"], sector_flow["flow_score"], strict=False))
    rows = []
    for t in dict.fromkeys(universe):
        sec = SECTOR_MAP.get(t, "SPY")
        score = flow_map.get(sec)
        if score is None:
            continue
        rows.append({"ticker": t, "sector_etf": sec, "sector_flow_score": score})
    return pd.DataFrame(rows)
