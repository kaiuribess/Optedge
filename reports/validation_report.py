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

import base64
import argparse
import glob
import html
import json
import math
import random
import sys
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
REPORT_HTML = DATA_DIR / "validation_report.html"
SUMMARY_JSON = DATA_DIR / "validation_summary.json"
EQUITY_PNG = DATA_DIR / "equity_curve.png"

MIN_CLOSED_SIGNALS = 500
BREAKEVEN_WIN_RATE = 0.50


def _model_update_cutoff() -> Optional[pd.Timestamp]:
    candidates = [
        ROOT / "config_runtime.py",
        DATA_DIR / "model_weights.json",
        DATA_DIR / "predictor_coefs.json",
    ]
    mtimes = []
    for path in candidates:
        if path.exists():
            mtimes.append(pd.Timestamp(path.stat().st_mtime, unit="s", tz="UTC"))
    return max(mtimes) if mtimes else None


def _filter_since(df: pd.DataFrame, since: Optional[pd.Timestamp], date_col: str = "entry_time") -> pd.DataFrame:
    if df is None or df.empty or since is None or pd.isna(since) or date_col not in df.columns:
        return df
    return df[pd.to_datetime(df[date_col], errors="coerce", utc=True) >= since].copy()


def _read_json_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


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
        shares["asset"] = "shares"
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
    open_df = pd.DataFrame(_read_json_rows(DATA_DIR / "open_positions.json"))
    closed_df = pd.DataFrame(_read_json_rows(DATA_DIR / "closed_positions.json"))
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
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


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
        "max_drawdown": _max_drawdown(r),
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
    side = out.get("side", pd.Series("", index=out.index)).astype(str).str.lower()
    out["pnl_pct_after_slippage"] = _num(out.get("pnl_pct", pd.Series(np.nan, index=out.index)))
    out.loc[side.isin(["call", "put"]), "pnl_pct_after_slippage"] -= slippage
    return out


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
        # 1x1 transparent PNG fallback.
        path.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/axnP7sAAAAASUVORK5CYII="
        ))
        return
    curve = closed.copy()
    sort_col = "exit_time" if "exit_time" in curve.columns else "entry_time"
    if sort_col in curve.columns:
        curve = curve.sort_values(sort_col)
    r = _num(curve["pnl_pct_after_slippage"]).fillna(0.0)
    equity = (1.0 + r).cumprod()
    try:
        import matplotlib.pyplot as plt

        plt.figure(figsize=(9, 4.5))
        plt.plot(range(1, len(equity) + 1), equity.values, linewidth=2.0, color="#2563eb")
        plt.axhline(1.0, color="#64748b", linewidth=1.0, linestyle="--")
        plt.title("Optedge Closed-Signal Equity Curve")
        plt.xlabel("Closed signal")
        plt.ylabel("Equity multiple")
        plt.grid(True, alpha=0.25)
        plt.tight_layout()
        plt.savefig(path, dpi=140)
        plt.close()
    except Exception:
        path.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/axnP7sAAAAASUVORK5CYII="
        ))


def build_summary(scope: str = "current_model", since: Optional[str] = None) -> Dict[str, Any]:
    logs = load_signal_logs()
    open_df, closed_raw = load_positions()
    closed = _closed_with_slippage(closed_raw)
    all_time_closed = closed.copy()

    cutoff = pd.to_datetime(since, errors="coerce", utc=True) if since else None
    if scope == "current_model" and cutoff is None:
        cutoff = _model_update_cutoff()
    if scope != "all_time":
        logs = _filter_since(logs, cutoff)
        open_df = _filter_since(open_df, cutoff)
        closed = _filter_since(closed, cutoff)

    total_signals = int(len(logs))
    closed_count = int(len(closed))
    open_count = int(len(open_df))
    overall = _stats(closed)
    after_slippage = _stats(closed, "pnl_pct_after_slippage")

    warnings = []
    if scope != "all_time" and cutoff is not None:
        stale_excluded = max(0, len(all_time_closed) - len(closed))
        if stale_excluded:
            warnings.append(
                f"Excluded {stale_excluded} older closed positions from the primary metrics because they predate the current model era."
            )
    elif scope != "all_time":
        warnings.append("No model-update timestamp found; validation scope could not isolate the current model era.")
    if closed_count < MIN_CLOSED_SIGNALS:
        warnings.append(
            f"Sample size too small: {closed_count} closed signals; need at least {MIN_CLOSED_SIGNALS}."
        )
    if overall.get("max_drawdown") is not None and overall["max_drawdown"] < -0.20:
        warnings.append(f"Max drawdown is worse than -20%: {_fmt_pct(overall['max_drawdown'])}.")
    if overall.get("win_rate") is not None and overall["win_rate"] < BREAKEVEN_WIN_RATE:
        warnings.append(f"Win rate is below the simple breakeven threshold: {_fmt_pct(overall['win_rate'])}.")

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "validation_scope": scope,
        "current_model_cutoff": cutoff.isoformat() if cutoff is not None and not pd.isna(cutoff) else None,
        "all_time_closed_positions": int(len(all_time_closed)),
        "stale_closed_positions_excluded": int(max(0, len(all_time_closed) - len(closed))),
        "total_signals": total_signals,
        "closed_positions": closed_count,
        "open_positions": open_count,
        "overall": overall,
        "all_time_overall": _stats(all_time_closed),
        "after_slippage": after_slippage,
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
        "benchmarks": _benchmark_comparison(closed),
        "random_baseline": _random_baseline(_num(closed.get("pnl_pct", pd.Series(dtype=float))).dropna()),
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


def render_html(summary: Dict[str, Any]) -> str:
    overall = summary["overall"]
    slip = summary["after_slippage"]
    bench = summary["benchmarks"]
    baseline = summary["random_baseline"]
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
section {{ background: white; border: 1px solid #e2e8f0; border-radius: 8px; padding: 18px; box-shadow: 0 1px 2px rgba(15,23,42,.04); }}
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
  <div class="muted">Scope: {html.escape(str(summary.get("validation_scope", "current_model")))}; cutoff: {html.escape(str(summary.get("current_model_cutoff") or "n/a"))}</div>

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
    <h2>Equity Curve</h2>
    <img src="equity_curve.png" alt="Optedge equity curve">
  </section>

  <div class="grid">
    <section><h2>Calls vs Puts</h2>{_bucket_table(summary["calls_vs_puts"])}</section>
    <section><h2>DTE Buckets</h2>{_bucket_table(summary["dte_buckets"])}</section>
    <section><h2>Spread Buckets</h2>{_bucket_table(summary["spread_buckets"])}</section>
    <section><h2>Confidence Buckets</h2>{_bucket_table(summary["confidence_buckets"])}</section>
  </div>
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
    SUMMARY_JSON.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    REPORT_HTML.write_text(render_html(summary), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Optedge validation report")
    parser.add_argument("--all-time", action="store_true",
                        help="Use every historical closed position instead of the current model era")
    parser.add_argument("--since", default=None,
                        help="ISO date/time cutoff for primary validation metrics")
    args = parser.parse_args()
    summary = write_report(scope="all_time" if args.all_time else "current_model", since=args.since)
    print(f"Validation report: {REPORT_HTML}")
    print(f"Validation summary: {SUMMARY_JSON}")
    print(f"Equity curve: {EQUITY_PNG}")
    if summary.get("warnings"):
        print("\nWarnings:")
        for warning in summary["warnings"]:
            print(f"  - {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
