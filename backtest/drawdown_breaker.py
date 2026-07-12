"""Drawdown circuit breaker.

Reads recent forward-test signal P&L from logs/*.parquet and computes the
trailing 14-day P&L. If it's deeply negative, automatically halves the Kelly
fraction until win rate recovers.

Triggers:
  rolling_14d_pnl_pct < -10%   -> halve Kelly  (multiplier = 0.5)
  rolling_14d_pnl_pct < -20%   -> quarter Kelly (multiplier = 0.25)
  rolling_14d_pnl_pct > +5%    -> restore (multiplier = 1.0)
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict

import pandas as pd

log = logging.getLogger("optedge.breaker")

ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT / "logs"


def _load_recent_forward_results(days: int = 14) -> pd.DataFrame:
    """Load forward-test results from data/ or logs/."""
    data_dir = ROOT / "data"
    if not data_dir.exists():
        return pd.DataFrame()
    files = sorted(data_dir.glob("forward_test_*.parquet"),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        return pd.DataFrame()
    # Use the most recent forward_test snapshot
    try:
        df = pd.read_parquet(files[0])
        return df
    except Exception as e:
        log.debug("breaker load fail: %s", e)
        return pd.DataFrame()


def compute_breaker_state(window_days: int = 14) -> Dict:
    """Returns dict with multiplier in [0.25, 1.0], explanation, stats."""
    df = _load_recent_forward_results(window_days)
    if df is None or df.empty:
        return {"multiplier": 1.0, "verdict": "no forward data", "n": 0,
                "rolling_pnl_pct": 0.0, "rolling_win_rate": 0.0}

    # Filter to last N days based on log_time or filing_date
    time_col = None
    for c in ("log_time", "filing_date", "asof"):
        if c in df.columns:
            time_col = c
            break
    if time_col is not None:
        cutoff = datetime.utcnow() - timedelta(days=window_days)
        try:
            df[time_col] = pd.to_datetime(df[time_col], errors="coerce", utc=True).dt.tz_localize(None)
            df = df[df[time_col] >= cutoff]
        except Exception:
            pass

    if df.empty:
        return {"multiplier": 1.0, "verdict": "no signals in window", "n": 0,
                "rolling_pnl_pct": 0.0, "rolling_win_rate": 0.0}

    pnl_col = None
    for c in ("pnl_pct", "realized_pnl_pct", "return_pct"):
        if c in df.columns:
            pnl_col = c
            break
    if pnl_col is None:
        return {"multiplier": 1.0, "verdict": "no pnl column", "n": len(df),
                "rolling_pnl_pct": 0.0, "rolling_win_rate": 0.0}

    pnls = pd.to_numeric(df[pnl_col], errors="coerce").dropna()
    if pnls.empty:
        return {"multiplier": 1.0, "verdict": "all NaN", "n": 0,
                "rolling_pnl_pct": 0.0, "rolling_win_rate": 0.0}
    avg_pnl = pnls.mean()
    win_rate = (pnls > 0).mean()

    if avg_pnl < -0.20:
        mult = 0.25
        verdict = f"DEEP drawdown — Kelly cut to ¼ (avg P&L {avg_pnl*100:+.1f}%)"
    elif avg_pnl < -0.10:
        mult = 0.5
        verdict = f"drawdown — Kelly halved (avg P&L {avg_pnl*100:+.1f}%)"
    elif avg_pnl > 0.05:
        mult = 1.0
        verdict = f"recovered (avg P&L {avg_pnl*100:+.1f}%)"
    else:
        # Neutral — apply mild reduction if win rate is poor
        if win_rate < 0.40:
            mult = 0.75
            verdict = f"poor win rate {win_rate*100:.0f}% — Kelly 0.75x"
        else:
            mult = 1.0
            verdict = "normal"

    return {
        "multiplier": mult,
        "verdict": verdict,
        "n": int(len(pnls)),
        "rolling_pnl_pct": float(avg_pnl),
        "rolling_win_rate": float(win_rate),
    }


def apply_breaker_to_kelly(kelly_pct: float, breaker_mult: float = 1.0) -> float:
    """Multiply kelly_pct by breaker_mult."""
    try:
        return float(kelly_pct) * float(breaker_mult)
    except Exception:
        return kelly_pct
