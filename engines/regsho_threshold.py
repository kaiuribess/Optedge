# Purpose: Nasdaq Trader Reg SHO threshold-security monitor.
"""Nasdaq Trader Reg SHO threshold-security monitor.

Free/keyless source:
https://www.nasdaqtrader.com/trader.aspx?id=regshothreshold

This is settlement/short-pressure context for research. It is not a standalone
entry or exit signal.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urljoin

import pandas as pd

import data_provider

log = logging.getLogger("optedge.regsho")

REGSHO_PAGE_URL = "https://www.nasdaqtrader.com/trader.aspx?id=regshothreshold"
SOURCE_NAME = "nasdaq_trader_regsho_threshold"


def _clean_header(value: str) -> str:
    return str(value or "").strip().lower().replace(" ", "_").replace("/", "_")


def parse_threshold_file(text: str, source_url: str | None = None) -> pd.DataFrame:
    """Parse Nasdaq's pipe-delimited Reg SHO threshold download file."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return pd.DataFrame()
    header = [_clean_header(part) for part in lines[0].split("|")]
    rows: list[dict[str, Any]] = []
    timestamp = None
    for raw in lines[1:]:
        parts = raw.split("|")
        if len(parts) == 1 and re.fullmatch(r"\d{14}", parts[0]):
            timestamp = parts[0]
            continue
        if len(parts) < 5:
            continue
        padded = parts + [""] * max(0, len(header) - len(parts))
        row = {header[idx]: padded[idx].strip() for idx in range(min(len(header), len(padded)))}
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        regsho_flag = str(row.get("reg_sho_threshold_flag") or "").strip().upper()
        rule_3210 = str(row.get("rule_3210") or "").strip().upper()
        rows.append(
            {
                "symbol": symbol,
                "name": row.get("security_name") or "",
                "market_category": row.get("market_category") or "",
                "reg_sho_threshold_flag": regsho_flag,
                "rule_3210": rule_3210,
                "is_threshold": regsho_flag == "Y" or rule_3210 == "Y",
                "settlement_risk_score": 86
                if regsho_flag == "Y"
                else 78
                if rule_3210 == "Y"
                else 55,
                "source": SOURCE_NAME,
                "source_url": source_url,
            }
        )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["file_timestamp"] = timestamp
    return df.sort_values(
        ["is_threshold", "settlement_risk_score", "symbol"],
        ascending=[False, False, True],
    ).reset_index(drop=True)


def _extract_download_url(html: str) -> str | None:
    match = re.search(r'href=["\'](?P<url>[^"\']*nasdaqth\d{8}\.txt)["\']', html or "", re.I)
    if match:
        return urljoin(REGSHO_PAGE_URL, match.group("url"))
    match = re.search(r"(?P<url>/dynamic/symdir/regsho/nasdaqth\d{8}\.txt)", html or "", re.I)
    if match:
        return urljoin(REGSHO_PAGE_URL, match.group("url"))
    return None


def fetch_threshold_list(cache_age: int = 6 * 3600) -> pd.DataFrame:
    """Fetch the current Nasdaq Trader threshold list using the official download link."""
    cache_key = "nasdaq_regsho_threshold:v1"
    cached = data_provider.cache_get(cache_key, max_age_sec=max(0, int(cache_age or 0)))
    if isinstance(cached, list):
        return pd.DataFrame(cached)

    session = data_provider.get_session()
    try:
        page = session.get(REGSHO_PAGE_URL, timeout=12)
        if getattr(page, "status_code", 200) != 200:
            log.debug("regsho page status=%s", getattr(page, "status_code", None))
            return pd.DataFrame()
        download_url = _extract_download_url(getattr(page, "text", "") or "")
        if not download_url:
            log.debug("regsho download link missing")
            return pd.DataFrame()
        response = session.get(download_url, timeout=12)
        if getattr(response, "status_code", 200) != 200:
            log.debug("regsho file status=%s", getattr(response, "status_code", None))
            return pd.DataFrame()
        df = parse_threshold_file(getattr(response, "text", "") or "", source_url=download_url)
        rows = df.to_dict("records")
        data_provider.cache_put(cache_key, rows)
        return pd.DataFrame(rows)
    except Exception as exc:
        log.debug("regsho threshold fetch failed: %s", exc)
        return pd.DataFrame()


def threshold_rows_for_symbols(symbols: list[str], cache_age: int = 6 * 3600) -> pd.DataFrame:
    wanted = {str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()}
    if not wanted:
        return pd.DataFrame()
    df = fetch_threshold_list(cache_age=cache_age)
    if df.empty or "symbol" not in df.columns:
        return pd.DataFrame()
    return df[df["symbol"].astype(str).str.upper().isin(wanted)].copy()
