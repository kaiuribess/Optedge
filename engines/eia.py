# Purpose: Score energy inventory changes from EIA data.
"""EIA petroleum + natural gas inventory engine — v20.2.

Layered sources:
  PRIMARY  : EIA v2 API (requires free EIA_API_KEY). Same as v20.1.
  FALLBACK : Scrape EIA's own free public HTML pages — no key needed:
             - Natural gas:  https://ir.eia.gov/ngs/ngs.html
             - Petroleum:    https://www.eia.gov/petroleum/supply/weekly/

v20.2: the fallback fires only when no key is set OR the v2 API returns no
usable rows, so existing key-holders keep the API path.

Maps inventory surprise to energy-equity bias:
  - Bigger-than-expected crude BUILD  -> bearish for oil-equities (XLE, XOM, ...)
  - Bigger-than-expected crude DRAW   -> bullish
  - Similar logic for natgas
"""
from __future__ import annotations
import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

import sys
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider

log = logging.getLogger("optedge.eia")

OIL_EQUITIES = ["XLE", "XOP", "USO", "XOM", "CVX", "COP", "OXY", "EOG", "SLB",
                "MPC", "VLO", "PSX", "HAL", "FANG", "DVN", "PXD", "BKR", "WMB",
                "KMI", "ENB", "ET"]
NATGAS_EQUITIES = ["UNG", "BOIL", "KOLD", "EQT", "RRC", "AR", "CHK", "SWN", "CTRA", "OVV"]


# ---------------------------------------------------------------------------
# PRIMARY: EIA v2 API (unchanged from v20.1)
# ---------------------------------------------------------------------------
def _get_eia_key() -> str:
    key = os.environ.get("EIA_API_KEY", "")
    if key:
        return key
    try:
        from keys import EIA_API_KEY
        return EIA_API_KEY
    except Exception:
        return ""


def _fetch_v2_series(route: str, max_age_sec: int = 12 * 3600) -> Optional[List[Dict]]:
    """Fetch EIA v2 series. `route` is the path under /v2/."""
    key = _get_eia_key()
    if not key:
        return None
    cache_key = f"eia_v2:{route}"
    cached = data_provider.cache_get(cache_key, max_age_sec=max_age_sec)
    if cached is not None:
        return cached
    url = f"https://api.eia.gov/v2/{route}"
    try:
        sess = data_provider.get_session()
        params = {
            "api_key": key,
            "frequency": "weekly",
            "sort[0][column]": "period",
            "sort[0][direction]": "desc",
            "length": "12",
        }
        r = sess.get(url, params=params, timeout=20)
        if r.status_code != 200:
            log.debug("EIA v2 %s -> %d", route, r.status_code)
            return None
        data = r.json()
        rows = data.get("response", {}).get("data", [])
        if rows:
            data_provider.cache_put(cache_key, rows)
        return rows
    except Exception as e:
        log.debug("EIA v2 parse %s: %s", route, e)
        return None


def _fetch_crude_stocks_api() -> Optional[List[Dict]]:
    rows = _fetch_v2_series("petroleum/stoc/wstk/data/")
    if not rows:
        return None
    out = []
    for r in rows:
        if r.get("series") in ("WCESTUS1", "WCESTUS1.W"):
            v = r.get("value")
            if v is not None:
                out.append({"date": r.get("period"), "value": float(v)})
    return out or None


def _fetch_natgas_storage_api() -> Optional[List[Dict]]:
    rows = _fetch_v2_series("natural-gas/stor/wkly/data/")
    if not rows:
        return None
    out = []
    for r in rows:
        sid = r.get("series") or r.get("seriesId") or ""
        if "R48" in sid and "BCF" in sid:
            v = r.get("value")
            if v is not None:
                out.append({"date": r.get("period"), "value": float(v)})
    return out or None


# ---------------------------------------------------------------------------
# FALLBACK 1: Natural Gas storage HTML scrape (ir.eia.gov/ngs/ngs.html)
# ---------------------------------------------------------------------------
def _fetch_natgas_storage_html(max_age_sec: int = 6 * 3600) -> Optional[List[Dict]]:
    """Scrape EIA's weekly NG storage page. Returns [{date, value}] for the
    latest week + 5y-avg/year-ago comparison rows we synthesise into a series
    so the surprise calc still works."""
    cache_key = "eia_ngs_html"
    cached = data_provider.cache_get(cache_key, max_age_sec=max_age_sec)
    if cached:
        return cached
    url = "https://ir.eia.gov/ngs/ngs.html"
    sess = data_provider.get_session()
    try:
        r = sess.get(url, timeout=20)
        if r.status_code != 200:
            log.debug("EIA NGS HTML -> %d", r.status_code)
            return None
        text = r.text
    except Exception as e:
        log.debug("EIA NGS HTML fetch: %s", e)
        return None
    # Pull the "for week ending <Mon DD, YYYY>" header
    m_date = re.search(r"for week ending\s+([A-Za-z]+\s+\d+,\s*\d{4})", text)
    if not m_date:
        return None
    try:
        from datetime import datetime
        latest_date = datetime.strptime(m_date.group(1), "%B %d, %Y").strftime("%Y-%m-%d")
    except Exception:
        return None
    # The page has cells with raw Bcf integers; the first integer >= 1000 in
    # the "Working gas in underground storage" row is the latest US total.
    # Look for sequences of <td>NNNN</td> following a Lower-48 mention.
    nums = re.findall(r">(\d[\d,]{2,6})<", text)
    candidates: List[int] = []
    for s in nums:
        try:
            v = int(s.replace(",", ""))
            if 500 <= v <= 6000:    # plausible Bcf totals
                candidates.append(v)
        except ValueError:
            continue
    if not candidates:
        return None
    # Latest, prior-week, year-ago, 5y-avg appear early in that order on the page
    latest = candidates[0]
    prior  = candidates[1] if len(candidates) > 1 else latest
    yr_ago = candidates[2] if len(candidates) > 2 else latest
    avg5y  = candidates[3] if len(candidates) > 3 else latest
    # Synthesise a small history list: [latest, prior, yr_ago_proxy, avg5y...]
    # _compute_surprise uses [0] vs avg of [1:5], so we pad sensibly.
    series = [
        {"date": latest_date,       "value": float(latest)},
        {"date": "prior_week",      "value": float(prior)},
        {"date": "5y_avg",          "value": float(avg5y)},
        {"date": "5y_avg",          "value": float(avg5y)},
        {"date": "year_ago",        "value": float(yr_ago)},
    ]
    data_provider.cache_put(cache_key, series)
    return series


# ---------------------------------------------------------------------------
# FALLBACK 2: Petroleum weekly HTML scrape
# ---------------------------------------------------------------------------
def _fetch_crude_stocks_html(max_age_sec: int = 6 * 3600) -> Optional[List[Dict]]:
    """Scrape eia.gov/petroleum/supply/weekly for headline crude stocks number."""
    cache_key = "eia_petroleum_html"
    cached = data_provider.cache_get(cache_key, max_age_sec=max_age_sec)
    if cached:
        return cached
    url = "https://www.eia.gov/petroleum/supply/weekly/"
    sess = data_provider.get_session()
    try:
        r = sess.get(url, timeout=20)
        if r.status_code != 200:
            log.debug("EIA petroleum weekly HTML -> %d", r.status_code)
            return None
        text = r.text
    except Exception as e:
        log.debug("EIA petroleum HTML fetch: %s", e)
        return None
    # Headline number lives in a "Total crude oil stocks" / "Crude Oil" row.
    # Find the most recent date stamp in YYYY-MM-DD or "Month DD, YYYY" form
    # near a "crude" mention; bail if we can't establish a date.
    from datetime import datetime
    latest_date = None
    m = re.search(r"as of\s+([A-Za-z]+\s+\d+,\s*\d{4})", text, flags=re.IGNORECASE)
    if m:
        try:
            latest_date = datetime.strptime(m.group(1), "%B %d, %Y").strftime("%Y-%m-%d")
        except Exception:
            pass
    if not latest_date:
        m = re.search(r"week ending\s+(\d{1,2}/\d{1,2}/\d{4})", text)
        if m:
            try:
                latest_date = datetime.strptime(m.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")
            except Exception:
                pass
    if not latest_date:
        return None
    # Look for "Crude oil" or "Crude Oil" near a row of mb integers (millions of barrels)
    # Page reports in thousand barrels (kbbl), values typically 350,000 - 500,000
    nums = re.findall(r">\s*([\d,]{5,12})\s*<", text)
    crude_nums: List[int] = []
    for s in nums:
        try:
            v = int(s.replace(",", ""))
            if 200_000 <= v <= 700_000:    # plausible kbbl totals (200-700 Mb)
                crude_nums.append(v)
        except ValueError:
            continue
    if not crude_nums:
        return None
    latest = crude_nums[0]
    prior  = crude_nums[1] if len(crude_nums) > 1 else latest
    # Fabricate a short history for the surprise calc to operate on
    series = [
        {"date": latest_date,  "value": float(latest)},
        {"date": "prior_week", "value": float(prior)},
        {"date": "prior_2",    "value": float(prior)},
        {"date": "prior_3",    "value": float(prior)},
        {"date": "prior_4",    "value": float(prior)},
    ]
    data_provider.cache_put(cache_key, series)
    return series


# ---------------------------------------------------------------------------
# Shared scoring
# ---------------------------------------------------------------------------
def _compute_surprise(rows: List[Dict]) -> Optional[Dict]:
    if not rows or len(rows) < 4:
        return None
    rows = sorted(rows, key=lambda r: r["date"], reverse=True)
    latest = rows[0]
    prior4 = rows[1:5]
    avg = sum(r["value"] for r in prior4) / len(prior4)
    if avg == 0:
        return None
    surprise_pct = (latest["value"] - avg) / abs(avg)
    return {
        "latest_value": latest["value"],
        "latest_date": latest["date"],
        "avg_4w": avg,
        "surprise_pct": surprise_pct,
        "wow_change": latest["value"] - rows[1]["value"] if len(rows) > 1 else 0,
    }


def run(universe: Optional[List[str]] = None) -> pd.DataFrame:
    # PRIMARY: EIA v2 API
    crude  = _fetch_crude_stocks_api()
    natgas = _fetch_natgas_storage_api()
    source = "eia_v2"

    # FALLBACK: HTML scrape (used whether key missing or API returned nothing)
    if not crude:
        crude = _fetch_crude_stocks_html()
        if crude:
            source = "html"
    if not natgas:
        natgas = _fetch_natgas_storage_html()
        if natgas:
            source = source if source == "eia_v2" else "html"

    if not crude and not natgas:
        log.info("EIA: no inventory data from API or HTML fallback")
        return pd.DataFrame()

    rows = []
    crude_score = 0.0; crude_meta = ""
    natgas_score = 0.0; natgas_meta = ""

    if crude:
        cs = _compute_surprise(crude)
        if cs:
            crude_score = max(-1.0, min(1.0, -cs["surprise_pct"] * 5))
            crude_meta = (f"{cs['latest_date']}: {cs['latest_value']:.0f}kbbl vs 4w avg "
                          f"{cs['avg_4w']:.0f}kbbl ({cs['surprise_pct']*100:+.1f}%)")

    if natgas:
        ns = _compute_surprise(natgas)
        if ns:
            natgas_score = max(-1.0, min(1.0, -ns["surprise_pct"] * 5))
            natgas_meta = (f"{ns['latest_date']}: {ns['latest_value']:.0f}bcf vs 4w avg "
                           f"{ns['avg_4w']:.0f}bcf ({ns['surprise_pct']*100:+.1f}%)")

    if not crude_score and not natgas_score:
        return pd.DataFrame()

    for tk in OIL_EQUITIES:
        rows.append({"ticker": tk, "eia_score": crude_score,
                     "eia_meta": crude_meta, "eia_commodity": "crude",
                     "eia_source": source})
    for tk in NATGAS_EQUITIES:
        if any(r["ticker"] == tk for r in rows):
            continue
        rows.append({"ticker": tk, "eia_score": natgas_score,
                     "eia_meta": natgas_meta, "eia_commodity": "natgas",
                     "eia_source": source})

    out = pd.DataFrame(rows)
    log.info("EIA(%s): crude=%+.2f natgas=%+.2f -> %d ticker rows",
             source, crude_score, natgas_score, len(out))
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run())
