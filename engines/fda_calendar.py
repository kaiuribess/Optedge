# Purpose: Aggregate upcoming biotech regulatory catalysts.
"""FDA catalyst calendar for biotech tickers — v20.2 (layered sources).

For each biotech-classified ticker in the universe we want:
  - days_to_next_catalyst (PDUFA, ADCOM, CHMP, topline data)
  - catalyst_type
  - catalyst_score: high when within 7-30 day window, decays outside

Sources (tried in order, results merged):
  1. BioPharmCatalyst FDA calendar HTML  (kept from v20 — may be CF-protected)
  2. RTTNews FDA calendar HTML            (kept from v20)
  3. Drugs.com new-drugs page             (kept from v20 — may be Akamai-blocked)
  4. openFDA recent submissions JSON      (new — keyless, official FDA feed)
  5. SEC EDGAR 8-K "PDUFA" full-text       (new — keyless, finds forward-looking
                                            PDUFA date mentions in company filings)

Per-ticker, we keep the SOONEST upcoming catalyst across all sources.
"""
from __future__ import annotations
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Set

import requests
import pandas as pd

import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider
from optedge.http_identity import SecContactRequiredError, outbound_headers, sec_headers

log = logging.getLogger("optedge.fda")

CACHE_KEY_PRIMARY = "fda_calendar"
CACHE_TTL_SEC = 86400      # FDA calendar updates ~once per day



def _sec_headers() -> dict[str, str]:
    return sec_headers(accept="application/json, text/plain, */*")

BIOTECH_TICKERS = {
    "MRNA", "BNTX", "NVAX", "OCGN", "VKTX", "SAVA", "SRPT", "BLUE", "FATE",
    "CRSP", "EDIT", "NTLA", "BEAM", "ARWR", "HALO", "EXEL", "INSM",
    "TVTX", "AKBA", "VANI", "ANIP", "PRTA", "CRDF", "IOVA", "LXRX",
    "REGN", "VRTX", "BIIB", "GILD", "AMGN", "BMY", "MRK", "LLY", "PFE",
    "ABBV", "NVO", "AZN", "BAYRY", "ROCHE",
}


# ---------------------------------------------------------------------------
# Source 1: BioPharmCatalyst (unchanged from v20)
# ---------------------------------------------------------------------------
def _fetch_biopharmcatalyst() -> List[Dict[str, Any]]:
    url = "https://www.biopharmcatalyst.com/calendars/fda-calendar"
    try:
        r = requests.get(url, headers=outbound_headers(), timeout=20)
        if r.status_code != 200:
            log.debug("biopharmcatalyst -> %d", r.status_code)
            return []
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")
        rows = []
        for table in soup.find_all("table"):
            for tr in table.find_all("tr"):
                cells = tr.find_all(["td", "th"])
                if len(cells) < 3:
                    continue
                texts = [c.get_text(" ", strip=True) for c in cells]
                ticker_match = None
                for txt in texts:
                    m = re.match(r"^([A-Z]{1,5})$", txt.strip())
                    if m:
                        ticker_match = m.group(1)
                        break
                if not ticker_match:
                    continue
                date_match = None
                for txt in texts:
                    m = (re.search(r"(\w+\s+\d{1,2},?\s*\d{4})", txt) or
                         re.search(r"(\d{1,2}/\d{1,2}/\d{4})", txt) or
                         re.search(r"(\d{4}-\d{2}-\d{2})", txt))
                    if m:
                        date_match = m.group(1)
                        break
                full_text = " | ".join(texts).lower()
                if "pdufa" in full_text: catalyst_type = "PDUFA"
                elif "adcom" in full_text or "advisory" in full_text: catalyst_type = "ADCOM"
                elif "topline" in full_text or "phase" in full_text: catalyst_type = "TOPLINE"
                elif "chmp" in full_text: catalyst_type = "CHMP"
                else: catalyst_type = "EVENT"
                rows.append({"ticker": ticker_match, "date_str": date_match,
                             "type": catalyst_type, "source": "biopharmcatalyst"})
        log.info("fda: biopharmcatalyst -> %d rows", len(rows))
        return rows
    except Exception as e:
        log.debug("biopharmcatalyst fetch failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Source 2: RTTNews (kept from v20)
# ---------------------------------------------------------------------------
def _fetch_rttnews() -> List[Dict[str, Any]]:
    url = "https://www.rttnews.com/CorpInfo/FDACalendar.aspx"
    try:
        r = requests.get(url, headers=outbound_headers(), timeout=20)
        if r.status_code != 200:
            log.debug("rttnews -> %d", r.status_code)
            return []
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")
        rows = []
        for tr in soup.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) < 3:
                continue
            texts = [c.get_text(" ", strip=True) for c in cells]
            ticker_match = None
            for txt in texts:
                m = re.search(r"\b([A-Z]{2,5})\b", txt.strip())
                if m and m.group(1) not in {"FDA", "PDUFA", "ADCOM", "CHMP", "NDA",
                                            "BLA", "ANDA", "REMS", "EUA"}:
                    ticker_match = m.group(1)
                    break
            if not ticker_match:
                continue
            date_match = None
            for txt in texts:
                m = (re.search(r"(\w+\s+\d{1,2},?\s*\d{4})", txt) or
                     re.search(r"(\d{1,2}/\d{1,2}/\d{4})", txt))
                if m:
                    date_match = m.group(1)
                    break
            full_text = " | ".join(texts).lower()
            if "pdufa" in full_text: catalyst_type = "PDUFA"
            elif "adcom" in full_text or "advisory" in full_text: catalyst_type = "ADCOM"
            elif "topline" in full_text or "phase" in full_text: catalyst_type = "TOPLINE"
            else: catalyst_type = "EVENT"
            rows.append({"ticker": ticker_match, "date_str": date_match,
                         "type": catalyst_type, "source": "rttnews"})
        log.info("fda: rttnews -> %d rows", len(rows))
        return rows
    except Exception as e:
        log.debug("rttnews fetch failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Source 3: Drugs.com new-drugs page (kept from v20)
# ---------------------------------------------------------------------------
def _fetch_drugscom() -> List[Dict[str, Any]]:
    url = "https://www.drugs.com/new-drugs.html"
    try:
        r = requests.get(url, headers=outbound_headers(), timeout=20)
        if r.status_code != 200:
            log.debug("drugs.com -> %d", r.status_code)
            return []
        # Drugs.com lists drug names but rarely tickers. We extract any cap-letter
        # patterns next to recognizable date strings.
        rows: List[Dict[str, Any]] = []
        text = r.text
        for m in re.finditer(r"\b([A-Z]{2,5})\b[^<]{0,80}?(\w+\s+\d{1,2},?\s*\d{4})", text):
            tk = m.group(1)
            if tk in {"FDA", "PDUFA", "ADCOM", "CHMP", "NDA", "BLA"}:
                continue
            rows.append({"ticker": tk, "date_str": m.group(2),
                         "type": "EVENT", "source": "drugscom"})
        log.info("fda: drugs.com -> %d rows", len(rows))
        return rows
    except Exception as e:
        log.debug("drugs.com fetch failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Source 4 (NEW): openFDA recent supplements feed (keyless, official)
# ---------------------------------------------------------------------------
def _fetch_openfda() -> List[Dict[str, Any]]:
    """openFDA exposes drug approval submissions. Free, no key. Doesn't give
    forward PDUFA dates directly, but the most recent SUPPL submissions act
    as a proxy: a fresh supplement means the FDA is actively reviewing that
    sponsor's product. We don't know the PDUFA, so we assign a generic
    'EVENT' type and decay using submission_status_date."""
    cache_key = "openfda_supplements"
    cached = data_provider.cache_get(cache_key, max_age_sec=24 * 3600)
    if cached is not None:
        return cached
    url = "https://api.fda.gov/drug/drugsfda.json"
    sess = data_provider.get_session()
    try:
        r = sess.get(
            url,
            params={"limit": 100, "search": "submissions.submission_status:'AP'"},
            headers=outbound_headers(accept="application/json"),
            timeout=20,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        rows: List[Dict[str, Any]] = []
        for rec in data.get("results", []):
            sponsor = (rec.get("sponsor_name") or "").upper()
            # Crude sponsor->ticker heuristic: try a few common mappings
            ticker = _sponsor_to_ticker(sponsor)
            if not ticker:
                continue
            subs = rec.get("submissions") or []
            for sub in subs[:3]:
                d = sub.get("submission_status_date")
                if not d:
                    continue
                rows.append({
                    "ticker": ticker,
                    "date_str": d,                        # YYYYMMDD
                    "type": "EVENT",
                    "source": "openfda",
                })
        if rows:
            data_provider.cache_put(cache_key, rows)
        log.info("fda: openFDA -> %d rows", len(rows))
        return rows
    except Exception as e:
        log.debug("openFDA fetch failed: %s", e)
        return []


_SPONSOR_MAP = {
    "PFIZER": "PFE", "MERCK SHARP": "MRK", "MERCK & CO": "MRK",
    "ELI LILLY": "LLY", "ABBVIE": "ABBV", "BRISTOL MYERS": "BMY",
    "BRISTOL-MYERS": "BMY", "JOHNSON": "JNJ", "ASTRAZENECA": "AZN",
    "NOVARTIS": "NVS", "GLAXOSMITHKLINE": "GSK", "GSK ": "GSK",
    "SANOFI": "SNY", "GILEAD": "GILD", "AMGEN": "AMGN",
    "BIOGEN": "BIIB", "REGENERON": "REGN", "VERTEX": "VRTX",
    "MODERNA": "MRNA", "BIONTECH": "BNTX", "NOVAVAX": "NVAX",
    "SAREPTA": "SRPT", "BLUEBIRD": "BLUE", "CRISPR": "CRSP",
    "EDITAS": "EDIT", "INTELLIA": "NTLA", "BEAM THERAPEUTICS": "BEAM",
    "ARROWHEAD": "ARWR", "HALOZYME": "HALO", "EXELIXIS": "EXEL",
    "INSMED": "INSM", "VIKING THERAPEUTICS": "VKTX",
    "CASSAVA": "SAVA", "OCUGEN": "OCGN", "FATE THERAPEUTICS": "FATE",
}

def _sponsor_to_ticker(sponsor: str) -> Optional[str]:
    if not sponsor:
        return None
    sp = sponsor.upper()
    for key, tk in _SPONSOR_MAP.items():
        if key in sp:
            return tk
    return None


# ---------------------------------------------------------------------------
# Source 5 (NEW): SEC EDGAR 8-K full-text "PDUFA" mentions
# ---------------------------------------------------------------------------
def _fetch_sec_8k_pdufa() -> List[Dict[str, Any]]:
    """Search recent 8-K filings mentioning a forward PDUFA date. EDGAR's
    full-text search returns recent filings with the term anywhere in the doc."""
    cache_key = "sec_8k_pdufa"
    cached = data_provider.cache_get(cache_key, max_age_sec=24 * 3600)
    if cached is not None:
        return cached
    date_to = datetime.utcnow().strftime("%Y-%m-%d")
    date_from = (datetime.utcnow() - timedelta(days=60)).strftime("%Y-%m-%d")
    url = (
        "https://efts.sec.gov/LATEST/search-index?"
        f"q=%22PDUFA%22&forms=8-K&dateRange=custom&startdt={date_from}&enddt={date_to}"
    )
    sess = data_provider.get_session()
    try:
        r = sess.get(url, headers=_sec_headers(), timeout=25)
        if r.status_code != 200:
            return []
        data = r.json()
        hits = data.get("hits", {}).get("hits", [])
        rows: List[Dict[str, Any]] = []
        for h in hits[:200]:
            s = h.get("_source", {})
            display_names = s.get("display_names") or []
            tickers: Set[str] = set()
            for dn in display_names:
                if not isinstance(dn, str):
                    continue
                m = re.search(r'\(([A-Z][A-Z0-9\-,\s\.]*)\)\s*\(CIK', dn)
                if m:
                    for part in m.group(1).split(","):
                        tk = part.strip().split("-")[0].split(".")[0].strip()
                        if tk and tk.isalpha() and 1 <= len(tk) <= 5:
                            tickers.add(tk)
            for tk in tickers:
                rows.append({
                    "ticker": tk,
                    "date_str": s.get("file_date", ""),
                    "type": "PDUFA",
                    "source": "sec_8k",
                })
        if rows:
            data_provider.cache_put(cache_key, rows)
        log.info("fda: sec 8-K PDUFA mentions -> %d rows", len(rows))
        return rows
    except SecContactRequiredError as e:
        log.warning("FDA calendar SEC source disabled: %s", e)
        return []
    except Exception as e:
        log.debug("sec 8-K fetch failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Date parsing + scoring
# ---------------------------------------------------------------------------
def _parse_date(s: str) -> Optional[datetime]:
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y",
                "%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _score(days: Optional[int], catalyst_type: str) -> float:
    if days is None or days < 0:
        return 0.0
    type_w = {"PDUFA": 1.0, "ADCOM": 0.9, "TOPLINE": 0.7,
              "CHMP": 0.6, "EVENT": 0.4}.get(catalyst_type, 0.3)
    if days <= 2:  return 0.5 * type_w
    if days <= 7:  return 0.9 * type_w
    if days <= 21: return 1.0 * type_w
    if days <= 45: return 0.6 * type_w
    return 0.2 * type_w


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run(universe: List[str]) -> pd.DataFrame:
    biotechs = set(t.upper() for t in universe) & BIOTECH_TICKERS
    if not biotechs:
        return pd.DataFrame()

    cached = data_provider.cache_get(CACHE_KEY_PRIMARY, max_age_sec=CACHE_TTL_SEC)
    if cached:
        rows = cached
    else:
        # Try all sources, merge results
        rows: List[Dict[str, Any]] = []
        rows.extend(_fetch_biopharmcatalyst())
        rows.extend(_fetch_rttnews())
        rows.extend(_fetch_drugscom())
        rows.extend(_fetch_openfda())
        rows.extend(_fetch_sec_8k_pdufa())
        if rows:
            data_provider.cache_put(CACHE_KEY_PRIMARY, rows)

    if not rows:
        log.info("fda_calendar: no rows from any source")
        return pd.DataFrame()

    now = datetime.now(timezone.utc)
    by_ticker: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        t = r.get("ticker", "").upper()
        if t not in biotechs:
            continue
        d = _parse_date(r.get("date_str"))
        if d is None:
            continue
        # For 8-K and openFDA, the date is the filing/submission date — we treat
        # it as the catalyst proxy if it's recent. For BPC/RTTNews/Drugs.com,
        # it's the forward PDUFA date. Allow up to 60d in the past.
        if d < now - timedelta(days=60):
            continue
        if d < now:
            # Already occurred — treat as a faded catalyst with small score
            days = -(now - d).days
        else:
            days = (d - now).days
        existing = by_ticker.get(t)
        # Prefer SOONEST upcoming; among in-the-past, prefer MOST RECENT
        if existing:
            ex_days = existing["days_to_catalyst"]
            if days >= 0 and ex_days >= 0 and days >= ex_days:
                continue
            if days < 0 and ex_days < 0 and days <= ex_days:
                continue
            if days < 0 and ex_days >= 0:
                continue
        by_ticker[t] = {
            "ticker": t,
            "next_catalyst_date": d.strftime("%Y-%m-%d"),
            "days_to_catalyst": days,
            "catalyst_type": r.get("type", "EVENT"),
            "fda_source": r.get("source", "?"),
            "fda_score": round(_score(max(days, 0), r.get("type", "EVENT")), 3),
        }

    df = pd.DataFrame(list(by_ticker.values()))
    if not df.empty:
        log.info("fda_calendar: %d biotechs with catalysts (sources=%s)",
                 len(df), df["fda_source"].value_counts().to_dict())
    return df
