"""Provide shared retry, option-math, statistics, and numeric helpers."""
import math
import time
import functools
import logging
from typing import Callable, Any, Optional
import pandas as pd
from scipy.stats import norm
from scipy.optimize import brentq

log = logging.getLogger("optedge")


def retry(times: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """Simple retry decorator with exponential backoff."""
    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            wait = delay
            last = None
            for _ in range(times):
                try:
                    return fn(*args, **kwargs)
                except Exception as e:
                    last = e
                    log.debug("retry %s failed (%s), sleeping %.1fs", fn.__name__, e, wait)
                    time.sleep(wait)
                    wait *= backoff
            log.warning("%s failed after %d retries: %s", fn.__name__, times, last)
            return None
        return wrapper
    return deco


def safe(fn: Callable, default: Any = None) -> Any:
    """Run fn() but swallow exceptions and return default."""
    try:
        return fn()
    except Exception as e:
        log.debug("safe() caught: %s", e)
        return default


# -------- Black-Scholes -----------------------------------------------
def bs_price(S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0,
             call: bool = True) -> float:
    """Black-Scholes price with continuous dividend yield q.

    S=spot, K=strike, T=years to expiry, r=risk-free, sigma=vol, q=div yield.
    """
    if T <= 0 or sigma <= 0:
        intrinsic = max(0.0, (S - K) if call else (K - S))
        return intrinsic
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if call:
        return S * math.exp(-q * T) * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * math.exp(-q * T) * norm.cdf(-d1)


def bs_implied_vol(price: float, S: float, K: float, T: float, r: float, q: float = 0.0,
                   call: bool = True) -> Optional[float]:
    """Solve for implied vol via Brent's method. Returns None if no solution."""
    if price <= 0 or T <= 0:
        return None
    intrinsic = max(0.0, (S - K) if call else (K - S))
    if price < intrinsic - 1e-4:
        return None  # arbitrage / stale data

    def objective(sigma):
        return bs_price(S, K, T, r, sigma, q, call) - price

    try:
        return brentq(objective, 1e-4, 5.0, maxiter=100)
    except Exception:
        return None


def bs_delta(S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0,
             call: bool = True) -> float:
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return math.exp(-q * T) * (norm.cdf(d1) if call else norm.cdf(d1) - 1)


# -------- Statistical helpers ----------------------------------------
def zscore(s: pd.Series) -> pd.Series:
    """Cross-sectional z-score that tolerates degenerate inputs."""
    s = pd.Series(s).astype(float)
    if s.std(skipna=True) == 0 or s.dropna().empty:
        return pd.Series(0.0, index=s.index)
    return (s - s.mean(skipna=True)) / s.std(skipna=True)


def winsor(s: pd.Series, p: float = 0.02) -> pd.Series:
    s = pd.Series(s).astype(float)
    lo, hi = s.quantile(p), s.quantile(1 - p)
    return s.clip(lo, hi)


def squash(x: float, k: float = 1.0) -> float:
    """Bounded scaling: tanh(x/k) maps any real to [-1,1]."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return 0.0
    return math.tanh(x / k)


def percentile_rank(value: float, series: pd.Series) -> float:
    s = pd.Series(series).dropna()
    if s.empty:
        return 50.0
    return float((s < value).mean() * 100.0)


# -------- NaN-safe primitives ----------------------------------------
def safe_int(v, default: int = 0) -> int:
    """Convert to int, treating None/NaN/inf as the default. yfinance loves NaNs."""
    try:
        if v is None:
            return default
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return default
        return int(v)
    except (ValueError, TypeError):
        return default


def safe_float(v, default: float = 0.0) -> float:
    """Convert to float, treating None/NaN/inf as the default."""
    try:
        if v is None:
            return default
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (ValueError, TypeError):
        return default
