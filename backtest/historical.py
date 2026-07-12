"""Historical backtest — Information Coefficient analysis.

For each ticker in the universe:
  - fetch the spot price 7d / 30d / 60d / 90d ago
  - compute the forward return from that date to today
  - join with the current factor scores (value_score, fund_score, etc.)
  - compute the Information Coefficient (Spearman rank correlation) between
    each factor's score TODAY and the forward return realized OVER the window

This isn't a perfect backtest — the factor scores aren't from those past
dates, they're from today. But fundamentals and macro tilts move slowly,
and the IC analysis still tells you which factors have predictive power
over different horizons.

Run: python run.py --backtest
"""
from __future__ import annotations
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any

import pandas as pd

import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider

log = logging.getLogger("optedge.historical")

HORIZONS = [7, 30, 60, 90]


def _process_ticker(t: str) -> List[Dict[str, Any]]:
    """Return a list of (ticker, horizon, forward_return) rows."""
    out = []
    h = data_provider.get_history(t, period="6mo")
    if h is None or h.empty or "Close" not in h.columns:
        return out
    close = h["Close"].dropna()
    if close.empty:
        return out
    spot_now = float(close.iloc[-1])
    for horizon in HORIZONS:
        if len(close) <= horizon:
            continue
        spot_then = float(close.iloc[-(horizon + 1)])
        if spot_then <= 0:
            continue
        out.append({
            "ticker": t,
            "horizon_days": horizon,
            "fwd_return": round(spot_now / spot_then - 1, 4),
        })
    return out


def run_historical_backtest(universe: List[str], factor_dfs: Dict[str, pd.DataFrame],
                             max_workers: int = 8) -> Dict[str, Any]:
    """factor_dfs maps factor_name -> DataFrame with columns ['ticker', factor_col].

    Example: factor_dfs = {
        'value_score': value_df,
        'fund_score': fund_df,
        'sentiment_delta': sent_df,
        'insider_score': ins_df,
    }
    """
    log.info("historical backtest: %d tickers x %d horizons", len(universe), len(HORIZONS))
    rows = []
    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_process_ticker, t): t for t in universe}
        for fut in as_completed(futures):
            try:
                rows.extend(fut.result())
            except Exception as e:
                log.debug("historical fail: %s", e)
            completed += 1
            if completed % 100 == 0 or completed == len(universe):
                log.info("[%d/%d]", completed, len(universe))

    rets = pd.DataFrame(rows)
    if rets.empty:
        return {"returns": rets, "ic": pd.DataFrame()}

    # Merge factor scores
    for factor_name, fdf in factor_dfs.items():
        if fdf is None or fdf.empty or factor_name not in fdf.columns:
            continue
        rets = rets.merge(fdf[["ticker", factor_name]], on="ticker", how="left")

    # Compute IC (Spearman rank correlation) per horizon per factor
    ic_rows = []
    factor_names = [f for f in factor_dfs.keys() if f in rets.columns]
    for h in HORIZONS:
        sub = rets[rets["horizon_days"] == h]
        if sub.empty:
            continue
        for f in factor_names:
            pair = sub[[f, "fwd_return"]].dropna()
            if len(pair) < 20:
                continue
            try:
                ic = pair[f].rank().corr(pair["fwd_return"].rank())  # Spearman
            except Exception:
                ic = None
            if ic is None or pd.isna(ic):
                continue
            # Decile spread: top 20% by factor score - bottom 20% by factor score, avg fwd_return
            sorted_pair = pair.sort_values(f, ascending=False)
            top_q = sorted_pair.head(max(1, len(sorted_pair) // 5))
            bot_q = sorted_pair.tail(max(1, len(sorted_pair) // 5))
            top_avg = top_q["fwd_return"].mean()
            bot_avg = bot_q["fwd_return"].mean()
            ic_rows.append({
                "horizon_days": h,
                "factor": f,
                "ic": round(ic, 3),
                "n": len(pair),
                "top_quintile_avg": round(top_avg, 4),
                "bot_quintile_avg": round(bot_avg, 4),
                "spread": round(top_avg - bot_avg, 4),
            })

    # Per-leg analysis: for each factor, compute top-quintile call-leg and put-leg P&L.
    # Call-leg P&L: + return if you'd bought a long call on that ticker (proxy: stock return).
    # Put-leg  P&L: - return (a long put profits when the underlying falls).
    # Share-leg P&L: same as call-leg (long stock = +return).
    leg_rows = []
    for f in factor_names:
        for h in HORIZONS:
            sub = rets[rets["horizon_days"] == h].dropna(subset=[f, "fwd_return"])
            if len(sub) < 20:
                continue
            sorted_sub = sub.sort_values(f, ascending=False)
            top_q = sorted_sub.head(max(1, len(sorted_sub) // 5))
            # Top-quintile factor picks: simulate long-call and long-put leg returns
            call_avg = float(top_q["fwd_return"].mean())          # call leg = +stock return
            put_avg = float(-top_q["fwd_return"].mean())          # put leg = -stock return
            share_avg = call_avg                                   # shares ≈ long stock
            leg_rows.append({
                "factor": f, "horizon_days": h, "n": len(top_q),
                "call_leg_avg": round(call_avg, 4),
                "put_leg_avg": round(put_avg, 4),
                "share_leg_avg": round(share_avg, 4),
                "call_win_rate": float((top_q["fwd_return"] > 0).mean()),
                "put_win_rate": float((top_q["fwd_return"] < 0).mean()),
            })

    return {
        "returns": rets,
        "ic": pd.DataFrame(ic_rows),
        "legs": pd.DataFrame(leg_rows),
    }
