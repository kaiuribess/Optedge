"""Build a formal validation report from logged Optedge signals.

Outputs:
  - data/validation_report.html
  - data/validation_summary.json
  - data/equity_curve.png

The report is deliberately conservative: missing data is shown as unavailable
instead of being inferred. Run with:

    python reports/validation_report.py
"""
from __future__ import annotations

import argparse
import binascii
import glob
import html
import json
import math
import random
import struct
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
LOGS_DIR = ROOT / "logs"
ARCHIVE_DIR = ROOT / "archive"
REPORT_HTML = DATA_DIR / "validation_report.html"
SUMMARY_JSON = DATA_DIR / "validation_summary.json"
EQUITY_PNG = DATA_DIR / "equity_curve.png"
FACTOR_IC_JSON = DATA_DIR / "factor_ic_summary.json"
POSITION_AGING_JSON = DATA_DIR / "position_aging_summary.json"

MIN_CLOSED_SIGNALS = 500
BREAKEVEN_WIN_RATE = 0.50
DEFAULT_EQUITY_ALLOCATION_PCT = 0.01
MAX_EQUITY_ALLOCATION_PCT = 0.10
EQUITY_RETURN_MODE = "normalized_signal_allocation"


def _latest_archive_cutoff() -> Optional[pd.Timestamp]:
    """Use the latest archive/reset as the active experiment boundary.

    Adaptive model files are updated during scans, so their mtimes can move
    past positions that are still part of the same clean experiment. The
    archive folder is a steadier signal: everything left in data/ after the
    latest reset belongs to the current unarchived run history.
    """
    if not ARCHIVE_DIR.exists():
        return None
    runs = [p for p in ARCHIVE_DIR.glob("run_*") if p.is_dir()]
    if not runs:
        return None
    latest = max(runs, key=lambda p: p.stat().st_mtime)
    return pd.Timestamp(latest.stat().st_mtime, unit="s", tz="UTC")


def _current_scope_cutoff() -> Optional[pd.Timestamp]:
    """Return only a deliberate experiment/reset boundary.

    Runtime weights, pricing-model weights, and predictor coefficients are
    rewritten during normal scans. Their filesystem mtimes are therefore not
    experiment boundaries and must never make valid outcomes disappear.
    """
    return _latest_archive_cutoff()


def _existing_total_signals() -> Optional[int]:
    summary_path = DATA_DIR / "validation_summary.json"
    if not summary_path.exists():
        return None
    try:
        value = json.loads(summary_path.read_text(encoding="utf-8-sig")).get("total_signals")
        return int(value) if value is not None else None
    except Exception:
        return None


def _filter_since(df: pd.DataFrame, since: Optional[pd.Timestamp], date_col: str = "entry_time") -> pd.DataFrame:
    if df is None or df.empty or since is None or pd.isna(since) or date_col not in df.columns:
        return df
    return df[pd.to_datetime(df[date_col], errors="coerce", utc=True) >= since].copy()


def _filter_logs_for_scope(logs: pd.DataFrame, cutoff: Optional[pd.Timestamp]) -> pd.DataFrame:
    """Filter signal logs only when the cutoff does not erase active run data.

    Adaptive files such as model_weights.json are updated during each scan. If
    their mtime lands after the run's entry_time stamp, blindly filtering logs
    by that mtime makes the current dashboard look disconnected from the
    signals it just produced. Closed positions can still be filtered by model
    era; logs represent the active, unarchived experiment folder.
    """
    if logs is None or logs.empty or cutoff is None or pd.isna(cutoff) or "entry_time" not in logs.columns:
        return logs
    filtered = _filter_since(logs, cutoff)
    if not filtered.empty:
        return filtered
    entries = pd.to_datetime(logs["entry_time"], errors="coerce", utc=True).dropna()
    if entries.empty:
        return logs
    if cutoff > entries.max():
        return logs
    return filtered


def _read_json_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _read_json_object(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_parquets(pattern: str) -> pd.DataFrame:
    frames = []
    for fp in sorted(glob.glob(str(pattern))):
        try:
            df = pd.read_parquet(fp)
            if df.empty:
                continue
            df["_source_file"] = Path(fp).name
            frames.append(df)
        except Exception:
            continue
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def load_signal_logs() -> pd.DataFrame:
    opts = _load_parquets(LOGS_DIR / "signals_*.parquet")
    if not opts.empty:
        opts = opts[~opts["_source_file"].str.startswith(("shares_", "futures_"))].copy()
        opts["asset"] = "option"
    shares = _load_parquets(LOGS_DIR / "shares_signals_*.parquet")
    if not shares.empty:
        shares["asset"] = "share"
        shares["side"] = "shares"
    futures = _load_parquets(LOGS_DIR / "futures_signals_*.parquet")
    if not futures.empty:
        futures["asset"] = "futures"
        futures["side"] = "futures"
    frames = [df for df in (opts, shares, futures) if not df.empty]
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True, sort=False)
    if "entry_time" in out.columns:
        out["entry_time"] = pd.to_datetime(out["entry_time"], errors="coerce", utc=True)
    return out


def load_positions() -> Tuple[pd.DataFrame, pd.DataFrame]:
    open_frames = []
    closed_frames = []
    for asset, open_name, closed_name in [
        ("option", "open_positions.json", "closed_positions.json"),
        ("share", "open_share_positions.json", "closed_share_positions.json"),
        ("futures", "open_futures_positions.json", "closed_futures_positions.json"),
    ]:
        open_part = pd.DataFrame(_read_json_rows(DATA_DIR / open_name))
        closed_part = pd.DataFrame(_read_json_rows(DATA_DIR / closed_name))
        if not open_part.empty:
            open_part["asset"] = asset
            open_frames.append(open_part)
        if not closed_part.empty:
            closed_part["asset"] = asset
            closed_frames.append(closed_part)
    open_df = pd.concat(open_frames, ignore_index=True, sort=False) if open_frames else pd.DataFrame()
    closed_df = pd.concat(closed_frames, ignore_index=True, sort=False) if closed_frames else pd.DataFrame()
    for df in (open_df, closed_df):
        if not df.empty:
            for col in ("entry_time", "exit_time"):
                if col in df.columns:
                    df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)
    return open_df, closed_df


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def _fmt_pct(v: Optional[float]) -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "n/a"
    return f"{v * 100:+.2f}%"


def _fmt(v: Optional[float], digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "n/a"
    return f"{v:.{digits}f}"


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        value = float(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is pd.NA:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _profit_factor(returns: pd.Series) -> Optional[float]:
    r = _num(returns).dropna()
    if r.empty:
        return None
    gross_profit = float(r[r > 0].sum())
    gross_loss = abs(float(r[r < 0].sum()))
    if gross_loss <= 0:
        return None if gross_profit <= 0 else float("inf")
    return gross_profit / gross_loss


def _max_drawdown(returns: pd.Series) -> Optional[float]:
    r = _num(returns).dropna()
    if r.empty:
        return None
    equity = (1.0 + r).cumprod()
    equity = pd.concat([pd.Series([1.0]), equity], ignore_index=True)
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def _allocation_series(df: pd.DataFrame) -> pd.Series:
    """Best-effort per-signal account allocation for validation drawdowns.

    Old recommendation history often lacks dollar exposure. In that case use a
    transparent 1% account allocation per signal so an option expiring worthless
    is a -1% account event, not a false -100% account wipeout.
    """
    if df is None or df.empty:
        return pd.Series(dtype=float)
    idx = df.index
    allocation = pd.Series(DEFAULT_EQUITY_ALLOCATION_PCT, index=idx, dtype=float)
    if "kelly_pct" in df.columns:
        kelly = pd.to_numeric(df["kelly_pct"], errors="coerce")
        allocation = allocation.where(~((kelly > 0) & np.isfinite(kelly)), kelly)
    if "suggested_dollars" in df.columns:
        dollars = pd.to_numeric(df["suggested_dollars"], errors="coerce")
        bankroll = pd.to_numeric(df.get("bankroll", pd.Series(np.nan, index=idx)), errors="coerce")
        dollar_alloc = dollars / bankroll.where(bankroll > 0)
        allocation = allocation.where(~((dollar_alloc > 0) & np.isfinite(dollar_alloc)), dollar_alloc)
    if "portfolio_weight" in df.columns:
        weight = pd.to_numeric(df["portfolio_weight"], errors="coerce")
        allocation = allocation.where(~((weight > 0) & np.isfinite(weight)), weight)
    return allocation.clip(lower=0.0, upper=MAX_EQUITY_ALLOCATION_PCT).fillna(DEFAULT_EQUITY_ALLOCATION_PCT)


def _equity_return_series(df: pd.DataFrame, return_col: str = "pnl_pct") -> pd.Series:
    """Return per-signal account contributions for the validation equity curve."""
    if df is None or df.empty or return_col not in df.columns:
        return pd.Series(dtype=float)
    idx = df.index
    if "equity_return" in df.columns:
        direct = pd.to_numeric(df["equity_return"], errors="coerce").dropna()
        if not direct.empty:
            return direct
    if {"pnl_dollars", "bankroll"} <= set(df.columns):
        pnl_dollars = pd.to_numeric(df["pnl_dollars"], errors="coerce")
        bankroll = pd.to_numeric(df["bankroll"], errors="coerce")
        direct = (pnl_dollars / bankroll.where(bankroll > 0)).dropna()
        if not direct.empty:
            return direct.clip(lower=-1.0)
    raw = pd.to_numeric(df[return_col], errors="coerce")
    allocation = _allocation_series(df).reindex(idx)
    return (raw * allocation).dropna().clip(lower=-1.0)


def _stats(df: pd.DataFrame, return_col: str = "pnl_pct") -> Dict[str, Any]:
    if df is None or df.empty or return_col not in df.columns:
        return {
            "n": 0,
            "win_rate": None,
            "avg_return": None,
            "median_return": None,
            "profit_factor": None,
            "max_drawdown": None,
        }
    r = _num(df[return_col]).dropna()
    if r.empty:
        return {
            "n": 0,
            "win_rate": None,
            "avg_return": None,
            "median_return": None,
            "profit_factor": None,
            "max_drawdown": None,
        }
    return {
        "n": int(len(r)),
        "win_rate": float((r > 0).mean()),
        "avg_return": float(r.mean()),
        "median_return": float(r.median()),
        "profit_factor": _profit_factor(r),
        "max_drawdown": _max_drawdown(_equity_return_series(df, return_col)),
        "max_drawdown_mode": EQUITY_RETURN_MODE,
        "best": float(r.max()),
        "worst": float(r.min()),
    }


def _bucket_label(v: Any, buckets: List[Tuple[float, float, str]]) -> str:
    try:
        x = float(v)
    except Exception:
        return "Unavailable"
    if math.isnan(x):
        return "Unavailable"
    for lo, hi, label in buckets:
        if lo <= x < hi:
            return label
    return buckets[-1][2]


def _bucket_performance(df: pd.DataFrame, source_col: str, buckets: List[Tuple[float, float, str]]) -> List[Dict[str, Any]]:
    if df.empty or source_col not in df.columns:
        return [{"bucket": "Unavailable", **_stats(pd.DataFrame())}]
    temp = df.copy()
    temp["_bucket"] = temp[source_col].map(lambda v: _bucket_label(v, buckets))
    rows = []
    for label, sub in temp.groupby("_bucket", dropna=False):
        row = {"bucket": str(label)}
        row.update(_stats(sub))
        rows.append(row)
    return sorted(rows, key=lambda r: r["bucket"])


def _factor_ic(closed: pd.DataFrame, min_n: int = 5,
               min_reliable_n: int = 100, min_reliable_days: int = 10) -> List[Dict[str, Any]]:
    """Per-factor information coefficient from closed positions.

    The position tracker now preserves factor z-columns at entry, so each
    closed recommendation can say which entry factors were predictive.
    """
    if closed is None or closed.empty or "pnl_pct" not in closed.columns:
        return []
    z_cols = [c for c in closed.columns if str(c).startswith("z_")]
    rows = []
    for col in z_cols:
        keep = [col, "pnl_pct"] + (["entry_time"] if "entry_time" in closed.columns else [])
        sub = closed[keep].copy()
        sub[col] = pd.to_numeric(sub[col], errors="coerce")
        sub["pnl_pct"] = pd.to_numeric(sub["pnl_pct"], errors="coerce")
        sub = sub.dropna(subset=[col, "pnl_pct"])
        if len(sub) < min_n or sub[col].nunique() < 2:
            continue
        ic = sub[col].corr(sub["pnl_pct"])
        if pd.isna(ic):
            continue
        trading_days = (
            int(pd.to_datetime(sub["entry_time"], errors="coerce", utc=True).dt.date.nunique())
            if "entry_time" in sub.columns else 0
        )
        is_reliable = len(sub) >= min_reliable_n and trading_days >= min_reliable_days
        if not is_reliable:
            reliability = "insufficient_history"
        elif ic >= 0.03:
            reliability = "supportive"
        elif ic <= -0.03:
            reliability = "adverse"
        else:
            reliability = "weak"
        rows.append({
            "factor": col.replace("z_", ""),
            "z_col": col,
            "n": int(len(sub)),
            "trading_days": trading_days,
            "ic": float(ic),
            "avg_score": float(sub[col].mean()),
            "reliability": reliability,
            "is_reliable": is_reliable,
        })
    return sorted(rows, key=lambda r: abs(r["ic"]), reverse=True)


def _position_aging(open_df: pd.DataFrame) -> Dict[str, Any]:
    if open_df is None or open_df.empty or "entry_time" not in open_df.columns:
        asset_counts = {}
        if open_df is not None and not open_df.empty and "asset" in open_df.columns:
            asset_counts = open_df["asset"].fillna("unknown").astype(str).value_counts().to_dict()
        return {
            "open_count": int(len(open_df) if open_df is not None else 0),
            "asset_breakdown": asset_counts,
            "buckets": [],
            "oldest": [],
        }
    df = open_df.copy()
    now = pd.Timestamp.now(tz="UTC")
    df["entry_time"] = pd.to_datetime(df["entry_time"], errors="coerce", utc=True)
    df["age_days"] = (now - df["entry_time"]).dt.total_seconds() / 86400.0
    df["age_days"] = df["age_days"].clip(lower=0)
    asset_counts = (
        df["asset"].fillna("unknown").astype(str).value_counts().to_dict()
        if "asset" in df.columns else {}
    )
    bins = [-0.01, 1, 3, 7, 14, 30, float("inf")]
    labels = ["0-1d", "1-3d", "3-7d", "7-14d", "14-30d", "30d+"]
    df["age_bucket"] = pd.cut(df["age_days"], bins=bins, labels=labels)
    buckets = []
    for label, sub in df.groupby("age_bucket", observed=True):
        avg_unrealized = None
        if "unrealized_pct" in sub.columns:
            avg = pd.to_numeric(sub["unrealized_pct"], errors="coerce").mean()
            avg_unrealized = None if pd.isna(avg) else float(avg)
        buckets.append({
            "bucket": str(label),
            "count": int(len(sub)),
            "avg_unrealized_pct": avg_unrealized,
        })
    keep = [c for c in ("asset", "ticker", "symbol", "side", "direction", "expiry", "entry_time", "age_days",
                        "unrealized_pct", "confidence", "trade_status")
            if c in df.columns]
    oldest = df.sort_values("age_days", ascending=False).head(20)[keep].to_dict(orient="records")
    return {
        "open_count": int(len(df)),
        "asset_breakdown": asset_counts,
        "buckets": buckets,
        "oldest": oldest,
    }


def _side_performance(closed: pd.DataFrame) -> List[Dict[str, Any]]:
    if closed.empty or "side" not in closed.columns:
        return []
    rows = []
    for side in ("call", "put"):
        sub = closed[closed["side"].astype(str).str.lower() == side]
        row = {"bucket": side}
        row.update(_stats(sub))
        rows.append(row)
    return rows


def _closed_with_slippage(closed: pd.DataFrame) -> pd.DataFrame:
    if closed.empty:
        return closed
    out = closed.copy()
    try:
        from config import FILL_SLIPPAGE_PCT

        slippage = float(FILL_SLIPPAGE_PCT)
    except Exception:
        slippage = 0.04
    asset = out.get("asset", pd.Series("option", index=out.index)).astype(str).str.lower()
    side = out.get("side", pd.Series("", index=out.index)).astype(str).str.lower()
    out["pnl_pct_after_slippage"] = _num(out.get("pnl_pct", pd.Series(np.nan, index=out.index)))
    out.loc[(asset == "option") | side.isin(["call", "put"]), "pnl_pct_after_slippage"] -= slippage
    return out


def _learning_sample_stats(asset: str, closed: pd.DataFrame) -> Dict[str, int]:
    if closed.empty:
        return {
            "learning_eligible_closed_positions": 0,
            "learning_excluded_closed_positions": 0,
            "execution_eligible_closed_positions": 0,
            "non_executable_closed_positions": 0,
            "excluded_explicit_not_actionable": 0,
            "excluded_non_actionable_status": 0,
            "excluded_guard_blocked": 0,
            "excluded_non_positive_size": 0,
            "same_scan_dynamic_exits": 0,
            "learning_closed_trading_days": 0,
        }
    try:
        from backtest.exit_learning import (
            MIN_LEARNING_HOLD_HOURS, eligible_closed_for_learning,
            execution_eligibility_summary,
        )

        eligible = eligible_closed_for_learning(asset, closed)
        execution_summary = execution_eligibility_summary(asset, closed)
    except Exception:
        MIN_LEARNING_HOLD_HOURS = 1.0
        eligible = pd.DataFrame()
        execution_summary = {
            "execution_eligible_closed_positions": 0,
            "non_executable_closed_positions": 0,
            "excluded_explicit_not_actionable": 0,
            "excluded_non_actionable_status": 0,
            "excluded_guard_blocked": 0,
            "excluded_non_positive_size": 0,
        }
    asset_rows = closed[closed.get("asset", "") == asset].copy()
    entry = pd.to_datetime(asset_rows.get("entry_time"), errors="coerce", utc=True)
    exit_time = pd.to_datetime(asset_rows.get("exit_time"), errors="coerce", utc=True)
    if isinstance(entry, pd.Series) and isinstance(exit_time, pd.Series):
        holding_hours = (exit_time - entry).dt.total_seconds() / 3600.0
    else:
        holding_hours = pd.Series(np.nan, index=asset_rows.index)
    reasons = asset_rows.get("exit_reason", pd.Series("", index=asset_rows.index)).fillna("").astype(str)
    same_scan = int((reasons.eq("dynamic_exit") & (
        holding_hours.isna() | (holding_hours < MIN_LEARNING_HOLD_HOURS)
    )).sum())
    date_source = eligible.get("entry_time", eligible.get("exit_time", pd.Series(dtype=str)))
    trading_days = int(pd.to_datetime(date_source, errors="coerce", utc=True).dt.date.nunique()) \
        if not eligible.empty else 0
    return {
        **execution_summary,
        "learning_eligible_closed_positions": int(len(eligible)),
        "learning_excluded_closed_positions": int(max(0, len(asset_rows) - len(eligible))),
        "same_scan_dynamic_exits": same_scan,
        "learning_closed_trading_days": trading_days,
    }


def _swing_eligible_sample(closed: pd.DataFrame) -> pd.DataFrame:
    if closed.empty:
        return closed.copy()
    try:
        from backtest.exit_learning import eligible_closed_for_learning

        frames = [eligible_closed_for_learning(asset, closed) for asset in ("option", "share", "futures")]
        frames = [frame for frame in frames if not frame.empty]
        return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def _asset_breakdown(open_df: pd.DataFrame, closed: pd.DataFrame) -> Dict[str, Any]:
    out = {}
    reviews = _load_exit_reviews()
    policy = _exit_policy_summary()
    policies = policy.get("assets", {}) if isinstance(policy, dict) else {}
    for asset in ("option", "share", "futures"):
        open_sub = open_df[open_df.get("asset", "") == asset] if not open_df.empty else pd.DataFrame()
        closed_sub = closed[closed.get("asset", "") == asset] if not closed.empty else pd.DataFrame()
        review_sub = (
            reviews[reviews["asset"] == asset]
            if not reviews.empty and "asset" in reviews.columns else pd.DataFrame()
        )
        asset_policy = policies.get(asset, {}) if isinstance(policies, dict) else {}
        out[asset] = {
            "open_positions": int(len(open_sub)),
            "closed_positions": int(len(closed_sub)),
            "overall": _stats(closed_sub),
            "after_slippage": _stats(closed_sub, "pnl_pct_after_slippage"),
            "exit_reasons": (
                closed_sub.get("exit_reason", pd.Series(dtype=str)).fillna("unknown")
                .astype(str).value_counts().to_dict()
                if not closed_sub.empty else {}
            ),
            "dynamic_exit_actions": (
                review_sub.get("action", pd.Series(dtype=str)).fillna("unknown")
                .astype(str).value_counts().to_dict()
                if not review_sub.empty else {}
            ),
            "learned_policy_active": bool(asset_policy.get("learned_active", False)),
            "policy_version": asset_policy.get("policy_version", policy.get("policy_version", "default")
                                                if isinstance(policy, dict) else "default"),
            "pnl_dollars": (
                float(pd.to_numeric(closed_sub.get("pnl_dollars"), errors="coerce").sum())
                if "pnl_dollars" in closed_sub.columns else None
            ),
            "pnl_points": (
                float(pd.to_numeric(closed_sub.get("pnl_points"), errors="coerce").sum())
                if "pnl_points" in closed_sub.columns else None
            ),
            "pnl_pct_column": "pnl_pct" if "pnl_pct" in closed_sub.columns else None,
        }
        out[asset].update(_learning_sample_stats(asset, closed))
    return out


def _load_exit_reviews() -> pd.DataFrame:
    path = DATA_DIR / "exit_reviews.jsonl"
    if not path.exists():
        return pd.DataFrame()
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except Exception:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _exit_review_summary() -> Dict[str, Any]:
    df = _load_exit_reviews()
    if df.empty:
        return {"by_asset": {}, "total_reviews": 0}
    by_asset = {}
    for asset, sub in df.groupby("asset"):
        by_asset[str(asset)] = {
            "reviews": int(len(sub)),
            "actions": sub.get("action", pd.Series(dtype=str)).fillna("unknown").astype(str).value_counts().to_dict(),
            "learned_policy_reviews": int(sub.get("used_learned_policy", pd.Series(False, index=sub.index)).fillna(False).astype(bool).sum()),
        }
    return {"by_asset": by_asset, "total_reviews": int(len(df))}


def _exit_effectiveness(closed: pd.DataFrame) -> Dict[str, Any]:
    if closed is None or closed.empty:
        return {}
    out = {}
    for asset, sub in closed.groupby(closed.get("asset", pd.Series("option", index=closed.index))):
        dyn = sub[sub.get("exit_reason", pd.Series("", index=sub.index)).astype(str) == "dynamic_exit"]
        hard = sub[sub.get("exit_reason", pd.Series("", index=sub.index)).astype(str).str.startswith("hard_")]
        out[str(asset)] = {
            "dynamic_exit": _stats(dyn),
            "hard_exit": _stats(hard),
            "dynamic_exit_count": int(len(dyn)),
            "hard_exit_count": int(len(hard)),
        }
    return out


def _exit_policy_summary() -> Dict[str, Any]:
    try:
        from backtest.exit_learning import load_exit_policy
        return load_exit_policy()
    except Exception:
        return {}


def _period_return(symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> Optional[float]:
    try:
        import yfinance as yf

        hist = yf.Ticker(symbol).history(
            start=start.date().isoformat(),
            end=(end + pd.Timedelta(days=1)).date().isoformat(),
            auto_adjust=True,
        )
        if hist is None or hist.empty or len(hist["Close"].dropna()) < 2:
            return None
        close = hist["Close"].dropna()
        return float(close.iloc[-1] / close.iloc[0] - 1.0)
    except Exception:
        return None


def _benchmark_comparison(closed: pd.DataFrame) -> Dict[str, Any]:
    if closed.empty or "entry_time" not in closed.columns:
        return {"SPY": None, "QQQ": None, "note": "No dated closed positions."}
    start = closed["entry_time"].dropna().min()
    end_col = "exit_time" if "exit_time" in closed.columns else "entry_time"
    end = closed[end_col].dropna().max()
    if pd.isna(start) or pd.isna(end):
        return {"SPY": None, "QQQ": None, "note": "Closed positions have missing dates."}
    return {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "SPY": _period_return("SPY", start, end),
        "QQQ": _period_return("QQQ", start, end),
    }


def _random_baseline(returns: Iterable[float], trials: int = 1000) -> Dict[str, Any]:
    vals = [abs(float(v)) for v in returns if v is not None and not math.isnan(float(v))]
    if not vals:
        return {"n": 0, "avg_return": None, "win_rate": None}
    rng = random.Random(42)
    trial_means = []
    trial_wins = []
    for _ in range(trials):
        signs = [1 if rng.random() >= 0.5 else -1 for _ in vals]
        sim = np.array(vals, dtype=float) * np.array(signs, dtype=float)
        trial_means.append(float(sim.mean()))
        trial_wins.append(float((sim > 0).mean()))
    return {
        "n": len(vals),
        "avg_return": float(np.mean(trial_means)),
        "median_avg_return": float(np.median(trial_means)),
        "win_rate": float(np.mean(trial_wins)),
        "trials": trials,
    }


def _write_equity_curve(closed: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if closed.empty or "pnl_pct_after_slippage" not in closed.columns:
        _write_empty_equity_curve(path)
        return
    curve = closed.copy()
    sort_col = "exit_time" if "exit_time" in curve.columns else "entry_time"
    if sort_col in curve.columns:
        curve = curve.sort_values(sort_col)
    r = _equity_return_series(curve, "pnl_pct_after_slippage").fillna(0.0)
    if r.empty:
        _write_empty_equity_curve(path)
        return
    equity = (1.0 + r).cumprod()
    try:
        import matplotlib.pyplot as plt

        plt.figure(figsize=(9, 4.5))
        plt.plot(range(1, len(equity) + 1), equity.values, linewidth=2.0, color="#2563eb")
        plt.axhline(1.0, color="#64748b", linewidth=1.0, linestyle="--")
        plt.title("Optedge Normalized Closed-Signal Equity Curve")
        plt.xlabel("Closed signal")
        plt.ylabel("Equity multiple (normalized allocation)")
        plt.grid(True, alpha=0.25)
        plt.tight_layout()
        plt.savefig(path, dpi=140)
        plt.close()
    except Exception:
        _write_equity_curve_pil(equity, path)


def _write_equity_curve_pil(equity: pd.Series, path: Path) -> None:
    """Dependency-light PNG chart fallback when matplotlib is unavailable."""
    try:
        from PIL import Image, ImageDraw, ImageFont

        vals = [float(v) for v in equity.dropna().tolist()]
        if not vals:
            _write_valid_blank_png(path)
            return
        width, height = 1000, 500
        margin_l, margin_r, margin_t, margin_b = 76, 28, 44, 60
        plot_w = width - margin_l - margin_r
        plot_h = height - margin_t - margin_b
        bg = (8, 11, 16)
        panel = (15, 23, 42)
        grid = (35, 48, 68)
        text = (203, 213, 225)
        muted = (148, 163, 184)
        blue = (56, 189, 248)
        green = (16, 185, 129)
        red = (239, 68, 68)

        img = Image.new("RGB", (width, height), bg)
        draw = ImageDraw.Draw(img)
        draw.rectangle([20, 18, width - 20, height - 20], fill=panel, outline=grid)
        font = ImageFont.load_default()
        draw.text((margin_l, 20), "Optedge Normalized Closed-Signal Equity Curve", fill=text, font=font)

        lo = min(min(vals), 1.0)
        hi = max(max(vals), 1.0)
        pad = max((hi - lo) * 0.08, 0.05)
        lo -= pad
        hi += pad
        if hi <= lo:
            hi = lo + 1.0

        def xy(idx: int, value: float) -> tuple[int, int]:
            x = margin_l + int((idx / max(len(vals) - 1, 1)) * plot_w)
            y = margin_t + int((1.0 - ((value - lo) / (hi - lo))) * plot_h)
            return x, y

        for i in range(5):
            y = margin_t + int(i * plot_h / 4)
            draw.line([(margin_l, y), (margin_l + plot_w, y)], fill=grid)
        for i in range(6):
            x = margin_l + int(i * plot_w / 5)
            draw.line([(x, margin_t), (x, margin_t + plot_h)], fill=grid)

        baseline_y = xy(0, 1.0)[1]
        draw.line([(margin_l, baseline_y), (margin_l + plot_w, baseline_y)], fill=muted)
        points = [xy(i, v) for i, v in enumerate(vals)]
        line_color = green if vals[-1] >= 1.0 else red
        if len(points) == 1:
            x, y = points[0]
            draw.ellipse([x - 3, y - 3, x + 3, y + 3], fill=line_color)
        else:
            draw.line(points, fill=blue, width=3)
            x, y = points[-1]
            draw.ellipse([x - 5, y - 5, x + 5, y + 5], fill=line_color)

        labels = [
            (f"{hi:.2f}x", margin_t),
            ("1.00x", baseline_y),
            (f"{lo:.2f}x", margin_t + plot_h - 10),
        ]
        for label, y in labels:
            draw.text((24, y), label, fill=muted, font=font)
        draw.text((margin_l, height - 38), f"Closed signals: {len(vals)}", fill=muted, font=font)
        draw.text((width - 210, height - 38), f"Latest: {vals[-1]:.2f}x", fill=text, font=font)
        img.save(path, format="PNG")
    except Exception:
        _write_valid_blank_png(path)


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)
    )


def _write_valid_blank_png(path: Path) -> None:
    """Write a tiny valid transparent PNG without optional dependencies."""
    width, height = 1, 1
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    raw = b"\x00\xff\xff\xff\x00"
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def _write_empty_equity_curve(path: Path) -> None:
    """Create a valid placeholder chart when no closed trades exist yet."""
    try:
        import matplotlib.pyplot as plt

        plt.figure(figsize=(9, 4.5))
        plt.axhline(1.0, color="#64748b", linewidth=1.0, linestyle="--")
        plt.text(
            0.5, 0.52, "No closed positions yet",
            ha="center", va="center", fontsize=15, color="#475569",
            transform=plt.gca().transAxes,
        )
        plt.title("Optedge Closed-Signal Equity Curve")
        plt.xlabel("Closed signal")
        plt.ylabel("Equity multiple")
        plt.xlim(0, 1)
        plt.ylim(0.95, 1.05)
        plt.grid(True, alpha=0.2)
        plt.tight_layout()
        plt.savefig(path, dpi=140)
        plt.close()
    except Exception:
        _write_valid_blank_png(path)


def build_summary(scope: str = "current_model", since: Optional[str] = None) -> Dict[str, Any]:
    logs = load_signal_logs()
    open_df, closed_raw = load_positions()
    closed = _closed_with_slippage(closed_raw)
    all_time_closed = closed.copy()

    cutoff = pd.to_datetime(since, errors="coerce", utc=True) if since else None
    cutoff_source = "explicit_since" if cutoff is not None and not pd.isna(cutoff) else None
    if scope == "current_model" and cutoff is None:
        cutoff = _current_scope_cutoff()
        cutoff_source = "latest_archive_reset" if cutoff is not None else "all_unarchived_history"
    elif scope == "all_time":
        cutoff_source = "all_time"
    if scope != "all_time":
        logs = _filter_logs_for_scope(logs, cutoff)
        closed = _filter_since(closed, cutoff)

    total_signals = int(len(logs))
    if total_signals == 0 and list(LOGS_DIR.glob("*signals_*.parquet")):
        total_signals = _existing_total_signals() or 0
    closed_count = int(len(closed))
    open_count = int(len(open_df))
    overall = _stats(closed)
    after_slippage = _stats(closed, "pnl_pct_after_slippage")
    assets = _asset_breakdown(open_df, closed)
    swing_eligible = _swing_eligible_sample(closed)
    swing_eligible_count = int(len(swing_eligible))
    swing_eligible_overall = _stats(swing_eligible)
    swing_eligible_after_slippage = _stats(swing_eligible, "pnl_pct_after_slippage")
    factor_ic = _factor_ic(swing_eligible)
    all_closure_factor_ic = _factor_ic(closed)
    fixed_horizon = _read_json_object(DATA_DIR / "fixed_horizon_summary.json")

    warnings = []
    if scope != "all_time" and cutoff is not None:
        stale_excluded = max(0, len(all_time_closed) - len(closed))
        if stale_excluded:
            warnings.append(
                f"Excluded {stale_excluded} older closed positions from the primary metrics because they predate the current experiment boundary."
            )
    elif scope != "all_time":
        warnings.append(
            "No archive/reset boundary found; current-scope metrics include all unarchived local outcomes."
        )
    if swing_eligible_count < MIN_CLOSED_SIGNALS:
        warnings.append(
            f"Executable swing sample too small: {swing_eligible_count} executable outcomes; "
            f"need at least {MIN_CLOSED_SIGNALS}."
        )
    if overall.get("max_drawdown") is not None and overall["max_drawdown"] < -0.20:
        warnings.append(f"All-closure max drawdown is worse than -20%: {_fmt_pct(overall['max_drawdown'])}.")
    if overall.get("win_rate") is not None and overall["win_rate"] < BREAKEVEN_WIN_RATE:
        warnings.append(
            f"All-closure win rate is below the simple breakeven threshold: {_fmt_pct(overall['win_rate'])}."
        )
    if (
        swing_eligible_after_slippage.get("max_drawdown") is not None
        and swing_eligible_after_slippage["max_drawdown"] < -0.20
    ):
        warnings.append(
            "Executable swing max drawdown after slippage is worse than -20%: "
            f"{_fmt_pct(swing_eligible_after_slippage['max_drawdown'])}."
        )
    if (
        swing_eligible_after_slippage.get("win_rate") is not None
        and swing_eligible_after_slippage["win_rate"] < BREAKEVEN_WIN_RATE
    ):
        warnings.append(
            "Executable swing win rate after slippage is below the simple breakeven threshold: "
            f"{_fmt_pct(swing_eligible_after_slippage['win_rate'])}."
        )
    option_learning = assets.get("option", {}) if isinstance(assets, dict) else {}
    same_scan_dynamic = int(option_learning.get("same_scan_dynamic_exits") or 0)
    if same_scan_dynamic:
        warnings.append(
            f"Detected {same_scan_dynamic} same-scan dynamic option exit(s). They remain in performance metrics "
            "but are excluded from exit-policy learning as lifecycle churn."
        )
    reliable_factor_count = sum(1 for row in factor_ic if row.get("is_reliable"))
    if factor_ic and reliable_factor_count == 0:
        warnings.append(
            "Factor IC is exploratory: no factor yet has both 100 executable outcomes and 10 distinct entry days."
        )
    for warning in fixed_horizon.get("warnings", [])[:3]:
        warnings.append(f"Fixed-horizon: {warning}")

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "validation_scope": scope,
        "validation_scope_basis": cutoff_source,
        "current_model_cutoff": cutoff.isoformat() if cutoff is not None and not pd.isna(cutoff) else None,
        "current_experiment_cutoff": cutoff.isoformat() if cutoff is not None and not pd.isna(cutoff) else None,
        "all_time_closed_positions": int(len(all_time_closed)),
        "stale_closed_positions_excluded": int(max(0, len(all_time_closed) - len(closed))),
        "total_signals": total_signals,
        "closed_positions": closed_count,
        "open_positions": open_count,
        "validation_basis": "executable_swing_after_slippage",
        "swing_eligible_closed_positions": swing_eligible_count,
        "swing_excluded_closed_positions": int(max(0, closed_count - swing_eligible_count)),
        "swing_eligible_overall": swing_eligible_overall,
        "swing_eligible_after_slippage": swing_eligible_after_slippage,
        "equity_curve": {
            "mode": EQUITY_RETURN_MODE,
            "default_allocation_pct": DEFAULT_EQUITY_ALLOCATION_PCT,
            "max_allocation_pct": MAX_EQUITY_ALLOCATION_PCT,
            "description": (
                "Drawdown and equity curve use per-signal account contributions. "
                "When exact exposure is unavailable, each signal is treated as a 1% account allocation."
            ),
        },
        "overall": overall,
        "all_time_overall": _stats(all_time_closed),
        "after_slippage": after_slippage,
        "assets": assets,
        "exit_reviews": _exit_review_summary(),
        "exit_effectiveness": _exit_effectiveness(closed),
        "exit_policy": _exit_policy_summary(),
        "calls_vs_puts": _side_performance(closed),
        "dte_buckets": _bucket_performance(closed, "dte_at_entry", [
            (0, 8, "0-7 DTE"),
            (8, 15, "8-14 DTE"),
            (15, 31, "15-30 DTE"),
            (31, 61, "31-60 DTE"),
            (61, float("inf"), "61+ DTE"),
        ]),
        "spread_buckets": _bucket_performance(closed, "spread_pct", [
            (0.0, 0.05, "0-5%"),
            (0.05, 0.10, "5-10%"),
            (0.10, 0.15, "10-15%"),
            (0.15, float("inf"), "15%+"),
        ]),
        "confidence_buckets": _bucket_performance(closed, "confidence", [
            (0, 55, "<55"),
            (55, 70, "55-69"),
            (70, 85, "70-84"),
            (85, float("inf"), "85+"),
        ]),
        "factor_ic_basis": "independent_swing_outcomes",
        "factor_ic_reliable_count": reliable_factor_count,
        "factor_ic": factor_ic,
        "all_closure_factor_ic": all_closure_factor_ic,
        "position_aging": _position_aging(open_df),
        "benchmarks": _benchmark_comparison(closed),
        "random_baseline": _random_baseline(_num(closed.get("pnl_pct", pd.Series(dtype=float))).dropna()),
        "fixed_horizon": fixed_horizon,
        "warnings": warnings,
    }
    return summary


def _metric_table(rows: List[Tuple[str, Any]]) -> str:
    body = []
    for label, value in rows:
        body.append(f"<tr><td>{html.escape(str(label))}</td><td>{html.escape(str(value))}</td></tr>")
    return "<table><tbody>" + "".join(body) + "</tbody></table>"


def _bucket_table(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "<p class='muted'>No rows available.</p>"
    body = []
    for row in rows:
        body.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('bucket', '-')))}</td>"
            f"<td>{int(row.get('n') or 0)}</td>"
            f"<td>{_fmt_pct(row.get('win_rate'))}</td>"
            f"<td>{_fmt_pct(row.get('avg_return'))}</td>"
            f"<td>{_fmt_pct(row.get('median_return'))}</td>"
            f"<td>{_fmt(row.get('profit_factor'))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Bucket</th><th>n</th><th>Win rate</th>"
        "<th>Avg return</th><th>Median return</th><th>Profit factor</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def _fixed_horizon_table(summary: Dict[str, Any]) -> str:
    rows = summary.get("by_horizon", []) if isinstance(summary, dict) else []
    if not rows:
        return "<p class='muted'>No matured fixed-session outcomes yet.</p>"
    body = []
    for row in rows:
        executable = row.get("executable", {}) or {}
        shadow = row.get("shadow_current_method", {}) or {}
        ci_low = shadow.get("win_rate_ci_low")
        ci_high = shadow.get("win_rate_ci_high")
        ci = (
            f"{_fmt_pct(ci_low)} to {_fmt_pct(ci_high)}"
            if ci_low is not None and ci_high is not None else "n/a"
        )
        body.append(
            "<tr>"
            f"<td>{int(row.get('horizon_sessions') or 0)}</td>"
            f"<td>{int(executable.get('n') or 0)}</td>"
            f"<td>{int(shadow.get('n') or 0)}</td>"
            f"<td>{int(shadow.get('unique_entry_days') or 0)}</td>"
            f"<td>{_fmt_pct(shadow.get('win_rate'))}</td>"
            f"<td>{html.escape(ci)}</td>"
            f"<td>{_fmt_pct(shadow.get('avg_return'))}</td>"
            f"<td>{_fmt_pct(shadow.get('avg_excess_vs_spy'))}</td>"
            f"<td>{_fmt(shadow.get('profit_factor'))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Sessions</th><th>Executed n</th><th>Shadow n</th>"
        "<th>Shadow days</th><th>Shadow win</th><th>95% interval</th><th>Avg after costs</th>"
        "<th>Avg excess vs SPY</th><th>Profit factor</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def _factor_ic_table(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "<p class='muted'>No closed positions with factor snapshots yet.</p>"
    body = []
    for row in rows:
        ic = row.get("ic")
        reliable = bool(row.get("is_reliable"))
        color = (
            "#64748b" if not reliable
            else "#047857" if ic is not None and ic > 0
            else "#b91c1c" if ic is not None and ic < 0
            else "#475569"
        )
        body.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('factor', '-')))}</td>"
            f"<td>{int(row.get('n') or 0)}</td>"
            f"<td>{int(row.get('trading_days') or 0)}</td>"
            f"<td style='color:{color};font-weight:600'>{_fmt(ic, 4)}</td>"
            f"<td>{_fmt(row.get('avg_score'), 3)}</td>"
            f"<td>{html.escape(str(row.get('reliability') or 'unknown'))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Factor</th><th>n</th><th>Days</th><th>IC</th>"
        "<th>Avg entry z</th><th>Reliability</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def _asset_table(assets: Dict[str, Any]) -> str:
    body = []
    for asset, row in (assets or {}).items():
        overall = row.get("overall", {})
        body.append(
            "<tr>"
            f"<td>{html.escape(str(asset))}</td>"
            f"<td>{int(row.get('open_positions') or 0)}</td>"
            f"<td>{int(row.get('closed_positions') or 0)}</td>"
            f"<td>{int(row.get('execution_eligible_closed_positions') or 0)}</td>"
            f"<td>{int(row.get('non_executable_closed_positions') or 0)}</td>"
            f"<td>{int(row.get('learning_eligible_closed_positions') or 0)}</td>"
            f"<td>{int(row.get('same_scan_dynamic_exits') or 0)}</td>"
            f"<td>{int(row.get('learning_closed_trading_days') or 0)}</td>"
            f"<td>{_fmt_pct(overall.get('win_rate'))}</td>"
            f"<td>{_fmt_pct(overall.get('avg_return'))}</td>"
            f"<td>{_fmt_pct(overall.get('median_return'))}</td>"
            f"<td>{_fmt_pct(overall.get('max_drawdown'))}</td>"
            f"<td>{_fmt(overall.get('profit_factor'))}</td>"
            f"<td>{html.escape(json.dumps(row.get('dynamic_exit_actions', {})))}</td>"
            f"<td>{html.escape(str(row.get('learned_policy_active', False)))}</td>"
            f"<td>{_fmt(row.get('pnl_dollars'))}</td>"
            f"<td>{_fmt(row.get('pnl_points'))}</td>"
            f"<td>{html.escape(json.dumps(row.get('exit_reasons', {})))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Asset</th><th>Open</th><th>Closed</th><th>Executable</th><th>Non-executable</th><th>Learnable</th><th>Same-scan churn</th><th>Learning days</th>"
        "<th>Win rate</th><th>Avg return</th><th>Median</th><th>Max DD</th>"
        "<th>Profit factor</th><th>Exit actions</th><th>Learned exits</th>"
        "<th>P&L dollars</th><th>P&L points</th><th>Exit reasons</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def render_html(summary: Dict[str, Any]) -> str:
    overall = summary["overall"]
    slip = summary["after_slippage"]
    bench = summary["benchmarks"]
    baseline = summary["random_baseline"]
    equity = summary.get("equity_curve") if isinstance(summary.get("equity_curve"), dict) else {}
    swing = (
        summary.get("swing_eligible_after_slippage")
        if isinstance(summary.get("swing_eligible_after_slippage"), dict)
        else {}
    )
    fixed_horizon = (
        summary.get("fixed_horizon")
        if isinstance(summary.get("fixed_horizon"), dict)
        else {}
    )
    option_market_data = (
        fixed_horizon.get("option_market_data")
        if isinstance(fixed_horizon.get("option_market_data"), dict)
        else {}
    )
    warnings = summary.get("warnings") or []
    warning_html = "".join(f"<li>{html.escape(w)}</li>" for w in warnings) or "<li>No major validation warnings.</li>"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Optedge Validation Report</title>
<style>
body {{ margin: 0; font-family: Inter, Arial, sans-serif; background: #f8fafc; color: #0f172a; }}
main {{ max-width: 1180px; margin: 0 auto; padding: 32px 20px 48px; }}
h1 {{ margin: 0 0 6px; font-size: 34px; }}
h2 {{ margin: 0 0 14px; font-size: 20px; }}
.muted {{ color: #64748b; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; margin: 22px 0; }}
section {{ min-width: 0; overflow-x: auto; background: white; border: 1px solid #e2e8f0; border-radius: 8px; padding: 18px; box-shadow: 0 1px 2px rgba(15,23,42,.04); }}
table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
th, td {{ padding: 9px 8px; border-bottom: 1px solid #e2e8f0; text-align: left; }}
th {{ color: #475569; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }}
.warn {{ border-left: 4px solid #f59e0b; }}
.danger {{ border-left: 4px solid #ef4444; }}
img {{ max-width: 100%; border: 1px solid #e2e8f0; border-radius: 8px; }}
</style>
</head>
<body>
<main>
  <h1>Optedge Validation Report</h1>
  <div class="muted">Generated {html.escape(summary["generated_at"])} from local signal logs and position state.</div>
  <div class="muted">Scope: {html.escape(str(summary.get("validation_scope", "current_model")))}; basis: {html.escape(str(summary.get("validation_scope_basis") or "n/a"))}; cutoff: {html.escape(str(summary.get("current_experiment_cutoff") or summary.get("current_model_cutoff") or "n/a"))}</div>

  <section class="warn">
    <h2>Warnings</h2>
    <ul>{warning_html}</ul>
  </section>

  <div class="grid">
    <section>
      <h2>Core Metrics</h2>
      {_metric_table([
          ("Total logged signals", summary["total_signals"]),
          ("Closed positions", summary["closed_positions"]),
          ("Open positions", summary["open_positions"]),
          ("All-time closed positions", summary.get("all_time_closed_positions", 0)),
          ("Stale closed excluded", summary.get("stale_closed_positions_excluded", 0)),
          ("Win rate", _fmt_pct(overall.get("win_rate"))),
          ("Average return", _fmt_pct(overall.get("avg_return"))),
          ("Median return", _fmt_pct(overall.get("median_return"))),
          ("Profit factor", _fmt(overall.get("profit_factor"))),
          ("Max drawdown", _fmt_pct(overall.get("max_drawdown"))),
          ("Drawdown mode", equity.get("mode", "n/a")),
          ("Default signal allocation", _fmt_pct(equity.get("default_allocation_pct"))),
      ])}
    </section>
    <section>
      <h2>Executable Swing Sample</h2>
      {_metric_table([
          ("Validation basis", summary.get("validation_basis", "n/a")),
          ("Executable closures", summary.get("swing_eligible_closed_positions", 0)),
          ("Excluded from executable sample", summary.get("swing_excluded_closed_positions", 0)),
          ("After-slippage win rate", _fmt_pct(swing.get("win_rate"))),
          ("After-slippage average", _fmt_pct(swing.get("avg_return"))),
          ("After-slippage median", _fmt_pct(swing.get("median_return"))),
          ("After-slippage profit factor", _fmt(swing.get("profit_factor"))),
          ("After-slippage max drawdown", _fmt_pct(swing.get("max_drawdown"))),
      ])}
    </section>
    <section>
      <h2>After Slippage</h2>
      {_metric_table([
          ("Win rate", _fmt_pct(slip.get("win_rate"))),
          ("Average return", _fmt_pct(slip.get("avg_return"))),
          ("Median return", _fmt_pct(slip.get("median_return"))),
          ("Profit factor", _fmt(slip.get("profit_factor"))),
          ("Max drawdown", _fmt_pct(slip.get("max_drawdown"))),
      ])}
    </section>
    <section>
      <h2>Baselines</h2>
      {_metric_table([
          ("SPY period return", _fmt_pct(bench.get("SPY"))),
          ("QQQ period return", _fmt_pct(bench.get("QQQ"))),
          ("Random baseline avg", _fmt_pct(baseline.get("avg_return"))),
          ("Random baseline win rate", _fmt_pct(baseline.get("win_rate"))),
      ])}
    </section>
  </div>

  <section>
    <h2>Independent Fixed-Session Forward Test</h2>
    <p class="muted">One thesis per asset, ticker, direction, and entry day. Shadow rows passed the current strategy before portfolio-level guardrails, allowing evidence to accumulate while execution stays blocked. Only completed market sessions are scored. Shares and futures use observed closes. Options prefer exact non-interpolated Robinhood trade bars and use a labeled constant-entry-IV proxy only when no exact target-date bar is cached. Neither source proves an Optedge fill.</p>
    {_metric_table([
        ("Broker-observed option outcomes", str(int(option_market_data.get("broker_observed_outcomes") or 0))),
        ("Modeled option outcomes", str(int(option_market_data.get("modeled_proxy_outcomes") or 0))),
        ("Observed option coverage", _fmt_pct(option_market_data.get("broker_observed_coverage_pct"))),
    ])}
    {_fixed_horizon_table(fixed_horizon)}
  </section>

  <section>
    <h2>Asset Breakdown</h2>
    {_asset_table(summary.get("assets", {}))}
  </section>

  <section>
    <h2>Equity Curve</h2>
    <p class="muted">{html.escape(str(equity.get("description") or "Equity curve uses normalized signal allocation."))}</p>
    <img src="equity_curve.png" alt="Optedge equity curve">
  </section>

  <div class="grid">
    <section><h2>Calls vs Puts</h2>{_bucket_table(summary["calls_vs_puts"])}</section>
    <section><h2>DTE Buckets</h2>{_bucket_table(summary["dte_buckets"])}</section>
    <section><h2>Spread Buckets</h2>{_bucket_table(summary["spread_buckets"])}</section>
    <section><h2>Confidence Buckets</h2>{_bucket_table(summary["confidence_buckets"])}</section>
  </div>

  <section>
    <h2>Factor IC - Executable Swing Sample</h2>
    <p class="muted">Reliability requires at least 100 executable outcomes across 10 distinct entry days. Watch/Skip, zero-size, guard-blocked, and same-scan churn rows remain auditable but cannot train the policy. Raw all-closure IC remains in validation_summary.json for comparison.</p>
    {_factor_ic_table(summary.get("factor_ic", []))}
  </section>
</main>
</body>
</html>
"""


def write_report(scope: str = "current_model", since: Optional[str] = None) -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    summary = build_summary(scope=scope, since=since)
    open_df, closed_raw = load_positions()
    closed = _closed_with_slippage(closed_raw)
    if scope != "all_time":
        cutoff = pd.to_datetime(summary.get("current_model_cutoff"), errors="coerce", utc=True)
        if cutoff is not None and not pd.isna(cutoff):
            closed = _filter_since(closed, cutoff)
    _write_equity_curve(closed, EQUITY_PNG)
    safe_summary = _json_safe(summary)
    SUMMARY_JSON.write_text(json.dumps(safe_summary, indent=2, allow_nan=False), encoding="utf-8")
    FACTOR_IC_JSON.write_text(
        json.dumps(_json_safe(summary.get("factor_ic", [])), indent=2, allow_nan=False),
        encoding="utf-8",
    )
    POSITION_AGING_JSON.write_text(
        json.dumps(_json_safe(summary.get("position_aging", {})), indent=2, allow_nan=False),
        encoding="utf-8",
    )
    try:
        from risk.research_guard import build_guard_report, save_guard_report

        guard = build_guard_report(validation_summary=safe_summary)
        save_guard_report(guard, path=DATA_DIR / "research_guard.json")
    except Exception:
        pass
    REPORT_HTML.write_text(render_html(summary), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Optedge validation report")
    parser.add_argument("--all-time", action="store_true",
                        help="Use every historical closed position instead of the current unarchived experiment")
    parser.add_argument("--since", default=None,
                        help="ISO date/time cutoff for primary validation metrics")
    args = parser.parse_args()
    summary = write_report(scope="all_time" if args.all_time else "current_model", since=args.since)
    print(f"Validation report: {REPORT_HTML}")
    print(f"Validation summary: {SUMMARY_JSON}")
    print(f"Equity curve: {EQUITY_PNG}")
    print(f"Factor IC summary: {FACTOR_IC_JSON}")
    print(f"Position aging summary: {POSITION_AGING_JSON}")
    if summary.get("warnings"):
        print("\nWarnings:")
        for warning in summary["warnings"]:
            print(f"  - {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
