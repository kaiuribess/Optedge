"""Position-level P&L tracking — v20.7.

Distinguishes "still-open recommendation" from "expired worthless" so the
forward-test win-rate / avg P&L is computed against the right denominator.

Design:
  - `data/open_positions.json` — current view of every still-open
    recommendation (entry date, side, strike, expiry, entry_price, dte).
    Updated each iter when new signals arrive AND when positions expire / close.
  - On each iter:
      1. Add new top-of-board signals as new positions (one row per
         unique (ticker, side, strike, expiry) tuple — dedups across iters).
      2. Re-price every still-open position via chain_provider; compute
         unrealized P&L vs entry_price.
      3. Move expired (DTE ≤ 0) or filled-at-target / hit-stop positions
         to `data/closed_positions.json` with realized P&L.
  - Forward test sees both files; dashboard can show open MTM and closed
    realised P&L separately.

This is independent from `backtest/track.py` (which keeps a row-per-iter log
of EVERY signal regardless of dedup). track.py = signal stream;
positions.py = portfolio state.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

import sys
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("optedge.positions")

DATA_DIR = ROOT / "data"
OPEN_FILE   = DATA_DIR / "open_positions.json"
CLOSED_FILE = DATA_DIR / "closed_positions.json"

OPTION_KEY_COLS = ("ticker", "side", "strike", "expiry")
TRACKED_SIGNAL_PREFIXES = ("z_", "factor_")
TRACKED_SIGNAL_COLS = {
    "rank_score", "fused_score", "confidence", "ev_pct", "kelly_pct",
    "prob_win", "setup_quality_mult", "trade_score", "bucket",
}


def _load(path: Path) -> List[Dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception as e:
        log.debug("positions load %s: %s", path.name, e)
        return []


def _save(path: Path, rows: List[Dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(rows, indent=2, default=str))
    except Exception as e:
        log.warning("positions save %s: %s", path.name, e)


def _option_key(row: Dict) -> Tuple:
    return tuple(row.get(c) for c in OPTION_KEY_COLS)


def add_new_signals(new_signals: pd.DataFrame, asof: datetime) -> int:
    """Insert any new (ticker, side, strike, expiry) tuples not already open.
    Returns the number of new positions added."""
    if new_signals is None or new_signals.empty:
        return 0
    open_rows = _load(OPEN_FILE)
    existing = {_option_key(r) for r in open_rows}
    added = 0
    for _, s in new_signals.iterrows():
        key = (s.get("ticker"), s.get("side"), s.get("strike"), s.get("expiry"))
        if key in existing or any(v is None for v in key):
            continue
        row = {
            "ticker": s.get("ticker"),
            "side": s.get("side"),
            "strike": float(s.get("strike") or 0),
            "expiry": s.get("expiry"),
            "dte_at_entry": int(s.get("dte") or 0),
            "entry_price": float(s.get("mid") or 0),
            "entry_spot":  float(s.get("spot") or 0),
            "entry_iv":    float(s.get("iv_market") or 0),
            "entry_delta": float(s.get("delta") or 0),
            "spread_pct":  float(s.get("spread_pct") or 0),
            "net_edge_pct": float(s.get("net_edge_pct") or 0),
            "entry_time":  asof.isoformat(),
            "fused_score": float(s.get("fused_score") or 0),
            "confidence":  float(s.get("confidence") or 0),
            "suggested_contracts": int(s.get("suggested_contracts") or 0),
            "trade_status": s.get("trade_status"),
            "research_guard_status": s.get("research_guard_status"),
            "research_guard_warnings": s.get("research_guard_warnings"),
            "stop_price":   float(s.get("stop_price") or 0),
            "target_price": float(s.get("target_price") or 0),
        }
        for col in s.index:
            if col in row:
                continue
            if col in TRACKED_SIGNAL_COLS or any(str(col).startswith(p) for p in TRACKED_SIGNAL_PREFIXES):
                value = s.get(col)
                try:
                    if pd.isna(value):
                        continue
                except Exception:
                    pass
                if hasattr(value, "item"):
                    value = value.item()
                row[str(col)] = value
        open_rows.append(row)
        added += 1
    if added:
        _save(OPEN_FILE, open_rows)
        log.info("positions: +%d new opens (total open=%d)", added, len(open_rows))
    return added


def _current_mid_for_position(pos: Dict, chain_blobs: Dict[str, dict]) -> Optional[float]:
    blob = chain_blobs.get((pos.get("ticker") or "").upper())
    if not blob:
        return None
    df = blob.get("chains", {}).get(str(pos.get("expiry")))
    if df is None or getattr(df, "empty", True):
        return None
    hit = df[(df["strike"].round(2) == round(float(pos.get("strike") or 0), 2)) &
             (df["side"] == pos.get("side"))]
    if hit.empty:
        return None
    r = hit.iloc[0]
    bid, ask = float(r.get("bid") or 0), float(r.get("ask") or 0)
    if bid > 0 and ask > 0 and ask >= bid:
        return (bid + ask) / 2
    last = float(r.get("lastPrice") or 0)
    return last if last > 0 else None


def mark_to_market(asof: datetime, max_chain_fetch: int = 60) -> Dict[str, float]:
    """Re-fetch the current chain for each unique open-position ticker and
    compute per-position unrealized P&L. Move expired/triggered positions
    to closed. Returns a small summary dict for logging."""
    open_rows = _load(OPEN_FILE)
    if not open_rows:
        return {"open": 0, "closed_this_iter": 0, "mean_unrealized_pct": 0.0}

    try:
        import chain_provider
    except Exception:
        log.debug("positions: chain_provider unavailable, skipping MTM")
        return {"open": len(open_rows), "closed_this_iter": 0,
                 "mean_unrealized_pct": 0.0}

    tickers = sorted({(r.get("ticker") or "").upper() for r in open_rows
                       if r.get("ticker")})
    if len(tickers) > max_chain_fetch:
        # Prioritize the freshest entries (most recent entry_time)
        recent_tk = (pd.DataFrame(open_rows)
                       .sort_values("entry_time", ascending=False)
                       .head(max_chain_fetch)["ticker"].astype(str).str.upper().tolist())
        tickers = list(dict.fromkeys(recent_tk))

    chains: Dict[str, dict] = {}
    for tk in tickers:
        try:
            b = chain_provider.fetch_chain(tk, cache_age=600)
            if b and b.get("chains"):
                chains[tk] = b
        except Exception:
            continue

    still_open: List[Dict] = []
    newly_closed: List[Dict] = []
    unrealized_pcts: List[float] = []
    now = asof if isinstance(asof, datetime) else datetime.now(timezone.utc)
    for pos in open_rows:
        try:
            entry_ts = pd.to_datetime(pos.get("entry_time"), errors="coerce", utc=True)
            age_days = max(0.0, (pd.Timestamp(now) - entry_ts).total_seconds() / 86400.0)
        except Exception:
            age_days = None
        # Check expiry first — if past today, close it
        try:
            exp_dt = datetime.strptime(str(pos.get("expiry")), "%Y-%m-%d") \
                              .replace(tzinfo=timezone.utc)
        except Exception:
            exp_dt = None
        is_expired = exp_dt is not None and now >= exp_dt
        cur_mid = _current_mid_for_position(pos, chains)
        if is_expired:
            entry = float(pos.get("entry_price") or 0)
            # At expiry, intrinsic value is the only thing left
            strike = float(pos.get("strike") or 0)
            spot = 0.0
            blob = chains.get((pos.get("ticker") or "").upper())
            if blob:
                spot = float(blob.get("spot") or 0)
            if pos.get("side") == "call":
                final = max(0.0, spot - strike)
            else:
                final = max(0.0, strike - spot)
            pnl_pct = ((final - entry) / entry) if entry > 0 else 0.0
            closed = {**pos, "exit_time": now.isoformat(),
                      "exit_price": final, "exit_reason": "expired",
                      "pnl_pct": pnl_pct, "age_days": age_days}
            newly_closed.append(closed)
            continue
        if cur_mid is None:
            # Couldn't reprice — keep open, no MTM update
            still_open.append({**pos, "age_days": age_days})
            continue
        entry = float(pos.get("entry_price") or 0)
        if entry <= 0:
            still_open.append({**pos, "current_mid": cur_mid, "age_days": age_days})
            continue
        pnl_pct = (cur_mid - entry) / entry
        unrealized_pcts.append(pnl_pct)
        # Stop / target triggers
        stop = float(pos.get("stop_price") or 0)
        target = float(pos.get("target_price") or 0)
        if stop > 0 and cur_mid <= stop:
            newly_closed.append({**pos, "exit_time": now.isoformat(),
                                  "exit_price": cur_mid, "exit_reason": "stop",
                                  "pnl_pct": pnl_pct, "age_days": age_days})
            continue
        if target > 0 and cur_mid >= target:
            newly_closed.append({**pos, "exit_time": now.isoformat(),
                                  "exit_price": cur_mid, "exit_reason": "target",
                                  "pnl_pct": pnl_pct, "age_days": age_days})
            continue
        still_open.append({**pos, "current_mid": cur_mid, "unrealized_pct": pnl_pct,
                           "age_days": age_days})

    if newly_closed:
        prev_closed = _load(CLOSED_FILE)
        _save(CLOSED_FILE, prev_closed + newly_closed)
    _save(OPEN_FILE, still_open)
    mean_un = (sum(unrealized_pcts) / len(unrealized_pcts)) if unrealized_pcts else 0.0
    log.info("positions: %d open (mean unrealized %+.1f%%), %d closed this iter",
             len(still_open), mean_un * 100, len(newly_closed))
    return {"open": len(still_open),
            "closed_this_iter": len(newly_closed),
            "mean_unrealized_pct": mean_un}


def summary() -> Dict[str, float]:
    """Roll-up of open + closed positions. Useful for the dashboard."""
    open_rows = _load(OPEN_FILE)
    closed_rows = _load(CLOSED_FILE)
    closed_pnls = [float(r.get("pnl_pct") or 0) for r in closed_rows]
    realized_win_rate = (sum(1 for p in closed_pnls if p > 0) / len(closed_pnls)) \
                         if closed_pnls else 0.0
    realized_avg = (sum(closed_pnls) / len(closed_pnls)) if closed_pnls else 0.0
    return {
        "open_count": len(open_rows),
        "closed_count": len(closed_rows),
        "realized_win_rate": realized_win_rate,
        "realized_avg_pnl_pct": realized_avg,
    }


def aging_summary(asof: Optional[datetime] = None) -> Dict[str, object]:
    """Summarize open recommendation age so stale theses are visible."""
    rows = _load(OPEN_FILE)
    if not rows:
        return {"open_count": 0, "buckets": [], "oldest": []}
    now = asof or datetime.now(timezone.utc)
    df = pd.DataFrame(rows)
    df["entry_time"] = pd.to_datetime(df.get("entry_time"), errors="coerce", utc=True)
    df["age_days"] = (pd.Timestamp(now) - df["entry_time"]).dt.total_seconds() / 86400.0
    df["age_days"] = df["age_days"].clip(lower=0)
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
    oldest_cols = [
        c for c in ("ticker", "side", "expiry", "entry_time", "age_days",
                    "unrealized_pct", "confidence", "trade_status")
        if c in df.columns
    ]
    oldest = (
        df.sort_values("age_days", ascending=False)
          .head(20)[oldest_cols]
          .to_dict(orient="records")
    )
    return {"open_count": int(len(df)), "buckets": buckets, "oldest": oldest}
