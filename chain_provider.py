"""Multi-source options chain provider — v20.2.

Layered, keyless sources (no signup required):
  PRIMARY   : CBOE delayed quotes JSON     (cdn.cboe.com/api/global/delayed_quotes)
  FALLBACK 1: NASDAQ option-chain JSON     (api.nasdaq.com/api/quote/{T}/option-chain)
  FALLBACK 2: yfinance (existing path)

Why this order:
  - CBOE returns the entire chain (all expirations, all strikes, both sides,
    bid/ask, IV, OI, volume, AND Greeks) in a single HTTP call. Typical
    response: ~0.2s. 410 tickers via 8 parallel workers ≈ 10s total.
  - NASDAQ returns the same per-strike data (no Greeks) in one call. Used
    when CBOE returns no contracts (rare — small caps, recent IPOs).
  - yfinance is the legacy path. Slow (3+ HTTP calls per ticker) and
    rate-limited, but stays as a final fallback so behavior degrades
    gracefully if CBOE/NASDAQ are both blocked.

All sources normalize to the same return shape:
  {"spot": float,
   "div_yield": float,
   "expirations": List[str],            # "YYYY-MM-DD"
   "chains": Dict[str, pd.DataFrame],   # per-expiration DataFrames
   "source": str}                       # "cboe" / "nasdaq" / "yfinance"

Each DataFrame's required columns:
  strike, side ("call"/"put"), bid, ask, lastPrice, volume, openInterest,
  impliedVolatility (best-effort), plus optional Greeks when source provides.
"""
from __future__ import annotations
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

import sys
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("optedge.chain")


CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{tk}.json"
NASDAQ_URL = "https://api.nasdaq.com/api/quote/{tk}/option-chain"
NASDAQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/16.6 Safari/605.1.15",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _safe_float(x: Any) -> float:
    try:
        if x is None or x == "" or x == "--":
            return float("nan")
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def _safe_int(x: Any) -> int:
    try:
        if x is None or x == "" or x == "--":
            return 0
        return int(float(x))
    except (TypeError, ValueError):
        return 0


def _parse_occ(symbol: str) -> Optional[Tuple[str, str, str, float]]:
    """OCC option symbol → (ticker, expiry_YYYY-MM-DD, side, strike).

    Format: ROOT + YYMMDD + C|P + 8-digit strike (5 dollars + 3 decimals).
    Example: AAPL260513C00200000 → AAPL, 2026-05-13, call, 200.0
    """
    m = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", symbol)
    if not m:
        return None
    root, ymd, cp, strike_str = m.groups()
    try:
        yr = 2000 + int(ymd[0:2])
        mo = int(ymd[2:4])
        dy = int(ymd[4:6])
        expiry = f"{yr:04d}-{mo:02d}-{dy:02d}"
        strike = int(strike_str) / 1000.0
        side = "call" if cp == "C" else "put"
        return root, expiry, side, strike
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Source 1: CBOE
# ---------------------------------------------------------------------------
def _fetch_cboe(ticker: str, session) -> Optional[Dict[str, Any]]:
    url = CBOE_URL.format(tk=ticker.upper())
    try:
        r = session.get(url, timeout=12)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception as e:
        log.debug("cboe %s fetch: %s", ticker, e)
        return None
    d = data.get("data") if isinstance(data, dict) else None
    if not isinstance(d, dict):
        return None
    contracts = d.get("options") or []
    if not contracts:
        return None
    spot = _safe_float(d.get("current_price"))
    if pd.isna(spot) or spot <= 0:
        return None

    # Group by expiration
    by_exp: Dict[str, List[Dict[str, Any]]] = {}
    for c in contracts:
        sym = c.get("option")
        parsed = _parse_occ(sym) if isinstance(sym, str) else None
        if not parsed:
            continue
        _, expiry, side, strike = parsed
        bid = _safe_float(c.get("bid"))
        ask = _safe_float(c.get("ask"))
        last = _safe_float(c.get("last_trade_price"))
        # Use mid as a best-effort "lastPrice" when last is missing
        if pd.isna(last) and not pd.isna(bid) and not pd.isna(ask):
            last = (bid + ask) / 2 if (bid > 0 and ask > 0) else float("nan")
        iv = _safe_float(c.get("iv"))
        # CBOE returns iv=0.0 for unliquid contracts; treat 0 as NaN
        if iv == 0:
            iv = float("nan")
        rec = {
            "strike": strike,
            "side": side,
            "bid": bid,
            "ask": ask,
            "lastPrice": last,
            "volume": _safe_int(c.get("volume")),
            "openInterest": _safe_int(c.get("open_interest")),
            "impliedVolatility": iv,
            "delta": _safe_float(c.get("delta")),
            "gamma": _safe_float(c.get("gamma")),
            "theta": _safe_float(c.get("theta")),
            "vega":  _safe_float(c.get("vega")),
            "rho":   _safe_float(c.get("rho")),
            "theo":  _safe_float(c.get("theo")),
        }
        by_exp.setdefault(expiry, []).append(rec)

    if not by_exp:
        return None
    chains = {exp: pd.DataFrame(rows) for exp, rows in by_exp.items()}
    return {
        "spot": float(spot),
        "div_yield": 0.0,    # CBOE doesn't provide; engines that need it
                              # already fall back to a small assumed value
        "expirations": sorted(by_exp.keys()),
        "chains": chains,
        "source": "cboe",
    }


# ---------------------------------------------------------------------------
# Source 2: NASDAQ
# ---------------------------------------------------------------------------
def _nasdaq_parse_spot(last_trade_str: str) -> float:
    """Parse 'LAST TRADE: $738.18 (AS OF MAY 12, 2026)' → 738.18."""
    if not isinstance(last_trade_str, str):
        return float("nan")
    m = re.search(r"\$\s*([\d,]+\.?\d*)", last_trade_str)
    if not m:
        return float("nan")
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return float("nan")


def _nasdaq_expiry_from_drilldown(drilldown_url: Optional[str], fallback_md: str) -> Optional[str]:
    """Parse a YYYY-MM-DD expiry. The drilldown URL contains the OCC-format
    symbol after the last '--' separator; fall back to parsing the displayed
    'May 13' via inferring the current year."""
    if isinstance(drilldown_url, str):
        m = re.search(r"--[a-z]+--?(\d{6})[cp]\d{8}", drilldown_url, flags=re.IGNORECASE)
        if m:
            ymd = m.group(1)
            try:
                yr = 2000 + int(ymd[0:2]); mo = int(ymd[2:4]); dy = int(ymd[4:6])
                return f"{yr:04d}-{mo:02d}-{dy:02d}"
            except ValueError:
                pass
    # Fallback: parse "May 13" and pick a year (current year if not in past)
    if isinstance(fallback_md, str):
        m = re.match(r"([A-Z][a-z]{2})\s+(\d{1,2})", fallback_md)
        if m:
            try:
                today = datetime.now(timezone.utc)
                month_num = datetime.strptime(m.group(1), "%b").month
                day = int(m.group(2))
                yr = today.year
                if month_num < today.month or (month_num == today.month and day < today.day):
                    yr += 1
                return f"{yr:04d}-{month_num:02d}-{day:02d}"
            except Exception:
                return None
    return None


def _fetch_nasdaq(ticker: str, session, asset_class: str = "stocks") -> Optional[Dict[str, Any]]:
    url = NASDAQ_URL.format(tk=ticker.upper())
    try:
        r = session.get(url, params={"assetclass": asset_class},
                        headers=NASDAQ_HEADERS, timeout=12)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception as e:
        log.debug("nasdaq %s/%s fetch: %s", ticker, asset_class, e)
        return None
    d = (data or {}).get("data") if isinstance(data, dict) else None
    if not isinstance(d, dict):
        return None
    rows = (d.get("table") or {}).get("rows") or []
    if not rows:
        return None
    spot = _nasdaq_parse_spot(d.get("lastTrade") or "")
    if pd.isna(spot) or spot <= 0:
        return None

    by_exp: Dict[str, List[Dict[str, Any]]] = {}
    for r0 in rows:
        strike = _safe_float(r0.get("strike"))
        if pd.isna(strike) or strike <= 0:
            continue
        expiry = _nasdaq_expiry_from_drilldown(r0.get("drillDownURL"),
                                               r0.get("expiryDate") or "")
        if not expiry:
            continue
        # NASDAQ packs call + put in a single row
        for side, p in (("call", "c_"), ("put", "p_")):
            bid = _safe_float(r0.get(p + "Bid"))
            ask = _safe_float(r0.get(p + "Ask"))
            last = _safe_float(r0.get(p + "Last"))
            vol = _safe_int(r0.get(p + "Volume"))
            oi = _safe_int(r0.get(p + "Openinterest"))
            # Drop empty contracts (all NaN/zero)
            if pd.isna(bid) and pd.isna(ask) and pd.isna(last) and vol == 0 and oi == 0:
                continue
            by_exp.setdefault(expiry, []).append({
                "strike": strike,
                "side": side,
                "bid": bid,
                "ask": ask,
                "lastPrice": last,
                "volume": vol,
                "openInterest": oi,
                "impliedVolatility": float("nan"),
            })
    if not by_exp:
        return None
    chains = {exp: pd.DataFrame(r) for exp, r in by_exp.items()}
    return {
        "spot": float(spot),
        "div_yield": 0.0,
        "expirations": sorted(by_exp.keys()),
        "chains": chains,
        "source": f"nasdaq_{asset_class}",
    }


# ---------------------------------------------------------------------------
# Source 3: yfinance (existing path)
# ---------------------------------------------------------------------------
def _fetch_yfinance(ticker: str) -> Optional[Dict[str, Any]]:
    """Use the existing data_provider.get_options_chain helper. Lazy-imported
    to avoid a circular import at module load."""
    try:
        import data_provider as _dp
    except Exception:
        return None
    # Reuse the legacy implementation via direct yfinance calls
    try:
        tk = _dp.yf_ticker(ticker)
        h = tk.history(period="5d")
        spot = float(h["Close"].iloc[-1]) if not h.empty else None
        if not spot:
            return None
        info = getattr(tk, "info", {}) or {}
        dy = info.get("dividendYield") or 0.0
        div_yield = dy if dy is not None and dy < 1 else (dy or 0) / 100.0
        expirations = tk.options or []
        chains: Dict[str, pd.DataFrame] = {}
        for exp in expirations:
            try:
                opt = tk.option_chain(exp)
                df_calls = opt.calls.copy(); df_calls["side"] = "call"
                df_puts = opt.puts.copy();  df_puts["side"] = "put"
                chains[exp] = pd.concat([df_calls, df_puts], ignore_index=True)
            except Exception:
                continue
            time.sleep(0.2)
        if not chains:
            return None
        return {
            "spot": spot,
            "div_yield": div_yield,
            "expirations": list(expirations),
            "chains": chains,
            "source": "yfinance",
        }
    except Exception as e:
        log.debug("yfinance %s fetch: %s", ticker, e)
        return None


# ---------------------------------------------------------------------------
# Public entry point — multi-source orchestrator
# ---------------------------------------------------------------------------
def fetch_chain(ticker: str, cache_age: int = 600) -> Dict[str, Any]:
    """Multi-source options chain fetch. Tries CBOE -> NASDAQ (stocks then
    etf then index) -> yfinance, returning the first source with usable data.

    Cache TTL matches data_provider.get_options_chain (10 min). Cached
    blobs round-trip through json so DataFrames are converted to records."""
    import data_provider as _dp
    key = f"chain:{ticker}"
    cached = _dp.cache_get(key, cache_age)
    if cached and isinstance(cached, dict) and cached.get("chains"):
        try:
            chains = {exp: pd.DataFrame(rows) for exp, rows in cached["chains"].items()}
            return {**cached, "chains": chains}
        except Exception:
            pass

    sess = _dp.get_session()

    # CBOE
    blob = _fetch_cboe(ticker, sess)
    # NASDAQ (try stocks, then etf, then index — first one that returns wins)
    if not blob:
        for ac in ("stocks", "etf", "index"):
            blob = _fetch_nasdaq(ticker, sess, asset_class=ac)
            if blob:
                break
    # yfinance fallback
    if not blob:
        blob = _fetch_yfinance(ticker)

    if not blob:
        return {}
    # Cache (convert DataFrames to records, drop source for stability)
    try:
        cached_blob = {
            **blob,
            "chains": {k: v.to_dict("records") for k, v in blob["chains"].items()},
        }
        _dp.cache_put(key, cached_blob)
    except Exception:
        pass
    return blob


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import os
    os.environ.pop("CACHE_DISABLED", "")
    for tk in ["AAPL", "NVDA", "TSLA", "SPY", "GME"]:
        b = fetch_chain(tk)
        if not b:
            print(f"{tk}: NO DATA")
            continue
        n = sum(len(df) for df in b["chains"].values())
        print(f"{tk}: source={b['source']:8} spot={b['spot']:.2f} "
              f"exps={len(b['expirations'])} contracts={n}")
