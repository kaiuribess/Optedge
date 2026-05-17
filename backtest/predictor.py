"""Return predictor + auto-retrain.

Two jobs:
  1. fit_return_predictor() — given historical price returns + factor z-scores,
     fit a Lasso regression that maps factor z's to expected forward return.
     Saves the coefficient vector to data/predictor_coefs.json.
  2. update_runtime_weights() — given forward test data (realized P&L) and
     backtest IC analysis, refits SIGNAL_WEIGHTS via Lasso and writes them
     to config_runtime.py (which run.py auto-loads if present).

Bootstrap path: when no forward-test data exists, seed the predictor from
the backtest IC × Q5-Q1 spread. The system is therefore calibrated FROM DAY
ONE — it doesn't need 30 days of logs to start predicting returns.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
import pandas as pd

import sys
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("optedge.predictor")
COEFS_PATH = ROOT / "data" / "predictor_coefs.json"
LAST_IC_PATH = ROOT / "data" / "last_ic.parquet"
RUNTIME_CONFIG_PATH = ROOT / "config_runtime.py"


def cache_ic(ic_df: pd.DataFrame):
    """Persist the latest IC analysis so subsequent runs can use it as fallback."""
    if ic_df is None or ic_df.empty:
        return
    try:
        LAST_IC_PATH.parent.mkdir(exist_ok=True)
        ic_df.to_parquet(LAST_IC_PATH, index=False)
    except Exception as e:
        log.debug("failed to cache IC: %s", e)


def load_cached_ic() -> Optional[pd.DataFrame]:
    if not LAST_IC_PATH.exists():
        return None
    try:
        return pd.read_parquet(LAST_IC_PATH)
    except Exception:
        return None

# Default horizon for option-buying predictions (matches typical 14-30 DTE picks).
DEFAULT_HORIZON_DAYS = 14

# Z-score columns we predict from
Z_COLS = [
    "z_mispricing", "z_iv_rank", "z_skew",
    "z_sent", "z_fund", "z_insider",
    "z_macro", "z_news", "z_earnings", "z_value",
]

# Map factor name (used in IC) to its z-column in fusion output
FACTOR_TO_ZCOL = {
    "value_score":     "z_value",
    "fund_score":      "z_fund",
    "sentiment_delta": "z_sent",
    "insider_score":   "z_insider",
}

ZCOL_TO_SIGNAL = {
    "z_mispricing": "mispricing",
    "z_iv_rank": "iv_rank",
    "z_skew": "skew",
    "z_sent": "sentiment_d",
    "z_fund": "fundamentals",
    "z_insider": "insider",
    "z_macro": "macro",
    "z_news": "news",
    "z_earnings": "earnings",
    "z_value": "value",
}


def _bootstrap_coefs_from_ic(ic_df: pd.DataFrame, horizon: int = DEFAULT_HORIZON_DAYS) -> Dict[str, float]:
    """Seed coefficients from backtest IC × Q5-Q1 spread when no forward data exists.

    Logic: a Q5-Q1 spread of S% spans roughly 4 z-units (top 20% mean ≈ +1.3z,
    bottom 20% mean ≈ -1.3z). So per-z-unit return ≈ S / 2.6.
    """
    if ic_df is None or ic_df.empty:
        return {c: 0.0 for c in Z_COLS}

    # Pick the closest horizon row available
    available = sorted(ic_df["horizon_days"].unique())
    target_h = min(available, key=lambda h: abs(h - horizon)) if available else horizon

    coefs: Dict[str, float] = {c: 0.0 for c in Z_COLS}
    sub = ic_df[ic_df["horizon_days"] == target_h]
    for _, r in sub.iterrows():
        zcol = FACTOR_TO_ZCOL.get(r["factor"])
        if zcol is None:
            continue
        # Scale: spread is total range; per-z-unit ≈ spread / 2.6
        coefs[zcol] = float(r["spread"]) / 2.6
    log.info("bootstrapped predictor coefs from IC at horizon %dd", target_h)
    return coefs


def _time_decay_weights(entry_times: pd.Series, half_life_days: float = 30.0) -> np.ndarray:
    """Exponential decay so recent signals weight more.

    half_life_days=30 means a signal from 30 days ago weights half as much as today's.
    Returns array of weights aligned to entry_times index.
    """
    now = datetime.now(timezone.utc)
    weights = []
    for et in entry_times:
        try:
            t = pd.to_datetime(et, utc=True)
            age_days = max(0.0, (now - t).total_seconds() / 86400)
        except Exception:
            age_days = 30.0
        # exponential decay: w = 0.5 ** (age / half_life)
        weights.append(0.5 ** (age_days / half_life_days))
    return np.array(weights, dtype=float)


def _fit_from_forward(forward_signals: pd.DataFrame,
                       regime: Optional[str] = None) -> Tuple[Dict[str, float], Dict[str, Any]]:
    """Refit coefs from realized P&L using Huber regression + time-decay weights.

    `regime` filters the data to only signals from the same regime (risk_on/risk_off/neutral)
    if a `regime` column is available in the signals dataframe.
    """
    df = forward_signals.copy()
    target_col = "pnl_pct_after_slippage" if "pnl_pct_after_slippage" in df.columns else "pnl_pct"
    if target_col not in df.columns:
        return {}, {"reason": "missing_target", "missing": ["pnl_pct"]}
    missing_features = [c for c in Z_COLS if c not in df.columns]
    for c in missing_features:
        df[c] = 0.0

    # Optional per-regime filter
    if regime and "regime" in df.columns:
        df = df[df["regime"] == regime]
    df = df.dropna(subset=[target_col])
    if len(df) < 50:
        return {}, {"reason": "insufficient_samples", "n": len(df)}

    X = df[Z_COLS].fillna(0.0).values
    y = df[target_col].values
    # Clip extreme outcomes (-100% blowups distort the fit even with Huber)
    y = np.clip(y, -1.0, 2.0)
    # Time-decay weights: recent signals matter more
    sw = (_time_decay_weights(df["entry_time"], half_life_days=30.0)
          if "entry_time" in df.columns else np.ones(len(df)))

    coefs: Dict[str, float] = {}
    meta: Dict[str, Any] = {
        "n": len(df),
        "regime": regime or "all",
        "target": target_col,
        "filled_missing_features": missing_features,
    }

    # Try Huber first (robust to outliers), fall back to LassoCV
    try:
        from sklearn.linear_model import HuberRegressor
        model = HuberRegressor(epsilon=1.35, max_iter=200).fit(X, y, sample_weight=sw)
        coefs = dict(zip(Z_COLS, model.coef_.astype(float)))
        meta.update({
            "reason": "huber_with_time_decay",
            "intercept": float(model.intercept_),
            "scale": float(model.scale_),
        })
        log.info("Huber refit from %d signals (half-life 30d, regime=%s)",
                 len(df), regime or "all")
        return coefs, meta
    except Exception as e:
        log.debug("Huber failed (%s), falling back to LassoCV", e)

    try:
        from sklearn.linear_model import LassoCV
        model = LassoCV(cv=min(5, len(df) // 10), max_iter=5000,
                        random_state=42).fit(X, y, sample_weight=sw)
        coefs = dict(zip(Z_COLS, model.coef_.astype(float)))
        meta.update({
            "reason": "lasso_with_time_decay",
            "alpha": float(model.alpha_),
            "intercept": float(model.intercept_),
            "r2": float(model.score(X, y)),
        })
        log.info("LassoCV refit from %d signals (half-life 30d, regime=%s)",
                 len(df), regime or "all")
        return coefs, meta
    except Exception as e:
        return {}, {"reason": f"fit_error: {e}"}


def fit_return_predictor(forward_signals: pd.DataFrame = None,
                         ic_df: pd.DataFrame = None,
                         horizon: int = DEFAULT_HORIZON_DAYS) -> Dict[str, Any]:
    """Fit/refit and persist the return predictor coefficients."""
    coefs: Dict[str, float] = {}
    meta: Dict[str, Any] = {"horizon": horizon, "fitted_at": datetime.now(timezone.utc).isoformat()}

    if forward_signals is not None and not forward_signals.empty:
        coefs, fwd_meta = _fit_from_forward(forward_signals)
        meta.update(fwd_meta)

    if not coefs and ic_df is not None and not ic_df.empty:
        coefs = _bootstrap_coefs_from_ic(ic_df, horizon=horizon)
        meta["source"] = "ic_bootstrap"
    elif coefs:
        meta["source"] = "forward_refit"
    else:
        coefs = {c: 0.0 for c in Z_COLS}
        meta["source"] = "zero_init"

    # Clamp absurd values: any single coef >|0.05| gets capped (5% / z-unit)
    for k in list(coefs.keys()):
        coefs[k] = max(-0.05, min(0.05, float(coefs[k])))

    payload = {"coefs": coefs, "meta": meta}
    COEFS_PATH.parent.mkdir(exist_ok=True)
    COEFS_PATH.write_text(json.dumps(payload, indent=2))
    return payload


def load_predictor_coefs() -> Dict[str, float]:
    """Load previously-fit coefficients. Returns zeros if none."""
    if not COEFS_PATH.exists():
        return {c: 0.0 for c in Z_COLS}
    try:
        data = json.loads(COEFS_PATH.read_text())
        return {c: float(data.get("coefs", {}).get(c, 0.0)) for c in Z_COLS}
    except Exception:
        return {c: 0.0 for c in Z_COLS}


def predict_returns(ranked: pd.DataFrame, coefs: Dict[str, float] = None) -> pd.Series:
    """Apply coefficients to z-scores, returning predicted % return per row."""
    if coefs is None:
        coefs = load_predictor_coefs()
    if ranked is None or ranked.empty:
        return pd.Series(dtype=float)
    used_cols = [c for c in Z_COLS if c in ranked.columns]
    if not used_cols:
        return pd.Series(0.0, index=ranked.index)
    M = ranked[used_cols].fillna(0).values
    w = np.array([coefs.get(c, 0.0) for c in used_cols])
    return pd.Series(M @ w, index=ranked.index)


def add_predictions_to_options(ranked: pd.DataFrame, coefs: Dict[str, float] = None) -> pd.DataFrame:
    """Attach pred_stock_return_pct and pred_option_return_pct columns."""
    if ranked is None or ranked.empty:
        return ranked
    df = ranked.copy()
    df["pred_stock_return_pct"] = predict_returns(df, coefs)
    # Option leverage ≈ 1 / |delta|, capped to keep things sane
    deltas = df.get("delta", pd.Series(0.5, index=df.index)).abs().clip(0.10, 0.95)
    leverage = 1.0 / deltas
    # Side-aligned: calls profit on +stock_return; puts profit on -stock_return.
    side_mult = np.where(df["side"] == "call", 1.0, -1.0)
    df["pred_option_return_pct"] = df["pred_stock_return_pct"] * leverage * side_mult
    # Cap option predictions at ±200% (per-trade)
    df["pred_option_return_pct"] = df["pred_option_return_pct"].clip(-2.0, 2.0)
    return df


def add_predictions_to_shares(ranked: pd.DataFrame, coefs: Dict[str, float] = None) -> pd.DataFrame:
    """Attach pred_stock_return_pct to shares output."""
    if ranked is None or ranked.empty:
        return ranked
    df = ranked.copy()
    df["pred_stock_return_pct"] = predict_returns(df, coefs)
    return df


# -------- Auto-retrain SIGNAL_WEIGHTS --------------------------------
def _has_enough_history_for_lasso(forward_signals: pd.DataFrame,
                                    min_samples: int = 500,
                                    min_unique_days: int = 10) -> bool:
    """v20.7 — walk-forward validation guard.

    A Lasso refit on a small / single-day sample overfits the one weird day
    and writes garbage into config_runtime.py for every subsequent run.
    Refuse to refit until we have BOTH:
      - ≥ min_samples (default 500) logged signals with realized P&L, AND
      - ≥ min_unique_days (default 10) distinct trading days represented.
    Until then we stick with the IC-based weight adjustment (slower drift,
    far more robust).
    """
    if forward_signals is None or forward_signals.empty:
        return False
    if "pnl_pct" not in forward_signals.columns:
        return False
    pnl = forward_signals["pnl_pct"].dropna()
    if len(pnl) < min_samples:
        return False
    # Count unique trading days from entry_time
    if "entry_time" not in forward_signals.columns:
        return False
    try:
        ts = pd.to_datetime(forward_signals["entry_time"], errors="coerce")
        unique_days = ts.dt.date.nunique()
        return unique_days >= min_unique_days
    except Exception:
        return False


def _rolling_forward_ic_weights(forward_signals: pd.DataFrame,
                                baseline: Dict[str, float],
                                lookback_days: int = 90,
                                min_samples: int = 100,
                                min_unique_days: int = 5) -> Optional[Dict[str, float]]:
    """Reweight factors from rolling forward IC before trusting full Lasso."""
    if forward_signals is None or forward_signals.empty:
        return None
    target_col = "pnl_pct_after_slippage" if "pnl_pct_after_slippage" in forward_signals.columns else "pnl_pct"
    if target_col not in forward_signals.columns or "entry_time" not in forward_signals.columns:
        return None
    df = forward_signals.copy()
    df["entry_time"] = pd.to_datetime(df["entry_time"], errors="coerce", utc=True)
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=lookback_days)
    df = df[df["entry_time"] >= cutoff].dropna(subset=[target_col])
    if len(df) < min_samples or df["entry_time"].dt.date.nunique() < min_unique_days:
        return None

    ic_by_signal: Dict[str, float] = {}
    for z_col, signal_key in ZCOL_TO_SIGNAL.items():
        if z_col not in df.columns or signal_key not in baseline:
            continue
        sub = df[[z_col, target_col]].copy()
        sub[z_col] = pd.to_numeric(sub[z_col], errors="coerce")
        sub[target_col] = pd.to_numeric(sub[target_col], errors="coerce")
        sub = sub.dropna()
        if len(sub) < min_samples or sub[z_col].nunique() < 2:
            continue
        ic = sub[z_col].corr(sub[target_col])
        if not pd.isna(ic):
            ic_by_signal[signal_key] = float(ic)
    if not ic_by_signal:
        return None

    raw = {}
    for key, base in baseline.items():
        ic = ic_by_signal.get(key)
        if ic is None:
            raw[key] = max(float(base) * 0.50, 0.001)
        elif ic <= -0.05:
            raw[key] = max(float(base) * 0.20, 0.001)
        elif ic < 0:
            raw[key] = max(float(base) * 0.50, 0.001)
        else:
            raw[key] = max(float(base), 0.001) * (1.0 + min(ic, 0.25) * 6.0)
    total = sum(raw.values())
    if total <= 0:
        return None
    weights = {k: round(v / total, 4) for k, v in raw.items()}
    log.info("rolling %dd IC weights from %d forward samples; strongest=%s",
             lookback_days, len(df), max(weights, key=weights.get))
    return weights


def update_runtime_weights(forward_signals: pd.DataFrame = None,
                            ic_df: pd.DataFrame = None,
                            min_samples: int = 500) -> Optional[Dict[str, float]]:
    """Refit fusion weights based on what's actually predictive.

    v20.7: Lasso path requires ≥500 logged signals AND ≥10 distinct trading
    days (walk-forward guard) so a single weird day doesn't poison the model.
    Until that threshold is met we use IC-based adjustment only.

    Strategy:
      - Start from config.SIGNAL_WEIGHTS as baseline.
      - For factors with measured IC: multiply baseline weight by adjustment.
        - Positive IC > 0.05  → upweight 1.20×
        - Strong positive IC > 0.10 → upweight 1.50×
        - Negative IC < -0.05 → downweight 0.50×
        - Strong negative IC < -0.10 → downweight 0.25×
      - For factors WITHOUT IC data: keep baseline weight.
      - Renormalize to sum to 1.
    """
    # Get baseline from the user's config.py
    try:
        import config as _cfg
        baseline = dict(_cfg.SIGNAL_WEIGHTS)
    except Exception:
        baseline = {
            "mispricing": 0.18, "iv_rank": 0.07, "skew": 0.05, "sentiment_d": 0.13,
            "fundamentals": 0.10, "insider": 0.10, "macro": 0.07,
            "news": 0.10, "earnings": 0.10, "value": 0.10,
        }

    # Forward-test refit (most authoritative — but only with walk-forward guard)
    if forward_signals is not None and not forward_signals.empty and \
       "pnl_pct" in forward_signals.columns:
        df = forward_signals.copy()
        for c in Z_COLS:
            if c not in df.columns:
                df[c] = 0.0
        target_col = "pnl_pct_after_slippage" if "pnl_pct_after_slippage" in df.columns else "pnl_pct"
        df = df.dropna(subset=[target_col])
        # v20.7: explicit walk-forward guard. Block Lasso refit until we have
        # enough samples spread across enough distinct days.
        if not _has_enough_history_for_lasso(df, min_samples=min_samples,
                                              min_unique_days=10):
            log.info("walk-forward guard: %d samples / need %d "
                     "across ≥10 distinct days — skipping Lasso refit, using IC only",
                     len(df), min_samples)
            rolling_weights = _rolling_forward_ic_weights(df, baseline)
            if rolling_weights:
                _persist_runtime_weights(rolling_weights, source="rolling_90d_forward_ic")
                return rolling_weights
        elif len(df) >= min_samples:
            try:
                from sklearn.linear_model import LassoCV
                X = df[Z_COLS].values
                y = df[target_col].values
                m = LassoCV(cv=min(5, len(df) // 10), max_iter=5000, random_state=42).fit(X, y)
                abs_coef = np.abs(m.coef_)
                if abs_coef.sum() > 0:
                    z_to_weight = dict(zip(Z_COLS, abs_coef / abs_coef.sum()))
                    weight_map = _normalize_to_signal_weights(z_to_weight)
                    _persist_runtime_weights(weight_map, source=f"forward_lasso_n{len(df)}")
                    log.info("weights refit from %d forward samples (walk-forward guard passed)",
                              len(df))
                    return weight_map
            except Exception as e:
                log.warning("forward weight refit failed: %s", e)

    # IC-based proportional adjustment of the baseline weights
    if ic_df is not None and not ic_df.empty:
        target_h = 7 if 7 in ic_df["horizon_days"].unique() else int(ic_df["horizon_days"].min())
        sub = ic_df[ic_df["horizon_days"] == target_h]

        # Map IC factor name → SIGNAL_WEIGHTS key
        zcol_to_signal = {
            "z_value":   "value",
            "z_fund":    "fundamentals",
            "z_sent":    "sentiment_d",
            "z_insider": "insider",
        }
        ic_lookup: Dict[str, float] = {}
        for _, r in sub.iterrows():
            zcol = FACTOR_TO_ZCOL.get(r["factor"])
            if zcol and zcol in zcol_to_signal:
                ic_lookup[zcol_to_signal[zcol]] = float(r["ic"])

        adjusted = dict(baseline)
        for sig_key, ic in ic_lookup.items():
            if sig_key not in adjusted:
                continue
            if ic > 0.10:
                mult = 1.50
            elif ic > 0.05:
                mult = 1.20
            elif ic < -0.10:
                mult = 0.25
            elif ic < -0.05:
                mult = 0.50
            else:
                mult = 1.0
            adjusted[sig_key] *= mult

        # Renormalize to sum to 1
        s = sum(adjusted.values())
        if s > 0:
            adjusted = {k: round(v / s, 4) for k, v in adjusted.items()}
            _persist_runtime_weights(adjusted, source=f"ic_adjustment_h{target_h}")
            log.info("weights adjusted via IC at h=%dd; %d factors had measured IC",
                     target_h, len(ic_lookup))
            # v20.7 — enumerate which factors had positive vs negative IC so the
            # user can see which signals are actually predictive vs anti-predictive.
            pos = {k: f"+{v:.3f}" for k, v in ic_lookup.items() if v > 0.02}
            neg = {k: f"{v:.3f}"  for k, v in ic_lookup.items() if v < -0.02}
            if pos:
                log.info("  IC pos (predictive):     %s", pos)
            if neg:
                log.info("  IC neg (anti-predictive): %s", neg)
            return adjusted

    return None


def _normalize_to_signal_weights(z_weights: Dict[str, float]) -> Dict[str, float]:
    """Map z-column weights back to SIGNAL_WEIGHTS keys, normalize to sum to 1."""
    name_map = {v: k for k, v in {
        "mispricing":   "z_mispricing",
        "iv_rank":      "z_iv_rank",
        "skew":         "z_skew",
        "sentiment_d":  "z_sent",
        "fundamentals": "z_fund",
        "insider":      "z_insider",
        "macro":        "z_macro",
        "news":         "z_news",
        "earnings":     "z_earnings",
        "value":        "z_value",
    }.items()}
    raw = {name_map.get(k): v for k, v in z_weights.items() if name_map.get(k)}
    # Use absolute value for weight magnitude; sign captured elsewhere
    abs_raw = {k: abs(v) for k, v in raw.items()}
    s = sum(abs_raw.values())
    if s == 0:
        # Default fallback
        return {
            "mispricing": 0.20, "iv_rank": 0.08, "skew": 0.05, "sentiment_d": 0.13,
            "fundamentals": 0.10, "insider": 0.10, "macro": 0.07,
            "news": 0.10, "earnings": 0.10, "value": 0.07,
        }
    return {k: round(v / s, 4) for k, v in abs_raw.items()}


def _persist_runtime_weights(weights: Dict[str, float], source: str = "auto"):
    """Write a config_runtime.py that, when imported, overrides SIGNAL_WEIGHTS."""
    lines = [
        f'"""Auto-generated runtime overrides. Source: {source}',
        f"   Generated: {datetime.now(timezone.utc).isoformat()}",
        f'   Delete this file to revert to config.SIGNAL_WEIGHTS defaults.',
        '"""',
        "",
        "SIGNAL_WEIGHTS = {",
    ]
    for k, v in weights.items():
        lines.append(f'    "{k}": {v},')
    lines.append("}")
    RUNTIME_CONFIG_PATH.write_text("\n".join(lines))


def load_runtime_weights() -> Optional[Dict[str, float]]:
    """If config_runtime.py exists, parse it and return the weight dict."""
    if not RUNTIME_CONFIG_PATH.exists():
        return None
    try:
        ns: Dict[str, Any] = {}
        exec(RUNTIME_CONFIG_PATH.read_text(), ns)
        w = ns.get("SIGNAL_WEIGHTS")
        if isinstance(w, dict):
            return w
    except Exception as e:
        log.debug("failed to load config_runtime.py: %s", e)
    return None
