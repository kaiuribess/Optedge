# Purpose: Measure FINRA daily short-sale volume ratios.
"""Dark pool / off-exchange volume — FINRA RegSHO daily file.

FINRA publishes daily short-sale volume (which is also a proxy for what
gets routed off-exchange / through dark pools, since most retail flow
goes through wholesalers that report there).

We pull yesterday's CNMS short volume file. Each row:
  Date | Ticker | ShortVolume | ShortExemptVolume | TotalVolume | Market

For each ticker:
  short_vol_ratio = ShortVolume / TotalVolume

High ratio (>50%) = lots of off-exchange shorting; could be institutional
positioning OR aggressive short flow. We treat it as a signed signal —
high short-vol-ratio is bearish, low is bullish.

Free, no auth, no API key.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider  # noqa: E402
from config import USER_AGENT  # noqa: E402

log = logging.getLogger("optedge.dark_pool")
FINRA_BASE = "https://cdn.finra.org/equity/regsho/daily"


def _fetch_daily_short_volume(date_str: str) -> pd.DataFrame:
    """Fetch FINRA CNMS short volume for one date (YYYYMMDD).

    Returns empty if file not yet published or unreachable. Cached 7 days.
    """
    cache_key = f"finra_shortvol:{date_str}"
    cached = data_provider.cache_get(cache_key, max_age_sec=7 * 86400)
    if cached is not None:
        if isinstance(cached, list):
            return pd.DataFrame(cached)
        return pd.DataFrame()

    url = f"{FINRA_BASE}/CNMSshvol{date_str}.txt"
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        if r.status_code != 200:
            log.debug("FINRA %s -> %d", date_str, r.status_code)
            return pd.DataFrame()
        # Pipe-delimited, header row first
        from io import StringIO

        df = pd.read_csv(StringIO(r.text), sep="|")
        if df.empty:
            return pd.DataFrame()
        # Cache rows-as-list-of-dicts (parquet-cache likes that)
        data_provider.cache_put(cache_key, df.to_dict("records"))
        return df
    except Exception as e:
        log.debug("FINRA fetch %s failed: %s", date_str, e)
        return pd.DataFrame()


def run(universe: list[str], lookback_days: int = 5) -> pd.DataFrame:
    """Pull last few days of FINRA short-vol, average per ticker.

    Returns DataFrame: ticker, short_vol_ratio, short_vol, total_vol, dark_pool_score.
    Score is signed (negative = lots of shorting = bearish).
    """
    universe_set = set(t.upper() for t in universe)
    today = datetime.now(UTC).date()
    frames = []
    # Try last `lookback_days` business days (FINRA skips weekends)
    for back in range(1, lookback_days + 3):  # extra for weekends
        d = today - timedelta(days=back)
        # Skip weekends
        if d.weekday() >= 5:
            continue
        df = _fetch_daily_short_volume(d.strftime("%Y%m%d"))
        if not df.empty:
            df["_date"] = d.isoformat()
            frames.append(df)
        if len(frames) >= lookback_days:
            break
    if not frames:
        log.warning("no FINRA daily files reachable")
        return pd.DataFrame()
    all_df = pd.concat(frames, ignore_index=True)
    # Normalize column names (FINRA uses uppercase headers)
    rename = {c: c.lower() for c in all_df.columns}
    all_df = all_df.rename(columns=rename)
    if "symbol" in all_df.columns:
        all_df["symbol"] = all_df["symbol"].astype(str).str.upper()
    elif "ticker" in all_df.columns:
        all_df["symbol"] = all_df["ticker"].astype(str).str.upper()
    else:
        log.warning("FINRA file missing symbol column")
        return pd.DataFrame()

    # Filter to universe + Aggregate
    sub = all_df[all_df["symbol"].isin(universe_set)].copy()
    if sub.empty:
        return pd.DataFrame()
    if "shortvolume" in sub.columns and "totalvolume" in sub.columns:
        agg = (
            sub.groupby("symbol")
            .agg(
                short_vol=("shortvolume", "sum"),
                total_vol=("totalvolume", "sum"),
            )
            .reset_index()
        )
    else:
        log.warning("FINRA columns unexpected: %s", list(sub.columns)[:10])
        return pd.DataFrame()

    agg["short_vol_ratio"] = (agg["short_vol"] / agg["total_vol"].clip(lower=1)).round(4)
    # Negative score = lots of shorting (bearish)
    # FINRA "short vol" includes routine market maker activity; ~50% is baseline noise.
    # We center around 0.50 and amplify deviations.
    agg["dark_pool_score"] = (-(agg["short_vol_ratio"] - 0.50) * 4).clip(-2, 2).round(3)
    agg = agg.rename(columns={"symbol": "ticker"})
    log.info("dark_pool: %d tickers with FINRA short-vol data", len(agg))
    return agg[["ticker", "short_vol_ratio", "short_vol", "total_vol", "dark_pool_score"]]
