# Purpose: Evidence-gated return prediction and adaptive fusion weights.
"""Fail-closed return prediction and adaptive fusion-weight research.

Normal scans are inference-only. Research fits remain untrusted shadows, and
legacy artifacts load as zero predictor coefficients or source-controlled
weights. Only an explicitly promoted, digest-valid champion with asset-isolated
purged out-of-sample evidence can affect ranking.
"""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("optedge.predictor")
COEFS_PATH = ROOT / "data" / "predictor_coefs.json"
LAST_IC_PATH = ROOT / "data" / "last_ic.parquet"
RUNTIME_CONFIG_PATH = ROOT / "config_runtime.py"
DATA_DIR = ROOT / "data"
CONFIG_PATH = ROOT / "config.py"

MIN_ADAPTIVE_SAMPLES = 500
MIN_ADAPTIVE_DAYS = 10
RUNTIME_WEIGHT_MAX_AGE_DAYS = 14
MAX_RUNTIME_FACTOR_WEIGHT = 0.30
MIN_RUNTIME_FACTOR_COVERAGE = 1.00
ADAPTIVE_WEIGHT_BLEND = 0.25

PREDICTOR_CHAMPION_SCHEMA = "optedge_predictor_champion_v1"
PREDICTOR_SHADOW_SCHEMA = "optedge_predictor_shadow_v1"
RUNTIME_WEIGHT_CHAMPION_SCHEMA = "optedge_runtime_weight_champion_v2"
RUNTIME_WEIGHT_SHADOW_SCHEMA = "optedge_runtime_weight_shadow_v1"
MODEL_TRUST_SCHEMA = "optedge_model_trust_v1"
TRUSTED_CHAMPION = "trusted_champion"
SHADOW_UNTRUSTED = "shadow_untrusted"
PREDICTOR_MODEL_KIND = "stock_return"
PREDICTOR_TARGET_BASIS = "fixed_horizon_after_cost_return"
PREDICTOR_SHADOW_TARGET_BASIS = "research_lifecycle_after_cost_return"
RUNTIME_TARGET_BASIS = "asset_isolated_fixed_horizon_after_cost_returns"
OOS_METHOD = "purged_expanding_window"
MIN_PROMOTION_FOLDS = 3
MIN_PROMOTION_EFFECTIVE_BLOCKS = 30
MIN_PROMOTION_ENTRY_DAYS = 30


def cache_ic(ic_df: pd.DataFrame):
    """Persist the latest historical IC analysis for diagnostics."""
    if ic_df is None or ic_df.empty:
        return
    try:
        LAST_IC_PATH.parent.mkdir(exist_ok=True)
        ic_df.to_parquet(LAST_IC_PATH, index=False)
    except Exception as e:
        log.debug("failed to cache IC: %s", e)


def load_cached_ic() -> pd.DataFrame | None:
    if not LAST_IC_PATH.exists():
        return None
    try:
        return pd.read_parquet(LAST_IC_PATH)
    except Exception:
        return None


def _load_json_rows(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return pd.DataFrame()
    return pd.DataFrame(rows if isinstance(rows, list) else [])


def load_adaptive_outcomes(data_dir: Path | None = None) -> pd.DataFrame:
    """Load independent, completed trade episodes for model adaptation.

    The forward-test stream contains a new row for every scan and is useful for
    monitoring, but repeated snapshots of the same thesis are not independent
    training observations. Adaptive weights therefore learn from lifecycle
    closures after the same churn filter used by exit-policy validation.
    """
    root = Path(data_dir) if data_dir is not None else DATA_DIR
    frames = []
    for asset, filename in (
        ("option", "closed_positions.json"),
        ("share", "closed_share_positions.json"),
        ("futures", "closed_futures_positions.json"),
    ):
        frame = _load_json_rows(root / filename)
        if frame.empty:
            continue
        frame["asset"] = asset
        frame["_had_after_slippage"] = (
            frame["pnl_pct_after_slippage"].notna()
            if "pnl_pct_after_slippage" in frame.columns
            else False
        )
        frames.append(frame)
    if not frames:
        return pd.DataFrame()

    closed = pd.concat(frames, ignore_index=True, sort=False)
    try:
        from backtest.exit_learning import eligible_closed_for_learning

        eligible = [eligible_closed_for_learning(asset, closed)
                    for asset in ("option", "share", "futures")]
        eligible = [frame for frame in eligible if not frame.empty]
        closed = (pd.concat(eligible, ignore_index=True, sort=False)
                  if eligible else pd.DataFrame())
    except Exception as exc:
        log.warning("adaptive outcomes unavailable: lifecycle filter failed: %s", exc)
        return pd.DataFrame()
    if closed.empty or "pnl_pct" not in closed.columns:
        return pd.DataFrame()

    closed["pnl_pct"] = pd.to_numeric(closed["pnl_pct"], errors="coerce")
    if "pnl_pct_after_slippage" not in closed.columns:
        closed["pnl_pct_after_slippage"] = closed["pnl_pct"]
    else:
        closed["pnl_pct_after_slippage"] = pd.to_numeric(
            closed["pnl_pct_after_slippage"], errors="coerce"
        )
        closed["pnl_pct_after_slippage"] = closed["pnl_pct_after_slippage"].where(
            closed["pnl_pct_after_slippage"].notna(), closed["pnl_pct"]
        )

    # Older option closures predate the explicit after-slippage field.
    try:
        from config import FILL_SLIPPAGE_PCT

        slippage = float(FILL_SLIPPAGE_PCT)
    except Exception:
        slippage = 0.04
    option_mask = closed["asset"].astype(str).eq("option")
    missing_option_slippage = option_mask & ~closed["_had_after_slippage"].fillna(False).astype(bool)
    closed.loc[missing_option_slippage, "pnl_pct_after_slippage"] = (
        closed.loc[missing_option_slippage, "pnl_pct"] - slippage
    )

    # Futures store equivalent context factors under z_context_* names.
    context_map = {
        "z_context_sentiment": "z_sent",
        "z_context_fund": "z_fund",
        "z_context_insider": "z_insider",
        "z_context_news": "z_news",
        "z_context_earnings": "z_earnings",
        "z_context_value": "z_value",
        "z_context_congress": "z_congress",
        "z_context_social": "z_social",
        "z_context_analyst": "z_analyst",
        "z_context_sector_rs": "z_sector_rs",
        "z_context_dark_pool": "z_dark_pool",
        "z_context_fda": "z_fda",
        "z_context_sector_flow": "z_sector_flow",
        "z_context_tech": "z_tech",
        "z_context_short_int": "z_short_int",
        "z_context_cot": "z_cot",
        "z_context_thirteen_f": "z_thirteen_f",
        "z_context_vix_term": "z_vix_term",
        "z_context_eia": "z_eia",
        "z_context_wasde": "z_wasde",
        "z_context_buyback": "z_buybacks",
        "z_context_gtrends": "z_gtrends",
        "z_context_form_144": "z_form_144",
        "z_context_whisper": "z_whisper",
        "z_context_hyperliquid": "z_hyperliquid",
        "z_context_twitter": "z_twitter",
        "z_context_r_options": "z_r_options",
        "z_context_curve": "z_yield_curve",
        "z_context_credit": "z_credit_spread",
        "z_context_cluster_buys": "z_cluster_buys",
    }
    for source, target in context_map.items():
        if source not in closed.columns:
            continue
        if target not in closed.columns:
            closed[target] = closed[source]
        else:
            closed[target] = closed[target].where(closed[target].notna(), closed[source])
    return closed.drop(columns=["_had_after_slippage"], errors="ignore").dropna(subset=["pnl_pct"])

# Default horizon for option-buying predictions (matches typical 14-30 DTE picks).
DEFAULT_HORIZON_DAYS = 14

# Keep this mapping aligned with config.SIGNAL_WEIGHTS and fusion.rank. Runtime
# learning may redistribute these factors, but it must not silently erase a
# factor merely because it was added after the first predictor implementation.
SIGNAL_TO_ZCOL = {
    "mispricing": "z_mispricing",
    "iv_rank": "z_iv_rank",
    "skew": "z_skew",
    "sentiment_d": "z_sent",
    "fundamentals": "z_fund",
    "insider": "z_insider",
    "macro": "z_macro",
    "news": "z_news",
    "earnings": "z_earnings",
    "value": "z_value",
    "congress": "z_congress",
    "social": "z_social",
    "analyst": "z_analyst",
    "uoa": "z_uoa",
    "sector_rs": "z_sector_rs",
    "dark_pool": "z_dark_pool",
    "fda": "z_fda",
    "sector_flow": "z_sector_flow",
    "technicals": "z_tech",
    "short_int": "z_short_int",
    "put_call": "z_put_call",
    "iv_surface": "z_iv_surface",
    "cot": "z_cot",
    "thirteen_f": "z_thirteen_f",
    "vix_term": "z_vix_term",
    "eia": "z_eia",
    "wasde": "z_wasde",
    "buybacks": "z_buybacks",
    "gtrends": "z_gtrends",
    "form_144": "z_form_144",
    "whisper": "z_whisper",
    "hyperliquid": "z_hyperliquid",
    "twitter": "z_twitter",
    "r_options": "z_r_options",
    "cluster_buys": "z_cluster_buys",
    "yield_curve": "z_yield_curve",
    "credit_spread": "z_credit_spread",
}

# Z-score columns we predict from.
Z_COLS = list(SIGNAL_TO_ZCOL.values())

# Map factor name (used in IC) to its z-column in fusion output
FACTOR_TO_ZCOL = {
    "value_score":     "z_value",
    "fund_score":      "z_fund",
    "sentiment_delta": "z_sent",
    "insider_score":   "z_insider",
}

ZCOL_TO_SIGNAL = {z_col: signal for signal, z_col in SIGNAL_TO_ZCOL.items()}

_ASSET_ALIASES = {
    "share": "share",
    "shares": "share",
    "stock": "share",
    "stocks": "share",
    "equity": "share",
    "equities": "share",
    "option": "option",
    "options": "option",
    "future": "futures",
    "futures": "futures",
}


def _zero_coefs() -> dict[str, float]:
    return {column: 0.0 for column in Z_COLS}


def _normalize_asset(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    return _ASSET_ALIASES.get(str(value).strip().lower())


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _predictor_content_digest(payload: dict[str, Any]) -> str:
    content = dict(payload)
    content.pop("content_digest_sha256", None)
    return _canonical_sha256(content)


def _runtime_content_digest(weights: dict[str, float], meta: dict[str, Any]) -> str:
    digest_meta = dict(meta)
    digest_meta.pop("content_digest_sha256", None)
    return _canonical_sha256({"meta": digest_meta, "weights": weights})


def _current_policy_digest() -> str | None:
    try:
        from backtest.fixed_horizon import evidence_policy_digest

        digest = evidence_policy_digest()
    except Exception:
        return None
    return str(digest) if _is_sha256(digest) else None


def _utc_timestamp(value: Any) -> datetime | None:
    try:
        timestamp = pd.to_datetime(value, errors="coerce", utc=True)
    except Exception:
        return None
    if pd.isna(timestamp):
        return None
    return timestamp.to_pydatetime()


def _finite_number(value: Any) -> float | None:
    if isinstance(value, (bool, np.bool_)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _whole_number(value: Any) -> int | None:
    number = _finite_number(value)
    if number is None or number < 0 or number != int(number):
        return None
    return int(number)


def _training_asset(frame: pd.DataFrame | None) -> tuple[str | None, str | None]:
    """Require one explicit asset family before fitting any return model."""
    if frame is None or frame.empty:
        return None, "missing_training_rows"
    if "asset" not in frame.columns:
        return None, "missing_asset_identity"
    normalized = frame["asset"].map(_normalize_asset)
    if normalized.isna().any():
        return None, "unsupported_or_missing_asset_identity"
    assets = sorted(set(normalized.astype(str)))
    if len(assets) != 1:
        return None, "mixed_asset_training_rejected"
    asset = assets[0]
    if asset == "option":
        return None, "option_adaptation_requires_direct_broker_observed_target"
    if asset != "share":
        return None, "stock_predictor_accepts_share_targets_only"
    return asset, None


def predictor_artifact_status(
    path: Path | None = None,
    *,
    requested_asset: str = "share",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate a promoted predictor artifact before it may affect ranking."""
    source_path = Path(path) if path is not None else COEFS_PATH
    checked_at = now or datetime.now(UTC)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    else:
        checked_at = checked_at.astimezone(UTC)
    wanted_asset = _normalize_asset(requested_asset)
    status: dict[str, Any] = {
        "path": str(source_path),
        "exists": source_path.exists(),
        "usable": False,
        "schema": None,
        "trust_state": None,
        "asset": None,
        "requested_asset": wanted_asset,
        "horizon_sessions": None,
        "target_basis": None,
        "content_digest_sha256": None,
        "age_days": None,
        "outcome_age_days": None,
        "coefs": None,
        "oos": None,
        "reasons": [],
    }
    if wanted_asset is None:
        status["reasons"].append("unsupported requested asset")
    if not source_path.exists():
        status["reasons"].append("predictor champion not found")
        return status
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except Exception as exc:
        status["reasons"].append(f"predictor artifact is unreadable: {type(exc).__name__}")
        return status
    if not isinstance(payload, dict):
        status["reasons"].append("predictor artifact must be an object")
        return status

    schema = str(payload.get("schema") or "")
    trust_state = str(payload.get("trust_state") or "")
    asset = _normalize_asset(payload.get("asset"))
    horizon = _whole_number(payload.get("horizon_sessions"))
    target_basis = str(payload.get("target_basis") or "")
    status.update(
        {
            "schema": schema or None,
            "trust_state": trust_state or None,
            "asset": asset,
            "horizon_sessions": horizon,
            "target_basis": target_basis or None,
            "oos": payload.get("oos") if isinstance(payload.get("oos"), dict) else None,
            "content_digest_sha256": payload.get("content_digest_sha256"),
        }
    )
    if schema != PREDICTOR_CHAMPION_SCHEMA:
        status["reasons"].append("predictor schema is not the trusted champion schema")
    if trust_state != TRUSTED_CHAMPION:
        status["reasons"].append("predictor artifact is not explicitly promoted")
    if str(payload.get("model_kind") or "") != PREDICTOR_MODEL_KIND:
        status["reasons"].append("predictor model kind is not stock_return")
    if asset != "share":
        status["reasons"].append(
            "only a share-specific champion is supported; option adaptation remains disabled"
        )
    if wanted_asset is not None and asset != wanted_asset:
        status["reasons"].append("predictor champion asset does not match the requested asset")
    if horizon is None or horizon <= 0:
        status["reasons"].append("predictor horizon_sessions must be a positive whole number")
    if target_basis != PREDICTOR_TARGET_BASIS:
        status["reasons"].append("predictor target basis is not fixed-horizon after-cost return")

    coefs = payload.get("coefs")
    parsed_coefs: dict[str, float] = {}
    if not isinstance(coefs, dict):
        status["reasons"].append("predictor coefficients are missing")
    else:
        try:
            parsed_coefs = {str(key): float(value) for key, value in coefs.items()}
        except (TypeError, ValueError):
            status["reasons"].append("predictor coefficients contain non-numeric values")
            parsed_coefs = {}
    if parsed_coefs:
        if set(parsed_coefs) != set(Z_COLS):
            status["reasons"].append("predictor coefficients do not cover the exact factor set")
        if any(not np.isfinite(value) or abs(value) > 0.05 for value in parsed_coefs.values()):
            status["reasons"].append("predictor coefficients are non-finite or exceed the hard cap")
        status["coefs"] = parsed_coefs

    oos = payload.get("oos")
    if not isinstance(oos, dict):
        status["reasons"].append("predictor OOS promotion evidence is missing")
    else:
        if str(oos.get("method") or "") != OOS_METHOD:
            status["reasons"].append("predictor OOS method is not purged expanding-window")
        if oos.get("passed") is not True:
            status["reasons"].append("predictor OOS promotion did not pass")
        folds = _whole_number(oos.get("folds"))
        entry_days = _whole_number(oos.get("unique_entry_days"))
        effective_blocks = _whole_number(oos.get("effective_horizon_blocks"))
        predictions = _whole_number(oos.get("n_predictions"))
        purge_sessions = _whole_number(oos.get("purge_sessions"))
        if folds is None or folds < MIN_PROMOTION_FOLDS:
            status["reasons"].append(
                f"predictor OOS evidence needs at least {MIN_PROMOTION_FOLDS} folds"
            )
        if entry_days is None or entry_days < MIN_PROMOTION_ENTRY_DAYS:
            status["reasons"].append(
                f"predictor OOS evidence needs at least {MIN_PROMOTION_ENTRY_DAYS} entry days"
            )
        if effective_blocks is None or effective_blocks < MIN_PROMOTION_EFFECTIVE_BLOCKS:
            status["reasons"].append(
                "predictor OOS evidence lacks 30 effective horizon-length blocks"
            )
        if predictions is None or predictions < max(MIN_ADAPTIVE_SAMPLES, entry_days or 0):
            status["reasons"].append("predictor OOS prediction count is insufficient")
        if horizon is None or purge_sessions is None or purge_sessions < horizon:
            status["reasons"].append("predictor OOS purge is shorter than the holding horizon")
        for key in (
            "after_cost_mean",
            "champion_delta_ci_low",
            "cost_stress_2x_mean",
            "recent_half_mean",
        ):
            value = _finite_number(oos.get(key))
            if value is None or value <= 0:
                status["reasons"].append(f"predictor OOS {key} must be positive")

    source = payload.get("source")
    if not isinstance(source, dict):
        status["reasons"].append("predictor source evidence metadata is missing")
    else:
        for key in ("outcome_digest_sha256", "policy_digest_sha256"):
            if not _is_sha256(source.get(key)):
                status["reasons"].append(f"predictor source {key} is invalid")
        expected_policy = _current_policy_digest()
        if (
            expected_policy is None
            or str(source.get("policy_digest_sha256") or "") != expected_policy
        ):
            status["reasons"].append("predictor source policy digest is not current")
        latest_outcome = _utc_timestamp(source.get("latest_outcome_at"))
        if latest_outcome is None:
            status["reasons"].append("predictor latest_outcome_at is invalid")
        else:
            outcome_age = (checked_at - latest_outcome).total_seconds() / 86400.0
            status["outcome_age_days"] = outcome_age
            if outcome_age > RUNTIME_WEIGHT_MAX_AGE_DAYS:
                status["reasons"].append("predictor source outcomes are stale")
            elif outcome_age < -1.0:
                status["reasons"].append("predictor source outcomes are future-dated")

    promoted_at = _utc_timestamp(payload.get("promoted_at"))
    if promoted_at is None:
        status["reasons"].append("predictor promoted_at is invalid")
    else:
        age = (checked_at - promoted_at).total_seconds() / 86400.0
        status["age_days"] = age
        if age > RUNTIME_WEIGHT_MAX_AGE_DAYS:
            status["reasons"].append("predictor champion is stale")
        elif age < -1.0:
            status["reasons"].append("predictor promoted_at is future-dated")

    digest = str(payload.get("content_digest_sha256") or "")
    try:
        expected_digest = _predictor_content_digest(payload)
    except (TypeError, ValueError):
        expected_digest = None
    if not _is_sha256(digest) or expected_digest is None or digest != expected_digest:
        status["reasons"].append("predictor content digest does not match")
    status["usable"] = not status["reasons"]
    return status


def _bootstrap_coefs_from_ic(ic_df: pd.DataFrame, horizon: int = DEFAULT_HORIZON_DAYS) -> dict[str, float]:
    """Seed coefficients from explicitly qualified lifecycle IC evidence.

    Logic: a Q5-Q1 spread of S% spans roughly 4 z-units (top 20% mean ≈ +1.3z,
    bottom 20% mean ≈ -1.3z). So per-z-unit return ≈ S / 2.6.
    """
    if ic_df is None or ic_df.empty:
        return {c: 0.0 for c in Z_COLS}

    # Pick the closest horizon row available
    available = sorted(ic_df["horizon_days"].unique())
    target_h = min(available, key=lambda h: abs(h - horizon)) if available else horizon

    coefs: dict[str, float] = {c: 0.0 for c in Z_COLS}
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
    now = datetime.now(UTC)
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


def _forward_history_stats(df: pd.DataFrame | None,
                           target_col: str = "pnl_pct") -> dict[str, int]:
    if df is None or df.empty or target_col not in df.columns:
        return {"samples": 0, "unique_days": 0}
    target = pd.to_numeric(df[target_col], errors="coerce")
    valid = target.notna()
    samples = int(valid.sum())
    if "entry_time" not in df.columns:
        return {"samples": samples, "unique_days": 0}
    entry_time = pd.to_datetime(df.loc[valid, "entry_time"], errors="coerce", utc=True)
    return {
        "samples": samples,
        "unique_days": int(entry_time.dt.date.nunique()),
    }


def _latest_outcome_time(df: pd.DataFrame | None) -> datetime | None:
    if df is None or df.empty:
        return None
    for column in ("exit_time", "entry_time"):
        if column not in df.columns:
            continue
        values = pd.to_datetime(df[column], errors="coerce", utc=True).dropna()
        if not values.empty:
            return values.max().to_pydatetime()
    return None


def _ic_frame_is_reliable(ic_df: pd.DataFrame | None,
                          min_samples: int = MIN_ADAPTIVE_SAMPLES,
                          min_unique_days: int = MIN_ADAPTIVE_DAYS) -> bool:
    """Only accept explicitly labeled, out-of-sample lifecycle IC evidence."""
    if ic_df is None or ic_df.empty:
        return False
    required = {"n", "trading_days", "is_reliable", "basis", "latest_outcome_at"}
    if not required.issubset(ic_df.columns):
        return False
    basis = ic_df["basis"].fillna("").astype(str)
    if not basis.eq("independent_swing_outcomes").all():
        return False
    samples = pd.to_numeric(ic_df["n"], errors="coerce")
    days = pd.to_numeric(ic_df["trading_days"], errors="coerce")
    reliable = ic_df["is_reliable"].fillna(False).astype(bool)
    outcome_times = pd.to_datetime(ic_df["latest_outcome_at"], errors="coerce", utc=True)
    return bool(
        samples.notna().all()
        and days.notna().all()
        and (samples >= min_samples).all()
        and (days >= min_unique_days).all()
        and reliable.all()
        and outcome_times.notna().all()
    )


def _balanced_time_weights(df: pd.DataFrame) -> np.ndarray:
    """Time decay with equal aggregate influence for each entry day."""
    if "entry_time" not in df.columns:
        return np.ones(len(df), dtype=float)
    entry_time = pd.to_datetime(df["entry_time"], errors="coerce", utc=True)
    time_weights = _time_decay_weights(entry_time, half_life_days=30.0)
    day_key = entry_time.dt.date
    day_counts = day_key.map(day_key.value_counts()).fillna(1).astype(float).to_numpy()
    weights = time_weights / np.maximum(day_counts, 1.0)
    total = float(weights.sum())
    return weights * (len(weights) / total) if total > 0 else np.ones(len(df), dtype=float)


def _fit_from_forward(forward_signals: pd.DataFrame,
                       regime: str | None = None) -> tuple[dict[str, float], dict[str, Any]]:
    """Build a research-only share-return challenger from realized P&L.

    `regime` filters the data to only signals from the same regime (risk_on/risk_off/neutral)
    if a `regime` column is available in the signals dataframe.
    """
    asset, asset_error = _training_asset(forward_signals)
    if asset_error:
        return {}, {"reason": asset_error, "asset": asset}
    df = forward_signals.copy()
    target_col = "pnl_pct_after_slippage"
    if target_col not in df.columns:
        return {}, {"reason": "missing_after_cost_target", "missing": [target_col]}
    # Optional per-regime filter
    if regime and "regime" in df.columns:
        df = df[df["regime"] == regime]
    df = df.dropna(subset=[target_col])
    history = _forward_history_stats(df, target_col)
    if (history["samples"] < MIN_ADAPTIVE_SAMPLES
            or history["unique_days"] < MIN_ADAPTIVE_DAYS):
        return {}, {
            "reason": "insufficient_walk_forward_history",
            "n": history["samples"],
            "unique_days": history["unique_days"],
            "required_samples": MIN_ADAPTIVE_SAMPLES,
            "required_days": MIN_ADAPTIVE_DAYS,
        }

    missing_features = [c for c in Z_COLS if c not in df.columns]
    for c in missing_features:
        df[c] = 0.0

    X = df[Z_COLS].fillna(0.0).values
    y = df[target_col].values
    # Clip extreme outcomes (-100% blowups distort the fit even with Huber)
    y = np.clip(y, -1.0, 2.0)
    # Time-decay weights: recent signals matter more
    sw = _balanced_time_weights(df)

    coefs: dict[str, float] = {}
    meta: dict[str, Any] = {
        "n": len(df),
        "unique_days": history["unique_days"],
        "asset": asset,
        "regime": regime or "all",
        "target": target_col,
        "filled_missing_features": missing_features,
    }

    # Try Huber first (robust to outliers), fall back to LassoCV
    try:
        from sklearn.linear_model import HuberRegressor
        model = HuberRegressor(epsilon=1.35, max_iter=200).fit(X, y, sample_weight=sw)
        coefs = dict(zip(Z_COLS, model.coef_.astype(float), strict=True))
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
        coefs = dict(zip(Z_COLS, model.coef_.astype(float), strict=True))
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


def fit_return_predictor(
    forward_signals: pd.DataFrame = None,
    ic_df: pd.DataFrame = None,
    horizon: int = DEFAULT_HORIZON_DAYS,
    *,
    asset: str | None = None,
) -> dict[str, Any]:
    """Build an untrusted research challenger without changing active models.

    Ordinary scans must never fit and consume a model in the same run.  This
    function is therefore pure with respect to model artifacts: it returns a
    shadow payload and never writes ``predictor_coefs.json``.
    """
    coefs: dict[str, float] = {}
    requested_asset = _normalize_asset(asset) if asset is not None else None
    requested_asset_error = asset is not None and requested_asset is None
    inferred_asset: str | None = None
    meta: dict[str, Any] = {
        "horizon": horizon,
        "fitted_at": datetime.now(UTC).isoformat(),
        "activation_eligible": False,
        "persistence": "disabled",
    }

    if forward_signals is not None and not forward_signals.empty:
        inferred_asset, asset_error = _training_asset(forward_signals)
        if requested_asset_error:
            meta.update({"reason": "unsupported_requested_asset", "source": "zero_init"})
        elif asset_error:
            meta.update({"reason": asset_error, "source": "zero_init"})
        elif requested_asset is not None and requested_asset != inferred_asset:
            meta.update({"reason": "requested_asset_mismatch", "source": "zero_init"})
        else:
            coefs, fwd_meta = _fit_from_forward(forward_signals)
            meta.update(fwd_meta)

    effective_asset = requested_asset or inferred_asset
    if (
        not coefs
        and effective_asset == "share"
        and forward_signals is None
        and _ic_frame_is_reliable(ic_df)
    ):
        coefs = _bootstrap_coefs_from_ic(ic_df, horizon=horizon)
        meta["source"] = "ic_bootstrap"
    elif coefs:
        meta["source"] = "shadow_forward_refit"
    else:
        coefs = _zero_coefs()
        meta.setdefault("source", "zero_init")
        if ic_df is not None and not ic_df.empty:
            meta["ic_bootstrap_skipped"] = "not_independent_or_insufficient_history"

    # Clamp absurd values: any single coef >|0.05| gets capped (5% / z-unit)
    for k in list(coefs.keys()):
        coefs[k] = max(-0.05, min(0.05, float(coefs[k])))

    payload = {
        "schema": PREDICTOR_SHADOW_SCHEMA,
        "trust_state": SHADOW_UNTRUSTED,
        "model_kind": PREDICTOR_MODEL_KIND,
        "asset": effective_asset,
        "horizon_sessions": int(horizon),
        "target_basis": PREDICTOR_SHADOW_TARGET_BASIS,
        "coefs": coefs,
        "meta": meta,
    }
    payload["content_digest_sha256"] = _predictor_content_digest(payload)
    return payload


def load_predictor_coefs(
    asset: str = "share",
    path: Path | None = None,
) -> dict[str, float]:
    """Load only a digest-valid explicitly promoted asset champion."""
    status = predictor_artifact_status(path, requested_asset=asset)
    if status["usable"] and isinstance(status.get("coefs"), dict):
        return {column: float(status["coefs"][column]) for column in Z_COLS}
    return _zero_coefs()


def predict_returns(
    ranked: pd.DataFrame,
    coefs: dict[str, float] = None,
    *,
    asset: str = "share",
) -> pd.Series:
    """Apply coefficients to z-scores, returning predicted % return per row."""
    if coefs is None:
        coefs = load_predictor_coefs(asset=asset)
    if ranked is None or ranked.empty:
        return pd.Series(dtype=float)
    used_cols = [c for c in Z_COLS if c in ranked.columns]
    if not used_cols:
        return pd.Series(0.0, index=ranked.index)
    M = ranked[used_cols].fillna(0).values
    w = np.array([coefs.get(c, 0.0) for c in used_cols])
    return pd.Series(M @ w, index=ranked.index)


def add_predictions_to_options(ranked: pd.DataFrame, coefs: dict[str, float] = None) -> pd.DataFrame:
    """Attach pred_stock_return_pct and pred_option_return_pct columns."""
    if ranked is None or ranked.empty:
        return ranked
    df = ranked.copy()
    df["pred_stock_return_pct"] = predict_returns(df, coefs, asset="option")
    # Option leverage ≈ 1 / |delta|, capped to keep things sane
    deltas = df.get("delta", pd.Series(0.5, index=df.index)).abs().clip(0.10, 0.95)
    leverage = 1.0 / deltas
    # Side-aligned: calls profit on +stock_return; puts profit on -stock_return.
    side_mult = np.where(df["side"] == "call", 1.0, -1.0)
    df["pred_option_return_pct"] = df["pred_stock_return_pct"] * leverage * side_mult
    # Cap option predictions at ±200% (per-trade)
    df["pred_option_return_pct"] = df["pred_option_return_pct"].clip(-2.0, 2.0)
    return df


def add_predictions_to_shares(ranked: pd.DataFrame, coefs: dict[str, float] = None) -> pd.DataFrame:
    """Attach pred_stock_return_pct to shares output."""
    if ranked is None or ranked.empty:
        return ranked
    df = ranked.copy()
    df["pred_stock_return_pct"] = predict_returns(df, coefs, asset="share")
    return df


# -------- Auto-retrain SIGNAL_WEIGHTS --------------------------------
def _has_enough_history_for_lasso(forward_signals: pd.DataFrame,
                                    min_samples: int = MIN_ADAPTIVE_SAMPLES,
                                    min_unique_days: int = MIN_ADAPTIVE_DAYS) -> bool:
    """v20.7 — walk-forward validation guard.

    A Lasso refit on a small / single-day sample overfits the one weird day
    and writes garbage into config_runtime.py for every subsequent run.
    Refuse to refit until we have BOTH:
      - ≥ min_samples (default 500) logged signals with realized P&L, AND
      - ≥ min_unique_days (default 10) distinct trading days represented.
    Until then we keep the source-controlled configured priors.
    """
    target_col = "pnl_pct_after_slippage"
    history = _forward_history_stats(forward_signals, target_col)
    return history["samples"] >= min_samples and history["unique_days"] >= min_unique_days


def _rolling_forward_ic_weights(forward_signals: pd.DataFrame,
                                baseline: dict[str, float],
                                lookback_days: int = 90,
                                min_samples: int = MIN_ADAPTIVE_SAMPLES,
                                min_unique_days: int = MIN_ADAPTIVE_DAYS) -> dict[str, float] | None:
    """Reweight factors from rolling forward IC before trusting full Lasso."""
    if forward_signals is None or forward_signals.empty:
        return None
    target_col = "pnl_pct_after_slippage"
    if target_col not in forward_signals.columns or "entry_time" not in forward_signals.columns:
        return None
    df = forward_signals.copy()
    df["entry_time"] = pd.to_datetime(df["entry_time"], errors="coerce", utc=True)
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=lookback_days)
    df = df[df["entry_time"] >= cutoff].dropna(subset=[target_col])
    if len(df) < min_samples or df["entry_time"].dt.date.nunique() < min_unique_days:
        return None

    ic_by_signal: dict[str, float] = {}
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
            raw[key] = max(float(base), 0.001)
        elif ic <= -0.05:
            raw[key] = max(float(base) * 0.20, 0.001)
        elif ic < 0:
            raw[key] = max(float(base) * 0.50, 0.001)
        else:
            raw[key] = max(float(base), 0.001) * (1.0 + min(ic, 0.25) * 6.0)
    weights = _normalize_and_cap_weights(raw)
    if not weights:
        return None
    log.info("rolling %dd IC weights from %d forward samples; strongest=%s",
             lookback_days, len(df), max(weights, key=weights.get))
    return weights


def _literal_assignments(path: Path, names: set[str]) -> dict[str, Any]:
    """Read selected literal assignments from a Python file without executing it."""
    if not path.exists():
        return {}
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id in names:
            values[target.id] = ast.literal_eval(node.value)
    return values


def _configured_signal_weights() -> dict[str, float]:
    """Return source-controlled priors, even after config is patched at runtime."""
    try:
        raw = _literal_assignments(CONFIG_PATH, {"SIGNAL_WEIGHTS"}).get("SIGNAL_WEIGHTS")
        if isinstance(raw, dict):
            parsed = {str(key): float(value) for key, value in raw.items()}
            if parsed:
                return parsed
    except Exception as exc:
        log.debug("failed to read configured signal priors: %s", exc)
    return {key: 1.0 for key in SIGNAL_TO_ZCOL}


def _normalize_and_cap_weights(weights: dict[str, float],
                               max_weight: float = MAX_RUNTIME_FACTOR_WEIGHT
                               ) -> dict[str, float]:
    """Normalize non-negative weights and redistribute mass above a hard cap."""
    clean: dict[str, float] = {}
    for key, value in weights.items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if np.isfinite(number) and number >= 0:
            clean[str(key)] = number
    total = sum(clean.values())
    if total <= 0:
        return {}
    normalized = {key: value / total for key, value in clean.items()}
    if len(normalized) * max_weight < 1.0 - 1e-12:
        return normalized

    result: dict[str, float] = {}
    active = list(normalized)
    remaining_mass = 1.0
    while active:
        active_total = sum(normalized[key] for key in active)
        if active_total <= 0:
            equal = remaining_mass / len(active)
            result.update({key: equal for key in active})
            break
        scaled = {
            key: remaining_mass * normalized[key] / active_total
            for key in active
        }
        over = [key for key, value in scaled.items() if value > max_weight + 1e-12]
        if not over:
            result.update(scaled)
            break
        for key in over:
            result[key] = max_weight
            active.remove(key)
            remaining_mass -= max_weight

    residual = 1.0 - sum(result.values())
    if residual > 1e-12:
        for key in sorted(result, key=result.get, reverse=True):
            capacity = max_weight - result[key]
            addition = min(residual, max(0.0, capacity))
            result[key] += addition
            residual -= addition
            if residual <= 1e-12:
                break
    return result


def _normalize_to_signal_weights(z_weights: dict[str, float],
                                 baseline: dict[str, float] | None = None
                                 ) -> dict[str, float]:
    """Shrink learned positive factor weights toward the configured priors."""
    priors = _normalize_and_cap_weights(baseline or _configured_signal_weights())
    if not priors:
        return {}

    learned: dict[str, float] = {}
    for z_col, value in z_weights.items():
        signal = ZCOL_TO_SIGNAL.get(z_col)
        if signal not in priors:
            continue
        try:
            learned[signal] = max(0.0, float(value))
        except (TypeError, ValueError):
            continue
    learned_total = sum(value for value in learned.values() if np.isfinite(value))
    if learned_total <= 0:
        return priors

    mapped = [key for key in priors if key in SIGNAL_TO_ZCOL]
    mapped_budget = sum(priors[key] for key in mapped)
    learned_distribution = {
        key: mapped_budget * learned.get(key, 0.0) / learned_total
        for key in mapped
    }
    blended = dict(priors)
    for key in mapped:
        blended[key] = (
            (1.0 - ADAPTIVE_WEIGHT_BLEND) * priors[key]
            + ADAPTIVE_WEIGHT_BLEND * learned_distribution[key]
        )
    return _normalize_and_cap_weights(blended)


def _walk_forward_splits(df: pd.DataFrame, max_splits: int = 5,
                         min_train_days: int = 5) -> list[tuple[np.ndarray, np.ndarray]]:
    entry_time = pd.to_datetime(df["entry_time"], errors="coerce", utc=True)
    days = entry_time.dt.date
    unique_days = sorted(day for day in days.dropna().unique())
    test_days = unique_days[min_train_days:]
    if len(test_days) > max_splits:
        test_days = test_days[-max_splits:]
    splits = []
    for test_day in test_days:
        train_idx = np.flatnonzero((days < test_day).fillna(False).to_numpy())
        test_idx = np.flatnonzero((days == test_day).fillna(False).to_numpy())
        if len(train_idx) and len(test_idx):
            splits.append((train_idx, test_idx))
    return splits


def update_runtime_weights(forward_signals: pd.DataFrame = None,
                           ic_df: pd.DataFrame = None,
                           min_samples: int = MIN_ADAPTIVE_SAMPLES
                           ) -> dict[str, float] | None:
    """Build a research-only weight challenger without persisting or activating it."""
    baseline = _normalize_and_cap_weights(_configured_signal_weights())

    if forward_signals is not None and not forward_signals.empty:
        _, asset_error = _training_asset(forward_signals)
        if asset_error:
            log.warning("adaptive-weight guard: %s; keeping configured priors", asset_error)
            return None
        df = forward_signals.copy()
        target_col = "pnl_pct_after_slippage"
        if target_col not in df.columns:
            log.warning("adaptive-weight guard: missing after-cost target")
            return None
        history = _forward_history_stats(df, target_col)
        if (history["samples"] < min_samples
                or history["unique_days"] < MIN_ADAPTIVE_DAYS):
            log.warning(
                "adaptive-weight guard: %d independent outcomes across %d entry days; "
                "need %d across %d days, keeping configured priors",
                history["samples"], history["unique_days"],
                min_samples, MIN_ADAPTIVE_DAYS,
            )
            return None

        df[target_col] = pd.to_numeric(df[target_col], errors="coerce")
        df = df.dropna(subset=[target_col, "entry_time"]).copy()
        df["entry_time"] = pd.to_datetime(df["entry_time"], errors="coerce", utc=True)
        df = df.dropna(subset=["entry_time"]).sort_values("entry_time").reset_index(drop=True)
        for col in Z_COLS:
            values = df[col] if col in df.columns else pd.Series(0.0, index=df.index)
            df[col] = pd.to_numeric(values, errors="coerce").fillna(0.0)

        try:
            from sklearn.linear_model import LassoCV

            splits = _walk_forward_splits(df)
            if not splits:
                raise ValueError("no chronological validation splits")
            x_values = df[Z_COLS].to_numpy(dtype=float)
            y_values = np.clip(df[target_col].to_numpy(dtype=float), -1.0, 2.0)
            model = LassoCV(
                cv=splits,
                max_iter=5000,
                positive=True,
                random_state=42,
            ).fit(x_values, y_values, sample_weight=_balanced_time_weights(df))
            positive_coef = np.maximum(model.coef_.astype(float), 0.0)
            if positive_coef.sum() > 0:
                z_to_weight = dict(
                    zip(Z_COLS, positive_coef / positive_coef.sum(), strict=True)
                )
                weight_map = _normalize_to_signal_weights(z_to_weight, baseline)
                log.info(
                    "shadow adaptive weights built from %d independent outcomes across %d days; "
                    "ordinary scans cannot persist or activate them",
                    history["samples"], history["unique_days"],
                )
                return weight_map
        except Exception as exc:
            log.warning("adaptive Lasso refit failed: %s", exc)

        rolling_weights = _rolling_forward_ic_weights(
            df,
            baseline,
            min_samples=min_samples,
            min_unique_days=MIN_ADAPTIVE_DAYS,
        )
        if rolling_weights:
            conservative = {
                key: (
                    (1.0 - ADAPTIVE_WEIGHT_BLEND) * baseline.get(key, 0.0)
                    + ADAPTIVE_WEIGHT_BLEND * rolling_weights.get(key, 0.0)
                )
                for key in baseline
            }
            conservative = _normalize_and_cap_weights(conservative)
            return conservative
        return None

    # Historical snapshot IC intentionally fails this gate: today's factors
    # correlated with already-realized returns are not walk-forward evidence.
    if not _ic_frame_is_reliable(ic_df, min_samples, MIN_ADAPTIVE_DAYS):
        if ic_df is not None and not ic_df.empty:
            log.warning("cached IC is not independent lifecycle evidence; keeping configured priors")
        return None

    adjusted = dict(baseline)
    ic_lookup: dict[str, float] = {}
    for _, row in ic_df.iterrows():
        signal = ZCOL_TO_SIGNAL.get(str(row.get("z_col") or ""))
        if signal in adjusted:
            ic_lookup[signal] = float(row["ic"])
    for signal, ic_value in ic_lookup.items():
        if ic_value > 0.10:
            multiplier = 1.50
        elif ic_value > 0.05:
            multiplier = 1.20
        elif ic_value < -0.10:
            multiplier = 0.25
        elif ic_value < -0.05:
            multiplier = 0.50
        else:
            multiplier = 1.0
        adjusted[signal] *= multiplier
    adjusted = _normalize_and_cap_weights(adjusted)
    blended = _normalize_and_cap_weights({
        key: (
            (1.0 - ADAPTIVE_WEIGHT_BLEND) * baseline.get(key, 0.0)
            + ADAPTIVE_WEIGHT_BLEND * adjusted.get(key, 0.0)
        )
        for key in baseline
    })
    sample_count = int(pd.to_numeric(ic_df["n"], errors="coerce").min())
    unique_days = int(pd.to_numeric(ic_df["trading_days"], errors="coerce").min())
    log.info(
        "shadow IC weight challenger built from %d outcomes across %d days; not persisted",
        sample_count,
        unique_days,
    )
    return blended


def _persist_runtime_weights(weights: dict[str, float], source: str = "auto",
                             sample_count: int = 0, unique_days: int = 0,
                             path: Path | None = None,
                             generated_at: datetime | None = None,
                             latest_outcome_at: datetime | None = None) -> None:
    """Persist a research shadow that can never be mistaken for a champion."""
    destination = Path(path) if path is not None else RUNTIME_CONFIG_PATH
    normalized = {
        key: float(f"{value:.10f}")
        for key, value in _normalize_and_cap_weights(weights).items()
    }
    timestamp = generated_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    outcome_timestamp = latest_outcome_at or timestamp
    if outcome_timestamp.tzinfo is None:
        outcome_timestamp = outcome_timestamp.replace(tzinfo=UTC)
    metadata = {
        "schema": RUNTIME_WEIGHT_SHADOW_SCHEMA,
        "trust_state": SHADOW_UNTRUSTED,
        "policy_version": 2,
        "source": str(source),
        "asset_scope": "share_research",
        "horizon_sessions": DEFAULT_HORIZON_DAYS,
        "target_basis": "research_lifecycle_after_cost_return",
        "generated_at": timestamp.astimezone(UTC).isoformat(),
        "latest_outcome_at": outcome_timestamp.astimezone(UTC).isoformat(),
        "sample_count": int(sample_count),
        "unique_days": int(unique_days),
        "factor_count": len(normalized),
        "max_factor_weight": max(normalized.values(), default=0.0),
        "adaptive_blend": ADAPTIVE_WEIGHT_BLEND,
        "oos": {"method": "not_evaluated", "passed": False},
    }
    metadata["content_digest_sha256"] = _runtime_content_digest(normalized, metadata)
    lines = [
        '"""Research-only adaptive fusion-weight shadow.',
        "",
        "Ordinary scans cannot load this file as a trusted champion.",
        '"""',
        "",
        f"RUNTIME_WEIGHT_META = {metadata!r}",
        "",
        "SIGNAL_WEIGHTS = {",
    ]
    for key, value in normalized.items():
        lines.append(f"    {key!r}: {value:.10f},")
    lines.extend(["}", ""])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines), encoding="utf-8")


def runtime_weight_status(
    path: Path | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate an explicitly promoted multi-asset runtime-weight champion."""
    source_path = Path(path) if path is not None else RUNTIME_CONFIG_PATH
    checked_at = now or datetime.now(UTC)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    else:
        checked_at = checked_at.astimezone(UTC)
    status: dict[str, Any] = {
        "path": str(source_path),
        "exists": source_path.exists(),
        "usable": False,
        "schema": None,
        "trust_state": None,
        "asset_scope": None,
        "horizon_sessions": None,
        "target_basis": None,
        "content_digest_sha256": None,
        "reasons": [],
        "weights": None,
        "meta": None,
        "age_days": None,
        "outcome_age_days": None,
        "factor_coverage": 0.0,
        "max_factor_weight": None,
    }
    if not source_path.exists():
        status["reasons"] = ["runtime file not found"]
        return status
    try:
        assignments = _literal_assignments(
            source_path, {"SIGNAL_WEIGHTS", "RUNTIME_WEIGHT_META"}
        )
    except Exception as exc:
        status["reasons"] = [f"malformed runtime file: {exc}"]
        return status

    weights = assignments.get("SIGNAL_WEIGHTS")
    meta = assignments.get("RUNTIME_WEIGHT_META")
    if not isinstance(weights, dict) or not weights:
        status["reasons"].append("missing SIGNAL_WEIGHTS")
        return status
    if not isinstance(meta, dict):
        status["reasons"].append("missing trust metadata")
        meta = {}

    schema = str(meta.get("schema") or "")
    trust_state = str(meta.get("trust_state") or "")
    asset_scope = str(meta.get("asset_scope") or "")
    horizon = _whole_number(meta.get("horizon_sessions"))
    target_basis = str(meta.get("target_basis") or "")
    status.update(
        {
            "schema": schema or None,
            "trust_state": trust_state or None,
            "asset_scope": asset_scope or None,
            "horizon_sessions": horizon,
            "target_basis": target_basis or None,
            "content_digest_sha256": meta.get("content_digest_sha256"),
        }
    )
    if schema != RUNTIME_WEIGHT_CHAMPION_SCHEMA:
        status["reasons"].append("runtime schema is not the trusted champion schema")
    if trust_state != TRUSTED_CHAMPION:
        status["reasons"].append("runtime weights are not explicitly promoted")
    if asset_scope != "multi_asset":
        status["reasons"].append("runtime champion must declare multi_asset scope")
    if horizon is None or horizon <= 0:
        status["reasons"].append("runtime horizon_sessions must be a positive whole number")
    if target_basis != RUNTIME_TARGET_BASIS:
        status["reasons"].append("runtime target basis is not asset-isolated fixed-horizon return")

    parsed: dict[str, float] = {}
    try:
        parsed = {str(key): float(value) for key, value in weights.items()}
    except (TypeError, ValueError):
        status["reasons"].append("weights contain non-numeric values")
    if parsed and any(not np.isfinite(value) or value < 0 for value in parsed.values()):
        status["reasons"].append("weights contain invalid values")
    total = sum(parsed.values()) if parsed else 0.0
    if not 0.99 <= total <= 1.01:
        status["reasons"].append("weights do not sum to one")

    priors = _configured_signal_weights()
    coverage = len(set(parsed) & set(priors)) / max(1, len(priors))
    max_factor = max(parsed.values(), default=0.0)
    status["factor_coverage"] = coverage
    status["max_factor_weight"] = max_factor
    if coverage < MIN_RUNTIME_FACTOR_COVERAGE:
        status["reasons"].append(
            f"factor coverage {coverage:.0%} is below {MIN_RUNTIME_FACTOR_COVERAGE:.0%}"
        )
    if max_factor > MAX_RUNTIME_FACTOR_WEIGHT + 1e-9:
        status["reasons"].append(
            f"factor concentration {max_factor:.0%} exceeds {MAX_RUNTIME_FACTOR_WEIGHT:.0%}"
        )
    factor_count = _whole_number(meta.get("factor_count"))
    declared_max = _finite_number(meta.get("max_factor_weight"))
    declared_blend = _finite_number(meta.get("adaptive_blend"))
    if factor_count != len(parsed):
        status["reasons"].append("runtime factor_count does not match the weight payload")
    if declared_max is None or not np.isclose(declared_max, max_factor, atol=1e-12):
        status["reasons"].append("runtime max_factor_weight does not match the weight payload")
    if declared_blend is None or not np.isclose(
        declared_blend,
        ADAPTIVE_WEIGHT_BLEND,
        atol=1e-12,
    ):
        status["reasons"].append("runtime adaptive_blend does not match the current policy")

    sample_count = _whole_number(meta.get("sample_count"))
    unique_days = _whole_number(meta.get("unique_days"))
    if sample_count is None or unique_days is None:
        sample_count = sample_count or 0
        unique_days = unique_days or 0
        status["reasons"].append("invalid evidence metadata")
    if sample_count < MIN_ADAPTIVE_SAMPLES:
        status["reasons"].append(
            f"only {sample_count} independent outcomes; need {MIN_ADAPTIVE_SAMPLES}"
        )
    if unique_days < MIN_PROMOTION_ENTRY_DAYS:
        status["reasons"].append(
            f"only {unique_days} entry days; need {MIN_PROMOTION_ENTRY_DAYS}"
        )

    generated_at = _utc_timestamp(meta.get("promoted_at") or meta.get("generated_at"))
    if generated_at is not None:
        age_days = (checked_at - generated_at).total_seconds() / 86400.0
        status["age_days"] = age_days
        if age_days > RUNTIME_WEIGHT_MAX_AGE_DAYS:
            status["reasons"].append(
                f"runtime weights are {age_days:.1f} days old; "
                f"max is {RUNTIME_WEIGHT_MAX_AGE_DAYS}"
            )
        elif age_days < -1.0:
            status["reasons"].append("runtime timestamp is in the future")
    else:
        status["reasons"].append("missing or invalid promoted_at")

    source = meta.get("source_evidence")
    if not isinstance(source, dict):
        status["reasons"].append("runtime source evidence metadata is missing")
        source = {}
    for key in ("outcome_digest_sha256", "policy_digest_sha256"):
        if not _is_sha256(source.get(key)):
            status["reasons"].append(f"runtime source {key} is invalid")
    expected_policy = _current_policy_digest()
    if (
        expected_policy is None
        or str(source.get("policy_digest_sha256") or "") != expected_policy
    ):
        status["reasons"].append("runtime source policy digest is not current")
    latest_outcome_at = _utc_timestamp(source.get("latest_outcome_at"))
    if latest_outcome_at is not None:
        outcome_age_days = (checked_at - latest_outcome_at).total_seconds() / 86400.0
        status["outcome_age_days"] = outcome_age_days
        if outcome_age_days > RUNTIME_WEIGHT_MAX_AGE_DAYS:
            status["reasons"].append(
                f"latest training outcome is {outcome_age_days:.1f} days old; "
                f"max is {RUNTIME_WEIGHT_MAX_AGE_DAYS}"
            )
        elif outcome_age_days < -1.0:
            status["reasons"].append("latest outcome timestamp is in the future")
    else:
        status["reasons"].append("missing or invalid latest_outcome_at")

    oos = meta.get("oos")
    if not isinstance(oos, dict):
        status["reasons"].append("runtime OOS promotion evidence is missing")
    else:
        if str(oos.get("method") or "") != OOS_METHOD:
            status["reasons"].append("runtime OOS method is not purged expanding-window")
        if oos.get("passed") is not True:
            status["reasons"].append("runtime OOS promotion did not pass")
        folds = _whole_number(oos.get("folds"))
        if folds is None or folds < MIN_PROMOTION_FOLDS:
            status["reasons"].append(
                f"runtime OOS evidence needs at least {MIN_PROMOTION_FOLDS} folds"
            )
        validated_assets = oos.get("validated_assets")
        if not isinstance(validated_assets, list):
            validated_assets = []
        validated = {_normalize_asset(value) for value in validated_assets}
        if not {"share", "option"}.issubset(validated):
            status["reasons"].append(
                "global runtime weights require separate share and option OOS validation"
            )
        if str(oos.get("options_target_basis") or "") != "broker_observed_option_return":
            status["reasons"].append(
                "option runtime adaptation requires direct broker-observed option targets"
            )
        purge_sessions = _whole_number(oos.get("purge_sessions"))
        if horizon is None or purge_sessions is None or purge_sessions < horizon:
            status["reasons"].append("runtime OOS purge is shorter than the holding horizon")
        effective = oos.get("effective_horizon_blocks_by_asset")
        delta = oos.get("champion_delta_ci_low_by_asset")
        stress = oos.get("cost_stress_2x_mean_by_asset")
        predictions = oos.get("n_predictions_by_asset")
        for asset in ("share", "option"):
            blocks = _whole_number(effective.get(asset)) if isinstance(effective, dict) else None
            delta_low = _finite_number(delta.get(asset)) if isinstance(delta, dict) else None
            stress_mean = _finite_number(stress.get(asset)) if isinstance(stress, dict) else None
            prediction_count = (
                _whole_number(predictions.get(asset)) if isinstance(predictions, dict) else None
            )
            if blocks is None or blocks < MIN_PROMOTION_EFFECTIVE_BLOCKS:
                status["reasons"].append(
                    f"runtime OOS {asset} evidence lacks 30 effective horizon blocks"
                )
            if delta_low is None or delta_low <= 0:
                status["reasons"].append(f"runtime OOS {asset} champion delta must be positive")
            if stress_mean is None or stress_mean <= 0:
                status["reasons"].append(f"runtime OOS {asset} 2x-cost mean must be positive")
            if prediction_count is None or prediction_count < MIN_ADAPTIVE_SAMPLES:
                status["reasons"].append(
                    f"runtime OOS {asset} prediction count is insufficient"
                )

    content_digest = str(meta.get("content_digest_sha256") or "")
    try:
        expected_digest = _runtime_content_digest(parsed, meta)
    except (TypeError, ValueError):
        expected_digest = None
    if (
        not _is_sha256(content_digest)
        or expected_digest is None
        or content_digest != expected_digest
    ):
        status["reasons"].append("runtime content digest does not match")

    status["weights"] = parsed or None
    status["meta"] = meta or None
    status["usable"] = not status["reasons"]
    return status


def load_runtime_weights(path: Path | None = None) -> dict[str, float] | None:
    """Return runtime weights only when every trust guard passes."""
    status = runtime_weight_status(path)
    if status["usable"]:
        return status["weights"]
    if status["exists"]:
        log.debug("runtime weights ignored: %s", "; ".join(status["reasons"]))
    return None


def model_trust_status(
    *,
    predictor_path: Path | None = None,
    runtime_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return one dashboard-ready trust view for every adaptive component."""
    predictor = predictor_artifact_status(
        predictor_path,
        requested_asset="share",
        now=now,
    )
    runtime = runtime_weight_status(runtime_path, now=now)
    trusted_components = []
    if predictor.get("usable"):
        trusted_components.append("share_predictor")
    if runtime.get("usable"):
        trusted_components.append("runtime_weights")
    return {
        "schema": MODEL_TRUST_SCHEMA,
        "status": "trusted_champion_active" if trusted_components else "source_controlled_defaults",
        "trusted_components": trusted_components,
        "predictor": predictor,
        "runtime_weights": runtime,
        "ordinary_scan_training": "disabled",
        "option_adaptation": "disabled_until_direct_broker_observed_targets_pass_oos",
        "safe_default": "zero_predictor_and_source_controlled_weights",
    }
