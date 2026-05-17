"""Forward-test framework — Optedge v16.

Replays prior signals (options, shares, futures) against historical price
to compute realized PnL with explicit target/stop/time-stop logic, then
hands realized outcomes to the learning module for per-bucket weight refit.

Files written:
  data/forward_outcomes_<bucket>.parquet  — every replayed signal with outcome
  data/forward_test_status.parquet        — rolling 30/60/90d hit rate per bucket

Files read:
  logs/signals_*.parquet                  — options + shares signal log (existing)
  logs/futures_signals_*.parquet          — futures signal log (NEW in v16)

Replay logic per bucket:
  - options_call/put: BS-reprice at current spot (existing logic, kept).
  - shares_long: stop_pct/target_pct vs realized close path; whichever hits first.
  - futures_*: stop_price/target_price vs daily high/low path; first-hit wins.
  - Time stop: 30 days or contract expiry, whichever earlier.
"""
from __future__ import annotations
import glob
import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
import pandas as pd

import sys
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider
from utils import bs_price, safe_float, safe_int
from engines import learning

log = logging.getLogger("optedge.forward_test")
LOGS_DIR = ROOT / "logs"
DATA_DIR = ROOT / "data"


# ---------------------------------------------------------------------------
# Signal log loaders
# ---------------------------------------------------------------------------
def _load_options_logs() -> pd.DataFrame:
    """Concatenate options signal logs with mtime stamped as log_time."""
    files = sorted(glob.glob(str(LOGS_DIR / "signals_*.parquet")))
    if not files:
        return pd.DataFrame()
    dfs = []
    for f in files:
        try:
            df = pd.read_parquet(f)
            mtime = datetime.fromtimestamp(Path(f).stat().st_mtime, tz=timezone.utc)
            df["log_time"] = mtime
            df["_log_file"] = f
            dfs.append(df)
        except Exception as e:
            log.debug("skip %s: %s", f, e)
    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True).drop_duplicates(
        subset=[c for c in ("contract", "log_time") if c in dfs[0].columns], keep="last"
    )


def _load_futures_logs() -> pd.DataFrame:
    """Concatenate futures signal logs."""
    files = sorted(glob.glob(str(LOGS_DIR / "futures_signals_*.parquet")))
    if not files:
        return pd.DataFrame()
    dfs = []
    for f in files:
        try:
            df = pd.read_parquet(f)
            mtime = datetime.fromtimestamp(Path(f).stat().st_mtime, tz=timezone.utc)
            df["log_time"] = mtime
            df["_log_file"] = f
            dfs.append(df)
        except Exception as e:
            log.debug("skip %s: %s", f, e)
    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True)


def log_futures_signals(df: pd.DataFrame, asof: datetime) -> Optional[Path]:
    """Persist futures signals to logs/futures_signals_<asof>.parquet."""
    if df is None or df.empty:
        return None
    LOGS_DIR.mkdir(exist_ok=True)
    fp = LOGS_DIR / f"futures_signals_{asof.strftime('%Y%m%d_%H%M%S')}.parquet"
    try:
        df.to_parquet(fp, index=False)
    except Exception as e:
        log.warning("futures signal log failed: %s", e)
        return None
    log.info("logged %d futures signals to %s", len(df), fp.name)
    return fp


def log_shares_signals(df: pd.DataFrame, asof: datetime) -> Optional[Path]:
    """Persist shares signals to logs/shares_signals_<asof>.parquet (v16.1+).

    Captures: ticker, side='shares', spot (entry), share_score, confidence,
    stop_pct, target_pct, plus the factor z-cols so the learner can refit.

    v16.2 fix: if upstream df doesn't include `spot`, we fetch current price
    per ticker before logging. Without spot, replay can't compute realized PnL.
    """
    if df is None or df.empty:
        return None
    LOGS_DIR.mkdir(exist_ok=True)
    fp = LOGS_DIR / f"shares_signals_{asof.strftime('%Y%m%d_%H%M%S')}.parquet"

    # v16.2: ensure `spot` is present. Fusion's top_shares output may not
    # carry it; fetch retroactively so replay has a valid entry price.
    df_out = df.copy()
    if "spot" not in df_out.columns or df_out["spot"].isna().all():
        log.info("shares log: backfilling missing 'spot' from yfinance...")
        spots = []
        for ticker in df_out["ticker"]:
            try:
                h = data_provider.get_history(ticker, period="5d", cache_age=600)
                if h is not None and not h.empty and "Close" in h.columns:
                    spots.append(float(h["Close"].iloc[-1]))
                else:
                    spots.append(None)
            except Exception:
                spots.append(None)
        df_out["spot"] = spots

    keep_cols = [
        "ticker", "spot", "share_score", "confidence", "classification", "market_cap",
        "stop_pct", "target_pct", "suggested_dollars", "kelly_pct",
        "z_mispricing", "z_iv_rank", "z_skew", "z_sent", "z_fund", "z_insider",
        "z_macro", "z_news", "z_earnings", "z_value",
        "sentiment_delta", "fund_score", "insider_score", "news_delta",
        "n_24h", "top_headline", "reasoning", "risks",
        "pred_stock_return_pct",
    ]
    keep = [c for c in keep_cols if c in df_out.columns]
    out = df_out[keep].copy()
    out["side"] = "shares"
    try:
        out.to_parquet(fp, index=False)
    except Exception as e:
        log.warning("shares signal log failed: %s", e)
        return None
    n_with_spot = int(out["spot"].notna().sum()) if "spot" in out.columns else 0
    log.info("logged %d shares signals (%d with spot) to %s",
             len(out), n_with_spot, fp.name)
    return fp


def _load_shares_logs() -> pd.DataFrame:
    """Concatenate shares signal logs."""
    files = sorted(glob.glob(str(LOGS_DIR / "shares_signals_*.parquet")))
    if not files:
        return pd.DataFrame()
    dfs = []
    for f in files:
        try:
            df = pd.read_parquet(f)
            mtime = datetime.fromtimestamp(Path(f).stat().st_mtime, tz=timezone.utc)
            df["log_time"] = mtime
            df["_log_file"] = f
            dfs.append(df)
        except Exception as e:
            log.debug("skip %s: %s", f, e)
    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True)


# ---------------------------------------------------------------------------
# Per-signal replay
# ---------------------------------------------------------------------------
def _replay_option_signal(row: pd.Series) -> Optional[Dict[str, Any]]:
    """Reprice an options signal using current spot + same IV (BS)."""
    ticker = row.get("ticker")
    if not ticker:
        return None
    log_time = row.get("log_time")
    days_old = None
    if log_time is not None:
        try:
            days_old = (datetime.now(timezone.utc) - pd.to_datetime(log_time, utc=True)).total_seconds() / 86400
        except Exception:
            pass

    hist = data_provider.get_history(ticker, period="5d")
    if hist is None or hist.empty:
        return None
    spot_now = float(hist["Close"].iloc[-1])

    is_call = row.get("side") == "call"
    bucket = "options_call" if is_call else "options_put"

    K = safe_float(row.get("strike"))
    iv = safe_float(row.get("iv_market"))
    if K <= 0 or iv <= 0:
        return None

    exp_str = row.get("expiry")
    if not exp_str:
        return None
    try:
        exp = datetime.strptime(str(exp_str), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return None
    T_days = (exp - datetime.now(timezone.utc)).total_seconds() / 86400
    if T_days <= 0:
        new_price = max(0.0, (spot_now - K) if is_call else (K - spot_now))
        outcome = "expired"
    else:
        new_price = bs_price(spot_now, K, T_days / 365.25, 0.045, iv, 0.0, call=is_call)
        outcome = "open"

    entry_mid = safe_float(row.get("mid"))
    if entry_mid <= 0:
        return None

    if row.get("is_buy", True):
        pnl_pct = (new_price - entry_mid) / entry_mid
    else:
        pnl_pct = (entry_mid - new_price) / entry_mid
    realized_dollars = pnl_pct * entry_mid * 100

    # Apply 50% stop / 100% target if option price decisively crossed (rough — we
    # don't have intraday option prices, so this is a *current price* check)
    if pnl_pct <= -0.50:
        outcome = "stop"
    elif pnl_pct >= 1.00:
        outcome = "target"

    base = {
        "bucket": bucket,
        "ticker": ticker,
        "log_time": log_time,
        "days_old": round(days_old, 1) if days_old is not None else None,
        "entry_price": round(entry_mid, 3),
        "current_price": round(new_price, 3),
        "pnl_pct": round(pnl_pct, 4),
        "realized_dollars": round(realized_dollars, 2),
        "outcome": outcome,
    }
    # Carry the factor columns through so the learner can refit
    for col in row.index:
        if col.startswith("z_") or col.startswith("factor_"):
            base[col] = row[col]
    # Also carry confidence + side
    if "confidence" in row.index:
        base["confidence"] = safe_int(row.get("confidence"))
    if "side" in row.index:
        base["signal_side"] = row.get("side")
    return base


def _replay_share_signal(row: pd.Series) -> Optional[Dict[str, Any]]:
    """Replay a shares signal: did stop or target hit first along realized path?"""
    ticker = row.get("ticker")
    if not ticker or row.get("side") in ("call", "put"):
        return None
    log_time = row.get("log_time")
    if log_time is None:
        return None

    stop_pct = safe_float(row.get("stop_pct")) or -0.08
    target_pct = safe_float(row.get("target_pct")) or 0.20

    # Pull history from log_time to now (max 60d)
    days_old = (datetime.now(timezone.utc) - pd.to_datetime(log_time, utc=True)).total_seconds() / 86400
    if days_old < 0.05:   # <~1 hour — too fresh to evaluate
        return None
    period = "3mo" if days_old > 30 else "1mo"
    h = data_provider.get_history(ticker, period=period)
    if h is None or h.empty:
        return None

    # Resolve entry. Prefer the logged 'spot'; fall back to the close on log_date
    # for legacy shares logs that didn't carry spot through (v16.0/v16.1 schema).
    entry = safe_float(row.get("spot"))
    if entry <= 0:
        try:
            log_date = pd.to_datetime(log_time).date()
            idx_dates = pd.Series(h.index).apply(lambda x: x.date() if hasattr(x, "date") else pd.to_datetime(x).date())
            on_or_after = h[idx_dates.values >= log_date]
            if not on_or_after.empty:
                entry = float(on_or_after["Close"].iloc[0])
        except Exception:
            return None
    if entry <= 0:
        return None

    # Slice to dates >= log_time. Compare on date to dodge TZ headaches
    # between UTC log timestamps and yfinance's local-time index.
    try:
        log_date = pd.to_datetime(log_time).date()
        idx_dates = pd.Series(h.index).apply(lambda x: x.date() if hasattr(x, "date") else pd.to_datetime(x).date())
        mask = idx_dates.values >= log_date
        h_after = h[mask]
    except Exception:
        h_after = h

    if h_after.empty:
        return None

    stop_price = entry * (1 + stop_pct)
    target_price = entry * (1 + target_pct)
    outcome = "open"
    exit_price = float(h_after["Close"].iloc[-1])
    holding_days = float(min(30, len(h_after)))

    if "Low" in h_after.columns and "High" in h_after.columns:
        for i, (_, bar) in enumerate(h_after.iterrows()):
            lo = float(bar["Low"])
            hi = float(bar["High"])
            if lo <= stop_price:
                outcome = "stop"
                exit_price = stop_price
                holding_days = float(i + 1)
                break
            if hi >= target_price:
                outcome = "target"
                exit_price = target_price
                holding_days = float(i + 1)
                break
        else:
            if len(h_after) >= 30:
                outcome = "time_stop"
                exit_price = float(h_after["Close"].iloc[min(29, len(h_after)-1)])
                holding_days = 30.0

    pnl_pct = (exit_price / entry) - 1
    realized_dollars = pnl_pct * entry  # per share

    base = {
        "bucket": "shares_long",
        "ticker": ticker,
        "log_time": log_time,
        "days_old": round(days_old, 1),
        "entry_price": round(entry, 3),
        "exit_price": round(exit_price, 3),
        "pnl_pct": round(pnl_pct, 4),
        "realized_dollars": round(realized_dollars, 2),
        "outcome": outcome,
        "holding_days": holding_days,
    }
    for col in row.index:
        if col.startswith("z_") or col.startswith("factor_"):
            base[col] = row[col]
    if "confidence" in row.index:
        base["confidence"] = safe_int(row.get("confidence"))
    return base


def _replay_futures_signal(row: pd.Series) -> Optional[Dict[str, Any]]:
    """Replay a futures signal: did stop or target hit first?"""
    sym = row.get("symbol")
    if not sym:
        return None
    log_time = row.get("log_time")
    if log_time is None:
        return None

    entry = safe_float(row.get("entry"))
    if entry <= 0:
        return None
    stop_price = safe_float(row.get("stop_price"))
    target_price = safe_float(row.get("target_price"))
    is_long = bool(row.get("is_long"))
    n_contracts = safe_int(row.get("n_contracts"))
    point_value = safe_float(row.get("point_value")) or 1.0
    bucket = row.get("bucket") or learning.bucket_for_futures_row(row.to_dict())

    days_old = (datetime.now(timezone.utc) - pd.to_datetime(log_time, utc=True)).total_seconds() / 86400
    if days_old < 0.05:   # <~1 hour — too fresh to evaluate
        return None
    period = "3mo" if days_old > 30 else "1mo"
    h = data_provider.get_history(sym, period=period)
    if h is None or h.empty:
        return None

    # Compare on date to dodge TZ mismatches (UTC log vs yfinance local index).
    try:
        log_date = pd.to_datetime(log_time).date()
        idx_dates = pd.Series(h.index).apply(lambda x: x.date() if hasattr(x, "date") else pd.to_datetime(x).date())
        mask = idx_dates.values >= log_date
        h_after = h[mask]
    except Exception:
        h_after = h

    if h_after.empty:
        return None

    outcome = "open"
    exit_price = float(h_after["Close"].iloc[-1])
    holding_days = float(min(30, len(h_after)))

    if "Low" in h_after.columns and "High" in h_after.columns and stop_price > 0 and target_price > 0:
        for i, (_, bar) in enumerate(h_after.iterrows()):
            lo = float(bar["Low"])
            hi = float(bar["High"])
            if is_long:
                if lo <= stop_price:
                    outcome = "stop"; exit_price = stop_price; holding_days = float(i+1); break
                if hi >= target_price:
                    outcome = "target"; exit_price = target_price; holding_days = float(i+1); break
            else:
                if hi >= stop_price:
                    outcome = "stop"; exit_price = stop_price; holding_days = float(i+1); break
                if lo <= target_price:
                    outcome = "target"; exit_price = target_price; holding_days = float(i+1); break
        else:
            if len(h_after) >= 30:
                outcome = "time_stop"
                exit_price = float(h_after["Close"].iloc[min(29, len(h_after)-1)])
                holding_days = 30.0

    pnl_pts = (exit_price - entry) if is_long else (entry - exit_price)
    pnl_pct = pnl_pts / entry if entry > 0 else 0
    realized_dollars = pnl_pts * point_value * n_contracts

    base = {
        "bucket": bucket,
        "ticker": sym,
        "symbol": sym,
        "log_time": log_time,
        "days_old": round(days_old, 1),
        "entry_price": round(entry, 4),
        "exit_price": round(exit_price, 4),
        "pnl_pts": round(pnl_pts, 4),
        "pnl_pct": round(pnl_pct, 4),
        "realized_dollars": round(realized_dollars, 2),
        "outcome": outcome,
        "holding_days": holding_days,
        "n_contracts": n_contracts,
    }
    for col in row.index:
        if col.startswith("factor_"):
            base[col] = row[col]
    return base


# ---------------------------------------------------------------------------
# Replay orchestration per bucket
# ---------------------------------------------------------------------------
def replay_outcomes(max_workers: int = 8, lookback_days: int = 60) -> Dict[str, pd.DataFrame]:
    """Replay every logged signal across all buckets. Returns dict[bucket -> outcomes_df]."""
    out: Dict[str, List[Dict[str, Any]]] = {b: [] for b in learning.BUCKET_KEYS}

    # Options + shares share the existing signals_*.parquet log
    opts_log = _load_options_logs()
    if not opts_log.empty:
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        try:
            opts_log = opts_log[pd.to_datetime(opts_log["log_time"], utc=True) >= cutoff]
        except Exception:
            pass

        # Replay options
        opts_only = opts_log[opts_log.get("side", "").isin(["call", "put"])] if "side" in opts_log.columns else opts_log
        if not opts_only.empty:
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futs = [ex.submit(_replay_option_signal, r) for _, r in opts_only.iterrows()]
                for f in as_completed(futs):
                    try:
                        r = f.result()
                        if r and r.get("pnl_pct") is not None:
                            out[r["bucket"]].append(r)
                    except Exception as e:
                        log.debug("opts replay fail: %s", e)

        # Replay shares (legacy fallback — pre-v16.1 signals_*.parquet that weren't options)
        sh_only = opts_log[~opts_log.get("side", "").isin(["call","put"])] if "side" in opts_log.columns else pd.DataFrame()
        if not sh_only.empty:
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futs = [ex.submit(_replay_share_signal, r) for _, r in sh_only.iterrows()]
                for f in as_completed(futs):
                    try:
                        r = f.result()
                        if r and r.get("pnl_pct") is not None:
                            out["shares_long"].append(r)
                    except Exception as e:
                        log.debug("shares replay fail: %s", e)

    # v16.1: dedicated shares-signal log
    sh_log = _load_shares_logs()
    if not sh_log.empty:
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        try:
            sh_log = sh_log[pd.to_datetime(sh_log["log_time"], utc=True) >= cutoff]
        except Exception:
            pass
        if not sh_log.empty:
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futs = [ex.submit(_replay_share_signal, r) for _, r in sh_log.iterrows()]
                for f in as_completed(futs):
                    try:
                        r = f.result()
                        if r and r.get("pnl_pct") is not None:
                            out["shares_long"].append(r)
                    except Exception as e:
                        log.debug("shares replay fail: %s", e)

    # Futures: from dedicated log
    fut_log = _load_futures_logs()
    if not fut_log.empty:
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        try:
            fut_log = fut_log[pd.to_datetime(fut_log["log_time"], utc=True) >= cutoff]
        except Exception:
            pass
        if not fut_log.empty:
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futs = [ex.submit(_replay_futures_signal, r) for _, r in fut_log.iterrows()]
                for f in as_completed(futs):
                    try:
                        r = f.result()
                        if r:
                            out[r["bucket"]].append(r)
                    except Exception as e:
                        log.debug("futures replay fail: %s", e)

    # Convert each bucket's list to a DataFrame, persist, return
    DATA_DIR.mkdir(exist_ok=True)
    bucket_dfs: Dict[str, pd.DataFrame] = {}
    for bucket, rows in out.items():
        df = pd.DataFrame(rows) if rows else pd.DataFrame()
        bucket_dfs[bucket] = df
        if not df.empty:
            try:
                df.to_parquet(DATA_DIR / f"forward_outcomes_{bucket}.parquet", index=False)
            except Exception as e:
                log.debug("save outcomes %s: %s", bucket, e)
    return bucket_dfs


# ---------------------------------------------------------------------------
# Rolling stats per bucket
# ---------------------------------------------------------------------------
def compute_rolling_stats(bucket_dfs: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Per-bucket rolling 30/60/90d hit rate + realized PnL."""
    rows = []
    now = datetime.now(timezone.utc)
    for bucket, df in bucket_dfs.items():
        if df is None or df.empty:
            rows.append({"bucket": bucket, "n_30d": 0, "n_60d": 0, "n_90d": 0,
                         "win_rate_30d": None, "win_rate_60d": None, "win_rate_90d": None,
                         "pnl_30d": 0.0, "pnl_60d": 0.0, "pnl_90d": 0.0,
                         "avg_pnl_pct_30d": None, "avg_pnl_pct_60d": None, "avg_pnl_pct_90d": None})
            continue
        df = df.copy()
        df["log_time"] = pd.to_datetime(df["log_time"], utc=True, errors="coerce")
        bucket_row = {"bucket": bucket}
        for d in (30, 60, 90):
            cutoff = now - timedelta(days=d)
            sub = df[df["log_time"] >= cutoff]
            n = len(sub)
            bucket_row[f"n_{d}d"] = n
            if n > 0 and "pnl_pct" in sub.columns:
                wins = (sub["pnl_pct"] > 0).sum()
                bucket_row[f"win_rate_{d}d"] = round(wins / n, 4)
                bucket_row[f"avg_pnl_pct_{d}d"] = round(float(sub["pnl_pct"].mean()), 4)
                if "realized_dollars" in sub.columns:
                    bucket_row[f"pnl_{d}d"] = round(float(sub["realized_dollars"].sum()), 2)
                else:
                    bucket_row[f"pnl_{d}d"] = 0.0
            else:
                bucket_row[f"win_rate_{d}d"] = None
                bucket_row[f"avg_pnl_pct_{d}d"] = None
                bucket_row[f"pnl_{d}d"] = 0.0
        rows.append(bucket_row)

    out_df = pd.DataFrame(rows)
    DATA_DIR.mkdir(exist_ok=True)
    try:
        out_df.to_parquet(DATA_DIR / "forward_test_status.parquet", index=False)
    except Exception as e:
        log.debug("save status: %s", e)
    return out_df


# ---------------------------------------------------------------------------
# Per-bucket weight refit — drives self-learning
# ---------------------------------------------------------------------------
def refit_all_buckets(bucket_dfs: Dict[str, pd.DataFrame],
                       min_lasso: int = 50, min_ic: int = 20) -> Dict[str, Dict[str, Any]]:
    """For each bucket with enough realized outcomes, refit weights via the learning module."""
    results: Dict[str, Dict[str, Any]] = {}
    for bucket in learning.BUCKET_KEYS:
        df = bucket_dfs.get(bucket)
        if df is None or df.empty:
            results[bucket] = {"mode": "no_data", "n": 0}
            continue

        # Pull factor columns relevant to this bucket
        priors = learning.get_factor_priors(bucket)
        factor_cols = [c for c in df.columns if c.startswith("factor_") and c[7:] in priors]
        # Legacy z-cols
        z_cols = [c for c in df.columns if c.startswith("z_")]

        # Map z_cols → factor_*  (only when factor_ form not already present)
        # The legacy options/shares logs use z_mispricing etc. — we copy them with factor_ prefix
        zcol_to_factor = {
            "z_mispricing": "mispricing", "z_iv_rank": "iv_rank", "z_skew": "skew",
            "z_sent": "sentiment_d", "z_fund": "fundamentals", "z_insider": "insider",
            "z_macro": "macro", "z_news": "news", "z_earnings": "earnings", "z_value": "value",
        }
        df = df.copy()
        for zcol, fname in zcol_to_factor.items():
            target_col = f"factor_{fname}"
            if zcol in df.columns and target_col not in df.columns:
                df[target_col] = df[zcol]

        factor_cols = [f"factor_{k}" for k in priors.keys() if f"factor_{k}" in df.columns]
        if not factor_cols:
            results[bucket] = {"mode": "no_factors", "n": len(df)}
            continue

        # Build factor matrix renamed to the factor name (no factor_ prefix)
        fmat = df[factor_cols].copy()
        fmat.columns = [c.replace("factor_", "") for c in fmat.columns]

        pnl_col = "realized_dollars" if "realized_dollars" in df.columns else "pnl_pct"
        pnl = pd.to_numeric(df[pnl_col], errors="coerce").fillna(0.0)

        result = learning.refit_bucket(bucket, fmat, pnl,
                                        min_lasso=min_lasso, min_ic=min_ic)
        results[bucket] = result or {"mode": "no_change", "n": len(df)}
    return results


# ---------------------------------------------------------------------------
# Public orchestrator — wired into run.py post-cycle
# ---------------------------------------------------------------------------
def run_forward_cycle(min_lasso: int = 50, min_ic: int = 20,
                       lookback_days: int = 60) -> Dict[str, Any]:
    """One end-to-end pass: replay → rolling stats → refit per bucket."""
    log.info("forward-test cycle: replaying logs (lookback=%dd)", lookback_days)
    bucket_dfs = replay_outcomes(lookback_days=lookback_days)
    n_total = sum(len(df) for df in bucket_dfs.values())
    log.info("forward-test: %d outcomes across %d buckets", n_total,
             sum(1 for df in bucket_dfs.values() if not df.empty))

    status = compute_rolling_stats(bucket_dfs)
    refit_results = refit_all_buckets(bucket_dfs, min_lasso=min_lasso, min_ic=min_ic)

    # Summarize for caller
    summary = {
        "n_total_outcomes": n_total,
        "buckets_with_data": sum(1 for df in bucket_dfs.values() if not df.empty),
        "refit_results": refit_results,
        "status_path": str(DATA_DIR / "forward_test_status.parquet"),
    }
    return summary


# ---------------------------------------------------------------------------
# Legacy compatibility — keeps the old `--forward` CLI working
# ---------------------------------------------------------------------------
def run_forward_test_legacy() -> Dict[str, Any]:
    """Drop-in replacement for backtest.forward.run_forward_test().

    Reproduces the v15 dashboard summary shape (overall / by_confidence / by_type).
    """
    bucket_dfs = replay_outcomes()
    all_rows = []
    for b, df in bucket_dfs.items():
        if df is None or df.empty:
            continue
        df = df.copy()
        df["bucket"] = b
        all_rows.append(df)
    if not all_rows:
        return {"signals": pd.DataFrame(), "summary": pd.DataFrame()}
    pnl = pd.concat(all_rows, ignore_index=True)
    if pnl.empty or "pnl_pct" not in pnl.columns:
        return {"signals": pnl, "summary": pd.DataFrame()}

    overall = {
        "n_signals": len(pnl),
        "win_rate": float((pnl["pnl_pct"] > 0).mean()),
        "avg_pnl_pct": float(pnl["pnl_pct"].mean()),
        "median_pnl_pct": float(pnl["pnl_pct"].median()),
        "best": float(pnl["pnl_pct"].max()),
        "worst": float(pnl["pnl_pct"].min()),
    }

    buckets = []
    if "confidence" in pnl.columns:
        for lo, hi, lab in [(0, 55, "low (<55)"), (55, 70, "med (55-70)"), (70, 200, "high (≥70)")]:
            sub = pnl[(pnl["confidence"] >= lo) & (pnl["confidence"] < hi)]
            if sub.empty:
                continue
            buckets.append({
                "bucket": lab,
                "n": len(sub),
                "win_rate": float((sub["pnl_pct"] > 0).mean()),
                "avg_pnl": float(sub["pnl_pct"].mean()),
                "median_pnl": float(sub["pnl_pct"].median()),
            })

    by_type = []
    for t in ("options_call", "options_put", "shares_long",
              "futures_equity", "futures_treasury", "futures_metal",
              "futures_energy", "futures_crypto", "futures_currency", "futures_agri"):
        sub = pnl[pnl["bucket"] == t]
        if sub.empty:
            continue
        by_type.append({
            "type": t,
            "n": len(sub),
            "win_rate": float((sub["pnl_pct"] > 0).mean()),
            "avg_pnl": float(sub["pnl_pct"].mean()),
        })

    return {
        "signals": pnl,
        "overall": overall,
        "by_confidence": pd.DataFrame(buckets),
        "by_type": pd.DataFrame(by_type),
    }
