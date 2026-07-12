"""Multi-source options chain provider - v20.2.

Layered, keyless sources (no signup required):
  PRIMARY   : CBOE delayed quotes JSON     (cdn.cboe.com/api/global/delayed_quotes)
  FALLBACK 1: NASDAQ option-chain JSON     (api.nasdaq.com/api/quote/{T}/option-chain)
  FALLBACK 2: Yahoo options JSON           (query1.finance.yahoo.com/v7/finance/options)
  FALLBACK 3: yfinance (existing path)

Why this order:
  - CBOE returns the entire chain (all expirations, all strikes, both sides,
    bid/ask, IV, OI, volume, AND Greeks) in a single HTTP call. Typical
    response: ~0.2s. 410 tickers via 8 parallel workers ~= 10s total.
  - NASDAQ returns the same per-strike data (no Greeks) in one call. Used
    when CBOE returns no contracts (rare - small caps, recent IPOs).
  - Yahoo options JSON is keyless and lighter than the yfinance library path.
    It is unofficial/research-grade, so it stays behind CBOE/NASDAQ.
  - yfinance is the legacy path. Slow (3+ HTTP calls per ticker) and
    rate-limited, but stays as a final fallback so behavior degrades
    gracefully if CBOE/NASDAQ/Yahoo direct are blocked.

All sources normalize to the same return shape:
  {"spot": float,
   "div_yield": float,
   "expirations": List[str],            # "YYYY-MM-DD"
   "chains": Dict[str, pd.DataFrame],   # per-expiration DataFrames
   "source": str}                       # "cboe" / "nasdaq" / "yahoo_options" / "yfinance"

Each DataFrame's required columns:
  strike, side ("call"/"put"), bid, ask, lastPrice, volume, openInterest,
  impliedVolatility (best-effort), plus optional Greeks when source provides.
"""
from __future__ import annotations
import logging
import os
import re
import time
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
YAHOO_OPTIONS_URL = "https://query1.finance.yahoo.com/v7/finance/options/{tk}"
TRADIER_BASE_URL = os.environ.get("OPTEDGE_TRADIER_BASE_URL", "https://api.tradier.com/v1").rstrip("/")
TRADIER_CHAIN_URL = f"{TRADIER_BASE_URL}/markets/options/chains"
TRADIER_EXPIRATIONS_URL = f"{TRADIER_BASE_URL}/markets/options/expirations"
TRADIER_MAX_EXPIRATIONS = max(1, int(os.environ.get("OPTEDGE_TRADIER_MAX_EXPIRATIONS", "12")))
YAHOO_OPTIONS_MAX_EXPIRATIONS = max(1, int(os.environ.get("OPTEDGE_YAHOO_OPTIONS_MAX_EXPIRATIONS", "18")))
NASDAQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/16.6 Safari/605.1.15",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}
YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": "https://finance.yahoo.com/",
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


def _tradier_token() -> str:
    return (
        os.environ.get("OPTEDGE_TRADIER_TOKEN")
        or os.environ.get("TRADIER_TOKEN")
        or os.environ.get("TRADIER_ACCESS_TOKEN")
        or ""
    ).strip()


def tradier_enabled() -> bool:
    return bool(_tradier_token())


def _tradier_headers() -> Dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {_tradier_token()}",
    }


def _tradier_option_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    options = (payload or {}).get("options") if isinstance(payload, dict) else None
    rows = options.get("option") if isinstance(options, dict) else None
    if rows is None:
        return []
    if isinstance(rows, dict):
        return [rows]
    return rows if isinstance(rows, list) else []


def _fetch_tradier_expirations(ticker: str, session) -> List[str]:
    if not tradier_enabled():
        return []
    try:
        r = session.get(
            TRADIER_EXPIRATIONS_URL,
            params={"symbol": ticker.upper(), "includeAllRoots": "false"},
            headers=_tradier_headers(),
            timeout=12,
        )
        if r.status_code != 200:
            log.debug("tradier expirations %s status=%s", ticker, r.status_code)
            return []
        data = r.json()
    except Exception as e:
        log.debug("tradier expirations %s fetch: %s", ticker, e)
        return []
    raw = ((data or {}).get("expirations") or {}).get("date")
    if isinstance(raw, str):
        return [raw]
    return [str(x) for x in raw] if isinstance(raw, list) else []


def _tradier_record(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    opt_symbol = str(row.get("symbol") or "")
    parsed = _parse_occ(opt_symbol)
    expiry = None
    side = str(row.get("option_type") or "").strip().lower()
    strike = _safe_float(row.get("strike"))
    if parsed:
        _, expiry, parsed_side, parsed_strike = parsed
        side = side or parsed_side
        if pd.isna(strike):
            strike = parsed_strike
    if side in {"c", "call"}:
        side = "call"
    elif side in {"p", "put"}:
        side = "put"
    else:
        return None
    if pd.isna(strike) or strike <= 0:
        return None
    greeks = row.get("greeks") if isinstance(row.get("greeks"), dict) else {}
    bid = _safe_float(row.get("bid"))
    ask = _safe_float(row.get("ask"))
    last = _safe_float(row.get("last"))
    if pd.isna(last) and not pd.isna(bid) and not pd.isna(ask) and bid > 0 and ask >= bid:
        last = (bid + ask) / 2.0
    return {
        "strike": strike,
        "side": side,
        "bid": bid,
        "ask": ask,
        "lastPrice": last,
        "volume": _safe_int(row.get("volume")),
        "openInterest": _safe_int(row.get("open_interest")),
        "impliedVolatility": _safe_float(greeks.get("mid_iv") or greeks.get("smv_vol")),
        "delta": _safe_float(greeks.get("delta")),
        "gamma": _safe_float(greeks.get("gamma")),
        "theta": _safe_float(greeks.get("theta")),
        "vega": _safe_float(greeks.get("vega")),
        "rho": _safe_float(greeks.get("rho")),
        "_expiry": expiry,
    }


def _fetch_tradier(ticker: str, session) -> Optional[Dict[str, Any]]:
    """Optional Tradier production option chains.

    Tradier exposes expirations separately, then one chain per expiration.
    Set OPTEDGE_TRADIER_TOKEN or TRADIER_TOKEN to enable this provider.
    """
    if not tradier_enabled():
        return None
    expirations = _fetch_tradier_expirations(ticker, session)
    if not expirations:
        return None
    by_exp: Dict[str, List[Dict[str, Any]]] = {}
    spot_candidates: List[float] = []
    for exp in expirations[:TRADIER_MAX_EXPIRATIONS]:
        try:
            r = session.get(
                TRADIER_CHAIN_URL,
                params={"symbol": ticker.upper(), "expiration": exp, "greeks": "true"},
                headers=_tradier_headers(),
                timeout=12,
            )
            if r.status_code != 200:
                log.debug("tradier chain %s %s status=%s", ticker, exp, r.status_code)
                continue
            rows = _tradier_option_rows(r.json())
        except Exception as e:
            log.debug("tradier chain %s %s fetch: %s", ticker, exp, e)
            continue
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            rec = _tradier_record(raw)
            if not rec:
                continue
            row_exp = rec.pop("_expiry") or exp
            by_exp.setdefault(row_exp, []).append(rec)
            underlying = _safe_float(raw.get("underlying_price"))
            if not pd.isna(underlying) and underlying > 0:
                spot_candidates.append(underlying)
    if not by_exp:
        return None
    spot = spot_candidates[-1] if spot_candidates else float("nan")
    if pd.isna(spot) or spot <= 0:
        try:
            import data_provider as _dp

            h = _dp.get_history(ticker, period="5d", cache_age=300)
            if not h.empty:
                spot = float(h["Close"].iloc[-1])
        except Exception:
            spot = float("nan")
    if pd.isna(spot) or spot <= 0:
        return None
    return {
        "spot": float(spot),
        "div_yield": 0.0,
        "expirations": sorted(by_exp.keys()),
        "chains": {exp: pd.DataFrame(rows) for exp, rows in by_exp.items()},
        "source": "tradier",
        "quote_quality": "live_or_broker",
    }


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
        "quote_quality": "free_or_delayed",
        "data_delay": "delayed",
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
        "quote_quality": "free_or_delayed",
        "data_delay": "delayed",
    }


# ---------------------------------------------------------------------------
# Source 3: Yahoo options JSON
# ---------------------------------------------------------------------------
def _yahoo_expiry_from_timestamp(value: Any) -> Optional[str]:
    try:
        ts = int(float(value))
        if ts <= 0:
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return None


def _yahoo_option_record(row: Dict[str, Any], side: str, expiry: str) -> Optional[Dict[str, Any]]:
    strike = _safe_float(row.get("strike"))
    if pd.isna(strike) or strike <= 0:
        return None
    side_norm = "call" if side == "calls" else "put"
    bid = _safe_float(row.get("bid"))
    ask = _safe_float(row.get("ask"))
    last = _safe_float(row.get("lastPrice"))
    if pd.isna(last) and not pd.isna(bid) and not pd.isna(ask) and bid > 0 and ask >= bid:
        last = (bid + ask) / 2.0
    return {
        "strike": strike,
        "side": side_norm,
        "bid": bid,
        "ask": ask,
        "lastPrice": last,
        "volume": _safe_int(row.get("volume")),
        "openInterest": _safe_int(row.get("openInterest")),
        "impliedVolatility": _safe_float(row.get("impliedVolatility")),
        "contractSymbol": row.get("contractSymbol"),
        "lastTradeDate": _yahoo_expiry_from_timestamp(row.get("lastTradeDate")),
        "inTheMoney": bool(row.get("inTheMoney", False)),
        "_expiry": expiry,
    }


def _yahoo_options_result(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    result = (((payload or {}).get("optionChain") or {}).get("result") or [])
    if not result or not isinstance(result[0], dict):
        return None
    return result[0]


def _yahoo_chain_from_results(
    quote: Dict[str, Any],
    option_groups: List[Dict[str, Any]],
    source_note: str = "yahoo_options",
) -> Optional[Dict[str, Any]]:
    spot = _safe_float(
        quote.get("regularMarketPrice")
        or quote.get("postMarketPrice")
        or quote.get("preMarketPrice")
        or quote.get("bid")
        or quote.get("ask")
    )
    if pd.isna(spot) or spot <= 0:
        return None
    dy = _safe_float(quote.get("trailingAnnualDividendYield") or quote.get("dividendYield"))
    div_yield = 0.0 if pd.isna(dy) else (float(dy) / 100.0 if dy > 1 else float(dy))

    by_exp: Dict[str, List[Dict[str, Any]]] = {}
    for opt_group in option_groups:
        if not isinstance(opt_group, dict):
            continue
        expiry = _yahoo_expiry_from_timestamp(opt_group.get("expirationDate"))
        if not expiry:
            continue
        for side in ("calls", "puts"):
            raw_rows = opt_group.get(side) or []
            if not isinstance(raw_rows, list):
                continue
            for raw in raw_rows:
                if not isinstance(raw, dict):
                    continue
                rec = _yahoo_option_record(raw, side, expiry)
                if not rec:
                    continue
                row_exp = rec.pop("_expiry")
                by_exp.setdefault(row_exp, []).append(rec)

    if not by_exp:
        return None
    return {
        "spot": float(spot),
        "div_yield": div_yield,
        "expirations": sorted(by_exp.keys()),
        "chains": {exp: pd.DataFrame(rows) for exp, rows in by_exp.items()},
        "source": source_note,
        "quote_quality": "free_or_delayed",
        "data_delay": "delayed_or_research",
    }


def _fetch_yahoo_options_via_yfinance(ticker: str) -> Optional[Dict[str, Any]]:
    """Reuse yfinance's Yahoo crumb/cookie handling, but only fetch a bounded
    number of expirations before the full legacy yfinance fallback runs."""
    try:
        import data_provider as _dp
    except Exception:
        return None
    try:
        tk = _dp.yf_ticker(ticker)
        first = tk._download_options()  # noqa: SLF001 - yfinance's public method fetches one expiry at a time.
    except Exception as e:
        log.debug("yahoo options yfinance-lite %s fetch: %s", ticker, e)
        return None
    if not isinstance(first, dict) or not first:
        return None
    quote = first.get("underlying") if isinstance(first.get("underlying"), dict) else {}
    option_groups: List[Dict[str, Any]] = [first]
    seen_expirations = {
        _yahoo_expiry_from_timestamp(first.get("expirationDate")),
    }
    expiration_map = getattr(tk, "_expirations", {}) or {}
    for _, exp_ts in list(expiration_map.items())[:YAHOO_OPTIONS_MAX_EXPIRATIONS]:
        expiry = _yahoo_expiry_from_timestamp(exp_ts)
        if not expiry or expiry in seen_expirations:
            continue
        try:
            group = tk._download_options(exp_ts)  # noqa: SLF001
        except Exception as e:
            log.debug("yahoo options yfinance-lite %s %s fetch: %s", ticker, exp_ts, e)
            continue
        if isinstance(group, dict) and group:
            option_groups.append(group)
            seen_expirations.add(expiry)
        time.sleep(0.05)
    return _yahoo_chain_from_results(quote, option_groups, source_note="yahoo_options")


def _fetch_yahoo_options(ticker: str, session) -> Optional[Dict[str, Any]]:
    """Fetch Yahoo's direct options JSON endpoint as a no-key fallback.

    This is intentionally behind CBOE/NASDAQ because it is an unofficial
    research-grade endpoint. It is still lighter than the yfinance wrapper and
    gives Optedge another free coverage path for swing-chain discovery.
    """
    url = YAHOO_OPTIONS_URL.format(tk=ticker.upper())
    first_result = None
    try:
        first = session.get(url, headers=YAHOO_HEADERS, timeout=12)
        if first.status_code != 200:
            return _fetch_yahoo_options_via_yfinance(ticker)
        first_result = _yahoo_options_result(first.json())
    except Exception as e:
        log.debug("yahoo options %s fetch: %s", ticker, e)
        return _fetch_yahoo_options_via_yfinance(ticker)
    if not first_result:
        return _fetch_yahoo_options_via_yfinance(ticker)

    quote = first_result.get("quote") if isinstance(first_result.get("quote"), dict) else {}
    expiration_dates = first_result.get("expirationDates") or []
    dates: List[int] = []
    for value in expiration_dates[:YAHOO_OPTIONS_MAX_EXPIRATIONS]:
        try:
            ts = int(float(value))
        except (TypeError, ValueError):
            continue
        if ts > 0 and ts not in dates:
            dates.append(ts)

    results: List[Dict[str, Any]] = [first_result]
    first_exp = _yahoo_expiry_from_timestamp(
        ((first_result.get("options") or [{}])[0] or {}).get("expirationDate")
    )
    fetched_first_ts = None
    if first_exp:
        try:
            fetched_first_ts = int(datetime.fromisoformat(first_exp).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            fetched_first_ts = None

    for exp_ts in dates:
        if fetched_first_ts is not None and exp_ts == fetched_first_ts:
            continue
        try:
            resp = session.get(url, params={"date": exp_ts}, headers=YAHOO_HEADERS, timeout=12)
            if resp.status_code != 200:
                continue
            result = _yahoo_options_result(resp.json())
            if result:
                results.append(result)
        except Exception as e:
            log.debug("yahoo options %s %s fetch: %s", ticker, exp_ts, e)
            continue
        time.sleep(0.05)

    option_groups: List[Dict[str, Any]] = []
    for result in results:
        for opt_group in result.get("options") or []:
            if not isinstance(opt_group, dict):
                continue
            option_groups.append(opt_group)
    return _yahoo_chain_from_results(quote, option_groups, source_note="yahoo_options")


# ---------------------------------------------------------------------------
# Source 4: yfinance (existing path)
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
            "quote_quality": "free_or_delayed",
            "data_delay": "delayed",
        }
    except Exception as e:
        log.debug("yfinance %s fetch: %s", ticker, e)
        return None


# ---------------------------------------------------------------------------
# Provider diagnostics
# ---------------------------------------------------------------------------
def _contract_count(blob: Optional[Dict[str, Any]]) -> int:
    if not blob or not isinstance(blob, dict):
        return 0
    chains = blob.get("chains")
    if not isinstance(chains, dict):
        return 0
    total = 0
    for rows in chains.values():
        if isinstance(rows, pd.DataFrame):
            total += len(rows)
        elif isinstance(rows, list):
            total += len(rows)
    return total


def _attempt_record(
    name: str,
    blob: Optional[Dict[str, Any]],
    elapsed_ms: float,
    note: str = "",
) -> Dict[str, Any]:
    rows = _contract_count(blob)
    return {
        "provider": name,
        "status": "ok" if rows > 0 else "warn",
        "rows": rows,
        "expirations": len((blob or {}).get("expirations") or []),
        "source": (blob or {}).get("source") or name,
        "quote_quality": (blob or {}).get("quote_quality"),
        "data_delay": (blob or {}).get("data_delay"),
        "provider_response_received_at": (blob or {}).get("provider_response_received_at"),
        "source_quote_time_basis": (blob or {}).get("source_quote_time_basis"),
        "latency_ms": round(elapsed_ms, 1),
        "note": note or ("usable chain returned" if rows > 0 else "no usable chain returned"),
    }


def _timed_attempt(name: str, fetcher) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    started = time.perf_counter()
    note = ""
    try:
        blob = fetcher()
        if isinstance(blob, dict) and blob:
            # This is a receipt timestamp, not an exchange quote timestamp. It
            # is assigned once before caching and therefore cannot be refreshed
            # by a cache read, scan, shortlist write, or later export.
            received_at = datetime.now(timezone.utc).isoformat()
            blob.setdefault("provider_response_received_at", received_at)
            if not blob.get("source_quote_at"):
                blob["source_quote_at"] = blob["provider_response_received_at"]
                blob["source_quote_time_basis"] = "provider_response_received_at"
    except Exception as exc:
        blob = None
        note = str(exc)[:180]
        log.debug("%s chain attempt failed: %s", name, exc)
    return blob, _attempt_record(name, blob, (time.perf_counter() - started) * 1000.0, note)


def _cacheable_blob(blob: Dict[str, Any]) -> Dict[str, Any]:
    return {
        **blob,
        "chains": {k: v.to_dict("records") for k, v in blob["chains"].items()},
    }


def _restore_cached_blob(cached: Dict[str, Any]) -> Dict[str, Any]:
    chains = {exp: pd.DataFrame(rows) for exp, rows in cached["chains"].items()}
    return {**cached, "chains": chains}


# ---------------------------------------------------------------------------
# Public entry point - multi-source orchestrator
# ---------------------------------------------------------------------------
def fetch_chain(ticker: str, cache_age: int = 600, include_diagnostics: bool = False) -> Dict[str, Any]:
    """Multi-source options chain fetch. Tries CBOE -> NASDAQ (stocks then
    etf then index) -> yfinance, returning the first source with usable data.

    Cache TTL matches data_provider.get_options_chain (10 min). Cached
    blobs round-trip through json so DataFrames are converted to records."""
    import data_provider as _dp
    key = f"chain:{ticker}"
    cached = _dp.cache_get(key, cache_age)
    if cached and isinstance(cached, dict) and cached.get("chains"):
        try:
            blob = _restore_cached_blob(cached)
            if include_diagnostics and "source_attempts" not in blob:
                blob["source_attempts"] = [{
                    "provider": "cache",
                    "status": "ok",
                    "rows": _contract_count(blob),
                    "expirations": len(blob.get("expirations") or []),
                    "source": blob.get("source"),
                    "quote_quality": blob.get("quote_quality"),
                    "data_delay": blob.get("data_delay"),
                    "provider_response_received_at": blob.get("provider_response_received_at"),
                    "source_quote_time_basis": blob.get("source_quote_time_basis"),
                    "latency_ms": 0,
                    "note": "cache hit",
                }]
            return blob
        except Exception:
            pass

    sess = _dp.get_session()
    attempts: List[Dict[str, Any]] = []

    # Optional live/broker source first when configured.
    blob = None
    if tradier_enabled():
        blob, attempt = _timed_attempt("tradier", lambda: _fetch_tradier(ticker, sess))
        attempts.append(attempt)
    # Free/keyless delayed source.
    if not blob:
        blob, attempt = _timed_attempt("cboe", lambda: _fetch_cboe(ticker, sess))
        attempts.append(attempt)
    # NASDAQ (try stocks, then etf, then index - first one that returns wins)
    if not blob:
        for ac in ("stocks", "etf", "index"):
            name = f"nasdaq_{ac}"
            blob, attempt = _timed_attempt(name, lambda ac=ac: _fetch_nasdaq(ticker, sess, asset_class=ac))
            attempts.append(attempt)
            if blob:
                break
    # Direct Yahoo options JSON fallback before the heavier yfinance wrapper.
    if not blob:
        blob, attempt = _timed_attempt("yahoo_options", lambda: _fetch_yahoo_options(ticker, sess))
        attempts.append(attempt)
    # yfinance fallback
    if not blob:
        blob, attempt = _timed_attempt("yfinance", lambda: _fetch_yfinance(ticker))
        attempts.append(attempt)

    if not blob:
        return {"source_attempts": attempts} if include_diagnostics else {}
    if include_diagnostics:
        blob["source_attempts"] = attempts
    # Cache (convert DataFrames to records, drop source for stability)
    try:
        _dp.cache_put(key, _cacheable_blob(blob))
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
