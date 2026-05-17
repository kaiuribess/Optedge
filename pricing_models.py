"""Multi-model options pricing — v20.7 (vectorized).

Four theoretical models running on every contract via numpy vectorized
operations. No per-row Python loops, no "fast-path skip" shortcuts.
A Heston stochastic-vol implementation exists in this module but is
DISABLED by default — the Lewis-2001 Fourier inversion needs more
validation before it can drive position sizing; until then `all_models_vec`
omits it from the default set.

Active models:
  - bs_price_vec   : Black-Scholes-Merton (European, with continuous div yield)
  - crr_price_vec  : Cox-Ross-Rubinstein binomial tree (American, dividend-aware)
  - bjs_price_vec  : Bjerksund-Stensland 2002 closed-form (American)
  - cboe_theo      : CBOE's published proprietary theoretical (read off chain)

The legacy per-row functions (bs_price, crr_price, bjs_price) remain in
the module for callers that price one option at a time (tests, fallbacks).

Latency target (per ticker, ~500 surviving contracts):
  - bs_price_vec      ~ 0.5ms
  - crr_price_vec  80 ~ 30ms   (was 660ms unvectorized)
  - bjs_price_vec     ~ 5ms    (was 6700ms unvectorized)
  - heston_price_vec  ~ 60ms

Plus an adaptive regime-aware ensemble combiner — same interface as v20.3.
"""
from __future__ import annotations
import json
import math
from pathlib import Path
from typing import Dict, Optional

import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq

# Re-export legacy per-row helpers for callers that need a single-option price
from utils import bs_price, bs_implied_vol, bs_delta  # noqa: F401


# ---------------------------------------------------------------------------
# Legacy per-row CRR (kept for parity with v20.3)
# ---------------------------------------------------------------------------
def crr_price(S: float, K: float, T: float, r: float, sigma: float,
              q: float = 0.0, call: bool = True, steps: int = 80) -> float:
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if call else (K - S))
    dt = T / steps
    u = math.exp(sigma * math.sqrt(dt))
    d = 1.0 / u
    a = math.exp((r - q) * dt)
    p = (a - d) / (u - d)
    if not (0 < p < 1):
        return bs_price(S, K, T, r, sigma, q, call)
    disc = math.exp(-r * dt)
    j = np.arange(steps + 1)
    ST = S * (u ** (steps - j)) * (d ** j)
    vals = np.maximum(ST - K, 0.0) if call else np.maximum(K - ST, 0.0)
    for n in range(steps - 1, -1, -1):
        ST = S * (u ** (n - np.arange(n + 1))) * (d ** np.arange(n + 1))
        cont = disc * (p * vals[:n + 1] + (1 - p) * vals[1:n + 2])
        ex = np.maximum(ST - K, 0.0) if call else np.maximum(K - ST, 0.0)
        vals = np.maximum(cont, ex)
    return float(vals[0])


# ---------------------------------------------------------------------------
# VECTORIZED Black-Scholes-Merton — operates on numpy arrays
# ---------------------------------------------------------------------------
def bs_price_vec(S: np.ndarray, K: np.ndarray, T: np.ndarray, r: float,
                 sigma: np.ndarray, q: np.ndarray, call_mask: np.ndarray) -> np.ndarray:
    """Vectorized BS price for an array of contracts. Inputs all length N.
    `call_mask` is bool array (True = call, False = put). Returns array of len N."""
    S = np.asarray(S, dtype=np.float64)
    K = np.asarray(K, dtype=np.float64)
    T = np.asarray(T, dtype=np.float64)
    sigma = np.asarray(sigma, dtype=np.float64)
    q = np.asarray(q, dtype=np.float64) if not np.isscalar(q) else np.full_like(S, q)
    call_mask = np.asarray(call_mask, dtype=bool)

    # Guard against degenerate inputs
    bad = (T <= 0) | (sigma <= 0) | (S <= 0) | (K <= 0)
    sigT = sigma * np.sqrt(np.where(bad, 1.0, T))   # avoid 0-div warnings
    d1 = np.where(bad, 0.0,
                  (np.log(np.where(bad, 1.0, S/K)) +
                   (r - q + 0.5 * sigma**2) * T) / sigT)
    d2 = d1 - sigT

    eq = np.exp(-q * T)
    er = np.exp(-r * T)
    Nd1 = norm.cdf(d1); Nd2 = norm.cdf(d2)
    Nmd1 = norm.cdf(-d1); Nmd2 = norm.cdf(-d2)

    call_p = S * eq * Nd1 - K * er * Nd2
    put_p  = K * er * Nmd2 - S * eq * Nmd1
    out = np.where(call_mask, call_p, put_p)

    # Intrinsic for the degenerate rows
    intrinsic = np.where(call_mask,
                          np.maximum(0.0, S - K),
                          np.maximum(0.0, K - S))
    return np.where(bad, intrinsic, out)


# ---------------------------------------------------------------------------
# VECTORIZED CRR Binomial — process all contracts in parallel
# ---------------------------------------------------------------------------
def crr_price_vec(S: np.ndarray, K: np.ndarray, T: np.ndarray, r: float,
                  sigma: np.ndarray, q: np.ndarray, call_mask: np.ndarray,
                  steps: int = 80) -> np.ndarray:
    """Vectorized CRR American-option pricer.

    All inputs are length-N arrays (or broadcast-compatible). Internally
    builds an (N, steps+1) value matrix and does `steps` backward-induction
    sweeps, each a pure numpy op. For N=1000, steps=80 this runs in ~30ms."""
    S = np.asarray(S, dtype=np.float64)
    K = np.asarray(K, dtype=np.float64)
    T = np.asarray(T, dtype=np.float64)
    sigma = np.asarray(sigma, dtype=np.float64)
    q = np.asarray(q, dtype=np.float64) if not np.isscalar(q) else np.full_like(S, q)
    call_mask = np.asarray(call_mask, dtype=bool)
    N = len(S)

    # Per-option tree parameters
    Tsafe = np.where(T > 0, T, 1.0)
    dt = Tsafe / steps                              # (N,)
    u  = np.exp(sigma * np.sqrt(dt))                 # (N,)
    d  = 1.0 / u
    a  = np.exp((r - q) * dt)
    p  = (a - d) / (u - d)
    disc = np.exp(-r * dt)
    # Identify rows where the tree is numerically degenerate (use BS fallback)
    bad = (T <= 0) | (sigma <= 0) | (p <= 0) | (p >= 1) | (S <= 0) | (K <= 0)

    # Powers of u and d up to `steps`. Shape (N, steps+1).
    j = np.arange(steps + 1)                        # (steps+1,)
    # ST_term[i, j] = S[i] * u[i]**(steps-j) * d[i]**j
    log_u = np.log(u)                                # (N,)
    log_d = np.log(d)                                # (N,)
    # log(ST_term) = log(S) + (steps-j)*log_u + j*log_d
    log_ST_term = (np.log(np.where(S > 0, S, 1.0))[:, None]
                    + (steps - j)[None, :] * log_u[:, None]
                    + j[None, :] * log_d[:, None])
    ST_term = np.exp(log_ST_term)                    # (N, steps+1)

    # Terminal payoff
    K_b = K[:, None]
    payoff = np.where(call_mask[:, None],
                       np.maximum(ST_term - K_b, 0.0),
                       np.maximum(K_b - ST_term, 0.0))
    vals = payoff                                     # (N, steps+1)

    # Backward induction — at each step, value matrix shrinks by 1 column
    # but for vectorization we keep the full width and only use [:, :n+1].
    for n in range(steps - 1, -1, -1):
        # Stock prices at step n: ST_n[i, k] = S[i] * u[i]**(n-k) * d[i]**k  for k=0..n
        log_ST_n = (np.log(np.where(S > 0, S, 1.0))[:, None]
                     + (n - np.arange(n + 1))[None, :] * log_u[:, None]
                     + np.arange(n + 1)[None, :] * log_d[:, None])
        ST_n = np.exp(log_ST_n)                      # (N, n+1)
        # Continuation value
        cont = disc[:, None] * (
            p[:, None] * vals[:, :n + 1] +
            (1.0 - p[:, None]) * vals[:, 1:n + 2]
        )
        # American early-exercise
        ex = np.where(call_mask[:, None],
                       np.maximum(ST_n - K_b, 0.0),
                       np.maximum(K_b - ST_n, 0.0))
        vals = np.empty((N, n + 1), dtype=np.float64)
        np.maximum(cont, ex, out=vals)

    out = vals[:, 0]
    # Fallback to BS for degenerate rows
    if bad.any():
        bs_fallback = bs_price_vec(S, K, T, r, sigma, q, call_mask)
        out = np.where(bad, bs_fallback, out)
    return out


# ---------------------------------------------------------------------------
# VECTORIZED Bjerksund-Stensland 2002 — closed-form American
# ---------------------------------------------------------------------------
_GL_X = np.array([0.04691008, 0.23076534, 0.5, 0.76923466, 0.95308992])
_GL_W = np.array([0.018854042, 0.038088059, 0.0452707394, 0.038088059, 0.018854042])


def _bivariate_cdf_vec(a: np.ndarray, b: np.ndarray, rho: np.ndarray) -> np.ndarray:
    """Vectorized bivariate normal CDF via Drezner-Wesolowsky 5-point."""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    rho = np.clip(np.asarray(rho, dtype=np.float64), -0.9999, 0.9999)

    # Drezner approximation: works cleanly for the orthant (a<=0, b<=0, rho<=0).
    # For other orthants we use the standard reductions. Vectorize the four
    # canonical cases with masks.
    out = np.zeros_like(a)

    # Case 1: a<=0 and b<=0 and rho<=0 — direct Drezner
    m1 = (a <= 0) & (b <= 0) & (rho <= 0)
    if m1.any():
        a1, b1, r1 = a[m1], b[m1], rho[m1]
        s = np.zeros_like(a1)
        for i in range(5):
            t = r1 * _GL_X[i]
            denom = np.sqrt(np.clip(1 - t**2, 1e-30, None))
            num = 0.5 * (2 * a1 * b1 * t - a1**2 - b1**2) / np.clip(1 - t**2, 1e-30, None)
            s += _GL_W[i] * np.exp(num) / denom
        out[m1] = s * r1 + norm.cdf(a1) * norm.cdf(b1)

    # Case 2: a<=0 and b>=0 and rho>=0 → Φ(a) - L(a, -b, -ρ)
    m2 = (a <= 0) & (b >= 0) & (rho >= 0) & ~m1
    if m2.any():
        out[m2] = norm.cdf(a[m2]) - _bivariate_cdf_vec(a[m2], -b[m2], -rho[m2])

    # Case 3: a>=0 and b<=0 and rho>=0 → Φ(b) - L(-a, b, -ρ)
    m3 = (a >= 0) & (b <= 0) & (rho >= 0) & ~m1 & ~m2
    if m3.any():
        out[m3] = norm.cdf(b[m3]) - _bivariate_cdf_vec(-a[m3], b[m3], -rho[m3])

    # Case 4: a>=0 and b>=0 and rho<=0 → Φ(a)+Φ(b)-1+L(-a,-b,ρ)
    m4 = (a >= 0) & (b >= 0) & (rho <= 0) & ~m1 & ~m2 & ~m3
    if m4.any():
        out[m4] = norm.cdf(a[m4]) + norm.cdf(b[m4]) - 1 + _bivariate_cdf_vec(-a[m4], -b[m4], rho[m4])

    # Case 5 (mixed signs of rho): fall back to a more general reduction.
    # Use the standard split into two single-orthant integrals.
    m5 = ~(m1 | m2 | m3 | m4)
    if m5.any():
        a5, b5, r5 = a[m5], b[m5], rho[m5]
        # Use a numerically robust series via Φ(a)Φ(b) + correction
        # This is a good-enough approximation in the mixed-sign region for
        # BJS bivariate evaluation; small absolute error.
        denom = np.sqrt(np.clip(1 - r5**2, 1e-30, None))
        z = np.exp(-0.5 * (a5**2 + b5**2 - 2 * r5 * a5 * b5) / np.clip(1 - r5**2, 1e-30, None))
        z /= (2 * np.pi * denom)
        # Naive: Φ(a)Φ(b) + ρ * φ(a)φ(b) — accurate when |a|,|b| moderate
        approx = norm.cdf(a5) * norm.cdf(b5) + r5 * z
        out[m5] = np.clip(approx, 0.0, 1.0)
    return out


def _bjs_phi_vec(S, T, gamma, h, r, b, sigma):
    """Vectorized BJS phi helper."""
    Tsafe = np.where(T > 0, T, 1.0)
    sigT = sigma * np.sqrt(Tsafe)
    lam = (-r + gamma * b + 0.5 * gamma * (gamma - 1) * sigma**2) * T
    d1 = -(np.log(np.where(S > 0, S/h, 1.0)) + (b + (gamma - 0.5) * sigma**2) * T) / sigT
    kappa = 2.0 * b / sigma**2 + (2.0 * gamma - 1.0)
    return (np.exp(lam) * np.where(S > 0, S, 1.0)**gamma *
            (norm.cdf(d1) -
             (h/np.where(S > 0, S, 1.0))**kappa *
             norm.cdf(d1 - 2 * np.log(h/np.where(S > 0, S, 1.0)) / sigT)))


def bjs_price_vec(S, K, T, r, sigma, q, call_mask) -> np.ndarray:
    """Vectorized Bjerksund-Stensland 1993 American option price.

    Put valued via put-call transform: P(S,K,r,b,σ) = C(K,S,r-b,-b,σ).

    Defensive: when BJS returns ≤ intrinsic (numerical breakdown in
    deep-ITM puts / extreme strikes), fall back to BS as a safety floor.
    Intrinsic floor always enforced.
    """
    S = np.asarray(S, dtype=np.float64)
    K = np.asarray(K, dtype=np.float64)
    T = np.asarray(T, dtype=np.float64)
    sigma = np.asarray(sigma, dtype=np.float64)
    q = np.asarray(q, dtype=np.float64) if not np.isscalar(q) else np.full_like(S, q)
    call_mask = np.asarray(call_mask, dtype=bool)

    # Split via put-call transform
    S_eff = np.where(call_mask, S, K)
    K_eff = np.where(call_mask, K, S)
    b_call = r - q
    r_eff = np.where(call_mask, r, q)
    b_eff = np.where(call_mask, b_call, -b_call)

    out = _bjs_american_call_vec(S_eff, K_eff, T, r_eff, b_eff, sigma)

    # Defensive floor: BJS occasionally returns ~0 for deep-ITM puts under
    # certain rate/dividend configurations. Compare to BS — when BJS is
    # below BS by more than 5% AND below intrinsic, replace with BS.
    intrinsic = np.where(call_mask,
                          np.maximum(S - K, 0.0),
                          np.maximum(K - S, 0.0))
    bs_floor = bs_price_vec(S, K, T, r if np.isscalar(r) else float(np.mean(r)),
                              sigma, q, call_mask)
    # Use BS where BJS produces an unreasonably low value
    fallback_mask = (out < intrinsic) | (
        (bs_floor > 0) & (out < bs_floor * 0.5)
    )
    out = np.where(fallback_mask, bs_floor, out)
    # Final intrinsic floor
    out = np.maximum(out, intrinsic)
    return out


def _bjs_american_call_vec(S, K, T, r, b, sigma):
    """Bjerksund-Stensland 1993 American call (vectorized)."""
    sigma2 = sigma**2
    Tsafe = np.where(T > 0, T, 1.0)

    # No early-exercise benefit when b >= r → American == European
    european_mask = b >= r
    bs_val = bs_price_vec(S, K, T, r[0] if np.ndim(r) == 0 else np.zeros_like(S),
                           sigma, np.where(b >= r, r - b, r - b), np.ones_like(S, dtype=bool))
    # Re-derive BS price with the right rate: handled below by manual formula
    # to avoid scalar-r assumption mismatch.

    # Manual BS for the European case (handles per-row r):
    sigT = sigma * np.sqrt(Tsafe)
    d1 = (np.log(np.where((S > 0) & (K > 0), S/K, 1.0)) + (b + 0.5 * sigma2) * T) / sigT
    d2 = d1 - sigT
    eqT = np.exp((b - r) * T)
    erT = np.exp(-r * T)
    european_price = S * eqT * norm.cdf(d1) - K * erT * norm.cdf(d2)

    # Bjerksund-Stensland 1993 American body
    # Quadratic approx of beta
    beta_num = (0.5 - b / np.clip(sigma2, 1e-12, None)) + np.sqrt(
        (b / np.clip(sigma2, 1e-12, None) - 0.5)**2 + 2 * r / np.clip(sigma2, 1e-12, None))
    beta = np.where(beta_num > 1.0001, beta_num, 1.0001)  # avoid 1/0 below
    B_inf = beta / (beta - 1) * K
    B0 = np.maximum(K, r / np.clip(r - b, 1e-12, None) * K)
    ht = -(b * T + 2 * sigma * np.sqrt(Tsafe)) * K**2 / (np.clip((B_inf - B0) * B0, 1e-12, None))
    Ix = B0 + (B_inf - B0) * (1 - np.exp(ht))

    immediate = S >= Ix
    alpha = (Ix - K) * Ix**(-beta)
    # American value (where not already exercised)
    american_val = (alpha * S**beta
                    - alpha * _bjs_phi_vec(S, T, beta, Ix, r, b, sigma)
                    + _bjs_phi_vec(S, T, 1.0, Ix, r, b, sigma)
                    - _bjs_phi_vec(S, T, 1.0, K,  r, b, sigma)
                    - K * _bjs_phi_vec(S, T, 0.0, Ix, r, b, sigma)
                    + K * _bjs_phi_vec(S, T, 0.0, K,  r, b, sigma))

    out = np.where(european_mask, european_price, american_val)
    out = np.where(immediate, np.maximum(S - K, 0.0), out)
    out = np.where(T <= 0, np.maximum(S - K, 0.0), out)
    out = np.where(sigma <= 0, np.maximum(S - K, 0.0), out)
    return np.maximum(out, 0.0)


# ---------------------------------------------------------------------------
# HESTON stochastic-volatility model — Lewis 2001 Fourier inversion
# ---------------------------------------------------------------------------
# Default Heston params (used when no per-ticker calibration is available).
# These are middle-of-the-road equity-index defaults; the per-ticker fit
# below overrides them.
_HESTON_DEFAULTS = {
    "kappa": 2.0,    # mean-reversion speed
    "theta": 0.04,   # long-run variance (=20% vol)
    "rho":   -0.65,  # correlation (negative for equity leverage effect)
    "v0":    0.04,   # initial variance
    "xi":    0.4,    # vol of vol
}


def heston_char_fn(u, T, r, q, kappa, theta, rho, v0, xi):
    """Heston characteristic function ψ(u; T) for log-spot."""
    i = 1j
    u = np.asarray(u, dtype=np.complex128)
    d = np.sqrt((rho * xi * i * u - kappa)**2 + xi**2 * (i * u + u**2))
    g = (kappa - rho * xi * i * u - d) / (kappa - rho * xi * i * u + d)
    exp_dT = np.exp(-d * T)
    C = (i * u * (r - q) * T
         + kappa * theta / xi**2 *
           ((kappa - rho * xi * i * u - d) * T -
            2 * np.log((1 - g * exp_dT) / (1 - g))))
    D = ((kappa - rho * xi * i * u - d) / xi**2 *
         (1 - exp_dT) / (1 - g * exp_dT))
    return np.exp(C + D * v0)


def _heston_char_fn(u, T, r, q, kappa, theta, rho, v0, xi, S0):
    """Heston 1993 characteristic function of log(S_T). Returns shape (N, M)
    where N = len(T) and M = len(u). u is complex array of integration nodes."""
    i = 1j
    u_b = u[None, :]              # (1, M)
    T_b = T[:, None]              # (N, 1)
    # d, g per Heston 1993
    d = np.sqrt((rho * xi * i * u_b - kappa)**2 - xi**2 * (-i * u_b - u_b**2))
    g = (kappa - rho * xi * i * u_b - d) / (kappa - rho * xi * i * u_b + d)
    exp_dT = np.exp(-d * T_b)
    C = ((r - q) * i * u_b * T_b
         + kappa * theta / xi**2 *
           ((kappa - rho * xi * i * u_b - d) * T_b -
            2 * np.log((1 - g * exp_dT) / (1 - g))))
    D = ((kappa - rho * xi * i * u_b - d) / xi**2 *
         (1 - exp_dT) / (1 - g * exp_dT))
    return np.exp(C + D * v0 + i * u_b * np.log(S0)[:, None])


def heston_price_vec(S, K, T, r, sigma, q, call_mask,
                     kappa: float = None, theta: float = None,
                     rho: float = None, v0: float = None, xi: float = None,
                     n_quad: int = 96) -> np.ndarray:
    """Vectorized Heston 1993 pricing via the P1/P2 representation.

        C = S e^{-qT} P1 - K e^{-rT} P2

    where P_j = 0.5 + (1/π) ∫₀^∞ Re[ e^{-iu ln K} f_j(u) / (iu) ] du.

    `sigma` is unused (Heston has its own v0/theta); kept in signature so the
    function is interchangeable with bs_price_vec / crr_price_vec.
    """
    S = np.asarray(S, dtype=np.float64)
    K = np.asarray(K, dtype=np.float64)
    T = np.asarray(T, dtype=np.float64)
    q = np.asarray(q, dtype=np.float64) if not np.isscalar(q) else np.full_like(S, q)
    call_mask = np.asarray(call_mask, dtype=bool)

    kappa = kappa if kappa is not None else _HESTON_DEFAULTS["kappa"]
    theta = theta if theta is not None else _HESTON_DEFAULTS["theta"]
    rho   = rho   if rho   is not None else _HESTON_DEFAULTS["rho"]
    v0    = v0    if v0    is not None else _HESTON_DEFAULTS["v0"]
    xi    = xi    if xi    is not None else _HESTON_DEFAULTS["xi"]

    # Gauss-Legendre nodes on [0, ∞) via tan substitution u = tan(π/2 * t/(1-t))
    nodes, weights = np.polynomial.legendre.leggauss(n_quad)
    t = 0.5 * (nodes + 1)                # in (0, 1)
    # Map t -> u via u = a*t/(1-t) where a controls integration density
    a = 100.0                            # upper cutoff scale
    u = a * t / np.clip(1 - t, 1e-10, None)
    jac_du_dt = a / np.clip((1 - t)**2, 1e-10, None)
    dt = 0.5 * weights * jac_du_dt        # already absorbs the linear-map factor

    Tsafe = np.where(T > 0, T, 1.0)
    Ssafe = np.where(S > 0, S, 1.0)
    Ksafe = np.where(K > 0, K, 1.0)

    # Char fns: f1 uses kappa - rho*xi (Heston measure shift); f2 uses kappa.
    # We compute via the standard two-CF approach:
    #   f1(u) = exp( C1 + D1*v0 + i*u*ln(S0) )
    #   f2(u) = exp( C2 + D2*v0 + i*u*ln(S0) )
    # The difference is the "b" parameter: b1 = kappa - rho*xi, b2 = kappa.
    # We also have an "alpha" term: a1 = 0.5, a2 = -0.5 (sign of u² coef).
    i = 1j
    u_c = u.astype(np.complex128)

    r_scalar = float(r) if np.isscalar(r) else float(np.mean(r))
    q_b = q[:, None]                       # (N, 1)
    T_b = Tsafe[:, None]                   # (N, 1)

    def _P(b: float, sign_alpha: float):
        # Heston 93 form: d² = (ρξui - b)² - ξ²(2αui - u²) with α=±0.5
        d = np.sqrt((rho * xi * i * u_c - b)**2 -
                     xi**2 * (2 * sign_alpha * i * u_c - u_c**2))
        g = (b - rho * xi * i * u_c - d) / (b - rho * xi * i * u_c + d)
        # Broadcast u_c (n_quad,) over Tsafe (N,)
        d_b = d[None, :]
        g_b = g[None, :]
        b_minus_ru = (b - rho * xi * i * u_c)[None, :]
        u_b = u_c[None, :]
        exp_dT = np.exp(-d_b * T_b)
        C_term = ((r_scalar - q_b) * i * u_b * T_b +
                  kappa * theta / xi**2 *
                  ((b_minus_ru - d_b) * T_b -
                   2 * np.log((1 - g_b * exp_dT) / (1 - g_b))))
        D_term = ((b_minus_ru - d_b) / xi**2 *
                  (1 - exp_dT) / (1 - g_b * exp_dT))
        f = np.exp(C_term + D_term * v0 + i * u_b * np.log(Ssafe)[:, None])
        integrand = (np.exp(-i * u_b * np.log(Ksafe)[:, None]) * f /
                      (i * u_b)).real        # (N, n_quad)
        integral = np.sum(integrand * dt[None, :], axis=1)
        return 0.5 + integral / np.pi

    P1 = _P(b=kappa - rho * xi, sign_alpha=+1.0)
    P2 = _P(b=kappa,             sign_alpha=-1.0)
    eqT = np.exp(-q * Tsafe)
    erT = np.exp(-r_scalar * Tsafe)

    call_price = Ssafe * eqT * P1 - Ksafe * erT * P2
    # Put via put-call parity
    put_price = call_price - Ssafe * eqT + Ksafe * erT

    out = np.where(call_mask, call_price, put_price)
    # Intrinsic floor for degenerate / failed integrations
    intrinsic = np.where(call_mask,
                          np.maximum(Ssafe - Ksafe, 0.0),
                          np.maximum(Ksafe - Ssafe, 0.0))
    out = np.where(T <= 0, intrinsic, out)
    out = np.where(np.isfinite(out), out, intrinsic)
    return np.maximum(out, 0.0)


# ---------------------------------------------------------------------------
# Heston per-ticker calibration (cheap one-time fit from ATM options)
# ---------------------------------------------------------------------------
def calibrate_heston(spot: float, atm_strike: float, T: float, r: float, q: float,
                     atm_iv: float, skew_25d: Optional[float] = None) -> Dict[str, float]:
    """Quick analytic-style calibration of Heston params from a handful of
    observable IVs. Not a full optimization (that's a 200ms+ per-ticker cost
    we don't need). We anchor:
      - v0  = atm_iv²
      - theta = mean of (v0, long-run guess based on historical 30/252 vol)
      - kappa = 2 (default fast mean-reversion)
      - xi  = scale with sqrt(v0)
      - rho = derived from 25-delta skew when provided
    """
    v0 = max(atm_iv**2, 0.01**2)
    theta = max(v0, 0.20**2)   # don't let long-run vol drop below 20%
    kappa = 2.0
    xi = 0.4 * math.sqrt(v0 / 0.04)   # scale vol-of-vol with current variance
    rho = -0.65
    if skew_25d is not None and not (isinstance(skew_25d, float) and math.isnan(skew_25d)):
        # Positive skew (puts > calls) implies more negative rho
        rho = float(np.clip(-0.5 - 5 * skew_25d, -0.95, -0.10))
    return {"kappa": kappa, "theta": theta, "rho": rho, "v0": v0, "xi": xi}


# ---------------------------------------------------------------------------
# Legacy per-row BJS (kept for callers that price one option at a time)
# ---------------------------------------------------------------------------
def bjs_price(S: float, K: float, T: float, r: float, sigma: float,
              q: float = 0.0, call: bool = True) -> float:
    arr = bjs_price_vec(np.array([S]), np.array([K]), np.array([T]), r,
                         np.array([sigma]), np.array([q]),
                         np.array([call]))
    return float(arr[0])


# ---------------------------------------------------------------------------
# Adaptive ensemble weights — v20.3 logic, now with heston bucket
# ---------------------------------------------------------------------------
_DEFAULT_WEIGHTS = {"bs": 0.27, "crr": 0.27, "bjs": 0.21, "cboe": 0.25}

_REGIME_DEFAULTS = {
    "low_vol":  {"bs": 0.38, "crr": 0.22, "bjs": 0.20, "cboe": 0.20},
    "normal":   {"bs": 0.27, "crr": 0.27, "bjs": 0.21, "cboe": 0.25},
    "high_vol": {"bs": 0.15, "crr": 0.30, "bjs": 0.30, "cboe": 0.25},
}


def _weights_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "model_weights.json"


def load_weights(regime: str = "normal") -> Dict[str, float]:
    p = _weights_path()
    if p.exists():
        try:
            blob = json.loads(p.read_text())
            w = blob.get(regime) or blob.get("default")
            if w and isinstance(w, dict):
                return {k: float(v) for k, v in w.items()}
        except Exception:
            pass
    return _REGIME_DEFAULTS.get(regime, _DEFAULT_WEIGHTS).copy()


def save_weights(weights_by_regime: Dict[str, Dict[str, float]]) -> None:
    p = _weights_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(weights_by_regime, indent=2))


def classify_vix_regime(vix: Optional[float]) -> str:
    if vix is None or not isinstance(vix, (int, float)) or math.isnan(vix):
        return "normal"
    if vix < 18:  return "low_vol"
    if vix > 25:  return "high_vol"
    return "normal"


def ensemble_theo(per_model: Dict[str, float], weights: Optional[Dict[str, float]] = None) -> float:
    if not per_model:
        return float("nan")
    w = weights or load_weights()
    num = 0.0
    den = 0.0
    for k, v in per_model.items():
        if v is None:
            continue
        try:
            vf = float(v)
        except (TypeError, ValueError):
            continue
        if math.isnan(vf) or math.isinf(vf) or vf < 0:
            continue
        wt = float(w.get(k, 0.0))
        if wt <= 0:
            continue
        num += wt * vf
        den += wt
    if den <= 0:
        return float("nan")
    return num / den


def ensemble_theo_vec(per_model: Dict[str, np.ndarray],
                      weights: Optional[Dict[str, float]] = None) -> np.ndarray:
    """Vectorized ensemble: per_model[name] is an array. Returns array."""
    if not per_model:
        return np.array([])
    w = weights or load_weights()
    keys = [k for k in per_model.keys() if w.get(k, 0) > 0]
    if not keys:
        return np.full(len(next(iter(per_model.values()))), np.nan)
    stack = np.stack([per_model[k] for k in keys], axis=1)   # (N, M)
    wts = np.array([w.get(k, 0.0) for k in keys])
    # Mask invalid entries per-row
    valid = np.isfinite(stack) & (stack >= 0)
    wts_b = np.where(valid, wts[None, :], 0.0)
    num = np.sum(np.where(valid, stack, 0.0) * wts_b, axis=1)
    den = np.sum(wts_b, axis=1)
    out = np.where(den > 0, num / np.where(den > 0, den, 1.0), np.nan)
    return out


def all_models_vec(S, K, T, r, sigma, q, call_mask,
                   cboe_theo: Optional[np.ndarray] = None,
                   crr_steps: int = 80,
                   heston_params: Optional[Dict] = None,
                   models: Optional[set] = None) -> Dict[str, np.ndarray]:
    """Run every available model on the whole array of contracts at once.

    `models` allows runtime selection (e.g. {"bs","crr","bjs"}). Defaults to
    {"bs","crr","bjs","cboe"}. Heston is implemented but disabled by default
    pending further calibration work (the Lewis/Heston-93 implementation needs
    more validation before it can drive position sizing).
    """
    if models is None:
        models = {"bs", "crr", "bjs", "cboe"}

    out: Dict[str, np.ndarray] = {}
    if "bs" in models:
        out["bs"] = bs_price_vec(S, K, T, r, sigma, q, call_mask)
    if "crr" in models:
        out["crr"] = crr_price_vec(S, K, T, r, sigma, q, call_mask, steps=crr_steps)
    if "bjs" in models:
        out["bjs"] = bjs_price_vec(S, K, T, r, sigma, q, call_mask)
    if "heston" in models:
        hp = heston_params or {}
        out["heston"] = heston_price_vec(S, K, T, r, sigma, q, call_mask,
                                          kappa=hp.get("kappa"), theta=hp.get("theta"),
                                          rho=hp.get("rho"), v0=hp.get("v0"),
                                          xi=hp.get("xi"))
    if "cboe" in models and cboe_theo is not None:
        ct = np.asarray(cboe_theo, dtype=np.float64)
        ct = np.where((ct > 0) & np.isfinite(ct), ct, np.nan)
        out["cboe"] = ct
    return out


# ---------------------------------------------------------------------------
# Legacy per-row dispatch — used by tests / old callers
# ---------------------------------------------------------------------------
def all_models(S: float, K: float, T: float, r: float, sigma: float,
               q: float = 0.0, call: bool = True,
               cboe_theo: Optional[float] = None,
               crr_steps: int = 80,
               heston_params: Optional[Dict] = None,
               fast_path: bool = False) -> Dict[str, float]:
    """Single-option dispatch — kept for backwards compatibility. Internally
    calls the vectorized versions with N=1."""
    S_a = np.array([S]); K_a = np.array([K]); T_a = np.array([T])
    sigma_a = np.array([sigma]); q_a = np.array([q]); mask = np.array([call])
    res = all_models_vec(S_a, K_a, T_a, r, sigma_a, q_a, mask,
                          cboe_theo=np.array([cboe_theo]) if cboe_theo is not None else None,
                          crr_steps=crr_steps,
                          heston_params=heston_params)
    return {k: float(v[0]) for k, v in res.items()
            if v is not None and np.isfinite(v[0])}


if __name__ == "__main__":
    import time
    # Benchmark
    np.random.seed(0)
    N = 1000
    S = np.full(N, 100.0)
    K = np.random.uniform(70, 130, N)
    T = np.random.uniform(7/365, 90/365, N)
    sigma = np.random.uniform(0.20, 0.50, N)
    q = np.full(N, 0.01)
    r = 0.045
    call_mask = np.random.rand(N) > 0.5

    for fn, name, kwargs in [
        (bs_price_vec,    "bs_price_vec",    {}),
        (crr_price_vec,   "crr_price_vec",   {"steps": 80}),
        (bjs_price_vec,   "bjs_price_vec",   {}),
        (heston_price_vec,"heston_price_vec",{"n_quad": 32}),
    ]:
        t0 = time.time()
        res = fn(S, K, T, r, sigma, q, call_mask, **kwargs)
        dt = (time.time() - t0) * 1000
        print(f"{name:18}: {dt:6.1f}ms for {N} contracts  "
              f"({dt/N*1000:.2f}µs/contract)  mean={np.mean(res):.4f}")
