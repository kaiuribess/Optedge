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
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

import pandas as pd

import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider

log = logging.getLogger("optedge.short_interest")


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------
def _score(spof: Optional[float], shares_short: Optional[float],
           shares_short_prior: Optional[float],
           short_ratio: Optional[float],
           finra_ratio: Optional[float] = None) -> float:
    """Map short interest stats -> directional score."""
    if not spof and not finra_ratio:
        return 0.0
    # Base from SI % of float (5% = +0.5, 15% = +1.5, 30% = +3.0)
    score = min(3.0, (spof or 0) * 10)
    # Days-to-cover amplifier — more squeeze fuel when slow to cover
    if short_ratio and short_ratio > 3:
        score *= 1 + min(0.5, (short_ratio - 3) / 10)
    # Change vs prior month
    if shares_short and shares_short_prior and shares_short_prior > 0:
        change_pct = (shares_short - shares_short_prior) / shares_short_prior
        if change_pct > 0.10:   score *= 1.3
        elif change_pct > 0.0:  score *= 1.1
        elif change_pct < -0.15: score *= 0.4
        elif change_pct < 0.0:  score *= 0.7
    # FINRA daily short-volume amplifier: if short-vol-ratio > 0.55 today
    # bears are still actively pushing -> nudge score up
    if finra_ratio is not None:
        if finra_ratio > 0.60:   score = score * 1.15 + 0.3
        elif finra_ratio > 0.55: score = score * 1.05 + 0.1
        elif finra_ratio < 0.35: score *= 0.85
    return round(score, 3)


# ---------------------------------------------------------------------------
# FINRA RegSHO daily short volume (new fallback / amplifier)
# ---------------------------------------------------------------------------
def _fetch_finra_short_volume(max_age_sec: int = 24 * 3600) -> Dict[str, float]:
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


def _parse_finra(text: str) -> Dict[str, float]:
    """Parse pipe-delimited CNMSshvol file -> {symbol: short_vol / total_vol}."""
    ratios: Dict[str, float] = {}
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


# ---------------------------------------------------------------------------
# Per-ticker processing
# ---------------------------------------------------------------------------
def _process_ticker(t: str, finra_ratios: Dict[str, float]) -> Optional[Dict[str, Any]]:
    info = data_provider.get_short_info(t)
    spof = info.get("shortPercentOfFloat")
    short_ratio = info.get("shortRatio")
    shares_short = info.get("sharesShort")
    shares_short_prior = info.get("sharesShortPriorMonth")
    date_short = info.get("dateShortInterest")
    finra_ratio = finra_ratios.get(t.upper())
    # If we have NEITHER yfinance short info NOR a FINRA ratio, no row
    if spof is None and shares_short is None and finra_ratio is None:
        return None
    score = _score(spof, shares_short, shares_short_prior, short_ratio, finra_ratio)
    return {
        "ticker": t,
        "short_pct_of_float": spof,
        "short_ratio_days_to_cover": short_ratio,
        "shares_short": shares_short,
        "shares_short_prior_month": shares_short_prior,
        "short_int_change_pct": (
            (shares_short - shares_short_prior) / shares_short_prior
            if (shares_short and shares_short_prior and shares_short_prior > 0)
            else None
        ),
        "date_short_interest": date_short,
        "finra_short_vol_ratio": finra_ratio,
        "short_int_score": score,
    }


def run(universe: List[str], max_workers: int = 12) -> pd.DataFrame:
    """Pull short-interest stats per ticker. Free — yfinance + FINRA."""
    log.info("short_interest: %d tickers", len(universe))
    # Pull FINRA once for the whole universe (single HTTP call)
    finra_ratios = _fetch_finra_short_volume()
    rows = []
    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_process_ticker, t, finra_ratios): t
                for t in dict.fromkeys(universe)}
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
        log.info("short_interest: %d rows, %d squeeze setups (score > 1.5), "
                 "finra coverage=%d",
                 len(df), len(squeeze_setups),
                 df["finra_short_vol_ratio"].notna().sum() if "finra_short_vol_ratio" in df.columns else 0)
    return df
