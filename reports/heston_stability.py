# Purpose: Compare Heston pricing stability with Black-Scholes.
"""Validate Heston pricing stability before enabling it in production."""

from __future__ import annotations

import glob
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
OUT_JSON = DATA_DIR / "heston_stability.json"


def _latest_contracts(max_rows: int = 1500) -> pd.DataFrame:
    files = sorted(glob.glob(str(DATA_DIR / "contracts_*.parquet")))
    if not files:
        return pd.DataFrame()
    for fp in reversed(files):
        try:
            df = pd.read_parquet(fp)
            if not df.empty:
                return df.head(max_rows).copy()
        except Exception:
            continue
    return pd.DataFrame()


def _synthetic_contracts(n: int = 500) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    spot = rng.uniform(20, 500, n)
    return pd.DataFrame(
        {
            "spot": spot,
            "strike": spot * rng.uniform(0.75, 1.25, n),
            "dte": rng.integers(14, 90, n),
            "iv_market": rng.uniform(0.15, 0.9, n),
            "side": np.where(rng.random(n) > 0.5, "call", "put"),
        }
    )


def build_report(max_rows: int = 1500) -> dict[str, Any]:
    from config import RISK_FREE_RATE_DEFAULT
    from pricing_models import bs_price_vec, heston_price_vec

    df = _latest_contracts(max_rows=max_rows)
    source = "latest_contracts"
    if df.empty:
        df = _synthetic_contracts()
        source = "synthetic"

    required = {"spot", "strike", "dte", "iv_market", "side"}
    missing = sorted(required - set(df.columns))
    if missing:
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "source": source,
            "ok": False,
            "reason": f"missing columns: {', '.join(missing)}",
        }

    spot = pd.to_numeric(df["spot"], errors="coerce").to_numpy(dtype=float)
    strike = pd.to_numeric(df["strike"], errors="coerce").to_numpy(dtype=float)
    dte = pd.to_numeric(df["dte"], errors="coerce").to_numpy(dtype=float)
    sigma = pd.to_numeric(df["iv_market"], errors="coerce").to_numpy(dtype=float)
    side = df["side"].astype(str).str.lower().to_numpy()
    mask = np.isfinite(spot) & np.isfinite(strike) & np.isfinite(dte) & np.isfinite(sigma)
    mask &= (spot > 0) & (strike > 0) & (dte > 0) & (sigma > 0)
    spot, strike, dte, sigma, side = spot[mask], strike[mask], dte[mask], sigma[mask], side[mask]
    if len(spot) == 0:
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "source": source,
            "ok": False,
            "reason": "no valid contracts",
        }

    t = np.clip(dte / 365.25, 1 / 365.25, 3.0)
    q = np.zeros_like(spot)
    call_mask = side == "call"
    heston = heston_price_vec(spot, strike, t, RISK_FREE_RATE_DEFAULT, sigma, q, call_mask)
    bs = bs_price_vec(spot, strike, t, RISK_FREE_RATE_DEFAULT, sigma, q, call_mask)

    finite = np.isfinite(heston)
    nonnegative = finite & (heston >= 0)
    ratio = np.where((bs > 0) & finite, heston / bs, np.nan)
    extreme = np.isfinite(ratio) & ((ratio < 0.05) | (ratio > 20.0))
    ok_rate = float(nonnegative.mean()) if len(heston) else 0.0
    extreme_rate = float(extreme.mean()) if len(heston) else 1.0
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": source,
        "contracts_checked": int(len(heston)),
        "finite_rate": float(finite.mean()),
        "nonnegative_rate": ok_rate,
        "extreme_vs_bs_rate": extreme_rate,
        "median_heston_to_bs": float(np.nanmedian(ratio)) if np.isfinite(ratio).any() else None,
        "p05_heston_to_bs": float(np.nanpercentile(ratio, 5)) if np.isfinite(ratio).any() else None,
        "p95_heston_to_bs": float(np.nanpercentile(ratio, 95))
        if np.isfinite(ratio).any()
        else None,
        "ok": bool(ok_rate >= 0.995 and extreme_rate <= 0.02),
    }
    if not report["ok"]:
        report["reason"] = "Heston is not stable enough to enable by default."
    return report


def write_report(max_rows: int = 1500) -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    report = build_report(max_rows=max_rows)
    OUT_JSON.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def main() -> int:
    report = write_report()
    print(f"Heston stability: {OUT_JSON}")
    print(f"checked={report.get('contracts_checked', 0)} ok={report.get('ok')}")
    if report.get("reason"):
        print(report["reason"])
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
