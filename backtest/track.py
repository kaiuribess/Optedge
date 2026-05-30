"""Signal logging — writes per-asset-type signal logs with entry_time stamped in.

Three writers, one schema convention:
  - log_signals()        → logs/signals_<asof>.parquet (options)
  - log_signals_shares() → logs/shares_signals_<asof>.parquet
  - log_signals_futures()→ logs/futures_signals_<asof>.parquet

Every row gets an `entry_time` column = the run's ASOF timestamp (ISO 8601 UTC).
That's the canonical "when did we recommend this" — used by forward test for
age-since-entry calculations + deduplication across runs.

This replaces the old behavior of using file mtime as a proxy for entry time
(which broke after the user copied files around).
"""
from __future__ import annotations
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from utils import bs_price

log = logging.getLogger("optedge.backtest")
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


def _ensure_entry_time(df: pd.DataFrame, asof: datetime) -> pd.DataFrame:
    """Stamp entry_time as ISO string and is_buy=True (long-only by design)."""
    out = df.copy()
    out["entry_time"] = asof.isoformat()
    if "is_buy" not in out.columns:
        out["is_buy"] = True
    return out


def log_signals(df: pd.DataFrame, asof: datetime) -> Optional[Path]:
    """Log option signals (calls + puts mixed) to logs/signals_<asof>.parquet."""
    if df is None or df.empty:
        return None
    LOG_DIR.mkdir(exist_ok=True)
    fp = LOG_DIR / f"signals_{asof.strftime('%Y%m%d_%H%M%S')}.parquet"
    cols = [
        "ticker", "contract", "side", "strike", "expiry", "dte", "spot",
        "mid", "bid", "ask", "spread_pct", "net_edge_pct",
        "iv_market", "fair_vol", "vol_premium", "delta", "open_interest",
        "regime", "macro_tilt",
        "is_buy", "fused_score", "confidence", "signal", "reasoning", "risks",
        "z_mispricing", "z_iv_rank", "z_skew", "z_sent", "z_fund", "z_insider",
        "z_macro", "z_news", "z_earnings", "z_value", "z_congress", "z_social", "z_analyst",
        "pred_stock_return_pct", "pred_option_return_pct", "ev_pct", "kelly_pct",
        "suggested_contracts", "actual_dollars", "stop_price", "target_price",
        "trade_status", "trade_score", "setup_quality_mult",
        "research_guard_status", "research_guard_warnings",
        "entry_time",
    ]
    out = _ensure_entry_time(df, asof)
    keep = [c for c in cols if c in out.columns]
    out[keep].to_parquet(fp, index=False)
    log.info("logged %d option signals to %s", len(out), fp.name)
    return fp


def log_signals_shares(df: pd.DataFrame, asof: datetime) -> Optional[Path]:
    """Log share-buy signals to logs/shares_signals_<asof>.parquet."""
    if df is None or df.empty:
        return None
    LOG_DIR.mkdir(exist_ok=True)
    fp = LOG_DIR / f"shares_signals_{asof.strftime('%Y%m%d_%H%M%S')}.parquet"
    cols = [
        "ticker", "spot", "share_score", "confidence", "classification", "market_cap",
        "regime", "macro_tilt",
        "stop_pct", "target_pct", "suggested_dollars", "kelly_pct", "ev_pct",
        "trade_status", "trade_score", "setup_quality_mult",
        "research_guard_status", "research_guard_warnings",
        "z_sent", "z_fund", "z_insider", "z_news", "z_earnings", "z_value",
        "z_congress", "z_social", "z_analyst",
        "sentiment_delta", "fund_score", "insider_score", "news_delta", "n_24h",
        "top_headline", "reasoning", "risks", "pred_stock_return_pct",
        "side", "entry_time",
    ]
    out = _ensure_entry_time(df, asof)
    out["side"] = "shares"
    keep = [c for c in cols if c in out.columns]
    out[keep].to_parquet(fp, index=False)
    log.info("logged %d share signals to %s", len(out), fp.name)
    return fp


def log_signals_futures(df: pd.DataFrame, asof: datetime) -> Optional[Path]:
    """Log futures signals to logs/futures_signals_<asof>.parquet."""
    if df is None or df.empty:
        return None
    LOG_DIR.mkdir(exist_ok=True)
    fp = LOG_DIR / f"futures_signals_{asof.strftime('%Y%m%d_%H%M%S')}.parquet"
    cols = [
        "symbol", "name", "etf", "kind", "bucket", "futures_score",
        "spot", "ret_5d", "ret_20d", "ret_60d", "hv20", "atr14", "range_pos",
        "side", "is_long", "macro_align",
        "micro_symbol", "point_value", "margin_per_contract",
        "contract", "micro_contract", "using_micro", "direction",
        "stop_atr_mult", "target_atr_mult", "entry", "stop_price", "target_price",
        "entry_price", "risk_dollars", "reward_dollars", "reward_risk_ratio",
        "suggested_contracts", "suggested_dollars_risk",
        "stop_pts", "target_pts", "dollar_risk_per_contract", "dollar_reward_per_contract",
        "n_contracts", "kelly_pct", "expected_pnl",
        "factor_trend", "factor_momentum", "factor_range_pos", "factor_macro_align",
        "factor_news", "factor_earnings", "factor_social", "factor_congress",
        "factor_iv_rank", "factor_atr_regime", "factor_term_structure", "factor_sentiment_d",
        "entry_time",
    ]
    out = _ensure_entry_time(df, asof)
    keep = [c for c in cols if c in out.columns]
    out[keep].to_parquet(fp, index=False)
    log.info("logged %d futures signals to %s", len(out), fp.name)
    return fp


# Legacy helpers kept for callers that haven't migrated to forward.py / predictor.py
def evaluate_log(fp: Path, horizon_days: int = 7) -> Optional[pd.DataFrame]:
    """Deprecated: use backtest.forward.run_forward_test() instead."""
    import yfinance as yf
    df = pd.read_parquet(fp)
    out = []
    for _, r in df.iterrows():
        try:
            tk = yf.Ticker(r["ticker"])
            h = tk.history(period=f"{horizon_days+5}d")
            if h.empty:
                continue
            new_spot = float(h["Close"].iloc[-1])
            T_new = max((pd.to_datetime(r["expiry"]) - pd.Timestamp.utcnow()).days, 1) / 365.25
            if T_new <= 0:
                continue
            new_price = bs_price(new_spot, r["strike"], T_new, 0.045,
                                 r["iv_market"], 0.0, call=(r["side"] == "call"))
            pnl_pct = (new_price - r["mid"]) / r["mid"] if r.get("is_buy", True) else (r["mid"] - new_price) / r["mid"]
            out.append({"contract": r["contract"], "fused_score": r["fused_score"], "pnl_pct": pnl_pct})
        except Exception:
            continue
    return pd.DataFrame(out) if out else None
