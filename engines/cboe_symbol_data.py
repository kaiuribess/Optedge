# Purpose: Cboe public option symbol activity.
"""Cboe public option symbol activity.

Free, no-key source:
https://www.cboe.com/us/options/market_statistics/symbol_data/

This is public exchange activity context, not a consolidated OPRA feed and not
an execution quote. Optedge uses it as a sanity-check layer for option
candidates before a Robinhood/Codex review.
"""
from __future__ import annotations

import io
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider
from optedge.http_identity import outbound_headers

log = logging.getLogger("optedge.cboe_symbol_data")

CBOE_SYMBOL_DATA_CSV = "https://www.cboe.com/us/options/market_statistics/symbol_data/csv/?mkt={market}"
CBOE_OPTION_MARKETS = {
    "cone": "Cboe Options",
    "opt": "BZX Options",
    "ctwo": "C2 Options",
    "exo": "EDGX Options",
}

MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _headers() -> dict[str, str]:
    return outbound_headers(accept="text/csv,text/plain,*/*")


def _num(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        out = float(text)
    except Exception:
        return None
    return out if math.isfinite(out) else None


def _infer_expiry(month: int, day: int, asof: datetime | None = None) -> str | None:
    asof = asof or datetime.now(timezone.utc)
    base_year = int(asof.year)
    try:
        expiry = datetime(base_year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None
    # Cboe labels omit year. If the date is already clearly behind us, treat it
    # as the next listed year.
    if expiry.date() < asof.date():
        try:
            expiry = datetime(base_year + 1, month, day, tzinfo=timezone.utc)
        except ValueError:
            return None
    return expiry.date().isoformat()


def _normalized_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return pd.to_datetime(text, utc=True).date().isoformat()
    except Exception:
        return None


def _parse_option_label(label: Any, asof: datetime | None = None) -> dict[str, Any]:
    """Parse labels like 'AAPL Jan 15 200.0 Call' into contract fields."""
    parts = str(label or "").strip().split()
    if len(parts) < 5:
        return {}
    side = parts[-1].lower()
    if side not in {"call", "put"}:
        return {}
    strike = _num(parts[-2])
    day = _num(parts[-3])
    month = MONTHS.get(parts[-4].lower()[:3])
    ticker = " ".join(parts[:-4]).strip().upper()
    if not ticker or strike is None or day is None or not month:
        return {}
    expiry = _infer_expiry(month, int(day), asof=asof)
    if not expiry:
        return {}
    return {
        "ticker": ticker,
        "expiry": expiry,
        "strike": float(strike),
        "option_side": side,
    }


def parse_symbol_data_csv(
    text: str,
    market: str = "cone",
    asof: datetime | None = None,
    symbols: set[str] | None = None,
) -> pd.DataFrame:
    """Parse one Cboe option symbol activity CSV payload."""
    if not text:
        return pd.DataFrame()
    try:
        raw = pd.read_csv(io.StringIO(text))
    except Exception:
        return pd.DataFrame()
    if raw.empty:
        return pd.DataFrame()
    symbols = {str(s).strip().upper() for s in (symbols or set()) if str(s).strip()}
    if symbols and "Symbol" in raw.columns:
        raw["Symbol"] = raw["Symbol"].astype(str).str.strip().str.upper()
        raw = raw[raw["Symbol"].isin(symbols)].copy()
        if raw.empty:
            return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    venue = CBOE_OPTION_MARKETS.get(market, market)
    for item in raw.to_dict(orient="records"):
        if "Option" in raw.columns:
            parsed = _parse_option_label(item.get("Option"), asof=asof)
            contract_label = str(item.get("Option") or "").strip()
        else:
            side = str(item.get("Call/Put") or "").strip().lower()
            side = {"c": "call", "p": "put"}.get(side[:1], side)
            strike = _num(item.get("Strike Price"))
            expiry = _normalized_date(item.get("Expiration"))
            ticker = str(item.get("Symbol") or "").strip().upper()
            parsed = {
                "ticker": ticker,
                "expiry": expiry,
                "strike": float(strike) if strike is not None else None,
                "option_side": side,
            }
            contract_label = (
                f"{ticker} {expiry} {strike:g} {side.title()}"
                if ticker and expiry and strike is not None and side in {"call", "put"}
                else ""
            )
            if not parsed:
                continue
        if symbols and parsed.get("ticker") not in symbols:
            continue
        if not parsed.get("ticker") or not parsed.get("expiry") or parsed.get("strike") is None:
            continue
        if parsed.get("option_side") not in {"call", "put"}:
            continue
        volume = _num(item.get("Volume")) or 0.0
        bid = _num(item.get("Bid Price"))
        ask = _num(item.get("Ask Price"))
        rows.append({
            **parsed,
            "cboe_activity_contract": contract_label,
            "cboe_activity_volume": int(volume),
            "cboe_activity_matched": int(_num(item.get("Matched")) or 0),
            "cboe_activity_routed": int(_num(item.get("Routed")) or 0),
            "cboe_activity_bid_size": int(_num(item.get("Bid Size")) or 0),
            "cboe_activity_bid": bid,
            "cboe_activity_ask_size": int(_num(item.get("Ask Size")) or 0),
            "cboe_activity_ask": ask,
            "cboe_activity_last": _num(item.get("Last Price")),
            "cboe_activity_venue": venue,
            "cboe_activity_source": "cboe_symbol_data",
        })
    return pd.DataFrame(rows)


def fetch_market(
    market: str,
    cache_age_sec: int = 5 * 60,
    symbols: set[str] | None = None,
) -> pd.DataFrame:
    """Fetch and parse one Cboe venue."""
    market = str(market or "cone").lower()
    symbols = {str(s).strip().upper() for s in (symbols or set()) if str(s).strip()}
    symbol_key = ",".join(sorted(symbols)) if symbols else "all"
    url = CBOE_SYMBOL_DATA_CSV.format(market=market)
    cache_key = f"cboe_symbol_data:{market}:{symbol_key}:v2"
    cached = data_provider.cache_get(cache_key, max_age_sec=cache_age_sec)
    if isinstance(cached, list):
        return pd.DataFrame(cached)
    sess = data_provider.get_session()
    resp = sess.get(url, headers=_headers(), timeout=20)
    if getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(f"Cboe symbol data {market} returned HTTP {getattr(resp, 'status_code', 'unknown')}")
    df = parse_symbol_data_csv(getattr(resp, "text", "") or "", market=market, symbols=symbols)
    if not df.empty:
        data_provider.cache_put(cache_key, df.to_dict("records"))
    return df


def aggregate_activity(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate duplicated contracts across Cboe venues."""
    if df is None or df.empty:
        return pd.DataFrame()
    group_cols = ["ticker", "expiry", "strike", "option_side"]
    for col in group_cols:
        if col not in df.columns:
            return pd.DataFrame()
    work = df.copy()
    for col in [
        "cboe_activity_volume", "cboe_activity_matched", "cboe_activity_routed",
        "cboe_activity_bid_size", "cboe_activity_ask_size",
    ]:
        work[col] = pd.to_numeric(work.get(col), errors="coerce").fillna(0)
    rows: list[dict[str, Any]] = []
    for key, group in work.groupby(group_cols, dropna=False):
        ticker, expiry, strike, option_side = key
        best = group.sort_values("cboe_activity_volume", ascending=False).iloc[0]
        venues = sorted({str(v) for v in group.get("cboe_activity_venue", pd.Series()).dropna() if str(v)})
        rows.append({
            "ticker": ticker,
            "expiry": expiry,
            "strike": float(strike),
            "option_side": option_side,
            "cboe_activity_volume": int(group["cboe_activity_volume"].sum()),
            "cboe_activity_matched": int(group["cboe_activity_matched"].sum()),
            "cboe_activity_routed": int(group["cboe_activity_routed"].sum()),
            "cboe_activity_bid_size": int(group["cboe_activity_bid_size"].sum()),
            "cboe_activity_ask_size": int(group["cboe_activity_ask_size"].sum()),
            "cboe_activity_bid": best.get("cboe_activity_bid"),
            "cboe_activity_ask": best.get("cboe_activity_ask"),
            "cboe_activity_last": best.get("cboe_activity_last"),
            "cboe_activity_contract": best.get("cboe_activity_contract"),
            "cboe_activity_venues": ",".join(venues),
            "cboe_activity_source": "cboe_symbol_data",
        })
    return pd.DataFrame(rows).sort_values("cboe_activity_volume", ascending=False).reset_index(drop=True)


def run(
    universe: list[str] | None = None,
    markets: list[str] | None = None,
    min_volume: int = 1,
) -> pd.DataFrame:
    """Return aggregated public Cboe option activity for the requested tickers."""
    markets = markets or list(CBOE_OPTION_MARKETS)
    symbols = {str(t).strip().upper() for t in (universe or []) if str(t).strip()}
    frames: list[pd.DataFrame] = []
    for market in markets:
        try:
            frame = fetch_market(market, symbols=symbols)
        except Exception as exc:
            log.debug("cboe symbol data %s failed: %s", market, exc)
            continue
        if frame is not None and not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame()
    out = aggregate_activity(pd.concat(frames, ignore_index=True))
    if symbols:
        out = out[out["ticker"].isin(symbols)].copy()
    if min_volume:
        out = out[pd.to_numeric(out["cboe_activity_volume"], errors="coerce").fillna(0) >= int(min_volume)].copy()
    if not out.empty:
        log.info("cboe_symbol_data: %d active option contracts", len(out))
    return out.reset_index(drop=True)
