# Purpose: Hyperliquid open interest engine.
"""Hyperliquid open interest engine.

Hyperliquid is a decentralized perpetuals exchange with public REST API.
Per-asset open interest and 24h funding rate provide a sentiment signal
for crypto-correlated equities (MSTR, COIN, MARA, RIOT, BITO, IBIT, etc.).

Bullish signal: high positive funding (longs paying shorts) + rising OI.
Bearish signal: deeply negative funding + falling OI.

Free, no auth, no rate limit issues.

API: https://api.hyperliquid.xyz/info
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Dict, List

import pandas as pd

import sys
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider

log = logging.getLogger("optedge.hyperliquid")

# Crypto symbol -> equity exposures (high-correlation only)
CRYPTO_EQUITY_MAP = {
    "BTC": ["BITO", "IBIT", "GBTC", "MSTR", "COIN", "MARA", "RIOT", "CLSK",
            "HUT", "WULF", "CIFR", "CORZ", "BITF", "HIVE"],
    "ETH": ["ETHA", "COIN"],
    "SOL": ["COIN"],
}

HYPERLIQUID_API = "https://api.hyperliquid.xyz/info"


def _fetch_meta_and_ctxs() -> Dict:
    """Fetch universe metadata + funding/OI snapshots."""
    key = "hyperliquid:meta_ctxs"
    cached = data_provider.cache_get(key, max_age_sec=300)  # 5min cache
    if cached is not None:
        return cached
    try:
        import requests
        r = requests.post(HYPERLIQUID_API, json={"type": "metaAndAssetCtxs"}, timeout=15)
        if r.status_code != 200:
            return {}
        data = r.json()
        if not isinstance(data, list) or len(data) < 2:
            return {}
        meta = data[0]
        ctxs = data[1]
        result = {"meta": meta, "ctxs": ctxs}
        data_provider.cache_put(key, result)
        return result
    except Exception as e:
        log.debug("hyperliquid fetch: %s", e)
        return {}


def _score_from_ctx(ctx: Dict) -> float:
    """Combine funding + OI change into a sentiment score.

    funding is hourly (decimal): >0 = longs paying = bullish bias
    openInterest: in coin units; we don't have history here without storing —
    so we use funding-only proxy.
    """
    try:
        funding = float(ctx.get("funding", 0))
    except Exception:
        funding = 0.0
    # Funding is hourly; annualised = funding * 24 * 365
    annual_funding = funding * 24 * 365
    # Score: bull above +20% annualised, bear below -20%, scaled
    if annual_funding > 0.40:
        return 1.0
    if annual_funding > 0.20:
        return 0.5
    if annual_funding > 0.05:
        return 0.2
    if annual_funding < -0.20:
        return -0.5
    if annual_funding < -0.05:
        return -0.2
    return 0.0


def run(universe: List[str] = None) -> pd.DataFrame:
    """Per-ticker hyperliquid_score for crypto-correlated equities."""
    payload = _fetch_meta_and_ctxs()
    if not payload:
        log.info("hyperliquid: no data (network blocked or API down)")
        return pd.DataFrame()
    meta = payload["meta"]
    ctxs = payload["ctxs"]
    if "universe" not in meta:
        return pd.DataFrame()
    asset_by_name = {a["name"]: i for i, a in enumerate(meta["universe"])}
    rows = []
    for crypto, equities in CRYPTO_EQUITY_MAP.items():
        idx = asset_by_name.get(crypto)
        if idx is None or idx >= len(ctxs):
            continue
        ctx = ctxs[idx]
        score = _score_from_ctx(ctx)
        try:
            funding = float(ctx.get("funding", 0)) * 24 * 365
            oi = float(ctx.get("openInterest", 0))
        except Exception:
            funding, oi = 0.0, 0.0
        for tk in equities:
            rows.append({
                "ticker": tk,
                "hyperliquid_score": score,
                "hl_crypto": crypto,
                "hl_funding_annual": funding,
                "hl_open_interest": oi,
            })
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    # Take strongest signal per ticker
    out["abs"] = out["hyperliquid_score"].abs()
    out = out.sort_values("abs", ascending=False).drop_duplicates("ticker").drop(columns="abs")
    log.info("hyperliquid: %d ticker rows from %d crypto assets",
             len(out), out["hl_crypto"].nunique())
    return out.reset_index(drop=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run())
