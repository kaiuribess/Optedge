# Purpose: Parse congressional transaction disclosures into signals.
"""Congressional trading engine — direct from House Clerk + Senate eFD.

100% free, no auth, unlimited. Bypasses the dead Stock Watcher S3 buckets
and Capitol Trades' paid API by parsing official disclosures ourselves.

Pipeline:
  House:
    1. Pull the House Clerk's annual FD.zip (filer index XML).
    2. Filter to Periodic Transaction Reports (FilingType = P) in last N days.
    3. For each PTR, download the PDF (7-day cache — immutable).
    4. Parse via pdfplumber + regex.

  Senate (NEW in v15):
    1. Auth dance: GET home → POST prohibition_agreement → get session cookie.
    2. Search via /search/report/data/ for Periodic Transactions (type 11).
    3. For each filing, fetch the report HTML (7-day cache).
    4. Parse the HTML table — Senate reports have explicit ticker columns,
       so this is way cleaner than House PDFs.

Senator trades are weighted 1.5× rep trades (more committee access).
"""
from __future__ import annotations
import io
import logging
import math
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
from xml.etree import ElementTree as ET

import requests
import pandas as pd

import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider
from config import USER_AGENT

log = logging.getLogger("optedge.congress")

LOOKBACK_DAYS = 90
HOUSE_FD_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
HOUSE_PTR_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
SENATE_HOME_URL = "https://efdsearch.senate.gov/search/home/"
SENATE_SEARCH_URL = "https://efdsearch.senate.gov/search/report/data/"
SENATE_REPORT_BASE = "https://efdsearch.senate.gov"

# Asset class codes we care about (stocks + options + ETFs)
RELEVANT_ASSETS = {"ST", "OP", "ETF", "RS", "OL", "OT"}
# Codes we IGNORE (treasuries, mutual funds, bonds — not useful as stock signals)
IGNORE_ASSETS = {"GS", "BD", "MF", "CS", "PE", "RP", "SR", "OI"}

# Transaction codes
BUY_CODES = {"P", "P (partial)"}
SELL_CODES = {"S", "S (partial)"}

SENATOR_WEIGHT = 1.5
REP_WEIGHT = 1.0


# -------- House Clerk index ------------------------------------------
def _fetch_house_fd_index(year: int) -> List[Dict[str, str]]:
    """Pull and cache the {year}FD.zip filer index. Returns list of dicts."""
    cache_key = f"congress:house_fd:{year}"
    cached = data_provider.cache_get(cache_key, max_age_sec=24 * 3600)
    if cached is not None:
        return cached

    url = HOUSE_FD_URL.format(year=year)
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        if r.status_code != 200:
            log.warning("House FD %d returned %d", year, r.status_code)
            return []
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        # Prefer XML — cleaner parse than the tab-separated TXT
        xml_name = next((n for n in zf.namelist() if n.endswith(".xml")), None)
        if not xml_name:
            log.warning("no XML in FD zip for year %d", year)
            return []
        xml_data = zf.read(xml_name)
        root = ET.fromstring(xml_data)
        out = []
        for m in root.findall("Member"):
            entry = {child.tag: (child.text or "").strip()
                     for child in m}
            out.append(entry)
        data_provider.cache_put(cache_key, out)
        log.info("House FD %d: %d total filings", year, len(out))
        return out
    except Exception as e:
        log.warning("House FD fetch failed: %s", e)
        return []


def _filter_ptrs(filings: List[Dict[str, str]], cutoff: datetime) -> List[Dict[str, str]]:
    """Keep only Periodic Transaction Reports (P) with FilingDate >= cutoff."""
    out = []
    for f in filings:
        if f.get("FilingType") != "P":
            continue
        date_str = f.get("FilingDate", "")
        try:
            fdate = datetime.strptime(date_str, "%m/%d/%Y").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if fdate < cutoff:
            continue
        f["_filing_date"] = fdate
        out.append(f)
    return out


# -------- PDF parsing -------------------------------------------------
# Regex for transaction lines. Matches asset descriptions ending with [TYPE]
# followed by transaction code (P/S, possibly with "(partial)") + dates + dollar range.
_TX_PATTERN = re.compile(
    r"(?P<asset>[\w\s.\-&,'\(\)/]{8,200}?)"        # asset description
    r"\[\s*(?P<atype>[A-Z]{2,4})\s*\]"             # asset class code [ST] etc
    r"\s*(?P<tx>[PSE](?:\s*\(partial\))?)"          # transaction code P/S/E
    r"\s+(?P<date1>\d{1,2}/\d{1,2}/\d{4})"          # transaction date
    r"\s+(?P<date2>\d{1,2}/\d{1,2}/\d{4})"          # notification date
    r"\s+\$\s?(?P<amt_lo>[\d,]+)\s*-?\s*"
    r"(?:\$\s?(?P<amt_hi>[\d,]+))?",
    re.MULTILINE,
)
# Ticker is usually in (PARENS) inside the asset description
_TICKER_PATTERN = re.compile(r"\(([A-Z]{1,5})\)")


def _parse_amount_midpoint(lo: str, hi: str) -> float:
    try:
        lo_f = float(lo.replace(",", "").strip())
        if hi:
            hi_f = float(hi.replace(",", "").strip())
            return (lo_f + hi_f) / 2
        return lo_f
    except (ValueError, AttributeError):
        return 0.0


def _parse_ptr_pdf(pdf_bytes: bytes, filer_name: str) -> List[Dict[str, Any]]:
    """Extract trades from a PTR PDF. Returns list of trade dicts."""
    try:
        import pdfplumber
    except ImportError:
        log.warning("pdfplumber not installed — pip install pdfplumber")
        return []

    rows = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            full_text = ""
            for page in pdf.pages:
                t = page.extract_text() or ""
                full_text += "\n" + t

            # Normalize whitespace so multi-line transactions become parseable
            normalized = re.sub(r"\s+", " ", full_text)

            for m in _TX_PATTERN.finditer(normalized):
                asset = m.group("asset").strip()
                atype = m.group("atype").strip()
                if atype not in RELEVANT_ASSETS:
                    continue
                # Need a ticker
                tk_match = _TICKER_PATTERN.search(asset)
                if not tk_match:
                    continue
                ticker = tk_match.group(1).upper()
                if ticker in {"USD", "ETF", "ST", "OP"} or len(ticker) > 5:
                    continue

                tx_code = m.group("tx").strip().split()[0]   # "P (partial)" → "P"
                is_buy = tx_code in {"P"} or "P (partial)" in m.group("tx")
                is_sell = tx_code in {"S"} or "S (partial)" in m.group("tx")
                if not (is_buy or is_sell):
                    continue

                date_str = m.group("date1")
                try:
                    tx_date = datetime.strptime(date_str, "%m/%d/%Y").replace(tzinfo=timezone.utc)
                except Exception:
                    continue

                amt = _parse_amount_midpoint(m.group("amt_lo"), m.group("amt_hi"))
                if amt <= 0:
                    continue

                rows.append({
                    "ticker": ticker,
                    "asset_type": atype,
                    "is_buy": is_buy,
                    "amount": amt,
                    "date": tx_date,
                    "filer": filer_name,
                })
    except Exception as e:
        log.debug("PDF parse error: %s", e)
        return []

    return rows


def _fetch_and_parse_ptr(filing: Dict[str, str]) -> List[Dict[str, Any]]:
    """Download one PTR PDF and parse trades. Cached 7 days."""
    doc_id = filing.get("DocID", "")
    year = filing.get("Year", "2026")
    if not doc_id or not doc_id.isdigit():
        return []

    cache_key = f"congress:ptr:{year}:{doc_id}"
    cached = data_provider.cache_get(cache_key, max_age_sec=7 * 86400)
    if cached is not None:
        # Cache stores serialized form — restore datetime
        for r in cached:
            if isinstance(r.get("date"), str):
                try:
                    r["date"] = datetime.fromisoformat(r["date"])
                except Exception:
                    pass
        return cached

    url = HOUSE_PTR_URL.format(year=year, doc_id=doc_id)
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        if r.status_code != 200:
            data_provider.cache_put(cache_key, [])
            return []
        filer_name = " ".join(s for s in [
            filing.get("Prefix", ""), filing.get("First", ""),
            filing.get("Last", ""), filing.get("Suffix", "")
        ] if s).strip()
        rows = _parse_ptr_pdf(r.content, filer_name)
        # Cache as JSON-friendly
        cached_form = [{**row, "date": row["date"].isoformat()
                                  if isinstance(row.get("date"), datetime)
                                  else row.get("date")} for row in rows]
        data_provider.cache_put(cache_key, cached_form)
        return rows
    except Exception as e:
        log.debug("PTR fetch %s failed: %s", doc_id, e)
        return []


# -------- Senate eFD scraping ----------------------------------------
def _build_senate_session() -> Optional[requests.Session]:
    """One-time auth dance: GET home → POST prohibition_agreement → ready."""
    # We don't cache the session itself because its cookies are not cleanly picklable.
    sess = requests.Session()
    sess.headers["User-Agent"] = USER_AGENT
    try:
        r1 = sess.get(SENATE_HOME_URL, timeout=15)
        if r1.status_code != 200:
            log.debug("Senate home GET failed: %d", r1.status_code)
            return None
        m = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', r1.text)
        if not m:
            log.debug("Senate: couldn't find csrfmiddlewaretoken in home page")
            return None
        form_csrf = m.group(1)
        r2 = sess.post(SENATE_HOME_URL,
                       data={"csrfmiddlewaretoken": form_csrf,
                             "prohibition_agreement": "1"},
                       headers={"Referer": SENATE_HOME_URL},
                       timeout=15, allow_redirects=True)
        if r2.status_code != 200:
            log.debug("Senate ToS POST failed: %d", r2.status_code)
            return None
        return sess
    except Exception as e:
        log.debug("Senate session build failed: %s", e)
        return None


def _fetch_senate_ptrs(sess: requests.Session, cutoff: datetime,
                       max_records: int = 500) -> List[Dict[str, Any]]:
    """Search Senate eFD for Periodic Transaction Reports since cutoff."""
    # Paginate — 100 at a time
    out = []
    start = 0
    page_size = 100
    cutoff_str = cutoff.strftime("%m/%d/%Y") + " 00:00:00"

    while start < max_records:
        csrf = sess.cookies.get("csrftoken", "")
        try:
            r = sess.post(SENATE_SEARCH_URL,
                          data={
                              "csrfmiddlewaretoken": csrf,
                              "report_types": "[11]",   # 11 = Periodic Transaction
                              "filer_types": "[]",
                              "submitted_start_date": cutoff_str,
                              "submitted_end_date": "",
                              "candidate_ddl": "",
                              "senator_first_name": "",
                              "senator_last_name": "",
                              "office_id": "",
                              "first_name": "",
                              "last_name": "",
                              "draw": str(start // page_size + 1),
                              "start": str(start),
                              "length": str(page_size),
                          },
                          headers={"X-CSRFToken": csrf,
                                   "Referer": "https://efdsearch.senate.gov/search/",
                                   "X-Requested-With": "XMLHttpRequest"},
                          timeout=20)
            if r.status_code != 200:
                log.debug("Senate search returned %d", r.status_code)
                break
            data = r.json()
            rows = data.get("data", [])
            if not rows:
                break
            for row in rows:
                # Row: [first, last, name_link, report_link_html, filed_date]
                if len(row) < 5:
                    continue
                first = row[0]; last = row[1]
                # Extract URL from HTML <a href="...">
                href_m = re.search(r'href="([^"]+)"', row[3])
                if not href_m:
                    continue
                report_url = SENATE_REPORT_BASE + href_m.group(1)
                date_str = row[4]
                try:
                    fdate = datetime.strptime(date_str, "%m/%d/%Y").replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                # Extract report title (shows period covered)
                title_m = re.search(r'>([^<]+)</a>', row[3])
                title = title_m.group(1).strip() if title_m else ""
                out.append({
                    "filer": f"{first} {last}".strip(),
                    "filer_first": first, "filer_last": last,
                    "report_url": report_url,
                    "filed_date": fdate,
                    "title": title,
                })
            total = data.get("recordsTotal", 0)
            if start + page_size >= total:
                break
            start += page_size
        except Exception as e:
            log.debug("Senate search error at start=%d: %s", start, e)
            break
    return out


def _parse_senate_html(html_text: str, filer_name: str) -> List[Dict[str, Any]]:
    """Parse a Senate PTR HTML report. Returns list of trade dicts."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    rows = []
    try:
        soup = BeautifulSoup(html_text, "html.parser")
        table = soup.find("table")
        if not table:
            return []
        all_rows = table.find_all("tr")
        if len(all_rows) < 2:
            return []
        # Header (skip), then data rows
        for tr in all_rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if len(cells) < 8:
                continue
            # Columns: #, Transaction Date, Owner, Ticker, Asset Name, Asset Type, Type, Amount, [Comment]
            tx_date_str = cells[1]
            ticker_raw = cells[3]
            asset_name = cells[4]
            asset_type = cells[5]
            tx_type = cells[6].lower()
            amount = cells[7]

            # Filter: only Stock / Option / ETF — skip Municipals/Bonds
            asset_type_low = asset_type.lower()
            if not any(k in asset_type_low for k in ("stock", "option", "etf", "exchange")):
                continue

            # Get ticker: prefer the explicit Ticker column, fall back to asset name parens
            ticker = ticker_raw.upper().strip() if ticker_raw and ticker_raw not in ("--", "—") else ""
            if not ticker or len(ticker) > 5 or not ticker.replace(".", "").isalpha():
                # Try parens in asset name
                m = re.search(r"\(([A-Z]{1,5})\)", asset_name)
                if m:
                    ticker = m.group(1)
                else:
                    continue

            try:
                tx_date = datetime.strptime(tx_date_str, "%m/%d/%Y").replace(tzinfo=timezone.utc)
            except Exception:
                continue

            is_buy = "purchase" in tx_type or "buy" in tx_type
            is_sell = "sale" in tx_type or "sell" in tx_type
            if not (is_buy or is_sell):
                continue

            # Parse amount: "$1,001 - $15,000" → midpoint
            amt_match = re.search(
                r"\$\s?([\d,]+)\s*-?\s*\$?\s?([\d,]+)?", amount
            )
            if not amt_match:
                continue
            try:
                lo = float(amt_match.group(1).replace(",", ""))
                hi = float(amt_match.group(2).replace(",", "")) if amt_match.group(2) else lo
                amt = (lo + hi) / 2
            except (ValueError, AttributeError):
                continue

            rows.append({
                "ticker": ticker,
                "asset_type": "ST" if "stock" in asset_type_low else "OP" if "option" in asset_type_low else "ETF",
                "is_buy": is_buy,
                "amount": amt,
                "date": tx_date,
                "filer": filer_name,
            })
    except Exception as e:
        log.debug("Senate HTML parse error: %s", e)
        return []
    return rows


def _fetch_and_parse_senate_filing(sess: requests.Session,
                                   filing: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch one Senate PTR HTML and parse it. Caches 7 days."""
    cache_key = f"congress:senate_ptr:{filing['report_url']}"
    cached = data_provider.cache_get(cache_key, max_age_sec=7 * 86400)
    if cached is not None:
        for r in cached:
            if isinstance(r.get("date"), str):
                try:
                    r["date"] = datetime.fromisoformat(r["date"])
                except Exception:
                    pass
        return cached
    try:
        r = sess.get(filing["report_url"], timeout=20)
        if r.status_code != 200:
            data_provider.cache_put(cache_key, [])
            return []
        rows = _parse_senate_html(r.text, filing["filer"])
        cached_form = [{**row, "date": row["date"].isoformat()
                                  if isinstance(row.get("date"), datetime)
                                  else row.get("date")} for row in rows]
        data_provider.cache_put(cache_key, cached_form)
        return rows
    except Exception as e:
        log.debug("Senate fetch %s failed: %s", filing["report_url"], e)
        return []


# -------- Public API --------------------------------------------------
def run(universe: List[str], lookback_days: int = LOOKBACK_DAYS,
        max_workers: int = 6, max_filings: int = 200,
        include_senate: bool = True) -> pd.DataFrame:
    """Build per-ticker Congressional trade signals from House Clerk PTRs.

    Direct, free, unlimited. Caches PDFs for 7 days (they're immutable once filed)
    and the FD index for 24 hours.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    universe_set = set(t.upper() for t in universe)

    # Pull FD indexes — current year + previous if cutoff straddles year boundary
    today = datetime.now(timezone.utc)
    years = [today.year]
    if cutoff.year < today.year:
        years.append(cutoff.year)

    all_filings = []
    for y in years:
        all_filings.extend(_fetch_house_fd_index(y))

    ptrs = _filter_ptrs(all_filings, cutoff)
    log.info("Found %d Periodic Transaction Reports in last %d days",
             len(ptrs), lookback_days)

    # Cap at max_filings (most recent first) to keep latency bounded
    ptrs = sorted(ptrs, key=lambda f: f.get("_filing_date") or datetime.min.replace(tzinfo=timezone.utc),
                  reverse=True)[:max_filings]

    # Parallel parse
    all_trades = []
    completed = 0
    log.info("parsing %d PTR PDFs (parallel, %d workers, cache 7d)",
             len(ptrs), max_workers)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_fetch_and_parse_ptr, f): f for f in ptrs}
        for fut in as_completed(futures):
            try:
                trades = fut.result()
                if trades:
                    all_trades.extend(trades)
            except Exception as e:
                log.debug("PTR worker error: %s", e)
            completed += 1
            if completed % 50 == 0 or completed == len(ptrs):
                log.info("[%d/%d]", completed, len(ptrs))

    # Tag House trades with chamber for downstream weighting
    for t in all_trades:
        t["chamber"] = "house"

    # ---- Senate ----
    if include_senate:
        log.info("Fetching Senate eFD…")
        sess = _build_senate_session()
        if sess is not None:
            sen_filings = _fetch_senate_ptrs(sess, cutoff)
            sen_filings = sorted(sen_filings, key=lambda f: f["filed_date"], reverse=True)[:max_filings]
            log.info("Found %d Senate Periodic Transaction Reports", len(sen_filings))
            sen_trades = []
            completed = 0
            with ThreadPoolExecutor(max_workers=4) as ex:
                # Senate uses session — limit concurrency to avoid overwhelming the server
                futures = {ex.submit(_fetch_and_parse_senate_filing, sess, f): f
                           for f in sen_filings}
                for fut in as_completed(futures):
                    try:
                        rows = fut.result()
                        if rows:
                            for r in rows:
                                r["chamber"] = "senate"
                            sen_trades.extend(rows)
                    except Exception as e:
                        log.debug("Senate worker error: %s", e)
                    completed += 1
                    if completed % 25 == 0 or completed == len(sen_filings):
                        log.info("[senate %d/%d]", completed, len(sen_filings))
            log.info("parsed %d Senate trades", len(sen_trades))
            all_trades.extend(sen_trades)

    log.info("parsed %d total Congressional trades (House + Senate)", len(all_trades))

    # Aggregate per ticker (universe-filtered, with Senate weight boost)
    by_ticker: Dict[str, Dict[str, Any]] = {}
    for t in all_trades:
        ticker = t["ticker"]
        if ticker not in universe_set:
            continue
        if t["date"] < cutoff:
            continue
        d = by_ticker.setdefault(ticker, {
            "buys_n": 0, "sells_n": 0,
            "buys_dollar": 0.0, "sells_dollar": 0.0,
            "buys_dollar_w": 0.0, "sells_dollar_w": 0.0,
            "filers": set(), "senators": set(),
            "top_buy_amount": 0.0, "top_buy_member": "",
        })
        weight = SENATOR_WEIGHT if t.get("chamber") == "senate" else REP_WEIGHT
        if t["is_buy"]:
            d["buys_n"] += 1
            d["buys_dollar"] += t["amount"]
            d["buys_dollar_w"] += t["amount"] * weight
            if t["amount"] > d["top_buy_amount"]:
                d["top_buy_amount"] = t["amount"]
                d["top_buy_member"] = t["filer"]
        else:
            d["sells_n"] += 1
            d["sells_dollar"] += t["amount"]
            d["sells_dollar_w"] += t["amount"] * weight
        d["filers"].add(t["filer"])
        if t.get("chamber") == "senate":
            d["senators"].add(t["filer"])

    rows = []
    for ticker, d in by_ticker.items():
        net_w = d["buys_dollar_w"] - d["sells_dollar_w"]
        score = math.copysign(math.log1p(abs(net_w) / 100_000), net_w) if net_w != 0 else 0
        n_filers = len(d["filers"])
        n_sens = len(d["senators"])
        n_reps = n_filers - n_sens
        if n_filers > 1:
            score *= (1 + 0.15 * (n_filers - 1))
        rows.append({
            "ticker": ticker,
            "congress_score": round(score, 3),
            "congress_buys_n": d["buys_n"],
            "congress_sells_n": d["sells_n"],
            "congress_buys_dollar": round(d["buys_dollar"], 0),
            "congress_sells_dollar": round(d["sells_dollar"], 0),
            "congress_n_reps": n_reps,
            "congress_n_sens": n_sens,
            "congress_top_buyer": d["top_buy_member"][:60],
        })

    log.info("congress: %d tickers with parsed activity", len(rows))
    return pd.DataFrame(rows)
