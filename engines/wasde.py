# Purpose: Score agricultural context around USDA WASDE releases.
"""USDA WASDE monthly reports engine — v20.2.

The World Agricultural Supply and Demand Estimates (WASDE) drops mid-month
(USDA publishes the schedule yearly; releases typically fall between the
8th and 12th, mid-morning ET) and routinely moves corn/soy/wheat 2-5% same-day.

v20.2 fix: the v20.1 release-date check assumed `d >= 13` meant this month
already happened, which incorrectly classified the day-of release (e.g.
2026-05-12) as last-month + 30 days old, dropping all rows. We now:
  - Treat any day on/after the 8th (lower bound of the release window) as
    "this month's WASDE has occurred."
  - Always emit a row per ag ticker, with a low baseline score when the
    calendar is quiet, so the dashboard surfaces ag exposure consistently.

Universe affected: DE, AGCO, BG, ADM, MOS, NTR, CF, CTVA, FMC, CORN, WEAT,
SOYB, DBA, MOO.

No network required for the proximity component; yfinance is used for the
5-day futures drift sign.
"""
from __future__ import annotations
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

import sys
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider

log = logging.getLogger("optedge.wasde")

AG_EQUITIES = ["DE", "AGCO", "BG", "ADM", "MOS", "NTR", "CF", "CTVA", "FMC",
               "CORN", "WEAT", "SOYB", "DBA", "MOO"]

# USDA's published WASDE schedule: 8th-12th of each month, mid-morning ET.
# We approximate the release as the 12th (latest typical date); using the 8th
# as the lower bound when deciding whether THIS month's release has happened.
RELEASE_TARGET_DAY = 12
RELEASE_WINDOW_START = 8


def _most_recent_release(today: datetime) -> datetime:
    """Latest WASDE release on/before `today`.

    If today is on/after the 8th of the month, this month's release counts.
    Otherwise, fall back to last month's 12th.
    """
    if today.day >= RELEASE_WINDOW_START:
        candidate = datetime(today.year, today.month, RELEASE_TARGET_DAY)
        # If the calendar 12th hasn't happened yet (e.g. it's the 9th), the
        # "release" date we use is whichever has already passed inside the
        # window; cap at today so days_since is never negative.
        return min(candidate, today)
    # Before window opens → use last month's 12th
    pm = today.month - 1 if today.month > 1 else 12
    py = today.year if today.month > 1 else today.year - 1
    return datetime(py, pm, RELEASE_TARGET_DAY)


def _next_release(today: datetime) -> datetime:
    """Next WASDE release strictly after `today`."""
    target = datetime(today.year, today.month, RELEASE_TARGET_DAY)
    if target <= today:
        nm = today.month + 1 if today.month < 12 else 1
        ny = today.year if today.month < 12 else today.year + 1
        target = datetime(ny, nm, RELEASE_TARGET_DAY)
    return target


def _proximity(days_since: int, days_to_next: int) -> float:
    """Catalyst proximity weight: peaks on release day, decays both directions."""
    if days_since <= 3:
        return 1.0
    if days_since <= 7:
        return 0.6
    if days_since <= 14:
        return 0.35
    if 0 <= days_to_next <= 5:
        return 0.3
    if 0 <= days_to_next <= 10:
        return 0.15
    return 0.1   # background — keep ag exposure on the board


def _futures_5d_drift() -> Dict[str, float]:
    """% change over last 5 trading days for corn/soy/wheat continuous futures."""
    out: Dict[str, float] = {}
    for sym in ["ZC=F", "ZS=F", "ZW=F"]:
        h = data_provider.get_history(sym, period="10d", cache_age=3600)
        if h.empty or len(h) < 5:
            out[sym] = 0.0
            continue
        try:
            closes = h["Close"].tolist()[-5:]
            out[sym] = (closes[-1] / closes[0] - 1)
        except Exception:
            out[sym] = 0.0
    return out


def run(universe: Optional[List[str]] = None) -> pd.DataFrame:
    """Per-ticker wasde_score = sign(corn+soy+wheat avg drift) × proximity_decay."""
    today = datetime.utcnow()
    last = _most_recent_release(today)
    nxt = _next_release(today)
    days_since = max(0, (today - last).days)
    days_to_next = max(0, (nxt - today).days)

    proximity = _proximity(days_since, days_to_next)

    drift = _futures_5d_drift()
    avg_drift = sum(drift.values()) / max(len(drift), 1)
    # Always emit; magnitude scales with proximity × drift.
    score = max(-1.0, min(1.0, avg_drift * 10 * proximity))

    log.info("WASDE: %d days since last (proximity=%.2f), %d to next, "
             "drift=%+.2f%% -> score=%+.2f, %d rows",
             days_since, proximity, days_to_next, avg_drift * 100,
             score, len(AG_EQUITIES))

    rows = [{
        "ticker": t,
        "wasde_score": score,
        "wasde_proximity": proximity,
        "wasde_days_since": days_since,
        "wasde_days_to_next": days_to_next,
        "wasde_drift_5d": avg_drift,
        "wasde_last_release": last.strftime("%Y-%m-%d"),
    } for t in AG_EQUITIES]
    return pd.DataFrame(rows)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run())
