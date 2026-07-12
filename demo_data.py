"""Synthetic but realistic data provider for sandbox demos.

When live data sources (Yahoo Finance, Reddit) are blocked, this module
generates plausible options chains, sentiment, fundamentals, and macro
inputs so the full fusion pipeline can be exercised end-to-end.

Calibrated to roughly Q2 2026 market levels. Returns the SAME schemas
as the real engines so fusion.rank.fuse() works without modification.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List
import math
import random

import pandas as pd

from utils import bs_price


# Plausible ~late April 2026 spot prices and base implied vol levels.
# Base IV is what the model treats as "fair vol" (HV30); we then synthesize a
# market IV with skew, term-structure, and noise so the engine has signals.
TICKER_PROFILES: Dict[str, Dict[str, float]] = {
    # Mega caps — mid-teens IV
    "SPY":   {"spot": 558.0, "iv": 0.13, "fund": 0.4, "div": 0.013},
    "QQQ":   {"spot": 482.0, "iv": 0.18, "fund": 0.5, "div": 0.005},
    "IWM":   {"spot": 215.0, "iv": 0.22, "fund": 0.0, "div": 0.013},
    "AAPL":  {"spot": 218.0, "iv": 0.22, "fund": 0.3, "div": 0.005},
    "MSFT":  {"spot": 442.0, "iv": 0.24, "fund": 0.7, "div": 0.007},
    "NVDA":  {"spot": 138.0, "iv": 0.45, "fund": 1.4, "div": 0.001},
    "AMZN":  {"spot": 192.0, "iv": 0.30, "fund": 0.6, "div": 0.0},
    "GOOGL": {"spot": 174.0, "iv": 0.26, "fund": 0.5, "div": 0.005},
    "META":  {"spot": 558.0, "iv": 0.30, "fund": 0.9, "div": 0.004},
    "TSLA":  {"spot": 282.0, "iv": 0.55, "fund": -0.2, "div": 0.0},
    "AMD":   {"spot": 165.0, "iv": 0.46, "fund": 0.4, "div": 0.0},
    "AVGO":  {"spot": 178.0, "iv": 0.32, "fund": 0.6, "div": 0.012},
    "NFLX":  {"spot": 695.0, "iv": 0.32, "fund": 0.5, "div": 0.0},
    "ORCL":  {"spot": 165.0, "iv": 0.28, "fund": 0.4, "div": 0.012},
    "JPM":   {"spot": 218.0, "iv": 0.21, "fund": 0.3, "div": 0.024},
    "BAC":   {"spot": 42.0,  "iv": 0.24, "fund": 0.0, "div": 0.025},
    "WFC":   {"spot": 64.0,  "iv": 0.25, "fund": 0.1, "div": 0.022},
    "GS":    {"spot": 528.0, "iv": 0.24, "fund": 0.4, "div": 0.022},
    "XOM":   {"spot": 118.0, "iv": 0.22, "fund": 0.3, "div": 0.033},
    "CVX":   {"spot": 158.0, "iv": 0.21, "fund": 0.2, "div": 0.041},
    "V":     {"spot": 285.0, "iv": 0.18, "fund": 0.6, "div": 0.007},
    "MA":    {"spot": 478.0, "iv": 0.18, "fund": 0.6, "div": 0.005},
    "DIS":   {"spot": 95.0,  "iv": 0.30, "fund": -0.1, "div": 0.012},
    "BA":    {"spot": 175.0, "iv": 0.42, "fund": -0.7, "div": 0.0},
    "WMT":   {"spot": 82.0,  "iv": 0.18, "fund": 0.4, "div": 0.012},
    "COST":  {"spot": 875.0, "iv": 0.21, "fund": 0.6, "div": 0.005},
    "UNH":   {"spot": 542.0, "iv": 0.30, "fund": 0.2, "div": 0.014},
    "LLY":   {"spot": 808.0, "iv": 0.32, "fund": 1.1, "div": 0.007},
    "JNJ":   {"spot": 158.0, "iv": 0.18, "fund": 0.2, "div": 0.031},
    "PFE":   {"spot": 27.0,  "iv": 0.26, "fund": -0.4, "div": 0.063},
    # Small/mid caps — higher IV, more dispersion
    "PLTR":  {"spot": 28.0,  "iv": 0.65, "fund": 0.6, "div": 0.0},
    "SOFI":  {"spot": 9.5,   "iv": 0.62, "fund": 0.3, "div": 0.0},
    "HOOD":  {"spot": 32.0,  "iv": 0.60, "fund": 0.4, "div": 0.0},
    "COIN":  {"spot": 218.0, "iv": 0.78, "fund": 0.2, "div": 0.0},
    "RIVN":  {"spot": 12.5,  "iv": 0.85, "fund": -1.4, "div": 0.0},
    "MARA":  {"spot": 18.0,  "iv": 0.95, "fund": -0.6, "div": 0.0},
    "RIOT":  {"spot": 9.5,   "iv": 0.92, "fund": -0.7, "div": 0.0},
    "CLSK":  {"spot": 8.0,   "iv": 0.98, "fund": -0.5, "div": 0.0},
    "MSTR":  {"spot": 215.0, "iv": 0.85, "fund": -0.3, "div": 0.0},
    "RBLX":  {"spot": 42.0,  "iv": 0.50, "fund": -0.2, "div": 0.0},
    "U":     {"spot": 22.0,  "iv": 0.62, "fund": -0.5, "div": 0.0},
    "AFRM":  {"spot": 48.0,  "iv": 0.70, "fund": 0.2, "div": 0.0},
    "UPST":  {"spot": 32.0,  "iv": 0.80, "fund": -0.4, "div": 0.0},
    "SMCI":  {"spot": 38.0,  "iv": 0.75, "fund": 0.5, "div": 0.0},
    "IONQ":  {"spot": 18.0,  "iv": 1.00, "fund": -1.0, "div": 0.0},
    "RGTI":  {"spot": 9.0,   "iv": 1.05, "fund": -1.2, "div": 0.0},
    "AI":    {"spot": 22.0,  "iv": 0.85, "fund": -0.6, "div": 0.0},
    "PATH":  {"spot": 11.0,  "iv": 0.62, "fund": -0.2, "div": 0.0},
    "ASTS":  {"spot": 22.0,  "iv": 0.95, "fund": -0.8, "div": 0.0},
    "JOBY":  {"spot": 6.5,   "iv": 0.88, "fund": -1.1, "div": 0.0},
    "ACHR":  {"spot": 8.0,   "iv": 0.95, "fund": -1.0, "div": 0.0},
    "LUNR":  {"spot": 9.0,   "iv": 1.00, "fund": -0.9, "div": 0.0},
    "TLRY":  {"spot": 1.4,   "iv": 0.85, "fund": -0.7, "div": 0.0},
    "CGC":   {"spot": 2.1,   "iv": 0.90, "fund": -0.8, "div": 0.0},
    "GME":   {"spot": 23.0,  "iv": 0.75, "fund": -0.4, "div": 0.0},
    "AMC":   {"spot": 3.5,   "iv": 0.95, "fund": -1.1, "div": 0.0},
    "F":     {"spot": 11.0,  "iv": 0.32, "fund": 0.0, "div": 0.054},
    "GM":    {"spot": 48.0,  "iv": 0.30, "fund": 0.2, "div": 0.010},
    "NIO":   {"spot": 4.5,   "iv": 0.78, "fund": -0.9, "div": 0.0},
    "LCID":  {"spot": 2.4,   "iv": 0.85, "fund": -1.2, "div": 0.0},
}

# Fundamentals (rough) for the explicitly named companies — feed `fundamentals` engine
FUND_ROWS: Dict[str, Dict[str, Any]] = {
    # ticker: rev_growth, op_margin, pe, ps, ev_ebitda, fcf_yield, mkt_cap (B), classification, earn_date
    "NVDA": dict(rev_growth=0.78, op_margin=0.55, pe=42, ps=22, ev_ebitda=38, fcf_yield=0.025, market_cap=3.4e12, classification="growth"),
    "AAPL": dict(rev_growth=0.04, op_margin=0.31, pe=33, ps=8.7, ev_ebitda=24, fcf_yield=0.034, market_cap=3.3e12, classification="core"),
    "MSFT": dict(rev_growth=0.16, op_margin=0.45, pe=36, ps=12, ev_ebitda=24, fcf_yield=0.025, market_cap=3.3e12, classification="growth"),
    "AMZN": dict(rev_growth=0.11, op_margin=0.10, pe=46, ps=3.4, ev_ebitda=20, fcf_yield=0.020, market_cap=2.0e12, classification="growth"),
    "GOOGL": dict(rev_growth=0.13, op_margin=0.32, pe=24, ps=6.7, ev_ebitda=16, fcf_yield=0.041, market_cap=2.1e12, classification="value"),
    "META": dict(rev_growth=0.22, op_margin=0.42, pe=28, ps=10, ev_ebitda=16, fcf_yield=0.045, market_cap=1.4e12, classification="growth"),
    "TSLA": dict(rev_growth=-0.05, op_margin=0.06, pe=110, ps=8, ev_ebitda=58, fcf_yield=0.005, market_cap=900e9, classification="speculative"),
    "AMD":  dict(rev_growth=0.13, op_margin=0.05, pe=215, ps=11, ev_ebitda=58, fcf_yield=0.012, market_cap=265e9, classification="growth"),
    "PLTR": dict(rev_growth=0.30, op_margin=0.16, pe=210, ps=58, ev_ebitda=140, fcf_yield=0.010, market_cap=60e9, classification="speculative"),
    "SOFI": dict(rev_growth=0.30, op_margin=0.05, pe=80, ps=4.5, ev_ebitda=22, fcf_yield=0.003, market_cap=10e9, classification="growth"),
    "COIN": dict(rev_growth=0.45, op_margin=0.18, pe=22, ps=10, ev_ebitda=14, fcf_yield=0.030, market_cap=55e9, classification="growth"),
    "MSTR": dict(rev_growth=-0.10, op_margin=-0.20, pe=-25, ps=15, ev_ebitda=22, fcf_yield=-0.020, market_cap=42e9, classification="speculative"),
    "HOOD": dict(rev_growth=0.42, op_margin=0.22, pe=44, ps=12, ev_ebitda=22, fcf_yield=0.015, market_cap=28e9, classification="growth"),
    "JPM":  dict(rev_growth=0.10, op_margin=0.40, pe=12, ps=3.7, ev_ebitda=10, fcf_yield=0.060, market_cap=620e9, classification="value"),
    "BAC":  dict(rev_growth=0.04, op_margin=0.30, pe=14, ps=3.0, ev_ebitda=11, fcf_yield=0.055, market_cap=320e9, classification="value"),
    "XOM":  dict(rev_growth=0.02, op_margin=0.16, pe=14, ps=1.4, ev_ebitda=7, fcf_yield=0.050, market_cap=470e9, classification="value"),
    "BA":   dict(rev_growth=0.04, op_margin=-0.04, pe=-180, ps=1.2, ev_ebitda=240, fcf_yield=-0.020, market_cap=105e9, classification="distressed"),
    "PFE":  dict(rev_growth=0.04, op_margin=0.18, pe=22, ps=2.5, ev_ebitda=12, fcf_yield=0.055, market_cap=153e9, classification="value"),
    "DIS":  dict(rev_growth=0.04, op_margin=0.10, pe=33, ps=1.8, ev_ebitda=14, fcf_yield=0.040, market_cap=170e9, classification="core"),
    "RIVN": dict(rev_growth=0.30, op_margin=-0.95, pe=-3.5, ps=3, ev_ebitda=-4, fcf_yield=-0.30, market_cap=12e9, classification="distressed"),
    "MARA": dict(rev_growth=0.25, op_margin=-0.30, pe=-12, ps=4.5, ev_ebitda=-15, fcf_yield=-0.10, market_cap=5.5e9, classification="distressed"),
    "GME":  dict(rev_growth=-0.15, op_margin=-0.05, pe=-40, ps=1.6, ev_ebitda=-25, fcf_yield=0.010, market_cap=8.0e9, classification="speculative"),
    "AMC":  dict(rev_growth=0.05, op_margin=-0.10, pe=-3.5, ps=0.8, ev_ebitda=22, fcf_yield=-0.20, market_cap=1.5e9, classification="distressed"),
    "UNH":  dict(rev_growth=0.08, op_margin=0.085, pe=22, ps=1.4, ev_ebitda=14, fcf_yield=0.038, market_cap=500e9, classification="core"),
    "LLY":  dict(rev_growth=0.32, op_margin=0.32, pe=80, ps=18, ev_ebitda=58, fcf_yield=0.008, market_cap=770e9, classification="growth"),
}


def _auto_profile(ticker: str, rng: random.Random) -> Dict[str, float]:
    """Generate a plausible profile for a ticker not in TICKER_PROFILES.

    Heuristic: hash the ticker to deterministically pick a class (mega/large/mid/small)
    then assign realistic spot, IV, dividend, and fundamental tilt within that class.
    """
    h = sum(ord(c) for c in ticker) % 100
    if h < 20:        # ~20% mid cap
        spot = rng.uniform(20, 90)
        iv = rng.uniform(0.30, 0.50)
        fund = rng.uniform(-0.4, 0.6)
        div = rng.choice([0.0, 0.0, 0.005, 0.012])
    elif h < 60:      # ~40% small cap with options
        spot = rng.uniform(5, 35)
        iv = rng.uniform(0.45, 0.85)
        fund = rng.uniform(-0.8, 0.4)
        div = 0.0
    else:             # ~40% micro / very speculative
        spot = rng.uniform(1, 12)
        iv = rng.uniform(0.65, 1.15)
        fund = rng.uniform(-1.2, 0.2)
        div = 0.0
    return {"spot": round(spot, 2), "iv": round(iv, 2), "fund": round(fund, 2), "div": div}


def _profile_for(ticker: str, rng: random.Random) -> Dict[str, float]:
    if ticker in TICKER_PROFILES:
        return TICKER_PROFILES[ticker]
    # Cache auto-generated profiles for stability across calls
    if not hasattr(_profile_for, "_cache"):
        _profile_for._cache = {}
    cache = _profile_for._cache
    if ticker not in cache:
        cache[ticker] = _auto_profile(ticker, rng)
    return cache[ticker]


def _earnings_window(days_out: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days_out)).date().isoformat()


def _next_three_expiries(asof: datetime) -> List[str]:
    """Return three monthly expiry strings ~21, 35, 49 days out."""
    out = []
    for d in (21, 35, 49):
        dt = (asof + timedelta(days=d)).date()
        # Snap to next Friday
        while dt.weekday() != 4:
            dt += timedelta(days=1)
        out.append(dt.isoformat())
    return out


def _strike_grid(spot: float) -> List[float]:
    """11 strikes from 88% to 112% of spot, rounded to clean increments."""
    if spot < 5:
        step = 0.5
    elif spot < 25:
        step = 1.0
    elif spot < 100:
        step = 2.5
    elif spot < 300:
        step = 5.0
    else:
        step = 10.0
    atm = round(spot / step) * step
    grid = [round(atm + step * i, 2) for i in range(-5, 6)]
    # Drop non-positive strikes (matters for sub-$5 names)
    return [k for k in grid if k > 0]


def _synthesize_chain(ticker: str, asof: datetime, rng: random.Random,
                       r: float = 0.045) -> pd.DataFrame:
    profile = _profile_for(ticker, rng)
    if not profile:
        return pd.DataFrame()
    spot = profile["spot"]
    base_iv = profile["iv"]   # this becomes our HV30 / "fair vol"
    div_yield = profile["div"]

    # Inject realistic per-ticker mispricing bias so the engine has signal to find.
    # Real-market mispricings are typically 1-3 vol points; weight the distribution
    # toward "fairly priced" with occasional larger anomalies.
    bias = rng.choices(
        [-0.04, -0.025, -0.015, -0.008, 0.0, 0.008, 0.015, 0.025, 0.04],
        weights=[1, 2, 3, 4, 5, 4, 3, 2, 1],
    )[0]

    rows = []
    for exp in _next_three_expiries(asof):
        T_days = (datetime.fromisoformat(exp).replace(tzinfo=timezone.utc) - asof).days
        T = T_days / 365.25
        # Term-structure: longer dated slightly higher IV (small real effect)
        term_bump = 0.01 * (T_days - 35) / 35
        for K in _strike_grid(spot):
            mny = K / spot
            for side in ("call", "put"):
                # Skew: puts richer than calls; small/mid caps have steeper skew
                if side == "put":
                    # OTM puts (mny<1) get a vol premium; ITM puts (mny>1) less so
                    skew_steep = 0.10 if base_iv < 0.30 else 0.18
                    vol_smile = max(0.0, skew_steep * (1 - mny)) + 0.04 * (mny - 1) ** 2
                else:
                    # Calls have a gentler smile; very deep OTM gets a small wing
                    vol_smile = 0.06 * (mny - 1) ** 2 + max(0.0, 0.02 * (1 - mny))
                # Market IV
                noise = rng.gauss(0, 0.008)
                iv_market = max(0.05, base_iv + bias + term_bump + vol_smile + noise)

                # Compute mid from BS at iv_market
                mid = bs_price(spot, K, T, r, iv_market, div_yield, call=(side == "call"))
                if mid < 0.10:
                    continue

                # Realistic spreads — tighter for liquid names/contracts
                liq_factor = 0.5 if ticker in ("SPY", "QQQ", "AAPL", "NVDA", "TSLA", "MSFT", "AMD") else 1.0
                spread_pct = max(0.01, rng.uniform(0.02, 0.10) * liq_factor + 0.04 * abs(1 - mny))
                bid = mid * (1 - spread_pct / 2)
                ask = mid * (1 + spread_pct / 2)

                # OI / volume scale with ticker liquidity and ATM-ness
                base_oi = 8000 if ticker in ("SPY", "QQQ") else 3500 if ticker in ("AAPL", "NVDA", "TSLA", "MSFT", "AMD") else 1200
                atm_factor = math.exp(-(K - spot) ** 2 / (2 * (spot * 0.05) ** 2))
                oi = int(base_oi * atm_factor * rng.uniform(0.6, 1.4))
                vol = int(oi * rng.uniform(0.05, 0.35))

                rows.append({
                    "ticker": ticker,
                    "expiry": exp,
                    "dte": T_days,
                    "T": T,
                    "side": side,
                    "strike": K,
                    "spot": spot,
                    "bid": round(bid, 2),
                    "ask": round(ask, 2),
                    "lastPrice": round(mid, 2),
                    "openInterest": oi,
                    "volume": vol,
                })
    return pd.DataFrame(rows)


def synthetic_mispricing(universe: List[str], asof: datetime, seed: int = 42) -> Dict[str, pd.DataFrame]:
    """Run the same enrichment + filter pipeline as engines.mispricing.run, on synth chains."""
    from engines.mispricing import _enrich_chain, _apply_filters, _per_ticker_summary
    rng = random.Random(seed)
    contracts_all = []
    summaries = []
    for ticker in universe:
        chain = _synthesize_chain(ticker, asof, rng)
        if chain.empty:
            continue
        profile = _profile_for(ticker, rng)
        blob = {
            "ticker": ticker,
            "spot": profile["spot"],
            "hv30": profile["iv"],          # treat as the "fair vol"
            "hv60": profile["iv"] * 1.02,
            "hv252": profile["iv"] * 0.95,
            "div_yield": profile["div"],
            "chain": chain,
        }
        enriched = _enrich_chain(blob)
        filtered = _apply_filters(enriched)
        summaries.append(_per_ticker_summary(enriched, blob))
        if filtered.empty:
            continue
        contracts_all.append(filtered)

    contracts = pd.concat(contracts_all, ignore_index=True) if contracts_all else pd.DataFrame()
    summary = pd.DataFrame([s for s in summaries if s])
    return {"contracts": contracts, "summary": summary}


def synthetic_sentiment(universe: List[str], seed: int = 42) -> pd.DataFrame:
    rng = random.Random(seed + 1)
    rows = []
    # Inject some hot tickers with rising sentiment
    hot = {"NVDA", "TSLA", "PLTR", "GME", "MSTR", "SMCI", "RIVN"}
    cold = {"BA", "PFE", "RIOT", "TLRY", "NIO", "LCID", "AMC"}
    for t in universe:
        is_hot = t in hot
        is_cold = t in cold
        base_mentions = rng.randint(20, 200) if is_hot else rng.randint(0, 30)
        if is_cold:
            base_mentions = rng.randint(5, 50)
        # Sentiment now and prev
        if is_hot:
            s_now = rng.uniform(0.15, 0.55)
            s_prev = rng.uniform(-0.10, 0.20)
        elif is_cold:
            s_now = rng.uniform(-0.55, -0.10)
            s_prev = rng.uniform(-0.20, 0.15)
        else:
            s_now = rng.uniform(-0.15, 0.20)
            s_prev = rng.uniform(-0.15, 0.20)
        rows.append({
            "ticker": t,
            "mentions": base_mentions,
            "sentiment_now": round(s_now, 3),
            "sentiment_prev": round(s_prev, 3),
            "sentiment_delta": round(s_now - s_prev, 3),
            "velocity": rng.randint(-20, 50) if is_hot else rng.randint(-10, 10),
            "ups": rng.randint(0, 5000) if is_hot else rng.randint(0, 500),
        })
    return pd.DataFrame(rows)


def synthetic_fundamentals(universe: List[str], seed: int = 42) -> pd.DataFrame:
    rng = random.Random(seed + 2)
    rows = []
    for t in universe:
        if t in FUND_ROWS:
            d = dict(FUND_ROWS[t])
        else:
            profile = _profile_for(t, rng)
            # Synthesize from profile fund tilt
            tilt = profile["fund"]
            d = dict(
                rev_growth=rng.uniform(-0.05, 0.20) + tilt * 0.10,
                op_margin=rng.uniform(-0.10, 0.30) + tilt * 0.05,
                pe=rng.uniform(15, 45) - tilt * 5 if tilt > -0.5 else rng.uniform(-50, -5),
                ps=rng.uniform(1, 8),
                ev_ebitda=rng.uniform(8, 25),
                fcf_yield=rng.uniform(-0.05, 0.06) + tilt * 0.01,
                market_cap=rng.uniform(1e9, 100e9),
                classification=("growth" if tilt > 0.5 else "value" if -0.2 < tilt < 0.3 else "distressed" if tilt < -0.5 else "speculative"),
            )
        d["ticker"] = t
        # Earnings within ~6 weeks for ~30% of names (creates realistic risk callouts)
        if rng.random() < 0.30:
            d["earnings_date"] = _earnings_window(rng.randint(2, 42))
        else:
            d["earnings_date"] = None
        # Compute a fundamentals score consistent with the engine's logic
        from engines.fundamentals import _fund_score
        d["fund_score"] = _fund_score(d.get("rev_growth"), d.get("op_margin"),
                                      d.get("pe"), d.get("ps"), d.get("fcf_yield"))
        # Default fwd_pe
        d.setdefault("fwd_pe", d.get("pe"))
        d.setdefault("gross_margin", None)
        rows.append(d)
    return pd.DataFrame(rows)


def synthetic_insider(universe: List[str], seed: int = 42) -> pd.DataFrame:
    """Realistic-ish insider activity: ~30% of names have notable buys, ~10% have notable sells."""
    rng = random.Random(seed + 3)
    rows = []
    for t in universe:
        roll = rng.random()
        if roll < 0.18:
            n_buys = rng.randint(1, 5)
            buys_value = rng.uniform(5e5, 1.5e7) * n_buys
            n_sells = rng.randint(0, 1)
            sells_value = rng.uniform(0, 2e6) * n_sells
        elif roll < 0.30:
            n_buys = 0
            buys_value = 0
            n_sells = rng.randint(2, 6)
            sells_value = rng.uniform(2e6, 4e7)
        else:
            n_buys = rng.randint(0, 1)
            n_sells = rng.randint(0, 2)
            buys_value = rng.uniform(0, 5e5) * n_buys
            sells_value = rng.uniform(0, 2e6) * n_sells
        net = buys_value * 1.5 - sells_value
        score = math.copysign(math.log1p(abs(net) / 1e6), net)
        rows.append({
            "ticker": t, "insider_score": round(score, 3),
            "buys_value": round(buys_value, 0), "sells_value": round(sells_value, 0),
            "n_buys": n_buys, "n_sells": n_sells,
        })
    return pd.DataFrame(rows)


def synthetic_news(universe: List[str], seed: int = 42) -> pd.DataFrame:
    """Generate plausible news activity. Hot tickers get higher counts + sentiment swings."""
    rng = random.Random(seed + 4)
    hot = {"NVDA", "TSLA", "PLTR", "GME", "MSTR", "SMCI", "RIVN", "META", "AAPL", "MSFT",
           "COIN", "MARA", "RIOT", "ASTS", "IONQ"}
    cold = {"BA", "PFE", "TLRY", "NIO", "LCID", "AMC", "RIOT"}
    rows = []
    sample_pos = ["beats Q2 estimates, raises guidance", "analyst upgrade to Buy",
                  "announces buyback", "partnership accelerates", "FDA approval"]
    sample_neg = ["misses revenue estimates", "downgraded by analysts", "investigation widens",
                  "guides below consensus", "CFO departs"]
    sample_neu = ["files 10-Q", "ex-dividend date approaches", "added to index", "Q&A scheduled"]
    for t in universe:
        if t in hot:
            n_24 = rng.randint(2, 12); n_7 = rng.randint(15, 60)
            sent_24 = rng.uniform(-0.05, 0.45); sent_7 = rng.uniform(-0.10, 0.30)
            head = rng.choice(sample_pos)
        elif t in cold:
            n_24 = rng.randint(1, 5); n_7 = rng.randint(5, 25)
            sent_24 = rng.uniform(-0.45, 0.0); sent_7 = rng.uniform(-0.35, 0.10)
            head = rng.choice(sample_neg)
        else:
            n_24 = rng.randint(0, 3); n_7 = rng.randint(0, 12)
            sent_24 = rng.uniform(-0.10, 0.10); sent_7 = rng.uniform(-0.10, 0.10)
            head = rng.choice(sample_neu) if n_7 > 0 else ""
        velocity = n_24 - (n_7 / 7.0)
        rows.append({
            "ticker": t,
            "n_24h": n_24,
            "n_7d": n_7,
            "news_sent_24h": round(sent_24, 3),
            "news_sent_7d": round(sent_7, 3),
            "news_delta": round(sent_24 - sent_7, 3),
            "news_velocity": round(velocity, 1),
            "top_headline": f"{t} {head}" if head else "",
        })
    return pd.DataFrame(rows)


def synthetic_earnings(universe: List[str], seed: int = 42) -> pd.DataFrame:
    """Generate plausible upcoming earnings dates + last-quarter surprise."""
    rng = random.Random(seed + 5)
    rows = []
    today = datetime.now(timezone.utc)
    for t in universe:
        roll = rng.random()
        if roll < 0.35:                 # 35% have earnings within 30d
            dte = rng.randint(2, 30)
            d = (today + timedelta(days=dte)).date().isoformat()
            sup = rng.uniform(-0.15, 0.25)
        elif roll < 0.55:               # 20% have earnings 30-60d out
            dte = rng.randint(31, 60)
            d = (today + timedelta(days=dte)).date().isoformat()
            sup = rng.uniform(-0.10, 0.20)
        else:                           # rest no upcoming
            dte = None; d = None; sup = rng.uniform(-0.10, 0.20)

        # Simple score replicating the live engine's logic
        score = 0.0
        if dte is not None and dte <= 30:
            window = 0.3 if dte < 3 else 0.6 if dte < 7 else 1.0 if dte < 21 else 0.5
            score = round(window * max(-1.0, min(1.0, sup * 5)), 3)

        rows.append({
            "ticker": t,
            "next_earnings_date": d,
            "days_to_earnings": dte,
            "eps_est": rng.uniform(0.5, 5.0),
            "eps_actual": rng.uniform(0.4, 5.5),
            "last_eps_surprise_pct": round(sup, 3),
            "earnings_score": score,
        })
    return pd.DataFrame(rows)


def synthetic_value(universe: List[str], seed: int = 42) -> pd.DataFrame:
    """Generate plausible value/EV factors per ticker."""
    rng = random.Random(seed + 6)
    rows = []
    for t in universe:
        profile = _profile_for(t, rng) if t not in TICKER_PROFILES else TICKER_PROFILES[t]
        fund_tilt = profile.get("fund", 0.0)
        # PE: low fundamentals tilt → higher PE; high tilt → reasonable PE
        pe = rng.uniform(15, 60) if fund_tilt > 0 else rng.uniform(20, 80)
        if fund_tilt < -0.5:
            pe = rng.choice([rng.uniform(-100, -5), rng.uniform(80, 200)])
        ps = rng.uniform(0.8, 8) if fund_tilt > -0.5 else rng.uniform(3, 25)
        ev_ebitda = rng.uniform(8, 25) if fund_tilt > -0.3 else rng.uniform(20, 80)
        fcf_y = rng.uniform(0.02, 0.08) if fund_tilt > 0 else rng.uniform(-0.05, 0.04)
        op_margin = rng.uniform(0.05, 0.30) if fund_tilt > 0 else rng.uniform(-0.20, 0.15)
        rev_growth = rng.uniform(0, 0.30) if fund_tilt > 0 else rng.uniform(-0.10, 0.20)
        ey = (1 / pe) if pe > 0 else None

        rows.append({
            "ticker": t,
            "pe": round(pe, 1),
            "fwd_pe": round(pe * 0.9, 1),
            "ps": round(ps, 2),
            "ev_ebitda": round(ev_ebitda, 1),
            "fcf_yield": round(fcf_y, 4),
            "earnings_yield": round(ey, 4) if ey else None,
            "ey_vs_treasury": round((ey - 0.045), 4) if ey else None,
            "roic_proxy": round(op_margin, 4),
            "peg_ratio": round(pe / (rev_growth * 100), 2) if rev_growth > 0 and pe > 0 else None,
            "rev_growth": round(rev_growth, 4),
            "op_margin": round(op_margin, 4),
            "graham_score": rng.randint(0, 6),
            "market_cap": rng.uniform(1e8, 5e11),
        })
    df = pd.DataFrame(rows)
    from engines.value import _build_value_score
    return _build_value_score(df)


def synthetic_futures() -> pd.DataFrame:
    """Generate plausible futures snapshot for demo mode."""
    rng = random.Random(42 + 7)
    from engines.futures import FUTURES
    rows = []
    for meta in FUTURES:
        spot = rng.uniform(50, 5000)
        ret_5d = rng.uniform(-0.05, 0.05)
        ret_20d = rng.uniform(-0.10, 0.12)
        ret_60d = rng.uniform(-0.20, 0.25)
        hv20 = rng.uniform(0.10, 0.40)
        range_pos = rng.uniform(0.05, 0.95)

        score = max(-2.0, min(2.0,
            ret_20d * 5 + ret_5d * 10 * 0.5
            + (0.3 if range_pos < 0.25 else (-0.2 if range_pos > 0.85 else 0))
        ))
        rows.append({
            **meta,
            "spot": round(spot, 2),
            "ret_5d": round(ret_5d, 4),
            "ret_20d": round(ret_20d, 4),
            "ret_60d": round(ret_60d, 4),
            "hv20": round(hv20, 4),
            "range_pos": round(range_pos, 3),
            "futures_score": round(score, 3),
        })
    return pd.DataFrame(rows).sort_values("futures_score", ascending=False).reset_index(drop=True)


def synthetic_congress(universe: List[str], seed: int = 42) -> pd.DataFrame:
    """Plausible Congressional buy/sell activity."""
    rng = random.Random(seed + 8)
    rows = []
    famous_buyers = ["Hon. Nancy Pelosi", "Hon. Tommy Tuberville", "Hon. Mark Green",
                     "Sen. Sheldon Whitehouse", "Hon. Josh Gottheimer",
                     "Sen. Tommy Tuberville", "Hon. Mike Garcia"]
    # Hot tickers: more activity
    hot = {"NVDA", "TSLA", "META", "AAPL", "MSFT", "GOOGL", "AMZN", "AMD", "MU",
           "PLTR", "PFE", "LLY", "JPM", "BA", "LMT", "RTX", "CRWD", "NFLX",
           "CVX", "XOM", "JNJ", "UNH"}
    for t in universe:
        if t not in hot and rng.random() > 0.15:
            continue   # 85% of non-hot tickers get no activity
        n_buys = rng.choices([0, 1, 2, 3, 5], weights=[5, 3, 2, 1, 1])[0] if t in hot else rng.choice([0, 1, 2])
        n_sells = rng.choices([0, 1, 2], weights=[6, 3, 1])[0]
        if n_buys + n_sells == 0:
            continue
        buys_dollar = sum(rng.choice([8000, 50000, 250000, 1_500_000]) for _ in range(n_buys))
        sells_dollar = sum(rng.choice([8000, 50000, 250000]) for _ in range(n_sells))
        n_sens = min(n_buys, rng.randint(0, 2))
        n_reps = max(0, n_buys - n_sens)
        net_w = buys_dollar * 1.5 - sells_dollar
        score = math.copysign(math.log1p(abs(net_w) / 100_000), net_w) if net_w != 0 else 0
        n_members = n_reps + n_sens
        if n_members > 1:
            score *= (1 + 0.15 * (n_members - 1))
        rows.append({
            "ticker": t,
            "congress_score": round(score, 3),
            "congress_buys_n": n_buys,
            "congress_sells_n": n_sells,
            "congress_buys_dollar": buys_dollar,
            "congress_sells_dollar": sells_dollar,
            "congress_n_reps": n_reps,
            "congress_n_sens": n_sens,
            "congress_top_buyer": rng.choice(famous_buyers) if n_buys > 0 else "",
        })
    return pd.DataFrame(rows)


def synthetic_social(universe: List[str], seed: int = 42) -> pd.DataFrame:
    """Plausible StockTwits + Trump social signal."""
    rng = random.Random(seed + 9)
    rows = []
    hot = {"NVDA", "TSLA", "META", "AAPL", "MSFT", "GOOGL", "AMZN", "AMD",
           "PLTR", "GME", "AMC", "DJT", "SPY", "QQQ", "BA", "JPM"}
    trump_hits = ["DJT", "AAPL", "BA", "META", "TSLA", "BTC", "NVDA"]
    sample_excerpts = [
        "Apple is doing great things — tremendous quarter ahead!",
        "Boeing must do better. American aerospace must lead the world.",
        "Tariffs on China bring our manufacturers home, FANTASTIC for U.S. companies.",
        "Truth Social, MY TRUTH SOCIAL, is the most successful platform.",
        "Tesla, Elon Musk — what a great American story!",
    ]
    for t in universe:
        if t not in hot and rng.random() > 0.20:
            continue
        st_n = rng.randint(8, 30) if t in hot else rng.randint(1, 12)
        st_avg = rng.uniform(-0.5, 0.7) if t in hot else rng.uniform(-0.3, 0.3)
        n_bull = max(0, int(st_n * (0.5 + st_avg / 2)))
        n_bear = st_n - n_bull
        tr_n, tr_avg, excerpt = 0, 0, ""
        if t in trump_hits and rng.random() < 0.4:
            tr_n = rng.randint(1, 3)
            tr_avg = rng.uniform(-0.4, 0.7)
            excerpt = rng.choice(sample_excerpts)
        st_component = st_avg * math.log1p(st_n) * 0.5
        tr_component = tr_avg * math.log1p(tr_n) * 1.2
        social_score = round(st_component + tr_component, 3)
        rows.append({
            "ticker": t,
            "social_score": social_score,
            "stocktwits_n": st_n,
            "stocktwits_avg_sent": round(st_avg, 3),
            "stocktwits_n_bull": n_bull,
            "stocktwits_n_bear": n_bear,
            "trump_n": tr_n,
            "trump_avg_sent": round(tr_avg, 3),
            "trump_excerpt": excerpt,
        })
    return pd.DataFrame(rows)


def synthetic_analyst(universe: List[str], seed: int = 42) -> pd.DataFrame:
    """Plausible Finnhub analyst data."""
    rng = random.Random(seed + 10)
    rows = []
    bullish = {"NVDA", "META", "MSFT", "AAPL", "AMZN", "GOOGL", "CRM", "AVGO",
               "LLY", "UNH", "MA", "V", "JPM", "PLTR", "AMD"}
    bearish = {"BA", "PFE", "INTC", "DIS", "WBA", "F", "GM", "RIVN", "LCID"}
    for t in universe[:120]:   # cap to top 120
        if t not in bullish and t not in bearish and rng.random() > 0.40:
            continue
        if t in bullish:
            sb = rng.randint(15, 30); b = rng.randint(20, 40)
            h = rng.randint(2, 8); s = rng.randint(0, 3); ss = 0
        elif t in bearish:
            sb = rng.randint(0, 3); b = rng.randint(2, 8)
            h = rng.randint(8, 20); s = rng.randint(3, 10); ss = rng.randint(0, 4)
        else:
            sb = rng.randint(2, 12); b = rng.randint(5, 25)
            h = rng.randint(5, 15); s = rng.randint(0, 5); ss = rng.randint(0, 2)
        total = sb + b + h + s + ss
        if total == 0:
            continue
        avg = (sb*2 + b*1 + h*0 + s*-1 + ss*-2) / total
        momentum = rng.choice([-3, -1, 0, 0, 0, 1, 2, 4]) if t in bullish else \
                   rng.choice([-4, -2, -1, 0, 0, 1]) if t in bearish else \
                   rng.choice([-1, 0, 0, 0, 1])
        score = max(-3.0, min(3.0, avg + 0.1 * momentum))
        rows.append({
            "ticker": t, "analyst_score": round(score, 3),
            "analyst_strong_buy": sb, "analyst_buy": b, "analyst_hold": h,
            "analyst_sell": s, "analyst_strong_sell": ss,
            "analyst_total": total, "analyst_avg": round(avg, 3),
            "analyst_momentum": momentum,
            "analyst_period": "2026-04-01",
        })
    return pd.DataFrame(rows)


def synthetic_macro() -> Dict[str, Any]:
    return {
        "asof": datetime.now(timezone.utc).isoformat(),
        "vix": 17.4,
        "yield_10y": 4.28,
        "yield_3m": 4.55,
        "yield_curve_slope": -0.27,        # mildly inverted
        "spy_3m_return": 0.034,             # +3.4% trailing 3M
        "cpi_level": None,
        "unrate": 4.1,
        "regime": "neutral",
        "macro_tilt": 0.05,
    }
