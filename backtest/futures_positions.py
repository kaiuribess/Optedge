"""Futures position lifecycle tracking."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OPEN_FILE = DATA_DIR / "open_futures_positions.json"
CLOSED_FILE = DATA_DIR / "closed_futures_positions.json"

log = logging.getLogger("optedge.futures_positions")


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


def _latest_price(symbol: str) -> Optional[float]:
    try:
        import data_provider
        hist = data_provider.get_history(symbol, period="5d")
        if hist is None or hist.empty or "Close" not in hist.columns:
            return None
        close = hist["Close"].dropna()
        return float(close.iloc[-1]) if not close.empty else None
    except Exception:
        return None


def _signal_map(current_signals: Optional[pd.DataFrame]) -> Dict[str, Dict]:
    if current_signals is None or current_signals.empty or "symbol" not in current_signals.columns:
        return {}
    return {str(r.get("symbol")): r.to_dict() for _, r in current_signals.iterrows()}


def add_new_futures_signals(new_signals: pd.DataFrame, asof: datetime) -> int:
    if new_signals is None or new_signals.empty:
        return 0
    rows = _load(OPEN_FILE)
    existing = {(r.get("symbol"), r.get("direction")) for r in rows}
    added = 0
    for _, s in new_signals.iterrows():
        symbol = str(s.get("symbol") or "")
        direction = str(s.get("direction") or "")
        if not symbol or direction not in {"long", "short"} or (symbol, direction) in existing:
            continue
        if str(s.get("trade_status")) != "Trade":
            continue
        entry_time = asof.isoformat()
        row = {
            "asset": "futures",
            "position_id": f"futures|{symbol}|{direction}|{entry_time}",
            "symbol": symbol,
            "name": s.get("name"),
            "contract": s.get("contract"),
            "using_micro": bool(s.get("using_micro")),
            "direction": direction,
            "entry_time": entry_time,
            "entry_price": float(s.get("entry_price") or s.get("spot") or 0),
            "current_price": float(s.get("entry_price") or s.get("spot") or 0),
            "stop_price": float(s.get("stop_price") or 0),
            "target_price": float(s.get("target_price") or 0),
            "point_value": float(s.get("point_value") or 0),
            "contracts": int(s.get("suggested_contracts") or 0),
            "risk_dollars": float(s.get("risk_dollars") or 0),
            "reward_dollars": float(s.get("reward_dollars") or 0),
            "futures_score": float(s.get("futures_score") or 0),
            "confidence": float(s.get("confidence") or 50),
            "regime": s.get("regime"),
            "hv20": s.get("hv20"),
            "trade_status": s.get("trade_status"),
            "latest_exit_pressure": None,
            "latest_exit_action": None,
            "reprice_failed_count": 0,
        }
        rows.append(row)
        existing.add((symbol, direction))
        added += 1
    if added:
        _save(OPEN_FILE, rows)
        log.info("futures positions: +%d new opens (total open=%d)", added, len(rows))
    return added


def _pnl(pos: Dict, price: float) -> tuple[float, float, float]:
    entry = float(pos.get("entry_price") or price)
    direction = str(pos.get("direction") or "long")
    points = price - entry if direction == "long" else entry - price
    dollars = points * float(pos.get("point_value") or 0) * int(pos.get("contracts") or 0)
    pct = points / entry if entry > 0 else 0.0
    return points, dollars, pct


def _close(pos: Dict, asof: datetime, price: float, reason: str) -> Dict:
    points, dollars, pct = _pnl(pos, price)
    return {
        **pos,
        "exit_time": asof.isoformat(),
        "exit_price": price,
        "exit_reason": reason,
        "pnl_points": points,
        "pnl_dollars": dollars,
        "pnl_pct": pct,
    }


def mark_to_market_futures(asof: datetime,
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
        symbol = str(pos.get("symbol") or "")
        price = _latest_price(symbol)
        entry_ts = pd.to_datetime(pos.get("entry_time"), errors="coerce", utc=True)
        pos["age_days"] = max(0.0, (pd.Timestamp(asof) - entry_ts).total_seconds() / 86400.0) if not pd.isna(entry_ts) else 0.0
        sig = signals.get(symbol)
        if price is None:
            pos["reprice_failed_count"] = int(pos.get("reprice_failed_count") or 0) + 1
            review = compute_exit_pressure(pos, sig, asset="futures")
            log_exit_review(review)
            still_open.append(apply_dynamic_exit_action(pos, review))
            continue
        points, dollars, pct = _pnl(pos, price)
        pos.update({"current_price": price, "pnl_points": points, "pnl_dollars": dollars, "unrealized_pct": pct})
        direction = str(pos.get("direction") or "long")
        stop = float(pos.get("stop_price") or 0)
        target = float(pos.get("target_price") or 0)
        hard_reason = None
        if direction == "long" and price <= stop:
            hard_reason = "hard_stop"
        elif direction == "long" and price >= target:
            hard_reason = "hard_target"
        elif direction == "short" and price >= stop:
            hard_reason = "hard_stop"
        elif direction == "short" and price <= target:
            hard_reason = "hard_target"
        if sig is not None:
            fs = float(sig.get("futures_score") or 0)
            if direction == "long" and fs < -0.5:
                hard_reason = hard_reason or "score_reversal"
            if direction == "short" and fs > 0.5:
                hard_reason = hard_reason or "score_reversal"
        if hard_reason:
            closed = _close(pos, asof, price, hard_reason)
            review = compute_exit_pressure(closed, sig, asset="futures")
            action = "hard_stop" if hard_reason == "hard_stop" else "hard_target" if hard_reason == "hard_target" else "close_early"
            review.update({"action": action, "current_price": price, "current_pnl_pct": closed["pnl_pct"], "current_pnl_dollars": closed["pnl_dollars"]})
            log_exit_review(review)
            newly_closed.append(closed)
            continue
        review = compute_exit_pressure(pos, sig, asset="futures")
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
