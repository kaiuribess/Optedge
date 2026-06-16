"""Nasdaq Trader trade-halt RSS monitor.

Free/keyless source:
https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts

This is risk context for manual research. It is not an execution feed.
"""
from __future__ import annotations

import logging
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import pandas as pd

import data_provider

log = logging.getLogger("optedge.trading_halts")

TRADE_HALTS_RSS_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
SOURCE_NAME = "nasdaq_trader_trade_halts"
_NDAQ_NS = "http://www.nasdaqtrader.com/"


def _text(parent: ET.Element, tag: str) -> str:
    node = parent.find(tag)
    return (node.text or "").strip() if node is not None else ""


def _ndaq_text(parent: ET.Element, tag: str) -> str:
    node = parent.find(f"{{{_NDAQ_NS}}}{tag}")
    return (node.text or "").strip() if node is not None else ""


def _parse_pubdate(value: str) -> str | None:
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
        return parsed.isoformat()
    except Exception:
        return None


def _parse_halt_time(date_text: str, time_text: str) -> str | None:
    if not date_text or not time_text:
        return None
    clean_time = time_text.split()[0]
    for fmt in ("%m/%d/%Y %H:%M:%S.%f", "%m/%d/%Y %H:%M:%S"):
        try:
            dt = datetime.strptime(f"{date_text} {clean_time}", fmt)
            return dt.replace(tzinfo=ZoneInfo("America/New_York")).isoformat()
        except ValueError:
            continue
    return None


def _risk_score(reason_code: str, is_active: bool) -> int:
    reason = str(reason_code or "").upper()
    score = 92 if is_active else 68
    if reason.startswith(("T1", "T2", "T3")):
        score += 6
    elif reason.startswith("LUD"):
        score += 2
    elif reason.startswith(("H", "M")):
        score += 4
    return int(max(0, min(100, score)))


def parse_trade_halt_rss(xml_text: str) -> pd.DataFrame:
    """Parse Nasdaq Trader trade halt RSS into normalized rows."""
    text = (xml_text or "").lstrip("\ufeff").strip()
    if not text:
        return pd.DataFrame()
    root = ET.fromstring(text)
    channel = root.find("channel")
    if channel is None:
        return pd.DataFrame()
    feed_pubdate = _parse_pubdate(_text(channel, "pubDate"))
    rows: list[dict[str, Any]] = []
    for item in channel.findall("item"):
        symbol = (_ndaq_text(item, "IssueSymbol") or _text(item, "title")).upper()
        if not symbol:
            continue
        halt_date = _ndaq_text(item, "HaltDate")
        halt_time = _ndaq_text(item, "HaltTime")
        resume_trade = _ndaq_text(item, "ResumptionTradeTime")
        reason_code = _ndaq_text(item, "ReasonCode").upper()
        is_active = not bool(resume_trade)
        rows.append({
            "symbol": symbol,
            "name": _ndaq_text(item, "IssueName"),
            "market": _ndaq_text(item, "Market"),
            "reason_code": reason_code,
            "halt_date": halt_date,
            "halt_time": halt_time,
            "halted_at": _parse_halt_time(halt_date, halt_time),
            "pause_threshold_price": _ndaq_text(item, "PauseThresholdPrice") or None,
            "resumption_date": _ndaq_text(item, "ResumptionDate") or None,
            "resumption_quote_time": _ndaq_text(item, "ResumptionQuoteTime") or None,
            "resumption_trade_time": resume_trade or None,
            "active_halt": is_active,
            "halt_risk_score": _risk_score(reason_code, is_active),
            "published_at": _parse_pubdate(_text(item, "pubDate")) or feed_pubdate,
            "feed_published_at": feed_pubdate,
            "source": SOURCE_NAME,
            "source_url": TRADE_HALTS_RSS_URL,
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    return df.sort_values(
        ["active_halt", "halt_risk_score", "halted_at"],
        ascending=[False, False, False],
        na_position="last",
    ).reset_index(drop=True)


def fetch_trade_halts(cache_age: int = 60, max_rows: int = 200) -> pd.DataFrame:
    """Fetch current Nasdaq Trader halt RSS rows with a polite one-minute default cache."""
    max_rows = max(1, int(max_rows or 200))
    effective_cache = max(0, int(cache_age or 0))
    cache_key = "nasdaq_trade_halts:rss:v1"
    cached = data_provider.cache_get(cache_key, max_age_sec=effective_cache)
    if isinstance(cached, list):
        return pd.DataFrame(cached).head(max_rows)

    session = data_provider.get_session()
    try:
        response = session.get(TRADE_HALTS_RSS_URL, timeout=10)
        if getattr(response, "status_code", 200) != 200:
            log.debug("trade halts fetch status=%s", getattr(response, "status_code", None))
            return pd.DataFrame()
        df = parse_trade_halt_rss(getattr(response, "text", "") or "")
        rows = df.head(max_rows).to_dict("records")
        data_provider.cache_put(cache_key, rows)
        return pd.DataFrame(rows)
    except Exception as exc:
        log.debug("trade halts fetch failed: %s", exc)
        return pd.DataFrame()


def halt_rows_for_symbols(symbols: list[str], cache_age: int = 60) -> pd.DataFrame:
    wanted = {str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()}
    if not wanted:
        return pd.DataFrame()
    df = fetch_trade_halts(cache_age=cache_age)
    if df.empty or "symbol" not in df.columns:
        return pd.DataFrame()
    return df[df["symbol"].astype(str).str.upper().isin(wanted)].copy()
