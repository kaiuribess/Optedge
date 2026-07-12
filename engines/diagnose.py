"""Diagnostic — why are shares/futures empty? (v16.1.1)

Walks bucket-by-bucket through the self-learning pipeline and reports:
  1. Is v16.1 actually deployed? (checks for shares_signals_*.parquet logger)
  2. How many signal logs exist per bucket?
  3. How many got successfully replayed?
  4. What stopped the rest? (yfinance failure / TZ filter / too fresh / etc.)

Usage:  python run.py --diagnose
"""
from __future__ import annotations
import glob
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any

import pandas as pd

import sys
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engines import learning, forward_test
import data_provider

log = logging.getLogger("optedge.diagnose")
LOGS_DIR = ROOT / "logs"
DATA_DIR = ROOT / "data"


def _summarize_log_files(pattern: str) -> Dict[str, Any]:
    """Inventory log files matching pattern."""
    files = sorted(glob.glob(str(LOGS_DIR / pattern)))
    if not files:
        return {"n_files": 0, "n_rows": 0, "newest": None, "oldest": None,
                "size_kb": 0}
    n_rows = 0
    size = 0
    for f in files:
        try:
            n_rows += len(pd.read_parquet(f))
            size += os.path.getsize(f)
        except Exception:
            continue
    newest = max(files, key=lambda f: os.path.getmtime(f))
    oldest = min(files, key=lambda f: os.path.getmtime(f))
    return {
        "n_files": len(files),
        "n_rows": n_rows,
        "newest": Path(newest).name,
        "oldest": Path(oldest).name,
        "newest_age_hours": (datetime.now().timestamp() - os.path.getmtime(newest)) / 3600,
        "oldest_age_hours": (datetime.now().timestamp() - os.path.getmtime(oldest)) / 3600,
        "size_kb": round(size / 1024, 1),
    }


def _check_v16_1_deployed() -> Dict[str, Any]:
    """Test if v16.1 code is in place by checking for log_shares_signals function."""
    deployed = hasattr(forward_test, "log_shares_signals")
    has_load_shares = hasattr(forward_test, "_load_shares_logs")
    # Also check README header
    try:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")[:500]
        readme_says = "v16.1" in readme
    except Exception:
        readme_says = False
    return {
        "log_shares_signals_present": deployed,
        "_load_shares_logs_present": has_load_shares,
        "readme_says_v16_1": readme_says,
        "verdict": "v16.1 DEPLOYED" if (deployed and has_load_shares) else
                   "v16.1 NOT DEPLOYED — restart your --loop process after extracting"
    }


def _trace_replay_one(row: pd.Series, replay_fn) -> Dict[str, Any]:
    """Run a single replay function and capture why it returned None."""
    ticker = row.get("ticker") or row.get("symbol") or "?"
    try:
        result = replay_fn(row)
        if result is not None:
            return {"ticker": ticker, "status": "OK",
                    "outcome": result.get("outcome"),
                    "pnl_pct": result.get("pnl_pct")}
        # Walk the failure points manually
        log_time = row.get("log_time")
        if log_time is None:
            return {"ticker": ticker, "status": "FAIL", "reason": "missing log_time"}
        days_old = (datetime.now(timezone.utc) - pd.to_datetime(log_time, utc=True)).total_seconds() / 86400
        if days_old < 0.05:
            return {"ticker": ticker, "status": "TOO_FRESH",
                    "age_hours": round(days_old * 24, 2),
                    "reason": "<1.2 hours old, replay skips"}
        # Try the price fetch
        symbol = row.get("symbol") or row.get("ticker")
        if not symbol:
            return {"ticker": ticker, "status": "FAIL", "reason": "no symbol"}
        h = data_provider.get_history(symbol, period="1mo")
        if h is None or h.empty:
            return {"ticker": ticker, "status": "FAIL",
                    "reason": "yfinance returned empty (rate-limited or delisted?)"}
        # Slice
        log_date = pd.to_datetime(log_time).date()
        idx_dates = pd.Series(h.index).apply(lambda x: x.date() if hasattr(x, "date") else pd.to_datetime(x).date())
        mask = idx_dates.values >= log_date
        h_after = h[mask]
        if h_after.empty:
            return {"ticker": ticker, "status": "FAIL",
                    "reason": f"no price bars >= log_date ({log_date}); h.index range {h.index.min()}..{h.index.max()}"}
        return {"ticker": ticker, "status": "FAIL", "reason": "replay returned None for unknown reason"}
    except Exception as e:
        return {"ticker": ticker, "status": "ERROR", "reason": str(e)[:120]}


def diagnose() -> None:
    print("=" * 72)
    print("Optedge v16.1 DIAGNOSTIC")
    print("=" * 72)

    # ---- 1. Deployment check ----
    dep = _check_v16_1_deployed()
    print(f"\n[1] Deployment check: {dep['verdict']}")
    print(f"    log_shares_signals: {dep['log_shares_signals_present']}")
    print(f"    _load_shares_logs:  {dep['_load_shares_logs_present']}")
    print(f"    README says v16.1:  {dep['readme_says_v16_1']}")
    if not dep["log_shares_signals_present"]:
        print("\n    ⚠  v16.1 not loaded. Stop your --loop process (Ctrl+C),")
        print("       extract optedge_v16_1.zip over the optedge folder, restart.")
        return

    # ---- 2. Signal log inventory ----
    print("\n[2] Signal log inventory (logs/ folder)")
    print("-" * 72)
    for pat, label in [
        ("signals_*.parquet",          "options (signals_*)"),
        ("shares_signals_*.parquet",   "shares  (shares_signals_*) [v16.1]"),
        ("futures_signals_*.parquet",  "futures (futures_signals_*) [v16+]"),
    ]:
        s = _summarize_log_files(pat)
        if s["n_files"] == 0:
            print(f"    {label:<48} NO FILES")
        else:
            print(f"    {label:<48}")
            print(f"      files={s['n_files']}  rows={s['n_rows']}  "
                  f"size={s['size_kb']}KB")
            print(f"      newest: {s['newest']}  ({s['newest_age_hours']:.1f}h old)")
            print(f"      oldest: {s['oldest']}  ({s['oldest_age_hours']:.1f}h old)")

    # ---- 3. Forward-outcomes inventory ----
    print("\n[3] Forward-outcomes inventory (data/forward_outcomes_<bucket>.parquet)")
    print("-" * 72)
    for bucket in learning.BUCKET_KEYS:
        p = DATA_DIR / f"forward_outcomes_{bucket}.parquet"
        if p.exists():
            try:
                df = pd.read_parquet(p)
                age = (datetime.now().timestamp() - os.path.getmtime(p)) / 3600
                print(f"    {bucket:<22} {len(df):>4} rows  ({age:.1f}h old)")
            except Exception as e:
                print(f"    {bucket:<22} READ ERROR: {e}")
        else:
            print(f"    {bucket:<22} —")

    # ---- 4. Per-bucket weight files ----
    print("\n[4] Per-bucket weight files (data/weights/<bucket>.json)")
    print("-" * 72)
    for s in learning.list_bucket_status():
        b = s["bucket"]
        mode = s["source"]
        n = s["n_samples"]
        fitted = (s.get("fitted_at") or "—").split("T")[0]
        print(f"    {b:<22} mode={mode:<15} n={n:<5} fitted={fitted}")

    # ---- 5. Replay deep-dive — sample the freshest shares + futures logs ----
    print("\n[5] Replay deep-dive — first 5 signals from newest shares + futures logs")
    print("-" * 72)
    for pat, replay_fn, label in [
        ("shares_signals_*.parquet",  forward_test._replay_share_signal, "shares"),
        ("futures_signals_*.parquet", forward_test._replay_futures_signal, "futures"),
    ]:
        files = sorted(glob.glob(str(LOGS_DIR / pat)),
                       key=lambda f: os.path.getmtime(f), reverse=True)
        if not files:
            print(f"\n  {label}: no log files. v16.1 hasn't logged any yet.")
            continue
        newest = files[0]
        try:
            df = pd.read_parquet(newest)
        except Exception as e:
            print(f"\n  {label}: failed to read {newest}: {e}")
            continue
        # Stamp log_time from mtime if missing
        from pathlib import Path as _P
        if "log_time" not in df.columns:
            mtime = datetime.fromtimestamp(_P(newest).stat().st_mtime, tz=timezone.utc)
            df["log_time"] = mtime
        print(f"\n  {label} log: {Path(newest).name} ({len(df)} signals)")
        sample = df.head(5)
        for _, row in sample.iterrows():
            r = _trace_replay_one(row, replay_fn)
            print(f"    {r['ticker']:<10} status={r['status']:<10} "
                  f"{r.get('reason') or r.get('outcome', '')}")

    # ---- 6. Summary verdict ----
    print()
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    sh_files = glob.glob(str(LOGS_DIR / "shares_signals_*.parquet"))
    fu_files = glob.glob(str(LOGS_DIR / "futures_signals_*.parquet"))
    if not sh_files:
        print("→ shares_long stuck at 0: no shares_signals_*.parquet logs exist yet.")
        print("  Either v16.1 not deployed, or top_sh has been empty every cycle.")
    elif (DATA_DIR / "forward_outcomes_shares_long.parquet").exists():
        df = pd.read_parquet(DATA_DIR / "forward_outcomes_shares_long.parquet")
        if df.empty:
            print(f"→ shares_long stuck at 0: {len(sh_files)} log files exist, but replay produced 0 outcomes.")
            print("  See deep-dive above for why each replay failed.")
        else:
            print(f"→ shares_long: {len(df)} outcomes replayed. Should appear in next dashboard.")
    else:
        print(f"→ shares_long: {len(sh_files)} log files but no outcomes file yet — replay hasn't run.")

    if not fu_files:
        print("→ futures_*: no futures_signals_*.parquet logs. v16+ not running, or skip-futures on.")
    else:
        any_outcomes = any((DATA_DIR / f"forward_outcomes_futures_{k}.parquet").exists()
                           and len(pd.read_parquet(DATA_DIR / f"forward_outcomes_futures_{k}.parquet")) > 0
                           for k in ["equity","treasury","metal","energy","crypto","currency","agri"])
        if any_outcomes:
            print("→ futures_*: outcomes exist. Check Self-Learning panel for which buckets.")
        else:
            print(f"→ futures_*: {len(fu_files)} log files but 0 replayed outcomes.")
            print("  See deep-dive above — most likely 'too fresh' (<1.2h old) if just deployed.")

    print()


if __name__ == "__main__":
    diagnose()
