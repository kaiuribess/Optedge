# Purpose: Portfolio-level Greek aggregation.
"""Portfolio-level Greek aggregation.

Sums delta, gamma, theta, vega across all option picks (weighted by suggested
contracts) so the user can see net portfolio exposure at a glance.

Outputs an HTML panel showing:
  - Net delta (+ = bullish, - = bearish)
  - Net gamma (vol of vol exposure)
  - Net theta ($/day decay)
  - Net vega ($ per 1pp IV move)
  - Hedge suggestion when |net delta| > threshold
"""
from __future__ import annotations
import logging
import math
from typing import Dict, Optional

import pandas as pd

log = logging.getLogger("optedge.greeks")


def _bs_gamma(S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0) -> float:
    """Black-Scholes gamma."""
    if T <= 0 or sigma <= 0 or S <= 0:
        return 0.0
    try:
        from scipy.stats import norm
    except ImportError:
        return 0.0
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return math.exp(-q * T) * norm.pdf(d1) / (S * sigma * math.sqrt(T))


def _bs_theta(S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0,
              call: bool = True) -> float:
    """Black-Scholes theta (annualised). Divide by 365 for $/day."""
    if T <= 0 or sigma <= 0 or S <= 0:
        return 0.0
    try:
        from scipy.stats import norm
    except ImportError:
        return 0.0
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    a = -S * math.exp(-q * T) * norm.pdf(d1) * sigma / (2 * math.sqrt(T))
    if call:
        b = -r * K * math.exp(-r * T) * norm.cdf(d2) + q * S * math.exp(-q * T) * norm.cdf(d1)
    else:
        b = r * K * math.exp(-r * T) * norm.cdf(-d2) - q * S * math.exp(-q * T) * norm.cdf(-d1)
    return a + b


def _bs_vega(S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0) -> float:
    """Black-Scholes vega (per 1.0 = 100% IV change). Divide by 100 for per-1pp."""
    if T <= 0 or sigma <= 0 or S <= 0:
        return 0.0
    try:
        from scipy.stats import norm
    except ImportError:
        return 0.0
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return S * math.exp(-q * T) * norm.pdf(d1) * math.sqrt(T)


def aggregate_portfolio_greeks(top_options: pd.DataFrame, risk_free: float = 0.045) -> Dict:
    """Sum greeks across portfolio.

    Each contract represents 100 shares; we multiply by suggested_contracts
    (the actual position size).
    """
    if top_options is None or top_options.empty:
        return {
            "net_delta": 0.0, "net_gamma": 0.0, "net_theta": 0.0, "net_vega": 0.0,
            "n_positions": 0, "n_calls": 0, "n_puts": 0,
        }
    net_delta = 0.0; net_gamma = 0.0; net_theta = 0.0; net_vega = 0.0
    n_calls = 0; n_puts = 0
    for _, r in top_options.iterrows():
        try:
            S = float(r.get("spot") or 0)
            K = float(r.get("strike") or 0)
            dte = float(r.get("dte") or 0)
            iv = float(r.get("iv_market") or 0)
            delta = float(r.get("delta") or 0)
            n = int(r.get("suggested_contracts") or 0)
            if n <= 0 or S <= 0 or K <= 0 or dte <= 0 or iv <= 0:
                continue
            T = dte / 365.0
            is_call = r.get("side") == "call"
            gamma = _bs_gamma(S, K, T, risk_free, iv)
            theta = _bs_theta(S, K, T, risk_free, iv, call=is_call)
            vega = _bs_vega(S, K, T, risk_free, iv)
            # 100 share contract multiplier baked in
            mult = 100 * n
            net_delta += delta * mult
            net_gamma += gamma * mult
            net_theta += theta * mult / 365  # $/day
            net_vega += vega * mult / 100   # $/1pp IV
            if is_call: n_calls += 1
            else: n_puts += 1
        except Exception as e:
            log.debug("greek aggregation skip: %s", e)
            continue
    return {
        "net_delta": net_delta, "net_gamma": net_gamma,
        "net_theta": net_theta, "net_vega": net_vega,
        "n_positions": n_calls + n_puts,
        "n_calls": n_calls, "n_puts": n_puts,
    }


def hedge_suggestion(greeks: Dict, threshold: float = 5000.0) -> Optional[Dict]:
    """If net delta exceeds threshold (default $5K), suggest a hedge.

    >+5000 net delta => suggest SPY puts (long-equity exposure too high)
    <-5000 net delta => suggest SPY calls (short-equity exposure too high)
    """
    nd = greeks.get("net_delta", 0)
    if abs(nd) < threshold:
        return None
    if nd > 0:
        return {
            "direction": "long delta",
            "exposure": nd,
            "suggestion": f"Net long-equity exposure ${nd:,.0f}. "
                          f"Consider SPY ATM puts ({int(abs(nd) / 100 / 4)} contracts) to hedge.",
        }
    else:
        return {
            "direction": "short delta",
            "exposure": nd,
            "suggestion": f"Net short-equity exposure ${nd:,.0f}. "
                          f"Consider SPY ATM calls ({int(abs(nd) / 100 / 4)} contracts) to hedge.",
        }
