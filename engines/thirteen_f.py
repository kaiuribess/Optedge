# Purpose: Compare SEC 13F holdings for tracked institutions.
"""13F institutional holdings engine — v20.1.

Pulls 13F-HR filings from SEC EDGAR for smart-money funds (Berkshire, Tepper,
Burry, Ackman, etc.) and detects quarter-over-quarter position deltas.

v20.1 fix: swapped fragile browse-edgar Atom feed for the modern
data.sec.gov/submissions/CIK{padded}.json endpoint. Stable, documented,
SEC actively maintains it.

Signal:
  +1.0 = new position added by 2+ smart-money funds this quarter
  +0.5 = single fund added or grew significantly
  -0.5 = single fund cut significantly
  -1.0 = 2+ funds fully exited

13F filings are reported within 45 days of quarter-end (always 1-quarter lagged).

Free, no auth (SEC EDGAR public).
"""

from __future__ import annotations

import logging
import re
import sys
import time
from pathlib import Path
from xml.etree import ElementTree as ET

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider  # noqa: E402
from optedge.http_identity import SecContactRequiredError, sec_headers  # noqa: E402

log = logging.getLogger("optedge.13f")

SMART_FUNDS = [
    {"cik": "1067983", "name": "Berkshire Hathaway", "weight": 1.5},
    {"cik": "1336528", "name": "Appaloosa (Tepper)", "weight": 1.4},
    {"cik": "1649339", "name": "Scion Asset Mgmt (Burry)", "weight": 1.3},
    {"cik": "1336184", "name": "Pershing Square (Ackman)", "weight": 1.4},
    {"cik": "1037389", "name": "Renaissance Technologies", "weight": 1.3},
    {"cik": "1350694", "name": "Bridgewater Associates", "weight": 1.2},
    {"cik": "1423053", "name": "Citadel Advisors", "weight": 1.2},
    {"cik": "1179392", "name": "Two Sigma Investments", "weight": 1.2},
    {"cik": "1358259", "name": "Coatue Management", "weight": 1.2},
    {"cik": "1112520", "name": "Soros Fund Management", "weight": 1.2},
    {"cik": "1167483", "name": "Tiger Global Management", "weight": 1.2},
    {"cik": "1709323", "name": "Light Street Capital", "weight": 1.0},
]


def _list_13f_filings(cik: str, count: int = 4) -> list[dict]:
    """List recent 13F-HR filings for a CIK via data.sec.gov submissions JSON."""
    cik_padded = cik.lstrip("0").zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    key = f"13f_subs:{cik}"
    cached = data_provider.cache_get(key, max_age_sec=12 * 3600)
    if cached is not None:
        return cached
    try:
        import requests

        # data.sec.gov is the right host for submissions — Host header must match
        h = sec_headers(accept="application/json", host="data.sec.gov")
        r = requests.get(url, headers=h, timeout=20)
        if r.status_code != 200:
            log.debug("13f subs %s -> %d", cik, r.status_code)
            return []
        data = r.json()
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accessions = recent.get("accessionNumber", [])
        dates = recent.get("filingDate", [])
        primary_docs = recent.get("primaryDocument", [])
        results = []
        for i, form in enumerate(forms):
            if form != "13F-HR":
                continue
            if i >= len(accessions):
                continue
            results.append(
                {
                    "accession": accessions[i],
                    "filing_date": dates[i] if i < len(dates) else "",
                    "primary_doc": primary_docs[i] if i < len(primary_docs) else "",
                }
            )
            if len(results) >= count:
                break
        data_provider.cache_put(key, results)
        return results
    except SecContactRequiredError as e:
        log.warning("13F SEC submissions disabled: %s", e)
        return []
    except Exception as e:
        log.debug("13f subs %s: %s", cik, e)
        return []


def _parse_13f_xml(accession: str, cik: str) -> list[dict]:
    """Fetch and parse the information table XML for a single 13F-HR filing."""
    if not accession:
        return []
    cik_int = int(cik.lstrip("0"))
    acc_clean = accession.replace("-", "")
    key = f"13f_data:{accession}:{cik}"
    cached = data_provider.cache_get(key, max_age_sec=30 * 24 * 3600)
    if cached is not None:
        return cached

    import requests

    base = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/"
    try:
        # The filing index lists every file in the submission
        headers = sec_headers(host="www.sec.gov")
        idx = requests.get(base + "index.json", headers=headers, timeout=20)
        if idx.status_code != 200:
            return []
        items = idx.json().get("directory", {}).get("item", [])
        info_file = None
        for it in items:
            n = it.get("name", "").lower()
            # Look for informationtable XML
            if "infotable" in n and n.endswith(".xml"):
                info_file = it["name"]
                break
            if n.endswith(".xml") and "primary" not in n and "form" not in n:
                info_file = it["name"]
        if not info_file:
            return []
        r = requests.get(base + info_file, headers=headers, timeout=30)
        if r.status_code != 200:
            return []
        text = r.content
        # Strip XML namespace declarations so XPath works without prefix mess
        text_stripped = re.sub(rb'xmlns(:[\w]+)?="[^"]+"', b"", text)
        root = ET.fromstring(text_stripped)
        rows = []
        for it in root.iter():
            if it.tag.endswith("infoTable") or it.tag == "infoTable":
                issuer = it.find(".//nameOfIssuer")
                cusip = it.find(".//cusip")
                value = it.find(".//value")
                shares = it.find(".//shrsOrPrnAmt/sshPrnamt") or it.find(".//sshPrnamt")
                if issuer is None:
                    continue
                try:
                    val = int(value.text) if value is not None and value.text else 0
                except Exception:
                    val = 0
                try:
                    shr = int(shares.text) if shares is not None and shares.text else 0
                except Exception:
                    shr = 0
                rows.append(
                    {
                        "issuer": (issuer.text or "").strip(),
                        "cusip": (cusip.text or "").strip() if cusip is not None else "",
                        "value": val,
                        "shares": shr,
                    }
                )
        data_provider.cache_put(key, rows)
        return rows
    except SecContactRequiredError as e:
        log.warning("13F SEC filing download disabled: %s", e)
        return []
    except Exception as e:
        log.debug("13f xml fail %s: %s", accession, e)
        return []


def _name_to_ticker(name: str, universe_set: set) -> str | None:
    if not name:
        return None
    n = name.upper().replace("&", "&AMP;")
    HAND = {
        "ALPHABET": "GOOGL",
        "META PLATFORMS": "META",
        "BERKSHIRE HATHAWAY": "BRK.B",
        "PROCTER & GAMBLE": "PG",
        "JPMORGAN CHASE": "JPM",
        "MICROSOFT CORP": "MSFT",
        "APPLE INC": "AAPL",
        "AMAZON COM": "AMZN",
        "NVIDIA": "NVDA",
        "TESLA": "TSLA",
        "BANK OF AMERICA": "BAC",
        "OCCIDENTAL PETROLEUM": "OXY",
        "CHEVRON": "CVX",
        "EXXON MOBIL": "XOM",
        "COCA-COLA": "KO",
        "COCA COLA": "KO",
        "WALMART": "WMT",
        "AMERICAN EXPRESS": "AXP",
        "UNITED PARCEL": "UPS",
        "GENERAL DYNAMICS": "GD",
        "GENERAL MOTORS": "GM",
        "WELLS FARGO": "WFC",
        "VISA INC": "V",
        "MASTERCARD": "MA",
        "PAYPAL": "PYPL",
        "GOLDMAN SACHS": "GS",
        "MORGAN STANLEY": "MS",
        "BROADCOM": "AVGO",
        "ORACLE": "ORCL",
        "ELI LILLY": "LLY",
        "JOHNSON & JOHNSON": "JNJ",
        "PFIZER": "PFE",
        "MERCK & CO": "MRK",
        "ABBVIE": "ABBV",
        "BRISTOL-MYERS": "BMY",
        "HOME DEPOT": "HD",
        "COSTCO": "COST",
        "TARGET CORP": "TGT",
        "MCDONALD": "MCD",
        "STARBUCKS": "SBUX",
        "NIKE INC": "NKE",
    }
    for k, v in HAND.items():
        if k in n:
            return v if v in universe_set else None
    first = n.split()[0].replace(",", "").replace(".", "")
    return first if first in universe_set else None


def run(universe: list[str]) -> pd.DataFrame:
    if not universe:
        return pd.DataFrame()
    try:
        sec_headers()
    except SecContactRequiredError as e:
        log.warning("13F SEC source disabled: %s", e)
        return pd.DataFrame()
    universe_set = {t.upper() for t in universe}
    ticker_deltas: dict[str, dict] = {}
    funds_processed = 0
    for f in SMART_FUNDS:
        filings = _list_13f_filings(f["cik"], count=2)
        if len(filings) < 1:
            continue
        latest = _parse_13f_xml(filings[0]["accession"], f["cik"])
        prior = _parse_13f_xml(filings[1]["accession"], f["cik"]) if len(filings) >= 2 else []
        if not latest:
            continue
        funds_processed += 1
        latest_map = {r["cusip"] or r["issuer"]: r for r in latest}
        prior_map = {r["cusip"] or r["issuer"]: r for r in prior}
        all_keys = set(latest_map) | set(prior_map)
        for k in all_keys:
            lat = latest_map.get(k)
            prv = prior_map.get(k)
            issuer = (lat or prv)["issuer"]
            tk = _name_to_ticker(issuer, universe_set)
            if not tk:
                continue
            lat_sh = lat["shares"] if lat else 0
            prv_sh = prv["shares"] if prv else 0
            if lat_sh == prv_sh:
                continue
            entry = ticker_deltas.setdefault(
                tk, {"n_new": 0, "n_growing": 0, "n_cutting": 0, "n_exiting": 0, "fund_names": []}
            )
            if prv_sh == 0 and lat_sh > 0:
                entry["n_new"] += 1
            elif prv_sh > 0 and lat_sh == 0:
                entry["n_exiting"] += 1
            elif lat_sh > prv_sh * 1.2:
                entry["n_growing"] += 1
            elif lat_sh < prv_sh * 0.8:
                entry["n_cutting"] += 1
            entry["fund_names"].append(f["name"])
        time.sleep(0.15)
    if not ticker_deltas:
        log.info("13F: no smart-money signals (network blocked or no overlap with universe)")
        return pd.DataFrame()
    rows = []
    for tk, d in ticker_deltas.items():
        bull = d["n_new"] * 1.0 + d["n_growing"] * 0.5
        bear = d["n_exiting"] * -1.0 + d["n_cutting"] * -0.5
        score = max(-1.0, min(1.0, (bull + bear) / 2))
        rows.append(
            {
                "ticker": tk,
                "thirteen_f_score": score,
                "tf_n_new": d["n_new"],
                "tf_n_growing": d["n_growing"],
                "tf_n_cutting": d["n_cutting"],
                "tf_n_exiting": d["n_exiting"],
                "tf_funds": ", ".join(d["fund_names"][:4]),
            }
        )
    out = pd.DataFrame(rows).sort_values("thirteen_f_score", ascending=False).reset_index(drop=True)
    log.info(
        "13F: %d/%d funds parsed, %d tickers with deltas",
        funds_processed,
        len(SMART_FUNDS),
        len(out),
    )
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(
        run(
            [
                "AAPL",
                "MSFT",
                "NVDA",
                "BAC",
                "OXY",
                "KO",
                "AMZN",
                "TSLA",
                "META",
                "NKE",
                "PG",
                "V",
                "MA",
            ]
        )
    )
