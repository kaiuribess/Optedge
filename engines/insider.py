# Purpose: Insider activity engine — SEC EDGAR Form 4 over the last N days.
"""Insider activity engine — SEC EDGAR Form 4 over the last N days.

Strategy:
  1. For each ticker, look up CIK via EDGAR's company-tickers JSON.
  2. Fetch the company's recent filings index, filter for Form 4.
  3. For each Form 4, parse the XML to extract:
       transactionCode (P=open-market buy, S=sale, M/A=compensation, F=tax-withhold)
       officer/director title
       transaction $ value
  4. Suppress 10b5-1 sales / scheduled dispositions.
  5. Score: P-transactions by execs/directors weighted by $ size.
"""

from __future__ import annotations

import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider  # noqa: E402
from config import (  # noqa: E402
    INSIDER_FAST_MODE,
    INSIDER_LOOKBACK_DAYS,
    INSIDER_MAX_FILINGS_PER_TICKER,
    INSIDER_PRIORITY_TITLES,
    WORKERS_INSIDER,
)
from engines import finnhub_provider as fh  # noqa: E402
from optedge.http_identity import SecContactRequiredError, sec_headers  # noqa: E402

log = logging.getLogger("optedge.insider")

EDGAR_TICKERS = "https://www.sec.gov/files/company_tickers.json"
EDGAR_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
EDGAR_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{primary_doc}"


def _sec_headers() -> dict[str, str]:
    return sec_headers()


def _ticker_to_cik() -> dict[str, str]:
    """Fetch SEC's ticker→CIK map. Retries on 429, caches to disk for 24h.

    The map changes very rarely so we cache aggressively. Without this,
    a single 429 at startup makes the entire insider engine return zeros.
    """
    cached = data_provider.cache_get("edgar_cik_map", max_age_sec=86400)
    if cached:
        return cached

    for attempt in range(4):
        try:
            r = requests.get(EDGAR_TICKERS, headers=_sec_headers(), timeout=20)
            if r.status_code == 429:
                wait = 2**attempt
                log.warning("EDGAR 429 on attempt %d — backing off %ds", attempt + 1, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
            mapping = {
                row["ticker"].upper(): str(row["cik_str"]).zfill(10) for row in data.values()
            }
            data_provider.cache_put("edgar_cik_map", mapping)
            return mapping
        except SecContactRequiredError as e:
            log.warning("SEC insider source disabled: %s", e)
            return {}
        except Exception as e:
            log.warning("EDGAR ticker map attempt %d failed: %s", attempt + 1, e)
            time.sleep(2**attempt)
    log.error("EDGAR ticker map permanently failed — insider engine will return zeros")
    return {}


def _fetch_submissions(cik: str) -> dict | None:
    cache_key = f"edgar_submissions:{cik}"
    cached = data_provider.cache_get(cache_key, max_age_sec=12 * 3600)
    if cached:
        return cached
    try:
        r = requests.get(EDGAR_SUBMISSIONS.format(cik=cik), headers=_sec_headers(), timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        data_provider.cache_put(cache_key, data)
        return data
    except Exception as e:
        log.debug("submissions fail %s: %s", cik, e)
        return None


def _form4_filings(submissions: dict, cutoff: datetime) -> list[dict[str, Any]]:
    if not submissions:
        return []
    recent = submissions.get("filings", {}).get("recent", {})
    if not recent:
        return []
    forms = recent.get("form", [])
    accs = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])
    primaries = recent.get("primaryDocument", [])
    out = []
    for f, acc, dt, prim in zip(forms, accs, dates, primaries, strict=False):
        if f != "4":
            continue
        try:
            filed = datetime.strptime(dt, "%Y-%m-%d").replace(tzinfo=UTC)
        except Exception:
            continue
        if filed < cutoff:
            continue
        out.append({"acc": acc, "date": dt, "primary": prim})
    return out


def _parse_form4(cik: str, acc: str, primary: str) -> list[dict[str, Any]]:
    """Try the .xml form first; fall back to scraping the primary doc.

    EDGAR's `primaryDocument` field often points to an XSL-rendered HTML wrapper
    (e.g., 'xslF345X06/wk-form4_xxx.xml'). The raw XML lives at the parent path
    without the xslF345X06/ prefix.
    """
    cik_int = str(int(cik))
    acc_nodash = acc.replace("-", "")

    candidates = []
    # Strip xslF345X06/ wrapper prefix to get raw XML
    if primary.startswith("xslF345X06/"):
        candidates.append(primary[len("xslF345X06/") :])
    if primary.endswith(".xml"):
        candidates.append(primary)
    # Common conventional names
    candidates.append("primary_doc.xml")
    candidates.append(f"wf-form4_{acc_nodash}.xml")
    candidates.append(f"wk-form4_{acc_nodash}.xml")

    txt = None
    for cand in candidates:
        url = EDGAR_ARCHIVE.format(cik=cik_int, acc_nodash=acc_nodash, primary_doc=cand)
        try:
            r = requests.get(url, headers=_sec_headers(), timeout=10)
            if r.status_code == 200:
                content = r.text.lstrip()
                # Skip if we got the HTML wrapper instead of raw XML
                if content.startswith("<?xml") or content.startswith("<ownership"):
                    txt = r.text
                    break
                if content.startswith("<") and "ownershipDocument" in content[:1000]:
                    txt = r.text
                    break
        except Exception:
            continue

    if not txt:
        return []

    try:
        root = ET.fromstring(txt)
    except Exception:
        return []

    # Extract reporting owner relationship
    is_dir = root.findtext(".//reportingOwnerRelationship/isDirector") == "1"
    is_off = root.findtext(".//reportingOwnerRelationship/isOfficer") == "1"
    is_10pct = root.findtext(".//reportingOwnerRelationship/isTenPercentOwner") == "1"
    title = root.findtext(".//reportingOwnerRelationship/officerTitle") or ""
    title = title.upper()

    rows = []
    for tx in root.findall(".//nonDerivativeTransaction"):
        code = tx.findtext(".//transactionCoding/transactionCode") or ""
        # Shares & price
        shares_txt = tx.findtext(".//transactionAmounts/transactionShares/value")
        price_txt = tx.findtext(".//transactionAmounts/transactionPricePerShare/value")
        ad_txt = tx.findtext(".//transactionAmounts/transactionAcquiredDisposedCode/value")
        try:
            shares = float(shares_txt) if shares_txt else 0.0
            price = float(price_txt) if price_txt else 0.0
        except ValueError:
            continue
        value = shares * price
        rows.append(
            {
                "code": code,
                "ad": ad_txt,
                "shares": shares,
                "price": price,
                "value": value,
                "title": title,
                "is_director": is_dir,
                "is_officer": is_off,
                "is_10pct": is_10pct,
            }
        )
    return rows


def _score(rows: list[dict[str, Any]]) -> dict[str, float]:
    """Aggregate insider rows into a conviction score for one ticker."""
    if not rows:
        return {
            "insider_score": 0.0,
            "buys_value": 0.0,
            "sells_value": 0.0,
            "n_buys": 0,
            "n_sells": 0,
        }
    buy_val = 0.0
    sell_val = 0.0
    n_buy = 0
    n_sell = 0
    weighted_buy = 0.0
    for r in rows:
        is_priority = (
            r["is_officer"]
            or r["is_director"]
            or r["is_10pct"]
            or any(kw in r["title"] for kw in INSIDER_PRIORITY_TITLES)
        )
        weight = 2.0 if is_priority else 1.0
        if r["code"] == "P":
            buy_val += r["value"]
            n_buy += 1
            weighted_buy += r["value"] * weight
        elif r["code"] == "S":
            sell_val += r["value"]
            n_sell += 1
    net = weighted_buy - sell_val
    # Compress to [-1, 1]-ish via log scale
    import math

    score = math.copysign(math.log1p(abs(net) / 1e6), net)
    return {
        "insider_score": float(score),
        "buys_value": buy_val,
        "sells_value": sell_val,
        "n_buys": n_buy,
        "n_sells": n_sell,
    }


def _process_ticker(
    t: str, cik_map: dict[str, str], cutoff: datetime, fast_mode: bool = False
) -> dict[str, Any]:
    """Process one ticker. fast_mode skips XML parsing — gives count-only signals."""
    cik = cik_map.get(t.upper())
    if not cik:
        return {
            "ticker": t,
            "insider_score": 0.0,
            "buys_value": 0,
            "sells_value": 0,
            "n_buys": 0,
            "n_sells": 0,
            "n_form4": 0,
        }

    mode_key = "fast" if fast_mode else "full"
    cache_key = f"insider:{t}:lookback{INSIDER_LOOKBACK_DAYS}:{mode_key}:v2"
    cached = data_provider.cache_get(cache_key, max_age_sec=86400)
    if cached:
        return cached
    # Backward-compatible warm start: older versions keyed the cache by UTC
    # cutoff date, which caused a full cold SEC parse every midnight UTC.
    old_cache_key = f"insider:{t}:{cutoff.strftime('%Y%m%d')}:{mode_key}"
    cached = data_provider.cache_get(old_cache_key, max_age_sec=86400)
    if cached:
        data_provider.cache_put(cache_key, cached)
        return cached

    subs = _fetch_submissions(cik)
    filings = _form4_filings(subs, cutoff)

    if fast_mode:
        # Just use Form 4 count as a (weak) activity proxy. Skips ~12s of XML parsing.
        n = len(filings)
        # Mild positive score for any insider activity (assumes most filings are routine sales,
        # so a higher count is mostly negative; but we don't know without parsing).
        s = {
            "ticker": t,
            "insider_score": 0.0,
            "buys_value": 0,
            "sells_value": 0,
            "n_buys": 0,
            "n_sells": 0,
            "n_form4": n,
        }
        data_provider.cache_put(cache_key, s)
        return s

    rows = []
    for f in filings[:INSIDER_MAX_FILINGS_PER_TICKER]:
        parsed = _parse_form4(cik, f["acc"], f["primary"])
        rows.extend(parsed)
    s = _score(rows)
    s["ticker"] = t
    s["n_form4"] = len(filings)
    data_provider.cache_put(cache_key, s)
    return s


def _fetch_finnhub_insider_sentiment(ticker: str) -> dict[str, float]:
    """Finnhub's aggregate insider sentiment (MSPR — Monthly Share Purchase Ratio).
    Returns {finnhub_mspr: float, finnhub_change: float} or empty dict.
    """
    today = datetime.now(UTC)
    start = (today - timedelta(days=180)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    data = fh.get(
        "/stock/insider-sentiment", {"symbol": ticker, "from": start, "to": end}, cache_ttl=86400
    )
    if not data or not isinstance(data, dict):
        return {}
    rows = data.get("data", [])
    if not rows:
        return {}
    # Use the most recent month
    latest = rows[-1]
    return {
        "finnhub_mspr": float(latest.get("mspr") or 0),  # net % shares purchased
        "finnhub_change": float(latest.get("change") or 0),  # net share change
    }


def run(
    universe: list[str], max_workers: int = None, fast_mode: bool = None, finnhub_top_n: int = 50
) -> pd.DataFrame:
    """Parallel per-ticker processing. 16 concurrent threads × ~0.5s/req ≈ 8 req/sec,
    safely under SEC's recommended 10 req/sec.

    fast_mode skips XML parsing entirely — signal quality drops but speed is ~5x.
    finnhub_top_n: also pulls Finnhub aggregate insider sentiment for top N tickers.
    """
    if fast_mode is None:
        fast_mode = INSIDER_FAST_MODE
    try:
        _sec_headers()
    except SecContactRequiredError as e:
        log.warning("SEC insider source disabled: %s", e)
        cik_map = {}
    else:
        cik_map = _ticker_to_cik()
    cutoff = datetime.now(UTC) - timedelta(days=INSIDER_LOOKBACK_DAYS)
    workers = max_workers or WORKERS_INSIDER

    out = []
    completed = 0
    log.info(
        "processing %d tickers (parallel, %d workers, fast=%s, cap=%d filings/ticker)",
        len(universe),
        workers,
        fast_mode,
        INSIDER_MAX_FILINGS_PER_TICKER,
    )
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_process_ticker, t, cik_map, cutoff, fast_mode): t for t in universe}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                out.append(fut.result())
            except Exception as e:
                log.warning("insider fail %s: %s", t, str(e)[:120])
                out.append(
                    {
                        "ticker": t,
                        "insider_score": 0.0,
                        "buys_value": 0,
                        "sells_value": 0,
                        "n_buys": 0,
                        "n_sells": 0,
                        "n_form4": 0,
                    }
                )
            completed += 1
            if completed % 50 == 0 or completed == len(universe):
                log.info("[%d/%d]", completed, len(universe))

    df = pd.DataFrame(out)
    # Augment top-N with Finnhub MSPR (aggregate insider sentiment)
    targets = list(dict.fromkeys(universe))[:finnhub_top_n]
    finnhub_data = {}
    log.info("augmenting top %d tickers with Finnhub insider sentiment", len(targets))
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_fetch_finnhub_insider_sentiment, t): t for t in targets}
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                d = fut.result()
                if d:
                    finnhub_data[t] = d
            except Exception as e:
                log.debug("finnhub mspr fail %s: %s", t, e)
    if finnhub_data and not df.empty:
        df["finnhub_mspr"] = df["ticker"].map(
            lambda t: finnhub_data.get(t, {}).get("finnhub_mspr", 0)
        )
        df["finnhub_change"] = df["ticker"].map(
            lambda t: finnhub_data.get(t, {}).get("finnhub_change", 0)
        )
        # Boost insider_score with Finnhub MSPR signal (it's already directional)
        # MSPR > 0 means net buying, MSPR < 0 means net selling
        df["insider_score"] = df["insider_score"] + df["finnhub_mspr"].fillna(0) * 0.1
    return df
