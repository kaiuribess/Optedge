# Purpose: Value / EV (good-value) engine — Greenblatt Magic Formula style.
"""Value / EV (good-value) engine — Greenblatt Magic Formula style.

For each ticker, computes:
  - earnings_yield  (EBIT / EV) — higher = cheaper
  - roic             (EBIT / Invested Capital) — higher = quality
  - fcf_yield        (FCF / Market Cap) — cash-on-cash return
  - peg_ratio        (P/E ÷ growth) — growth-adjusted multiple
  - earnings_yield_vs_treasury — cheap relative to risk-free rate?
  - graham_score     (composite Ben Graham filter)

Combines via Greenblatt: rank-of-earnings-yield + rank-of-roic, lowest sum wins.
We ALSO output a value_score in z-units so it merges cleanly with fusion's other signals.
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
from config import RISK_FREE_RATE_DEFAULT  # noqa: E402
from utils import safe_float, zscore  # noqa: E402

log = logging.getLogger("optedge.value")


def _per_ticker(t: str) -> dict[str, Any]:
    info = data_provider.get_fundamentals(t)
    if not info:
        return {"ticker": t}

    pe = safe_float(info.get("trailingPE"))
    fwd_pe = safe_float(info.get("forwardPE"))
    ps = safe_float(info.get("priceToSalesTrailing12Months"))
    ev_ebitda = safe_float(info.get("enterpriseToEbitda"))
    fcf = safe_float(info.get("freeCashflow"))
    mcap = safe_float(info.get("marketCap"))
    rev_growth = safe_float(info.get("revenueGrowth"))
    op_margin = safe_float(info.get("operatingMargins"))

    fcf_yield = (fcf / mcap) if (fcf and mcap and mcap > 0) else None
    earnings_yield = (1 / pe) if (pe and pe > 0) else None
    # Approximate ROIC via op_margin × asset_turnover proxy. Without asset
    # turnover from yfinance, we use op_margin as a quality proxy.
    roic_proxy = op_margin if op_margin else None

    # PEG: P/E ÷ growth (annualised). Only meaningful if positive growth.
    peg = None
    if pe and pe > 0 and rev_growth and rev_growth > 0:
        peg = pe / (rev_growth * 100)

    # Earnings yield vs 10Y treasury
    ey_vs_treasury = None
    if earnings_yield is not None:
        ey_vs_treasury = earnings_yield - RISK_FREE_RATE_DEFAULT

    # Graham-ish composite filter:
    # +1 for each of: P/E < 20, P/B < 3 (proxied via P/S < 3), positive FCF yield,
    #   positive op margin, positive growth, P/E > 0
    graham = 0
    if pe and 0 < pe < 20:
        graham += 1
    if ps and 0 < ps < 3:
        graham += 1
    if fcf_yield and fcf_yield > 0.04:
        graham += 1
    if op_margin and op_margin > 0.10:
        graham += 1
    if rev_growth and rev_growth > 0:
        graham += 1
    if pe and pe > 0:
        graham += 1

    return {
        "ticker": t,
        "pe": pe if pe else None,
        "fwd_pe": fwd_pe if fwd_pe else None,
        "ps": ps if ps else None,
        "ev_ebitda": ev_ebitda if ev_ebitda else None,
        "fcf_yield": fcf_yield,
        "earnings_yield": earnings_yield,
        "ey_vs_treasury": ey_vs_treasury,
        "roic_proxy": roic_proxy,
        "peg_ratio": peg,
        "rev_growth": rev_growth if rev_growth else None,
        "op_margin": op_margin if op_margin else None,
        "graham_score": graham,
        "market_cap": mcap if mcap else None,
    }


def _build_value_score(df: pd.DataFrame) -> pd.DataFrame:
    """Combine factors into a single value_score. Higher = better value."""
    if df.empty:
        df["value_score"] = 0.0
        df["magic_rank"] = None
        return df

    # Magic Formula: rank by earnings yield (high) + rank by ROIC (high), lowest sum wins
    df["ey_rank"] = df["earnings_yield"].rank(ascending=False, method="min")
    df["roic_rank"] = df["roic_proxy"].rank(ascending=False, method="min")
    df["magic_sum"] = df["ey_rank"].fillna(df["ey_rank"].max() + 1) + df["roic_rank"].fillna(
        df["roic_rank"].max() + 1
    )
    df["magic_rank"] = df["magic_sum"].rank(method="min").astype("Int64")

    # Z-score each component, then weighted combine into value_score.
    z_ey = zscore(df["earnings_yield"].fillna(0))
    z_fcf = zscore(df["fcf_yield"].fillna(0))
    z_roic = zscore(df["roic_proxy"].fillna(0))
    z_graham = zscore(df["graham_score"].fillna(0))
    # Lower P/E, P/S, EV/EBITDA = better value → use NEGATIVE z
    z_pe = -zscore(df["pe"].fillna(df["pe"].median() if not df["pe"].isna().all() else 20))
    z_ps = -zscore(df["ps"].fillna(df["ps"].median() if not df["ps"].isna().all() else 3))
    z_ev = -zscore(
        df["ev_ebitda"].fillna(df["ev_ebitda"].median() if not df["ev_ebitda"].isna().all() else 12)
    )

    df["value_score"] = (
        0.20 * z_ey
        + 0.20 * z_fcf
        + 0.15 * z_roic
        + 0.15 * z_graham
        + 0.10 * z_pe
        + 0.10 * z_ps
        + 0.10 * z_ev
    ).fillna(0)

    # Label: value bucket
    def _bucket(s):
        if s > 1.0:
            return "deep value"
        if s > 0.5:
            return "value"
        if s > -0.5:
            return "fair"
        return "expensive"

    df["value_bucket"] = df["value_score"].apply(_bucket)
    return df


def run(universe: list[str], max_workers: int = 8) -> pd.DataFrame:
    log.info("value/EV: %d tickers (parallel, %d workers)", len(universe), max_workers)
    rows = []
    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_per_ticker, t): t for t in universe}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                rows.append(fut.result())
            except Exception as e:
                log.debug("value fail %s: %s", t, e)
                rows.append({"ticker": t})
            completed += 1
            if completed % 100 == 0 or completed == len(universe):
                log.info("[%d/%d]", completed, len(universe))
    df = pd.DataFrame(rows)
    return _build_value_score(df)
