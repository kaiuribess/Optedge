# Purpose: Nasdaq public stock screener helpers.
"""Nasdaq public stock screener helpers.

Free/no-key source used for small-cap discovery. This is delayed research data,
not an execution quote feed.
"""
from __future__ import annotations

import json
import logging
import math
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

import pandas as pd

import data_provider

log = logging.getLogger("optedge.nasdaq_screener")

NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks"
NASDAQ_SCREENER_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}


def _num(value: Any) -> float:
    text = str(value or "").strip()
    if not text or text in {"--", "N/A"}:
        return float("nan")
    text = text.replace("$", "").replace(",", "").replace("%", "")
    try:
        return float(text)
    except ValueError:
        return float("nan")


def _screener_cache_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    return df.to_dict("records")


def fetch_stock_screener(cache_age: int = 1800) -> pd.DataFrame:
    """Fetch Nasdaq's public stock screener table.

    The endpoint usually returns the full table when `download=true`. Keep this
    cached because the payload is large enough that it should not be hit on
    every dashboard refresh.
    """
    key = "nasdaq_screener:stocks:download"
    cached = data_provider.cache_get(key, cache_age)
    if cached is not None:
        df = pd.DataFrame(cached)
        if not df.empty:
            df.attrs["source"] = "nasdaq_screener"
        return df

    params = urllib.parse.urlencode({
        "tableonly": "true",
        "limit": "10000",
        "offset": "0",
        "download": "true",
    })
    req = urllib.request.Request(
        f"{NASDAQ_SCREENER_URL}?{params}",
        headers=NASDAQ_SCREENER_HEADERS,
    )
    try:
        with urllib.request.urlopen(req, timeout=18) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        log.debug("nasdaq screener fetch failed: %s", exc)
        return pd.DataFrame()

    rows = ((payload or {}).get("data") or {}).get("rows") or []
    parsed: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol or symbol.startswith("^") or "/" in symbol:
            continue
        parsed.append({
            "symbol": symbol,
            "name": str(row.get("name") or "").strip(),
            "last_price": _num(row.get("lastsale")),
            "net_change": _num(row.get("netchange")),
            "pct_change": _num(row.get("pctchange")),
            "volume": _num(row.get("volume")),
            "market_cap": _num(row.get("marketCap")),
            "country": str(row.get("country") or "").strip(),
            "sector": str(row.get("sector") or "").strip(),
            "industry": str(row.get("industry") or "").strip(),
            "url": str(row.get("url") or "").strip(),
            "source": "nasdaq_screener",
            "quote_quality": "free_or_delayed",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })
    df = pd.DataFrame(parsed)
    if not df.empty:
        df.attrs["source"] = "nasdaq_screener"
        data_provider.cache_put(key, _screener_cache_records(df))
    return df


def _small_cap_score(row: pd.Series) -> int:
    pct = abs(_num(row.get("pct_change")))
    vol = _num(row.get("volume"))
    cap = _num(row.get("market_cap"))
    price = _num(row.get("last_price"))
    score = 35.0
    if math.isfinite(pct):
        score += min(28.0, pct * 3.0)
    if math.isfinite(vol):
        score += min(20.0, math.log10(max(vol, 1.0)) * 3.2)
    if math.isfinite(cap) and cap > 0:
        if cap <= 300_000_000:
            score += 12
        elif cap <= 1_000_000_000:
            score += 8
        elif cap <= 2_000_000_000:
            score += 4
    if math.isfinite(price):
        if 1.0 <= price <= 15.0:
            score += 6
        elif price < 1.0:
            score -= 18
    return int(max(0, min(100, round(score))))


def small_cap_movers(
    *,
    max_rows: int = 30,
    min_price: float = 1.0,
    max_price: float = 35.0,
    min_volume: float = 250_000,
    min_abs_pct_change: float = 2.0,
    min_market_cap: float = 25_000_000,
    max_market_cap: float = 2_000_000_000,
    cache_age: int = 1800,
) -> pd.DataFrame:
    """Return a ranked small-cap mover watchlist from the public Nasdaq screener."""
    df = fetch_stock_screener(cache_age=cache_age)
    if df.empty:
        return df
    out = df.copy()
    for col in ("last_price", "pct_change", "volume", "market_cap"):
        out[col] = pd.to_numeric(out.get(col), errors="coerce")
    out = out[
        out["last_price"].between(min_price, max_price, inclusive="both")
        & out["market_cap"].between(min_market_cap, max_market_cap, inclusive="both")
        & (out["volume"] >= min_volume)
        & (out["pct_change"].abs() >= min_abs_pct_change)
    ].copy()
    if out.empty:
        return out
    out["nasdaq_mover_score"] = out.apply(_small_cap_score, axis=1)
    out["mover_direction"] = out["pct_change"].apply(lambda value: "up" if value >= 0 else "down")
    out["market_cap_bucket"] = pd.cut(
        out["market_cap"],
        bins=[0, 300_000_000, 1_000_000_000, 2_000_000_000],
        labels=["micro", "small", "upper_small"],
        include_lowest=True,
    ).astype(str)
    out = out.sort_values(
        ["nasdaq_mover_score", "volume", "pct_change"],
        ascending=[False, False, False],
        kind="mergesort",
    )
    return out.head(max(1, int(max_rows))).reset_index(drop=True)


if __name__ == "__main__":
    movers = small_cap_movers(max_rows=20)
    print(movers[["symbol", "last_price", "pct_change", "volume", "market_cap", "nasdaq_mover_score"]])
