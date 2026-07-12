# Purpose: Load defaults and refit per-asset signal weights.
"""Central self-learning weight manager — Optedge v16.

Owns per-bucket signal weights, hand-crafted priors, and the LassoCV/IC-bootstrap
refit logic. Versioned default weights live under optedge/default_weights/.
Runtime learning writes only to the ignored data/weights/{bucket}.json files.

This module is intentionally side-effect-free aside from filesystem reads/writes
to data/weights/, so it can be imported by both the live engines and the
backtest module without circular dependencies.

Buckets:
  - options_call, options_put, shares_long
  - futures_equity, futures_treasury, futures_metal,
    futures_energy, futures_crypto, futures_currency, futures_agri

Per-bucket file format (JSON):
  {
    "weights": {factor: weight, ...},
    "meta": {
      "source": "priors|ic_bootstrap|lasso_refit",
      "n_samples": int,
      "fitted_at": "ISO8601",
      "ic_score": float,           # if available
      "factor_ic": {factor: ic},   # latest IC per factor
      "decay_flags": [factor,...]  # factors whose IC has dropped >50% recently
    }
  }
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, List

import numpy as np
import pandas as pd

log = logging.getLogger("optedge.learning")
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WEIGHTS_DIR = ROOT / "optedge" / "default_weights"
WEIGHTS_DIR = ROOT / "data" / "weights"


# ---------------------------------------------------------------------------
# Bucket registry
# ---------------------------------------------------------------------------
BUCKET_KEYS = [
    "options_call", "options_put", "shares_long",
    "futures_equity", "futures_treasury", "futures_metal",
    "futures_energy", "futures_crypto", "futures_currency", "futures_agri",
]

# Map raw 'kind' field on futures contracts to bucket key
FUTURES_KIND_TO_BUCKET = {
    "equity":    "futures_equity",
    "vol":       "futures_equity",
    "bond":      "futures_treasury",
    "commodity": "futures_metal",   # default; overridden per-symbol below
    "agri":      "futures_agri",
    "fx":        "futures_currency",
    "crypto":    "futures_crypto",
}

# Per-symbol overrides for commodity futures (energy vs metal split)
SYMBOL_TO_BUCKET = {
    "CL=F": "futures_energy",
    "NG=F": "futures_energy",
    "RB=F": "futures_energy",
    "HO=F": "futures_energy",
    "GC=F": "futures_metal",
    "SI=F": "futures_metal",
    "PL=F": "futures_metal",
    "PA=F": "futures_metal",
    "HG=F": "futures_metal",
}


def bucket_for_futures_row(row: Dict[str, Any]) -> str:
    """Resolve which weight bucket a futures contract belongs to."""
    sym = row.get("symbol", "")
    if sym in SYMBOL_TO_BUCKET:
        return SYMBOL_TO_BUCKET[sym]
    kind = (row.get("kind") or "").lower()
    return FUTURES_KIND_TO_BUCKET.get(kind, "futures_equity")


# ---------------------------------------------------------------------------
# Hand-crafted priors per bucket — used until 20+ outcomes accumulate
# ---------------------------------------------------------------------------
PRIORS: Dict[str, Dict[str, float]] = {
    # Options & shares share most factors with the existing fusion stack.
    # These priors mirror config.SIGNAL_WEIGHTS but split call/put bias.
    "options_call": {
        "mispricing":   0.13, "iv_rank": 0.05, "skew": 0.04,
        "sentiment_d":  0.10, "fundamentals": 0.08, "insider": 0.08,
        "macro":        0.07, "news": 0.08, "earnings": 0.08,
        "value":        0.10, "congress": 0.06, "social": 0.05, "analyst": 0.08,
    },
    "options_put": {
        "mispricing":   0.13, "iv_rank": 0.05, "skew": 0.04,
        "sentiment_d":  0.10, "fundamentals": 0.08, "insider": 0.08,
        "macro":        0.10, "news": 0.08, "earnings": 0.08,
        "value":        0.06, "congress": 0.05, "social": 0.05, "analyst": 0.10,
    },
    "shares_long": {
        "mispricing":   0.05, "iv_rank": 0.02, "skew": 0.01,
        "sentiment_d":  0.13, "fundamentals": 0.13, "insider": 0.12,
        "macro":        0.06, "news": 0.10, "earnings": 0.08,
        "value":        0.13, "congress": 0.07, "social": 0.05, "analyst": 0.05,
    },

    # ------------------------------------------------------------------
    # Futures buckets — each contract scored against asset-class-relevant factors.
    # Factor names are stable across buckets; weights set to 0 for irrelevant ones.
    # ------------------------------------------------------------------
    # Equity index futures (ES/NQ/YM/RTY): top component sentiment + macro + Trump posts
    "futures_equity": {
        "trend":        0.20,   # 20d momentum z
        "momentum":     0.10,   # 5d momentum z
        "range_pos":    0.05,   # 52w range mean-rev
        "macro_align":  0.18,   # VIX/yield/DXY alignment
        "components":   0.12,   # aggregate sentiment of top SPY/QQQ holdings
        "news":         0.08,   # aggregate news flow on components
        "earnings":     0.05,   # earnings season catalyst
        "social":       0.06,   # Trump posts on Fed/markets
        "congress":     0.04,
        "fred_panel":   0.07,   # CPI, jobless, M2, HY spread
        "iv_rank":      0.03,
        "atr_regime":   0.02,   # vol contraction / expansion
    },
    # Treasury futures (ZB/ZN): all about Fed expectations + macro
    "futures_treasury": {
        "trend":        0.15,
        "momentum":     0.08,
        "range_pos":    0.05,
        "macro_align":  0.20,   # 10Y/3M curve, real yields
        "fred_panel":   0.30,   # CPI, jobless, FOMC proximity
        "news":         0.10,   # Fed/rates news
        "social":       0.08,   # Trump on Fed
        "atr_regime":   0.04,
    },
    # Metals (GC/SI/PL/HG): DXY-inverse + real-yields-inverse + geo
    "futures_metal": {
        "trend":        0.15,
        "momentum":     0.08,
        "range_pos":    0.05,
        "macro_align":  0.25,   # DXY inverse, real yields inverse, VIX positive
        "fred_panel":   0.10,   # M2, CPI
        "news":         0.10,   # geopolitical
        "social":       0.07,   # Trump on Fed/dollar
        "components":   0.05,   # gold-miner ETFs (GDX) sentiment as proxy
        "atr_regime":   0.05,
        "term_structure": 0.10,  # contango/backwardation when available
    },
    # Energy (CL/NG): geopolitics + OPEC + supply news + Trump posts
    "futures_energy": {
        "trend":        0.18,
        "momentum":     0.10,
        "range_pos":    0.05,
        "macro_align":  0.10,   # DXY inverse
        "fred_panel":   0.05,   # industrial production
        "news":         0.20,   # OPEC/Russia/Iran/Venezuela
        "social":       0.12,   # Trump on energy/Russia
        "components":   0.05,   # XLE sentiment
        "atr_regime":   0.05,
        "term_structure": 0.10,
    },
    # Crypto (BTC=F/ETH=F): retail sentiment + risk-on macro
    "futures_crypto": {
        "trend":        0.18,
        "momentum":     0.12,
        "range_pos":    0.05,
        "macro_align":  0.12,   # risk-on flag (VIX low, HY tight, M2 rising)
        "fred_panel":   0.05,
        "news":         0.10,
        "social":       0.10,   # crypto-subreddit + Trump on crypto
        "components":   0.10,   # MSTR/COIN/MARA sentiment as proxy
        "sentiment_d":  0.10,   # r/cryptocurrency / r/bitcoin
        "atr_regime":   0.04,
        "iv_rank":      0.04,
    },
    # Currency (DX=F)
    "futures_currency": {
        "trend":        0.20,
        "momentum":     0.15,
        "range_pos":    0.10,
        "macro_align":  0.20,   # 10Y differential, Fed expectations
        "fred_panel":   0.20,
        "news":         0.10,
        "social":       0.05,
    },
    # Agriculture (ZC/ZS/ZW)
    "futures_agri": {
        "trend":        0.30,
        "momentum":     0.15,
        "range_pos":    0.10,
        "macro_align":  0.10,   # DXY inverse
        "news":         0.20,   # weather/supply
        "atr_regime":   0.05,
        "term_structure": 0.10,
    },
}


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------
def _bucket_path(bucket: str) -> Path:
    return WEIGHTS_DIR / f"{bucket}.json"


def _default_bucket_path(bucket: str) -> Path:
    return DEFAULT_WEIGHTS_DIR / f"{bucket}.json"


def _load_bucket_payload(bucket: str) -> Dict[str, Any] | None:
    """Load the first valid runtime or versioned-default payload."""
    for path in (_bucket_path(bucket), _default_bucket_path(bucket)):
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            weights = payload.get("weights")
            if not isinstance(weights, dict) or not weights:
                raise ValueError("weights must be a non-empty object")
            payload["weights"] = {key: float(value) for key, value in weights.items()}
            return payload
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            log.warning("invalid weight file %s: %s; trying fallback", path, exc)
    return None


def load_weights(bucket: str) -> Dict[str, float]:
    """Return runtime weights, then versioned defaults, then code priors."""
    if bucket not in BUCKET_KEYS:
        log.warning("unknown bucket %r — falling back to options_call priors", bucket)
        bucket = "options_call"
    payload = _load_bucket_payload(bucket)
    if payload is not None:
        return dict(payload["weights"])
    return dict(PRIORS.get(bucket, {}))


def save_weights(bucket: str, weights: Dict[str, float],
                 source: str = "manual",
                 n_samples: int = 0,
                 ic_score: Optional[float] = None,
                 factor_ic: Optional[Dict[str, float]] = None,
                 decay_flags: Optional[List[str]] = None) -> Path:
    """Persist a weight set with metadata."""
    if bucket not in BUCKET_KEYS:
        raise ValueError(f"unknown bucket {bucket}")
    WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "weights": {k: round(float(v), 6) for k, v in weights.items()},
        "meta": {
            "source": source,
            "n_samples": int(n_samples),
            "fitted_at": datetime.now(timezone.utc).isoformat(),
            "ic_score": ic_score,
            "factor_ic": factor_ic or {},
            "decay_flags": decay_flags or [],
        },
    }
    p = _bucket_path(bucket)
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return p


def load_meta(bucket: str) -> Dict[str, Any]:
    """Read just the meta block (for dashboard self-learning panel)."""
    payload = _load_bucket_payload(bucket)
    if payload is not None and isinstance(payload.get("meta"), dict):
        return dict(payload["meta"])
    return {"source": "priors", "n_samples": 0, "fitted_at": None,
            "factor_ic": {}, "decay_flags": []}


def get_factor_priors(bucket: str) -> Dict[str, float]:
    """Hand-crafted priors. Always available, never None."""
    return dict(PRIORS.get(bucket, {}))


def list_bucket_status() -> List[Dict[str, Any]]:
    """Per-bucket summary for the Self-Learning dashboard panel."""
    out = []
    for b in BUCKET_KEYS:
        meta = load_meta(b)
        weights = load_weights(b)
        # Top factors by absolute weight
        top = sorted(weights.items(), key=lambda kv: abs(kv[1]), reverse=True)[:5]
        out.append({
            "bucket": b,
            "source": meta.get("source", "priors"),
            "n_samples": int(meta.get("n_samples", 0) or 0),
            "fitted_at": meta.get("fitted_at"),
            "top_factors": top,
            "decay_flags": meta.get("decay_flags", []),
            "ic_score": meta.get("ic_score"),
        })
    return out


# ---------------------------------------------------------------------------
# Fitting machinery
# ---------------------------------------------------------------------------
def refit_bucket(bucket: str, factor_matrix: pd.DataFrame, pnl: pd.Series,
                 min_lasso: int = 50, min_ic: int = 20,
                 prior_blend: float = 0.5) -> Optional[Dict[str, Any]]:
    """Refit weights for one bucket from realized outcomes.

    Strategy:
      - n >= min_lasso: full Lasso refit (out-of-sample), magnitudes normalize to
        priors range, signs from coefficients. Cap |w| <= 2x prior magnitude.
      - min_ic <= n < min_lasso: per-factor Spearman IC bootstrap, blend with
        priors at `prior_blend` (50/50 default).
      - n < min_ic: keep priors.

    factor_matrix: rows=signals, cols=factor names matching priors.
    pnl: realized return per signal (% or $; either works for IC, Lasso assumes
         normalized).
    """
    priors = get_factor_priors(bucket)
    if factor_matrix is None or factor_matrix.empty or len(factor_matrix) < min_ic:
        # Not enough data — keep priors
        save_weights(bucket, priors, source="priors",
                     n_samples=0 if factor_matrix is None else len(factor_matrix))
        return {"mode": "priors", "n": 0 if factor_matrix is None else len(factor_matrix)}

    # Align columns to priors
    factor_names = [c for c in priors.keys() if c in factor_matrix.columns]
    if not factor_names:
        log.warning("[%s] no factor columns matched priors", bucket)
        save_weights(bucket, priors, source="priors", n_samples=len(factor_matrix))
        return {"mode": "priors", "n": len(factor_matrix), "reason": "no_factor_overlap"}

    X = factor_matrix[factor_names].fillna(0.0).values
    y = pnl.fillna(0.0).values
    n = len(X)

    # Per-factor IC (Spearman rank correlation with realized PnL)
    factor_ic: Dict[str, float] = {}
    for i, fn in enumerate(factor_names):
        try:
            r = pd.Series(X[:, i]).rank().corr(pd.Series(y).rank())
            factor_ic[fn] = float(r) if r is not None and not pd.isna(r) else 0.0
        except Exception:
            factor_ic[fn] = 0.0

    # Decay flags: factors whose IC magnitude dropped vs the previous fit
    prev_meta = load_meta(bucket)
    prev_ic = prev_meta.get("factor_ic", {}) or {}
    decay_flags: List[str] = []
    for fn, ic_now in factor_ic.items():
        ic_prev = prev_ic.get(fn)
        if ic_prev is not None and abs(ic_prev) > 0.05:
            if abs(ic_now) < abs(ic_prev) * 0.5:
                decay_flags.append(fn)

    # Pick fitting mode
    if n >= min_lasso:
        mode = "lasso_refit"
        try:
            from sklearn.linear_model import LassoCV
            cv = max(3, min(5, n // 10))
            model = LassoCV(cv=cv, max_iter=5000, random_state=42).fit(X, y)
            coefs = dict(zip(factor_names, model.coef_.astype(float)))
            r2 = float(model.score(X, y))
        except Exception as e:
            log.warning("[%s] Lasso failed (%s) — falling back to IC blend", bucket, e)
            mode = "ic_bootstrap"
            coefs = {fn: factor_ic.get(fn, 0.0) for fn in factor_names}
            r2 = None

        # Convert raw coefs → normalized weights, signs preserved.
        # Anchor: total magnitude = total prior magnitude (so we don't zero-out the bucket).
        prior_total = sum(abs(v) for v in priors.values()) or 1.0
        coef_total = sum(abs(v) for v in coefs.values()) or 1.0
        scale = prior_total / coef_total
        new_weights: Dict[str, float] = {}
        for fn in priors.keys():
            raw = coefs.get(fn, 0.0) * scale
            # Cap deviation: at most 2x prior magnitude, keep sign
            cap = max(0.02, 2.0 * abs(priors.get(fn, 0.0)))
            new_weights[fn] = float(np.sign(raw) * min(abs(raw), cap)) if raw != 0 else priors[fn]

        save_weights(bucket, new_weights, source=mode, n_samples=n,
                     ic_score=r2, factor_ic=factor_ic, decay_flags=decay_flags)
        return {"mode": mode, "n": n, "r2": r2, "decay_flags": decay_flags}

    if n >= min_ic:
        # IC-bootstrap blend with priors
        mode = "ic_bootstrap"
        # Convert IC to candidate weights: |IC| × sign(IC) gives direction
        ic_total = sum(abs(v) for v in factor_ic.values()) or 1.0
        prior_total = sum(abs(v) for v in priors.values()) or 1.0
        scale = prior_total / ic_total
        blended: Dict[str, float] = {}
        for fn in priors.keys():
            ic = factor_ic.get(fn, 0.0)
            ic_w = ic * scale
            blended[fn] = (1.0 - prior_blend) * priors[fn] + prior_blend * ic_w
            # Cap
            cap = max(0.02, 2.0 * abs(priors.get(fn, 0.0)))
            blended[fn] = float(np.sign(blended[fn]) * min(abs(blended[fn]), cap)) if blended[fn] != 0 else priors[fn]

        save_weights(bucket, blended, source=mode, n_samples=n,
                     factor_ic=factor_ic, decay_flags=decay_flags)
        return {"mode": mode, "n": n, "decay_flags": decay_flags}

    # Otherwise priors
    save_weights(bucket, priors, source="priors", n_samples=n)
    return {"mode": "priors", "n": n}


def apply_weights(factor_matrix: pd.DataFrame, bucket: str) -> pd.Series:
    """Compute weighted score per row using bucket's active weights.

    Missing factors in factor_matrix are treated as 0 (skipped).
    """
    weights = load_weights(bucket)
    if factor_matrix is None or factor_matrix.empty or not weights:
        return pd.Series(0.0, index=(factor_matrix.index if factor_matrix is not None else []))
    cols = [c for c in weights.keys() if c in factor_matrix.columns]
    if not cols:
        return pd.Series(0.0, index=factor_matrix.index)
    X = factor_matrix[cols].fillna(0.0).values
    w = np.array([weights[c] for c in cols])
    return pd.Series(X @ w, index=factor_matrix.index)


# ---------------------------------------------------------------------------
# Initialization helper — write priors to disk on cold start
# ---------------------------------------------------------------------------
def initialize_priors(force: bool = False) -> int:
    """Make sure every runtime bucket has a weights file.

    Versioned defaults are copied exactly on cold start so shipped learned
    weights and their provenance remain intact. Code priors are used only when
    no versioned default exists.

    Returns the number of buckets initialized.
    """
    WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    for b in BUCKET_KEYS:
        p = _bucket_path(b)
        if force or not p.exists():
            default_path = _default_bucket_path(b)
            if default_path.exists():
                p.write_text(default_path.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                save_weights(b, get_factor_priors(b), source="priors", n_samples=0)
            n += 1
    return n
