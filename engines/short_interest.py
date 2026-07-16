# Purpose: Score short-interest and squeeze context.
"""Short Interest engine — squeeze-setup detection — v20.2.

v20.2 fix: the v20.1 engine reads `data_provider.get_fundamentals(t)` for short
interest fields, but that cache schema intentionally excludes them, so every
field was None and the engine emitted 0 rows. v20.2 calls
`data_provider.get_short_info(t)` (new) which pulls and caches the short
fields separately. We also layer in FINRA's daily public short-volume file
as an amplifier so even tickers with no yfinance short-info still get a signal.

Primary fields (yfinance, free, no key):
  - shortPercentOfFloat  (the canonical "short interest" pct)
  - shortRatio           (days-to-cover at avg daily volume)
  - sharesShort          (absolute count)
  - sharesShortPriorMonth (prior reading)
  - dateShortInterest    (when reading was taken)

Fallback / amplifier (FINRA RegSHO, free, no key, daily):
  - https://cdn.finra.org/equity/regsho/daily/CNMSshvol{YYYYMMDD}.txt
    Columns: Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market
    Short volume / total volume = "% of today's trading that was short-selling"

Signal logic:
  - Short % of float > 15% = elevated; > 25% = extreme (squeeze fuel)
  - Days-to-cover > 5 = harder to cover quickly
  - Rising short interest = bears piling on; if stock holding up -> squeeze risk
  - Falling short interest = bears covering = bullish unwind
  - High FINRA short ratio (>0.55) sustained = bears active right now -> amplifies
"""

from __future__ import annotations

import io
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider  # noqa: E402

log = logging.getLogger("optedge.short_interest")

FINRA_SHORT_INTEREST_PAGE = (
    "https://www.finra.org/finra-data/browse-catalog/equity-short-interest/files"
)
FINRA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Optedge/1.0; research)",
    "Accept": "text/html,text/plain,*/*",
}


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------
def _score(
    spof: float | None,
    shares_short: float | None,
    shares_short_prior: float | None,
    short_ratio: float | None,
    finra_ratio: float | None = None,
) -> float:
    """Map short interest stats -> directional score."""
    if not spof and not finra_ratio and not short_ratio and not shares_short:
        return 0.0
    # Base from SI % of float (5% = +0.5, 15% = +1.5, 30% = +3.0)
    score = min(3.0, (spof or 0) * 10)
    # FINRA's official short-interest file does not include float, so when it
    # is our only source we keep a conservative days-to-cover signal alive.
    if score <= 0 and short_ratio:
        score = min(2.5, max(0.0, short_ratio) / 2.5)
    if score <= 0 and shares_short:
        score = 0.25
    # Days-to-cover amplifier — more squeeze fuel when slow to cover
    if short_ratio and short_ratio > 3:
        score *= 1 + min(0.5, (short_ratio - 3) / 10)
    # Change vs prior month
    if shares_short and shares_short_prior and shares_short_prior > 0:
        change_pct = (shares_short - shares_short_prior) / shares_short_prior
        if change_pct > 0.10:
            score *= 1.3
        elif change_pct > 0.0:
            score *= 1.1
        elif change_pct < -0.15:
            score *= 0.4
        elif change_pct < 0.0:
            score *= 0.7
    # FINRA daily short-volume amplifier: if short-vol-ratio > 0.55 today
    # bears are still actively pushing -> nudge score up
    if finra_ratio is not None:
        if finra_ratio > 0.60:
            score = score * 1.15 + 0.3
        elif finra_ratio > 0.55:
            score = score * 1.05 + 0.1
        elif finra_ratio < 0.35:
            score *= 0.85
    return round(score, 3)


# ---------------------------------------------------------------------------
# FINRA RegSHO daily short volume (new fallback / amplifier)
# ---------------------------------------------------------------------------
def _fetch_finra_short_volume(max_age_sec: int = 24 * 3600) -> dict[str, float]:
    """Fetch the most recent FINRA daily short-volume file and return
    {symbol: short_volume / total_volume}. We walk back day-by-day up to 5
    business days since FINRA publishes ~1-2 days behind."""
    cache_key = "finra_regsho_daily"
    cached = data_provider.cache_get(cache_key, max_age_sec=max_age_sec)
    if cached is not None:
        return cached
    sess = data_provider.get_session()
    base = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{ymd}.txt"
    today = datetime.utcnow().date()
    for back in range(0, 6):
        d = today - timedelta(days=back)
        # Skip weekends (FINRA doesn't publish)
        if d.weekday() >= 5:
            continue
        url = base.format(ymd=d.strftime("%Y%m%d"))
        try:
            r = sess.get(url, timeout=20)
            if r.status_code != 200 or "Symbol" not in r.text[:200]:
                continue
            ratios = _parse_finra(r.text)
            if ratios:
                data_provider.cache_put(cache_key, ratios)
                log.info("finra: %d symbols from %s", len(ratios), d.strftime("%Y-%m-%d"))
                return ratios
        except Exception as e:
            log.debug("finra %s: %s", url, e)
            continue
    return {}


def _parse_finra(text: str) -> dict[str, float]:
    """Parse pipe-delimited CNMSshvol file -> {symbol: short_vol / total_vol}."""
    ratios: dict[str, float] = {}
    try:
        df = pd.read_csv(io.StringIO(text), sep="|")
    except Exception:
        return {}
    if "Symbol" not in df.columns or "TotalVolume" not in df.columns:
        return {}
    for _, row in df.iterrows():
        try:
            sym = str(row["Symbol"]).strip().upper()
            sv = float(row.get("ShortVolume") or 0)
            tv = float(row.get("TotalVolume") or 0)
            if not sym or tv <= 0:
                continue
            ratios[sym] = sv / tv
        except (ValueError, TypeError):
            continue
    return ratios


def _parse_finra_short_interest(text: str) -> dict[str, dict[str, Any]]:
    """Parse FINRA's official twice-monthly short-interest file by symbol."""
    try:
        df = pd.read_csv(io.StringIO(text), sep="|")
    except Exception:
        return {}
    required = {"symbolCode", "currentShortPositionQuantity", "settlementDate"}
    if not required.issubset(df.columns):
        return {}
    number_cols = {
        "currentShortPositionQuantity": "finra_short_interest_shares",
        "previousShortPositionQuantity": "finra_short_interest_prior_shares",
        "averageDailyVolumeQuantity": "finra_short_interest_avg_daily_volume",
        "daysToCoverQuantity": "finra_short_interest_days_to_cover",
        "changePercent": "finra_short_interest_change_pct",
        "changePreviousNumber": "finra_short_interest_change_shares",
    }
    out: dict[str, dict[str, Any]] = {}
    for _, row in df.iterrows():
        symbol = str(row.get("symbolCode") or "").strip().upper()
        if not symbol:
            continue
        record: dict[str, Any] = {
            "finra_short_interest_source": "finra_equity_short_interest_file",
            "finra_short_interest_settlement_date": str(row.get("settlementDate") or "").strip(),
            "finra_short_interest_issue_name": str(row.get("issueName") or "").strip(),
            "finra_short_interest_exchange_code": str(
                row.get("issuerServicesGroupExchangeCode") or ""
            ).strip(),
            "finra_short_interest_market_class": str(row.get("marketClassCode") or "").strip(),
        }
        for src, dst in number_cols.items():
            val = pd.to_numeric(row.get(src), errors="coerce")
            record[dst] = None if pd.isna(val) else float(val)
        out[symbol] = record
    return out


def _fetch_finra_short_interest(max_age_sec: int = 7 * 24 * 3600) -> dict[str, dict[str, Any]]:
    """Fetch the latest official FINRA short-interest CSV index/file."""
    cache_key = "finra_equity_short_interest_latest:v1"
    cached = data_provider.cache_get(cache_key, max_age_sec=max_age_sec)
    if cached is not None:
        return cached
    sess = data_provider.get_session()
    try:
        page = sess.get(FINRA_SHORT_INTEREST_PAGE, timeout=25)
        page_text = page.text or ""
        if page.status_code != 200 or "Just a moment" in page_text[:1000]:
            import requests

            page_text = requests.get(
                FINRA_SHORT_INTEREST_PAGE,
                headers=FINRA_HEADERS,
                timeout=25,
            ).text
        urls = re.findall(
            r"https://cdn\.finra\.org/equity/otcmarket/biweekly/shrt\d{8}\.csv",
            page_text or "",
        )
        urls = list(dict.fromkeys(urls))
    except Exception as e:
        log.debug("finra short-interest page: %s", e)
        return {}
    for url in urls[:3]:
        try:
            r = sess.get(url, timeout=45)
            text = r.text or ""
            if r.status_code != 200 or "symbolCode" not in text[:300]:
                import requests

                text = requests.get(url, headers=FINRA_HEADERS, timeout=45).text
            if "symbolCode" not in text[:300]:
                continue
            parsed = _parse_finra_short_interest(text)
            if parsed:
                data_provider.cache_put(cache_key, parsed)
                log.info(
                    "finra short-interest: %d symbols from %s",
                    len(parsed),
                    url.rsplit("/", 1)[-1],
                )
                return parsed
        except Exception as e:
            log.debug("finra short-interest file %s: %s", url, e)
            continue
    return {}


# ---------------------------------------------------------------------------
# Per-ticker processing
# ---------------------------------------------------------------------------
def _process_ticker(
    t: str,
    finra_ratios: dict[str, float],
    finra_short_interest: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    info = data_provider.get_short_info(t)
    official = finra_short_interest.get(t.upper()) or {}
    spof = info.get("shortPercentOfFloat")
    short_ratio = info.get("shortRatio") or official.get("finra_short_interest_days_to_cover")
    shares_short = info.get("sharesShort") or official.get("finra_short_interest_shares")
    shares_short_prior = info.get("sharesShortPriorMonth") or official.get(
        "finra_short_interest_prior_shares"
    )
    date_short = info.get("dateShortInterest") or official.get(
        "finra_short_interest_settlement_date"
    )
    finra_ratio = finra_ratios.get(t.upper())
    # If we have NEITHER yfinance short info NOR a FINRA ratio, no row
    if spof is None and shares_short is None and finra_ratio is None:
        return None
    score = _score(spof, shares_short, shares_short_prior, short_ratio, finra_ratio)
    if shares_short and shares_short_prior and shares_short_prior > 0:
        change_pct = (shares_short - shares_short_prior) / shares_short_prior
    elif official.get("finra_short_interest_change_pct") is not None:
        change_pct = official.get("finra_short_interest_change_pct") / 100.0
    else:
        change_pct = None
    return {
        "ticker": t,
        "short_pct_of_float": spof,
        "short_ratio_days_to_cover": short_ratio,
        "shares_short": shares_short,
        "shares_short_prior_month": shares_short_prior,
        "short_int_change_pct": change_pct,
        "date_short_interest": date_short,
        "finra_short_vol_ratio": finra_ratio,
        "short_int_score": score,
        **official,
    }


def run(universe: list[str], max_workers: int = 12) -> pd.DataFrame:
    """Pull short-interest stats per ticker. Free — yfinance + FINRA."""
    log.info("short_interest: %d tickers", len(universe))
    # Pull FINRA once for the whole universe (single HTTP call)
    finra_ratios = _fetch_finra_short_volume()
    finra_short_interest = _fetch_finra_short_interest()
    rows = []
    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {
            ex.submit(_process_ticker, t, finra_ratios, finra_short_interest): t
            for t in dict.fromkeys(universe)
        }
        for fut in as_completed(futs):
            try:
                r = fut.result()
                if r:
                    rows.append(r)
            except Exception as e:
                log.debug("short_interest fail: %s", e)
            completed += 1
            if completed % 100 == 0 or completed == len(universe):
                log.info("[short_int %d/%d]", completed, len(universe))
    df = pd.DataFrame(rows)
    if not df.empty:
        squeeze_setups = df[df["short_int_score"] > 1.5]
        log.info(
            "short_interest: %d rows, %d squeeze setups (score > 1.5), finra coverage=%d",
            len(df),
            len(squeeze_setups),
            df["finra_short_vol_ratio"].notna().sum()
            if "finra_short_vol_ratio" in df.columns
            else 0,
        )
        if "finra_short_interest_source" in df.columns:
            log.info(
                "short_interest: official FINRA SI coverage=%d",
                df["finra_short_interest_source"].notna().sum(),
            )
    return df
