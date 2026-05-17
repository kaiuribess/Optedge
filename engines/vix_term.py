"""VIX futures term structure engine.

Computes contango / backwardation from VIX spot vs front-month VIX futures.

  Contango  (VX1 > VIX)  = normal market, low vol regime, BUY-stocks bias
  Flat                   = transition, neutral
  Backwardation (VX1 < VIX) = stress regime, SELL-stocks / hedge bias

We use the CBOE-derived monthly VIX futures tickers via yfinance proxies:
  ^VIX = spot
  ^VIX3M = 3M constant maturity (CBOE)
  ^VIX9D = 9-day constant maturity (CBOE)

The ratio VIX3M/VIX gives a clean contango measure.

This engine produces a single market-wide score that gets applied to ALL
tickers as a regime gate (not per-ticker), so the output is a one-row DF
with a special 'ticker' value '__MARKET__' or applied via macro_state.

For simplicity we expose it via a per-row score that broadcasts to all tickers
when fused.
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Dict, List

import pandas as pd

import sys
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider

log = logging.getLogger("optedge.vix_term")


def _last_close(ticker: str) -> float:
    h = data_provider.get_history(ticker, period="5d", cache_age=1800)
    if h.empty:
        return 0.0
    try:
        return float(h["Close"].iloc[-1])
    except Exception:
        return 0.0


def compute_term_structure() -> Dict:
    """Return dict with:
       vix_spot, vix_3m, vix_9d, contango_ratio (vix_3m/vix_spot),
       regime in {'deep_contango','contango','flat','backwardation','deep_backwardation'},
       term_score in [-1,1] (positive = contango = bull bias)
    """
    vix = _last_close("^VIX")
    vix3m = _last_close("^VIX3M")
    vix9d = _last_close("^VIX9D")
    if vix <= 0 or vix3m <= 0:
        return {"regime": "unknown", "term_score": 0.0, "contango_ratio": None,
                "vix_spot": vix, "vix_3m": vix3m, "vix_9d": vix9d}
    ratio = vix3m / vix
    if ratio >= 1.15:
        regime, score = "deep_contango", 1.0
    elif ratio >= 1.05:
        regime, score = "contango", 0.5
    elif ratio >= 0.98:
        regime, score = "flat", 0.0
    elif ratio >= 0.90:
        regime, score = "backwardation", -0.5
    else:
        regime, score = "deep_backwardation", -1.0
    return {
        "regime": regime,
        "term_score": score,
        "contango_ratio": ratio,
        "vix_spot": vix,
        "vix_3m": vix3m,
        "vix_9d": vix9d,
    }


def run(universe: List[str]) -> pd.DataFrame:
    """Broadcast the market-wide VIX term score to every ticker in the universe."""
    state = compute_term_structure()
    if state["regime"] == "unknown":
        log.info("VIX term: cannot compute (^VIX or ^VIX3M unavailable)")
        return pd.DataFrame()
    log.info("VIX term: regime=%s ratio=%.3f spot=%.2f 3M=%.2f score=%+.1f",
             state["regime"], state["contango_ratio"] or 0,
             state["vix_spot"], state["vix_3m"], state["term_score"])
    score = state["term_score"]
    rows = [{
        "ticker": t,
        "vix_term_score": score,
        "vix_regime": state["regime"],
        "vix_contango_ratio": state["contango_ratio"],
    } for t in universe]
    return pd.DataFrame(rows)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(compute_term_structure())
