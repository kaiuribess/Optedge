"""Forward testing — replay all logged signals with current prices.

Reads three log file types:
  - signals_*.parquet           (options: calls + puts)
  - shares_signals_*.parquet    (small-cap share buys)
  - futures_signals_*.parquet   (futures contracts)

Computes appropriate P&L per asset type, dedupes by (contract|ticker|symbol, entry_time),
and stratifies by confidence bucket + asset type + per-side (call/put).

Backward-compatible: if an old log lacks `entry_time`, file mtime is used as fallback.

Run: `python run.py --forward`
"""
from __future__ import annotations
import glob
import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, List

import numpy as np
import pandas as pd

import sys
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider
from utils import bs_price, safe_float, safe_int

log = logging.getLogger("optedge.forward")
LOGS_DIR = ROOT / "logs"
FEATURE_COLS = [
    "z_mispricing", "z_iv_rank", "z_skew", "z_sent", "z_fund", "z_insider",
    "z_macro", "z_news", "z_earnings", "z_value", "z_congress", "z_social",
    "z_analyst", "pred_stock_return_pct", "pred_option_return_pct", "ev_pct",
    "kelly_pct", "suggested_contracts", "actual_dollars",
    "mispricing_pct", "net_edge_pct", "buyer_edge_pct", "seller_edge_pct",
    "pricing_direction", "pricing_edge_ok", "trade_gate_reason",
]


# -------- Loading helpers ---------------------------------------------
def _load_logs(prefix: str) -> pd.DataFrame:
    """Concatenate every {prefix}_*.parquet log, ensuring entry_time exists."""
    pattern = "signals_*.parquet" if prefix == "options" else f"{prefix}_signals_*.parquet"
    files = sorted(glob.glob(str(LOGS_DIR / pattern)))
    # For options, skip files that match the shares/futures patterns
    if prefix == "options":
        files = [f for f in files if "shares_signals" not in f and "futures_signals" not in f]
    if not files:
        return pd.DataFrame()
    dfs = []
    for f in files:
        try:
            df = pd.read_parquet(f)
            if df.empty:
                continue
            if "entry_time" not in df.columns or df["entry_time"].isna().all():
                mtime = datetime.fromtimestamp(Path(f).stat().st_mtime, tz=timezone.utc)
                df["entry_time"] = mtime.isoformat()
            df["_log_file"] = Path(f).name
            dfs.append(df)
        except Exception as e:
            log.debug("skip %s: %s", f, e)
    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True)


def _load_all_logs() -> pd.DataFrame:
    """Load every asset log into one normalized research table."""
    frames = []
    for prefix, asset in (("options", "option"), ("shares", "share"), ("futures", "futures")):
        frame = _load_logs(prefix)
        if frame.empty:
            continue
        frame = frame.copy()
        frame["asset"] = asset
        frames.append(frame)
    return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()


def _carry_features(row: pd.Series, out: Dict[str, Any]) -> Dict[str, Any]:
    """Keep model feature columns on re-priced rows so retraining can learn."""
    for col in FEATURE_COLS:
        if col in row.index:
            out[col] = row.get(col)
    return out


def _slippage_adjusted_pnl(row: pd.Series, pnl_pct: float, asset: str) -> float:
    """Conservative paper-trade P&L after estimated round-trip fill friction."""
    if asset != "option":
        return pnl_pct
    try:
        from config import FILL_SLIPPAGE_PCT
        slippage = float(FILL_SLIPPAGE_PCT)
    except Exception:
        slippage = 0.04
    return pnl_pct - slippage


def _truthy(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "long", "buy"}:
        return True
    if text in {"0", "false", "no", "n", "short", "sell"}:
        return False
    return None


def _age_days(entry_time: Any) -> Optional[float]:
    entry = pd.to_datetime(entry_time, errors="coerce", utc=True)
    if pd.isna(entry):
        return None
    return max(0.0, (pd.Timestamp.now(tz="UTC") - entry).total_seconds() / 86400.0)


# -------- Re-pricing per asset type -----------------------------------
def _price_option_now(row: pd.Series, spot_now: float) -> Optional[float]:
    """Re-price an option contract at current spot, holding entry IV constant."""
    try:
        K = safe_float(row.get("strike"))
        is_call = row.get("side") == "call"
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
            return max(0.0, (spot_now - K) if is_call else (K - spot_now))
        T = T_days / 365.25
        return bs_price(spot_now, K, T, 0.045, iv, 0.0, call=is_call)
    except Exception:
        return None


def _process_option(row: pd.Series,
                    histories: Optional[Dict[str, pd.DataFrame]] = None) -> Dict[str, Any]:
    ticker = row.get("ticker")
    if not ticker:
        return {"ticker": None, "_drop_reason": "no_ticker"}
    entry_mid = safe_float(row.get("mid"))
    if entry_mid <= 0:
        return {"ticker": ticker, "_drop_reason": "no_entry_mid"}
    hist = (histories or {}).get(str(ticker).upper())
    if hist is None:
        hist = data_provider.get_history(ticker, period="1y")
    if hist is None or hist.empty:
        return {"ticker": ticker, "_drop_reason": "no_spot"}
    spot_now = float(hist["Close"].iloc[-1])
    new_price = _price_option_now(row, spot_now)
    if new_price is None:
        new_price = 0.0
    is_buy = bool(row.get("is_buy", True))
    pnl_pct = ((new_price - entry_mid) / entry_mid) if is_buy else ((entry_mid - new_price) / entry_mid)
    out = {
        "asset": "option",
        "ticker": ticker,
        "contract": row.get("contract"),
        "side": row.get("side"),                          # call / put
        "confidence": safe_int(row.get("confidence")),
        "entry_time": row.get("entry_time"),
        "age_days": _age_days(row.get("entry_time")),
        "entry_price": round(entry_mid, 3),
        "current_price": round(float(new_price), 3),
        "spot_now": round(spot_now, 3),
        "pnl_pct": round(pnl_pct, 4),
        "pnl_pct_after_slippage": round(_slippage_adjusted_pnl(row, pnl_pct, "option"), 4),
    }
    return _carry_features(row, out)


def _process_share(row: pd.Series,
                   histories: Optional[Dict[str, pd.DataFrame]] = None) -> Dict[str, Any]:
    ticker = row.get("ticker")
    if not ticker:
        return {"ticker": None, "_drop_reason": "no_ticker"}
    entry_spot = safe_float(row.get("spot") or row.get("entry_price") or row.get("current_price"))
    if entry_spot <= 0:
        return {"ticker": ticker, "_drop_reason": "no_entry_spot"}
    hist = (histories or {}).get(str(ticker).upper())
    if hist is None:
        hist = data_provider.get_history(ticker, period="1y")
    if hist is None or hist.empty:
        return {"ticker": ticker, "_drop_reason": "no_spot"}
    spot_now = float(hist["Close"].iloc[-1])
    pnl_pct = (spot_now / entry_spot) - 1
    out = {
        "asset": "share",
        "ticker": ticker,
        "contract": ticker,
        "side": "shares",
        "confidence": safe_int(row.get("confidence")),
        "entry_time": row.get("entry_time"),
        "age_days": _age_days(row.get("entry_time")),
        "entry_price": round(entry_spot, 3),
        "current_price": round(spot_now, 3),
        "spot_now": round(spot_now, 3),
        "pnl_pct": round(pnl_pct, 4),
        "pnl_pct_after_slippage": round(_slippage_adjusted_pnl(row, pnl_pct, "share"), 4),
        "classification": row.get("classification"),
    }
    return _carry_features(row, out)


def _process_future(row: pd.Series,
                    histories: Optional[Dict[str, pd.DataFrame]] = None) -> Dict[str, Any]:
    """Futures P&L: point-based, scaled by point_value × n_contracts."""
    symbol = row.get("symbol")
    if not symbol:
        return {"_drop_reason": "no_symbol"}
    entry = safe_float(row.get("entry") or row.get("entry_price") or row.get("spot"))
    direction = str(row.get("direction") or row.get("side") or "").lower()
    is_long = _truthy(row.get("is_long"))
    if is_long is None:
        is_long = direction in {"long", "buy", "long futures"}
    point_value = safe_float(row.get("point_value"))
    n_contracts = safe_int(row.get("n_contracts") or row.get("suggested_contracts") or row.get("contracts"))
    if entry <= 0 or point_value <= 0:
        return {"_drop_reason": "no_entry_or_pointvalue"}
    # Prefer the actual continuous futures series; use the ETF only as a fallback.
    hist = (histories or {}).get(str(symbol).upper())
    if hist is None:
        hist = data_provider.get_history(symbol, period="1y")
    pricing_method = "observed_continuous_futures_close"
    current_est = (
        float(hist["Close"].dropna().iloc[-1])
        if hist is not None and not hist.empty and not hist["Close"].dropna().empty
        else 0.0
    )
    if current_est <= 0:
        proxy = str(row.get("etf") or "").strip()
        proxy_hist = (histories or {}).get(proxy.upper()) if proxy else pd.DataFrame()
        if proxy and proxy_hist is None:
            proxy_hist = data_provider.get_history(proxy, period="1y")
        if proxy_hist is None or proxy_hist.empty:
            return {"_drop_reason": "no_futures_or_proxy_spot"}
        entry_time = pd.to_datetime(row.get("entry_time"), errors="coerce", utc=True)
        proxy_dates = pd.to_datetime(proxy_hist.index, errors="coerce", utc=True)
        valid = ~proxy_dates.isna()
        proxy_hist = proxy_hist.loc[valid]
        proxy_dates = proxy_dates[valid]
        if not pd.isna(entry_time):
            eligible = proxy_hist[proxy_dates.date >= entry_time.date()]
        else:
            eligible = proxy_hist
        closes = eligible["Close"].dropna()
        if len(closes) < 2:
            return {"_drop_reason": "no_proxy_entry_history"}
        proxy_return = float(closes.iloc[-1]) / float(closes.iloc[0]) - 1.0
        current_est = entry * (1.0 + proxy_return)
        pricing_method = "etf_proxy_since_entry"
    point_move = (current_est - entry) if is_long else (entry - current_est)
    dollar_pnl = point_move * point_value * max(0, n_contracts)
    pnl_pct = point_move / entry
    out = {
        "asset": "futures",
        "ticker": symbol,
        "contract": symbol,
        "side": "futures",
        "confidence": safe_int(row.get("futures_score") * 10 if row.get("futures_score") else 50),
        "entry_time": row.get("entry_time"),
        "age_days": _age_days(row.get("entry_time")),
        "entry_price": round(entry, 3),
        "current_price": round(current_est, 3),
        "pnl_pct": round(pnl_pct, 4),
        "pnl_pct_after_slippage": round(_slippage_adjusted_pnl(row, pnl_pct, "futures"), 4),
        "dollar_pnl": round(dollar_pnl, 2),
        "bucket": row.get("bucket"),
        "valuation_method": pricing_method,
    }
    return _carry_features(row, out)


# -------- Stats helpers ----------------------------------------------
def _bucket_by_confidence(pnl: pd.DataFrame) -> pd.DataFrame:
    out = []
    for lo, hi, lab in [(0, 55, "low (<55)"), (55, 70, "med (55-70)"), (70, 200, "high (≥70)")]:
        sub = pnl[(pnl["confidence"] >= lo) & (pnl["confidence"] < hi)]
        if sub.empty:
            continue
        out.append({
            "bucket": lab,
            "n": len(sub),
            "win_rate": float((sub["pnl_pct"] > 0).mean()),
            "avg_pnl": float(sub["pnl_pct"].mean()),
            "median_pnl": float(sub["pnl_pct"].median()),
        })
    return pd.DataFrame(out)


def _bucket_by_side(pnl: pd.DataFrame, sides: List[str]) -> pd.DataFrame:
    out = []
    for s in sides:
        sub = pnl[pnl["side"] == s]
        if sub.empty:
            continue
        out.append({
            "type": s,
            "n": len(sub),
            "win_rate": float((sub["pnl_pct"] > 0).mean()),
            "avg_pnl": float(sub["pnl_pct"].mean()),
            "median_pnl": float(sub["pnl_pct"].median()),
        })
    return pd.DataFrame(out)


def _risk_metrics(pnl: pd.DataFrame) -> Dict[str, float]:
    """Sharpe-style metrics on the per-signal P&L series."""
    if pnl.empty:
        return {}
    r = pnl["pnl_pct"].dropna()
    if len(r) < 5:
        return {}
    mean = r.mean()
    std = r.std() or 1e-6
    downside = r[r < 0].std() or 1e-6
    # Treat each signal as one independent bet; "sharpe" = mean/std × sqrt(N)
    sharpe = float(mean / std * math.sqrt(len(r)))
    sortino = float(mean / downside * math.sqrt(len(r)))
    # Max drawdown if we treated signals chronologically (proxy)
    eq = (1 + r.sort_index()).cumprod()
    peak = eq.cummax()
    dd = (eq / peak - 1).min()
    return {
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "max_drawdown_pct": round(float(dd), 4),
        "n": len(r),
    }


def _prefetch_current_histories(opt_df: pd.DataFrame, sh_df: pd.DataFrame,
                                fut_df: pd.DataFrame, max_workers: int) -> Dict[str, pd.DataFrame]:
    """Fetch one daily history per unique symbol for current-mark telemetry."""
    symbols = set()
    for frame, column in ((opt_df, "ticker"), (sh_df, "ticker"), (fut_df, "symbol"),
                          (fut_df, "etf")):
        if frame is None or frame.empty or column not in frame.columns:
            continue
        values = frame[column].dropna().astype(str).str.strip().str.upper()
        symbols.update(value for value in values if value and value not in {"NAN", "NONE"})
    histories: Dict[str, pd.DataFrame] = {}

    def fetch(symbol: str):
        return symbol, data_provider.get_history(symbol, period="1y")

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        jobs = {executor.submit(fetch, symbol): symbol for symbol in sorted(symbols)}
        for future in as_completed(jobs):
            symbol = jobs[future]
            try:
                key, history = future.result()
                histories[key] = history if history is not None else pd.DataFrame()
            except Exception as exc:
                log.debug("forward history prefetch failed %s: %s", symbol, exc)
                histories[symbol] = pd.DataFrame()
    return histories


# -------- Public --------------------------------------------------
def run_forward_test(max_workers: int = 8,
                     include_fixed_horizon: bool = True) -> Dict[str, Any]:
    """Re-price every logged signal across options/shares/futures.

    Returns:
      {
        "signals": full per-signal DataFrame with pnl_pct,
        "overall": dict of win_rate, avg_pnl, etc.,
        "by_confidence": DataFrame stratified by conf bucket,
        "by_type": DataFrame stratified by side (call/put/shares/futures),
        "by_asset": DataFrame stratified by asset class (option/share/futures),
        "risk": dict with sharpe + sortino + drawdown,
        "dropped": {reason: count} for surfacing why signals weren't re-priced,
      }
    """
    # Load all 3 log types
    opt_df = _load_logs("options")
    sh_df = _load_logs("shares")
    fut_df = _load_logs("futures")

    # Dedup each by (contract|ticker|symbol, entry_time) — keep first entry
    if not opt_df.empty:
        opt_df = opt_df.drop_duplicates(["contract", "entry_time"], keep="first")
    if not sh_df.empty:
        sh_df = sh_df.drop_duplicates(["ticker", "entry_time"], keep="first")
    if not fut_df.empty:
        fut_df = fut_df.drop_duplicates(["symbol", "entry_time"], keep="first")

    total = len(opt_df) + len(sh_df) + len(fut_df)
    if total == 0:
        log.info("no signal logs found yet — run `python run.py` first to log signals")
        return {"signals": pd.DataFrame(), "summary": pd.DataFrame()}

    log.info("forward test: %d option + %d shares + %d futures = %d signals",
             len(opt_df), len(sh_df), len(fut_df), total)

    histories = _prefetch_current_histories(opt_df, sh_df, fut_df, max_workers)

    # Process each in parallel
    rows = []
    dropped = {}

    def _push(r: Dict[str, Any]):
        if not r:
            return
        if r.get("_drop_reason"):
            dropped[r["_drop_reason"]] = dropped.get(r["_drop_reason"], 0) + 1
            return
        rows.append(r)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = []
        for _, r in opt_df.iterrows():
            futs.append(ex.submit(_process_option, r, histories))
        for _, r in sh_df.iterrows():
            futs.append(ex.submit(_process_share, r, histories))
        for _, r in fut_df.iterrows():
            futs.append(ex.submit(_process_future, r, histories))
        for fut in as_completed(futs):
            try:
                _push(fut.result())
            except Exception as e:
                log.debug("forward fail: %s", e)
                dropped["exception"] = dropped.get("exception", 0) + 1

    pnl = pd.DataFrame(rows)
    if pnl.empty:
        log.warning("no signals re-priced — drops: %s", dropped)
        return {"signals": pnl, "summary": pd.DataFrame(), "dropped": dropped}

    # Overall
    overall = {
        "basis": "mixed_age_current_mark_telemetry",
        "n_signals": len(pnl),
        "n_total_logged": total,
        "n_dropped": sum(dropped.values()),
        "win_rate": float((pnl["pnl_pct"] > 0).mean()),
        "avg_pnl_pct": float(pnl["pnl_pct"].mean()),
        "median_pnl_pct": float(pnl["pnl_pct"].median()),
        "best": float(pnl["pnl_pct"].max()),
        "worst": float(pnl["pnl_pct"].min()),
    }

    # Per-asset breakdown
    by_asset = []
    for asset in ("option", "share", "futures"):
        sub = pnl[pnl["asset"] == asset]
        if sub.empty:
            continue
        by_asset.append({
            "asset": asset,
            "n": len(sub),
            "win_rate": float((sub["pnl_pct"] > 0).mean()),
            "avg_pnl": float(sub["pnl_pct"].mean()),
            "median_pnl": float(sub["pnl_pct"].median()),
            "sharpe": _risk_metrics(sub).get("sharpe"),
            "max_drawdown_pct": _risk_metrics(sub).get("max_drawdown_pct"),
        })

    result = {
        "signals": pnl,
        "overall": overall,
        "by_confidence": _bucket_by_confidence(pnl),
        "by_type": _bucket_by_side(pnl, ["call", "put", "shares", "futures"]),
        "by_asset": pd.DataFrame(by_asset),
        "risk": _risk_metrics(pnl),
        "dropped": dropped,
    }
    if include_fixed_horizon:
        try:
            from backtest.fixed_horizon import run_fixed_horizon_test

            fixed = run_fixed_horizon_test(
                signals=_load_all_logs(), max_workers=max_workers,
            )
            result["fixed_horizon"] = fixed["summary"]
            result["fixed_horizon_outcomes"] = fixed["outcomes"]
        except Exception as exc:
            log.warning("fixed-horizon validation skipped: %s", exc)
    return result
