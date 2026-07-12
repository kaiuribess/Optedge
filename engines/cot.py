"""CFTC Commitments of Traders (CoT) engine — v20.2.

v20.2 fix: the original v20.1 cot pulled from cftc.gov/dea/newcot/c_disagg.txt
which is the **Disaggregated commodities** file only — it does NOT contain
the financial futures (S&P 500, NASDAQ, Russell, T-Notes, EURO FX, BTC).
So 8 of the 14 markets we care about silently dropped to 0 rows.

Fix: switch primary source to the CFTC Public Reporting Socrata API at
publicreporting.cftc.gov. Two endpoints, both keyless, both return JSON:

  - 72hh-3qpy : Disaggregated (commodities)        managed-money columns
  - gpe5-46if : Traders in Financial Futures (TFF)  leveraged-money columns

We fall back to c_disagg.txt (commodities only) if Socrata is unreachable.

Released every Friday 3:30pm ET with data through prior Tuesday.

References:
- https://publicreporting.cftc.gov/Data-API/72hh-3qpy
- https://publicreporting.cftc.gov/Data-API/gpe5-46if
- https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm
"""
from __future__ import annotations
import io
import logging
from pathlib import Path
from typing import Dict, Optional, List

import pandas as pd

import sys
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider

log = logging.getLogger("optedge.cot")

# CFTC market codes for the contracts we care about.
# `kind` says which Socrata endpoint to query and which speculator column to
# read (managed_money for commodities, leveraged_money for financials).
COT_MARKETS = {
    # ---- Financial futures (TFF) ----
    "13874A": {"name": "S&P 500 E-MINI",        "fut": "/ES",  "kind": "tff",
               "etfs": ["SPY", "VOO", "IVV"]},
    "209742": {"name": "NASDAQ-100 E-MINI",     "fut": "/NQ",  "kind": "tff",
               "etfs": ["QQQ"]},
    "239742": {"name": "RUSSELL 2000 E-MINI",   "fut": "/RTY", "kind": "tff",
               "etfs": ["IWM"]},
    "043602": {"name": "10-YEAR US T-NOTES",    "fut": "/ZN",  "kind": "tff",
               "etfs": ["IEF", "TLT"]},
    "020601": {"name": "30-YEAR US TBONDS",     "fut": "/ZB",  "kind": "tff",
               "etfs": ["TLT"]},
    "098662": {"name": "EURO FX",               "fut": "/6E",  "kind": "tff",
               "etfs": ["FXE"]},
    "133741": {"name": "BITCOIN",               "fut": "/BTC", "kind": "tff",
               "etfs": ["BITO", "IBIT", "GBTC", "MSTR", "COIN", "MARA", "RIOT"]},
    # ---- Commodity futures (Disagg) ----
    "088691": {"name": "GOLD",                  "fut": "/GC",  "kind": "disagg",
               "etfs": ["GLD", "GDX", "NEM", "GOLD", "RGLD"]},
    "084691": {"name": "SILVER",                "fut": "/SI",  "kind": "disagg",
               "etfs": ["SLV", "PAAS", "AG", "HL"]},
    "067651": {"name": "WTI CRUDE OIL",         "fut": "/CL",  "kind": "disagg",
               "etfs": ["XLE", "USO", "XOM", "CVX", "COP", "OXY"]},
    "023651": {"name": "NATURAL GAS",           "fut": "/NG",  "kind": "disagg",
               "etfs": ["UNG", "BOIL", "KOLD"]},
    "001602": {"name": "WHEAT",                 "fut": "/ZW",  "kind": "disagg",
               "etfs": ["WEAT"]},
    "002602": {"name": "CORN",                  "fut": "/ZC",  "kind": "disagg",
               "etfs": ["CORN", "DE", "AGCO"]},
    "005602": {"name": "SOYBEANS",              "fut": "/ZS",  "kind": "disagg",
               "etfs": ["SOYB", "BG", "ADM"]},
}

SOCRATA_DISAGG = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
SOCRATA_TFF    = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
LEGACY_TXT     = "https://www.cftc.gov/dea/newcot/c_disagg.txt"


def _socrata_fetch_one(url: str, code: str, max_age_sec: int = 24 * 3600) -> Optional[Dict]:
    """Pull the single latest row for a given CFTC contract market code."""
    cache_key = f"cot_socrata:{url}:{code}"
    cached = data_provider.cache_get(cache_key, max_age_sec=max_age_sec)
    if cached:
        return cached
    sess = data_provider.get_session()
    try:
        params = {
            "cftc_contract_market_code": code,
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": 1,
        }
        r = sess.get(url, params=params, timeout=20)
        if r.status_code != 200:
            log.debug("Socrata %s %s -> %d", url, code, r.status_code)
            return None
        rows = r.json()
        if not rows:
            return None
        # ONLY cache populated rows so empty fetches don't poison the cache
        data_provider.cache_put(cache_key, rows[0])
        return rows[0]
    except Exception as e:
        log.debug("Socrata %s %s parse: %s", url, code, e)
        return None


def _score_from_row(row: Dict, kind: str) -> Optional[Dict]:
    """Compute managed/leveraged-money net position + week-over-week change."""
    try:
        if kind == "tff":
            long_now   = float(row.get("lev_money_positions_long")  or 0)
            short_now  = float(row.get("lev_money_positions_short") or 0)
            chg_long   = float(row.get("change_in_lev_money_long")  or 0)
            chg_short  = float(row.get("change_in_lev_money_short") or 0)
        else:  # disagg
            long_now   = float(row.get("m_money_positions_long_all")  or 0)
            short_now  = float(row.get("m_money_positions_short_all") or 0)
            chg_long   = float(row.get("change_in_m_money_long_all")  or 0)
            chg_short  = float(row.get("change_in_m_money_short_all") or 0)
    except (TypeError, ValueError):
        return None
    net_now = long_now - short_now
    net_chg = chg_long - chg_short
    date = (row.get("report_date_as_yyyy_mm_dd") or "")[:10]
    return {
        "net_latest": net_now,
        "net_change": net_chg,
        "net_change_pct": net_chg / max(abs(net_now), 1.0),
        "report_date": date,
    }


def _try_legacy_txt() -> Optional[pd.DataFrame]:
    """Last-resort fallback to the commodities-only TXT file."""
    key = f"cot_txt:{LEGACY_TXT}"
    cached = data_provider.cache_get(key, max_age_sec=24 * 3600)
    if cached is not None and isinstance(cached, dict) and cached.get("rows"):
        try:
            return pd.DataFrame(cached["rows"])
        except Exception:
            pass
    sess = data_provider.get_session()
    try:
        r = sess.get(LEGACY_TXT, timeout=30)
        if r.status_code != 200:
            return None
        df = pd.read_csv(io.StringIO(r.text), header=None, low_memory=False)
        if df.empty:
            return None
        data_provider.cache_put(key, {"rows": df.to_dict("records")})
        return df
    except Exception as e:
        log.debug("cot legacy txt: %s", e)
        return None


def _score_from_txt_row(row) -> Optional[Dict]:
    """TXT column positions (Disaggregated): col 13/14 = M_Money long/short,
       col 32/33 = change in M_Money long/short."""
    try:
        m_long  = float(row.iloc[13])
        m_short = float(row.iloc[14])
        if len(row) >= 34:
            chg_long  = float(row.iloc[32])
            chg_short = float(row.iloc[33])
        else:
            chg_long = chg_short = 0.0
    except Exception:
        return None
    net_now = m_long - m_short
    net_chg = chg_long - chg_short
    return {
        "net_latest": net_now,
        "net_change": net_chg,
        "net_change_pct": net_chg / max(abs(net_now), 1.0),
        "report_date": str(row.iloc[2]) if len(row) > 2 else "",
    }


def run(universe: Optional[List[str]] = None) -> pd.DataFrame:
    rows: List[Dict] = []
    sources_used = {"socrata": 0, "txt": 0}

    # Primary path: Socrata, one query per market code (small fan-out, all keyless)
    for code, meta in COT_MARKETS.items():
        endpoint = SOCRATA_TFF if meta["kind"] == "tff" else SOCRATA_DISAGG
        rec = _socrata_fetch_one(endpoint, code)
        if not rec:
            continue
        sig = _score_from_row(rec, meta["kind"])
        if not sig:
            continue
        sources_used["socrata"] += 1
        score = max(-1.0, min(1.0, sig["net_change_pct"]))
        for sym in meta["etfs"]:
            rows.append({
                "ticker": sym,
                "cot_score": score,
                "cot_market": meta["name"],
                "cot_net_latest": sig["net_latest"],
                "cot_net_change": sig["net_change"],
                "cot_report_date": sig["report_date"],
            })

    # Fallback: legacy TXT (commodities only — financials remain empty if we get here)
    if not rows:
        df_all = _try_legacy_txt()
        if df_all is not None and not df_all.empty and df_all.shape[1] >= 15:
            for code, meta in COT_MARKETS.items():
                if meta["kind"] != "disagg":
                    continue
                mask = df_all.iloc[:, 3].astype(str).str.strip() == code
                if not mask.any():
                    continue
                sig = _score_from_txt_row(df_all[mask].iloc[0])
                if not sig:
                    continue
                sources_used["txt"] += 1
                score = max(-1.0, min(1.0, sig["net_change_pct"]))
                for sym in meta["etfs"]:
                    rows.append({
                        "ticker": sym,
                        "cot_score": score,
                        "cot_market": meta["name"],
                        "cot_net_latest": sig["net_latest"],
                        "cot_net_change": sig["net_change"],
                        "cot_report_date": sig["report_date"],
                    })

    if not rows:
        log.info("CoT: 0 markets matched from any source (network blocked?)")
        return pd.DataFrame()

    out = pd.DataFrame(rows)
    out["abs"] = out["cot_score"].abs()
    out = out.sort_values("abs", ascending=False).drop_duplicates("ticker").drop(columns="abs")
    log.info("CoT: socrata=%d txt=%d markets -> %d ticker rows",
             sources_used["socrata"], sources_used["txt"], len(out))
    return out.reset_index(drop=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run().head(20))
