"""Share position lifecycle tracking."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OPEN_FILE = DATA_DIR / "open_share_positions.json"
CLOSED_FILE = DATA_DIR / "closed_share_positions.json"

log = logging.getLogger("optedge.share_positions")

ENTRY_FACTOR_PREFIXES = ("z_", "factor_")
ENTRY_FACTOR_COLUMNS = {
    "mentions", "sentiment_now", "sentiment_delta", "sentiment_decay", "velocity",
    "news_delta", "news_velocity", "top_headline", "n_24h", "fund_score",
    "classification", "rev_growth", "op_margin", "pe", "market_cap",
    "insider_score", "n_buys", "n_sells", "buys_value", "sells_value",
    "earnings_date", "next_earnings_date", "days_to_earnings", "earnings_score",
    "value_score", "value_bucket", "earnings_yield", "fcf_yield", "graham_score",
    "congress_score", "congress_buys_n", "congress_top_buyer", "social_score",
    "stocktwits_n", "stocktwits_avg_sent", "analyst_score", "analyst_total",
    "analyst_avg", "analyst_momentum", "sector_rs_score", "sector_etf",
    "ticker_ret_20d", "sector_ret_20d", "dark_pool_score", "short_vol_ratio",
    "fda_score", "days_to_catalyst", "sector_flow_score", "tech_score", "rsi",
    "macd_hist", "bb_percent_b", "ma_cross", "adx", "stoch_k", "obv_slope",
    "short_int_score", "short_pct_of_float", "short_ratio_days_to_cover",
    "cot_score", "thirteen_f_score", "vix_term_score", "eia_score",
    "wasde_score", "buyback_score", "gtrends_score", "form_144_score",
    "whisper_score", "hyperliquid_score", "twitter_score", "r_options_score",
    "curve_score", "credit_score", "cluster_buys_score",
}


def _load(path: Path) -> List[Dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(path: Path, rows: List[Dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")


def _position_id(ticker: str, entry_time: str) -> str:
    return f"share|{ticker}|{entry_time}"


def _json_value(value):
    if hasattr(value, "item"):
        value = value.item()
    if not isinstance(value, (list, dict, tuple)):
        try:
            if pd.isna(value):
                return None
        except Exception:
            pass
    return value


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def _latest_price(ticker: str) -> Optional[float]:
    try:
        import data_provider
        hist = data_provider.get_history(ticker, period="5d")
        if hist is None or hist.empty or "Close" not in hist.columns:
            return None
        close = hist["Close"].dropna()
        return float(close.iloc[-1]) if not close.empty else None
    except Exception:
        return None


def _signal_map(current_signals: Optional[pd.DataFrame]) -> Dict[str, Dict]:
    if current_signals is None or current_signals.empty or "ticker" not in current_signals.columns:
        return {}
    return {
        str(r.get("ticker")).upper(): r.to_dict()
        for _, r in current_signals.iterrows()
        if r.get("ticker") is not None
    }


def add_new_share_signals(new_signals: pd.DataFrame, asof: datetime) -> int:
    if new_signals is None or new_signals.empty:
        return 0
    rows = _load(OPEN_FILE)
    existing = {str(r.get("ticker", "")).upper() for r in rows}
    added = 0
    for _, s in new_signals.iterrows():
        ticker = str(s.get("ticker") or "").upper()
        if not ticker or ticker in existing:
            continue
        status = str(s.get("trade_status") or "").strip().lower()
        if status and status not in {"trade", "buy", "long"}:
            continue
        if "is_actionable" in s.index and not bool(s.get("is_actionable")):
            continue
        entry = _safe_float(s.get("spot") or s.get("entry_price") or s.get("current_price"), 0.0)
        if entry <= 0:
            entry = _safe_float(_latest_price(ticker), 0.0)
        if entry <= 0:
            continue
        stop_pct = float(s.get("stop_pct") if s.get("stop_pct") is not None else -0.08)
        target_pct = float(s.get("target_pct") if s.get("target_pct") is not None else 0.20)
        entry_time = asof.isoformat()
        row = {
            "asset": "share",
            "position_id": _position_id(ticker, entry_time),
            "ticker": ticker,
            "entry_time": entry_time,
            "entry_price": entry,
            "current_price": entry,
            "suggested_dollars": float(s.get("suggested_dollars") or 0),
            "confidence": float(s.get("confidence") or 0),
            "fused_score": float(s.get("fused_score") or s.get("share_score") or s.get("rank_score") or 0),
            "rank_score": float(s.get("rank_score") or s.get("share_score") or 0),
            "ev_pct": float(s.get("ev_pct") or 0),
            "kelly_pct": float(s.get("kelly_pct") or 0),
            "stop_pct": stop_pct,
            "target_pct": target_pct,
            "stop_price": entry * (1.0 + stop_pct),
            "target_price": entry * (1.0 + target_pct),
            "trade_status": s.get("trade_status"),
            "research_guard_status": s.get("research_guard_status"),
            "research_guard_warnings": s.get("research_guard_warnings"),
            "latest_exit_pressure": None,
            "latest_exit_action": None,
            "reprice_failed_count": 0,
        }
        for col in s.index:
            if col in row:
                continue
            col_name = str(col)
            if col_name.startswith(ENTRY_FACTOR_PREFIXES) or col_name in ENTRY_FACTOR_COLUMNS:
                row[col_name] = _json_value(s.get(col))
        rows.append(row)
        existing.add(ticker)
        added += 1
    if added:
        _save(OPEN_FILE, rows)
        log.info("share positions: +%d new opens (total open=%d)", added, len(rows))
    return added


def _close(pos: Dict, asof: datetime, price: float, reason: str) -> Dict:
    entry = float(pos.get("entry_price") or 0)
    pnl_pct = (price - entry) / entry if entry > 0 else 0.0
    dollars = float(pos.get("suggested_dollars") or 0) * pnl_pct
    return {
        **pos,
        "exit_time": asof.isoformat(),
        "exit_price": price,
        "exit_reason": reason,
        "pnl_pct": pnl_pct,
        "pnl_dollars": dollars,
    }


def mark_to_market_shares(asof: datetime,
                          current_signals: Optional[pd.DataFrame] = None) -> Dict[str, float]:
    from backtest.exit_rules import (
        apply_dynamic_exit_action, compute_exit_pressure, log_exit_review,
    )

    rows = _load(OPEN_FILE)
    if not rows:
        return {"open": 0, "closed_this_iter": 0}
    signals = _signal_map(current_signals)
    still_open, newly_closed = [], []
    for pos in rows:
        ticker = str(pos.get("ticker") or "").upper()
        price = _latest_price(ticker)
        now_ts = pd.Timestamp(asof)
        entry_ts = pd.to_datetime(pos.get("entry_time"), errors="coerce", utc=True)
        age_days = max(0.0, (now_ts - entry_ts).total_seconds() / 86400.0) if not pd.isna(entry_ts) else 0.0
        pos["age_days"] = age_days
        if price is None:
            pos["reprice_failed_count"] = int(pos.get("reprice_failed_count") or 0) + 1
            review = compute_exit_pressure(pos, signals.get(ticker), asset="share")
            log_exit_review(review)
            still_open.append(apply_dynamic_exit_action(pos, review))
            continue
        entry = float(pos.get("entry_price") or price)
        pos["current_price"] = price
        pos["unrealized_pct"] = (price - entry) / entry if entry > 0 else 0.0
        if price <= float(pos.get("stop_price") or 0):
            closed = _close(pos, asof, price, "hard_stop")
            review = compute_exit_pressure(closed, signals.get(ticker), asset="share")
            review.update({"action": "hard_stop", "current_price": price, "current_pnl_pct": closed["pnl_pct"]})
            log_exit_review(review)
            newly_closed.append(closed)
            continue
        if price >= float(pos.get("target_price") or float("inf")):
            closed = _close(pos, asof, price, "hard_target")
            review = compute_exit_pressure(closed, signals.get(ticker), asset="share")
            review.update({"action": "hard_target", "current_price": price, "current_pnl_pct": closed["pnl_pct"]})
            log_exit_review(review)
            newly_closed.append(closed)
            continue
        review = compute_exit_pressure(pos, signals.get(ticker), asset="share")
        log_exit_review(review)
        if review["action"] == "close_early":
            newly_closed.append(_close(pos, asof, price, "dynamic_exit"))
        else:
            still_open.append(apply_dynamic_exit_action(pos, review, current_price=price))
    if newly_closed:
        _save(CLOSED_FILE, _load(CLOSED_FILE) + newly_closed)
    _save(OPEN_FILE, still_open)
    return {"open": len(still_open), "closed_this_iter": len(newly_closed)}


def summary() -> Dict[str, float]:
    open_rows = _load(OPEN_FILE)
    closed_rows = _load(CLOSED_FILE)
    pnls = [float(r.get("pnl_pct") or 0) for r in closed_rows]
    return {
        "open_count": len(open_rows),
        "closed_count": len(closed_rows),
        "realized_win_rate": sum(1 for p in pnls if p > 0) / len(pnls) if pnls else 0.0,
        "realized_avg_pnl_pct": sum(pnls) / len(pnls) if pnls else 0.0,
    }
