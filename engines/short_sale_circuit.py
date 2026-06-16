"""Nasdaq Trader short-sale circuit breaker monitor.

Free/keyless source:
https://www.nasdaqtrader.com/trader.aspx?id=shortsalecircuitbreaker

This is Rule 201 / SSR context for research. It is not a standalone entry or
exit signal.
"""
from __future__ import annotations

import csv
import logging
import re
from datetime import datetime
from io import StringIO
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import pandas as pd

import data_provider

log = logging.getLogger("optedge.ssr")

SHORT_SALE_CIRCUIT_PAGE_URL = "https://www.nasdaqtrader.com/trader.aspx?id=shortsalecircuitbreaker"
SOURCE_NAME = "nasdaq_trader_short_sale_circuit_breaker"


def _clean_header(value: str) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


def _parse_trigger_time(value: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=ZoneInfo("America/New_York")).isoformat()
        except ValueError:
            continue
    return None


def parse_short_sale_circuit_file(text: str, source_url: str | None = None) -> pd.DataFrame:
    """Parse Nasdaq's CSV-style short-sale circuit breaker download file."""
    clean = (text or "").lstrip("\ufeff").strip()
    if not clean:
        return pd.DataFrame()
    reader = csv.reader(StringIO(clean))
    try:
        headers = [_clean_header(col) for col in next(reader)]
    except StopIteration:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    timestamp = None
    for parts in reader:
        if not parts:
            continue
        if len(parts) == 1 and re.fullmatch(r"\d{14}", parts[0].strip()):
            timestamp = parts[0].strip()
            continue
        if len(parts) < 4:
            continue
        padded = parts + [""] * max(0, len(headers) - len(parts))
        raw = {headers[idx]: padded[idx].strip() for idx in range(min(len(headers), len(padded)))}
        symbol = str(raw.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        trigger_time = raw.get("trigger_time") or ""
        rows.append({
            "symbol": symbol,
            "name": raw.get("security_name") or "",
            "market_category": raw.get("market_category") or "",
            "trigger_time": trigger_time,
            "triggered_at": _parse_trigger_time(trigger_time),
            "short_sale_restricted": True,
            "ssr_risk_score": 82,
            "source": SOURCE_NAME,
            "source_url": source_url,
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["file_timestamp"] = timestamp
    return df.sort_values(["triggered_at", "symbol"], ascending=[False, True], na_position="last").reset_index(drop=True)


def _extract_download_url(html: str) -> str | None:
    match = re.search(r'href=["\'](?P<url>[^"\']*shorthalts\d{8}\.txt)["\']', html or "", re.I)
    if match:
        return urljoin(SHORT_SALE_CIRCUIT_PAGE_URL, match.group("url"))
    match = re.search(r'(?P<url>/dynamic/symdir/shorthalts/shorthalts\d{8}\.txt)', html or "", re.I)
    if match:
        return urljoin(SHORT_SALE_CIRCUIT_PAGE_URL, match.group("url"))
    return None


def fetch_short_sale_circuit_breakers(cache_age: int = 30 * 60) -> pd.DataFrame:
    """Fetch current Nasdaq Trader short-sale circuit breaker rows."""
    cache_key = "nasdaq_short_sale_circuit_breakers:v1"
    cached = data_provider.cache_get(cache_key, max_age_sec=max(0, int(cache_age or 0)))
    if isinstance(cached, list):
        return pd.DataFrame(cached)

    session = data_provider.get_session()
    try:
        page = session.get(SHORT_SALE_CIRCUIT_PAGE_URL, timeout=12)
        if getattr(page, "status_code", 200) != 200:
            log.debug("short-sale circuit page status=%s", getattr(page, "status_code", None))
            return pd.DataFrame()
        download_url = _extract_download_url(getattr(page, "text", "") or "")
        if not download_url:
            log.debug("short-sale circuit download link missing")
            return pd.DataFrame()
        response = session.get(download_url, timeout=12)
        if getattr(response, "status_code", 200) != 200:
            log.debug("short-sale circuit file status=%s", getattr(response, "status_code", None))
            return pd.DataFrame()
        df = parse_short_sale_circuit_file(getattr(response, "text", "") or "", source_url=download_url)
        rows = df.to_dict("records")
        data_provider.cache_put(cache_key, rows)
        return pd.DataFrame(rows)
    except Exception as exc:
        log.debug("short-sale circuit fetch failed: %s", exc)
        return pd.DataFrame()


def circuit_rows_for_symbols(symbols: list[str], cache_age: int = 30 * 60) -> pd.DataFrame:
    wanted = {str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()}
    if not wanted:
        return pd.DataFrame()
    df = fetch_short_sale_circuit_breakers(cache_age=cache_age)
    if df.empty or "symbol" not in df.columns:
        return pd.DataFrame()
    return df[df["symbol"].astype(str).str.upper().isin(wanted)].copy()
