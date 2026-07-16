# Purpose: SEC fails-to-deliver context engine.
"""SEC fails-to-deliver context engine.

Official, free, no-key source:
https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data

FTD rows are delayed settlement context, not proof of naked shorting and not a
standalone trade signal. Optedge uses this as a small non-option factor for
small-cap/share and futures ETF-proxy review.
"""

from __future__ import annotations

import io
import logging
import math
import re
import sys
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider  # noqa: E402
from optedge.http_identity import SecContactRequiredError, sec_headers  # noqa: E402

log = logging.getLogger("optedge.sec_ftd")

SEC_FTD_PAGE = "https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data"


def _sec_headers() -> dict[str, str]:
    return sec_headers()


def _latest_zip_urls(html: str, limit: int = 2) -> list[str]:
    """Return newest SEC FTD zip links in page order."""
    urls: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r'href=["\']([^"\']*cnsfails\d{6}[ab]\.zip)["\']', html, re.I):
        url = urljoin(SEC_FTD_PAGE, match.group(1))
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
        if len(urls) >= limit:
            break
    return urls


def _parse_ftd_text(text: str) -> pd.DataFrame:
    """Parse the SEC pipe-delimited FTD file into normalized columns."""
    try:
        df = pd.read_csv(io.StringIO(text), sep="|", dtype=str)
    except Exception:
        return pd.DataFrame()
    if df.empty:
        return pd.DataFrame()
    rename = {
        "SETTLEMENT DATE": "settlement_date",
        "CUSIP": "cusip",
        "SYMBOL": "ticker",
        "QUANTITY (FAILS)": "sec_ftd_fails",
        "DESCRIPTION": "sec_ftd_description",
        "PRICE": "sec_ftd_price",
    }
    df = df.rename(columns={c: rename.get(str(c).strip().upper(), c) for c in df.columns})
    required = {"settlement_date", "ticker", "sec_ftd_fails"}
    if not required.issubset(df.columns):
        return pd.DataFrame()
    out = df[[c for c in rename.values() if c in df.columns]].copy()
    out["ticker"] = out["ticker"].astype(str).str.strip().str.upper()
    out = out[out["ticker"] != ""]
    out["settlement_date"] = pd.to_datetime(
        out["settlement_date"].astype(str).str.strip(),
        format="%Y%m%d",
        errors="coerce",
    )
    out["sec_ftd_fails"] = pd.to_numeric(out["sec_ftd_fails"], errors="coerce").fillna(0)
    out["sec_ftd_price"] = pd.to_numeric(
        out.get("sec_ftd_price", pd.Series(index=out.index, dtype=object)).replace(".", None),
        errors="coerce",
    )
    out = out.dropna(subset=["settlement_date"])
    return out


def _fetch_ftd_page(max_age_sec: int = 6 * 3600) -> str:
    cache_key = "sec_ftd_page:v1"
    cached = data_provider.cache_get(cache_key, max_age_sec=max_age_sec)
    if isinstance(cached, str) and cached:
        return cached
    sess = data_provider.get_session()
    resp = sess.get(SEC_FTD_PAGE, headers=_sec_headers(), timeout=30)
    if getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(f"SEC FTD page returned HTTP {getattr(resp, 'status_code', 'unknown')}")
    text = getattr(resp, "text", "") or ""
    data_provider.cache_put(cache_key, text)
    return text


def _fetch_zip_frame(url: str, max_age_sec: int = 7 * 24 * 3600) -> pd.DataFrame:
    cache_key = f"sec_ftd_zip:{url}"
    cached = data_provider.cache_get(cache_key, max_age_sec=max_age_sec)
    if isinstance(cached, list):
        df = pd.DataFrame(cached)
        if not df.empty and "settlement_date" in df.columns:
            df["settlement_date"] = pd.to_datetime(df["settlement_date"], errors="coerce")
            df = df.dropna(subset=["settlement_date"])
        for col in ("sec_ftd_fails", "sec_ftd_price"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
    sess = data_provider.get_session()
    resp = sess.get(url, headers=_sec_headers(), timeout=45)
    if getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(f"SEC FTD zip returned HTTP {getattr(resp, 'status_code', 'unknown')}")
    raw = getattr(resp, "content", b"") or b""
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = [name for name in zf.namelist() if name.lower().endswith(".txt")]
        if not names:
            return pd.DataFrame()
        text = zf.read(names[0]).decode("latin-1", errors="replace")
    df = _parse_ftd_text(text)
    if not df.empty:
        df["sec_ftd_file"] = url.rsplit("/", 1)[-1]
        records = df.assign(settlement_date=df["settlement_date"].dt.strftime("%Y-%m-%d")).to_dict(
            "records"
        )
        data_provider.cache_put(cache_key, records)
    return df


def _score_ftd(fails: float, dollars: float, active_days: int) -> float:
    """Small positive settlement-pressure context score, capped conservatively."""
    fails = max(0.0, float(fails or 0.0))
    dollars = max(0.0, float(dollars or 0.0))
    score = 0.0
    if fails >= 100_000:
        score += 0.25
    if fails >= 500_000:
        score += 0.35
    if fails >= 1_000_000:
        score += 0.45
    if dollars >= 250_000:
        score += 0.25
    if dollars >= 1_000_000:
        score += 0.35
    if dollars >= 5_000_000:
        score += 0.45
    if active_days >= 3:
        score += 0.20
    if active_days >= 8:
        score += 0.20
    if fails > 0:
        score += min(0.30, math.log10(fails + 1) / 25.0)
    return round(min(score, 2.5), 3)


def _summarize_symbol(ticker: str, group: pd.DataFrame) -> dict[str, Any]:
    g = group.sort_values("settlement_date")
    latest = g.iloc[-1]
    fails = float(latest.get("sec_ftd_fails") or 0.0)
    price = latest.get("sec_ftd_price")
    price_f = float(price) if pd.notna(price) else 0.0
    dollars = fails * price_f if price_f > 0 else 0.0
    max_fails = float(pd.to_numeric(g["sec_ftd_fails"], errors="coerce").max() or 0.0)
    active_days = int(g["settlement_date"].nunique())
    return {
        "ticker": ticker,
        "sec_ftd_score": _score_ftd(fails, dollars, active_days),
        "sec_ftd_latest_date": latest["settlement_date"].strftime("%Y-%m-%d"),
        "sec_ftd_fails": int(fails),
        "sec_ftd_price": round(price_f, 4) if price_f else None,
        "sec_ftd_dollars": round(dollars, 2) if dollars else 0.0,
        "sec_ftd_max_fails": int(max_fails),
        "sec_ftd_active_days": active_days,
        "sec_ftd_description": latest.get("sec_ftd_description"),
        "sec_ftd_file": latest.get("sec_ftd_file"),
        "sec_ftd_source": "sec_fails_to_deliver",
        "sec_ftd_note": "Delayed SEC settlement context; not proof of abusive shorting.",
    }


def run(universe: list[str], max_files: int = 2) -> pd.DataFrame:
    """Return SEC FTD context rows for symbols in the current universe."""
    symbols = {str(t).strip().upper() for t in universe or [] if str(t).strip()}
    if not symbols:
        return pd.DataFrame()
    try:
        html = _fetch_ftd_page()
        urls = _latest_zip_urls(html, limit=max(1, int(max_files or 2)))
    except SecContactRequiredError as exc:
        log.warning("SEC fails-to-deliver source disabled: %s", exc)
        return pd.DataFrame()
    except Exception as exc:
        log.debug("sec_ftd page lookup failed: %s", exc)
        return pd.DataFrame()
    frames: list[pd.DataFrame] = []
    for url in urls:
        try:
            frame = _fetch_zip_frame(url)
        except Exception as exc:
            log.debug("sec_ftd fetch failed %s: %s", url, exc)
            continue
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = df[df["ticker"].isin(symbols)].copy()
    if df.empty:
        log.info("sec_ftd: 0 universe matches from %d latest file(s)", len(urls))
        return pd.DataFrame()
    rows = [_summarize_symbol(ticker, group) for ticker, group in df.groupby("ticker")]
    out = pd.DataFrame(rows).sort_values("sec_ftd_score", ascending=False).reset_index(drop=True)
    log.info("sec_ftd: %d tickers with delayed FTD context", len(out))
    return out
