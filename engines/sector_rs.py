"""Sector Relative Strength — is this ticker outperforming its sector?

For each ticker we map to its primary sector ETF (XLK/XLF/XLV/...) and
compute 20-day return diff vs that ETF. Positive = outperforming sector.

This catches stocks moving against their sector's tide — useful for both:
  - bullish setups (stock leading the sector higher)
  - bearish setups (stock lagging a strong sector → maybe weak fundamentals)

Adds a new fusion factor `sector_rs_score` (already side-aligned via z-score sign).
"""
from __future__ import annotations
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Optional

import pandas as pd

import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider

log = logging.getLogger("optedge.sector_rs")

# Per-ticker sector ETF map (extend as needed). Default fallback: SPY.
SECTOR_MAP = {
    # Tech (XLK)
    "AAPL": "XLK", "MSFT": "XLK", "NVDA": "XLK", "AMD": "XLK", "AVGO": "XLK",
    "ORCL": "XLK", "CRM": "XLK", "ADBE": "XLK", "CSCO": "XLK", "IBM": "XLK",
    "QCOM": "XLK", "TXN": "XLK", "MU": "XLK", "PANW": "XLK", "ANET": "XLK",
    "INTU": "XLK", "NOW": "XLK", "INTC": "XLK", "SNOW": "XLK", "CRWD": "XLK",
    "NET": "XLK", "MDB": "XLK", "DDOG": "XLK", "WDAY": "XLK", "SHOP": "XLK",
    "TEAM": "XLK", "ZS": "XLK", "OKTA": "XLK", "PLTR": "XLK", "SMCI": "XLK",
    "DOCU": "XLK", "TWLO": "XLK", "ZM": "XLK", "FSLY": "XLK", "DOCN": "XLK",
    "ADI": "XLK", "LRCX": "XLK", "KLAC": "XLK", "MRVL": "XLK", "ON": "XLK",
    "MCHP": "XLK", "AMAT": "XLK", "ASML": "XLK", "TSM": "XLK",
    # Communication (XLC) — mapped to XLK as fallback
    "META": "XLK", "GOOGL": "XLK", "GOOG": "XLK", "NFLX": "XLK", "DIS": "XLY",
    "WBD": "XLY", "PARA": "XLY", "CMCSA": "XLY", "TMUS": "XLY", "T": "XLY",
    "VZ": "XLY", "SPOT": "XLY", "ROKU": "XLY", "FOX": "XLY", "FOXA": "XLY",
    # Consumer Disc (XLY)
    "AMZN": "XLY", "TSLA": "XLY", "HD": "XLY", "LOW": "XLY", "NKE": "XLY",
    "SBUX": "XLY", "MCD": "XLY", "ABNB": "XLY", "BKNG": "XLY", "DAL": "XLY",
    "AAL": "XLY", "UAL": "XLY", "LUV": "XLY", "MAR": "XLY", "HLT": "XLY",
    "MGM": "XLY", "WYNN": "XLY", "LVS": "XLY", "CCL": "XLY", "RCL": "XLY",
    "NCLH": "XLY", "DKNG": "XLY", "PENN": "XLY", "F": "XLY", "GM": "XLY",
    "TM": "XLY", "STLA": "XLY", "TJX": "XLY", "ROST": "XLY", "ULTA": "XLY",
    "LULU": "XLY", "DECK": "XLY", "YUM": "XLY", "CMG": "XLY", "QSR": "XLY",
    "DPZ": "XLY", "PTON": "XLY", "BYND": "XLY", "ETSY": "XLY", "CHWY": "XLY",
    "DASH": "XLY", "UBER": "XLY", "LYFT": "XLY", "BMBL": "XLY", "MTCH": "XLY",
    # Consumer Staples (XLP)
    "WMT": "XLP", "COST": "XLP", "TGT": "XLP", "KO": "XLP", "PEP": "XLP",
    "PG": "XLP", "CL": "XLP", "KMB": "XLP", "MO": "XLP", "PM": "XLP",
    "EL": "XLP", "CHD": "XLP", "CLX": "XLP", "GIS": "XLP", "DG": "XLP",
    "DLTR": "XLP", "BBY": "XLY",
    # Financials (XLF)
    "JPM": "XLF", "BAC": "XLF", "WFC": "XLF", "C": "XLF", "GS": "XLF",
    "MS": "XLF", "USB": "XLF", "PNC": "XLF", "SCHW": "XLF", "COF": "XLF",
    "AXP": "XLF", "V": "XLF", "MA": "XLF", "PYPL": "XLF", "BLK": "XLF",
    "BX": "XLF", "KKR": "XLF", "TROW": "XLF", "STT": "XLF", "BK": "XLF",
    "MET": "XLF", "PRU": "XLF", "AFL": "XLF", "ALL": "XLF", "TRV": "XLF",
    "CB": "XLF", "AIG": "XLF", "HOOD": "XLF", "SOFI": "XLF", "COIN": "XLF",
    "AFRM": "XLF", "UPST": "XLF", "ROOT": "XLF", "LMND": "XLF", "OSCR": "XLF",
    # Energy (XLE)
    "XOM": "XLE", "CVX": "XLE", "COP": "XLE", "OXY": "XLE", "EOG": "XLE",
    "SLB": "XLE", "MPC": "XLE", "VLO": "XLE", "PSX": "XLE", "HAL": "XLE",
    "FANG": "XLE", "DVN": "XLE", "BKR": "XLE", "WMB": "XLE", "KMI": "XLE",
    "ENB": "XLE", "ET": "XLE",
    # Industrials (XLI)
    "BA": "XLI", "CAT": "XLI", "DE": "XLI", "HON": "XLI", "GE": "XLI",
    "MMM": "XLI", "RTX": "XLI", "LMT": "XLI", "NOC": "XLI", "GD": "XLI",
    "UPS": "XLI", "FDX": "XLI", "EMR": "XLI", "ETN": "XLI", "ITW": "XLI",
    "PH": "XLI", "ROK": "XLI", "JCI": "XLI", "CMI": "XLI", "PCAR": "XLI",
    "CSX": "XLI", "NSC": "XLI", "UNP": "XLI", "WM": "XLI", "RSG": "XLI",
    # Healthcare (XLV)
    "UNH": "XLV", "LLY": "XLV", "JNJ": "XLV", "PFE": "XLV", "MRK": "XLV",
    "ABBV": "XLV", "BMY": "XLV", "GILD": "XLV", "AMGN": "XLV", "MDT": "XLV",
    "TMO": "XLV", "DHR": "XLV", "CVS": "XLV", "ABT": "XLV", "ISRG": "XLV",
    "REGN": "XLV", "VRTX": "XLV", "BIIB": "XLV", "ZTS": "XLV", "BSX": "XLV",
    "EW": "XLV", "CI": "XLV", "ELV": "XLV", "HCA": "XLV", "HUM": "XLV",
    "MRNA": "XLV", "BNTX": "XLV", "NVAX": "XLV", "OCGN": "XLV", "VKTX": "XLV",
    "SAVA": "XLV", "SRPT": "XLV", "BLUE": "XLV", "FATE": "XLV", "CRSP": "XLV",
    "EDIT": "XLV", "NTLA": "XLV", "BEAM": "XLV",
    # Materials (XLB)
    "LIN": "XLB", "APD": "XLB", "SHW": "XLB", "ECL": "XLB", "FCX": "XLB",
    "NEM": "XLB", "AA": "XLB", "X": "XLB", "CLF": "XLB", "STLD": "XLB", "NUE": "XLB",
    # Real estate (XLRE)
    "AMT": "XLRE", "EQIX": "XLRE", "PLD": "XLRE", "SPG": "XLRE", "O": "XLRE",
    "CCI": "XLRE", "PSA": "XLRE", "SBAC": "XLRE", "DLR": "XLRE", "VICI": "XLRE",
}


def _ret_for(ticker: str, period: str = "1mo") -> Optional[float]:
    """Get 20-day total return for a ticker. Uses get_history's cache."""
    try:
        h = data_provider.get_history(ticker, period=period)
        if h is None or h.empty or len(h) < 5:
            return None
        # Use first→last close
        first = float(h["Close"].iloc[0])
        last = float(h["Close"].iloc[-1])
        if first <= 0:
            return None
        return (last / first) - 1
    except Exception:
        return None


def run(universe: List[str], max_workers: int = 8) -> pd.DataFrame:
    """Compute per-ticker sector relative strength.

    Returns a DataFrame with columns:
      ticker, sector_etf, ticker_ret_20d, sector_ret_20d, sector_rs_score
    """
    universe_list = list(dict.fromkeys(universe))
    log.info("sector_rs: %d tickers", len(universe_list))

    # 1. Compute sector returns once
    sectors_needed = set(SECTOR_MAP.values()) | {"SPY"}  # SPY as fallback
    sector_returns: Dict[str, Optional[float]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_ret_for, s): s for s in sectors_needed}
        for fut in as_completed(futs):
            s = futs[fut]
            try:
                sector_returns[s] = fut.result()
            except Exception:
                sector_returns[s] = None

    # 2. Compute per-ticker return
    ticker_returns: Dict[str, Optional[float]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_ret_for, t): t for t in universe_list}
        completed = 0
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                ticker_returns[t] = fut.result()
            except Exception:
                ticker_returns[t] = None
            completed += 1
            if completed % 100 == 0 or completed == len(universe_list):
                log.info("[sector_rs %d/%d]", completed, len(universe_list))

    # 3. Compute RS score
    rows = []
    for t in universe_list:
        tret = ticker_returns.get(t)
        sec = SECTOR_MAP.get(t, "SPY")
        sret = sector_returns.get(sec)
        if tret is None or sret is None:
            continue
        diff = tret - sret
        rows.append({
            "ticker": t,
            "sector_etf": sec,
            "ticker_ret_20d": round(tret, 4),
            "sector_ret_20d": round(sret, 4),
            "sector_rs_score": round(diff, 4),   # ±0.05 = ±5% outperformance
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        log.info("sector_rs done: %d tickers, mean RS %+0.3f", len(df), df["sector_rs_score"].mean())
    return df
