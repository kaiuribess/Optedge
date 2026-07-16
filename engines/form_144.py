# Purpose: Find proposed insider sales in SEC Form 144 filings.
"""SEC Form 144 — Pre-sale notices engine — v20.2.

Form 144 is filed when an insider intends to sell within 90 days. LEADING
information vs Form 4 (filed AFTER sale).

A surge in Form 144 filings is bearish — insiders preparing to dump.

v20.2 fixes vs v20.1:
- Don't poison the disk cache with empty fetches (was caching `[]` for 12h
  every time the EDGAR call had a transient hiccup).
- Use data_provider session (curl_cffi when available) for better reliability.
- Add CIK->ticker resolution from SEC company_tickers.json as a fallback for
  filings whose display_names happen to omit the parenthesized ticker.
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider  # noqa: E402
from optedge.http_identity import SecContactRequiredError, sec_headers  # noqa: E402

log = logging.getLogger("optedge.form_144")


def _sec_headers() -> dict[str, str]:
    return sec_headers(accept="application/json, text/plain, */*")


# Display names look like: 'BLACKLINE, INC.  (BL)  (CIK 0001666134)'
_TICKER_PAREN_RE = re.compile(r"\(([A-Z][A-Z0-9\-,\s\.]*)\)\s*\(CIK")
_CIK_RE = re.compile(r"\(CIK\s+(\d+)\)")


def _extract_tickers(display_names) -> set[str]:
    tickers: set[str] = set()
    if not display_names:
        return tickers
    if isinstance(display_names, str):
        display_names = [display_names]
    for dn in display_names:
        if not isinstance(dn, str):
            continue
        m = _TICKER_PAREN_RE.search(dn)
        if not m:
            continue
        for part in m.group(1).split(","):
            tk = part.strip().split("-")[0].split(".")[0].strip()
            if tk and tk.isalpha() and 1 <= len(tk) <= 5:
                tickers.add(tk)
    return tickers


def _extract_ciks(display_names) -> set[str]:
    ciks: set[str] = set()
    if not display_names:
        return ciks
    if isinstance(display_names, str):
        display_names = [display_names]
    for dn in display_names:
        if not isinstance(dn, str):
            continue
        for m in _CIK_RE.finditer(dn):
            ciks.add(m.group(1).lstrip("0") or "0")
    return ciks


def _cik_to_ticker_map(max_age_sec: int = 7 * 86400) -> dict[str, str]:
    """SEC publishes a master CIK->ticker map at /files/company_tickers.json.
    Free, no key. Cached for a week (changes are rare)."""
    key = "sec_cik_ticker_map"
    cached = data_provider.cache_get(key, max_age_sec=max_age_sec)
    if cached:
        return cached
    sess = data_provider.get_session()
    try:
        r = sess.get(
            "https://www.sec.gov/files/company_tickers.json", headers=_sec_headers(), timeout=20
        )
        if r.status_code != 200:
            return {}
        data = r.json()
        out: dict[str, str] = {}
        for _, rec in data.items():
            cik = str(rec.get("cik_str", "")).lstrip("0") or "0"
            tk = (rec.get("ticker") or "").upper().strip()
            if cik and tk:
                out[cik] = tk
        if out:
            data_provider.cache_put(key, out)
        return out
    except SecContactRequiredError as e:
        log.warning("form 144 SEC CIK map disabled: %s", e)
        return {}
    except Exception as e:
        log.debug("CIK map fetch: %s", e)
        return {}


def _search_form_144(days_back: int = 30) -> list[dict]:
    date_to = datetime.utcnow().strftime("%Y-%m-%d")
    date_from = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    key = f"form144:{date_from}:{date_to}"
    cached = data_provider.cache_get(key, max_age_sec=12 * 3600)
    # Only use cache if it actually has entries — prior versions poisoned with []
    if isinstance(cached, list) and cached:
        return cached
    url = (
        f"https://efts.sec.gov/LATEST/search-index?q=&forms=144"
        f"&dateRange=custom&startdt={date_from}&enddt={date_to}"
    )
    sess = data_provider.get_session()
    try:
        r = sess.get(url, headers=_sec_headers(), timeout=25)
        if r.status_code != 200:
            log.info("form 144: EDGAR %d", r.status_code)
            return []
        data = r.json()
        hits = data.get("hits", {}).get("hits", [])
        out = []
        for h in hits[:1000]:
            s = h.get("_source", {})
            out.append(
                {
                    "display_names": s.get("display_names", []),
                    "tickers": s.get("tickers", []),
                    "ciks": s.get("ciks", []),
                    "file_date": s.get("file_date", ""),
                }
            )
        if out:  # only cache non-empty
            data_provider.cache_put(key, out)
        return out
    except SecContactRequiredError as e:
        log.warning("form 144 SEC search disabled: %s", e)
        return []
    except Exception as e:
        log.debug("form 144 search: %s", e)
        return []


def run(universe: list[str]) -> pd.DataFrame:
    if not universe:
        return pd.DataFrame()
    universe_set = {t.upper() for t in universe}
    filings = _search_form_144(30)
    if not filings:
        log.info("form 144: no filings retrieved (EDGAR unreachable?)")
        return pd.DataFrame()

    # Build CIK fallback map lazily — only fetch if we have filings that
    # didn't yield a ticker via display_names
    cik_map: dict[str, str] | None = None

    counts: dict[str, int] = {}
    dates: dict[str, list[str]] = {}
    for f in filings:
        tickers: set[str] = set()
        # Direct tickers field
        for t in f.get("tickers") or []:
            if isinstance(t, str) and t:
                tickers.add(t.upper())
        # Parens in display_names
        if not tickers:
            tickers |= _extract_tickers(f.get("display_names"))
        # Last resort: CIK -> ticker map
        if not tickers:
            if cik_map is None:
                cik_map = _cik_to_ticker_map()
            for cik in _extract_ciks(f.get("display_names")):
                tk = cik_map.get(cik)
                if tk:
                    tickers.add(tk.upper())
        for tk in tickers:
            if tk not in universe_set:
                continue
            counts[tk] = counts.get(tk, 0) + 1
            dates.setdefault(tk, []).append(f.get("file_date", ""))

    if not counts:
        log.info("form 144: %d filings -> 0 matched universe", len(filings))
        return pd.DataFrame()

    rows = []
    for tk, n in counts.items():
        if n >= 5:
            score = -1.0
        elif n >= 3:
            score = -0.5
        elif n >= 1:
            score = -0.2
        else:
            score = 0.0
        rows.append(
            {
                "ticker": tk,
                "form_144_score": score,
                "form_144_count_30d": n,
                "form_144_latest_date": max(dates.get(tk, [""])),
            }
        )
    out = pd.DataFrame(rows).sort_values("form_144_score").reset_index(drop=True)
    log.info("form 144: %d filings -> %d tickers in universe (30d)", len(filings), len(out))
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(
        run(
            [
                "AAPL",
                "MSFT",
                "NVDA",
                "TSLA",
                "META",
                "STZ",
                "BL",
                "CIFR",
                "LAD",
                "FSLY",
                "NNBR",
                "CRCL",
                "POWI",
            ]
        )
    )
