# Purpose: Preserve a legacy chronological factor diagnostic for compatibility.
"""Legacy chronological factor diagnostic with per-factor attribution.

This module consumes the older ``forward_outcomes_<bucket>`` artifacts. Those
variable-schema outcomes are not the current policy-bound evidence set, so all
results are diagnostic-only and cannot promote weights, clear Edge Lab, or
authorize live review. Current decision evidence comes from
``backtest.fixed_horizon`` and ``backtest.edge_lab``.

Per bucket:
  1. Pull realized outcomes from data/forward_outcomes_<bucket>.parquet
  2. Sort by log_time, split 80/20 in-sample / out-of-sample
  3. Fit weights on first 80%, score the last 20% with those weights
  4. Compute Sharpe, max DD, win rate, profit factor, equity curve
  5. Per-factor attribution: how much realized PnL each factor contributes

Output:
  data/backtest_summary.json   — labeled legacy diagnostic summary
  data/backtest_<asof>.parquet — labeled local diagnostic curve data

No current dashboard or broker-review gate consumes these files.
"""

from __future__ import annotations

import json
import logging
import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engines import learning  # noqa: E402

log = logging.getLogger("optedge.backtest_engine")
DATA_DIR = ROOT / "data"

EVIDENCE_STATUS = "diagnostic_only_legacy_forward_outcomes"
ELIGIBLE_FOR_MODEL_PROMOTION = False
ELIGIBLE_FOR_LIVE_REVIEW = False


def diagnostic_metadata() -> dict[str, Any]:
    """Return the immutable evidence restrictions for this diagnostic."""
    return {
        "evidence_status": EVIDENCE_STATUS,
        "eligible_for_model_promotion": ELIGIBLE_FOR_MODEL_PROMOTION,
        "eligible_for_live_review": ELIGIBLE_FOR_LIVE_REVIEW,
    }


def _diagnostic_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {**diagnostic_metadata(), **payload}


# ---------------------------------------------------------------------------
# Per-bucket walk-forward backtest
# ---------------------------------------------------------------------------
def _equity_curve(realized_dollars: pd.Series) -> list[float]:
    """Cumulative equity curve from realized PnL, starting at 0."""
    if realized_dollars is None or realized_dollars.empty:
        return []
    return realized_dollars.cumsum().tolist()


def _max_drawdown(equity: list[float]) -> float:
    """Max peak-to-trough drawdown of an equity curve."""
    if not equity:
        return 0.0
    peak = equity[0]
    dd = 0.0
    for v in equity:
        peak = max(peak, v)
        dd = min(dd, v - peak)
    return float(dd)


def _sharpe(returns: pd.Series) -> float | None:
    """Annualized Sharpe assuming daily samples."""
    if returns is None or returns.empty:
        return None
    s = returns.std()
    if not s or pd.isna(s) or s == 0:
        return None
    return float((returns.mean() / s) * math.sqrt(252))


def walk_forward_one_bucket(bucket: str, train_frac: float = 0.8) -> dict[str, Any]:
    """Walk-forward backtest of one bucket. Returns stats + per-factor attribution."""
    out_path = DATA_DIR / f"forward_outcomes_{bucket}.parquet"
    if not out_path.exists():
        return _diagnostic_payload({"bucket": bucket, "n": 0, "status": "no_outcomes"})

    try:
        df = pd.read_parquet(out_path)
    except Exception as e:
        log.warning("read %s failed: %s", out_path, e)
        return _diagnostic_payload({"bucket": bucket, "n": 0, "status": "read_failed"})

    if df.empty:
        return _diagnostic_payload({"bucket": bucket, "n": 0, "status": "empty"})

    df = df.copy()
    df["log_time"] = pd.to_datetime(df["log_time"], utc=True, errors="coerce")
    df = df.sort_values("log_time").dropna(subset=["log_time"])
    n = len(df)
    if n < 20:
        return _diagnostic_payload(
            {
                "bucket": bucket,
                "n": n,
                "status": "insufficient_samples",
            }
        )

    priors = learning.get_factor_priors(bucket)
    factor_cols = [f"factor_{k}" for k in priors.keys() if f"factor_{k}" in df.columns]
    # Map any z-cols left over
    zcol_to_factor = {
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
    for zcol, fname in zcol_to_factor.items():
        target = f"factor_{fname}"
        if zcol in df.columns and target not in df.columns:
            df[target] = df[zcol]
    factor_cols = [f"factor_{k}" for k in priors.keys() if f"factor_{k}" in df.columns]
    if not factor_cols:
        return _diagnostic_payload(
            {
                "bucket": bucket,
                "n": n,
                "status": "no_factor_columns",
            }
        )

    fmat = df[factor_cols].fillna(0.0).copy()
    fmat.columns = [c.replace("factor_", "") for c in fmat.columns]
    pnl_col = "realized_dollars" if "realized_dollars" in df.columns else "pnl_pct"
    pnl = pd.to_numeric(df[pnl_col], errors="coerce").fillna(0.0).reset_index(drop=True)

    # Walk-forward split
    cut = max(15, int(n * train_frac))
    train_X = fmat.iloc[:cut].reset_index(drop=True)
    train_y = pnl.iloc[:cut].reset_index(drop=True)
    test_X = fmat.iloc[cut:].reset_index(drop=True)
    test_y = pnl.iloc[cut:].reset_index(drop=True)

    # Fit on training set
    train_weights: dict[str, float] = dict(priors)
    fit_mode = "priors"
    r2_in = None
    if len(train_X) >= 50:
        try:
            from sklearn.linear_model import LassoCV

            cv = max(3, min(5, len(train_X) // 10))
            model = LassoCV(cv=cv, max_iter=5000, random_state=42).fit(
                train_X.values, train_y.values
            )
            coefs = dict(zip(train_X.columns, model.coef_.astype(float), strict=False))
            # Re-anchor to prior magnitudes
            prior_total = sum(abs(v) for v in priors.values()) or 1.0
            coef_total = sum(abs(v) for v in coefs.values()) or 1.0
            scale = prior_total / coef_total
            train_weights = {
                fn: float(
                    np.sign(coefs.get(fn, 0.0))
                    * min(
                        abs(coefs.get(fn, 0.0) * scale), max(0.02, 2.0 * abs(priors.get(fn, 0.0)))
                    )
                )
                for fn in priors.keys()
            }
            r2_in = float(model.score(train_X.values, train_y.values))
            fit_mode = "lasso"
        except Exception as e:
            log.debug("[%s] backtest lasso failed: %s", bucket, e)

    # Score the OOS test set with train_weights
    cols = [c for c in train_weights.keys() if c in test_X.columns]
    w = np.array([train_weights[c] for c in cols])
    test_scores = test_X[cols].values @ w if cols and len(test_X) > 0 else np.zeros(len(test_X))

    # Define a "trade" = score crosses |0.3| threshold; long if positive, short if negative.
    # Realized PnL is just the bucket's recorded outcome (already directional from is_long).
    trades = pd.DataFrame({"score": test_scores, "pnl": test_y.values})
    if len(trades) == 0:
        return _diagnostic_payload(
            {
                "bucket": bucket,
                "n": n,
                "status": "no_oos_trades",
                "fit_mode": fit_mode,
            }
        )

    # Out-of-sample summary
    n_oos = len(trades)
    pnl_total = float(trades["pnl"].sum())
    wins = int((trades["pnl"] > 0).sum())
    losses = int((trades["pnl"] < 0).sum())
    win_rate = wins / n_oos if n_oos else 0
    avg_win = float(trades.loc[trades["pnl"] > 0, "pnl"].mean() or 0)
    avg_loss = float(trades.loc[trades["pnl"] < 0, "pnl"].mean() or 0)
    profit_factor = (
        float(
            abs(
                trades.loc[trades["pnl"] > 0, "pnl"].sum()
                / (trades.loc[trades["pnl"] < 0, "pnl"].sum() or -1)
            )
        )
        if losses
        else None
    )
    equity_curve = _equity_curve(trades["pnl"])
    max_dd = _max_drawdown(equity_curve)

    # Sharpe of normalized PnL
    sharpe = _sharpe(trades["pnl"] / max(1.0, trades["pnl"].abs().mean() or 1.0))

    # Per-factor attribution: factor value × weight × sign of realized PnL
    attribution = []
    for fn in cols:
        fvals = test_X[fn].values
        contrib = float(np.sum(fvals * train_weights[fn] * np.sign(trades["pnl"].values)))
        attribution.append(
            {
                "factor": fn,
                "weight": float(train_weights[fn]),
                "pnl_contribution_proxy": contrib,
            }
        )
    attribution.sort(key=lambda r: abs(r["pnl_contribution_proxy"]), reverse=True)

    return _diagnostic_payload(
        {
            "bucket": bucket,
            "n": n,
            "n_train": len(train_X),
            "n_oos": n_oos,
            "fit_mode": fit_mode,
            "r2_in_sample": r2_in,
            "oos_pnl": pnl_total,
            "oos_win_rate": round(win_rate, 4),
            "oos_avg_win": round(avg_win, 4),
            "oos_avg_loss": round(avg_loss, 4),
            "oos_profit_factor": profit_factor,
            "oos_sharpe": sharpe,
            "oos_max_dd": max_dd,
            "equity_curve": equity_curve,
            "factor_attribution": attribution,
            "status": "ok",
        }
    )


def run_full_backtest() -> dict[str, Any]:
    """Run every legacy bucket and persist explicitly labeled diagnostics."""
    DATA_DIR.mkdir(exist_ok=True)
    results = {}
    for b in learning.BUCKET_KEYS:
        try:
            results[b] = walk_forward_one_bucket(b)
        except Exception as e:
            log.warning("[%s] backtest failed: %s", b, e)
            results[b] = {"bucket": b, "status": f"error: {e}"}

    # Compatibility artifact for explicit local inspection only.
    summary = {
        **diagnostic_metadata(),
        "asof": datetime.now(UTC).isoformat(),
        "buckets": {},
    }
    for b, r in results.items():
        if r.get("status") != "ok":
            summary["buckets"][b] = {"status": r.get("status", "no_data"), "n": r.get("n", 0)}
            continue
        summary["buckets"][b] = {
            "n_total": r.get("n"),
            "n_train": r.get("n_train"),
            "n_oos": r.get("n_oos"),
            "fit_mode": r.get("fit_mode"),
            "oos_pnl": round(r.get("oos_pnl", 0.0), 2),
            "oos_win_rate": r.get("oos_win_rate"),
            "oos_profit_factor": (
                round(r.get("oos_profit_factor"), 2)
                if r.get("oos_profit_factor") is not None
                else None
            ),
            "oos_sharpe": (
                round(r.get("oos_sharpe"), 2) if r.get("oos_sharpe") is not None else None
            ),
            "oos_max_dd": round(r.get("oos_max_dd", 0.0), 2),
            "equity_curve": r.get("equity_curve", [])[:100],  # cap series length
            "top_factors": [
                {
                    "factor": fa["factor"],
                    "weight": round(fa["weight"], 4),
                    "pnl_contrib": round(fa["pnl_contribution_proxy"], 2),
                }
                for fa in r.get("factor_attribution", [])[:5]
            ],
            "status": "ok",
        }

    summary_path = DATA_DIR / "backtest_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    # Also save full per-bucket equity curves as parquet (for deeper drill-down)
    asof_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    rows = []
    for b, r in results.items():
        for i, eq in enumerate(r.get("equity_curve", []) or []):
            rows.append({"bucket": b, "trade_idx": i, "cum_pnl": eq})
    if rows:
        try:
            detail = pd.DataFrame(rows)
            detail["evidence_status"] = EVIDENCE_STATUS
            detail["eligible_for_model_promotion"] = ELIGIBLE_FOR_MODEL_PROMOTION
            detail["eligible_for_live_review"] = ELIGIBLE_FOR_LIVE_REVIEW
            detail.to_parquet(DATA_DIR / f"backtest_{asof_tag}.parquet", index=False)
        except Exception as e:
            log.debug("save backtest detail: %s", e)

    return _diagnostic_payload({"summary_path": str(summary_path), "results": results})


def load_backtest_summary() -> dict[str, Any] | None:
    """Read the compatibility summary and enforce diagnostic labels in memory."""
    p = DATA_DIR / "backtest_summary.json"
    if not p.exists():
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        return _diagnostic_payload(payload)
    except Exception:
        return None
