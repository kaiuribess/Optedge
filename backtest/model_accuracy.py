# Purpose: Compute quarantined diagnostic pricing-model weights without promotion.
"""Quarantined current-mid pricing-model diagnostic.

Reads `logs/model_predictions_*.parquet` (written by engines/mispricing.py)
and the current options chain, then computes mean-absolute-error per pricing
model for each volatility regime. The result is diagnostic-only and is returned
to the caller in memory. This module never writes runtime pricing weights or
their production history.

Workflow:
  1. Collect all model_predictions logs from the last 14 days (~672 files
     in a 30-min loop, capped at 2k for memory).
  2. For each logged prediction:
       - Fetch the option's current mid via chain_provider.
       - error[model] = |market_mid_now - theo_model_at_prediction_time| / market_mid_now
  3. Group by regime (low_vol / normal / high_vol).
  4. Weight[model, regime] ∝ 1 / mean_abs_error[model, regime]
     Normalize so weights sum to 1.
  5. Return the diagnostic weights without persisting them.

Comparing an old theoretical value with a variable-age current mid is not a
fixed-horizon, out-of-sample forecast test. It must not update live pricing
weights. The legacy scorer remains available only behind an explicit
diagnostic flag while a fill-grade immutable outcome ledger is developed.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("optedge.model_accuracy")

LOG_DIR = ROOT / "logs"
DATA_DIR = ROOT / "data"
WEIGHTS_FILE = DATA_DIR / "model_weights.json"

MODELS = ("bs", "crr", "bjs", "cboe")
LOOKBACK_DAYS = 14
MAX_FILES = 2000
MAX_CONTRACTS_TO_REPRICE = 800  # cap network calls per refit


def _load_recent_predictions(lookback_days: int = LOOKBACK_DAYS) -> pd.DataFrame:
    """Read both .parquet and .json prediction logs from the last N days."""
    if not LOG_DIR.exists():
        return pd.DataFrame()
    files = sorted(
        list(LOG_DIR.glob("model_predictions_*.parquet"))
        + list(LOG_DIR.glob("model_predictions_*.json"))
    )
    if not files:
        return pd.DataFrame()
    cutoff = datetime.now(UTC) - timedelta(days=lookback_days)
    cutoff_str = cutoff.strftime("%Y%m%d_%H%M%S")
    recent = [f for f in files if f.stem.split("_", 2)[-1] >= cutoff_str]
    if len(recent) > MAX_FILES:
        recent = recent[-MAX_FILES:]
    dfs = []
    for f in recent:
        try:
            if f.suffix == ".parquet":
                dfs.append(pd.read_parquet(f))
            else:
                dfs.append(pd.read_json(f, orient="records"))
        except Exception as e:
            log.debug("skip %s: %s", f.name, e)
            continue
    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True)


def _current_mid_for(predictions: pd.DataFrame) -> pd.DataFrame:
    """Re-fetch current chains for unique tickers in `predictions` and
    join the current market_mid back onto each prediction row."""
    try:
        import chain_provider
    except Exception as e:
        log.warning("model_accuracy: chain_provider not importable (%s)", e)
        return predictions.assign(market_mid_now=float("nan"))

    tickers = sorted(set(predictions["ticker"].astype(str).str.upper()))
    # Cap to avoid hammering the chain providers for a stale prediction set
    if len(tickers) > 100:
        # Prefer the most-recently-predicted tickers
        recent_ts = predictions.groupby("ticker")["asof"].max().sort_values(ascending=False)
        tickers = list(recent_ts.head(100).index)

    chain_blobs: dict[str, dict] = {}
    for tk in tickers:
        try:
            b = chain_provider.fetch_chain(tk, cache_age=300)
            if b and b.get("chains"):
                chain_blobs[tk] = b
        except Exception:
            continue

    def _mid_lookup(row) -> float | None:
        tk = str(row["ticker"]).upper()
        blob = chain_blobs.get(tk)
        if not blob:
            return None
        df = blob["chains"].get(str(row["expiry"]))
        if df is None or df.empty:
            return None
        hit = df[
            (df["strike"].round(2) == round(float(row["strike"]), 2)) & (df["side"] == row["side"])
        ]
        if hit.empty:
            return None
        r = hit.iloc[0]
        bid, ask = float(r.get("bid") or 0), float(r.get("ask") or 0)
        if bid > 0 and ask > 0 and ask >= bid:
            return (bid + ask) / 2
        last = float(r.get("lastPrice") or 0)
        return last if last > 0 else None

    return predictions.assign(market_mid_now=predictions.apply(_mid_lookup, axis=1))


def _compute_mae_by_regime(scored: pd.DataFrame) -> dict[str, dict[str, float]]:
    """Per (regime, model) mean absolute % error."""
    out: dict[str, dict[str, float]] = {}
    for regime, group in scored.groupby("regime"):
        per_model = {}
        for m in MODELS:
            col = f"theo_{m}"
            if col not in group.columns:
                continue
            sub = group[(group["market_mid_now"] > 0) & (group[col].notna()) & (group[col] > 0)]
            if sub.empty:
                continue
            errs = ((sub[col] - sub["market_mid_now"]).abs() / sub["market_mid_now"]).clip(0, 5)
            per_model[m] = float(errs.mean())
        if per_model:
            out[str(regime)] = per_model
    return out


def _mae_to_weights(mae_by_regime: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """Convert per-model MAE to normalized weights (∝ 1 / MAE), with a
    meaningful floor and ceiling so no single model dominates the ensemble.

    Why we don't let CBOE (or any model) get >65% weight:
      - CBOE's `theo` is derived from the live IV surface, so by construction
        it's the closest thing to market mid. Optimizing for mid-prediction
        accuracy would always crown it ~85%+ and turn the ensemble into a
        CBOE rebroadcast. We want each model's independent opinion to
        contribute, because the SIGNAL is in the DISAGREEMENT, not the consensus.
      - BS/CRR/BJS provide independent fair-value estimates using a smoothed
        vol input. When they all agree but the market mid (≈ CBOE theo)
        disagrees, that's the real mispricing signal.

    Constraints:
      - Floor:  every model gets at least 10% weight
      - Ceiling: no model exceeds 65% weight
    """
    weights: dict[str, dict[str, float]] = {}
    FLOOR = 0.10
    CEILING = 0.55  # 4 models: 0.55 + 3*0.10 = 0.85 leaves 0.15 buffer
    EPS = 1e-9
    for regime, mae in mae_by_regime.items():
        inv = {m: 1.0 / max(e, 0.01) for m, e in mae.items()}
        total = sum(inv.values())
        if total <= 0:
            continue
        # Initial weight ∝ 1/MAE
        w = {m: v / total for m, v in inv.items()}
        n = len(w)
        # Water-fill clipping: iterate until sum == 1 with all values in
        # [FLOOR, CEILING]. With FLOOR=0.10, CEILING=0.55, n=4 we have
        # 4*0.10=0.40 <= 1.0 and 1*0.55+3*0.10=0.85 ≥ 0.55: solvable.
        for _ in range(n + 2):
            capped = {m: max(FLOOR, min(CEILING, v)) for m, v in w.items()}
            s = sum(capped.values())
            if abs(s - 1.0) < EPS:
                w = capped
                break
            # Models not pinned to a boundary -> redistribute the gap to them
            free = [m for m, v in capped.items() if FLOOR + EPS < v < CEILING - EPS]
            if not free:
                # Everything pinned — distribute the gap (1 - s) equally to
                # all models that AREN'T at the ceiling (they have room up).
                up_room = [m for m, v in capped.items() if v < CEILING - EPS]
                if not up_room:
                    # Numerically infeasible — fall back to plain normalize
                    w = {m: v / s for m, v in capped.items()}
                    break
                bump = (1.0 - s) / len(up_room)
                w = {m: (v + bump if m in up_room else v) for m, v in capped.items()}
            else:
                fixed_sum = sum(v for m, v in capped.items() if m not in free)
                free_target = 1.0 - fixed_sum
                free_current = sum(capped[m] for m in free)
                if free_current <= 0:
                    w = {m: v / s for m, v in capped.items()}
                    break
                scale = free_target / free_current
                w = {m: (v * scale if m in free else capped[m]) for m, v in capped.items()}
        else:
            # Hit iteration cap — final normalize
            s = sum(w.values())
            if s > 0:
                w = {m: v / s for m, v in w.items()}
        weights[regime] = w
    return weights


def refit_weights(
    *,
    allow_lookahead_diagnostic: bool = False,
) -> dict[str, dict[str, float]] | None:
    """Return diagnostic weights computed from variable-age current mids.

    This legacy calculation is deliberately compute-only. Even with the
    explicit opt-in flag it cannot promote its result to ``model_weights.json``
    or append to the runtime weight history. Production model updates require
    fixed-horizon, out-of-sample evidence through a separate promotion path.
    """
    if not allow_lookahead_diagnostic:
        log.warning(
            "model_accuracy promotion quarantined: variable-age current-mid scoring "
            "is not fixed-horizon out-of-sample evidence"
        )
        return None
    preds = _load_recent_predictions()
    if preds.empty:
        log.info("model_accuracy: no recent model_predictions logs to score")
        return None
    log.info(
        "model_accuracy: scoring %d predictions across %d tickers",
        len(preds),
        preds["ticker"].nunique(),
    )
    # Cap rows we'll round-trip — most actionable per ticker
    if len(preds) > MAX_CONTRACTS_TO_REPRICE:
        # Prefer recently-predicted, OI-rich contracts via sort by asof
        preds = preds.sort_values("asof").tail(MAX_CONTRACTS_TO_REPRICE)
    scored = _current_mid_for(preds)
    scored = scored[scored["market_mid_now"].notna()]
    if scored.empty:
        log.info("model_accuracy: 0 predictions could be re-matched to current chains")
        return None
    mae = _compute_mae_by_regime(scored)
    if not mae:
        log.info("model_accuracy: insufficient overlap to compute MAE per regime")
        return None
    weights = _mae_to_weights(mae)
    if not weights:
        return None
    log.info(
        "model_accuracy: computed diagnostic-only weights for regimes=%s; "
        "production artifacts were not modified",
        list(weights.keys()),
    )
    for regime, w in weights.items():
        log.info("  %s: %s", regime, {k: round(v, 3) for k, v in w.items()})
    return weights


WEIGHT_HISTORY_FILE = DATA_DIR / "model_weights_history.jsonl"


def load_weight_history(limit: int = 200) -> list[dict]:
    """Read the rolling weight history. Returns a list of {ts, weights, mae}."""
    if not WEIGHT_HISTORY_FILE.exists():
        return []
    try:
        rows = [
            json.loads(line)
            for line in WEIGHT_HISTORY_FILE.read_text().splitlines()
            if line.strip()
        ]
        return rows[-limit:]
    except Exception:
        return []


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    refit_weights()
