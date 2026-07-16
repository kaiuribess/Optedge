# Purpose: Options mispricing engine.
"""Options mispricing engine.

For each ticker:
  1. Pull options chain via data_provider (uses curl_cffi + caching).
  2. Get spot, dividend yield, historical vol (HV30, HV60, HV252).
  3. For each contract:
       - solve implied vol from market mid
       - compute theoretical price using HV30 as fair vol
       - compute mispricing in $ and σ terms
       - compute IV rank, liquidity & spread metrics
  4. Detect skew (25-delta put IV vs 25-delta call IV).
  5. Detect term-structure slope.
"""

from __future__ import annotations

import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider  # noqa: E402
from config import (  # noqa: E402
    HESTON_ENABLED,
    MAX_BID_ASK_SPREAD_PCT,
    MAX_DTE,
    MIN_DAILY_VOLUME,
    MIN_DTE,
    MIN_OPEN_INTEREST,
    MIN_OPTION_PRICE,
    RISK_FREE_RATE_DEFAULT,
    WORKERS_MISPRICING,
)
from utils import bs_delta, bs_implied_vol, bs_price  # noqa: E402

# v20.3/v20.4: multi-model vectorized pricing ensemble
try:
    from pricing_models import (
        all_models_vec,
        bs_price_vec,
        classify_vix_regime,
        ensemble_theo_vec,
        load_weights,
    )

    HAVE_ENSEMBLE = True
except Exception:
    HAVE_ENSEMBLE = False

log = logging.getLogger("optedge.mispricing")


# -------- Helpers ------------------------------------------------------
def _historical_vol(close: pd.Series, n: int) -> float | None:
    if close is None or len(close) < n + 1:
        return None
    rets = np.log(close / close.shift(1)).dropna().tail(n)
    if rets.empty:
        return None
    return float(rets.std() * np.sqrt(252))


def _years_to_expiry(expiry_str: str, asof: datetime) -> float:
    try:
        exp = datetime.strptime(expiry_str, "%Y-%m-%d").replace(tzinfo=UTC)
        days = (exp - asof).total_seconds() / 86400
        return max(days / 365.25, 1 / 365.25)
    except Exception:
        return 0.0


def _fetch_blob(ticker: str) -> dict[str, Any]:
    """Pull spot, HV stats, dividend yield, and chain in DTE window."""
    chain_data = data_provider.get_options_chain(ticker)
    if not chain_data or not chain_data.get("expirations"):
        return {}

    spot = chain_data["spot"]
    div_yield = chain_data["div_yield"]

    # Historical vol — needs price history
    hist = data_provider.get_history(ticker, period="1y", interval="1d")
    close = hist["Close"] if not hist.empty else pd.Series(dtype=float)
    hv30 = _historical_vol(close, 30) or 0.30
    hv60 = _historical_vol(close, 60) or hv30
    hv252 = _historical_vol(close, 252) or hv30

    asof = datetime.now(UTC)
    chains = []
    for exp, df in chain_data["chains"].items():
        T = _years_to_expiry(exp, asof)
        dte = T * 365.25
        if dte < MIN_DTE or dte > MAX_DTE:
            continue
        df = df.copy()
        df["expiry"] = exp
        df["dte"] = dte
        df["T"] = T
        chains.append(df)

    if not chains:
        return {}

    chain_df = pd.concat(chains, ignore_index=True)
    return {
        "ticker": ticker,
        "spot": spot,
        "hv30": hv30,
        "hv60": hv60,
        "hv252": hv252,
        "div_yield": div_yield,
        "chain": chain_df,
    }


def _pricing_edges(
    mispricing_pct: np.ndarray, spread_pct: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return anomaly, long-buyer, and seller edges after round-trip spread.

    ``mispricing_pct`` is positive when market mid is above model value and
    negative when it is below model value. Absolute anomaly is useful for
    diagnostics, but only negative mispricing can create value for a long
    option buyer.
    """
    mispricing = np.asarray(mispricing_pct, dtype=float)
    spread = np.asarray(spread_pct, dtype=float)
    anomaly_edge = np.abs(mispricing) - spread
    buyer_edge = -mispricing - spread
    seller_edge = mispricing - spread
    return anomaly_edge, buyer_edge, seller_edge


def _enrich_chain(
    blob: dict[str, Any], r: float = RISK_FREE_RATE_DEFAULT, regime: str = "normal"
) -> pd.DataFrame:
    """Compute multi-model theoretical prices + IV + mispricing for an entire
    chain at once. v20.4: fully vectorized — BS, CRR (80 steps), BJS all run
    on numpy arrays over the filtered contracts. No per-row Python loops.

    Per-model theoretical values are logged for a quarantined current-mid
    diagnostic. They cannot update production ensemble weights."""
    if not blob:
        return pd.DataFrame()
    df = blob["chain"]
    spot = blob["spot"]
    q_scalar = float(blob["div_yield"])
    hv30 = blob["hv30"]
    weights = load_weights(regime) if HAVE_ENSEMBLE else None

    # OI+volume pre-filter — drop dead contracts BEFORE any pricing work
    if "openInterest" in df.columns and "volume" in df.columns:
        prefilter = (df["openInterest"].fillna(0) >= MIN_OPEN_INTEREST) & (
            df["volume"].fillna(0) >= MIN_DAILY_VOLUME
        )
        df = df[prefilter].copy()
    if df.empty:
        return pd.DataFrame()

    # ---- Build vectorized arrays ----
    bid_arr = pd.to_numeric(df.get("bid"), errors="coerce").fillna(0).to_numpy()
    ask_arr = pd.to_numeric(df.get("ask"), errors="coerce").fillna(0).to_numpy()
    last_arr = pd.to_numeric(df.get("lastPrice"), errors="coerce").fillna(0).to_numpy()
    K_arr = pd.to_numeric(df.get("strike"), errors="coerce").fillna(0).to_numpy()
    T_arr = pd.to_numeric(df.get("T"), errors="coerce").fillna(0).to_numpy()
    dte_arr = pd.to_numeric(df.get("dte"), errors="coerce").fillna(0).astype(int).to_numpy()
    oi_arr = pd.to_numeric(df.get("openInterest"), errors="coerce").fillna(0).astype(int).to_numpy()
    vol_arr = pd.to_numeric(df.get("volume"), errors="coerce").fillna(0).astype(int).to_numpy()
    side_arr = df["side"].to_numpy()
    call_mask = side_arr == "call"

    # Mid + spread
    valid_quote = (bid_arr > 0) & (ask_arr > 0) & (ask_arr >= bid_arr)
    mid_arr = np.where(valid_quote, (bid_arr + ask_arr) / 2.0, last_arr)
    spread_arr = np.where(
        valid_quote & (mid_arr > 0), (ask_arr - bid_arr) / np.where(mid_arr > 0, mid_arr, 1.0), 1.0
    )
    keep = (mid_arr >= MIN_OPTION_PRICE) & (T_arr > 0) & (K_arr > 0)
    # Drop dead rows before pricing
    if not keep.any():
        return pd.DataFrame()
    df = df.iloc[keep].reset_index(drop=True)
    bid_arr = bid_arr[keep]
    ask_arr = ask_arr[keep]
    last_arr = last_arr[keep]
    K_arr = K_arr[keep]
    T_arr = T_arr[keep]
    dte_arr = dte_arr[keep]
    oi_arr = oi_arr[keep]
    vol_arr = vol_arr[keep]
    side_arr = side_arr[keep]
    call_mask = call_mask[keep]
    mid_arr = mid_arr[keep]
    spread_arr = spread_arr[keep]

    N = len(df)
    S_arr = np.full(N, spot)
    q_arr = np.full(N, q_scalar)
    # Use a flat HV30 as fair vol across all strikes (per-strike IV is computed below)
    fair_vol_arr = np.full(N, hv30)

    # ---- Implied vol per contract (still per-row; brentq is the bottleneck) ----
    iv_market_arr = np.zeros(N)
    for j in range(N):
        iv = bs_implied_vol(
            mid_arr[j], spot, K_arr[j], T_arr[j], r, q_scalar, call=bool(call_mask[j])
        )
        iv_market_arr[j] = iv if iv and iv > 0 else np.nan

    # Drop rows where IV is unsolvable (arbitrage / stale data)
    keep2 = ~np.isnan(iv_market_arr)
    if not keep2.any():
        return pd.DataFrame()
    df = df.iloc[keep2].reset_index(drop=True)
    bid_arr = bid_arr[keep2]
    ask_arr = ask_arr[keep2]
    last_arr = last_arr[keep2]
    K_arr = K_arr[keep2]
    T_arr = T_arr[keep2]
    dte_arr = dte_arr[keep2]
    oi_arr = oi_arr[keep2]
    vol_arr = vol_arr[keep2]
    side_arr = side_arr[keep2]
    call_mask = call_mask[keep2]
    mid_arr = mid_arr[keep2]
    spread_arr = spread_arr[keep2]
    iv_market_arr = iv_market_arr[keep2]
    S_arr = S_arr[keep2]
    q_arr = q_arr[keep2]
    fair_vol_arr = fair_vol_arr[keep2]

    # ---- v20.5: per-expiry smile-smoothed fair vol ----
    # Use a robust median of high-quality contracts (tight spread, decent OI)
    # within each expiry as the "fair vol" input for BS/CRR/BJS. This closes
    # most of the gap with CBOE's surface-aware theo. Falls back to HV30 when
    # the expiry has too few high-quality contracts to fit reliably.
    exp_arr = df["expiry"].to_numpy() if "expiry" in df.columns else None
    if exp_arr is not None:
        # Quality mask: tight spread + meaningful OI + reasonable IV range
        quality_mask = (
            (spread_arr <= 0.20)
            & (oi_arr >= 200)
            & (iv_market_arr >= 0.05)
            & (iv_market_arr <= 3.0)
        )
        # Per-expiry median IV (robust to outliers)
        per_exp_iv: dict[str, float] = {}
        unique_exps = np.unique(exp_arr)
        for exp in unique_exps:
            mask = (exp_arr == exp) & quality_mask
            if mask.sum() >= 6:  # need at least 6 quality contracts
                per_exp_iv[exp] = float(np.median(iv_market_arr[mask]))
        # Apply: fair_vol_arr[i] = per-expiry median IV (fallback HV30)
        fair_vol_arr = np.array([per_exp_iv.get(str(exp_arr[j]), hv30) for j in range(len(df))])
        # Sanity floor/ceiling
        fair_vol_arr = np.clip(fair_vol_arr, 0.05, 3.0)

    # CBOE-provided theos (when CBOE was the chain source)
    cboe_arr = None
    if "theo" in df.columns:
        cboe_arr = pd.to_numeric(df["theo"], errors="coerce").to_numpy()

    # ---- Vectorized multi-model pricing ----
    if HAVE_ENSEMBLE:
        models = {"bs", "crr", "bjs", "cboe"}
        if HESTON_ENABLED:
            models.add("heston")
        per_model = all_models_vec(
            S_arr,
            K_arr,
            T_arr,
            r,
            fair_vol_arr,
            q_arr,
            call_mask,
            cboe_theo=cboe_arr,
            crr_steps=80,
            models=models,
        )
        theo_arr = ensemble_theo_vec(per_model, weights)
        theo_bs_arr = per_model.get("bs", np.full(len(df), np.nan))
        theo_crr_arr = per_model.get("crr", np.full(len(df), np.nan))
        theo_bjs_arr = per_model.get("bjs", np.full(len(df), np.nan))
        theo_cboe_arr = per_model.get("cboe", np.full(len(df), np.nan))
    else:
        theo_bs_arr = (
            bs_price_vec(S_arr, K_arr, T_arr, r, fair_vol_arr, q_arr, call_mask)
            if HAVE_ENSEMBLE
            else np.array(
                [
                    bs_price(
                        spot,
                        K_arr[j],
                        T_arr[j],
                        r,
                        fair_vol_arr[j],
                        q_scalar,
                        call=bool(call_mask[j]),
                    )
                    for j in range(len(df))
                ]
            )
        )
        theo_arr = theo_bs_arr
        theo_crr_arr = np.full(len(df), np.nan)
        theo_bjs_arr = np.full(len(df), np.nan)
        theo_cboe_arr = np.full(len(df), np.nan)

    # Fallback to BS if ensemble fails for any row
    theo_arr = np.where(np.isfinite(theo_arr) & (theo_arr > 0), theo_arr, theo_bs_arr)

    # Greeks: prefer source-provided (CBOE) when present
    delta_src = (
        pd.to_numeric(df.get("delta"), errors="coerce").fillna(0).to_numpy()
        if "delta" in df.columns
        else np.zeros(len(df))
    )
    # Compute BS delta where source delta is zero/missing
    delta_bs_arr = np.array(
        [
            bs_delta(
                spot, K_arr[j], T_arr[j], r, iv_market_arr[j], q_scalar, call=bool(call_mask[j])
            )
            for j in range(len(df))
        ]
    )
    delta_arr = np.where(delta_src != 0, delta_src, delta_bs_arr)

    mispricing_dollar_arr = mid_arr - theo_arr
    mispricing_pct_arr = np.where(theo_arr > 0, (mid_arr - theo_arr) / theo_arr, 0.0)
    # v20.7 — net_edge accounts for half the bid-ask spread on each side of
    # the trade. A 5% mispricing on a contract with a 6% spread is NOT tradable.
    # net_edge_pct keeps the absolute anomaly for backward-compatible telemetry.
    # buyer/seller edges below preserve direction after the full spread.
    net_edge_pct_arr, buyer_edge_pct_arr, seller_edge_pct_arr = _pricing_edges(
        mispricing_pct_arr,
        spread_arr,
    )
    pricing_direction_arr = np.where(
        buyer_edge_pct_arr > 0,
        "underpriced_after_spread",
        np.where(seller_edge_pct_arr > 0, "overpriced_after_spread", "inside_spread"),
    )
    vol_premium_arr = iv_market_arr - fair_vol_arr

    # Source-provided Greeks (CBOE) — keep raw values for the dashboard
    gamma_src = (
        pd.to_numeric(df.get("gamma"), errors="coerce").fillna(0).to_numpy()
        if "gamma" in df.columns
        else np.zeros(len(df))
    )
    theta_src = (
        pd.to_numeric(df.get("theta"), errors="coerce").fillna(0).to_numpy()
        if "theta" in df.columns
        else np.zeros(len(df))
    )
    vega_src = (
        pd.to_numeric(df.get("vega"), errors="coerce").fillna(0).to_numpy()
        if "vega" in df.columns
        else np.zeros(len(df))
    )

    out = pd.DataFrame(
        {
            "ticker": blob["ticker"],
            "expiry": df["expiry"].to_numpy(),
            "dte": dte_arr,
            "side": side_arr,
            "strike": K_arr,
            "spot": S_arr,
            "bid": bid_arr,
            "ask": ask_arr,
            "mid": mid_arr,
            "last": last_arr,
            "spread_pct": spread_arr,
            "open_interest": oi_arr,
            "volume": vol_arr,
            "iv_market": iv_market_arr,
            "fair_vol": fair_vol_arr,
            "vol_premium": vol_premium_arr,
            "theo_price": theo_arr,
            "theo_bs": theo_bs_arr,
            "theo_crr": theo_crr_arr,
            "theo_bjs": theo_bjs_arr,
            "theo_cboe": theo_cboe_arr,
            "mispricing_dollar": mispricing_dollar_arr,
            "mispricing_pct": mispricing_pct_arr,
            "net_edge_pct": net_edge_pct_arr,
            "buyer_edge_pct": buyer_edge_pct_arr,
            "seller_edge_pct": seller_edge_pct_arr,
            "pricing_direction": pricing_direction_arr,
            "delta": delta_arr,
            "gamma_src": gamma_src,
            "theta_src": theta_src,
            "vega_src": vega_src,
            "moneyness": K_arr / np.where(S_arr > 0, S_arr, 1.0),
            "chain_source": blob.get("source", "unknown"),
            "quote_quality": blob.get("quote_quality", "free_or_delayed"),
            "regime": regime,
        }
    )
    return out


def _apply_filters(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    f = df[
        (df["open_interest"] >= MIN_OPEN_INTEREST)
        & (df["volume"] >= MIN_DAILY_VOLUME)
        & (df["spread_pct"] <= MAX_BID_ASK_SPREAD_PCT)
        & (df["mid"] >= MIN_OPTION_PRICE)
    ].copy()
    return f


def _per_ticker_summary(df: pd.DataFrame, blob: dict[str, Any]) -> dict[str, Any]:
    if df.empty:
        return {}

    # Skew: 25-delta put vs 25-delta call IV (front-month)
    front = df[df["dte"] == df["dte"].min()].copy()
    skew_25d = None
    if not front.empty:
        calls = front[front["side"] == "call"]
        puts = front[front["side"] == "put"]
        if not calls.empty and not puts.empty:
            call25 = calls.iloc[(calls["delta"] - 0.25).abs().argsort()[:1]]
            put25 = puts.iloc[(puts["delta"] + 0.25).abs().argsort()[:1]]
            if not call25.empty and not put25.empty:
                skew_25d = float(put25["iv_market"].iloc[0] - call25["iv_market"].iloc[0])

    # Term structure: front ATM IV vs back ATM IV
    term_slope = None
    front_atm = front[(front["side"] == "call")]
    if not front_atm.empty:
        front_atm = front_atm.iloc[(front_atm["moneyness"] - 1).abs().argsort()[:1]]
    back = df[df["dte"] == df["dte"].max()]
    back_atm = back[back["side"] == "call"]
    if not back_atm.empty:
        back_atm = back_atm.iloc[(back_atm["moneyness"] - 1).abs().argsort()[:1]]
    if not front_atm.empty and not back_atm.empty:
        term_slope = float(back_atm["iv_market"].iloc[0] - front_atm["iv_market"].iloc[0])

    iv_rank = 50.0
    hv30 = blob["hv30"]
    hv252 = blob["hv252"]
    if hv252 > 0:
        ratio = hv30 / hv252
        iv_rank = float(min(100, max(0, 50 + (ratio - 1) * 100)))

    return {
        "ticker": blob["ticker"],
        "spot": blob["spot"],
        "hv30": blob["hv30"],
        "hv252": blob["hv252"],
        "iv_rank": iv_rank,
        "skew_25d": skew_25d,
        "term_slope": term_slope,
    }


def _detect_regime() -> str:
    """Detect VIX regime by reading the most recent VIX close. Falls back
    to 'normal' if VIX history isn't reachable."""
    if not HAVE_ENSEMBLE:
        return "normal"
    try:
        h = data_provider.get_history("^VIX", period="5d", cache_age=3600)
        if h is None or h.empty:
            return "normal"
        vix = float(h["Close"].iloc[-1])
        return classify_vix_regime(vix)
    except Exception:
        return "normal"


def _process_ticker(t: str, regime: str = "normal") -> dict[str, Any]:
    """One ticker → {filtered_contracts: DataFrame|None, summary: dict|None}."""
    try:
        blob = _fetch_blob(t)
        if not blob:
            return {"filtered": None, "summary": None}
        enriched = _enrich_chain(blob, regime=regime)
        filtered = _apply_filters(enriched)
        return {
            "filtered": filtered if not filtered.empty else None,
            "summary": _per_ticker_summary(enriched, blob),
        }
    except Exception as e:
        log.warning("mispricing fail %s: %s", t, str(e)[:120])
        return {"filtered": None, "summary": None, "error": str(e)[:120]}


def _log_model_predictions(contracts: pd.DataFrame) -> None:
    """Log per-model predictions for a compute-only current-mid diagnostic.

    The diagnostic is intentionally ineligible to update production weights;
    fixed-horizon, out-of-sample evidence is required for model promotion.
    """
    if contracts is None or contracts.empty:
        return
    cols_needed = [
        "ticker",
        "expiry",
        "strike",
        "side",
        "spot",
        "mid",
        "theo_bs",
        "theo_crr",
        "theo_bjs",
        "theo_cboe",
        "regime",
        "chain_source",
        "quote_quality",
    ]
    cols = [c for c in cols_needed if c in contracts.columns]
    if not cols:
        return
    sub = contracts[cols].copy()
    sub["asof"] = datetime.now(UTC).isoformat()
    log_dir = Path(__file__).resolve().parent.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    parquet_path = log_dir / f"model_predictions_{stamp}.parquet"
    json_path = log_dir / f"model_predictions_{stamp}.json"
    try:
        sub.to_parquet(parquet_path, index=False)
        log.info("logged %d model predictions to %s", len(sub), parquet_path.name)
        return
    except Exception as e:
        log.debug("model predictions parquet failed (%s) — falling back to json", e)
    try:
        sub.to_json(json_path, orient="records", lines=False)
        log.info("logged %d model predictions to %s", len(sub), json_path.name)
    except Exception as e:
        log.debug("model predictions json log fail: %s", e)


def run(universe: list[str], max_workers: int = None) -> dict[str, Any]:
    """Parallel per-ticker processing. yfinance is rate-sensitive, so keep
    workers low (default 6).

    Detects the VIX regime, runs the multi-model pricing ensemble in
    ``_enrich_chain``, and logs per-model values for quarantined diagnostics.
    The diagnostic output never changes the production ensemble.
    """
    workers = max_workers or WORKERS_MISPRICING
    regime = _detect_regime()
    if HAVE_ENSEMBLE:
        w = load_weights(regime)
        log.info(
            "mispricing: regime=%s ensemble weights=%s",
            regime,
            {k: round(v, 2) for k, v in w.items()},
        )
    rows = []
    summaries = []
    failures = 0
    completed = 0
    log.info("fetching chains for %d tickers (parallel, %d workers)", len(universe), workers)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_process_ticker, t, regime): t for t in universe}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                r = fut.result()
                if r.get("error"):
                    failures += 1
                if r.get("filtered") is not None:
                    rows.append(r["filtered"])
                if r.get("summary"):
                    summaries.append(r["summary"])
            except Exception as e:
                failures += 1
                log.warning("mispricing worker fail %s: %s", t, str(e)[:120])
            completed += 1
            if completed % 25 == 0 or completed == len(universe):
                log.info("[%d/%d]", completed, len(universe))

    if failures > len(universe) * 0.5:
        log.error(
            "MORE THAN HALF the tickers failed (%d/%d). Yahoo may be rate-limiting "
            "your IP. Wait 15 min and retry, or use --demo, or run setup_check.py.",
            failures,
            len(universe),
        )

    contracts = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    summary = pd.DataFrame([s for s in summaries if s])
    # v20.3: log per-model predictions for accuracy tracking (sample to cap I/O)
    if HAVE_ENSEMBLE and not contracts.empty:
        # Keep only the most actionable contracts (decent OI/volume) to avoid
        # logging hundreds of thousands of stale strikes each iter
        sub = (
            contracts[
                (contracts.get("open_interest", 0) >= 100) & (contracts.get("volume", 0) >= 10)
            ].copy()
            if "open_interest" in contracts.columns
            else contracts
        )
        if len(sub) > 5000:
            sub = sub.nlargest(5000, "open_interest")
        _log_model_predictions(sub)
    return {"contracts": contracts, "summary": summary}
