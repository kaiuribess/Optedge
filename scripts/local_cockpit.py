"""Free local Optedge cockpit server.

This is a lightweight browser UI for existing Optedge artifacts. It does not
place trades, does not store broker credentials, and does not require paid
dashboard services.
"""
from __future__ import annotations

import argparse
import binascii
import io
import json
import math
import mimetypes
import re
import struct
import sys
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

import pandas as pd

ROOT_BOOTSTRAP = Path(__file__).resolve().parent.parent
if str(ROOT_BOOTSTRAP) not in sys.path:
    sys.path.insert(0, str(ROOT_BOOTSTRAP))

import data_provider
from engines import dark_pool as dark_pool_engine
from engines.fred_public import fred_csv_history
from engines.nasdaq_screener import small_cap_movers
from scripts.lookup_symbol import DATA_DIR, ROOT, lookup_symbol, render_html
from scripts.export_external_paper_track import (
    _load_option_chain_shortlist,
    build_external_orders,
    write_outputs as write_paper_outputs,
)
from scripts.export_robinhood_agentic_queue import (
    build_robinhood_queue, write_outputs as write_robinhood_queue_outputs,
)
from scripts.research_jobs import (
    create_job, create_refresh_job, job_dashboard_path, job_lookup_path, list_jobs,
    read_job, read_job_log,
)
from scripts.sec_filings import companyfacts_for_symbol, recent_filings_for_symbol
from scripts.symbol_resolver import (
    COMMON_ALIASES, load_nasdaq_symbol_directory, load_sec_company_tickers,
    nasdaq_symbol_cache_meta, nasdaq_symbol_search, resolve_symbol,
    sec_company_cache_meta, sec_company_search,
)


FRESH_SNAPSHOT_MINUTES = 90.0
STALE_SNAPSHOT_MINUTES = 360.0
MIN_SWING_OPTION_DTE = 90

ARTIFACTS = {
    "latest-dashboard": ("dashboard_*.html", "text/html; charset=utf-8"),
    "validation-report": ("validation_report.html", "text/html; charset=utf-8"),
    "validation-summary": ("validation_summary.json", "application/json; charset=utf-8"),
    "factor-ic": ("factor_ic_summary.json", "application/json; charset=utf-8"),
    "position-aging": ("position_aging_summary.json", "application/json; charset=utf-8"),
    "equity-curve": ("equity_curve.png", "image/png"),
    "external-paper-orders": ("external_paper_orders.csv", "text/csv; charset=utf-8"),
    "option-chain-shortlist": ("option_chain_shortlist.csv", "text/csv; charset=utf-8"),
    "option-chain-shortlist-json": ("option_chain_shortlist.json", "application/json; charset=utf-8"),
    "swing-packet-json": ("swing_packet.json", "application/json; charset=utf-8"),
    "swing-packet-md": ("swing_packet.md", "text/markdown; charset=utf-8"),
    "robinhood-agentic-queue": ("robinhood_agentic_queue.json", "application/json; charset=utf-8"),
    "robinhood-agentic-prompt": ("robinhood_agentic_prompt.md", "text/markdown; charset=utf-8"),
}

OPPORTUNITY_SPECS = {
    "option": {
        "pattern": "top_options_*.parquet",
        "label": "Options",
        "symbol_col": "ticker",
        "columns": [
            "asset", "actionable", "ticker", "side", "strike", "expiry", "dte", "mid", "spot",
            "confidence", "rank_score", "fused_score", "trade_status",
            "suggested_contracts", "spread_pct", "ev_pct", "net_edge_pct",
            "stop_price", "target_price", "chain_source", "quote_quality",
            "snapshot_age_min", "snapshot_freshness", "top_headline",
        ],
    },
    "share": {
        "pattern": "top_shares_*.parquet",
        "label": "Shares",
        "symbol_col": "ticker",
        "columns": [
            "asset", "actionable", "ticker", "spot", "confidence", "rank_score", "fused_score",
            "trade_status", "suggested_dollars", "ev_pct", "stop_price",
            "target_price", "snapshot_age_min", "snapshot_freshness", "top_headline",
        ],
    },
    "futures": {
        "pattern": "top_futures_*.parquet",
        "label": "Futures",
        "symbol_col": "symbol",
        "columns": [
            "asset", "actionable", "symbol", "name", "direction", "contract", "using_micro",
            "futures_score", "rank_score", "confidence", "trade_status",
            "suggested_contracts", "entry_price", "stop_price", "target_price",
            "risk_dollars", "reward_dollars", "ret_20d", "hv20", "range_pos",
            "snapshot_age_min", "snapshot_freshness", "top_headline",
        ],
    },
    "value": {
        "pattern": "top_value_*.parquet",
        "label": "Value",
        "symbol_col": "ticker",
        "columns": [
            "asset", "actionable", "ticker", "value_score", "value_bucket", "pe", "fcf_yield",
            "earnings_yield", "rev_growth", "op_margin", "insider_score",
            "n_buys", "n_sells", "snapshot_age_min", "snapshot_freshness", "top_headline",
        ],
    },
}

CHAIN_PRESETS = {
    "custom": {
        "label": "Custom",
        "description": "Use the filter controls as entered.",
    },
    "swing": {
        "label": "3m+ swing",
        "description": "90-180 DTE, moderate spreads, under about $500 premium.",
        "side": "all",
        "min_dte": MIN_SWING_OPTION_DTE,
        "max_dte": 180,
        "max_spread_pct": 0.20,
        "max_premium": 500.0,
        "min_open_interest": 25,
    },
    "leaps": {
        "label": "Long dated",
        "description": "180-900 DTE contracts for slower swing/LEAPS-style review.",
        "side": "all",
        "min_dte": 180,
        "max_dte": 900,
        "max_spread_pct": 0.25,
        "max_premium": 750.0,
        "min_open_interest": 10,
    },
    "liquid": {
        "label": "Liquid",
        "description": "Higher open interest and tighter spreads for 90+ DTE review.",
        "side": "all",
        "min_dte": MIN_SWING_OPTION_DTE,
        "max_dte": 365,
        "max_spread_pct": 0.12,
        "max_premium": 0.0,
        "min_open_interest": 100,
    },
}

CHAIN_CONTEXT_FIELDS = {
    "source",
    "chain_source",
    "quote_quality",
    "data_delay",
    "contract_grade",
    "review_lane",
    "review_thesis",
    "grade_reasons",
    "readiness_label",
    "readiness_score",
    "risk_flags",
    "contract_quality_score",
    "swing_fit_score",
    "swing_fit_label",
    "chain_factor_summary",
    "chain_factor_breakdown",
    "swing_fit_reasons",
    "swing_fit_warnings",
    "breakeven_move_label",
    "liquidity_label",
    "bid",
    "ask",
    "mid",
    "spread_pct",
    "premium_dollars",
    "volume",
    "openInterest",
    "impliedVolatility",
    "delta",
    "moneyness_pct",
    "breakeven_price",
    "breakeven_move_pct",
    "breakeven_direction",
    "budget_usage_pct",
    "contracts_for_budget",
    "stop_price_reference",
    "target_price_reference",
    "risk_dollars_reference",
    "reward_dollars_reference",
    "reward_risk_reference",
    "budget_fit",
    "dte",
    "dte_bucket",
    "scan_preset",
    "scan_symbol",
}

CHAIN_SHORTLIST_COLUMNS = [
    "generated_at",
    "symbol",
    "contract_query",
    "side",
    "expiry",
    "strike",
    "dte",
    "mid",
    "premium_dollars",
    "bid",
    "ask",
    "spread_pct",
    "openInterest",
    "volume",
    "impliedVolatility",
    "delta",
    "moneyness_pct",
    "breakeven_price",
    "breakeven_move_pct",
    "breakeven_direction",
    "budget_usage_pct",
    "contracts_for_budget",
    "stop_price_reference",
    "target_price_reference",
    "risk_dollars_reference",
    "reward_dollars_reference",
    "reward_risk_reference",
    "budget_fit",
    "contract_grade",
    "review_lane",
    "readiness_label",
    "readiness_score",
    "contract_quality_score",
    "swing_fit_score",
    "swing_fit_label",
    "chain_factor_summary",
    "chain_factor_breakdown",
    "swing_fit_reasons",
    "swing_fit_warnings",
    "breakeven_move_label",
    "liquidity_label",
    "quote_quality",
    "chain_source",
    "data_delay",
    "candidate_source",
    "candidate_reason",
    "open_exposure_count",
    "open_exposure_assets",
    "open_exposure_summary",
    "open_exposure_attention_count",
    "risk_flags",
    "grade_reasons",
    "review_thesis",
]

POSITION_FILES = {
    "option": "open_positions.json",
    "share": "open_share_positions.json",
    "futures": "open_futures_positions.json",
}

MARKET_PULSE_SYMBOLS = [
    {"symbol": "SPY", "label": "S&P 500", "kind": "equity_index", "risk_weight": 1.0},
    {"symbol": "QQQ", "label": "Nasdaq 100", "kind": "growth_index", "risk_weight": 1.0},
    {"symbol": "IWM", "label": "Small caps", "kind": "risk_breadth", "risk_weight": 1.0},
    {"symbol": "DIA", "label": "Dow", "kind": "large_caps", "risk_weight": 0.6},
    {"symbol": "TLT", "label": "Long bonds", "kind": "rates", "risk_weight": -0.4},
    {"symbol": "GLD", "label": "Gold", "kind": "safe_haven", "risk_weight": -0.2},
    {"symbol": "USO", "label": "Oil", "kind": "energy", "risk_weight": 0.2},
    {"symbol": "UUP", "label": "Dollar", "kind": "dollar", "risk_weight": -0.3},
    {"symbol": "^VIX", "label": "VIX", "kind": "volatility", "risk_weight": -1.0},
]

CBOE_PUT_CALL_SOURCES = [
    {
        "key": "total",
        "label": "Total options",
        "url": "https://cdn.cboe.com/resources/options/volume_and_call_put_ratios/totalpc.csv",
    },
    {
        "key": "equity",
        "label": "Equity options",
        "url": "https://cdn.cboe.com/resources/options/volume_and_call_put_ratios/equitypc.csv",
    },
    {
        "key": "index",
        "label": "Index options",
        "url": "https://cdn.cboe.com/resources/options/volume_and_call_put_ratios/indexpcarchive.csv",
    },
]
CBOE_DAILY_STATS_URL = "https://www.cboe.com/markets/us/options/market-statistics/daily/"


SECTOR_PULSE_SYMBOLS = [
    {"symbol": "XLK", "sector": "Technology", "group": "sector"},
    {"symbol": "XLF", "sector": "Financials", "group": "sector"},
    {"symbol": "XLE", "sector": "Energy", "group": "sector"},
    {"symbol": "XLV", "sector": "Healthcare", "group": "sector"},
    {"symbol": "XLY", "sector": "Consumer Discretionary", "group": "sector"},
    {"symbol": "XLP", "sector": "Consumer Staples", "group": "sector"},
    {"symbol": "XLI", "sector": "Industrials", "group": "sector"},
    {"symbol": "XLC", "sector": "Communication Services", "group": "sector"},
    {"symbol": "XLB", "sector": "Materials", "group": "sector"},
    {"symbol": "XLU", "sector": "Utilities", "group": "sector"},
    {"symbol": "XLRE", "sector": "Real Estate", "group": "sector"},
    {"symbol": "SMH", "sector": "Semiconductors", "group": "industry_proxy"},
    {"symbol": "IYT", "sector": "Transports", "group": "industry_proxy"},
]

BREADTH_PULSE_PAIRS = [
    {
        "label": "Equal-weight breadth",
        "numerator": "RSP",
        "denominator": "SPY",
        "kind": "participation",
        "bullish_when": "positive",
        "description": "Equal-weight S&P 500 outperforming cap-weighted SPY means participation is broadening.",
    },
    {
        "label": "Small-cap breadth",
        "numerator": "IWM",
        "denominator": "SPY",
        "kind": "risk_breadth",
        "bullish_when": "positive",
        "description": "Small caps outperforming SPY usually supports risk-on swing conditions.",
    },
    {
        "label": "Growth leadership",
        "numerator": "QQQ",
        "denominator": "SPY",
        "kind": "growth",
        "bullish_when": "positive",
        "description": "QQQ outperforming SPY shows growth leadership.",
    },
    {
        "label": "Consumer risk appetite",
        "numerator": "XLY",
        "denominator": "XLP",
        "kind": "risk_appetite",
        "bullish_when": "positive",
        "description": "Discretionary outperforming staples is a classic risk-appetite check.",
    },
    {
        "label": "Credit risk appetite",
        "numerator": "HYG",
        "denominator": "LQD",
        "kind": "credit",
        "bullish_when": "positive",
        "description": "High-yield credit outperforming investment-grade credit supports risk appetite.",
    },
    {
        "label": "Semiconductor leadership",
        "numerator": "SMH",
        "denominator": "QQQ",
        "kind": "leadership",
        "bullish_when": "positive",
        "description": "Semis leading QQQ can support tech and AI-related swing setups.",
    },
    {
        "label": "Defensive pressure",
        "numerator": "XLU",
        "denominator": "SPY",
        "kind": "defensive",
        "bullish_when": "negative",
        "description": "Utilities outperforming SPY can signal defensive pressure; lower is better for risk-on longs.",
    },
]

MACRO_STRESS_SERIES = [
    {
        "series_id": "BAMLH0A0HYM2",
        "label": "High-yield credit spread",
        "category": "credit",
        "unit": "%",
        "days": 90,
        "description": "Wider junk-bond spreads usually mean risk appetite is weakening.",
    },
    {
        "series_id": "T10Y3M",
        "label": "10Y-3M yield curve",
        "category": "rates",
        "unit": "%",
        "days": 180,
        "description": "A deeply inverted curve is a macro warning for swing risk.",
    },
    {
        "series_id": "UNRATE",
        "label": "Unemployment rate",
        "category": "labor",
        "unit": "%",
        "days": 420,
        "description": "A rising unemployment rate can signal late-cycle deterioration.",
    },
    {
        "series_id": "ICSA",
        "label": "Initial jobless claims",
        "category": "labor",
        "unit": "claims",
        "days": 180,
        "description": "Claims above trend can point to weakening labor conditions.",
    },
    {
        "series_id": "CPIAUCSL",
        "label": "CPI inflation YoY",
        "category": "inflation",
        "unit": "%",
        "days": 520,
        "description": "High inflation can keep policy tighter for longer.",
    },
    {
        "series_id": "INDPRO",
        "label": "Industrial production YoY",
        "category": "growth",
        "unit": "%",
        "days": 520,
        "description": "Negative production growth can warn that economic momentum is fading.",
    },
    {
        "series_id": "M2SL",
        "label": "M2 liquidity YoY",
        "category": "liquidity",
        "unit": "%",
        "days": 520,
        "description": "Rising money supply can be a liquidity tailwind; contraction is a headwind.",
    },
]

FREE_DATA_SOURCE_REGISTRY = [
    {
        "name": "Yahoo chart",
        "category": "prices",
        "coverage": "US equities, ETFs, indexes, futures proxies",
        "credential": "none",
        "quality": "free_or_delayed",
        "used_by": "history, technicals, futures, market pulse, repricing",
        "primary": True,
        "caveat": "Rate limits and symbol gaps can happen.",
    },
    {
        "name": "yfinance",
        "category": "prices/fundamentals/options",
        "coverage": "history, fundamentals, earnings, fallback option chains",
        "credential": "none",
        "quality": "free_or_delayed",
        "used_by": "fundamentals, earnings, whisper, option fallback",
        "primary": False,
        "caveat": "Unofficial and can throttle.",
    },
    {
        "name": "Google News RSS",
        "category": "news",
        "coverage": "ticker headline search and news momentum",
        "credential": "none",
        "quality": "public_web",
        "used_by": "news sentiment, FinBERT headline scoring, dashboard context",
        "primary": True,
        "caveat": "Headline search can be noisy or temporarily blocked.",
    },
    {
        "name": "Yahoo Finance RSS",
        "category": "news",
        "coverage": "ticker-specific finance headlines",
        "credential": "none",
        "quality": "public_web",
        "used_by": "news fallback, headline sentiment, dashboard context",
        "primary": False,
        "caveat": "Coverage can be sparse and should be treated as delayed headline context.",
    },
    {
        "name": "CBOE option chains",
        "category": "options",
        "coverage": "US listed equity and ETF option chains",
        "credential": "none",
        "quality": "free_or_delayed",
        "used_by": "mispricing, chain scan, chain sweep, saved contract quotes",
        "primary": True,
        "caveat": "Not an execution quote; availability can vary by ticker.",
    },
    {
        "name": "CBOE put/call market statistics",
        "category": "options_sentiment",
        "coverage": "market-wide total, equity, and index option put/call ratios",
        "credential": "none",
        "quality": "informational_delayed",
        "used_by": "market pulse and options sentiment context",
        "primary": True,
        "caveat": "Informational market statistics, not a live options quote feed.",
    },
    {
        "name": "FINRA RegSHO short volume",
        "category": "market_structure/short_volume",
        "coverage": "daily short-volume ratio by US equity symbol",
        "credential": "none",
        "quality": "official_public_delayed",
        "used_by": "dark pool score, short-interest amplifier, small-cap mover context",
        "primary": True,
        "caveat": "Short-volume is not the same as short interest; use it as pressure context only.",
    },
    {
        "name": "Nasdaq option/historical",
        "category": "prices/options",
        "coverage": "US historical rows and option-chain fallback paths",
        "credential": "none",
        "quality": "free_or_delayed",
        "used_by": "history fallback, option fallback",
        "primary": False,
        "caveat": "Public endpoint can be partial or blocked.",
    },
    {
        "name": "Stooq CSV",
        "category": "prices",
        "coverage": "US daily history fallback",
        "credential": "none",
        "quality": "delayed",
        "used_by": "last-resort history fallback",
        "primary": False,
        "caveat": "Daily data only and may not cover every symbol.",
    },
    {
        "name": "SEC EDGAR",
        "category": "filings/fundamentals",
        "coverage": "company tickers, company facts, Forms 4/144, 8-K catalysts, 13F, filings",
        "credential": "none",
        "quality": "official_public",
        "used_by": "insider, form 144, FDA catalyst fallback, 13F, symbol search, ticker lookup fundamentals",
        "primary": True,
        "caveat": "Filing timestamps and issuer mappings need normalization.",
    },
    {
        "name": "Nasdaq Trader symbol directory",
        "category": "symbol_search/universe",
        "coverage": "Nasdaq-listed and other-exchange US symbols, ETFs, exchange flags, test-issue flags",
        "credential": "none",
        "quality": "official_public",
        "used_by": "dashboard autocomplete, ticker resolution, universe hygiene",
        "primary": True,
        "caveat": "Directory metadata is not a quote feed and does not imply options liquidity.",
    },
    {
        "name": "House/Senate disclosures",
        "category": "filings",
        "coverage": "Congressional transaction disclosure PDFs",
        "credential": "none",
        "quality": "public_delayed",
        "used_by": "congress engine",
        "primary": True,
        "caveat": "PDF parsing can be slow and disclosure lag is normal.",
    },
    {
        "name": "Reddit JSON",
        "category": "social",
        "coverage": "WSB and r/options retail ticker mentions",
        "credential": "none",
        "quality": "public_web",
        "used_by": "WSB trending, r/options, sentiment",
        "primary": True,
        "caveat": "Social data is noisy and rate-limited.",
    },
    {
        "name": "ApeWisdom / StockTwits",
        "category": "social",
        "coverage": "retail attention and ticker chatter",
        "credential": "none",
        "quality": "public_web",
        "used_by": "twitter/social retail attention",
        "primary": True,
        "caveat": "Public web layouts can change.",
    },
    {
        "name": "Wikipedia pageviews",
        "category": "attention",
        "coverage": "search/attention fallback for ticker/company pages",
        "credential": "none",
        "quality": "public_delayed",
        "used_by": "Google Trends fallback",
        "primary": False,
        "caveat": "Proxy for attention, not trading flow.",
    },
    {
        "name": "CFTC Socrata CoT",
        "category": "futures/macro",
        "coverage": "commitment-of-traders positioning",
        "credential": "none",
        "quality": "official_public_delayed",
        "used_by": "CoT engine and futures context",
        "primary": True,
        "caveat": "Weekly and delayed by design.",
    },
    {
        "name": "EIA public energy data",
        "category": "macro/commodities",
        "coverage": "oil and natural gas inventory context",
        "credential": "none",
        "quality": "official_public",
        "used_by": "EIA engine, energy/futures context",
        "primary": True,
        "caveat": "Release schedule matters; not tick-by-tick.",
    },
    {
        "name": "FRED public CSV",
        "category": "macro/rates",
        "coverage": "rates, spreads, macro series",
        "credential": "none",
        "quality": "official_public",
        "used_by": "yield curve, credit spread, macro context",
        "primary": True,
        "caveat": "Economic series update on official release cadence.",
    },
    {
        "name": "Treasury yield XML",
        "category": "macro/rates",
        "coverage": "official daily Treasury par yield curve",
        "credential": "none",
        "quality": "official_public",
        "used_by": "yield curve fallback and rates context",
        "primary": False,
        "caveat": "Official end-of-day curve data, not intraday rates.",
    },
    {
        "name": "Hyperliquid public API",
        "category": "crypto",
        "coverage": "crypto market context and futures proxies",
        "credential": "none",
        "quality": "public_near_realtime",
        "used_by": "hyperliquid engine",
        "primary": True,
        "caveat": "Crypto context does not replace equity option quotes.",
    },
]

WATCHLIST_FILENAME = "cockpit_watchlist.json"


def _latest_file(data_dir: Path, pattern: str) -> Path | None:
    files = [p for p in data_dir.glob(pattern) if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: (p.stat().st_mtime, p.name))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _count_json_rows(path: Path) -> int:
    rows = _read_json(path)
    return len(rows) if isinstance(rows, list) else 0


def _direct_open_counts(data_dir: Path) -> dict[str, int]:
    return {
        "options": _count_json_rows(data_dir / "open_positions.json"),
        "shares": _count_json_rows(data_dir / "open_share_positions.json"),
        "futures": _count_json_rows(data_dir / "open_futures_positions.json"),
    }


def _snapshot_age_minutes(path: Path) -> float:
    modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - modified).total_seconds() / 60.0)


def _snapshot_freshness(age_minutes: float | None) -> str:
    if age_minutes is None:
        return "unknown"
    if age_minutes <= FRESH_SNAPSHOT_MINUTES:
        return "fresh"
    if age_minutes <= STALE_SNAPSHOT_MINUTES:
        return "aging"
    return "stale"


def _read_parquet(path: Path | None) -> pd.DataFrame:
    if path is None:
        return pd.DataFrame()
    try:
        df = pd.read_parquet(path)
    except Exception:
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    age = _snapshot_age_minutes(path)
    out["_source_file"] = path.name
    out["snapshot_age_min"] = round(age, 1)
    out["snapshot_freshness"] = _snapshot_freshness(age)
    return out


def _file_meta(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    stat = path.stat()
    modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
    age_minutes = max(0.0, (datetime.now(timezone.utc) - modified).total_seconds() / 60.0)
    return {
        "name": path.name,
        "path": str(path),
        "size_bytes": int(stat.st_size),
        "modified_at": modified.isoformat(),
        "age_minutes": round(age_minutes, 1),
    }


def _png_validation_error(path: Path | None) -> str | None:
    """Return a short error if a PNG is missing/corrupt, otherwise None."""
    if path is None or not path.exists() or not path.is_file():
        return "missing"
    try:
        data = path.read_bytes()
    except Exception as exc:
        return f"could not read: {exc}"
    if len(data) < 12 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "not a PNG"
    offset = 8
    saw_iend = False
    while offset + 8 <= len(data):
        try:
            length = struct.unpack(">I", data[offset:offset + 4])[0]
        except Exception:
            return "invalid chunk length"
        chunk_type = data[offset + 4:offset + 8]
        offset += 8
        chunk_end = offset + length
        crc_end = chunk_end + 4
        if crc_end > len(data):
            return f"truncated {chunk_type.decode('ascii', errors='replace')} chunk"
        chunk = data[offset:chunk_end]
        expected = struct.unpack(">I", data[chunk_end:crc_end])[0]
        actual = binascii.crc32(chunk_type + chunk) & 0xFFFFFFFF
        if actual != expected:
            name = chunk_type.decode("ascii", errors="replace")
            return f"{name} CRC mismatch"
        offset = crc_end
        if chunk_type == b"IEND":
            saw_iend = True
            break
    if not saw_iend:
        return "missing IEND chunk"
    return None


def _clean_value(value: Any) -> Any:
    try:
        if value is None or pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def _clean_watchlist_context(context: Any) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for key in CHAIN_CONTEXT_FIELDS:
        if key not in context:
            continue
        value = context.get(key)
        if isinstance(value, list):
            items = []
            for item in value[:8]:
                clean = _clean_value(item)
                if isinstance(clean, str):
                    clean = clean[:220]
                if clean is None or isinstance(clean, (str, int, float, bool)):
                    items.append(clean)
            if items:
                cleaned[key] = items
            continue
        clean_value = _clean_value(value)
        if isinstance(clean_value, str):
            clean_value = clean_value[:700]
        if clean_value is None or isinstance(clean_value, (str, int, float, bool)):
            cleaned[key] = clean_value
    if cleaned:
        cleaned["saved_from"] = "option_chain_scan"
        cleaned["saved_context_at"] = _now_iso()
    return cleaned


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except Exception:
        return default
    return out if math.isfinite(out) else default


def _position_identity(row: dict[str, Any]) -> tuple:
    pid = row.get("position_id")
    if pid:
        return ("id", str(pid))
    return (
        str(row.get("asset") or ""),
        str(row.get("ticker") or row.get("symbol") or row.get("ticker_or_symbol") or ""),
        str(row.get("side") or row.get("direction") or row.get("side_or_direction") or ""),
        str(row.get("strike") or row.get("contract") or row.get("strike_or_contract") or ""),
        str(row.get("expiry") or ""),
        str(row.get("entry_time") or ""),
        str(row.get("entry_price") or ""),
    )


def _dedupe_position_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = _position_identity(row)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _watchlist_file(data_dir: Path = DATA_DIR) -> Path:
    return Path(data_dir) / WATCHLIST_FILENAME


def _safe_id(value: Any) -> str:
    text = str(value or "").strip()
    safe = "".join(ch if ch.isalnum() or ch in {"_", "-", "=", "."} else "_" for ch in text)
    return safe[:96] or "item"


def _watchlist_entry_id(resolution: dict[str, Any], query: str) -> str:
    symbol = str(resolution.get("symbol") or query).upper()
    request = resolution.get("request") or {}
    if request:
        raw = (
            f"{symbol}_{request.get('side','')}_{request.get('expiry','')}_"
            f"{request.get('strike','')}"
        )
    else:
        raw = symbol
    return _safe_id(raw)


def _watchlist_lookup_query(entry: dict[str, Any]) -> str:
    if entry.get("request"):
        return str(entry.get("query") or entry.get("symbol") or "").strip()
    return str(entry.get("symbol") or entry.get("query") or "").strip()


def _enrich_watchlist_entry(entry: dict[str, Any], data_dir: Path) -> dict[str, Any]:
    out = dict(entry)
    query = _watchlist_lookup_query(entry)
    if not query:
        return out
    try:
        report = lookup_symbol(query, data_dir, include_sec=False)
        brief = report.get("brief") or {}
        best = brief.get("best_idea") or {}
        open_pos = brief.get("open_positions") or {}
        readiness = brief.get("paper_readiness") or {}
        readiness_checks = readiness.get("checks") if isinstance(readiness.get("checks"), list) else []
        out.update({
            "local_hits": _clean_value(report.get("total_hits")),
            "best_idea": _clean_value(best.get("label")),
            "best_status": _clean_value(best.get("trade_status")),
            "best_confidence": _clean_value(best.get("confidence")),
            "best_score": _clean_value(best.get("score")),
            "paper_readiness_status": _clean_value(readiness.get("status")),
            "paper_readiness_label": _clean_value(readiness.get("label")),
            "paper_readiness_score": _clean_value(readiness.get("score")),
            "paper_readiness_bad_count": sum(1 for row in readiness_checks if row.get("level") == "bad"),
            "paper_readiness_warn_count": sum(1 for row in readiness_checks if row.get("level") == "warn"),
            "open_count": _clean_value(open_pos.get("count")),
            "avg_unrealized_pct": _clean_value(open_pos.get("avg_unrealized_pct")),
            "max_exit_pressure": _clean_value(open_pos.get("max_exit_pressure")),
            "warning_count": len(brief.get("risk_warnings") or []),
            "last_enriched_at": _now_iso(),
        })
    except Exception as exc:
        out["enrichment_error"] = str(exc)[:180]
    return out


def _watchlist_sort_key(row: dict[str, Any]) -> tuple[int, float, float, float, str]:
    status_rank = {
        "ready": 3,
        "caution": 2,
        "blocked": 1,
    }.get(str(row.get("paper_readiness_status") or "").lower(), 0)
    return (
        status_rank,
        _float_value(row.get("paper_readiness_score"), 0.0),
        _float_value(row.get("max_exit_pressure"), 0.0),
        _float_value(row.get("best_score"), 0.0),
        str(row.get("updated_at") or row.get("added_at") or ""),
    )


def load_watchlist(data_dir: Path = DATA_DIR, enrich: bool = False) -> dict[str, Any]:
    rows = _read_json(_watchlist_file(data_dir))
    if not isinstance(rows, list):
        rows = []
    cleaned: list[dict[str, Any]] = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id = str(row.get("id") or "").strip()
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        cleaned.append(row)
    entries = [_enrich_watchlist_entry(row, Path(data_dir)) for row in cleaned] if enrich else cleaned
    if enrich:
        entries = sorted(entries, key=_watchlist_sort_key, reverse=True)
    return {
        "generated_at": _now_iso(),
        "count": len(entries),
        "enriched": enrich,
        "entries": entries,
        "path": str(_watchlist_file(data_dir)),
        "notes": [
            "Watchlist entries are local research targets only.",
            "Enriched watchlists read the latest local scan snapshots and open positions.",
            "Run all launches focused scans for resolved symbols; no trades are placed.",
        ],
    }


def _parse_date_yyyy_mm_dd(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:10]).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _sec_filing_priority(form: Any, signal: Any, days_old: int | None) -> tuple[int, str]:
    clean_form = str(form or "").upper().strip()
    clean_signal = str(signal or "").strip().lower()
    base = {
        "S-1": 96,
        "S-3": 94,
        "424B5": 94,
        "424B2": 90,
        "8-K": 86,
        "SC 13D": 82,
        "SC 13G": 76,
        "10-Q": 72,
        "10-K": 70,
        "4": 66,
    }.get(clean_form, 55)
    if "dilution" in clean_signal or "offering" in clean_signal:
        base = max(base, 94)
    elif "material_event" in clean_signal:
        base = max(base, 86)
    elif "ownership_change" in clean_signal:
        base = max(base, 78)
    if days_old is None:
        return max(40, base - 8), "date_unknown"
    if days_old <= 3:
        return min(100, base + 5), "fresh"
    if days_old <= 14:
        return base, "recent"
    if days_old <= 45:
        return max(35, int(base - days_old * 0.7)), "aging"
    return max(20, int(base - days_old * 0.9)), "old"


def build_watchlist_sec_filings(data_dir: Path = DATA_DIR, limit: int = 40) -> dict[str, Any]:
    """Build a no-key SEC recent-filing monitor for saved research targets."""
    limit = max(1, min(int(limit or 40), 120))
    watchlist = load_watchlist(data_dir, enrich=False)
    symbols: list[str] = []
    seen = set()
    for row in watchlist.get("entries", []):
        symbol = str(row.get("symbol") or "").upper().strip()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)

    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    today = datetime.now(timezone.utc)
    for symbol in symbols[:60]:
        try:
            report = recent_filings_for_symbol(symbol, limit=8)
        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)[:180]})
            continue
        filings = report.get("rows", []) if isinstance(report, dict) else []
        for filing in filings:
            if not isinstance(filing, dict):
                continue
            filing_date = _parse_date_yyyy_mm_dd(filing.get("filing_date"))
            days_old = (today.date() - filing_date.date()).days if filing_date else None
            priority, freshness = _sec_filing_priority(
                filing.get("form"), filing.get("filing_signal"), days_old,
            )
            rows.append({
                "priority": priority,
                "ticker": symbol,
                "company_name": filing.get("company_name") or report.get("company_name"),
                "form": filing.get("form"),
                "filing_date": filing.get("filing_date"),
                "days_old": days_old,
                "freshness": freshness,
                "signal": filing.get("filing_signal"),
                "description": filing.get("description"),
                "url": filing.get("url"),
            })

    rows = sorted(
        rows,
        key=lambda row: (
            _float_value(row.get("priority"), default=0.0),
            str(row.get("filing_date") or ""),
        ),
        reverse=True,
    )[:limit]
    signal_counts: dict[str, int] = {}
    form_counts: dict[str, int] = {}
    for row in rows:
        signal = str(row.get("signal") or "unknown")
        form = str(row.get("form") or "unknown")
        signal_counts[signal] = signal_counts.get(signal, 0) + 1
        form_counts[form] = form_counts.get(form, 0) + 1

    fresh_count = sum(_float_value(row.get("days_old"), default=9999.0) <= 14 for row in rows)
    high_impact_count = sum(
        str(row.get("signal") or "") in {
            "dilution_or_offering_watch",
            "material_event_review",
            "ownership_change_review",
            "fundamental_update_review",
        }
        for row in rows
    )
    return {
        "generated_at": _now_iso(),
        "symbols_checked": len(symbols),
        "filing_count": len(rows),
        "fresh_count": fresh_count,
        "high_impact_count": high_impact_count,
        "error_count": len(errors),
        "form_counts": form_counts,
        "signal_counts": signal_counts,
        "rows": [{k: _clean_value(v) for k, v in row.items()} for row in rows],
        "errors": errors,
        "notes": [
            "SEC Filing Monitor uses the official no-key SEC submissions API.",
            "It watches saved research targets only, so it stays focused and polite to public sources.",
            "Filings are review prompts, not automatic trade entries or exits.",
        ],
    }


def _request_dte(expiry: Any) -> int | None:
    exp = pd.to_datetime(str(expiry or ""), errors="coerce", utc=True)
    if pd.isna(exp):
        return None
    return int((exp.date() - datetime.now(timezone.utc).date()).days)


def _saved_contract_status(dte: int | None, readiness: Any) -> str:
    if dte is None:
        return "needs_expiry_check"
    if dte < 0:
        return "expired"
    if dte < MIN_SWING_OPTION_DTE:
        return "below_3m"
    ready = str(readiness or "").strip().lower()
    if ready == "ready":
        return "ready_review"
    if ready in {"caution", "blocked"}:
        return ready
    return "saved_review"


def _norm_option_side(value: Any) -> str:
    side = str(value or "").strip().lower()
    if side in {"c", "call", "calls"}:
        return "call"
    if side in {"p", "put", "puts"}:
        return "put"
    return side


def _saved_contract_quote_snapshot(symbol: str, request: dict[str, Any], dte: int | None) -> dict[str, Any]:
    ticker = str(symbol or request.get("ticker") or "").upper()
    expiry = str(request.get("expiry") or "").strip()
    side = _norm_option_side(request.get("side"))
    strike = _float_value(request.get("strike"), default=math.nan)
    if not ticker or not expiry or side not in {"call", "put"} or not math.isfinite(strike):
        return {"quote_status": "invalid_request", "quote_checked_at": _now_iso()}

    try:
        blob = _fetch_option_chain(ticker, cache_age=300)
    except Exception as exc:
        return {
            "quote_status": "fetch_failed",
            "quote_error": str(exc)[:160],
            "quote_checked_at": _now_iso(),
        }
    chains = blob.get("chains") if isinstance(blob, dict) else None
    chain_df = chains.get(expiry) if isinstance(chains, dict) else None
    if not isinstance(chain_df, pd.DataFrame) or chain_df.empty:
        return {
            "quote_status": "missing_expiry",
            "chain_source": _clean_value(blob.get("source")) if isinstance(blob, dict) else None,
            "quote_quality": _clean_value(blob.get("quote_quality")) if isinstance(blob, dict) else None,
            "quote_checked_at": _now_iso(),
        }

    matches = []
    for _, raw in chain_df.iterrows():
        raw_side = _norm_option_side(raw.get("side"))
        raw_strike = _float_value(raw.get("strike"), default=math.nan)
        if raw_side == side and math.isfinite(raw_strike) and abs(raw_strike - strike) <= 0.0001:
            matches.append(raw)
    if not matches:
        return {
            "quote_status": "missing_contract",
            "chain_source": _clean_value(blob.get("source")) if isinstance(blob, dict) else None,
            "quote_quality": _clean_value(blob.get("quote_quality")) if isinstance(blob, dict) else None,
            "quote_checked_at": _now_iso(),
        }

    best = sorted(
        matches,
        key=lambda row: (
            _float_value(row.get("openInterest"), default=0.0),
            _float_value(row.get("volume"), default=0.0),
        ),
        reverse=True,
    )[0]
    mid = _option_mid(best)
    spread_pct = _option_spread_pct(best, mid)
    spot = _float_value(blob.get("spot"), default=math.nan) if isinstance(blob, dict) else math.nan
    moneyness = ((strike - spot) / spot) if math.isfinite(strike) and math.isfinite(spot) and spot > 0 else None
    quote_quality = (
        blob.get("quote_quality")
        or ("live_or_broker" if str(blob.get("source") or "") == "tradier" else "free_or_delayed")
    ) if isinstance(blob, dict) else "unknown"
    quote_row = {
        "symbol": ticker,
        "side": side,
        "expiry": expiry,
        "dte": dte,
        "strike": strike,
        "bid": _clean_value(best.get("bid")),
        "ask": _clean_value(best.get("ask")),
        "mid": round(mid, 4) if math.isfinite(mid) else None,
        "premium_dollars": round(mid * 100.0, 2) if math.isfinite(mid) else None,
        "spread_pct": _clean_value(spread_pct),
        "volume": int(_float_value(best.get("volume"), default=0.0)),
        "openInterest": int(_float_value(best.get("openInterest"), default=0.0)),
        "impliedVolatility": _clean_value(best.get("impliedVolatility")),
        "delta": _clean_value(best.get("delta")),
        "moneyness_pct": _clean_value(moneyness),
    }
    quote_row["contract_quality_score"] = round(_option_chain_score(quote_row), 3)
    quote_row.update(_option_contract_readiness(quote_row, str(quote_quality)))
    return {
        "quote_status": "matched",
        "quote_checked_at": _now_iso(),
        "chain_source": _clean_value(blob.get("source")) if isinstance(blob, dict) else None,
        "quote_quality": _clean_value(quote_quality),
        "current_mid": quote_row["mid"],
        "current_bid": quote_row["bid"],
        "current_ask": quote_row["ask"],
        "current_premium_dollars": quote_row["premium_dollars"],
        "current_spread_pct": quote_row["spread_pct"],
        "current_volume": quote_row["volume"],
        "current_open_interest": quote_row["openInterest"],
        "current_iv": quote_row["impliedVolatility"],
        "current_delta": quote_row["delta"],
        "quote_readiness_label": quote_row["readiness_label"],
        "quote_readiness_score": quote_row["readiness_score"],
        "quote_flags": quote_row["risk_flags"],
        "contract_quality_score": quote_row["contract_quality_score"],
    }


def _saved_contract_review(row: dict[str, Any]) -> dict[str, Any]:
    score = 100
    reasons: list[str] = []
    dte = _float_value(row.get("dte"), default=math.nan)
    quote_status = str(row.get("quote_status") or "not_checked")
    spread = _float_value(row.get("current_spread_pct"), default=math.nan)
    quote_score = _float_value(row.get("quote_readiness_score"), default=math.nan)
    quote_label = str(row.get("quote_readiness_label") or "").lower()
    paper_status = str(row.get("paper_readiness") or row.get("status") or "").lower()
    warnings = _float_value(row.get("warning_count"), default=0.0)
    saved_grade = str(row.get("saved_contract_grade") or "").upper()

    if not math.isfinite(dte):
        score -= 25
        reasons.append("expiry needs review")
    elif dte < 0:
        score -= 100
        reasons.append("expired")
    elif dte < MIN_SWING_OPTION_DTE:
        score -= 35
        reasons.append("below 90 DTE")
    else:
        reasons.append("3m+ DTE")

    if quote_status == "matched":
        reasons.append("quote matched")
    elif quote_status == "not_checked":
        score -= 18
        reasons.append("refresh quote first")
    elif quote_status == "not_checked_limit":
        score -= 16
        reasons.append("quote refresh limit")
    else:
        score -= 35
        reasons.append(str(quote_status).replace("_", " "))

    if math.isfinite(spread):
        if spread > 0.25:
            score -= 30
            reasons.append(f"spread {spread * 100:.1f}%")
        elif spread > 0.15:
            score -= 15
            reasons.append(f"spread {spread * 100:.1f}%")
        else:
            reasons.append(f"spread {spread * 100:.1f}%")
    elif quote_status == "matched":
        score -= 12
        reasons.append("spread missing")

    if math.isfinite(quote_score):
        if quote_score < 65:
            score -= 18
            reasons.append(f"quote score {quote_score:g}")
        elif quote_score >= 80:
            reasons.append(f"quote score {quote_score:g}")
    if saved_grade in {"A", "B"}:
        reasons.append(f"saved grade {saved_grade}")
    elif saved_grade == "D":
        score -= 8
        reasons.append("saved grade D")
    if quote_label == "wait":
        score -= 20
        reasons.append("quote readiness wait")
    if "blocked" in paper_status:
        score -= 15
        reasons.append("local readiness blocked")
    if warnings >= 5:
        score -= 10
        reasons.append(f"{int(warnings)} local warning(s)")

    clean_score = max(0, min(100, int(round(score))))
    if clean_score >= 80 and quote_status == "matched":
        action = "review_now"
    elif quote_status != "matched":
        action = "refresh_quote"
    elif clean_score >= 60:
        action = "watch"
    else:
        action = "wait"
    return {
        "review_score": clean_score,
        "review_action": action,
        "review_reasons": reasons[:6],
    }


def _saved_grade_rank(value: Any) -> int:
    return {"A": 4, "B": 3, "C": 2, "D": 1}.get(str(value or "").upper(), 0)


def _saved_contract_triage(row: dict[str, Any]) -> dict[str, Any]:
    score = _float_value(row.get("review_score"), default=50.0)
    reasons: list[str] = []
    grade = str(row.get("saved_contract_grade") or "").upper()
    quote_status = str(row.get("quote_status") or "not_checked")
    review_action = str(row.get("review_action") or "")
    dte = _float_value(row.get("dte"), default=math.nan)
    spread = _float_value(row.get("current_spread_pct"), default=math.nan)
    if not math.isfinite(spread):
        spread = _float_value(row.get("saved_spread_pct"), default=math.nan)
    saved_quality = _float_value(row.get("saved_contract_quality_score"), default=math.nan)

    if grade == "A":
        score += 8
        reasons.append("A-grade chain save")
    elif grade == "B":
        score += 4
        reasons.append("B-grade chain save")
    elif grade == "D":
        score -= 10
        reasons.append("D-grade save")

    if quote_status == "matched":
        score += 6
        reasons.append("quote refreshed")
    else:
        reasons.append("needs quote refresh")

    if math.isfinite(dte):
        if dte < 0:
            score = 0
            reasons.append("expired")
        elif dte < MIN_SWING_OPTION_DTE:
            score -= 25
            reasons.append("below 90 DTE")
        elif dte >= 180:
            score += 3
            reasons.append("long-dated")
        else:
            reasons.append("3m+ swing")

    if math.isfinite(spread):
        if spread <= 0.10:
            score += 5
            reasons.append("tight spread")
        elif spread <= 0.20:
            reasons.append("acceptable spread")
        else:
            score -= 12
            reasons.append("wide spread")

    if math.isfinite(saved_quality) and saved_quality >= 80:
        score += 3
        reasons.append("strong saved quality")

    clean_score = max(0, min(100, int(round(score))))
    if math.isfinite(dte) and dte < 0:
        bucket = "expired"
        label = "Expired"
    elif quote_status != "matched":
        bucket = "refresh_quote"
        label = "Refresh Quote"
    elif review_action == "review_now" or clean_score >= 85:
        bucket = "ready_now"
        label = "Ready Review"
    elif clean_score >= 70:
        bucket = "shortlist"
        label = "Shortlist"
    elif clean_score >= 55:
        bucket = "watch"
        label = "Watch"
    else:
        bucket = "wait"
        label = "Wait"
    return {
        "triage_score": clean_score,
        "triage_bucket": bucket,
        "triage_label": label,
        "triage_reasons": reasons[:6],
    }


def build_saved_option_contracts(
    data_dir: Path = DATA_DIR,
    enrich: bool = True,
    limit: int = 80,
    refresh_quotes: bool = False,
    quote_limit: int = 20,
) -> dict[str, Any]:
    """Return saved option-request watchlist entries as a clean contract review queue."""
    limit = max(1, min(int(limit or 80), 250))
    quote_limit = max(0, min(int(quote_limit or 20), 80))
    watchlist = load_watchlist(data_dir, enrich=enrich)
    rows: list[dict[str, Any]] = []
    quote_checked_count = 0
    for entry in watchlist.get("entries", []):
        request = entry.get("request") if isinstance(entry, dict) else None
        if not isinstance(request, dict) or request.get("asset") != "option":
            continue
        chain_context = entry.get("chain_context") if isinstance(entry.get("chain_context"), dict) else {}
        dte = _request_dte(request.get("expiry"))
        side = str(request.get("side") or "").strip().lower()
        row = {
            "id": entry.get("id"),
            "query": entry.get("query"),
            "symbol": str(entry.get("symbol") or request.get("ticker") or "").upper(),
            "side": side,
            "side_code": "C" if side == "call" else "P" if side == "put" else None,
            "expiry": request.get("expiry"),
            "strike": _clean_value(request.get("strike")),
            "dte": dte,
            "dte_bucket": _option_dte_bucket(float(dte if dte is not None else -1)),
            "status": _saved_contract_status(dte, entry.get("paper_readiness_status")),
            "paper_readiness": _clean_value(entry.get("paper_readiness_label") or entry.get("paper_readiness_status")),
            "paper_readiness_score": _clean_value(entry.get("paper_readiness_score")),
            "best_idea": _clean_value(entry.get("best_idea")),
            "best_status": _clean_value(entry.get("best_status")),
            "best_confidence": _clean_value(entry.get("best_confidence")),
            "local_hits": _clean_value(entry.get("local_hits")),
            "open_count": _clean_value(entry.get("open_count")),
            "warning_count": _clean_value(entry.get("warning_count")),
            "added_at": entry.get("added_at"),
            "updated_at": entry.get("updated_at"),
            "saved_context_at": _clean_value(chain_context.get("saved_context_at")),
            "saved_contract_grade": _clean_value(chain_context.get("contract_grade")),
            "saved_review_lane": _clean_value(chain_context.get("review_lane")),
            "saved_review_thesis": _clean_value(chain_context.get("review_thesis")),
            "saved_grade_reasons": _clean_value(chain_context.get("grade_reasons")),
            "saved_readiness_label": _clean_value(chain_context.get("readiness_label")),
            "saved_readiness_score": _clean_value(chain_context.get("readiness_score")),
            "saved_contract_quality_score": _clean_value(chain_context.get("contract_quality_score")),
            "saved_chain_source": _clean_value(chain_context.get("chain_source") or chain_context.get("source")),
            "saved_quote_quality": _clean_value(chain_context.get("quote_quality")),
            "saved_data_delay": _clean_value(chain_context.get("data_delay")),
            "saved_mid": _clean_value(chain_context.get("mid")),
            "saved_bid": _clean_value(chain_context.get("bid")),
            "saved_ask": _clean_value(chain_context.get("ask")),
            "saved_spread_pct": _clean_value(chain_context.get("spread_pct")),
            "saved_premium_dollars": _clean_value(chain_context.get("premium_dollars")),
            "saved_volume": _clean_value(chain_context.get("volume")),
            "saved_open_interest": _clean_value(chain_context.get("openInterest")),
        }
        if refresh_quotes and quote_checked_count < quote_limit:
            row.update(_saved_contract_quote_snapshot(str(row.get("symbol") or ""), request, dte))
            quote_checked_count += 1
        else:
            row["quote_status"] = "not_checked" if not refresh_quotes else "not_checked_limit"
        row.update(_saved_contract_review(row))
        row.update(_saved_contract_triage(row))
        rows.append({k: _clean_value(v) for k, v in row.items()})

    rows = sorted(
        rows,
        key=lambda row: (
            _float_value(row.get("triage_score"), default=0.0),
            _saved_grade_rank(row.get("saved_contract_grade")),
            _float_value(row.get("dte"), default=-9999.0) >= MIN_SWING_OPTION_DTE,
            _float_value(row.get("paper_readiness_score"), default=0.0),
            _float_value(row.get("dte"), default=-9999.0),
            str(row.get("updated_at") or ""),
        ),
        reverse=True,
    )[:limit]
    status_counts: dict[str, int] = {}
    quote_status_counts: dict[str, int] = {}
    review_action_counts: dict[str, int] = {}
    saved_grade_counts: dict[str, int] = {}
    triage_counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        quote_status = str(row.get("quote_status") or "unknown")
        quote_status_counts[quote_status] = quote_status_counts.get(quote_status, 0) + 1
        review_action = str(row.get("review_action") or "unknown")
        review_action_counts[review_action] = review_action_counts.get(review_action, 0) + 1
        saved_grade = str(row.get("saved_contract_grade") or "ungraded")
        saved_grade_counts[saved_grade] = saved_grade_counts.get(saved_grade, 0) + 1
        triage_bucket = str(row.get("triage_bucket") or "unknown")
        triage_counts[triage_bucket] = triage_counts.get(triage_bucket, 0) + 1
    return {
        "generated_at": _now_iso(),
        "count": len(rows),
        "enriched": enrich,
        "refresh_quotes": refresh_quotes,
        "quote_limit": quote_limit,
        "quote_checked_count": quote_checked_count,
        "status_counts": status_counts,
        "quote_status_counts": quote_status_counts,
        "review_action_counts": review_action_counts,
        "saved_grade_counts": saved_grade_counts,
        "triage_counts": triage_counts,
        "call_count": sum(row.get("side") == "call" for row in rows),
        "put_count": sum(row.get("side") == "put" for row in rows),
        "swing_count": sum(_float_value(row.get("dte"), default=-1.0) >= MIN_SWING_OPTION_DTE for row in rows),
        "rows": rows,
        "notes": [
            "Saved contracts come from the local research watchlist option requests.",
            "3m+ status uses the current calendar date and Optedge's 90 DTE swing floor.",
            "Quote refresh uses the same free option-chain stack and may be delayed or incomplete.",
            "Use Chain to refresh the underlying option chain before acting; no trades are placed.",
        ],
    }


def _save_watchlist(entries: list[dict[str, Any]], data_dir: Path = DATA_DIR) -> None:
    path = _watchlist_file(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2, default=str), encoding="utf-8")


def add_watchlist_query(query: str, data_dir: Path = DATA_DIR, context: dict[str, Any] | None = None) -> dict[str, Any]:
    clean = str(query or "").strip()
    if not clean:
        return {"ok": False, "error": "query is required"}
    resolution = resolve_symbol(clean)
    if not resolution.get("symbol"):
        return {
            "ok": False,
            "error": resolution.get("error") or "could not resolve symbol",
            "resolution": resolution,
        }
    current = load_watchlist(data_dir)["entries"]
    item_id = _watchlist_entry_id(resolution, clean)
    now = _now_iso()
    chain_context = _clean_watchlist_context(context)
    entry = {
        "id": item_id,
        "query": clean,
        "symbol": str(resolution.get("symbol") or "").upper(),
        "name": resolution.get("name"),
        "source": resolution.get("source"),
        "request": resolution.get("request"),
        "resolution": resolution,
        "added_at": now,
        "updated_at": now,
    }
    if chain_context:
        entry["chain_context"] = chain_context
    replaced = False
    for idx, row in enumerate(current):
        if row.get("id") == item_id:
            entry["added_at"] = row.get("added_at") or now
            if not chain_context and isinstance(row.get("chain_context"), dict):
                entry["chain_context"] = row.get("chain_context")
            current[idx] = entry
            replaced = True
            break
    if not replaced:
        current.append(entry)
    _save_watchlist(current, data_dir)
    return {"ok": True, "entry": entry, "updated_existing": replaced, **load_watchlist(data_dir)}


def add_watchlist_queries(items: Any, data_dir: Path = DATA_DIR, limit: int = 12) -> dict[str, Any]:
    if not isinstance(items, list):
        return {"ok": False, "error": "items list is required"}
    limit = max(1, min(int(limit or 12), 25))
    saved: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    updated_existing = 0
    for item in items[:limit]:
        if isinstance(item, dict):
            query = str(item.get("query") or "").strip()
            context = item.get("context") if isinstance(item.get("context"), dict) else None
        else:
            query = str(item or "").strip()
            context = None
        if not query:
            errors.append({"query": query, "error": "query is required"})
            continue
        result = add_watchlist_query(query, data_dir, context=context)
        if result.get("ok"):
            entry = result.get("entry") or {}
            if result.get("updated_existing"):
                updated_existing += 1
            saved.append({
                "id": entry.get("id"),
                "query": entry.get("query"),
                "symbol": entry.get("symbol"),
                "updated_existing": bool(result.get("updated_existing")),
            })
        else:
            errors.append({
                "query": query,
                "error": result.get("error") or "could not save",
            })
    watchlist = load_watchlist(data_dir)
    return {
        "ok": bool(saved) and not errors,
        "saved_count": len(saved),
        "error_count": len(errors),
        "updated_existing_count": updated_existing,
        "saved": saved,
        "errors": errors,
        **watchlist,
    }


def remove_watchlist_entry(entry_id: str, data_dir: Path = DATA_DIR) -> dict[str, Any]:
    clean_id = str(entry_id or "").strip()
    current = load_watchlist(data_dir)["entries"]
    remaining = [row for row in current if str(row.get("id")) != clean_id]
    removed = len(remaining) != len(current)
    _save_watchlist(remaining, data_dir)
    return {"ok": removed, "removed": removed, **load_watchlist(data_dir)}


def _scan_args_from_controls(mode: str = "full", bankroll: Any = None,
                             aggressive: bool = False) -> list[str]:
    scan_args = ["--minimal"] if str(mode or "full").strip().lower() == "quick" else []
    if aggressive:
        scan_args.append("--aggressive")
    try:
        bankroll_float = float(bankroll or 0)
    except Exception:
        bankroll_float = 0.0
    if bankroll_float > 0:
        scan_args.extend(["--bankroll", str(bankroll_float)])
    return scan_args


def run_watchlist_scans(
    data_dir: Path = DATA_DIR,
    mode: str = "full",
    bankroll: Any = None,
    aggressive: bool = False,
    launch: bool = True,
) -> dict[str, Any]:
    entries = load_watchlist(data_dir)["entries"]
    scan_args = _scan_args_from_controls(mode, bankroll, aggressive)
    jobs = []
    for entry in entries[:25]:
        query = str(entry.get("query") or entry.get("symbol") or "").strip()
        if not query:
            continue
        jobs.append(create_job(
            query,
            data_dir,
            launch=launch,
            extra_scan_args=scan_args,
            scan_mode=str(mode or "full"),
        ))
    return {
        "ok": True,
        "count": len(jobs),
        "jobs": jobs,
        "scan_args": scan_args,
        "launched": launch,
    }


def warm_symbol_caches(data_dir: Path = DATA_DIR, timeout: float = 8.0) -> dict[str, Any]:
    """Warm free symbol-search caches used by company-name autocomplete."""
    cache_path = Path(data_dir) / "sec_company_tickers.json"
    nasdaq_path = Path(data_dir) / "nasdaq_symbol_directory.json"
    rows = load_sec_company_tickers(cache_path=cache_path, timeout=timeout, fetch_if_stale=True)
    nasdaq_rows = load_nasdaq_symbol_directory(
        cache_path=nasdaq_path,
        timeout=timeout,
        fetch_if_stale=True,
    )
    meta = sec_company_cache_meta(cache_path)
    nasdaq_meta = nasdaq_symbol_cache_meta(nasdaq_path)
    sec_ok = bool(rows) and meta.get("status") in {"fresh", "stale"}
    nasdaq_ok = bool(nasdaq_rows) and nasdaq_meta.get("status") in {"fresh", "stale"}
    ok = sec_ok or nasdaq_ok
    return {
        "ok": ok,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "row_count": len(rows),
        "nasdaq_row_count": len(nasdaq_rows),
        "cache": meta,
        "nasdaq_cache": nasdaq_meta,
        "message": (
            f"Symbol search caches ready: SEC {len(rows)} row(s), Nasdaq {len(nasdaq_rows)} row(s)."
            if ok else "Symbol search caches could not be warmed right now."
        ),
    }


def warm_sec_ticker_cache(data_dir: Path = DATA_DIR, timeout: float = 8.0) -> dict[str, Any]:
    """Backward-compatible wrapper for the broader symbol-cache warmer."""
    return warm_symbol_caches(data_dir, timeout=timeout)


def _is_refreshable_market_warning(label: Any) -> bool:
    clean = str(label or "").strip().lower()
    return (
        clean in {"dashboard is old", "dashboard missing"}
        or clean.endswith("snapshot old")
        or clean.endswith("snapshot missing")
    )


def _position_label(row: dict[str, Any]) -> str:
    symbol = str(row.get("ticker") or row.get("symbol") or "-").upper()
    side = str(row.get("side") or row.get("direction") or "").upper()
    strike = row.get("strike")
    expiry = str(row.get("expiry") or "")
    contract = str(row.get("contract") or "")
    if strike not in (None, "", "-") and expiry:
        try:
            strike_txt = f"{float(strike):g}"
        except Exception:
            strike_txt = str(strike)
        return f"{symbol} {side[:1]} {strike_txt} {expiry[-5:]}"
    if contract:
        return f"{symbol} {side} {contract}".strip()
    return f"{symbol} {side}".strip()


def _position_age_days(row: dict[str, Any]) -> float:
    if row.get("age_days") is not None:
        return _float_value(row.get("age_days"))
    entry = row.get("entry_time")
    if not entry:
        return 0.0
    try:
        ts = pd.to_datetime(entry, errors="coerce", utc=True)
        if pd.isna(ts):
            return 0.0
        return max(0.0, float((pd.Timestamp.now(tz="UTC") - ts).total_seconds() / 86400.0))
    except Exception:
        return 0.0


def _position_pnl_pct(row: dict[str, Any]) -> float:
    for col in ("unrealized_pct", "current_pnl_pct", "pnl_pct"):
        if row.get(col) is not None:
            return _float_value(row.get(col))
    current = row.get("current_mid", row.get("current_price"))
    if current is None:
        current = row.get("last_price")
    entry = _float_value(row.get("entry_price"))
    cur = _float_value(current)
    return (cur - entry) / entry if entry > 0 and cur > 0 else 0.0


def _normalize_position(row: dict[str, Any], asset: str) -> dict[str, Any]:
    out = dict(row)
    out.setdefault("asset", asset)
    symbol = out.get("ticker") or out.get("symbol") or "-"
    current_price = out.get("current_mid", out.get("current_price", out.get("last_price")))
    pnl_pct = _position_pnl_pct(out)
    exit_pressure = _float_value(out.get("latest_exit_pressure"), default=0.0)
    reprice_failed = _float_value(out.get("reprice_failed_count"), default=0.0)
    attention = exit_pressure >= 60 or reprice_failed >= 2 or pnl_pct <= -0.30
    return {
        "asset": out.get("asset"),
        "position_label": _position_label(out),
        "ticker_or_symbol": str(symbol).upper(),
        "side_or_direction": out.get("side") or out.get("direction") or out.get("asset"),
        "strike_or_contract": out.get("strike", out.get("contract")),
        "expiry": out.get("expiry"),
        "trade_status": out.get("trade_status") or "Open",
        "entry_time": out.get("entry_time"),
        "age_days": round(_position_age_days(out), 2),
        "entry_price": _clean_value(out.get("entry_price")),
        "current_price": _clean_value(current_price),
        "pnl_pct": pnl_pct,
        "pnl_dollars": _clean_value(out.get("pnl_dollars")),
        "confidence": _clean_value(out.get("confidence")),
        "latest_exit_pressure": _clean_value(out.get("latest_exit_pressure")),
        "latest_exit_action": out.get("latest_exit_action"),
        "stop_price": _clean_value(out.get("stop_price")),
        "target_price": _clean_value(out.get("target_price")),
        "reprice_failed_count": _clean_value(out.get("reprice_failed_count")),
        "research_guard_status": out.get("research_guard_status"),
        "attention": attention,
    }


def _position_sort_key(row: dict[str, Any]) -> tuple:
    return (
        1 if row.get("attention") else 0,
        _float_value(row.get("latest_exit_pressure")),
        abs(_float_value(row.get("pnl_pct"))),
        _float_value(row.get("age_days")),
    )


def _opportunity_score(row: pd.Series) -> float:
    for col in ("rank_score", "fused_score", "futures_score", "value_score"):
        if col in row:
            score = _float_value(row.get(col), default=math.nan)
            if math.isfinite(score):
                return score
    return _float_value(row.get("confidence"), default=0.0) / 100.0


def _fetch_option_chain(
    ticker: str,
    cache_age: int = 600,
    include_diagnostics: bool = False,
) -> dict[str, Any]:
    import chain_provider

    return chain_provider.fetch_chain(
        ticker,
        cache_age=cache_age,
        include_diagnostics=include_diagnostics,
    )


def _option_mid(row: pd.Series) -> float:
    bid = _float_value(row.get("bid"), default=math.nan)
    ask = _float_value(row.get("ask"), default=math.nan)
    last = _float_value(row.get("lastPrice"), default=math.nan)
    if math.isfinite(bid) and math.isfinite(ask) and bid > 0 and ask >= bid:
        return (bid + ask) / 2.0
    if math.isfinite(last) and last > 0:
        return last
    return float("nan")


def _option_spread_pct(row: pd.Series, mid: float) -> float | None:
    bid = _float_value(row.get("bid"), default=math.nan)
    ask = _float_value(row.get("ask"), default=math.nan)
    if not (math.isfinite(bid) and math.isfinite(ask) and math.isfinite(mid)):
        return None
    if bid < 0 or ask <= 0 or ask < bid or mid <= 0:
        return None
    return (ask - bid) / mid


def _option_chain_trade_refs(row: dict[str, Any], spot: float, max_premium: float) -> dict[str, Any]:
    """Build conservative one-contract review references for chain candidates."""
    side = str(row.get("side") or "").strip().lower()
    strike = _float_value(row.get("strike"), default=math.nan)
    mid = _float_value(row.get("mid"), default=math.nan)
    premium = _float_value(row.get("premium_dollars"), default=math.nan)
    if not math.isfinite(premium) and math.isfinite(mid):
        premium = mid * 100.0
    budget = max_premium if max_premium > 0 else 500.0

    out: dict[str, Any] = {
        "breakeven_price": None,
        "breakeven_move_pct": None,
        "breakeven_direction": "",
        "budget_usage_pct": None,
        "contracts_for_budget": 0,
        "stop_price_reference": None,
        "target_price_reference": None,
        "risk_dollars_reference": None,
        "reward_dollars_reference": None,
        "reward_risk_reference": None,
        "budget_fit": "unknown",
    }
    if math.isfinite(premium) and premium > 0 and budget > 0:
        usage = premium / budget
        out["budget_usage_pct"] = round(usage, 4)
        out["contracts_for_budget"] = int(max(0, math.floor(budget / premium)))
        if usage <= 1.0:
            out["budget_fit"] = "inside_budget"
        elif usage <= 1.25:
            out["budget_fit"] = "stretch"
        else:
            out["budget_fit"] = "over_budget"

    if math.isfinite(mid) and mid > 0:
        stop = max(0.01, mid * 0.50)
        target = mid * 2.00
        risk = max(0.0, (mid - stop) * 100.0)
        reward = max(0.0, (target - mid) * 100.0)
        out["stop_price_reference"] = round(stop, 2)
        out["target_price_reference"] = round(target, 2)
        out["risk_dollars_reference"] = round(risk, 2)
        out["reward_dollars_reference"] = round(reward, 2)
        out["reward_risk_reference"] = round(reward / risk, 2) if risk > 0 else None

    if math.isfinite(strike) and math.isfinite(mid) and mid > 0:
        if side.startswith("call"):
            breakeven = strike + mid
            out["breakeven_direction"] = "up"
            if math.isfinite(spot) and spot > 0:
                out["breakeven_move_pct"] = round((breakeven - spot) / spot, 4)
        elif side.startswith("put"):
            breakeven = max(0.0, strike - mid)
            out["breakeven_direction"] = "down"
            if math.isfinite(spot) and spot > 0:
                out["breakeven_move_pct"] = round((spot - breakeven) / spot, 4)
        else:
            breakeven = float("nan")
        if math.isfinite(breakeven):
            out["breakeven_price"] = round(breakeven, 4)

    return out


def _option_chain_score(row: dict[str, Any]) -> float:
    oi = _float_value(row.get("openInterest"), default=0.0)
    volume = _float_value(row.get("volume"), default=0.0)
    spread = _float_value(row.get("spread_pct"), default=0.50)
    premium = _float_value(row.get("premium_dollars"), default=0.0)
    moneyness = abs(_float_value(row.get("moneyness_pct"), default=0.0))
    return (
        math.log1p(max(0.0, oi))
        + 0.65 * math.log1p(max(0.0, volume))
        - 8.0 * max(0.0, spread)
        - 0.04 * moneyness
        - 0.0002 * max(0.0, premium)
    )


def _option_dte_bucket(dte: float) -> str:
    if dte < 30:
        return "0-29d"
    if dte < MIN_SWING_OPTION_DTE:
        return "30-89d"
    if dte < 180:
        return "90-179d"
    if dte < 365:
        return "180-364d"
    return "365d+"


def _option_contract_readiness(row: dict[str, Any], quote_quality: str) -> dict[str, Any]:
    score = 100
    flags: list[str] = []
    dte = _float_value(row.get("dte"), default=math.nan)
    spread = _float_value(row.get("spread_pct"), default=math.nan)
    oi = _float_value(row.get("openInterest"), default=0.0)
    volume = _float_value(row.get("volume"), default=0.0)
    premium = _float_value(row.get("premium_dollars"), default=math.nan)
    moneyness = abs(_float_value(row.get("moneyness_pct"), default=0.0))
    quote_norm = str(quote_quality or "").lower()

    if not math.isfinite(dte) or dte < MIN_SWING_OPTION_DTE:
        score -= 35
        flags.append("below 90 DTE")
    elif dte > 900:
        score -= 8
        flags.append("very far expiry")
    if math.isfinite(spread):
        if spread > 0.30:
            score -= 35
            flags.append("very wide spread")
        elif spread > 0.20:
            score -= 22
            flags.append("wide spread")
        elif spread > 0.12:
            score -= 8
            flags.append("spread check")
    else:
        score -= 14
        flags.append("missing spread")
    if oi < 25:
        score -= 18
        flags.append("thin open interest")
    elif oi < 100:
        score -= 8
        flags.append("light open interest")
    if volume <= 0:
        score -= 8
        flags.append("no volume today")
    if not math.isfinite(premium) or premium <= 0:
        score -= 30
        flags.append("invalid premium")
    elif premium > 1000:
        score -= 8
        flags.append("large premium")
    if moneyness > 0.35:
        score -= 8
        flags.append("far from spot")
    if quote_norm in {"", "unknown"}:
        score -= 8
        flags.append("unknown quote source")
    elif "free" in quote_norm or "delayed" in quote_norm:
        score -= 6
        flags.append("verify live quote")

    return _readiness(score, flags)


def _option_chain_swing_fit(row: dict[str, Any], max_premium: float) -> dict[str, Any]:
    """Score whether a chain row is usable for 3m+ swing review."""
    score = 100
    reasons: list[str] = []
    warnings: list[str] = []
    dte = _float_value(row.get("dte"), default=math.nan)
    spread = _float_value(row.get("spread_pct"), default=math.nan)
    oi = _float_value(row.get("openInterest"), default=0.0)
    volume = _float_value(row.get("volume"), default=0.0)
    premium = _float_value(row.get("premium_dollars"), default=math.nan)
    budget_usage = _float_value(row.get("budget_usage_pct"), default=math.nan)
    breakeven_move = _float_value(row.get("breakeven_move_pct"), default=math.nan)
    delta = abs(_float_value(row.get("delta"), default=math.nan))
    quote_norm = str(row.get("quote_quality") or "").lower()
    budget = max_premium if max_premium > 0 else 500.0

    if not math.isfinite(dte):
        score -= 24
        warnings.append("unknown DTE")
    elif dte < MIN_SWING_OPTION_DTE:
        score -= 40
        warnings.append("below 3m swing minimum")
    elif dte < 180:
        reasons.append("3m+ runway")
    elif dte <= 540:
        reasons.append("long swing runway")
    elif dte <= 900:
        score -= 4
        reasons.append("LEAPS-style runway")
    else:
        score -= 12
        warnings.append("very far-dated")

    if math.isfinite(spread):
        if spread <= 0.08:
            reasons.append("tight spread")
        elif spread <= 0.15:
            reasons.append("acceptable spread")
        elif spread <= 0.25:
            score -= 14
            warnings.append("spread needs live check")
        else:
            score -= 32
            warnings.append("spread too wide")
    else:
        score -= 18
        warnings.append("missing spread")

    if oi >= 500:
        reasons.append("deep open interest")
        liquidity_label = "deep"
    elif oi >= 100:
        reasons.append("usable open interest")
        liquidity_label = "usable"
    elif oi >= 25:
        score -= 8
        warnings.append("light open interest")
        liquidity_label = "light"
    else:
        score -= 24
        warnings.append("thin open interest")
        liquidity_label = "thin"
    if volume > 0:
        reasons.append("traded today")
    else:
        score -= 6
        warnings.append("no same-day volume")

    if not math.isfinite(premium) or premium <= 0:
        score -= 35
        warnings.append("invalid premium")
    elif premium <= budget:
        if math.isfinite(budget_usage) and budget_usage <= 0.50:
            reasons.append("small budget usage")
        else:
            reasons.append("inside premium budget")
    elif premium <= budget * 1.25:
        score -= 12
        warnings.append("premium near budget cap")
    else:
        score -= 28
        warnings.append("premium above budget cap")

    breakeven_label = "unknown"
    if math.isfinite(breakeven_move):
        move_abs = abs(breakeven_move)
        if move_abs <= 0.08:
            breakeven_label = "light"
            reasons.append("light break-even move")
        elif move_abs <= 0.15:
            breakeven_label = "moderate"
            score -= 5
            reasons.append("moderate break-even move")
        elif move_abs <= 0.25:
            breakeven_label = "heavy"
            score -= 14
            warnings.append("heavy break-even move")
        else:
            breakeven_label = "speculative"
            score -= 28
            warnings.append("speculative break-even move")
    else:
        score -= 8
        warnings.append("unknown break-even")

    if math.isfinite(delta):
        if 0.25 <= delta <= 0.65:
            reasons.append("swing delta zone")
        elif delta < 0.15:
            score -= 10
            warnings.append("low-delta lottery profile")
        elif delta > 0.85:
            score -= 4
            warnings.append("stock-like high delta")

    if quote_norm in {"", "unknown"}:
        score -= 8
        warnings.append("unknown quote source")
    elif "free" in quote_norm or "delayed" in quote_norm:
        score -= 4
        warnings.append("verify delayed quote")

    clean_score = max(0, min(100, int(round(score))))
    if clean_score >= 85 and len(warnings) <= 1:
        label = "clean_swing"
    elif clean_score >= 70:
        label = "reviewable_swing"
    elif clean_score >= 55:
        label = "speculative_swing"
    else:
        label = "avoid"

    return {
        "swing_fit_score": clean_score,
        "swing_fit_label": label,
        "swing_fit_reasons": reasons[:6],
        "swing_fit_warnings": warnings[:6],
        "breakeven_move_label": breakeven_label,
        "liquidity_label": liquidity_label,
    }


def _option_contract_grade(row: dict[str, Any], max_premium: float) -> dict[str, Any]:
    readiness = _float_value(row.get("readiness_score"), default=0.0)
    spread = _float_value(row.get("spread_pct"), default=math.nan)
    oi = _float_value(row.get("openInterest"), default=0.0)
    volume = _float_value(row.get("volume"), default=0.0)
    premium = _float_value(row.get("premium_dollars"), default=math.nan)
    dte = _float_value(row.get("dte"), default=math.nan)
    side = str(row.get("side") or "").lower()
    budget = max_premium if max_premium > 0 else 500.0
    reasons: list[str] = []

    tight_spread = math.isfinite(spread) and spread <= 0.12
    acceptable_spread = math.isfinite(spread) and spread <= 0.20
    liquid = oi >= 100
    tradable_oi = oi >= 25
    under_budget = math.isfinite(premium) and premium <= budget
    swing_dte = math.isfinite(dte) and dte >= MIN_SWING_OPTION_DTE
    long_dated = math.isfinite(dte) and dte >= 180

    if tight_spread:
        reasons.append("tight spread")
    elif acceptable_spread:
        reasons.append("acceptable spread")
    elif math.isfinite(spread):
        reasons.append("spread needs work")
    if liquid:
        reasons.append("100+ OI")
    elif tradable_oi:
        reasons.append("25+ OI")
    else:
        reasons.append("thin OI")
    if volume > 0:
        reasons.append("traded today")
    if under_budget:
        reasons.append("inside premium budget")
    elif math.isfinite(premium):
        reasons.append("above premium budget")
    if long_dated:
        reasons.append("long-dated swing")
    elif swing_dte:
        reasons.append("3m+ swing")

    if readiness >= 80 and tight_spread and liquid and under_budget and swing_dte:
        grade = "A"
        lane = "primary_review"
    elif readiness >= 70 and acceptable_spread and tradable_oi and under_budget and swing_dte:
        grade = "B"
        lane = "secondary_review"
    elif readiness >= 65 and swing_dte:
        grade = "C"
        lane = "long_dated_review" if long_dated else "secondary_review"
    else:
        grade = "D"
        lane = "wait"

    side_text = "call" if side.startswith("call") else "put" if side.startswith("put") else "contract"
    dte_text = f"{int(dte)} DTE" if math.isfinite(dte) else "unknown DTE"
    premium_text = f"${premium:.0f} premium" if math.isfinite(premium) else "unknown premium"
    spread_text = f"{spread * 100:.1f}% spread" if math.isfinite(spread) else "unknown spread"
    break_even_move = _float_value(row.get("breakeven_move_pct"), default=math.nan)
    break_even_text = (
        f"{break_even_move * 100:.1f}% break-even move"
        if math.isfinite(break_even_move) else "unknown break-even"
    )
    budget_fit = str(row.get("budget_fit") or "").replace("_", " ")
    thesis = (
        f"{grade}-grade {dte_text} {side_text}: {premium_text}, "
        f"{spread_text}, {break_even_text}, OI {int(oi)}. "
        f"{budget_fit or 'budget fit unknown'}. "
        f"{'; '.join(reasons[:4]) if reasons else 'Review exact quote before acting.'}"
    )
    return {
        "contract_grade": grade,
        "review_lane": lane,
        "review_thesis": thesis,
        "grade_reasons": reasons[:6],
    }


def _option_chain_factor_breakdown(row: dict[str, Any], max_premium: float) -> list[dict[str, Any]]:
    budget = max_premium if max_premium > 0 else 500.0
    dte = _float_value(row.get("dte"), default=math.nan)
    spread = _float_value(row.get("spread_pct"), default=math.nan)
    oi = _float_value(row.get("openInterest"), default=0.0)
    volume = _float_value(row.get("volume"), default=0.0)
    premium = _float_value(row.get("premium_dollars"), default=math.nan)
    budget_usage = _float_value(row.get("budget_usage_pct"), default=math.nan)
    breakeven_move = abs(_float_value(row.get("breakeven_move_pct"), default=math.nan))
    swing_fit = _float_value(row.get("swing_fit_score"), default=0.0)
    quality = _float_value(row.get("contract_quality_score"), default=0.0)
    factors: list[dict[str, Any]] = []

    def add(factor: str, score: float, detail: str) -> None:
        if not math.isfinite(score):
            return
        factors.append({
            "factor": factor,
            "score": int(round(_clamp(score, 0.0, 100.0))),
            "detail": detail[:160],
        })

    if math.isfinite(dte):
        if dte < MIN_SWING_OPTION_DTE:
            runway_score = 35.0
        elif dte < 180:
            runway_score = 82.0
        elif dte <= 540:
            runway_score = 94.0
        elif dte <= 900:
            runway_score = 80.0
        else:
            runway_score = 55.0
        add("Runway", runway_score, f"{int(dte)} DTE, {row.get('dte_bucket') or _option_dte_bucket(dte)}")

    liquidity_score = min(100.0, 28.0 + 12.0 * math.log1p(max(0.0, oi)) + 4.0 * math.log1p(max(0.0, volume)))
    add("Liquidity", liquidity_score, f"OI {int(oi):,}, volume {int(volume):,}")

    if math.isfinite(spread):
        if spread <= 0.08:
            spread_score = 96.0
        elif spread <= 0.12:
            spread_score = 86.0
        elif spread <= 0.20:
            spread_score = 68.0
        elif spread <= 0.30:
            spread_score = 45.0
        else:
            spread_score = 18.0
        add("Spread", spread_score, f"{spread * 100:.1f}% bid/ask spread")

    if math.isfinite(premium):
        if math.isfinite(budget_usage):
            usage_text = f"{budget_usage * 100:.0f}% of ${budget:,.0f} cap"
        else:
            usage_text = f"${premium:,.0f} premium vs ${budget:,.0f} cap"
        if premium <= budget * 0.50:
            budget_score = 96.0
        elif premium <= budget:
            budget_score = 84.0
        elif premium <= budget * 1.25:
            budget_score = 58.0
        else:
            budget_score = 24.0
        add("Budget", budget_score, usage_text)

    if math.isfinite(breakeven_move):
        if breakeven_move <= 0.08:
            breakeven_score = 92.0
        elif breakeven_move <= 0.15:
            breakeven_score = 76.0
        elif breakeven_move <= 0.25:
            breakeven_score = 50.0
        else:
            breakeven_score = 20.0
        label = str(row.get("breakeven_move_label") or "").replace("_", " ") or "break-even"
        add("Break-even", breakeven_score, f"{breakeven_move * 100:.1f}% move, {label}")

    add("Swing fit", swing_fit, f"{str(row.get('swing_fit_label') or 'unscored').replace('_', ' ')} / {int(round(swing_fit))}")
    if math.isfinite(quality):
        add("Quality", min(100.0, max(0.0, 50.0 + quality * 5.0)), f"quality score {quality:.1f}")

    return sorted(
        factors,
        key=lambda item: _float_value(item.get("score"), default=0.0),
        reverse=True,
    )


def _chain_preset_config(preset: str) -> tuple[str, dict[str, Any]]:
    preset_norm = str(preset or "custom").strip().lower().replace("-", "_")
    if preset_norm in {"long", "long_dated", "leap"}:
        preset_norm = "leaps"
    if preset_norm not in CHAIN_PRESETS:
        preset_norm = "custom"
    return preset_norm, CHAIN_PRESETS[preset_norm]


def _chain_contract_label(row: dict[str, Any] | None) -> str | None:
    if not row:
        return None
    side = str(row.get("side") or "").upper()[:1]
    strike = _short_number(row.get("strike"))
    expiry = str(row.get("expiry") or "").strip()
    premium = _float_value(row.get("premium_dollars"), default=math.nan)
    premium_text = f" ${premium:.0f}" if math.isfinite(premium) else ""
    return " ".join(part for part in (side, strike, expiry + premium_text) if part)


def _option_grade_rank(value: Any) -> int:
    return {"A": 4, "B": 3, "C": 2, "D": 1}.get(str(value or "").upper(), 0)


def _option_contract_rank_tuple(row: dict[str, Any], budget: float) -> tuple[Any, ...]:
    premium = _float_value(row.get("premium_dollars"), default=math.inf)
    spread = _float_value(row.get("spread_pct"), default=9.0)
    return (
        premium <= budget,
        _option_grade_rank(row.get("contract_grade")),
        str(row.get("review_lane") or "") == "primary_review",
        _float_value(row.get("swing_fit_score"), default=0.0),
        _float_value(row.get("contract_quality_score"), default=0.0),
        _float_value(row.get("openInterest"), default=0.0),
        -spread,
    )


def _option_chain_scan_summary(rows: list[dict[str, Any]], max_premium: float) -> dict[str, Any]:
    if not rows:
        return {
            "best_call": None,
            "best_put": None,
            "best_reviewable": None,
            "best_ready": None,
            "best_budget": None,
            "best_liquid": None,
            "best_long_dated": None,
            "grade_counts": {},
            "primary_review_count": 0,
            "median_spread_pct": None,
            "under_budget_count": 0,
            "liquid_count": 0,
            "swing_count": 0,
            "long_dated_count": 0,
            "ready_count": 0,
            "review_count": 0,
            "wait_count": 0,
            "swing_fit_counts": {},
            "clean_swing_count": 0,
            "reviewable_swing_count": 0,
            "best_swing_fit": None,
        }
    spreads = sorted(
        _float_value(row.get("spread_pct"), default=math.nan)
        for row in rows
        if math.isfinite(_float_value(row.get("spread_pct"), default=math.nan))
    )
    mid_idx = len(spreads) // 2
    median_spread = None
    if spreads:
        median_spread = spreads[mid_idx] if len(spreads) % 2 else (spreads[mid_idx - 1] + spreads[mid_idx]) / 2.0
    best_call = next((row for row in rows if str(row.get("side") or "").lower().startswith("call")), None)
    best_put = next((row for row in rows if str(row.get("side") or "").lower().startswith("put")), None)
    best_reviewable = next(
        (row for row in rows if str(row.get("readiness_label") or "") in {"ready", "review"}),
        None,
    )
    budget = max_premium if max_premium > 0 else 500.0
    best_ready = next((row for row in rows if str(row.get("readiness_label") or "") == "ready"), None)
    best_budget = next(
        (row for row in rows if _float_value(row.get("premium_dollars"), default=math.inf) <= budget),
        None,
    )
    best_liquid = next(
        (
            row for row in rows
            if _float_value(row.get("openInterest"), default=0.0) >= 100
            and _float_value(row.get("spread_pct"), default=1.0) <= 0.15
        ),
        None,
    )
    best_long_dated = next((row for row in rows if _float_value(row.get("dte"), default=0.0) >= 180), None)
    grade_counts: dict[str, int] = {}
    swing_fit_counts: dict[str, int] = {}
    for row in rows:
        grade = str(row.get("contract_grade") or "ungraded")
        grade_counts[grade] = grade_counts.get(grade, 0) + 1
        fit = str(row.get("swing_fit_label") or "unscored")
        swing_fit_counts[fit] = swing_fit_counts.get(fit, 0) + 1
    return {
        "best_call": _chain_contract_label(best_call),
        "best_put": _chain_contract_label(best_put),
        "best_reviewable": _chain_contract_label(best_reviewable),
        "best_ready": _chain_contract_label(best_ready),
        "best_budget": _chain_contract_label(best_budget),
        "best_liquid": _chain_contract_label(best_liquid),
        "best_long_dated": _chain_contract_label(best_long_dated),
        "grade_counts": grade_counts,
        "primary_review_count": sum(str(row.get("review_lane") or "") == "primary_review" for row in rows),
        "median_spread_pct": _clean_value(median_spread),
        "under_budget_count": sum(_float_value(row.get("premium_dollars"), default=math.inf) <= budget for row in rows),
        "liquid_count": sum(
            _float_value(row.get("openInterest"), default=0.0) >= 100
            and _float_value(row.get("spread_pct"), default=1.0) <= 0.15
            for row in rows
        ),
        "swing_count": sum(MIN_SWING_OPTION_DTE <= _float_value(row.get("dte"), default=-1.0) <= 180 for row in rows),
        "long_dated_count": sum(_float_value(row.get("dte"), default=0.0) >= 180 for row in rows),
        "ready_count": sum(str(row.get("readiness_label") or "") == "ready" for row in rows),
        "review_count": sum(str(row.get("readiness_label") or "") == "review" for row in rows),
        "wait_count": sum(str(row.get("readiness_label") or "") == "wait" for row in rows),
        "swing_fit_counts": swing_fit_counts,
        "clean_swing_count": swing_fit_counts.get("clean_swing", 0),
        "reviewable_swing_count": swing_fit_counts.get("reviewable_swing", 0),
        "best_swing_fit": _chain_contract_label(max(
            rows,
            key=lambda row: _float_value(row.get("swing_fit_score"), default=-1.0),
        )),
    }


def _option_chain_expiry_summary(rows: list[dict[str, Any]], max_premium: float = 0.0) -> list[dict[str, Any]]:
    by_expiry: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_expiry.setdefault(str(row.get("expiry") or ""), []).append(row)

    summaries: list[dict[str, Any]] = []
    budget = max_premium if max_premium > 0 else 500.0
    for expiry, items in by_expiry.items():
        if not expiry:
            continue
        spreads = sorted(
            _float_value(row.get("spread_pct"), default=math.nan)
            for row in items
            if math.isfinite(_float_value(row.get("spread_pct"), default=math.nan))
        )
        mid_idx = len(spreads) // 2
        median_spread = None
        if spreads:
            median_spread = spreads[mid_idx] if len(spreads) % 2 else (spreads[mid_idx - 1] + spreads[mid_idx]) / 2.0
        dte_values = [_float_value(row.get("dte"), default=math.nan) for row in items]
        dte_values = [value for value in dte_values if math.isfinite(value)]
        dte = int(min(dte_values)) if dte_values else None
        best_call = next((row for row in items if str(row.get("side") or "") == "call"), None)
        best_put = next((row for row in items if str(row.get("side") or "") == "put"), None)
        under_budget = [
            row for row in items
            if _float_value(row.get("premium_dollars"), default=math.inf) <= budget
        ]
        best_budget = max(under_budget or items, key=lambda row: _option_contract_rank_tuple(row, budget))
        best_budget_premium = _float_value(best_budget.get("premium_dollars"), default=math.nan)
        summaries.append({
            "expiry": expiry,
            "dte": dte,
            "dte_bucket": _option_dte_bucket(float(dte or 0)),
            "contracts": len(items),
            "calls": sum(str(row.get("side") or "") == "call" for row in items),
            "puts": sum(str(row.get("side") or "") == "put" for row in items),
            "median_spread_pct": _clean_value(median_spread),
            "under_budget_count": len(under_budget),
            "reviewable_count": sum(str(row.get("readiness_label") or "") in {"ready", "review"} for row in items),
            "primary_review_count": sum(str(row.get("review_lane") or "") == "primary_review" for row in items),
            "clean_swing_count": sum(str(row.get("swing_fit_label") or "") == "clean_swing" for row in items),
            "liquid_count": sum(
                _float_value(row.get("openInterest"), default=0.0) >= 100
                and _float_value(row.get("spread_pct"), default=1.0) <= 0.15
                for row in items
            ),
            "best_call": _chain_contract_label(best_call),
            "best_put": _chain_contract_label(best_put),
            "best_budget": _chain_contract_label(best_budget),
            "best_budget_grade": _clean_value(best_budget.get("contract_grade")),
            "best_budget_fit": _clean_value(best_budget.get("budget_fit")),
            "best_budget_premium": _clean_value(round(best_budget_premium, 2) if math.isfinite(best_budget_premium) else None),
            "best_budget_spread_pct": _clean_value(best_budget.get("spread_pct")),
            "best_budget_score": _clean_value(best_budget.get("swing_fit_score") or best_budget.get("contract_quality_score")),
        })
    return sorted(
        summaries,
        key=lambda row: (
            _float_value(row.get("under_budget_count"), default=0.0) > 0,
            _float_value(row.get("primary_review_count"), default=0.0),
            _float_value(row.get("clean_swing_count"), default=0.0),
            _option_grade_rank(row.get("best_budget_grade")),
            -_float_value(row.get("best_budget_spread_pct"), default=9.0),
            -_float_value(row.get("dte"), default=9999.0),
        ),
        reverse=True,
    )[:12]


def _option_chain_trade_plan(
    primary: dict[str, Any],
    status: str,
    risk_notes: list[str],
) -> dict[str, Any]:
    side = str(primary.get("side") or "").strip().lower()
    mid = _float_value(primary.get("mid"), default=math.nan)
    premium = _float_value(primary.get("premium_dollars"), default=math.nan)
    contracts_for_budget = int(_float_value(primary.get("contracts_for_budget"), default=0.0))
    quantity = 1 if contracts_for_budget >= 1 and math.isfinite(mid) and mid > 0 else 0
    action = "review_contract" if status in {"primary_review", "secondary_review"} and quantity > 0 else "watch_only"
    contract = str(primary.get("contract_query") or "").strip()
    side_label = "call" if side.startswith("call") else "put" if side.startswith("put") else "option"
    entry_text = f"{mid:.2f}" if math.isfinite(mid) else "unknown"
    premium_text = f"${premium:.0f}" if math.isfinite(premium) else "unknown premium"
    checklist = [
        "Refresh live bid/ask before any paper/manual entry.",
        "Skip if spread widens beyond the selected filter.",
        "Confirm no same-symbol duplicate exposure.",
        "Check current news and earnings timing.",
    ]
    if risk_notes:
        checklist.append("Resolve listed risk notes before acting.")
    summary = (
        f"{quantity} contract {side_label} review at reference entry {entry_text}; "
        f"premium reference {premium_text}."
        if quantity > 0
        else "No budget-safe contract quantity under the current premium limit."
    )
    return {
        "action": action,
        "contract": contract,
        "side": side_label,
        "quantity": quantity,
        "entry_price_reference": _clean_value(mid),
        "premium_dollars_reference": _clean_value(premium),
        "max_loss_dollars_reference": _clean_value(premium),
        "stop_price_reference": _clean_value(primary.get("stop_price_reference")),
        "target_price_reference": _clean_value(primary.get("target_price_reference")),
        "stop_loss_dollars_reference": _clean_value(primary.get("risk_dollars_reference")),
        "target_gain_dollars_reference": _clean_value(primary.get("reward_dollars_reference")),
        "reward_risk_reference": _clean_value(primary.get("reward_risk_reference")),
        "breakeven_price": _clean_value(primary.get("breakeven_price")),
        "breakeven_move_pct": _clean_value(primary.get("breakeven_move_pct")),
        "budget_fit": _clean_value(primary.get("budget_fit")),
        "budget_usage_pct": _clean_value(primary.get("budget_usage_pct")),
        "contracts_for_budget": contracts_for_budget,
        "summary": summary,
        "checklist": checklist,
    }


def _open_exposure_for_symbol(data_dir: Path, symbol: str) -> dict[str, Any]:
    clean = str(symbol or "").strip().upper()
    if not clean:
        return {"symbol": clean, "has_open": False, "open_count": 0, "asset_counts": {}, "labels": []}
    try:
        report = build_positions(data_dir, asset="all", query=clean, status="all", limit=500)
    except Exception:
        return {"symbol": clean, "has_open": False, "open_count": 0, "asset_counts": {}, "labels": []}
    rows = [
        row for row in (report.get("rows") or [])
        if str(row.get("ticker_or_symbol") or "").upper() == clean
    ]
    asset_counts: dict[str, int] = {}
    labels: list[str] = []
    attention_count = 0
    for row in rows:
        asset = str(row.get("asset") or "unknown")
        asset_counts[asset] = asset_counts.get(asset, 0) + 1
        if row.get("attention"):
            attention_count += 1
        label = str(row.get("position_label") or "").strip()
        if label and label not in labels:
            labels.append(label)
    return {
        "symbol": clean,
        "has_open": bool(rows),
        "open_count": len(rows),
        "asset_counts": asset_counts,
        "attention_count": attention_count,
        "labels": labels[:5],
        "summary": (
            f"{len(rows)} open {clean} position(s)"
            if rows else f"No open {clean} positions found"
        ),
    }


def _chain_open_exposure_fields(exposure: dict[str, Any] | None) -> dict[str, Any]:
    exposure = exposure if isinstance(exposure, dict) else {}
    asset_counts = exposure.get("asset_counts") if isinstance(exposure.get("asset_counts"), dict) else {}
    return {
        "open_exposure_count": int(_float_value(exposure.get("open_count"), default=0.0)),
        "open_exposure_assets": ", ".join(
            f"{asset}:{count}" for asset, count in sorted(asset_counts.items())
        ),
        "open_exposure_summary": _clean_value(exposure.get("summary")),
        "open_exposure_attention_count": int(_float_value(exposure.get("attention_count"), default=0.0)),
    }


def _add_open_exposure_warning(row: dict[str, Any], exposure: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(row)
    fields = _chain_open_exposure_fields(exposure)
    out.update(fields)
    if fields["open_exposure_count"] <= 0:
        return out
    warning = f"open exposure: {fields['open_exposure_summary']}"
    raw_flags = out.get("risk_flags")
    if isinstance(raw_flags, list):
        flags = [str(flag) for flag in raw_flags if str(flag).strip()]
    elif raw_flags:
        flags = [str(raw_flags)]
    else:
        flags = []
    if warning not in flags:
        flags.append(warning)
    out["risk_flags"] = flags
    return out


def _option_chain_decision_pack(
    rows: list[dict[str, Any]],
    quote_quality: str,
    source: str,
    exposure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not rows:
        return {
            "status": "empty",
            "label": "No match",
            "primary": None,
            "alternatives": [],
            "risk_notes": ["No contracts matched the current filters."],
            "next_step": "Loosen filters or scan a different ticker.",
            "saveable_count": 0,
            "trade_plan": None,
            "open_exposure": exposure or {},
        }
    saveable = [
        row for row in rows
        if str(row.get("contract_grade") or "").upper() in {"A", "B"}
        and str(row.get("review_lane") or "") != "wait"
    ]
    reviewable = [
        row for row in rows
        if str(row.get("readiness_label") or "") in {"ready", "review"}
    ]
    primary = (saveable or reviewable or rows)[0]
    alternatives: list[dict[str, Any]] = []
    seen: set[str] = {str(primary.get("contract_query") or "")}
    for row in rows:
        query = str(row.get("contract_query") or "")
        if not query or query in seen:
            continue
        alternatives.append(row)
        seen.add(query)
        if len(alternatives) >= 3:
            break

    grade = str(primary.get("contract_grade") or "D").upper()
    lane = str(primary.get("review_lane") or "review")
    readiness = str(primary.get("readiness_label") or "review")
    risk_notes: list[str] = []
    exposure = exposure if isinstance(exposure, dict) else {}
    if exposure.get("has_open"):
        risk_notes.append(
            f"Duplicate exposure check: {exposure.get('summary')}; review open positions before adding."
        )
    if str(quote_quality or "").lower() != "live_or_broker":
        risk_notes.append("Quote may be free/delayed; refresh before acting.")
    spread = _float_value(primary.get("spread_pct"), default=math.nan)
    if math.isfinite(spread) and spread > 0.15:
        risk_notes.append(f"Spread is wide at {spread * 100:.1f}%.")
    oi = _float_value(primary.get("openInterest"), default=0.0)
    if oi < 100:
        risk_notes.append("Open interest is light.")
    budget_fit = str(primary.get("budget_fit") or "").lower()
    budget_usage = _float_value(primary.get("budget_usage_pct"), default=math.nan)
    if budget_fit == "over_budget":
        risk_notes.append("One contract is above the selected premium budget.")
    elif budget_fit == "stretch":
        risk_notes.append("One contract is near the selected premium budget.")
    elif math.isfinite(budget_usage) and budget_usage > 0.75:
        risk_notes.append(f"One contract uses {budget_usage * 100:.0f}% of the selected premium budget.")
    dte = _float_value(primary.get("dte"), default=0.0)
    if dte < MIN_SWING_OPTION_DTE:
        risk_notes.append(f"DTE is below the {MIN_SWING_OPTION_DTE}d swing minimum.")
    if grade not in {"A", "B"}:
        risk_notes.append("No A/B contract passed the current filters.")
    swing_label = str(primary.get("swing_fit_label") or "").lower()
    if swing_label in {"speculative_swing", "avoid"}:
        risk_notes.append(f"Swing fit is {swing_label.replace('_', ' ')}.")
    swing_warnings = primary.get("swing_fit_warnings")
    if isinstance(swing_warnings, list):
        risk_notes.extend(str(flag) for flag in swing_warnings[:3] if flag)
    row_flags = primary.get("risk_flags")
    if isinstance(row_flags, list):
        risk_notes.extend(str(flag) for flag in row_flags[:3] if flag)
    risk_notes = list(dict.fromkeys(risk_notes))[:6]

    if saveable and grade == "A":
        status = "primary_review"
        label = "Best contract"
        next_step = "Save this contract, then refresh quotes before any paper/manual review."
    elif saveable:
        status = "secondary_review"
        label = "Reviewable contract"
        next_step = "Save for watchlist review; require a cleaner quote before acting."
    else:
        status = "watch_only"
        label = "Watch only"
        next_step = "Do not force this chain; wait for better liquidity, spread, or DTE."
    trade_plan = _option_chain_trade_plan(primary, status, risk_notes)

    return {
        "status": status,
        "label": label,
        "source": source,
        "quote_quality": quote_quality,
        "primary": {k: _clean_value(v) for k, v in primary.items()},
        "alternatives": [{k: _clean_value(v) for k, v in row.items()} for row in alternatives],
        "risk_notes": risk_notes,
        "next_step": next_step,
        "saveable_count": len(saveable),
        "trade_plan": trade_plan,
        "primary_grade": grade,
        "primary_lane": lane,
        "primary_readiness": readiness,
        "open_exposure": {k: _clean_value(v) for k, v in exposure.items()},
    }


def build_option_chain_scan(
    query: str,
    data_dir: Path = DATA_DIR,
    side: str = "all",
    min_dte: int = MIN_SWING_OPTION_DTE,
    max_dte: int = 900,
    max_spread_pct: float = 0.25,
    max_premium: float = 0.0,
    min_open_interest: int = 0,
    limit: int = 80,
    preset: str = "custom",
) -> dict[str, Any]:
    """Inspect a ticker's current option chain using the existing free chain stack."""
    clean = str(query or "").strip()
    if not clean:
        return {"ok": False, "error": "ticker or company name is required", "rows": []}
    resolution = resolve_symbol(clean)
    ticker = str(resolution.get("symbol") or "").upper()
    if not ticker:
        return {"ok": False, "error": resolution.get("error") or "could not resolve ticker", "rows": []}
    if ticker.endswith("=F") or ticker.startswith("^"):
        return {"ok": False, "error": f"{ticker} is not an equity or ETF option-chain symbol", "rows": []}

    preset_norm, preset_cfg = _chain_preset_config(preset)
    if preset_norm != "custom":
        side = str(preset_cfg.get("side", side))
        min_dte = int(preset_cfg.get("min_dte", min_dte))
        max_dte = int(preset_cfg.get("max_dte", max_dte))
        max_spread_pct = float(preset_cfg.get("max_spread_pct", max_spread_pct))
        max_premium = float(preset_cfg.get("max_premium", max_premium))
        min_open_interest = int(preset_cfg.get("min_open_interest", min_open_interest))

    side_norm = str(side or "all").strip().lower()
    if side_norm in {"c", "calls"}:
        side_norm = "call"
    elif side_norm in {"p", "puts"}:
        side_norm = "put"
    if side_norm not in {"all", "call", "put"}:
        side_norm = "all"

    try:
        blob = _fetch_option_chain_for_provider_status(ticker)
    except Exception as exc:
        return {"ok": False, "error": f"option-chain fetch failed: {exc}", "symbol": ticker, "rows": []}
    if not blob or not blob.get("chains"):
        return {"ok": False, "error": "no option-chain data returned", "symbol": ticker, "rows": []}

    spot = _float_value(blob.get("spot"), default=math.nan)
    source = str(blob.get("source") or "unknown")
    quote_quality = blob.get("quote_quality") or ("live_or_broker" if source == "tradier" else "free_or_delayed")
    source_attempts = blob.get("source_attempts") if isinstance(blob.get("source_attempts"), list) else []
    open_exposure = _open_exposure_for_symbol(data_dir, ticker)
    today = datetime.now(timezone.utc).date()
    rows: list[dict[str, Any]] = []
    rejection_examples: list[dict[str, Any]] = []
    rejected = 0
    rejection_reason_counts: dict[str, int] = {}
    total_contracts = 0

    for expiry, chain_df in (blob.get("chains") or {}).items():
        if not isinstance(chain_df, pd.DataFrame) or chain_df.empty:
            continue
        exp_ts = pd.to_datetime(str(expiry), errors="coerce", utc=True)
        if pd.isna(exp_ts):
            continue
        dte = int((exp_ts.date() - today).days)
        for _, raw in chain_df.iterrows():
            total_contracts += 1
            contract_side = str(raw.get("side") or "").strip().lower()
            if contract_side in {"c"}:
                contract_side = "call"
            elif contract_side in {"p"}:
                contract_side = "put"
            strike = _float_value(raw.get("strike"), default=math.nan)
            mid = _option_mid(raw)
            spread_pct = _option_spread_pct(raw, mid)
            oi = int(_float_value(raw.get("openInterest"), default=0.0))
            volume = int(_float_value(raw.get("volume"), default=0.0))
            premium = mid * 100.0 if math.isfinite(mid) else float("nan")
            moneyness = ((strike - spot) / spot) if math.isfinite(strike) and math.isfinite(spot) and spot > 0 else None

            if side_norm != "all" and contract_side != side_norm:
                reasons = [f"side is not {side_norm}"]
            else:
                reasons = []
            if dte < min_dte or dte > max_dte:
                reasons.append("outside DTE range")
            if not math.isfinite(strike) or not math.isfinite(mid) or mid <= 0:
                reasons.append("missing strike or mid")
            if max_premium > 0 and (not math.isfinite(premium) or premium > max_premium):
                reasons.append("premium above budget")
            if spread_pct is not None and max_spread_pct > 0 and spread_pct > max_spread_pct:
                reasons.append("spread above filter")
            if oi < min_open_interest:
                reasons.append("open interest below filter")
            if reasons:
                rejected += 1
                unique_reasons = list(dict.fromkeys(reasons))
                for reason in unique_reasons:
                    rejection_reason_counts[reason] = rejection_reason_counts.get(reason, 0) + 1
                if len(rejection_examples) < 25:
                    rejection_examples.append({
                        "side": contract_side or "-",
                        "expiry": str(expiry),
                        "dte": dte,
                        "strike": _clean_value(strike),
                        "mid": _clean_value(round(mid, 4) if math.isfinite(mid) else None),
                        "premium_dollars": _clean_value(round(premium, 2) if math.isfinite(premium) else None),
                        "spread_pct": _clean_value(spread_pct),
                        "openInterest": oi,
                        "volume": volume,
                        "reason": "; ".join(unique_reasons),
                        "reasons": unique_reasons,
                    })
                continue

            row = {
                "symbol": ticker,
                "side": contract_side,
                "expiry": str(expiry),
                "chain_source": source,
                "quote_quality": quote_quality,
                "data_delay": _clean_value(blob.get("data_delay")),
                "dte": dte,
                "dte_bucket": _option_dte_bucket(float(dte)),
                "strike": strike,
                "bid": _clean_value(raw.get("bid")),
                "ask": _clean_value(raw.get("ask")),
                "mid": round(mid, 4),
                "premium_dollars": round(premium, 2),
                "spread_pct": _clean_value(spread_pct),
                "volume": volume,
                "openInterest": oi,
                "impliedVolatility": _clean_value(raw.get("impliedVolatility")),
                "delta": _clean_value(raw.get("delta")),
                "moneyness_pct": _clean_value(moneyness),
            }
            row["contract_query"] = (
                f"{ticker} {expiry} {'C' if contract_side == 'call' else 'P'} {strike:g}"
                if math.isfinite(strike) else ""
            )
            row.update(_option_chain_trade_refs(row, spot, max_premium))
            row.update(_option_chain_swing_fit(row, max_premium))
            row["contract_quality_score"] = round(_option_chain_score(row), 3)
            row.update(_option_contract_readiness(row, str(quote_quality)))
            row.update(_option_contract_grade(row, max_premium))
            row["chain_factor_breakdown"] = _option_chain_factor_breakdown(row, max_premium)
            row["chain_factor_summary"] = _swing_factor_summary(row["chain_factor_breakdown"])
            rows.append(row)

    rows = sorted(
        rows,
        key=lambda r: (
            _float_value(r.get("swing_fit_score"), default=-999.0),
            _float_value(r.get("contract_quality_score"), default=-999.0),
            -abs(_float_value(r.get("moneyness_pct"), default=99.0)),
            -_float_value(r.get("dte"), default=9999.0),
        ),
        reverse=True,
    )
    limited = [{k: _clean_value(v) for k, v in row.items()} for row in rows[:limit]]
    summary = _option_chain_scan_summary(rows, max_premium)
    expiry_summary = _option_chain_expiry_summary(rows, max_premium)
    decision = _option_chain_decision_pack(rows, str(quote_quality), source, open_exposure)
    rejection_reason_counts = dict(
        sorted(rejection_reason_counts.items(), key=lambda item: (-item[1], item[0]))
    )
    top_rejection_reasons = _top_reason_rows(rejection_reason_counts)
    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "query": clean,
        "symbol": ticker,
        "resolution": resolution,
        "source": source,
        "quote_quality": quote_quality,
        "data_delay": _clean_value(blob.get("data_delay")),
        "source_attempts": [{k: _clean_value(v) for k, v in row.items()} for row in source_attempts],
        "open_exposure": open_exposure,
        "providers_checked": len(source_attempts),
        "spot": _clean_value(spot),
        "total_expirations": len(blob.get("expirations") or []),
        "total_contracts": total_contracts,
        "filtered_count": len(rows),
        "rejected_count": rejected,
        "rejection_reason_counts": rejection_reason_counts,
        "top_rejection_reasons": top_rejection_reasons,
        "rejection_examples": rejection_examples,
        "preset": preset_norm,
        "preset_label": preset_cfg.get("label"),
        "preset_description": preset_cfg.get("description"),
        "scan_summary": summary,
        "decision": decision,
        "expiry_summary": expiry_summary,
        "filters": {
            "side": side_norm,
            "min_dte": min_dte,
            "max_dte": max_dte,
            "max_spread_pct": max_spread_pct,
            "max_premium": max_premium,
            "min_open_interest": min_open_interest,
        },
        "summary": summary,
        "rows": limited,
        "notes": [
            "Option-chain scan uses Optedge's existing provider stack.",
            "Free/keyless sources may be delayed, incomplete, or blocked for some tickers.",
            "This view inspects contracts only; it does not place trades.",
        ],
    }


def _bulk_chain_symbol_candidates(data_dir: Path, query: str = "", limit: int = 8) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 8), 20))
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(symbol: Any, source: str, score: Any = None, reason: str = "") -> None:
        clean = str(symbol or "").strip().upper()
        if not clean or clean in seen:
            return
        if clean.endswith("=F") or clean.startswith("^"):
            return
        seen.add(clean)
        rows.append({
            "symbol": clean,
            "source": source,
            "score": _clean_value(score),
            "reason": reason or source,
        })

    raw_query = str(query or "").strip()
    if raw_query:
        for token in re.split(r"[,;\s]+", raw_query):
            token = token.strip()
            if not token:
                continue
            resolution = resolve_symbol(token)
            add(resolution.get("symbol") or token, "typed shortlist", 1.0, token)
        return rows[:limit]

    try:
        gated = build_climate_gated_setups(data_dir, per_asset=6, limit=max(limit, 10), include_held=False)
        for row in gated.get("rows", []):
            asset = str(row.get("asset") or "")
            if asset not in {"option", "share", "value"}:
                continue
            reasons = row.get("climate_gate_reasons") if isinstance(row.get("climate_gate_reasons"), list) else []
            reason = "; ".join(str(item) for item in reasons[:3] if item) or row.get("setup") or ""
            add(
                row.get("ticker_or_symbol"),
                f"climate-gated {asset} setup",
                row.get("climate_gate_score") or row.get("score"),
                reason,
            )
            if len(rows) >= limit:
                return rows
    except Exception:
        pass

    try:
        scout = build_swing_scout(
            data_dir,
            limit=max(limit * 2, 12),
            asset="all",
            min_score=55,
            include_wait=False,
        )
        for row in scout.get("rows", []):
            asset = str(row.get("asset") or "")
            if asset not in {"option", "share", "value"}:
                continue
            reasons = row.get("reasons") if isinstance(row.get("reasons"), list) else []
            reason = "; ".join(str(item) for item in reasons[:3] if item) or row.get("lane") or row.get("setup") or ""
            add(
                row.get("ticker_or_symbol"),
                f"swing scout {asset}",
                row.get("swing_scout_score"),
                reason,
            )
            if len(rows) >= limit:
                return rows
    except Exception:
        pass

    setups = build_best_setups(data_dir, per_asset=6, limit=24)
    for row in setups.get("rows", []):
        asset = str(row.get("asset") or "")
        if asset not in {"option", "share", "value"}:
            continue
        add(
            row.get("ticker_or_symbol"),
            f"best {asset} setup",
            row.get("score"),
            row.get("reason_selected") or row.get("setup") or "",
        )
        if len(rows) >= limit:
            return rows

    for item in load_watchlist(data_dir).get("entries", []):
        add(item.get("symbol"), "research watchlist", 0.75, item.get("query") or "")
        if len(rows) >= limit:
            return rows

    return rows[:limit]


def _chain_grade_rank(row: dict[str, Any]) -> int:
    return _option_grade_rank(row.get("contract_grade"))


def build_option_chain_batch(
    data_dir: Path = DATA_DIR,
    query: str = "",
    side: str = "all",
    min_dte: int = MIN_SWING_OPTION_DTE,
    max_dte: int = 900,
    max_spread_pct: float = 0.25,
    max_premium: float = 500.0,
    min_open_interest: int = 0,
    preset: str = "swing",
    symbols_limit: int = 6,
    contracts_per_symbol: int = 4,
    limit: int = 18,
) -> dict[str, Any]:
    """Scan option chains for a compact shortlist of symbols using free/provider-stack data."""
    symbols_limit = max(1, min(int(symbols_limit or 6), 20))
    contracts_per_symbol = max(1, min(int(contracts_per_symbol or 4), 12))
    limit = max(1, min(int(limit or 18), 80))
    candidates = _bulk_chain_symbol_candidates(data_dir, query=query, limit=symbols_limit)

    rows: list[dict[str, Any]] = []
    symbol_summaries: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    grade_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}

    for idx, candidate in enumerate(candidates, start=1):
        symbol = str(candidate.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        report = build_option_chain_scan(
            symbol,
            data_dir,
            side=side,
            min_dte=min_dte,
            max_dte=max_dte,
            max_spread_pct=max_spread_pct,
            max_premium=max_premium,
            min_open_interest=min_open_interest,
            limit=contracts_per_symbol,
            preset=preset,
        )
        summary = {
            "symbol": symbol,
            "candidate_source": candidate.get("source"),
            "candidate_score": candidate.get("score"),
            "candidate_reason": candidate.get("reason"),
            "ok": bool(report.get("ok")),
            "source": _clean_value(report.get("source")),
            "quote_quality": _clean_value(report.get("quote_quality")),
            "data_delay": _clean_value(report.get("data_delay")),
            "total_contracts": _clean_value(report.get("total_contracts")),
            "filtered_count": _clean_value(report.get("filtered_count")),
            "rejected_count": _clean_value(report.get("rejected_count")),
            "top_rejects": "; ".join(
                f"{row.get('reason')} ({row.get('count')})"
                for row in (report.get("top_rejection_reasons") or [])[:3]
                if row.get("reason")
            ),
            "grades": _clean_value((report.get("scan_summary") or {}).get("grade_counts")),
            "best_reviewable": _clean_value((report.get("scan_summary") or {}).get("best_reviewable")),
        }
        exposure_fields = _chain_open_exposure_fields(report.get("open_exposure"))
        summary.update(exposure_fields)
        symbol_summaries.append(summary)
        if not report.get("ok"):
            errors.append({
                "symbol": symbol,
                "candidate_source": candidate.get("source"),
                "error": report.get("error") or "chain scan failed",
            })
            continue
        source = str(report.get("source") or "unknown")
        source_counts[source] = source_counts.get(source, 0) + 1
        for rank, row in enumerate(report.get("rows", [])[:contracts_per_symbol], start=1):
            item = _add_open_exposure_warning(row, report.get("open_exposure"))
            item["candidate_rank"] = idx
            item["candidate_source"] = candidate.get("source")
            item["candidate_score"] = candidate.get("score")
            item["candidate_reason"] = candidate.get("reason")
            item["symbol_contract_rank"] = rank
            item["batch_source"] = source
            item["batch_quote_quality"] = _clean_value(report.get("quote_quality"))
            item["batch_data_delay"] = _clean_value(report.get("data_delay"))
            rows.append(item)
            grade = str(item.get("contract_grade") or "ungraded")
            grade_counts[grade] = grade_counts.get(grade, 0) + 1

    rows = sorted(
        rows,
        key=lambda row: (
            _chain_grade_rank(row),
            str(row.get("review_lane") or "") == "primary_review",
            _float_value(row.get("contract_quality_score"), default=0.0),
            _float_value(row.get("openInterest"), default=0.0),
            -_float_value(row.get("spread_pct"), default=9.0),
        ),
        reverse=True,
    )[:limit]
    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "query": str(query or "").strip(),
        "preset": _chain_preset_config(preset)[0],
        "candidate_count": len(candidates),
        "symbols_scanned": len(symbol_summaries),
        "successful_scans": sum(1 for row in symbol_summaries if row.get("ok")),
        "error_count": len(errors),
        "row_count": len(rows),
        "open_exposure_count": sum(
            int(_float_value(row.get("open_exposure_count"), default=0.0))
            for row in symbol_summaries
        ),
        "open_exposure_symbols": [
            row.get("symbol") for row in symbol_summaries
            if _float_value(row.get("open_exposure_count"), default=0.0) > 0
        ],
        "grade_counts": grade_counts,
        "source_counts": source_counts,
        "candidates": candidates,
        "symbol_summaries": symbol_summaries,
        "errors": errors,
        "rows": [{k: _clean_value(v) for k, v in row.items()} for row in rows],
        "notes": [
            "Bulk chain scan ranks contracts across a small shortlist so free sources are not hammered.",
            "Blank symbol input uses climate-gated setups, swing scout winners, latest best setups, and the research watchlist.",
            "Free/keyless chain quotes may be delayed or incomplete; verify before any manual paper entry.",
        ],
    }


def _shortlist_cell(value: Any) -> Any:
    value = _clean_value(value)
    if isinstance(value, list):
        cleaned = []
        for item in value:
            clean_item = _clean_value(item)
            if clean_item is None or clean_item == "":
                continue
            if isinstance(clean_item, (dict, list)):
                cleaned.append(json.dumps(clean_item, sort_keys=True))
            else:
                cleaned.append(str(clean_item))
        return "; ".join(cleaned)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return value


def write_option_chain_shortlist(report: dict[str, Any], data_dir: Path = DATA_DIR) -> dict[str, Any]:
    """Write the latest 3m+ option-chain shortlist as portable review artifacts."""
    if not isinstance(report, dict):
        return {"ok": False, "error": "chain shortlist report is required"}
    rows = report.get("rows")
    if not isinstance(rows, list) or not rows:
        return {"ok": False, "error": "no chain shortlist rows to export"}

    generated_at = datetime.now(timezone.utc).isoformat()
    source_generated_at = str(report.get("generated_at") or "")
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = {col: _shortlist_cell(row.get(col)) for col in CHAIN_SHORTLIST_COLUMNS}
        item["generated_at"] = generated_at
        if not item.get("quote_quality"):
            item["quote_quality"] = _shortlist_cell(row.get("batch_quote_quality"))
        if not item.get("data_delay"):
            item["data_delay"] = _shortlist_cell(row.get("batch_data_delay"))
        if not item.get("chain_source"):
            item["chain_source"] = _shortlist_cell(row.get("batch_source"))
        normalized.append(item)

    if not normalized:
        return {"ok": False, "error": "no valid chain shortlist rows to export"}

    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    csv_path = data_dir / "option_chain_shortlist.csv"
    json_path = data_dir / "option_chain_shortlist.json"
    pd.DataFrame(normalized, columns=CHAIN_SHORTLIST_COLUMNS).to_csv(csv_path, index=False)
    payload = {
        "ok": True,
        "generated_at": generated_at,
        "source_generated_at": source_generated_at,
        "count": len(normalized),
        "preset": report.get("preset"),
        "query": report.get("query"),
        "candidate_count": report.get("candidate_count"),
        "symbols_scanned": report.get("symbols_scanned"),
        "successful_scans": report.get("successful_scans"),
        "grade_counts": report.get("grade_counts") or {},
        "source_counts": report.get("source_counts") or {},
        "notes": [
            "External review shortlist only; no trades are placed.",
            "Rows come from Optedge's free/provider option-chain batch scan.",
            "Free/keyless option-chain quotes may be delayed or incomplete.",
        ],
        "rows": normalized,
    }
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "ok": True,
        "count": len(normalized),
        "csv_path": str(csv_path),
        "json_path": str(json_path),
        "artifacts": {
            "csv": "/artifact/option-chain-shortlist",
            "json": "/artifact/option-chain-shortlist-json",
        },
        "notes": payload["notes"],
    }


def _is_actionable(row: pd.Series) -> bool:
    status = str(row.get("trade_status") or "").strip().lower()
    if status in {"watch", "skip", "blocked"}:
        return False
    asset = str(row.get("asset") or "").lower()
    if asset == "option":
        if str(row.get("swing_fit_label") or "").strip().lower() == "avoid":
            return False
        return _float_value(row.get("suggested_contracts")) > 0
    if asset == "futures":
        return _float_value(row.get("suggested_contracts")) > 0
    if asset == "share":
        return _float_value(row.get("suggested_dollars")) > 0
    return True


def _opportunity_records(df: pd.DataFrame, asset: str, limit: int) -> list[dict[str, Any]]:
    if df.empty:
        return []
    spec = OPPORTUNITY_SPECS[asset]
    cols = [c for c in spec["columns"] if c in df.columns]
    if "asset" not in cols:
        cols.insert(0, "asset")
    records: list[dict[str, Any]] = []
    for _, row in df[cols].head(limit).iterrows():
        records.append({str(k): _clean_value(v) for k, v in row.to_dict().items()})
    return records


def _short_number(value: Any) -> str:
    number = _float_value(value, default=math.nan)
    if math.isfinite(number):
        return f"{number:g}"
    return str(value or "").strip()


def _setup_symbol(row: pd.Series, asset: str, spec: dict[str, Any]) -> str:
    symbol_col = str(spec.get("symbol_col") or "ticker")
    return str(row.get(symbol_col) or row.get("ticker") or row.get("symbol") or "").strip().upper()


def _setup_label(row: pd.Series, asset: str, symbol: str) -> str:
    if asset == "option":
        side_raw = str(row.get("side") or "").strip().lower()
        side = "C" if side_raw.startswith("c") else "P" if side_raw.startswith("p") else side_raw.upper()
        strike = _short_number(row.get("strike"))
        expiry = str(row.get("expiry") or "").strip()
        return " ".join(part for part in (symbol, side, strike, expiry) if part)
    if asset == "futures":
        direction = str(row.get("direction") or "").strip().upper()
        name = str(row.get("name") or row.get("contract") or "").strip()
        return " ".join(part for part in (symbol, direction or None, name or None) if part)
    if asset == "value":
        bucket = str(row.get("value_bucket") or "value").strip()
        return f"{symbol} {bucket}".strip()
    return f"{symbol} share".strip()


def _setup_entry_price(row: pd.Series, asset: str) -> Any:
    for col in ("mid", "entry_price", "spot", "price", "last"):
        if col in row and _clean_value(row.get(col)) is not None:
            return _clean_value(row.get(col))
    return None


def _setup_size(row: pd.Series, asset: str) -> str:
    if asset in {"option", "futures"}:
        contracts = _float_value(row.get("suggested_contracts"), default=0.0)
        if contracts > 0:
            return f"{contracts:g} contract(s)"
        return "-"
    if asset == "share":
        dollars = _float_value(row.get("suggested_dollars"), default=0.0)
        if dollars > 0:
            return f"${dollars:,.0f}"
    return "-"


def _setup_quality(row: pd.Series, asset: str) -> str:
    pieces: list[str] = []
    if asset == "option":
        spread = _float_value(row.get("spread_pct"), default=math.nan)
        if math.isfinite(spread):
            pieces.append(f"spread {spread * 100:.1f}%")
        source = str(row.get("chain_source") or row.get("quote_quality") or "").strip()
        if source:
            pieces.append(source)
    elif asset == "futures":
        hv20 = _float_value(row.get("hv20"), default=math.nan)
        if math.isfinite(hv20):
            pieces.append(f"HV20 {hv20:.2f}")
        if row.get("using_micro") is not None:
            pieces.append("micro" if bool(row.get("using_micro")) else "full")
    elif asset == "share":
        ev = _float_value(row.get("ev_pct"), default=math.nan)
        if math.isfinite(ev):
            pieces.append(f"EV {ev * 100:.1f}%")
    elif asset == "value":
        pe = _float_value(row.get("pe"), default=math.nan)
        if math.isfinite(pe):
            pieces.append(f"P/E {pe:.1f}")
        fcf = _float_value(row.get("fcf_yield"), default=math.nan)
        if math.isfinite(fcf):
            pieces.append(f"FCF {fcf * 100:.1f}%")
    return " | ".join(pieces[:3]) or "-"


def _setup_reason(row: pd.Series, asset: str) -> str:
    confidence = _float_value(row.get("confidence"), default=math.nan)
    score = _opportunity_score(row)
    parts = [f"score {score:.2f}"]
    if math.isfinite(confidence) and confidence > 0:
        parts.append(f"conf {confidence:.0f}")
    if asset == "option":
        edge = _float_value(row.get("net_edge_pct"), default=math.nan)
        if math.isfinite(edge):
            parts.append(f"edge {edge * 100:.1f}%")
    elif asset == "futures":
        fut_score = _float_value(row.get("futures_score"), default=math.nan)
        if math.isfinite(fut_score):
            parts.append(f"futures {fut_score:.2f}")
    elif asset == "value":
        bucket = str(row.get("value_bucket") or "").strip()
        if bucket:
            parts.append(bucket)
    return ", ".join(parts)


def _readiness(score: int, flags: list[str]) -> dict[str, Any]:
    clean_score = max(0, min(100, int(score)))
    if clean_score >= 80 and not flags:
        label = "ready"
        next_step = "Review exact quote and thesis."
    elif clean_score >= 65:
        label = "review"
        next_step = "Check flagged items before acting."
    else:
        label = "wait"
        next_step = "Do not act until flags improve."
    return {
        "readiness_score": clean_score,
        "readiness_label": label,
        "risk_flags": flags[:5],
        "next_step": next_step,
    }


def _setup_readiness(row: pd.Series, asset: str) -> dict[str, Any]:
    score = 100
    flags: list[str] = []
    confidence = _float_value(row.get("confidence"), default=math.nan)
    status = str(row.get("trade_status") or "").strip().lower()
    freshness = str(row.get("snapshot_freshness") or "").strip().lower()
    stop = _float_value(row.get("stop_price"), default=math.nan)
    target = _float_value(row.get("target_price"), default=math.nan)

    if status in {"watch", "skip", "blocked"}:
        score -= 35
        flags.append(f"status {status}")
    if math.isfinite(confidence):
        if confidence < 55:
            score -= 25
            flags.append("low confidence")
        elif confidence < 70:
            score -= 10
            flags.append("medium confidence")
    elif asset != "value":
        score -= 10
        flags.append("missing confidence")
    if freshness == "stale":
        score -= 20
        flags.append("stale snapshot")
    elif freshness == "aging":
        score -= 8
        flags.append("aging snapshot")

    if asset in {"option", "share", "futures"}:
        if not math.isfinite(stop) or stop <= 0:
            score -= 15
            flags.append("missing stop")
        if not math.isfinite(target) or target <= 0:
            score -= 10
            flags.append("missing target")

    if asset == "option":
        dte = _float_value(row.get("dte"), default=math.nan)
        spread = _float_value(row.get("spread_pct"), default=math.nan)
        contracts = _float_value(row.get("suggested_contracts"), default=0.0)
        quote_quality = str(row.get("quote_quality") or row.get("chain_source") or "").lower()
        swing_label = str(row.get("swing_fit_label") or "").strip().lower()
        swing_score = _float_value(row.get("swing_fit_score"), default=math.nan)
        if not math.isfinite(dte) or dte < MIN_SWING_OPTION_DTE:
            score -= 45
            flags.append("below 90 DTE")
        if math.isfinite(spread):
            if spread > 0.25:
                score -= 30
                flags.append("very wide spread")
            elif spread > 0.15:
                score -= 12
                flags.append("wide spread")
        else:
            score -= 8
            flags.append("missing spread")
        if contracts <= 0:
            score -= 40
            flags.append("no contract size")
        if swing_label == "avoid":
            score -= 45
            flags.append("avoid swing fit")
        elif swing_label == "speculative_swing":
            score -= 14
            flags.append("speculative swing fit")
        elif swing_label == "clean_swing" and math.isfinite(swing_score) and swing_score >= 85:
            score += 6
        elif swing_label == "reviewable_swing":
            score += 2
        if quote_quality in {"", "unknown"}:
            score -= 8
            flags.append("unknown quote source")
        elif "delayed" in quote_quality or "free" in quote_quality:
            score -= 6
            flags.append("verify live quote")
    elif asset == "share":
        dollars = _float_value(row.get("suggested_dollars"), default=0.0)
        if dollars <= 0:
            score -= 30
            flags.append("no share size")
    elif asset == "futures":
        contracts = _float_value(row.get("suggested_contracts"), default=0.0)
        risk = _float_value(row.get("risk_dollars"), default=math.nan)
        reward = _float_value(row.get("reward_dollars"), default=math.nan)
        if contracts <= 0:
            score -= 35
            flags.append("no contract size")
        if math.isfinite(risk) and math.isfinite(reward) and risk > 0 and reward / risk < 1.5:
            score -= 12
            flags.append("weak reward/risk")
    elif asset == "value":
        if not str(row.get("value_bucket") or "").strip():
            score -= 10
            flags.append("missing value bucket")

    return _readiness(score, flags)


def _best_setup_record(row: pd.Series, asset: str, source_file: str | None) -> dict[str, Any]:
    spec = OPPORTUNITY_SPECS[asset]
    symbol = _setup_symbol(row, asset, spec)
    score = _opportunity_score(row)
    readiness = _setup_readiness(row, asset)
    row_source = str(row.get("_source_file") or source_file or "").strip() or None
    record = {
        "asset": asset,
        "ticker_or_symbol": symbol,
        "setup": _setup_label(row, asset, symbol),
        "action": _clean_value(row.get("side") or row.get("direction") or ("buy" if asset != "value" else "review")),
        "score": round(score, 4),
        "confidence": _clean_value(row.get("confidence")),
        "trade_status": _clean_value(row.get("trade_status") or ("Trade" if bool(row.get("actionable")) else "Review")),
        "entry_price": _setup_entry_price(row, asset),
        "stop_price": _clean_value(row.get("stop_price")),
        "target_price": _clean_value(row.get("target_price")),
        "size": _setup_size(row, asset),
        "suggested_contracts": _clean_value(row.get("suggested_contracts")),
        "suggested_dollars": _clean_value(row.get("suggested_dollars")),
        "spread_pct": _clean_value(row.get("spread_pct")),
        "risk_dollars": _clean_value(row.get("risk_dollars")),
        "reward_dollars": _clean_value(row.get("reward_dollars")),
        "dte": _clean_value(row.get("dte")),
        "expiry": _clean_value(row.get("expiry")),
        "quality": _setup_quality(row, asset),
        "source_file": row_source,
        "snapshot_freshness": _clean_value(row.get("snapshot_freshness")),
        "snapshot_age_min": _clean_value(row.get("snapshot_age_min")),
        "reason_selected": _setup_reason(row, asset),
        "_sort_score": score,
    }
    if asset == "option":
        for key in (
            "contract",
            "swing_fit_score",
            "swing_fit_label",
            "swing_fit_reasons",
            "swing_fit_warnings",
            "breakeven_move_label",
            "liquidity_label",
        ):
            if key in row:
                record[key] = _clean_value(row.get(key))
    record.update(readiness)
    return record


def build_best_setups(
    data_dir: Path = DATA_DIR,
    per_asset: int = 3,
    limit: int = 12,
) -> dict[str, Any]:
    """Build a compact decision surface from the latest local opportunity snapshots."""
    per_asset = max(1, min(int(per_asset or 3), 10))
    limit = max(1, min(int(limit or 12), 40))
    rows: list[dict[str, Any]] = []
    by_asset: dict[str, list[dict[str, Any]]] = {}
    summaries: list[dict[str, Any]] = []
    sources: dict[str, str | None] = {}

    for asset_name, spec in OPPORTUNITY_SPECS.items():
        path = _latest_file(data_dir, spec["pattern"])
        source_file = path.name if path else None
        sources[asset_name] = source_file
        df = _read_parquet(path)
        chain_shortlist_rows = 0
        if asset_name == "option":
            chain_df = _load_option_chain_shortlist(data_dir)
            if not chain_df.empty:
                chain_shortlist_rows = int(len(chain_df))
                sources["option_chain_shortlist"] = str(chain_df["_source_file"].iloc[0]) if "_source_file" in chain_df.columns else "option_chain_shortlist"
                df = pd.concat([df, chain_df], ignore_index=True, sort=False) if not df.empty else chain_df
        if df.empty:
            by_asset[asset_name] = []
            summaries.append({
                "asset": asset_name,
                "source_file": source_file,
                "rows": 0,
                "actionable_rows": 0,
                "selected": 0,
                "chain_shortlist_rows": chain_shortlist_rows,
                "status": "missing",
            })
            continue

        out = df.copy()
        out["asset"] = asset_name
        out["actionable"] = out.apply(_is_actionable, axis=1)
        out["_opportunity_score"] = out.apply(_opportunity_score, axis=1)
        actionable = out[out["actionable"]].copy()
        if asset_name == "option" and "dte" in actionable.columns:
            actionable = actionable[
                pd.to_numeric(actionable["dte"], errors="coerce").fillna(MIN_SWING_OPTION_DTE)
                >= MIN_SWING_OPTION_DTE
            ]
        candidates = actionable if (asset_name == "option" or not actionable.empty) else out.copy()
        candidates = candidates.sort_values("_opportunity_score", ascending=False, kind="mergesort")

        asset_records = [
            _best_setup_record(row, asset_name, source_file)
            for _, row in candidates.head(per_asset).iterrows()
        ]
        by_asset[asset_name] = [
            {k: v for k, v in record.items() if not k.startswith("_")}
            for record in asset_records
        ]
        rows.extend(asset_records)
        summaries.append({
            "asset": asset_name,
            "source_file": source_file,
            "rows": int(len(out)),
            "actionable_rows": int(len(actionable)),
            "selected": int(len(asset_records)),
            "chain_shortlist_rows": chain_shortlist_rows,
            "status": "actionable" if len(actionable) else "review_only",
            "snapshot_freshness": _clean_value(out["snapshot_freshness"].iloc[0]) if "snapshot_freshness" in out.columns else None,
            "snapshot_age_min": _clean_value(out["snapshot_age_min"].iloc[0]) if "snapshot_age_min" in out.columns else None,
        })

    rows = sorted(rows, key=lambda row: _float_value(row.get("_sort_score")), reverse=True)[:limit]
    clean_rows = [{k: v for k, v in row.items() if not k.startswith("_")} for row in rows]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(clean_rows),
        "rows": clean_rows,
        "by_asset": by_asset,
        "asset_summaries": summaries,
        "sources": sources,
        "notes": [
            "Best setups read the latest local top_* snapshots and apply Optedge sizing/status filters.",
            "Options include chain-source and spread quality when available.",
            "Saved 3m+ chain shortlist rows are included as option review candidates when present.",
            "This is a research shortlist only; no orders are placed.",
        ],
    }


def _market_cap_profile(row: pd.Series) -> dict[str, Any]:
    cap = _float_value(row.get("market_cap") or row.get("market_cap_f"), default=math.nan)
    if not math.isfinite(cap) or cap <= 0:
        return {"market_cap": None, "bucket": "unknown", "bonus": 0.0, "warning": "missing market cap"}
    if cap < 300_000_000:
        return {"market_cap": cap, "bucket": "micro", "bonus": 14.0, "warning": "micro-cap execution risk"}
    if cap < 2_000_000_000:
        return {"market_cap": cap, "bucket": "small", "bonus": 18.0, "warning": None}
    if cap < 10_000_000_000:
        return {"market_cap": cap, "bucket": "small-mid", "bonus": 14.0, "warning": None}
    if cap < 25_000_000_000:
        return {"market_cap": cap, "bucket": "mid", "bonus": 7.0, "warning": None}
    return {"market_cap": cap, "bucket": "large", "bonus": -10.0, "warning": "large-cap; less small-cap asymmetry"}


def _positive_component(value: Any, scale: float, cap: float) -> float:
    val = _float_value(value, default=0.0)
    return _clamp(val * scale, 0.0, cap)


def _swing_scout_components(row: pd.Series, asset: str) -> dict[str, Any]:
    cap_profile = _market_cap_profile(row)
    confidence = _float_value(row.get("confidence"), default=math.nan)
    readiness = _setup_readiness(row, asset)
    readiness_score = _float_value(readiness.get("readiness_score"), default=0.0)
    squeeze = (
        _positive_component(row.get("short_int_score"), 6.0, 12.0)
        + _positive_component(row.get("short_pct_of_float"), 55.0, 12.0)
        + _positive_component(_float_value(row.get("short_vol_ratio"), default=0.0) - 0.45, 18.0, 7.0)
        + _positive_component(row.get("dark_pool_score"), 5.0, 7.0)
        + _positive_component(row.get("uoa_score"), 5.0, 8.0)
    )
    attention = (
        _positive_component(row.get("social_score"), 8.0, 8.0)
        + _positive_component(row.get("gtrends_score"), 7.0, 8.0)
        + _positive_component(row.get("twitter_score"), 7.0, 8.0)
        + _positive_component(row.get("r_options_score"), 8.0, 8.0)
        + _positive_component(row.get("sentiment_delta"), 8.0, 6.0)
        + _positive_component(row.get("mentions"), 0.08, 5.0)
    )
    momentum = (
        _positive_component(row.get("tech_score"), 8.0, 8.0)
        + _positive_component(row.get("sector_rs_score"), 45.0, 9.0)
        + _positive_component(row.get("sector_flow_score"), 10.0, 6.0)
        + _positive_component(row.get("ticker_ret_20d") or row.get("ret_20d"), 45.0, 11.0)
        + _positive_component(row.get("rank_score"), 3.0, 10.0)
        + _positive_component(row.get("futures_score"), 18.0, 18.0)
    )
    value = (
        _positive_component(row.get("value_score"), 5.0, 12.0)
        + _positive_component(row.get("fcf_yield"), 40.0, 6.0)
        + _positive_component(row.get("rev_growth"), 18.0, 6.0)
        + _positive_component(row.get("fund_score"), 3.0, 6.0)
    )
    execution = 0.0
    if asset == "option":
        dte = _float_value(row.get("dte"), default=math.nan)
        spread = _float_value(row.get("spread_pct"), default=math.nan)
        contracts = _float_value(row.get("suggested_contracts"), default=0.0)
        if math.isfinite(dte) and dte >= MIN_SWING_OPTION_DTE:
            execution += 8.0
        if math.isfinite(spread):
            if spread <= 0.12:
                execution += 8.0
            elif spread <= 0.20:
                execution += 4.0
            elif spread > 0.30:
                execution -= 10.0
        if contracts > 0:
            execution += 7.0
    elif asset == "share":
        if _float_value(row.get("suggested_dollars"), default=0.0) > 0:
            execution += 7.0
    elif asset == "futures":
        if _float_value(row.get("suggested_contracts"), default=0.0) > 0:
            execution += 8.0
        if bool(row.get("using_micro")):
            execution += 4.0
        risk = _float_value(row.get("risk_dollars"), default=math.nan)
        reward = _float_value(row.get("reward_dollars"), default=math.nan)
        if math.isfinite(risk) and math.isfinite(reward) and risk > 0:
            execution += _clamp((reward / risk - 1.0) * 5.0, 0.0, 8.0)
    elif asset == "value":
        if str(row.get("value_bucket") or "").lower() in {"deep value", "value"}:
            execution += 5.0

    quality = _clamp((readiness_score - 55.0) * 0.35, 0.0, 16.0)
    confidence_component = _clamp((confidence - 50.0) * 0.30, 0.0, 15.0) if math.isfinite(confidence) else 0.0
    return {
        "market_cap_profile": cap_profile,
        "squeeze": round(squeeze, 2),
        "attention": round(attention, 2),
        "momentum": round(momentum, 2),
        "value": round(value, 2),
        "execution": round(execution, 2),
        "quality": round(quality, 2),
        "confidence": round(confidence_component, 2),
        "readiness": readiness,
    }


def _fmt_compact_number(value: Any, *, precision: int = 1) -> str | None:
    val = _float_value(value, default=math.nan)
    if not math.isfinite(val):
        return None
    if abs(val) >= 1_000_000_000:
        return f"{val / 1_000_000_000:.1f}B"
    if abs(val) >= 1_000_000:
        return f"{val / 1_000_000:.1f}M"
    if abs(val) >= 1_000:
        return f"{val / 1_000:.1f}K"
    return f"{val:.{precision}f}"


def _fmt_ratio_pct(value: Any, *, precision: int = 0) -> str | None:
    val = _float_value(value, default=math.nan)
    if not math.isfinite(val):
        return None
    return f"{val * 100:.{precision}f}%"


def _fmt_dollars(value: Any) -> str | None:
    val = _float_value(value, default=math.nan)
    if not math.isfinite(val):
        return None
    return f"${val:,.0f}" if abs(val) >= 100 else f"${val:,.2f}"


def _join_factor_parts(parts: list[str | None], fallback: str) -> str:
    clean = [part for part in parts if part]
    return ", ".join(clean[:4]) if clean else fallback


def _swing_factor_summary(factors: list[dict[str, Any]], limit: int = 3) -> str | None:
    names = [str(item.get("factor") or "").strip() for item in factors if item.get("factor")]
    return " + ".join(names[:limit]) if names else None


def _swing_factor_breakdown(row: pd.Series, asset: str, components: dict[str, Any]) -> list[dict[str, Any]]:
    cap_profile = components.get("market_cap_profile") if isinstance(components.get("market_cap_profile"), dict) else {}
    readiness = components.get("readiness") if isinstance(components.get("readiness"), dict) else {}
    factors: list[dict[str, Any]] = []

    def add(factor: str, score: Any, detail: str, minimum: float = 0.1) -> None:
        score_val = _float_value(score, default=0.0)
        if score_val < minimum:
            return
        factors.append({
            "factor": factor,
            "score": round(score_val, 2),
            "detail": detail[:160],
        })

    if asset != "futures":
        cap_bonus = _float_value(cap_profile.get("bonus"), default=0.0)
        cap = _fmt_compact_number(cap_profile.get("market_cap"), precision=0)
        detail = _join_factor_parts([
            str(cap_profile.get("bucket") or "unknown") + " cap",
            f"${cap}" if cap else None,
        ], "market cap profile")
        add("Market cap", max(cap_bonus, 0.0), detail, minimum=0.1)

    add("Squeeze", components.get("squeeze"), _join_factor_parts([
        f"short float {_fmt_ratio_pct(row.get('short_pct_of_float'))}" if _fmt_ratio_pct(row.get("short_pct_of_float")) else None,
        f"short vol {_fmt_ratio_pct(row.get('short_vol_ratio'))}" if _fmt_ratio_pct(row.get("short_vol_ratio")) else None,
        f"short score {_fmt_compact_number(row.get('short_int_score'))}" if _fmt_compact_number(row.get("short_int_score")) else None,
        f"UOA {_fmt_compact_number(row.get('uoa_score'))}" if _fmt_compact_number(row.get("uoa_score")) else None,
        f"dark pool {_fmt_compact_number(row.get('dark_pool_score'))}" if _fmt_compact_number(row.get("dark_pool_score")) else None,
    ], "short interest or flow pressure"))
    add("Attention", components.get("attention"), _join_factor_parts([
        f"social {_fmt_compact_number(row.get('social_score'))}" if _fmt_compact_number(row.get("social_score")) else None,
        f"trends {_fmt_compact_number(row.get('gtrends_score'))}" if _fmt_compact_number(row.get("gtrends_score")) else None,
        f"twitter {_fmt_compact_number(row.get('twitter_score'))}" if _fmt_compact_number(row.get("twitter_score")) else None,
        f"mentions {_fmt_compact_number(row.get('mentions'), precision=0)}" if _fmt_compact_number(row.get("mentions"), precision=0) else None,
    ], "retail attention lift"))
    add("Momentum", components.get("momentum"), _join_factor_parts([
        f"tech {_fmt_compact_number(row.get('tech_score'))}" if _fmt_compact_number(row.get("tech_score")) else None,
        f"sector RS {_fmt_compact_number(row.get('sector_rs_score'))}" if _fmt_compact_number(row.get("sector_rs_score")) else None,
        f"20d {_fmt_ratio_pct(row.get('ticker_ret_20d') or row.get('ret_20d'))}" if _fmt_ratio_pct(row.get("ticker_ret_20d") or row.get("ret_20d")) else None,
        f"rank {_fmt_compact_number(row.get('rank_score'))}" if _fmt_compact_number(row.get("rank_score")) else None,
        f"futures {_fmt_compact_number(row.get('futures_score'))}" if _fmt_compact_number(row.get("futures_score")) else None,
    ], "price or macro momentum"))
    add("Value", components.get("value"), _join_factor_parts([
        f"value {_fmt_compact_number(row.get('value_score'))}" if _fmt_compact_number(row.get("value_score")) else None,
        f"FCF {_fmt_ratio_pct(row.get('fcf_yield'))}" if _fmt_ratio_pct(row.get("fcf_yield")) else None,
        f"growth {_fmt_ratio_pct(row.get('rev_growth'))}" if _fmt_ratio_pct(row.get("rev_growth")) else None,
        f"fund {_fmt_compact_number(row.get('fund_score'))}" if _fmt_compact_number(row.get("fund_score")) else None,
    ], "value or fundamental dislocation"))

    execution_parts: list[str | None]
    if asset == "option":
        execution_parts = [
            f"{int(_float_value(row.get('dte')))}d DTE" if math.isfinite(_float_value(row.get("dte"), default=math.nan)) else None,
            f"spread {_fmt_ratio_pct(row.get('spread_pct'))}" if _fmt_ratio_pct(row.get("spread_pct")) else None,
            f"{_fmt_compact_number(row.get('suggested_contracts'), precision=0)} contract(s)" if _fmt_compact_number(row.get("suggested_contracts"), precision=0) else None,
        ]
    elif asset == "share":
        execution_parts = [
            f"size {_fmt_dollars(row.get('suggested_dollars'))}" if _fmt_dollars(row.get("suggested_dollars")) else None,
            f"EV {_fmt_ratio_pct(row.get('ev_pct'))}" if _fmt_ratio_pct(row.get("ev_pct")) else None,
        ]
    elif asset == "futures":
        reward = _float_value(row.get("reward_dollars"), default=math.nan)
        risk = _float_value(row.get("risk_dollars"), default=math.nan)
        rr = reward / risk if math.isfinite(reward) and math.isfinite(risk) and risk > 0 else math.nan
        execution_parts = [
            str(row.get("contract") or row.get("micro_contract") or "").strip() or None,
            "micro" if bool(row.get("using_micro")) else None,
            f"{_fmt_compact_number(row.get('suggested_contracts'), precision=0)} contract(s)" if _fmt_compact_number(row.get("suggested_contracts"), precision=0) else None,
            f"R/R {rr:.1f}x" if math.isfinite(rr) else None,
        ]
    else:
        execution_parts = [str(row.get("value_bucket") or "").strip() or None]
    add("Execution", components.get("execution"), _join_factor_parts(execution_parts, "sizing and execution quality"))
    add("Quality", components.get("quality"), _join_factor_parts([
        str(readiness.get("readiness_label") or "").strip() or None,
        f"readiness {_fmt_compact_number(readiness.get('readiness_score'), precision=0)}" if _fmt_compact_number(readiness.get("readiness_score"), precision=0) else None,
    ], "setup readiness"))
    add("Confidence", components.get("confidence"), _join_factor_parts([
        f"confidence {_fmt_compact_number(row.get('confidence'), precision=0)}" if _fmt_compact_number(row.get("confidence"), precision=0) else None,
    ], "model confidence"))
    return sorted(factors, key=lambda item: _float_value(item.get("score"), default=0.0), reverse=True)[:5]


def _swing_scout_lane(row: pd.Series, asset: str, components: dict[str, Any]) -> str:
    if asset == "futures":
        return "futures_macro_swing"
    squeeze = _float_value(components.get("squeeze"))
    attention = _float_value(components.get("attention"))
    value = _float_value(components.get("value"))
    if asset == "option":
        if squeeze >= 14 or attention >= 12:
            return "small_cap_options_momentum"
        return "long_dated_option_swing"
    if asset == "value" or value >= 10:
        return "small_cap_value_dislocation"
    if squeeze >= 14:
        return "small_cap_squeeze_watch"
    if attention >= 12:
        return "retail_attention_swing"
    return "small_cap_share_swing"


def _swing_scout_reasons(row: pd.Series, asset: str, components: dict[str, Any]) -> tuple[list[str], list[str]]:
    reasons: list[str] = []
    warnings: list[str] = []
    cap = components["market_cap_profile"]
    if asset != "futures":
        reasons.append(f"{cap['bucket']} cap" if cap.get("bucket") != "unknown" else "market cap unknown")
        if cap.get("warning"):
            warnings.append(str(cap["warning"]))
    if _float_value(components.get("squeeze")) >= 12:
        reasons.append("short/squeeze pressure")
    if _float_value(components.get("attention")) >= 10:
        reasons.append("retail/attention lift")
    if _float_value(components.get("momentum")) >= 12:
        reasons.append("momentum confirmation")
    if _float_value(components.get("value")) >= 10:
        reasons.append("value/fundamental dislocation")
    if asset == "futures":
        reasons.append("futures/macro momentum")
    readiness = components.get("readiness") if isinstance(components.get("readiness"), dict) else {}
    warnings.extend(str(flag) for flag in (readiness.get("risk_flags") or [])[:4])
    if str(row.get("snapshot_freshness") or "").lower() == "stale":
        warnings.append("stale snapshot")
    if asset == "option":
        dte = _float_value(row.get("dte"), default=math.nan)
        if math.isfinite(dte) and dte < MIN_SWING_OPTION_DTE:
            warnings.append("below 90 DTE")
        spread = _float_value(row.get("spread_pct"), default=math.nan)
        if math.isfinite(spread) and spread > 0.20:
            warnings.append("wide option spread")
    return list(dict.fromkeys(reasons))[:6], list(dict.fromkeys(warnings))[:6]


def _swing_scout_record(row: pd.Series, asset: str, source_file: str | None) -> dict[str, Any]:
    components = _swing_scout_components(row, asset)
    cap_profile = components["market_cap_profile"]
    base = 34.0 if asset != "futures" else 42.0
    if asset != "futures":
        base += _float_value(cap_profile.get("bonus"))
    raw_score = (
        base
        + _float_value(components.get("squeeze"))
        + _float_value(components.get("attention"))
        + _float_value(components.get("momentum"))
        + _float_value(components.get("value"))
        + _float_value(components.get("execution"))
        + _float_value(components.get("quality"))
        + _float_value(components.get("confidence"))
    )
    reasons, warnings = _swing_scout_reasons(row, asset, components)
    readiness = components.get("readiness") if isinstance(components.get("readiness"), dict) else {}
    symbol = _setup_symbol(row, asset, OPPORTUNITY_SPECS[asset])
    setup = _setup_label(row, asset, symbol)
    factor_breakdown = _swing_factor_breakdown(row, asset, components)
    return {
        "asset": asset,
        "ticker_or_symbol": symbol,
        "setup": setup,
        "lane": _swing_scout_lane(row, asset, components),
        "swing_scout_score": int(round(_clamp(raw_score, 0.0, 100.0))),
        "market_cap_bucket": cap_profile.get("bucket"),
        "market_cap": _clean_value(cap_profile.get("market_cap")),
        "squeeze_score": components.get("squeeze"),
        "attention_score": components.get("attention"),
        "momentum_score": components.get("momentum"),
        "value_score_component": components.get("value"),
        "execution_score": components.get("execution"),
        "readiness_score": readiness.get("readiness_score"),
        "readiness_label": readiness.get("readiness_label"),
        "confidence": _clean_value(row.get("confidence")),
        "rank_score": _clean_value(row.get("rank_score")),
        "trade_status": _clean_value(row.get("trade_status") or ("Trade" if bool(row.get("actionable")) else "Review")),
        "entry_price": _setup_entry_price(row, asset),
        "stop_price": _clean_value(row.get("stop_price")),
        "target_price": _clean_value(row.get("target_price")),
        "size": _setup_size(row, asset),
        "dte": _clean_value(row.get("dte")),
        "expiry": _clean_value(row.get("expiry")),
        "spread_pct": _clean_value(row.get("spread_pct")),
        "short_pct_of_float": _clean_value(row.get("short_pct_of_float")),
        "short_vol_ratio": _clean_value(row.get("short_vol_ratio")),
        "social_score": _clean_value(row.get("social_score")),
        "gtrends_score": _clean_value(row.get("gtrends_score")),
        "tech_score": _clean_value(row.get("tech_score")),
        "futures_score": _clean_value(row.get("futures_score")),
        "ret_20d": _clean_value(row.get("ret_20d") or row.get("ticker_ret_20d")),
        "quality": _setup_quality(row, asset),
        "factor_breakdown": factor_breakdown,
        "factor_summary": _swing_factor_summary(factor_breakdown),
        "reasons": reasons,
        "warnings": warnings,
        "source_file": _clean_value(row.get("_source_file") or source_file),
        "snapshot_freshness": _clean_value(row.get("snapshot_freshness")),
        "snapshot_age_min": _clean_value(row.get("snapshot_age_min")),
    }


def _swing_scout_query_text(row: dict[str, Any]) -> str:
    parts = [
        row.get("asset"),
        row.get("ticker_or_symbol"),
        row.get("setup"),
        row.get("lane"),
        row.get("market_cap_bucket"),
        row.get("trade_status"),
        row.get("quality"),
    ]
    parts.extend(row.get("reasons") or [])
    parts.extend(row.get("warnings") or [])
    return " ".join(str(part or "") for part in parts).lower()


def _nasdaq_mover_scout_record(row: pd.Series) -> dict[str, Any]:
    symbol = str(row.get("symbol") or "").strip().upper()
    pct = _float_value(row.get("pct_change"), default=0.0)
    volume = _float_value(row.get("volume"), default=0.0)
    cap = _float_value(row.get("market_cap"), default=0.0)
    short_vol_ratio = _float_value(row.get("short_vol_ratio"), default=math.nan)
    dark_pool_score = _float_value(row.get("dark_pool_score"), default=math.nan)
    short_pressure_bonus = 0
    if math.isfinite(short_vol_ratio):
        if short_vol_ratio >= 0.60:
            short_pressure_bonus = 7
        elif short_vol_ratio >= 0.55:
            short_pressure_bonus = 4
        elif short_vol_ratio <= 0.35:
            short_pressure_bonus = -2
    direction = "upside momentum" if pct >= 0 else "downside volatility"
    reasons = [
        f"Nasdaq screener {direction}",
        f"{pct:+.1f}% move",
        f"{volume:,.0f} volume",
    ]
    if cap > 0:
        reasons.append(f"${cap / 1_000_000:.0f}M market cap")
    if math.isfinite(short_vol_ratio):
        reasons.append(f"FINRA short-volume {short_vol_ratio * 100:.0f}%")
    warnings = [
        "free/delayed screener row",
        "run focused scan before acting",
    ]
    if pct < 0:
        warnings.append("red mover: confirm reversal thesis")
    if math.isfinite(short_vol_ratio) and short_vol_ratio >= 0.60:
        warnings.append("heavy short-volume pressure")
    base_score = _float_value(row.get("nasdaq_mover_score"), default=0.0) + short_pressure_bonus
    factor_breakdown = [
        {
            "factor": "Momentum",
            "score": round(abs(pct), 2),
            "detail": f"{pct:+.1f}% move, {volume:,.0f} volume",
        },
    ]
    if math.isfinite(short_vol_ratio):
        factor_breakdown.append({
            "factor": "Short volume",
            "score": round(max(short_pressure_bonus, 0), 2),
            "detail": f"FINRA short-volume {short_vol_ratio * 100:.0f}%",
        })
    if cap > 0:
        factor_breakdown.append({
            "factor": "Market cap",
            "score": 8.0 if cap < 2_000_000_000 else 3.0,
            "detail": f"${cap / 1_000_000:.0f}M market cap",
        })
    factor_breakdown = sorted(
        factor_breakdown,
        key=lambda item: _float_value(item.get("score"), default=0.0),
        reverse=True,
    )[:5]
    return {
        "asset": "share",
        "ticker_or_symbol": symbol,
        "setup": f"{symbol} small-cap mover",
        "lane": "nasdaq_small_cap_mover",
        "swing_scout_score": int(round(_clamp(base_score, 0.0, 100.0))),
        "market_cap_bucket": _clean_value(row.get("market_cap_bucket")),
        "market_cap": _clean_value(row.get("market_cap")),
        "squeeze_score": _clean_value(
            round(max(short_pressure_bonus, 0), 2) if math.isfinite(short_vol_ratio) else None
        ),
        "attention_score": None,
        "momentum_score": _clean_value(abs(pct)),
        "value_score_component": None,
        "execution_score": None,
        "readiness_score": None,
        "readiness_label": "review",
        "confidence": None,
        "rank_score": None,
        "trade_status": "Review",
        "entry_price": _clean_value(row.get("last_price")),
        "stop_price": None,
        "target_price": None,
        "size": None,
        "dte": None,
        "expiry": None,
        "spread_pct": None,
        "short_pct_of_float": None,
        "short_vol_ratio": _clean_value(short_vol_ratio if math.isfinite(short_vol_ratio) else None),
        "dark_pool_score": _clean_value(dark_pool_score if math.isfinite(dark_pool_score) else None),
        "social_score": None,
        "gtrends_score": None,
        "tech_score": None,
        "futures_score": None,
        "ret_20d": None,
        "quality": "live-radar review",
        "pct_change": _clean_value(row.get("pct_change")),
        "volume": _clean_value(row.get("volume")),
        "sector": _clean_value(row.get("sector")),
        "industry": _clean_value(row.get("industry")),
        "name": _clean_value(row.get("name")),
        "factor_breakdown": factor_breakdown,
        "factor_summary": _swing_factor_summary(factor_breakdown),
        "reasons": reasons[:6],
        "warnings": warnings[:6],
        "source_file": "nasdaq_screener",
        "snapshot_freshness": "live_public_delayed",
        "snapshot_age_min": 0,
    }


def _append_nasdaq_mover_scout_rows(
    rows: list[dict[str, Any]],
    *,
    asset: str,
    query_norm: str,
    lane: str,
    min_score: float,
    include_wait: bool,
    max_rows: int = 24,
) -> tuple[int, str | None]:
    if asset not in {"all", "share"}:
        return 0, None
    try:
        movers = small_cap_movers(max_rows=max_rows)
    except Exception:
        return 0, None
    if movers.empty:
        return 0, None
    mover_symbols = [
        str(symbol or "").strip().upper()
        for symbol in movers.get("symbol", pd.Series(dtype=str)).tolist()
        if str(symbol or "").strip()
    ]
    try:
        short_flow = dark_pool_engine.run(mover_symbols, lookback_days=3)
    except Exception:
        short_flow = pd.DataFrame()
    if isinstance(short_flow, pd.DataFrame) and not short_flow.empty and "ticker" in short_flow.columns:
        enrich_cols = [
            col for col in ("ticker", "short_vol_ratio", "short_vol", "total_vol", "dark_pool_score")
            if col in short_flow.columns
        ]
        if len(enrich_cols) > 1:
            movers = movers.merge(
                short_flow[enrich_cols].rename(columns={"ticker": "symbol"}),
                on="symbol",
                how="left",
            )
    added = 0
    seen_symbols = {str(row.get("ticker_or_symbol") or "").upper() for row in rows}
    for _, mover in movers.iterrows():
        record = _nasdaq_mover_scout_record(mover)
        symbol = str(record.get("ticker_or_symbol") or "").upper()
        if not symbol or symbol in seen_symbols:
            continue
        if record["swing_scout_score"] < min_score:
            continue
        if not include_wait and str(record.get("readiness_label") or "").lower() == "wait":
            continue
        if lane != "all" and str(record.get("lane") or "").lower() != lane:
            continue
        if query_norm and query_norm not in _swing_scout_query_text(record):
            continue
        rows.append(record)
        seen_symbols.add(symbol)
        added += 1
    return added, "nasdaq_screener"


def build_swing_scout(
    data_dir: Path = DATA_DIR,
    limit: int = 18,
    asset: str = "all",
    query: str = "",
    lane: str = "all",
    min_score: float = 45.0,
    include_wait: bool = True,
    include_nasdaq_movers: bool = False,
) -> dict[str, Any]:
    """Surface speculative small/mid-cap and futures swing candidates from local free-factor outputs."""
    limit = max(1, min(int(limit or 18), 60))
    asset = str(asset or "all").strip().lower()
    if asset not in {"all", "option", "share", "value", "futures"}:
        asset = "all"
    lane = str(lane or "all").strip().lower()
    query_norm = str(query or "").strip().lower()
    min_score = _clamp(_float_value(min_score, default=45.0), 0.0, 100.0)
    rows: list[dict[str, Any]] = []
    asset_counts: dict[str, int] = {}
    source_files: dict[str, str | None] = {}
    reviewed_count = 0
    for asset_name in ("option", "share", "value", "futures"):
        if asset != "all" and asset_name != asset:
            continue
        spec = OPPORTUNITY_SPECS[asset_name]
        path = _latest_file(data_dir, spec["pattern"])
        source_file = path.name if path else None
        source_files[asset_name] = source_file
        df = _read_parquet(path)
        if df.empty:
            continue
        out = df.copy()
        out["asset"] = asset_name
        out["actionable"] = out.apply(_is_actionable, axis=1)
        reviewed_count += int(len(out))
        if asset_name == "option" and "dte" in out.columns:
            out = out[pd.to_numeric(out["dte"], errors="coerce").fillna(0.0) >= MIN_SWING_OPTION_DTE]
        for _, row in out.iterrows():
            record = _swing_scout_record(row, asset_name, source_file)
            if record["swing_scout_score"] < min_score:
                continue
            if not include_wait and str(record.get("readiness_label") or "").lower() == "wait":
                continue
            if lane != "all" and str(record.get("lane") or "").lower() != lane:
                continue
            if query_norm and query_norm not in _swing_scout_query_text(record):
                continue
            rows.append(record)
            asset_counts[asset_name] = asset_counts.get(asset_name, 0) + 1
    nasdaq_added = 0
    nasdaq_source = None
    if include_nasdaq_movers:
        nasdaq_added, nasdaq_source = _append_nasdaq_mover_scout_rows(
            rows,
            asset=asset,
            query_norm=query_norm,
            lane=lane,
            min_score=min_score,
            include_wait=include_wait,
        )
        if nasdaq_added:
            asset_counts["share"] = asset_counts.get("share", 0) + nasdaq_added
            source_files["nasdaq_movers"] = nasdaq_source
    rows = sorted(
        rows,
        key=lambda row: (
            _float_value(row.get("swing_scout_score")),
            _float_value(row.get("readiness_score")),
            _float_value(row.get("confidence")),
        ),
        reverse=True,
    )[:limit]
    lane_counts: dict[str, int] = {}
    for row in rows:
        lane_name = str(row.get("lane") or "unknown")
        lane_counts[lane_name] = lane_counts.get(lane_name, 0) + 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(rows),
        "reviewed_count": reviewed_count,
        "rows": rows,
        "asset_counts": asset_counts,
        "lane_counts": lane_counts,
        "sources": source_files,
        "min_option_dte": MIN_SWING_OPTION_DTE,
        "filters": {
            "asset": asset,
            "query": query,
            "lane": lane,
            "min_score": min_score,
            "include_wait": bool(include_wait),
            "include_nasdaq_movers": bool(include_nasdaq_movers),
            "limit": limit,
        },
        "notes": [
            "Swing scout is a read-only ranking of high-upside review candidates from existing Optedge snapshots.",
            "When enabled from the cockpit, Nasdaq's public screener adds a capped no-key small-cap mover radar.",
            "Equity-linked rows emphasize small/mid-cap asymmetry, short pressure, retail attention, momentum, value, and execution quality.",
            "Futures rows use the futures/macro momentum lane because market-cap logic does not apply to futures contracts.",
            "Options under 90 DTE are excluded from this scout; use the option-chain panel for exact contract review.",
        ],
    }


def _suggestion_text(row: dict[str, Any]) -> str:
    return " ".join(str(row.get(key) or "") for key in ("symbol", "label", "name", "query", "source")).lower()


def _add_suggestion(
    rows: list[dict[str, Any]],
    seen: set[tuple[str, str, str]],
    symbol: Any,
    label: str,
    kind: str,
    source: str,
    query: str | None = None,
    name: Any = None,
    score: Any = None,
    trade_status: Any = None,
) -> None:
    clean_symbol = str(symbol or "").strip().upper()
    clean_label = str(label or clean_symbol).strip()
    clean_query = str(query or clean_symbol).strip()
    if not clean_symbol or not clean_query:
        return
    key = (kind, clean_symbol, clean_query.upper())
    if key in seen:
        return
    seen.add(key)
    rows.append({
        "symbol": clean_symbol,
        "label": clean_label,
        "kind": kind,
        "source": source,
        "query": clean_query,
        "name": _clean_value(name),
        "score": _clean_value(score),
        "trade_status": _clean_value(trade_status),
    })


def _option_query_from_row(row: pd.Series) -> str:
    ticker = str(row.get("ticker") or "").strip().upper()
    expiry = str(row.get("expiry") or "").strip()
    side_raw = str(row.get("side") or "").strip().upper()
    side = "C" if side_raw.startswith("C") else "P" if side_raw.startswith("P") else side_raw[:1]
    strike = row.get("strike")
    if not ticker or not expiry or not side or strike in (None, ""):
        return ticker
    try:
        strike_text = f"{float(strike):g}"
    except Exception:
        strike_text = str(strike)
    return f"{ticker} {expiry} {side} {strike_text}"


def build_symbol_suggestions(
    data_dir: Path = DATA_DIR,
    query: str = "",
    limit: int = 16,
) -> dict[str, Any]:
    """Suggest local tickers/contracts from current Optedge artifacts."""
    query_norm = str(query or "").strip().lower()
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for alias, (symbol, name) in sorted(COMMON_ALIASES.items()):
        _add_suggestion(
            rows, seen, symbol, f"{symbol} - {name}", "alias", "built-in aliases",
            query=symbol, name=name, score=0.25,
        )
        if alias != symbol.lower():
            _add_suggestion(
                rows, seen, symbol, f"{alias.title()} -> {symbol}", "alias",
                "built-in aliases", query=alias, name=name, score=0.2,
            )

    for asset_name, spec in OPPORTUNITY_SPECS.items():
        df = _read_parquet(_latest_file(data_dir, spec["pattern"]))
        if df.empty:
            continue
        symbol_col = str(spec["symbol_col"])
        if symbol_col not in df.columns:
            continue
        for _, row in df.head(600).iterrows():
            symbol = row.get(symbol_col)
            status = row.get("trade_status")
            score = _opportunity_score(row)
            if asset_name == "option":
                option_query = _option_query_from_row(row)
                side = str(row.get("side") or "").upper()[:1]
                label = f"{symbol} {side} {row.get('strike', '-')} {row.get('expiry', '-')}"
                _add_suggestion(
                    rows, seen, symbol, label, "option", "latest options",
                    query=option_query, score=score, trade_status=status,
                )
            elif asset_name == "futures":
                name = row.get("name")
                direction = str(row.get("direction") or "").upper()
                contract = str(row.get("contract") or "")
                label = f"{symbol} {direction} {contract}".strip()
                if name:
                    label = f"{label} - {name}"
                _add_suggestion(
                    rows, seen, symbol, label, "futures", "latest futures",
                    query=str(symbol or ""), name=name, score=score, trade_status=status,
                )
            else:
                label = f"{symbol} {asset_name}"
                _add_suggestion(
                    rows, seen, symbol, label, asset_name, f"latest {asset_name}",
                    query=str(symbol or ""), score=score, trade_status=status,
                )

    for asset_name, filename in POSITION_FILES.items():
        raw = _read_json(data_dir / filename)
        if not isinstance(raw, list):
            continue
        for item in raw[:800]:
            if not isinstance(item, dict):
                continue
            normalized = _normalize_position(item, asset_name)
            symbol = normalized.get("ticker_or_symbol")
            label = normalized.get("position_label") or str(symbol or "")
            _add_suggestion(
                rows, seen, symbol, label, f"open_{asset_name}",
                "open positions", query=str(symbol or ""), score=0.5,
                trade_status=normalized.get("trade_status"),
            )

    for item in load_watchlist(data_dir).get("entries", []):
        _add_suggestion(
            rows, seen, item.get("symbol"), str(item.get("query") or item.get("symbol") or ""),
            "watchlist", "research watchlist", query=str(item.get("query") or item.get("symbol") or ""),
            name=item.get("name"), score=0.75,
        )

    if len(query_norm) >= 2:
        for item in sec_company_search(query, limit=limit, fetch_if_stale=False):
            _add_suggestion(
                rows, seen, item.get("symbol"),
                f"{item.get('symbol')} - {item.get('name') or 'SEC company'}",
                "sec", "SEC company tickers", query=str(item.get("symbol") or ""),
                name=item.get("name"), score=item.get("score"),
            )
        for item in nasdaq_symbol_search(query, limit=limit, fetch_if_stale=False):
            type_label = str(item.get("type") or "symbol").lower()
            _add_suggestion(
                rows, seen, item.get("symbol"),
                f"{item.get('symbol')} - {item.get('name') or 'Nasdaq symbol'}",
                "nasdaq", "Nasdaq Trader symbol directory", query=str(item.get("symbol") or ""),
                name=item.get("name"), score=item.get("score"), trade_status=type_label,
            )

    if query_norm:
        rows = [row for row in rows if query_norm in _suggestion_text(row)]

    def sort_key(row: dict[str, Any]) -> tuple[int, int, float, str]:
        text = _suggestion_text(row)
        symbol = str(row.get("symbol") or "").lower()
        prefix = 1 if query_norm and (symbol.startswith(query_norm) or text.startswith(query_norm)) else 0
        exact = 1 if query_norm and (symbol == query_norm or str(row.get("query") or "").lower() == query_norm) else 0
        return (exact, prefix, _float_value(row.get("score")), str(row.get("symbol") or ""))

    rows = sorted(rows, key=sort_key, reverse=True)[:limit]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "query": query,
        "count": len(rows),
        "rows": rows,
        "notes": [
            "Suggestions are built from local scan snapshots, open positions, watchlist entries, built-in aliases, SEC tickers, and Nasdaq Trader's free symbol directory.",
            "Selecting a suggestion only fills or runs local research; it does not place trades.",
        ],
    }


def _records_from_frame(df: pd.DataFrame, limit: int = 100) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    records: list[dict[str, Any]] = []
    for _, row in df.head(limit).iterrows():
        records.append({str(k): _clean_value(v) for k, v in row.to_dict().items()})
    return records


def _split_reason_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_split_reason_values(item))
        return out
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return []
    return [part.strip() for part in text.split(";") if part.strip()]


def _reason_counts_from_frame(df: pd.DataFrame, column: str = "reason_excluded") -> dict[str, int]:
    if df is None or df.empty or column not in df.columns:
        return {}
    counts: dict[str, int] = {}
    for value in df[column].tolist():
        for reason in _split_reason_values(value):
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _top_reason_rows(reason_counts: dict[str, int], limit: int = 6) -> list[dict[str, Any]]:
    return [
        {"reason": reason, "count": count}
        for reason, count in list(reason_counts.items())[: max(1, int(limit or 6))]
    ]


def build_paper_candidates(
    data_dir: Path = DATA_DIR,
    max_new: int = 5,
    max_open: int = 30,
    include_watch: bool = False,
    allow_zero_size_placeholder: bool = False,
    asset: str = "all",
    dry_run: bool = False,
    write: bool = False,
    query: str = "",
) -> dict[str, Any]:
    """Build or write the compact external paper tracking candidate list."""
    df = build_external_orders(
        data_dir=data_dir,
        max_new=max_new,
        max_open=max_open,
        include_watch=include_watch,
        allow_zero_size_placeholder=allow_zero_size_placeholder,
        asset=asset,
        dry_run=dry_run,
        query=query,
    )
    paths: dict[str, str] = {}
    if write and not dry_run:
        csv_path, json_path = write_paper_outputs(df, data_dir)
        paths = {"csv": str(csv_path), "json": str(json_path)}
    selected_count = 0
    excluded_count = 0
    if not df.empty and "reason_excluded" in df.columns:
        excluded_mask = df["reason_excluded"].astype(str).str.len() > 0
        excluded_count = int(excluded_mask.sum())
        selected_count = int((~excluded_mask).sum())
    else:
        selected_count = int(len(df))
    reason_counts = _reason_counts_from_frame(df)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "asset": asset,
        "query": query,
        "max_new": max_new,
        "max_open": max_open,
        "include_watch": include_watch,
        "allow_zero_size_placeholder": allow_zero_size_placeholder,
        "dry_run": dry_run,
        "wrote_files": bool(paths),
        "paths": paths,
        "count": int(len(df)),
        "selected_count": selected_count,
        "excluded_count": excluded_count,
        "rejection_reason_counts": reason_counts,
        "top_rejection_reasons": _top_reason_rows(reason_counts),
        "rows": _records_from_frame(df, limit=150),
        "notes": [
            "External paper candidates are a small filtered subset, not every internal signal.",
            "Use the filter box to preview candidates for one ticker, futures symbol, or option contract.",
            "This creates manual paper-tracking files only; no trades are placed.",
            "Dry-run review includes rejected rows and exclusion reasons.",
        ],
    }


def build_robinhood_agentic_queue_report(
    data_dir: Path = DATA_DIR,
    account_budget: float = 500.0,
    max_candidates: int = 5,
    max_orders: int = 2,
    min_dte: int = 180,
    min_confidence: float = 55.0,
    query: str = "",
    write: bool = False,
) -> dict[str, Any]:
    """Build or write the long-dated option candidate queue for agent review."""
    queue = build_robinhood_queue(
        data_dir=data_dir,
        account_budget=account_budget,
        max_candidates=max_candidates,
        max_orders=max_orders,
        min_dte=min_dte,
        min_confidence=min_confidence,
        query=query,
    )
    paths: dict[str, str] = {}
    if write:
        queue_path, prompt_path = write_robinhood_queue_outputs(queue, data_dir)
        paths = {"queue": str(queue_path), "prompt": str(prompt_path)}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "wrote_files": bool(paths),
        "paths": paths,
        "status": queue.get("status"),
        "account_budget": queue.get("account_budget"),
        "max_candidates": queue.get("max_candidates"),
        "max_orders_to_submit": queue.get("max_orders_to_submit"),
        "max_total_premium": queue.get("max_total_premium"),
        "max_premium_per_order": queue.get("max_premium_per_order"),
        "min_dte": queue.get("min_dte"),
        "min_confidence": queue.get("min_confidence"),
        "estimated_total_candidate_premium": queue.get("estimated_total_candidate_premium"),
        "readiness": queue.get("readiness") or {},
        "rejection_reason_counts": queue.get("rejection_reason_counts") or {},
        "top_rejection_reasons": queue.get("top_rejection_reasons") or [],
        "candidate_count": len(queue.get("orders") or []),
        "rejected_count": len(queue.get("rejected") or []),
        "orders": queue.get("orders") or [],
        "rejected": (queue.get("rejected") or [])[:25],
        "notes": [
            "This is a long-dated options handoff queue for an external agent.",
            "It does not place trades or store broker credentials.",
            "The agent should verify live quotes, spread, positions, buying power, and current news.",
        ],
    }


def build_opportunities(
    data_dir: Path = DATA_DIR,
    asset: str = "all",
    query: str = "",
    status: str = "all",
    min_confidence: float = 0.0,
    limit: int = 80,
) -> dict[str, Any]:
    selected = list(OPPORTUNITY_SPECS) if asset == "all" else [asset]
    query_norm = str(query or "").strip().upper()
    status_norm = str(status or "all").strip().lower()
    rows: list[pd.DataFrame] = []
    sources: dict[str, str | None] = {}

    for asset_name in selected:
        spec = OPPORTUNITY_SPECS.get(asset_name)
        if spec is None:
            continue
        path = _latest_file(data_dir, spec["pattern"])
        sources[asset_name] = path.name if path else None
        df = _read_parquet(path)
        if df.empty:
            continue
        out = df.copy()
        out["asset"] = asset_name
        out["actionable"] = out.apply(_is_actionable, axis=1)
        out["_opportunity_score"] = out.apply(_opportunity_score, axis=1)
        if "confidence" in out.columns:
            out = out[pd.to_numeric(out["confidence"], errors="coerce").fillna(0.0) >= min_confidence]
        elif min_confidence > 0:
            out = out.iloc[0:0]
        if query_norm:
            symbol_col = str(spec["symbol_col"])
            symbol_match = (
                out[symbol_col].astype(str).str.upper().str.contains(query_norm, na=False, regex=False)
                if symbol_col in out.columns else pd.Series(False, index=out.index)
            )
            headline_match = (
                out["top_headline"].astype(str).str.upper().str.contains(query_norm, na=False, regex=False)
                if "top_headline" in out.columns else pd.Series(False, index=out.index)
            )
            out = out[symbol_match | headline_match]
        if status_norm == "actionable":
            out = out[out["actionable"]]
        elif status_norm != "all" and "trade_status" in out.columns:
            out = out[out["trade_status"].astype(str).str.lower() == status_norm]
        rows.append(out)

    if rows:
        combined = pd.concat(rows, ignore_index=True, sort=False)
        combined = combined.sort_values("_opportunity_score", ascending=False, kind="mergesort")
    else:
        combined = pd.DataFrame()

    records = []
    for asset_name in OPPORTUNITY_SPECS:
        part = combined[combined["asset"] == asset_name] if "asset" in combined.columns else pd.DataFrame()
        records.extend(_opportunity_records(part, asset_name, limit))
    records = sorted(records, key=lambda r: _float_value(r.get("rank_score") or r.get("fused_score") or r.get("futures_score") or r.get("value_score")), reverse=True)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "asset": asset,
        "query": query,
        "status": status,
        "min_confidence": min_confidence,
        "count": len(records[:limit]),
        "sources": sources,
        "rows": records[:limit],
        "notes": [
            "Explorer reads the latest local top_* parquet snapshots.",
            "Actionable excludes Watch/Skip where sizing fields are present.",
            "This is research output only; no orders are placed.",
        ],
    }


def build_positions(
    data_dir: Path = DATA_DIR,
    asset: str = "all",
    query: str = "",
    status: str = "all",
    limit: int = 250,
) -> dict[str, Any]:
    selected = list(POSITION_FILES) if asset == "all" else [asset]
    query_norm = str(query or "").strip().upper()
    status_norm = str(status or "all").strip().lower()
    rows: list[dict[str, Any]] = []
    sources: dict[str, str | None] = {}

    for asset_name in selected:
        filename = POSITION_FILES.get(asset_name)
        if not filename:
            continue
        path = data_dir / filename
        raw = _read_json(path)
        sources[asset_name] = filename if path.exists() else None
        if not isinstance(raw, list):
            continue
        for item in raw:
            if isinstance(item, dict):
                rows.append(_normalize_position(item, asset_name))

    rows = _dedupe_position_rows(rows)
    if query_norm:
        rows = [
            row for row in rows
            if query_norm in str(row.get("ticker_or_symbol") or "").upper()
            or query_norm in str(row.get("position_label") or "").upper()
        ]
    if status_norm == "attention":
        rows = [row for row in rows if row.get("attention")]
    elif status_norm != "all":
        rows = [
            row for row in rows
            if str(row.get("trade_status") or "").strip().lower() == status_norm
            or str(row.get("latest_exit_action") or "").strip().lower() == status_norm
        ]
    rows = sorted(rows, key=_position_sort_key, reverse=True)
    limited = rows[:limit]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "asset": asset,
        "query": query,
        "status": status,
        "count": len(limited),
        "total_before_limit": len(rows),
        "sources": sources,
        "rows": limited,
        "notes": [
            "Position monitor reads current open position state only.",
            "Attention means high exit pressure, repeated repricing trouble, or a large unrealized drawdown.",
            "This is research output only; no orders are placed.",
        ],
    }


def _exit_review_symbol(row: dict[str, Any]) -> str:
    return str(row.get("ticker") or row.get("symbol") or row.get("ticker_or_symbol") or "-").upper()


def _exit_review_position_key(row: dict[str, Any]) -> str:
    position_id = str(row.get("position_id") or "").strip()
    if position_id:
        return position_id
    return f"{row.get('asset') or 'unknown'}:{row.get('ticker_or_symbol') or '-'}"


def _exit_action_priority(action: Any) -> int:
    clean = str(action or "").strip().lower()
    if clean in {"hard_stop", "expired"}:
        return 100
    if clean in {"hard_target", "close_early"}:
        return 90
    if clean == "tighten_stop":
        return 75
    if clean == "watch":
        return 55
    if clean == "hold":
        return 20
    return 30


def _read_exit_review_rows(data_dir: Path = DATA_DIR) -> list[dict[str, Any]]:
    path = Path(data_dir) / "exit_reviews.jsonl"
    if not path.exists() or not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except Exception:
        return []
    for line in lines:
        clean = line.strip()
        if not clean:
            continue
        try:
            row = json.loads(clean)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def build_exit_review_summary(
    data_dir: Path = DATA_DIR,
    asset: str = "all",
    query: str = "",
    limit: int = 80,
) -> dict[str, Any]:
    """Summarize dynamic exit-review decisions logged by the lifecycle trackers."""
    asset_norm = str(asset or "all").strip().lower()
    if asset_norm not in {"all", "option", "share", "futures"}:
        asset_norm = "all"
    query_norm = str(query or "").strip().upper()
    limit = max(1, min(int(limit or 80), 500))
    raw_rows = _read_exit_review_rows(data_dir)
    rows: list[dict[str, Any]] = []

    for raw in raw_rows:
        row_asset = str(raw.get("asset") or "unknown").strip().lower()
        symbol = _exit_review_symbol(raw)
        if asset_norm != "all" and row_asset != asset_norm:
            continue
        position_id = str(raw.get("position_id") or "")
        if query_norm and query_norm not in symbol and query_norm not in position_id.upper():
            continue
        reasons = raw.get("reasons") if isinstance(raw.get("reasons"), list) else []
        row = {
            "timestamp": _clean_value(raw.get("timestamp")),
            "asset": row_asset,
            "ticker_or_symbol": symbol,
            "position_id": _clean_value(raw.get("position_id")),
            "action": _clean_value(raw.get("action") or "unknown"),
            "exit_pressure": _clean_value(raw.get("exit_pressure")),
            "current_pnl_pct": _clean_value(raw.get("current_pnl_pct")),
            "current_pnl_dollars": _clean_value(raw.get("current_pnl_dollars")),
            "current_price": _clean_value(raw.get("current_price")),
            "old_stop": _clean_value(raw.get("old_stop")),
            "new_stop": _clean_value(raw.get("new_stop")),
            "old_target": _clean_value(raw.get("old_target")),
            "new_target": _clean_value(raw.get("new_target")),
            "entry_confidence": _clean_value(raw.get("entry_confidence")),
            "current_confidence": _clean_value(raw.get("current_confidence")),
            "used_learned_policy": bool(raw.get("used_learned_policy", False)),
            "policy_version": _clean_value(raw.get("policy_version")),
            "reasons": [_clean_value(reason) for reason in reasons[:6]],
            "reasons_text": "; ".join(str(reason) for reason in reasons[:4] if reason) or "-",
        }
        rows.append(row)

    rows = sorted(rows, key=lambda row: str(row.get("timestamp") or ""), reverse=True)
    action_counts: dict[str, int] = {}
    asset_counts: dict[str, int] = {}
    symbol_rollup: dict[str, dict[str, Any]] = {}
    latest_by_position: dict[str, dict[str, Any]] = {}
    pressures: list[float] = []
    learned_count = 0
    high_pressure_count = 0
    for row in rows:
        action = str(row.get("action") or "unknown")
        row_asset = str(row.get("asset") or "unknown")
        symbol = str(row.get("ticker_or_symbol") or "-").upper()
        pressure = _float_value(row.get("exit_pressure"), default=math.nan)
        action_counts[action] = action_counts.get(action, 0) + 1
        asset_counts[row_asset] = asset_counts.get(row_asset, 0) + 1
        if row.get("used_learned_policy"):
            learned_count += 1
        if math.isfinite(pressure):
            pressures.append(pressure)
            if pressure >= 80:
                high_pressure_count += 1
        sym = symbol_rollup.setdefault(symbol, {
            "ticker_or_symbol": symbol,
            "asset": row_asset,
            "review_count": 0,
            "latest_action": action,
            "latest_timestamp": row.get("timestamp"),
            "max_exit_pressure": None,
            "latest_reasons": row.get("reasons_text"),
        })
        sym["review_count"] += 1
        current_max = _float_value(sym.get("max_exit_pressure"), default=math.nan)
        if math.isfinite(pressure) and (not math.isfinite(current_max) or pressure > current_max):
            sym["max_exit_pressure"] = pressure
        if str(row.get("timestamp") or "") >= str(sym.get("latest_timestamp") or ""):
            sym["latest_action"] = action
            sym["latest_timestamp"] = row.get("timestamp")
            sym["latest_reasons"] = row.get("reasons_text")
            sym["asset"] = row_asset
        position_key = _exit_review_position_key(row)
        current = latest_by_position.get(position_key)
        if current is None or str(row.get("timestamp") or "") >= str(current.get("timestamp") or ""):
            latest_by_position[position_key] = dict(row, decision_key=position_key)

    by_symbol = sorted(
        symbol_rollup.values(),
        key=lambda row: (
            _float_value(row.get("max_exit_pressure"), default=0.0),
            int(row.get("review_count") or 0),
            str(row.get("latest_timestamp") or ""),
        ),
        reverse=True,
    )[:15]
    current_decisions = []
    for row in latest_by_position.values():
        action = str(row.get("action") or "unknown")
        decision = {
            "decision_key": _clean_value(row.get("decision_key")),
            "timestamp": _clean_value(row.get("timestamp")),
            "asset": _clean_value(row.get("asset")),
            "ticker_or_symbol": _clean_value(row.get("ticker_or_symbol")),
            "position_id": _clean_value(row.get("position_id")),
            "latest_action": _clean_value(action),
            "exit_pressure": _clean_value(row.get("exit_pressure")),
            "current_pnl_pct": _clean_value(row.get("current_pnl_pct")),
            "current_pnl_dollars": _clean_value(row.get("current_pnl_dollars")),
            "current_price": _clean_value(row.get("current_price")),
            "old_stop": _clean_value(row.get("old_stop")),
            "new_stop": _clean_value(row.get("new_stop")),
            "used_learned_policy": bool(row.get("used_learned_policy", False)),
            "policy_version": _clean_value(row.get("policy_version")),
            "reasons_text": _clean_value(row.get("reasons_text")),
            "decision_priority": _exit_action_priority(action),
        }
        current_decisions.append(decision)
    current_decisions = sorted(
        current_decisions,
        key=lambda row: (
            _exit_action_priority(row.get("latest_action")),
            _float_value(row.get("exit_pressure"), default=0.0),
            str(row.get("timestamp") or ""),
        ),
        reverse=True,
    )
    current_action_counts: dict[str, int] = {}
    for row in current_decisions:
        action = str(row.get("latest_action") or "unknown")
        current_action_counts[action] = current_action_counts.get(action, 0) + 1
    avg_pressure = (sum(pressures) / len(pressures)) if pressures else None
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "asset": asset_norm,
        "query": query,
        "source": "exit_reviews.jsonl" if (Path(data_dir) / "exit_reviews.jsonl").exists() else None,
        "count": len(rows[:limit]),
        "total_before_limit": len(rows),
        "action_counts": action_counts,
        "asset_counts": asset_counts,
        "high_pressure_count": high_pressure_count,
        "learned_policy_count": learned_count,
        "current_decision_count": len(current_decisions),
        "current_high_pressure_count": sum(
            _float_value(row.get("exit_pressure"), default=0.0) >= 80 for row in current_decisions
        ),
        "current_action_counts": current_action_counts,
        "avg_exit_pressure": _clean_value(round(avg_pressure, 2) if avg_pressure is not None else None),
        "latest_timestamp": rows[0].get("timestamp") if rows else None,
        "by_symbol": [{k: _clean_value(v) for k, v in row.items()} for row in by_symbol],
        "current_decisions": [{k: _clean_value(v) for k, v in row.items()} for row in current_decisions[:25]],
        "rows": [{k: _clean_value(v) for k, v in row.items()} for row in rows[:limit]],
        "notes": [
            "Exit reviews are local lifecycle decisions logged after every scan.",
            "Hard stops, targets, and expirations still take priority over dynamic review actions.",
            "This panel is decision support only and does not place trades.",
        ],
    }


def _history_close_series(df: pd.DataFrame) -> pd.Series:
    if df is None or df.empty or "Close" not in df.columns:
        return pd.Series(dtype="float64")
    close = pd.to_numeric(df["Close"], errors="coerce").dropna()
    return close[close > 0]


def _history_last_date(df: pd.DataFrame, close: pd.Series) -> str | None:
    if close.empty:
        return None
    idx = close.index[-1]
    if hasattr(idx, "date"):
        try:
            return str(idx.date())
        except Exception:
            pass
    if df is not None and not df.empty and "Date" in df.columns:
        try:
            return str(df.loc[idx, "Date"])
        except Exception:
            try:
                return str(df["Date"].dropna().iloc[-1])
            except Exception:
                return None
    return None


def _period_return(close: pd.Series, periods: int) -> float | None:
    if len(close) <= periods:
        return None
    end = _float_value(close.iloc[-1], default=math.nan)
    start = _float_value(close.iloc[-1 - periods], default=math.nan)
    if not (math.isfinite(end) and math.isfinite(start)) or start <= 0:
        return None
    return (end / start) - 1.0


def _realized_vol_20d(close: pd.Series) -> float | None:
    if len(close) < 22:
        return None
    returns = close.pct_change().dropna().tail(20)
    if returns.empty:
        return None
    vol = float(returns.std()) * math.sqrt(252.0)
    return vol if math.isfinite(vol) else None


def _market_trend_label(ret_5d: float | None, ret_20d: float | None) -> str:
    r5 = ret_5d if ret_5d is not None else 0.0
    r20 = ret_20d if ret_20d is not None else 0.0
    if r5 > 0.01 and r20 > 0.02:
        return "uptrend"
    if r5 < -0.01 and r20 < -0.02:
        return "downtrend"
    if r20 > 0.02:
        return "constructive"
    if r20 < -0.02:
        return "weak"
    return "mixed"


def _market_pulse_row(spec: dict[str, Any], history: pd.DataFrame) -> dict[str, Any]:
    close = _history_close_series(history)
    if close.empty:
        return {
            "symbol": spec["symbol"],
            "label": spec["label"],
            "kind": spec["kind"],
            "status": "missing",
            "trend": "unknown",
            "note": "No free history returned.",
        }
    ret_5d = _period_return(close, 5)
    ret_20d = _period_return(close, 20)
    ret_60d = _period_return(close, 60)
    vol_20d = _realized_vol_20d(close)
    trend = _market_trend_label(ret_5d, ret_20d)
    last_date = _history_last_date(history, close)
    return {
        "symbol": spec["symbol"],
        "label": spec["label"],
        "kind": spec["kind"],
        "status": "ok",
        "last_close": _clean_value(round(_float_value(close.iloc[-1]), 4)),
        "ret_5d": _clean_value(ret_5d),
        "ret_20d": _clean_value(ret_20d),
        "ret_60d": _clean_value(ret_60d),
        "vol_20d": _clean_value(vol_20d),
        "trend": trend,
        "rows": int(len(close)),
        "last_date": last_date,
    }


def _sector_strength_score(
    ret_20d: float | None,
    ret_60d: float | None,
    vol_20d: float | None,
) -> float:
    r20 = ret_20d if ret_20d is not None and math.isfinite(ret_20d) else 0.0
    r60 = ret_60d if ret_60d is not None and math.isfinite(ret_60d) else 0.0
    vol = vol_20d if vol_20d is not None and math.isfinite(vol_20d) else 0.0
    return (0.65 * r20) + (0.35 * r60) - (0.10 * vol)


def _sector_pulse_row(spec: dict[str, Any], history: pd.DataFrame) -> dict[str, Any]:
    close = _history_close_series(history)
    if close.empty:
        return {
            "symbol": spec["symbol"],
            "sector": spec["sector"],
            "group": spec["group"],
            "status": "missing",
            "trend": "unknown",
            "note": "No free history returned.",
        }
    ret_5d = _period_return(close, 5)
    ret_20d = _period_return(close, 20)
    ret_60d = _period_return(close, 60)
    vol_20d = _realized_vol_20d(close)
    trend = _market_trend_label(ret_5d, ret_20d)
    last_date = _history_last_date(history, close)
    strength = _sector_strength_score(ret_20d, ret_60d, vol_20d)
    return {
        "symbol": spec["symbol"],
        "sector": spec["sector"],
        "group": spec["group"],
        "status": "ok",
        "last_close": _clean_value(round(_float_value(close.iloc[-1]), 4)),
        "ret_5d": _clean_value(ret_5d),
        "ret_20d": _clean_value(ret_20d),
        "ret_60d": _clean_value(ret_60d),
        "vol_20d": _clean_value(vol_20d),
        "trend": trend,
        "strength_score": _clean_value(round(strength, 4)),
        "rows": int(len(close)),
        "last_date": last_date,
    }


def _relative_return(numerator: pd.Series, denominator: pd.Series, periods: int) -> float | None:
    if len(numerator) <= periods or len(denominator) <= periods:
        return None
    n0 = _float_value(numerator.iloc[-1 - periods], default=math.nan)
    n1 = _float_value(numerator.iloc[-1], default=math.nan)
    d0 = _float_value(denominator.iloc[-1 - periods], default=math.nan)
    d1 = _float_value(denominator.iloc[-1], default=math.nan)
    if not all(math.isfinite(v) and v > 0 for v in (n0, n1, d0, d1)):
        return None
    return (n1 / n0) / (d1 / d0) - 1.0


def _breadth_signal_label(score: float) -> str:
    if score >= 0.015:
        return "supportive"
    if score <= -0.015:
        return "warning"
    return "neutral"


def _breadth_pulse_row(
    spec: dict[str, Any],
    histories: dict[str, pd.DataFrame],
) -> dict[str, Any]:
    numerator_symbol = str(spec["numerator"])
    denominator_symbol = str(spec["denominator"])
    numerator = _history_close_series(histories.get(numerator_symbol, pd.DataFrame()))
    denominator = _history_close_series(histories.get(denominator_symbol, pd.DataFrame()))
    if numerator.empty or denominator.empty:
        return {
            "label": spec["label"],
            "pair": f"{numerator_symbol}/{denominator_symbol}",
            "kind": spec["kind"],
            "status": "missing",
            "signal": "unknown",
            "note": "One or both free histories were unavailable.",
        }

    rel_5d = _relative_return(numerator, denominator, 5)
    rel_20d = _relative_return(numerator, denominator, 20)
    rel_60d = _relative_return(numerator, denominator, 60)
    raw_score = (
        0.15 * (rel_5d if rel_5d is not None and math.isfinite(rel_5d) else 0.0)
        + 0.55 * (rel_20d if rel_20d is not None and math.isfinite(rel_20d) else 0.0)
        + 0.30 * (rel_60d if rel_60d is not None and math.isfinite(rel_60d) else 0.0)
    )
    orientation = -1.0 if str(spec.get("bullish_when")) == "negative" else 1.0
    score = raw_score * orientation
    last_date = _history_last_date(histories.get(numerator_symbol, pd.DataFrame()), numerator)
    return {
        "label": spec["label"],
        "pair": f"{numerator_symbol}/{denominator_symbol}",
        "kind": spec["kind"],
        "status": "ok",
        "signal": _breadth_signal_label(score),
        "breadth_score": _clean_value(round(score, 4)),
        "relative_5d": _clean_value(rel_5d),
        "relative_20d": _clean_value(rel_20d),
        "relative_60d": _clean_value(rel_60d),
        "bullish_when": spec.get("bullish_when"),
        "description": spec.get("description"),
        "rows": int(min(len(numerator), len(denominator))),
        "last_date": last_date,
    }


def _breadth_regime_label(score: float, rows: list[dict[str, Any]]) -> str:
    supportive = sum(row.get("signal") == "supportive" for row in rows)
    warnings = sum(row.get("signal") == "warning" for row in rows)
    if score >= 0.025 and supportive >= warnings + 2:
        return "broad_risk_on"
    if score <= -0.025 or warnings > supportive:
        return "narrow_or_defensive"
    if supportive > warnings:
        return "selective_risk_on"
    return "mixed"


def _parse_cboe_put_call_csv(text: str, source: dict[str, Any]) -> dict[str, Any]:
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    header_idx = next((idx for idx, line in enumerate(lines) if line.upper().startswith("DATE,")), None)
    if header_idx is None:
        return {
            "key": source.get("key"),
            "label": source.get("label"),
            "status": "missing",
            "signal": "unknown",
            "note": "Cboe CSV header not found.",
        }
    try:
        df = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])))
    except Exception as exc:
        return {
            "key": source.get("key"),
            "label": source.get("label"),
            "status": "error",
            "signal": "unknown",
            "note": f"Cboe CSV parse failed: {exc}",
        }
    if df.empty or "P/C Ratio" not in df.columns:
        return {
            "key": source.get("key"),
            "label": source.get("label"),
            "status": "missing",
            "signal": "unknown",
            "note": "Cboe CSV contained no put/call rows.",
        }
    call_col = "CALLS" if "CALLS" in df.columns else "CALL" if "CALL" in df.columns else ""
    put_col = "PUTS" if "PUTS" in df.columns else "PUT" if "PUT" in df.columns else ""
    date = pd.to_datetime(df["DATE"], errors="coerce")
    ratio = pd.to_numeric(df["P/C Ratio"], errors="coerce")
    calls = pd.to_numeric(df[call_col], errors="coerce") if call_col else pd.Series(index=df.index, dtype="float64")
    puts = pd.to_numeric(df[put_col], errors="coerce") if put_col else pd.Series(index=df.index, dtype="float64")
    clean = pd.DataFrame({"date": date, "pc_ratio": ratio, "calls": calls, "puts": puts}).dropna(subset=["date", "pc_ratio"])
    clean = clean[clean["pc_ratio"] > 0].sort_values("date")
    if clean.empty:
        return {
            "key": source.get("key"),
            "label": source.get("label"),
            "status": "missing",
            "signal": "unknown",
            "note": "No valid Cboe put/call observations.",
        }
    latest = clean.iloc[-1]
    recent = clean.tail(20)
    ratio_latest = _float_value(latest["pc_ratio"], default=math.nan)
    avg_5d = float(clean["pc_ratio"].tail(5).mean()) if len(clean) >= 5 else None
    avg_20d = float(recent["pc_ratio"].mean()) if len(recent) else None
    pct_vs_20d = ((ratio_latest / avg_20d) - 1.0) if avg_20d and avg_20d > 0 else None
    signal = _cboe_put_call_signal(str(source.get("key") or ""), ratio_latest, avg_20d)
    return {
        "key": source.get("key"),
        "label": source.get("label"),
        "status": "ok",
        "signal": signal,
        "latest_date": str(pd.Timestamp(latest["date"]).date()),
        "pc_ratio": _clean_value(round(ratio_latest, 4)),
        "avg_5d": _clean_value(round(avg_5d, 4) if avg_5d is not None else None),
        "avg_20d": _clean_value(round(avg_20d, 4) if avg_20d is not None else None),
        "pct_vs_20d": _clean_value(round(pct_vs_20d, 4) if pct_vs_20d is not None else None),
        "calls": _clean_value(int(latest["calls"]) if math.isfinite(_float_value(latest["calls"], default=math.nan)) else None),
        "puts": _clean_value(int(latest["puts"]) if math.isfinite(_float_value(latest["puts"], default=math.nan)) else None),
        "rows": int(len(clean)),
        "source": "cboe_put_call_ratio_csv",
        "quality": "informational_delayed",
    }


def _cboe_put_call_signal(key: str, ratio: float, avg_20d: float | None) -> str:
    if not math.isfinite(ratio):
        return "unknown"
    high = 1.10 if key != "equity" else 0.85
    low = 0.70 if key != "equity" else 0.55
    if ratio >= high:
        return "defensive_hedging"
    if ratio <= low:
        return "call_demand_high"
    if avg_20d and avg_20d > 0:
        if ratio >= avg_20d * 1.15:
            return "hedging_rising"
        if ratio <= avg_20d * 0.85:
            return "call_demand_rising"
    return "balanced"


def _fetch_cboe_put_call_snapshot(source: dict[str, Any], cache_age: int = 6 * 3600) -> dict[str, Any]:
    key = f"cboe_put_call_ratio:{source.get('key')}"
    cached = data_provider.cache_get(key, cache_age)
    if isinstance(cached, dict) and cached:
        return cached
    request = Request(
        str(source.get("url")),
        headers={
            "User-Agent": "Mozilla/5.0 Optedge research cockpit",
            "Accept": "text/csv,*/*",
        },
    )
    with urlopen(request, timeout=12) as response:
        text = response.read().decode("utf-8", "replace")
    parsed = _parse_cboe_put_call_csv(text, source)
    data_provider.cache_put(key, parsed)
    return parsed


def _cboe_put_call_stale(row: dict[str, Any], max_age_days: int = 10) -> bool:
    if row.get("status") != "ok":
        return True
    date_text = str(row.get("latest_date") or "")
    try:
        latest = pd.to_datetime(date_text, utc=True).date()
    except Exception:
        return True
    age_days = (datetime.now(timezone.utc).date() - latest).days
    return age_days > max_age_days


def _parse_cboe_daily_put_call_ratios(text: str) -> dict[str, float]:
    search_text = str(text or "").replace('\\"', '"')
    patterns = {
        "total": r'"name"\s*:\s*"TOTAL PUT/CALL RATIO"\s*,\s*"value"\s*:\s*"([0-9.]+)"',
        "index": r'"name"\s*:\s*"INDEX PUT/CALL RATIO"\s*,\s*"value"\s*:\s*"([0-9.]+)"',
        "equity": r'"name"\s*:\s*"EQUITY PUT/CALL RATIO"\s*,\s*"value"\s*:\s*"([0-9.]+)"',
    }
    out: dict[str, float] = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, search_text)
        if not match:
            continue
        value = _float_value(match.group(1), default=math.nan)
        if math.isfinite(value) and value > 0:
            out[key] = value
    return out


def _fetch_cboe_daily_put_call_ratios(cache_age: int = 30 * 60) -> dict[str, float]:
    cached = data_provider.cache_get("cboe_daily_put_call_ratios", cache_age)
    if isinstance(cached, dict) and cached:
        return {str(k): float(v) for k, v in cached.items()}
    request = Request(
        CBOE_DAILY_STATS_URL,
        headers={
            "User-Agent": "Mozilla/5.0 Optedge research cockpit",
            "Accept": "text/html,*/*",
        },
    )
    with urlopen(request, timeout=12) as response:
        text = response.read().decode("utf-8", "replace")
    out = _parse_cboe_daily_put_call_ratios(text)
    if out:
        data_provider.cache_put("cboe_daily_put_call_ratios", out)
    return out


def _daily_put_call_row(source: dict[str, Any], ratio: float, note: str) -> dict[str, Any]:
    return {
        "key": source.get("key"),
        "label": source.get("label"),
        "status": "ok",
        "signal": _cboe_put_call_signal(str(source.get("key") or ""), ratio, None),
        "latest_date": str(datetime.now(timezone.utc).date()),
        "pc_ratio": _clean_value(round(ratio, 4)),
        "avg_5d": None,
        "avg_20d": None,
        "pct_vs_20d": None,
        "calls": None,
        "puts": None,
        "rows": None,
        "source": "cboe_daily_market_statistics",
        "quality": "informational_delayed",
        "note": note,
    }


def _options_sentiment_regime(rows: list[dict[str, Any]]) -> str:
    ok_rows = [row for row in rows if row.get("status") == "ok"]
    if not ok_rows:
        return "unknown"
    defensive = sum(str(row.get("signal")) in {"defensive_hedging", "hedging_rising"} for row in ok_rows)
    call_demand = sum(str(row.get("signal")) in {"call_demand_high", "call_demand_rising"} for row in ok_rows)
    if defensive >= 2:
        return "defensive_hedging"
    if call_demand >= 2:
        return "call_demand_high"
    if defensive > call_demand:
        return "hedging_rising"
    if call_demand > defensive:
        return "call_demand_rising"
    return "balanced"


def build_options_sentiment(data_dir: Path = DATA_DIR) -> dict[str, Any]:
    """Build Cboe market-wide options sentiment from no-key statistics."""
    del data_dir
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    daily_ratios: dict[str, float] = {}
    for source in CBOE_PUT_CALL_SOURCES:
        try:
            row = _fetch_cboe_put_call_snapshot(source)
        except Exception as exc:
            row = {
                "key": source.get("key"),
                "label": source.get("label"),
                "status": "error",
                "signal": "unknown",
                "note": str(exc)[:180],
            }
        if _cboe_put_call_stale(row):
            try:
                if not daily_ratios:
                    daily_ratios = _fetch_cboe_daily_put_call_ratios()
                ratio = daily_ratios.get(str(source.get("key")))
                if ratio is not None:
                    row = _daily_put_call_row(
                        source,
                        ratio,
                        "Archive CSV was stale or unavailable; using Cboe daily market statistics.",
                    )
                else:
                    row["status"] = "stale" if row.get("status") == "ok" else row.get("status")
                    row["note"] = f"{row.get('note') or 'Archive snapshot stale'}; daily fallback unavailable."[:220]
            except Exception as exc:
                row["status"] = "stale" if row.get("status") == "ok" else row.get("status")
                row["note"] = f"{row.get('note') or 'Archive snapshot stale'}; daily fallback failed: {exc}"[:220]
        rows.append({k: _clean_value(v) for k, v in row.items()})
        if row.get("status") != "ok":
            warnings.append(f"{source.get('label')} put/call unavailable.")
    ok_count = sum(1 for row in rows if row.get("status") == "ok")
    by_key = {str(row.get("key")): row for row in rows}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Cboe market statistics and put/call ratio CSV",
        "status": "ok" if ok_count else "missing",
        "regime": _options_sentiment_regime(rows),
        "coverage": f"{ok_count}/{len(CBOE_PUT_CALL_SOURCES)}",
        "total_pc": by_key.get("total", {}).get("pc_ratio"),
        "equity_pc": by_key.get("equity", {}).get("pc_ratio"),
        "index_pc": by_key.get("index", {}).get("pc_ratio"),
        "rows": rows,
        "warnings": warnings,
        "notes": [
            "Cboe put/call ratios are market-wide options sentiment context.",
            "Data is informational/delayed and should not be treated as an execution quote.",
        ],
    }


def _risk_score_from_market_rows(rows: list[dict[str, Any]]) -> float:
    total_weight = 0.0
    score = 0.0
    weights = {str(item["symbol"]).upper(): _float_value(item.get("risk_weight")) for item in MARKET_PULSE_SYMBOLS}
    for row in rows:
        if row.get("status") != "ok":
            continue
        symbol = str(row.get("symbol") or "").upper()
        weight = weights.get(symbol, 0.0)
        ret_20d = _float_value(row.get("ret_20d"), default=math.nan)
        if not math.isfinite(ret_20d) or weight == 0:
            continue
        clipped = max(-0.08, min(0.08, ret_20d)) / 0.08
        score += clipped * weight
        total_weight += abs(weight)
    return score / total_weight if total_weight else 0.0


def _market_regime_label(score: float, rows: list[dict[str, Any]]) -> str:
    vix_row = next((row for row in rows if row.get("symbol") == "^VIX"), None)
    vix_20d = _float_value(vix_row.get("ret_20d"), default=0.0) if vix_row else 0.0
    if score >= 0.30 and vix_20d < 0.15:
        return "risk_on"
    if score <= -0.25 or vix_20d > 0.20:
        return "risk_off"
    if score >= 0.10:
        return "constructive"
    if score <= -0.10:
        return "defensive"
    return "mixed"


def build_market_pulse(data_dir: Path = DATA_DIR, period: str = "6mo") -> dict[str, Any]:
    """Build a free no-key market regime snapshot for swing-trade context."""
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    for spec in MARKET_PULSE_SYMBOLS:
        try:
            history = data_provider.get_history(spec["symbol"], period=period, interval="1d", cache_age=1800)
            row = _market_pulse_row(spec, history)
        except Exception as exc:
            row = {
                "symbol": spec["symbol"],
                "label": spec["label"],
                "kind": spec["kind"],
                "status": "error",
                "trend": "unknown",
                "note": str(exc)[:160],
            }
        rows.append(row)
        if row.get("status") != "ok":
            warnings.append(f"{row['symbol']} history unavailable.")

    risk_score = _risk_score_from_market_rows(rows)
    regime = _market_regime_label(risk_score, rows)
    leaders = sorted(
        [row for row in rows if row.get("status") == "ok"],
        key=lambda row: _float_value(row.get("ret_20d"), default=-999.0),
        reverse=True,
    )[:3]
    laggards = sorted(
        [row for row in rows if row.get("status") == "ok"],
        key=lambda row: _float_value(row.get("ret_20d"), default=999.0),
    )[:3]
    options_sentiment = build_options_sentiment(data_dir)
    warnings.extend(options_sentiment.get("warnings") or [])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": period,
        "regime": regime,
        "risk_score": _clean_value(round(risk_score, 4)),
        "coverage": f"{sum(1 for row in rows if row.get('status') == 'ok')}/{len(rows)}",
        "options_sentiment": options_sentiment,
        "rows": [{k: _clean_value(v) for k, v in row.items()} for row in rows],
        "leaders": [{k: _clean_value(v) for k, v in row.items()} for row in leaders],
        "laggards": [{k: _clean_value(v) for k, v in row.items()} for row in laggards],
        "warnings": warnings,
        "notes": [
            "Market Pulse uses free/no-key historical price providers through data_provider.get_history.",
            "It gives regime context for swing-trade review; it is not a trading signal by itself.",
            "VIX and ETF proxies may be delayed or unavailable depending on the free source.",
        ],
    }


def build_sector_pulse(data_dir: Path = DATA_DIR, period: str = "6mo") -> dict[str, Any]:
    """Build a free no-key sector and industry proxy strength map."""
    del data_dir  # reserved for future persisted pulse snapshots
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    for spec in SECTOR_PULSE_SYMBOLS:
        try:
            history = data_provider.get_history(spec["symbol"], period=period, interval="1d", cache_age=1800)
            row = _sector_pulse_row(spec, history)
        except Exception as exc:
            row = {
                "symbol": spec["symbol"],
                "sector": spec["sector"],
                "group": spec["group"],
                "status": "error",
                "trend": "unknown",
                "note": str(exc)[:160],
            }
        rows.append(row)
        if row.get("status") != "ok":
            warnings.append(f"{row['symbol']} history unavailable.")

    ok_rows = [row for row in rows if row.get("status") == "ok"]
    leaders = sorted(
        ok_rows,
        key=lambda row: _float_value(row.get("strength_score"), default=-999.0),
        reverse=True,
    )[:5]
    laggards = sorted(
        ok_rows,
        key=lambda row: _float_value(row.get("strength_score"), default=999.0),
    )[:5]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": period,
        "coverage": f"{len(ok_rows)}/{len(rows)}",
        "rows": [{k: _clean_value(v) for k, v in row.items()} for row in rows],
        "leaders": [{k: _clean_value(v) for k, v in row.items()} for row in leaders],
        "laggards": [{k: _clean_value(v) for k, v in row.items()} for row in laggards],
        "warnings": warnings,
        "notes": [
            "Sector Pulse uses free ETF and industry-proxy histories through data_provider.get_history.",
            "Use it as context for ticker and option-chain review; it is not an order signal by itself.",
            "ETF histories may be delayed or temporarily unavailable depending on the free source.",
        ],
    }


def build_breadth_pulse(data_dir: Path = DATA_DIR, period: str = "6mo") -> dict[str, Any]:
    """Build free ETF-pair breadth confirmation for swing-trade context."""
    del data_dir  # reserved for future persisted pulse snapshots
    symbols = sorted({
        str(spec["numerator"]) for spec in BREADTH_PULSE_PAIRS
    } | {
        str(spec["denominator"]) for spec in BREADTH_PULSE_PAIRS
    })
    histories: dict[str, pd.DataFrame] = {}
    warnings: list[str] = []
    for symbol in symbols:
        try:
            histories[symbol] = data_provider.get_history(symbol, period=period, interval="1d", cache_age=1800)
        except Exception as exc:
            histories[symbol] = pd.DataFrame()
            warnings.append(f"{symbol} history unavailable: {str(exc)[:80]}")

    rows = [_breadth_pulse_row(spec, histories) for spec in BREADTH_PULSE_PAIRS]
    for row in rows:
        if row.get("status") != "ok":
            warnings.append(f"{row.get('pair')} history unavailable.")
    ok_rows = [row for row in rows if row.get("status") == "ok"]
    scores = [
        _float_value(row.get("breadth_score"), default=math.nan)
        for row in ok_rows
        if math.isfinite(_float_value(row.get("breadth_score"), default=math.nan))
    ]
    breadth_score = sum(scores) / len(scores) if scores else 0.0
    regime = _breadth_regime_label(breadth_score, ok_rows)
    supportive = sum(row.get("signal") == "supportive" for row in ok_rows)
    warning_count = sum(row.get("signal") == "warning" for row in ok_rows)
    neutral = sum(row.get("signal") == "neutral" for row in ok_rows)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": period,
        "coverage": f"{len(ok_rows)}/{len(rows)}",
        "regime": regime,
        "breadth_score": _clean_value(round(breadth_score, 4)),
        "supportive_count": supportive,
        "warning_count": warning_count,
        "neutral_count": neutral,
        "rows": [{k: _clean_value(v) for k, v in row.items()} for row in rows],
        "supportive": [
            {k: _clean_value(v) for k, v in row.items()}
            for row in sorted(ok_rows, key=lambda item: _float_value(item.get("breadth_score")), reverse=True)
            if row.get("signal") == "supportive"
        ][:5],
        "warnings_list": [
            {k: _clean_value(v) for k, v in row.items()}
            for row in sorted(ok_rows, key=lambda item: _float_value(item.get("breadth_score")))
            if row.get("signal") == "warning"
        ][:5],
        "warnings": warnings,
        "notes": [
            "Breadth Pulse uses free ETF-pair histories through data_provider.get_history.",
            "It checks whether swing longs are broad, narrow, or defensive using relative ETF performance.",
            "ETF histories may be delayed or unavailable depending on the free source.",
        ],
    }


def _fred_series_values(series_id: str, days: int) -> list[dict[str, Any]]:
    try:
        rows = fred_csv_history(series_id, days=days, cache_hours=12)
    except Exception:
        return []
    return rows if isinstance(rows, list) else []


def _series_latest(rows: list[dict[str, Any]]) -> tuple[float, str | None]:
    for row in rows:
        value = _float_value(row.get("value"), default=math.nan) if isinstance(row, dict) else math.nan
        if math.isfinite(value):
            return value, str(row.get("date") or "") or None
    return math.nan, None


def _series_prior(rows: list[dict[str, Any]], offset: int) -> float:
    clean = [
        _float_value(row.get("value"), default=math.nan)
        for row in rows
        if isinstance(row, dict) and math.isfinite(_float_value(row.get("value"), default=math.nan))
    ]
    if len(clean) <= offset:
        return math.nan
    return clean[offset]


def _series_yoy(rows: list[dict[str, Any]]) -> float:
    latest, _ = _series_latest(rows)
    prior = _series_prior(rows, 12)
    if not math.isfinite(latest) or not math.isfinite(prior) or prior == 0:
        return math.nan
    return (latest / prior) - 1.0


def _macro_signal_for_series(series_id: str, latest: float, change_4: float, yoy: float) -> tuple[str, int, str]:
    if not math.isfinite(latest):
        return "missing", 6, "No recent FRED observation."
    if series_id == "BAMLH0A0HYM2":
        if latest >= 5.0:
            return "stress", 18, "High-yield spreads are stress-level wide."
        if latest >= 4.0 or change_4 >= 0.60:
            return "warning", 11, "Credit spreads are elevated or widening."
        if latest <= 3.0:
            return "supportive", 0, "Credit spreads are contained."
        return "neutral", 4, "Credit spreads are middle-of-range."
    if series_id == "T10Y3M":
        if latest <= -0.75:
            return "stress", 14, "Yield curve is deeply inverted."
        if latest < 0.0:
            return "warning", 9, "Yield curve is inverted."
        if latest >= 0.75:
            return "supportive", 0, "Yield curve is positively sloped."
        return "neutral", 3, "Yield curve is near flat."
    if series_id == "UNRATE":
        if latest >= 5.0 or change_4 >= 0.50:
            return "stress", 14, "Labor market deterioration is meaningful."
        if latest >= 4.3 or change_4 >= 0.25:
            return "warning", 8, "Unemployment is elevated or rising."
        return "supportive", 0, "Unemployment is not flashing stress."
    if series_id == "ICSA":
        if latest >= 275_000 or change_4 >= 35_000:
            return "stress", 12, "Initial claims are materially elevated."
        if latest >= 240_000 or change_4 >= 20_000:
            return "warning", 7, "Initial claims are rising or above comfort."
        return "supportive", 0, "Initial claims are contained."
    if series_id == "CPIAUCSL":
        if not math.isfinite(yoy):
            return "missing", 6, "CPI YoY could not be computed."
        if yoy >= 0.045:
            return "stress", 12, "Inflation is hot enough to keep policy restrictive."
        if yoy >= 0.032:
            return "warning", 7, "Inflation is above comfort."
        if yoy <= 0.025:
            return "supportive", 0, "Inflation pressure is more manageable."
        return "neutral", 3, "Inflation is moderate."
    if series_id == "INDPRO":
        if not math.isfinite(yoy):
            return "missing", 5, "Industrial production YoY could not be computed."
        if yoy <= -0.025:
            return "stress", 11, "Industrial production is contracting."
        if yoy < 0.0:
            return "warning", 6, "Industrial production growth is negative."
        return "supportive", 0, "Industrial production is expanding."
    if series_id == "M2SL":
        if not math.isfinite(yoy):
            return "missing", 5, "M2 liquidity YoY could not be computed."
        if yoy <= -0.02:
            return "stress", 10, "Money supply is contracting."
        if yoy < 0.0:
            return "warning", 6, "Money supply is still a liquidity headwind."
        if yoy >= 0.06:
            return "supportive", 0, "Money supply is a liquidity tailwind."
        return "neutral", 3, "Money supply growth is modest."
    return "neutral", 3, "No specific macro rule for this series."


def _macro_stress_label(score: int) -> tuple[str, str]:
    if score >= 65:
        return "macro_stress", "Macro backdrop is stressed; require exceptional setups only."
    if score >= 40:
        return "macro_caution", "Macro backdrop is cautious; tighten filters and sizing."
    if score <= 15:
        return "macro_supportive", "Macro backdrop is supportive enough for normal review gates."
    return "macro_neutral", "Macro backdrop is mixed; let setup quality decide."


def _macro_stress_component(score: int, regime: str) -> float:
    if regime == "macro_stress":
        return -10.0
    if regime == "macro_caution":
        return -5.0
    if regime == "macro_supportive":
        return 3.0
    if score <= 25:
        return 1.0
    return 0.0


def build_macro_stress_pulse(data_dir: Path = DATA_DIR) -> dict[str, Any]:
    """Build a keyless FRED macro-stress pulse for swing-trade context."""
    del data_dir
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    total_points = 0
    max_points = 0
    for spec in MACRO_STRESS_SERIES:
        series_id = str(spec["series_id"])
        raw_rows = _fred_series_values(series_id, int(spec.get("days") or 180))
        latest, latest_date = _series_latest(raw_rows)
        prior_4 = _series_prior(raw_rows, 4)
        change_4 = latest - prior_4 if math.isfinite(latest) and math.isfinite(prior_4) else math.nan
        yoy = _series_yoy(raw_rows) if int(spec.get("days") or 0) >= 365 else math.nan
        signal, points, note = _macro_signal_for_series(series_id, latest, change_4, yoy)
        max_points += 18
        total_points += points
        row = {
            "series_id": series_id,
            "label": spec.get("label"),
            "category": spec.get("category"),
            "latest": _clean_value(round(latest, 4) if math.isfinite(latest) else None),
            "latest_date": latest_date,
            "change_4obs": _clean_value(round(change_4, 4) if math.isfinite(change_4) else None),
            "yoy": _clean_value(round(yoy, 4) if math.isfinite(yoy) else None),
            "unit": spec.get("unit"),
            "signal": signal,
            "stress_points": points,
            "note": note,
            "description": spec.get("description"),
            "source": "FRED public CSV",
        }
        rows.append(row)
        if signal in {"missing", "stress", "warning"}:
            warnings.append(f"{spec.get('label')}: {note}")
    stress_score = int(round(_clamp((total_points / max_points) * 100.0 if max_points else 0.0, 0.0, 100.0)))
    regime, posture = _macro_stress_label(stress_score)
    counts: dict[str, int] = {}
    for row in rows:
        signal = str(row.get("signal") or "unknown")
        counts[signal] = counts.get(signal, 0) + 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "FRED public CSV",
        "status": "ok" if any(row.get("signal") != "missing" for row in rows) else "missing",
        "regime": regime,
        "stress_score": stress_score,
        "posture": posture,
        "coverage": f"{sum(row.get('signal') != 'missing' for row in rows)}/{len(rows)}",
        "signal_counts": counts,
        "rows": [{k: _clean_value(v) for k, v in row.items()} for row in rows],
        "warnings": warnings[:8],
        "notes": [
            "Macro Stress uses FRED's public graph CSV endpoint, so it does not require a FRED API key.",
            "It is slower-moving economic context for swing review, not an intraday signal.",
            "Stress tightens filters; it does not place trades or override hard risk rules.",
        ],
    }


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _swing_climate_label(score: int) -> tuple[str, str]:
    if score >= 75:
        return "aggressive_swing", "Risk-on enough to press only the cleanest ready setups."
    if score >= 60:
        return "constructive_selective", "Constructive, but stay selective and respect stops."
    if score >= 45:
        return "mixed_selective", "Mixed tape; prefer smaller size, cleaner liquidity, and stronger sectors."
    return "defensive_wait", "Defensive tape; wait for cleaner breadth or use only exceptional setups."


def _swing_playbook(label: str) -> dict[str, Any]:
    if label == "aggressive_swing":
        return {
            "min_readiness_score": 75,
            "option_min_dte": MIN_SWING_OPTION_DTE,
            "option_max_spread_pct": 0.20,
            "max_new_candidates": 5,
            "candidate_status": "ready or strong review",
            "sizing_bias": "normal research sizing only when validation and open-risk checks agree",
        }
    if label == "constructive_selective":
        return {
            "min_readiness_score": 80,
            "option_min_dte": MIN_SWING_OPTION_DTE,
            "option_max_spread_pct": 0.15,
            "max_new_candidates": 3,
            "candidate_status": "ready preferred",
            "sizing_bias": "modest sizing; avoid crowded exposure",
        }
    if label == "mixed_selective":
        return {
            "min_readiness_score": 85,
            "option_min_dte": 120,
            "option_max_spread_pct": 0.12,
            "max_new_candidates": 2,
            "candidate_status": "ready only",
            "sizing_bias": "small sizing; require unusually clean liquidity and thesis",
        }
    return {
        "min_readiness_score": 90,
        "option_min_dte": 180,
        "option_max_spread_pct": 0.10,
        "max_new_candidates": 1,
        "candidate_status": "exceptional ready only",
        "sizing_bias": "capital preservation; mostly wait",
    }


def _swing_asset_bias(label: str, top_sector: dict[str, Any]) -> list[dict[str, str]]:
    top_name = str(top_sector.get("sector") or "leading groups")
    if label == "aggressive_swing":
        return [
            {"asset": "options", "bias": "allowed", "rule": "Favor 90+ DTE calls/puts only when contract readiness is clean."},
            {"asset": "shares", "bias": "allowed", "rule": f"Favor strong setups tied to {top_name} when factor thesis agrees."},
            {"asset": "futures", "bias": "selective", "rule": "Use only futures rows with clear macro/risk confirmation and defined stops."},
        ]
    if label == "constructive_selective":
        return [
            {"asset": "options", "bias": "selective", "rule": "Prefer 90+ DTE, tighter spreads, and smaller candidate count."},
            {"asset": "shares", "bias": "allowed", "rule": f"Prefer leading-sector shares, especially {top_name}, with trend support."},
            {"asset": "futures", "bias": "selective", "rule": "Require clean reward/risk and no macro conflict."},
        ]
    if label == "mixed_selective":
        return [
            {"asset": "options", "bias": "strict", "rule": "Use 120+ DTE and tight spreads; skip marginal review rows."},
            {"asset": "shares", "bias": "selective", "rule": "Favor smaller, cleaner share ideas over wide option contracts."},
            {"asset": "futures", "bias": "strict", "rule": "Only take strongest macro-aligned futures setups."},
        ]
    return [
        {"asset": "options", "bias": "mostly wait", "rule": "Use 180+ DTE only for exceptional ready setups; otherwise skip."},
        {"asset": "shares", "bias": "mostly wait", "rule": "Preserve capital unless thesis, trend, and guardrails are unusually strong."},
        {"asset": "futures", "bias": "mostly wait", "rule": "Avoid forcing futures trades while breadth or market regime is defensive."},
    ]


def _swing_climate_from_pulses(
    market: dict[str, Any],
    breadth: dict[str, Any],
    sector: dict[str, Any],
    macro: dict[str, Any] | None = None,
) -> dict[str, Any]:
    macro = macro if isinstance(macro, dict) else {}
    market_score = _float_value(market.get("risk_score"), default=0.0)
    breadth_score = _float_value(breadth.get("breadth_score"), default=0.0)
    options_sentiment = market.get("options_sentiment") if isinstance(market.get("options_sentiment"), dict) else {}
    options_regime = str(options_sentiment.get("regime") or "unknown")
    options_component = {
        "defensive_hedging": -8.0,
        "hedging_rising": -4.0,
        "call_demand_rising": 4.0,
        "call_demand_high": 3.0,
        "balanced": 0.0,
    }.get(options_regime, 0.0)
    macro_regime = str(macro.get("regime") or "unknown")
    macro_stress_score = int(_float_value(macro.get("stress_score"), default=0.0))
    macro_component = _macro_stress_component(macro_stress_score, macro_regime)
    sector_leaders = sector.get("leaders") if isinstance(sector.get("leaders"), list) else []
    sector_laggards = sector.get("laggards") if isinstance(sector.get("laggards"), list) else []
    leader_scores = [
        _float_value(row.get("strength_score"), default=math.nan)
        for row in sector_leaders[:3]
        if isinstance(row, dict) and math.isfinite(_float_value(row.get("strength_score"), default=math.nan))
    ]
    top_sector_score = sum(leader_scores) / len(leader_scores) if leader_scores else 0.0

    components = {
        "market": round((_clamp(market_score, -0.60, 0.60) / 0.60) * 35.0, 2),
        "breadth": round((_clamp(breadth_score, -0.08, 0.08) / 0.08) * 30.0, 2),
        "sector": round((_clamp(top_sector_score, -0.12, 0.12) / 0.12) * 15.0, 2),
        "options_sentiment": round(options_component, 2),
        "macro": round(macro_component, 2),
    }
    raw_score = (
        50.0
        + components["market"]
        + components["breadth"]
        + components["sector"]
        + components["options_sentiment"]
        + components["macro"]
    )

    market_regime = str(market.get("regime") or "unknown")
    breadth_regime = str(breadth.get("regime") or "unknown")
    warning_count = int(_float_value(breadth.get("warning_count"), default=0.0))
    if market_regime in {"risk_off", "defensive"}:
        raw_score -= 15.0
    if breadth_regime == "narrow_or_defensive":
        raw_score -= 15.0
    raw_score -= min(12.0, warning_count * 4.0)

    score = int(round(_clamp(raw_score, 0.0, 100.0)))
    label, posture = _swing_climate_label(score)

    positives: list[str] = []
    warnings_out: list[str] = []
    if market_regime in {"risk_on", "constructive"}:
        positives.append(f"Market pulse is {market_regime}.")
    elif market_regime in {"risk_off", "defensive"}:
        warnings_out.append(f"Market pulse is {market_regime}.")
    if breadth_regime in {"broad_risk_on", "selective_risk_on"}:
        positives.append(f"Breadth pulse is {breadth_regime}.")
    elif breadth_regime == "narrow_or_defensive":
        warnings_out.append("Breadth is narrow or defensive.")
    supportive_count = int(_float_value(breadth.get("supportive_count"), default=0.0))
    if supportive_count:
        positives.append(f"{supportive_count} breadth pair(s) are supportive.")
    if warning_count:
        warnings_out.append(f"{warning_count} breadth pair(s) are warning.")
    if options_regime in {"call_demand_high", "call_demand_rising"}:
        positives.append(f"Cboe options sentiment is {options_regime}.")
        if options_regime == "call_demand_high":
            warnings_out.append("Call demand is hot; avoid chasing wide or illiquid option contracts.")
    elif options_regime in {"defensive_hedging", "hedging_rising"}:
        warnings_out.append(f"Cboe options sentiment is {options_regime}.")
    if macro_regime == "macro_supportive":
        positives.append("FRED macro stress pulse is supportive.")
    elif macro_regime in {"macro_caution", "macro_stress"}:
        warnings_out.append(f"FRED macro stress pulse is {macro_regime}.")

    top_sector = sector_leaders[0] if sector_leaders and isinstance(sector_leaders[0], dict) else {}
    weak_sector = sector_laggards[0] if sector_laggards and isinstance(sector_laggards[0], dict) else {}
    if top_sector:
        positives.append(f"Strongest group: {top_sector.get('symbol')} {top_sector.get('sector')}.")
    if weak_sector:
        warnings_out.append(f"Weakest group: {weak_sector.get('symbol')} {weak_sector.get('sector')}.")
    warnings_out.extend(str(item) for item in (market.get("warnings") or [])[:2])
    warnings_out.extend(str(item) for item in (breadth.get("warnings") or [])[:2])
    warnings_out.extend(str(item) for item in (sector.get("warnings") or [])[:2])

    focus = [
        {
            "label": "Best setup filter",
            "detail": "Prefer ready setups with 90+ DTE options, tight spreads, and sector support.",
        },
        {
            "label": "Sizing posture",
            "detail": "Use normal sizing only when validation, liquidity, and open-position risk agree.",
        },
    ]
    if top_sector:
        focus.insert(0, {
            "label": "Leading group",
            "detail": f"Prioritize tickers tied to {top_sector.get('sector')} when their own thesis confirms.",
        })
    if label in {"mixed_selective", "defensive_wait"}:
        focus.append({
            "label": "Risk control",
            "detail": "Avoid forcing marginal Watch rows; require cleaner contract readiness before acting.",
        })
    playbook = _swing_playbook(label)
    asset_bias = _swing_asset_bias(label, top_sector)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": market.get("period") or breadth.get("period") or sector.get("period"),
        "climate_score": score,
        "climate_label": label,
        "posture": posture,
        "market_regime": market_regime,
        "breadth_regime": breadth_regime,
        "macro_regime": macro_regime,
        "macro_stress_score": _clean_value(macro.get("stress_score")),
        "macro_coverage": _clean_value(macro.get("coverage")),
        "macro_stress": {
            "regime": macro_regime,
            "stress_score": _clean_value(macro.get("stress_score")),
            "coverage": _clean_value(macro.get("coverage")),
            "signal_counts": _clean_value(macro.get("signal_counts")),
        },
        "options_sentiment_regime": options_regime,
        "options_sentiment_coverage": _clean_value(options_sentiment.get("coverage")),
        "options_put_call": {
            "total": _clean_value(options_sentiment.get("total_pc")),
            "equity": _clean_value(options_sentiment.get("equity_pc")),
            "index": _clean_value(options_sentiment.get("index_pc")),
        },
        "options_sentiment": {
            "status": _clean_value(options_sentiment.get("status")),
            "regime": options_regime,
            "coverage": _clean_value(options_sentiment.get("coverage")),
            "total_pc": _clean_value(options_sentiment.get("total_pc")),
            "equity_pc": _clean_value(options_sentiment.get("equity_pc")),
            "index_pc": _clean_value(options_sentiment.get("index_pc")),
        },
        "market_risk_score": _clean_value(market.get("risk_score")),
        "breadth_score": _clean_value(breadth.get("breadth_score")),
        "top_sector": _clean_value(top_sector.get("sector")),
        "top_sector_symbol": _clean_value(top_sector.get("symbol")),
        "weak_sector": _clean_value(weak_sector.get("sector")),
        "weak_sector_symbol": _clean_value(weak_sector.get("symbol")),
        "components": components,
        "coverage": {
            "market": market.get("coverage"),
            "breadth": breadth.get("coverage"),
            "sector": sector.get("coverage"),
            "options_sentiment": options_sentiment.get("coverage"),
            "macro": macro.get("coverage"),
        },
        "playbook": playbook,
        "trade_gates": [
            {"gate": "Minimum readiness", "value": f"{playbook['min_readiness_score']}/100"},
            {"gate": "Options DTE floor", "value": f"{playbook['option_min_dte']}+ days"},
            {"gate": "Options max spread", "value": f"{playbook['option_max_spread_pct'] * 100:.0f}%"},
            {"gate": "Options sentiment", "value": options_regime},
            {"gate": "Macro stress", "value": f"{macro_regime} ({macro_stress_score}/100)"},
            {"gate": "Max new candidates", "value": str(playbook["max_new_candidates"])},
            {"gate": "Candidate status", "value": str(playbook["candidate_status"])},
            {"gate": "Sizing bias", "value": str(playbook["sizing_bias"])},
        ],
        "asset_bias": asset_bias,
        "positives": positives[:6],
        "warnings": warnings_out[:8],
        "focus": focus[:5],
        "notes": [
            "Swing Climate combines the free Market, Breadth, Sector, Cboe options sentiment, and FRED macro stress context.",
            "It is a review posture, not a trade signal or broker instruction.",
            "Use it to decide how strict to be with setup readiness, liquidity, and sizing.",
        ],
    }


def build_swing_climate(data_dir: Path = DATA_DIR, period: str = "6mo") -> dict[str, Any]:
    """Combine free context panels into a single swing-trading posture."""
    market = build_market_pulse(data_dir, period=period)
    breadth = build_breadth_pulse(data_dir, period=period)
    sector = build_sector_pulse(data_dir, period=period)
    macro = build_macro_stress_pulse(data_dir)
    return _swing_climate_from_pulses(market, breadth, sector, macro)


def _climate_gate_review(row: dict[str, Any], playbook: dict[str, Any], climate_score: int) -> dict[str, Any]:
    asset = str(row.get("asset") or "").strip().lower()
    blockers: list[str] = []
    confirmations: list[str] = []
    readiness = _float_value(row.get("readiness_score"), default=0.0)
    min_readiness = _float_value(playbook.get("min_readiness_score"), default=80.0)
    readiness_label = str(row.get("readiness_label") or "").strip().lower()
    status = str(row.get("trade_status") or "").strip().lower()

    if readiness < min_readiness:
        blockers.append(f"readiness {readiness:g} below climate gate {min_readiness:g}")
    else:
        confirmations.append(f"readiness {readiness:g} passes climate gate")
    if readiness_label == "wait":
        blockers.append("setup readiness is wait")
    if status in {"watch", "skip", "blocked"}:
        blockers.append(f"trade status is {status}")

    if asset == "option":
        dte = _float_value(row.get("dte"), default=math.nan)
        min_dte = _float_value(playbook.get("option_min_dte"), default=MIN_SWING_OPTION_DTE)
        spread = _float_value(row.get("spread_pct"), default=math.nan)
        max_spread = _float_value(playbook.get("option_max_spread_pct"), default=0.20)
        contracts = _float_value(row.get("suggested_contracts"), default=0.0)
        swing_label = str(row.get("swing_fit_label") or "").strip().lower()
        if not math.isfinite(dte) or dte < min_dte:
            blockers.append(f"DTE below climate floor {min_dte:g}")
        else:
            confirmations.append(f"DTE {dte:g}+ fits swing window")
        if math.isfinite(spread):
            if spread > max_spread:
                blockers.append(f"spread {spread * 100:.1f}% above climate max {max_spread * 100:.0f}%")
            else:
                confirmations.append(f"spread {spread * 100:.1f}% inside climate max")
        else:
            blockers.append("missing option spread")
        if contracts <= 0:
            blockers.append("no sized option contracts")
        if swing_label == "avoid":
            blockers.append("swing fit is avoid")
        elif swing_label == "speculative_swing":
            blockers.append("swing fit is speculative")
        elif swing_label in {"clean_swing", "reviewable_swing"}:
            confirmations.append(f"swing fit is {swing_label.replace('_', ' ')}")
    elif asset == "share":
        dollars = _float_value(row.get("suggested_dollars"), default=0.0)
        if dollars <= 0:
            blockers.append("no suggested share size")
        else:
            confirmations.append("share sizing is present")
    elif asset == "futures":
        contracts = _float_value(row.get("suggested_contracts"), default=0.0)
        direction = str(row.get("action") or "").strip().lower()
        if contracts <= 0:
            blockers.append("no sized futures contracts")
        else:
            confirmations.append("futures sizing is present")
        if direction not in {"long", "short"}:
            blockers.append("missing futures direction")
    elif asset == "value":
        if readiness >= min_readiness:
            confirmations.append("value thesis clears readiness gate")

    penalty = len(blockers) * 12.0
    gate_score = int(round(_clamp(readiness + (climate_score - 50) * 0.15 - penalty, 0.0, 100.0)))
    return {
        "climate_gate_status": "pass" if not blockers else "hold",
        "climate_gate_score": gate_score,
        "climate_gate_reasons": confirmations[:4] if not blockers else blockers[:5],
        "climate_gate_blockers": blockers[:5],
    }


def build_climate_gated_setups(
    data_dir: Path = DATA_DIR,
    per_asset: int = 4,
    limit: int = 12,
    include_held: bool = True,
) -> dict[str, Any]:
    """Gate the latest local setup shortlist against the current swing climate playbook."""
    per_asset = max(1, min(int(per_asset or 4), 10))
    limit = max(1, min(int(limit or 12), 40))
    climate = build_swing_climate(data_dir)
    playbook = climate.get("playbook") if isinstance(climate.get("playbook"), dict) else _swing_playbook("")
    climate_score = int(_float_value(climate.get("climate_score"), default=50.0))
    setup_report = build_best_setups(data_dir, per_asset=per_asset, limit=40)

    reviewed: list[dict[str, Any]] = []
    for raw in setup_report.get("rows", []):
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        row.update(_climate_gate_review(row, playbook, climate_score))
        row["climate_label"] = climate.get("climate_label")
        row["playbook_min_readiness"] = playbook.get("min_readiness_score")
        row["playbook_option_min_dte"] = playbook.get("option_min_dte")
        row["playbook_option_max_spread_pct"] = playbook.get("option_max_spread_pct")
        reviewed.append(row)

    reviewed = sorted(
        reviewed,
        key=lambda row: (
            1 if row.get("climate_gate_status") == "pass" else 0,
            _float_value(row.get("climate_gate_score")),
            _float_value(row.get("readiness_score")),
            _float_value(row.get("score")),
        ),
        reverse=True,
    )
    max_candidates = int(_float_value(playbook.get("max_new_candidates"), default=limit))
    max_candidates = max(1, min(limit, max_candidates))
    passed = [row for row in reviewed if row.get("climate_gate_status") == "pass"]
    selected = passed[:max_candidates]
    overflow = passed[max_candidates:]
    for row in overflow:
        row["climate_gate_status"] = "hold"
        row["climate_gate_reasons"] = ["candidate cap reached for current climate"]
        row["climate_gate_blockers"] = ["candidate cap reached for current climate"]
    held = [row for row in reviewed if row.get("climate_gate_status") != "pass"]
    held = sorted(
        held,
        key=lambda row: (
            _float_value(row.get("climate_gate_score")),
            _float_value(row.get("readiness_score")),
            _float_value(row.get("score")),
        ),
        reverse=True,
    )

    asset_counts: dict[str, dict[str, int]] = {}
    for row in selected:
        asset = str(row.get("asset") or "unknown")
        asset_counts.setdefault(asset, {"pass": 0, "hold": 0})["pass"] += 1
    for row in held:
        asset = str(row.get("asset") or "unknown")
        asset_counts.setdefault(asset, {"pass": 0, "hold": 0})["hold"] += 1

    clean_selected = [{k: _clean_value(v) for k, v in row.items()} for row in selected]
    clean_held = [{k: _clean_value(v) for k, v in row.items()} for row in held[:25]] if include_held else []
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "climate_label": climate.get("climate_label"),
        "climate_score": climate.get("climate_score"),
        "posture": climate.get("posture"),
        "playbook": playbook,
        "trade_gates": climate.get("trade_gates") or [],
        "asset_bias": climate.get("asset_bias") or [],
        "count": len(clean_selected),
        "selected_count": len(clean_selected),
        "held_count": len(held),
        "source_setup_count": len(reviewed),
        "max_new_candidates": max_candidates,
        "asset_counts": asset_counts,
        "rows": clean_selected,
        "held": clean_held,
        "asset_summaries": setup_report.get("asset_summaries") or [],
        "sources": setup_report.get("sources") or {},
        "notes": [
            "Climate-gated setups combine the latest best setups with the current Swing Climate playbook.",
            "Held rows are not rejected forever; they need cleaner readiness, liquidity, DTE, sizing, or climate conditions.",
            "This remains local research only and does not place trades.",
        ],
    }


def _avg(values: list[float]) -> float | None:
    clean = [v for v in values if math.isfinite(v)]
    return (sum(clean) / len(clean)) if clean else None


def build_risk_summary(data_dir: Path = DATA_DIR) -> dict[str, Any]:
    """Summarize current open-position risk from local lifecycle state."""
    positions = build_positions(data_dir, asset="all", status="all", limit=2000).get("rows", [])
    by_asset: dict[str, dict[str, Any]] = {}
    by_symbol: dict[str, dict[str, Any]] = {}
    pnl_values: list[float] = []
    pressure_values: list[float] = []
    high_pressure = 0
    attention = 0
    reprice_trouble = 0

    for row in positions:
        asset = str(row.get("asset") or "unknown")
        symbol = str(row.get("ticker_or_symbol") or "-").upper()
        pnl = _float_value(row.get("pnl_pct"), default=math.nan)
        pressure = _float_value(row.get("latest_exit_pressure"), default=math.nan)
        reprice_failed = _float_value(row.get("reprice_failed_count"))
        has_pnl = math.isfinite(pnl)
        has_pressure = math.isfinite(pressure)
        if has_pnl:
            pnl_values.append(pnl)
        if has_pressure:
            pressure_values.append(pressure)
        if has_pressure and pressure >= 80:
            high_pressure += 1
        if row.get("attention"):
            attention += 1
        if reprice_failed >= 2:
            reprice_trouble += 1

        asset_row = by_asset.setdefault(asset, {
            "asset": asset, "count": 0, "attention_count": 0, "high_pressure_count": 0,
            "avg_pnl_pct": None, "_pnls": [],
        })
        asset_row["count"] += 1
        if row.get("attention"):
            asset_row["attention_count"] += 1
        if has_pressure and pressure >= 80:
            asset_row["high_pressure_count"] += 1
        if has_pnl:
            asset_row["_pnls"].append(pnl)

        sym_row = by_symbol.setdefault(symbol, {
            "symbol": symbol, "count": 0, "attention_count": 0,
            "worst_pnl_pct": pnl if has_pnl else None,
            "max_exit_pressure": pressure if has_pressure else None,
        })
        sym_row["count"] += 1
        if row.get("attention"):
            sym_row["attention_count"] += 1
        if has_pnl:
            current_worst = sym_row.get("worst_pnl_pct")
            sym_row["worst_pnl_pct"] = pnl if current_worst is None else min(current_worst, pnl)
        if has_pressure:
            current_max = sym_row.get("max_exit_pressure")
            sym_row["max_exit_pressure"] = pressure if current_max is None else max(current_max, pressure)

    asset_rows = []
    for row in by_asset.values():
        pnls = row.pop("_pnls", [])
        row["avg_pnl_pct"] = _avg(pnls)
        asset_rows.append({k: _clean_value(v) for k, v in row.items()})
    asset_rows = sorted(asset_rows, key=lambda r: (int(r.get("attention_count") or 0), int(r.get("count") or 0)), reverse=True)

    total = len(positions)
    concentration = []
    for row in by_symbol.values():
        item = dict(row)
        item["share_of_open_positions"] = (item["count"] / total) if total else 0.0
        concentration.append({k: _clean_value(v) for k, v in item.items()})
    concentration = sorted(
        concentration,
        key=lambda r: (
            int(r.get("count") or 0),
            _float_value(r.get("max_exit_pressure")),
            abs(_float_value(r.get("worst_pnl_pct"))),
        ),
        reverse=True,
    )[:12]

    worst_positions = sorted(
        positions,
        key=lambda r: _float_value(r.get("pnl_pct")),
    )[:12]
    exit_pressure_rows = sorted(
        [row for row in positions if _float_value(row.get("latest_exit_pressure")) > 0],
        key=lambda r: _float_value(r.get("latest_exit_pressure")),
        reverse=True,
    )[:12]

    risk_level = "low"
    warnings: list[str] = []
    if high_pressure:
        risk_level = "high"
        warnings.append(f"{high_pressure} open position(s) have exit pressure >= 80.")
    elif attention:
        risk_level = "medium"
        warnings.append(f"{attention} open position(s) need attention.")
    if concentration and _float_value(concentration[0].get("share_of_open_positions")) >= 0.10:
        risk_level = "high" if risk_level == "high" else "medium"
        warnings.append(
            f"{concentration[0]['symbol']} is {concentration[0]['share_of_open_positions'] * 100:.1f}% of open position count."
        )
    if reprice_trouble:
        risk_level = "high" if risk_level == "high" else "medium"
        warnings.append(f"{reprice_trouble} open position(s) have repeated repricing trouble.")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "risk_level": risk_level,
        "total_open": total,
        "attention_count": attention,
        "high_exit_pressure_count": high_pressure,
        "reprice_trouble_count": reprice_trouble,
        "avg_open_pnl_pct": _clean_value(_avg(pnl_values)),
        "max_exit_pressure": _clean_value(max(pressure_values) if pressure_values else None),
        "asset_breakdown": asset_rows,
        "concentration": concentration,
        "worst_positions": worst_positions,
        "highest_exit_pressure": exit_pressure_rows,
        "warnings": warnings,
        "notes": [
            "Risk summary uses current local open position state only.",
            "Concentration is position-count based when dollar exposure is unavailable.",
            "This is decision support only; no trades are placed.",
        ],
    }


def _performance_tip(item: dict[str, Any]) -> str:
    engine = str(item.get("engine") or "").lower()
    elapsed = _float_value(item.get("last_elapsed") or item.get("elapsed_sec"))
    if engine == "insider" and elapsed >= 90:
        return "Use --turbo or --fast-insider during loops; run a full insider parse less often."
    if engine == "mispricing" and elapsed >= 90:
        return "Options chains are the likely bottleneck; use turbo cache or narrow --universe for focused scans."
    if engine in {"congress", "thirteen_f"} and elapsed >= 45:
        return "Regulatory/PDF parsing can be slow; rely on cache in loop mode or skip for quick scans."
    if engine in {"news", "sentiment", "gtrends", "twitter", "social"} and elapsed >= 30:
        return "Retail/news web sources are slow or rate-limited; turbo cache helps after the first run."
    if elapsed >= 60:
        return "Slow engine; check source health and consider a focused --universe scan."
    return "Healthy enough for now."


def _latest_finbert_device(data_dir: Path) -> dict[str, Any]:
    rows = []
    for pattern in ("top_options_*.parquet", "top_shares_*.parquet"):
        df = _read_parquet(_latest_file(data_dir, pattern))
        if not df.empty and "finbert_device" in df.columns:
            rows.extend(str(x) for x in df["finbert_device"].dropna().unique().tolist())
    devices = sorted(set(x for x in rows if x and x.lower() != "nan"))
    return {
        "device": devices[0] if devices else None,
        "devices_seen": devices,
        "status": "gpu" if any("cuda" in d.lower() for d in devices) else "unknown",
    }


def build_performance_summary(data_dir: Path = DATA_DIR) -> dict[str, Any]:
    """Summarize local speed, cache, and engine health telemetry for the cockpit."""
    try:
        from telemetry import cache_stats as _cache_stats
        cache_prefixes = _cache_stats.summary()
    except Exception:
        cache_prefixes = {}
    try:
        from telemetry import perf as _perf
        latest = _perf.latest_run_summary()
        rolling = _perf.summary(last_n=20)
    except Exception:
        latest = {}
        rolling = {}
    try:
        from telemetry import engine_health as _engine_health
        health = _engine_health.load_summary()
    except Exception:
        health = {"engines": []}

    latest_rows = []
    for engine, row in (latest or {}).items():
        item = {
            "engine": engine,
            "elapsed_sec": round(_float_value(row.get("elapsed_sec")), 2),
            "rows": int(_float_value(row.get("rows"))),
            "ok": bool(row.get("ok", False)),
        }
        item["tip"] = _performance_tip(item)
        latest_rows.append(item)
    latest_rows = sorted(latest_rows, key=lambda r: _float_value(r.get("elapsed_sec")), reverse=True)

    rolling_rows = []
    for engine, row in (rolling or {}).items():
        item = {
            "engine": engine,
            "n": int(_float_value(row.get("n"))),
            "mean_sec": round(_float_value(row.get("mean")), 2),
            "p95_sec": round(_float_value(row.get("p95")), 2),
            "ok_rate": _clean_value(row.get("ok_rate")),
            "avg_rows": round(_float_value(row.get("avg_rows")), 1),
        }
        item["tip"] = _performance_tip({"engine": engine, "last_elapsed": item["p95_sec"]})
        rolling_rows.append(item)
    rolling_rows = sorted(rolling_rows, key=lambda r: _float_value(r.get("p95_sec")), reverse=True)

    cache_rows = []
    for prefix, row in (cache_prefixes or {}).items():
        cache_rows.append({
            "prefix": prefix,
            "hits": int(_float_value(row.get("hits"))),
            "misses": int(_float_value(row.get("misses"))),
            "hit_rate": _clean_value(row.get("hit_rate")),
            "total": int(_float_value(row.get("total"))),
        })
    cache_rows = sorted(cache_rows, key=lambda r: int(r.get("total") or 0), reverse=True)[:12]

    engine_health_rows = (health or {}).get("engines", []) if isinstance(health, dict) else []
    if not latest_rows or max(_float_value(row.get("elapsed_sec")) for row in latest_rows) <= 0:
        for row in engine_health_rows:
            item = {
                "engine": row.get("engine"),
                "elapsed_sec": round(_float_value(row.get("last_elapsed")), 2),
                "rows": int(_float_value(row.get("last_rows"))),
                "ok": bool(row.get("last_ok", False)),
                "source": "engine_health",
            }
            item["tip"] = _performance_tip(item)
            latest_rows.append(item)
        latest_rows = sorted(latest_rows, key=lambda r: _float_value(r.get("elapsed_sec")), reverse=True)
    worst_health = sorted(
        engine_health_rows,
        key=lambda r: (_float_value(r.get("health_score"), default=100.0), str(r.get("engine") or "")),
    )[:12]
    ram_stats = data_provider.cache_stats()
    finbert = _latest_finbert_device(data_dir)
    total_latest_sec = sum(_float_value(row.get("elapsed_sec")) for row in latest_rows)
    warnings = []
    if latest_rows and _float_value(latest_rows[0].get("elapsed_sec")) >= 90:
        warnings.append(f"{latest_rows[0]['engine']} was the slowest recent engine at {latest_rows[0]['elapsed_sec']}s.")
    if not ram_stats.get("ram_cache_enabled"):
        warnings.append("RAM cache is disabled; --turbo enables it for loop scans.")
    if finbert.get("status") != "gpu":
        warnings.append("FinBERT GPU status is unknown in the latest local snapshots.")

    return {
        "generated_at": _now_iso(),
        "total_latest_engine_sec": round(total_latest_sec, 2),
        "ram_cache": ram_stats,
        "finbert": finbert,
        "latest_slowest": latest_rows[:12],
        "rolling_slowest": rolling_rows[:12],
        "cache_prefixes": cache_rows,
        "engine_health": worst_health,
        "warnings": warnings,
        "recommended_command": "python run.py --aggressive --bankroll 25000 --loop 30 --turbo --no-open",
        "notes": [
            "Performance summary reads local telemetry; it does not run a scan.",
            "Engine seconds are per-engine wall times and can overlap because engines run concurrently.",
            "RAM cache helps repeat work inside one loop process; disk cache helps across restarts.",
        ],
    }


def _command_ribbon_tone(status: str) -> str:
    text = str(status or "").lower()
    if text in {"bad", "blocked", "weak", "missing", "critical", "high"}:
        return "bad"
    if text in {"warn", "warning", "review", "partial", "stale", "aging", "mixed", "medium"}:
        return "warn"
    if text in {"ok", "good", "ready", "clean", "fresh", "low"}:
        return "good"
    return ""


def _find_health_check(checks: list[dict[str, Any]], label_fragment: str) -> dict[str, Any] | None:
    needle = label_fragment.lower()
    for row in checks:
        label = str(row.get("label") or "").lower()
        if needle in label:
            return row
    return None


def _snapshot_freshness_rollup(snapshots: dict[str, Any]) -> dict[str, Any]:
    counts = {"fresh": 0, "aging": 0, "stale": 0, "missing": 0}
    oldest_asset = None
    oldest_age = -1.0
    for asset, meta in (snapshots or {}).items():
        if not isinstance(meta, dict):
            counts["missing"] += 1
            continue
        age = _float_value(meta.get("age_minutes"), default=math.nan)
        if not math.isfinite(age):
            counts["missing"] += 1
        elif age <= FRESH_SNAPSHOT_MINUTES:
            counts["fresh"] += 1
        elif age <= STALE_SNAPSHOT_MINUTES:
            counts["aging"] += 1
        else:
            counts["stale"] += 1
        if math.isfinite(age) and age > oldest_age:
            oldest_asset = asset
            oldest_age = age
    if counts["missing"] or counts["stale"]:
        status = "stale"
    elif counts["aging"]:
        status = "aging"
    elif counts["fresh"]:
        status = "fresh"
    else:
        status = "missing"
    detail_parts = [f"{counts['fresh']} fresh", f"{counts['aging']} aging", f"{counts['stale']} stale", f"{counts['missing']} missing"]
    if oldest_asset is not None and oldest_age >= 0:
        detail_parts.append(f"oldest {oldest_asset} {oldest_age:.0f}m")
    return {"status": status, "counts": counts, "detail": " / ".join(detail_parts)}


def _build_trust_ribbon(
    health: dict[str, Any],
    sources: dict[str, Any],
    performance: dict[str, Any],
    chain: dict[str, Any],
) -> list[dict[str, Any]]:
    checks = health.get("checks") if isinstance(health.get("checks"), list) else []
    health_counts = {
        "ok": sum(1 for row in checks if row.get("level") == "ok"),
        "warn": sum(1 for row in checks if row.get("level") == "warn"),
        "bad": sum(1 for row in checks if row.get("level") == "bad"),
    }
    validation_check = _find_health_check(checks, "Validation open count")
    aging_check = _find_health_check(checks, "Position aging count")
    snapshot_rollup = _snapshot_freshness_rollup(
        health.get("snapshots") if isinstance(health.get("snapshots"), dict) else {}
    )
    chain_quality = chain.get("quality_summary") if isinstance(chain.get("quality_summary"), dict) else {}
    chain_status = chain_quality.get("status") or chain.get("status") or "missing"
    chain_score = _float_value(chain_quality.get("score"), default=0.0)
    chain_detail = (
        f"{int(_float_value(chain.get('count')))} saved, "
        f"{int(_float_value(chain_quality.get('primary_review_count')))} primary, "
        f"{int(_float_value(chain_quality.get('clean_swing_count')))} clean"
        if chain.get("status") == "ready"
        else "Run a 3m+ chain scan before choosing contracts."
    )
    validation_level = str((validation_check or {}).get("level") or "warn")
    validation_detail = str((validation_check or {}).get("detail") or "Validation summary has not been refreshed yet.")
    aging_level = str((aging_check or {}).get("level") or "warn")
    aging_detail = str((aging_check or {}).get("detail") or "Position aging summary has not been refreshed yet.")
    source_count = int(_float_value(sources.get("source_count")))
    no_key_count = int(_float_value(sources.get("no_key_count")))
    primary_count = int(_float_value(sources.get("primary_count")))
    perf_warnings = len(performance.get("warnings") or [])
    ram_cache = performance.get("ram_cache") if isinstance(performance.get("ram_cache"), dict) else {}
    finbert = performance.get("finbert") if isinstance(performance.get("finbert"), dict) else {}
    rows = [
        {
            "label": "Data integrity",
            "value": health.get("status") or "unknown",
            "detail": f"{health_counts['bad']} bad / {health_counts['warn']} warning / {health_counts['ok']} ok checks",
            "tone": _command_ribbon_tone(str(health.get("status") or "")),
        },
        {
            "label": "Snapshot freshness",
            "value": snapshot_rollup["status"],
            "detail": snapshot_rollup["detail"],
            "tone": _command_ribbon_tone(snapshot_rollup["status"]),
        },
        {
            "label": "Validation alignment",
            "value": validation_level,
            "detail": validation_detail,
            "tone": _command_ribbon_tone(validation_level),
        },
        {
            "label": "Position aging",
            "value": aging_level,
            "detail": aging_detail,
            "tone": _command_ribbon_tone(aging_level),
        },
        {
            "label": "Chain readiness",
            "value": f"{chain_status} {int(chain_score)}/100" if chain_score else chain_status,
            "detail": chain_detail,
            "tone": _command_ribbon_tone(str(chain_status)),
        },
        {
            "label": "Free sources",
            "value": f"{no_key_count}/{source_count}",
            "detail": f"{primary_count} primary no-key/public sources wired into Optedge.",
            "tone": "good" if no_key_count else "warn",
        },
        {
            "label": "Runtime",
            "value": f"{performance.get('total_latest_engine_sec', 0)}s",
            "detail": (
                f"{perf_warnings} warning(s); RAM cache {'on' if ram_cache.get('ram_cache_enabled') else 'off'}; "
                f"FinBERT {finbert.get('device') or finbert.get('status') or 'unknown'}."
            ),
            "tone": "warn" if perf_warnings else "good",
        },
    ]
    return [{k: _clean_value(v) for k, v in row.items()} for row in rows]


def _provider_probe(label: str, category: str, fn) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = fn()
        ok = bool(result.get("ok")) if isinstance(result, dict) else bool(result)
        row = result if isinstance(result, dict) else {}
    except Exception as exc:
        ok = False
        row = {"note": str(exc)[:180]}
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    status = "ok" if ok else "warn"
    return {
        "provider": label,
        "category": category,
        "status": status,
        "latency_ms": elapsed_ms,
        **{k: _clean_value(v) for k, v in row.items() if k != "ok"},
    }


def _history_probe_result(df: pd.DataFrame, note: str = "") -> dict[str, Any]:
    if df is None or df.empty:
        return {"ok": False, "rows": 0, "note": note or "No rows returned."}
    close = None
    try:
        close = float(df["Close"].dropna().iloc[-1])
    except Exception:
        close = None
    return {
        "ok": True,
        "rows": int(len(df)),
        "last_close": _clean_value(round(close, 4) if close is not None else None),
        "history_source": _clean_value(getattr(df, "attrs", {}).get("history_source")),
        "history_quality": _clean_value(getattr(df, "attrs", {}).get("history_quality")),
        "note": note or "Returned OHLCV rows.",
    }


def _chain_probe_result(blob: dict[str, Any]) -> dict[str, Any]:
    chains = blob.get("chains") if isinstance(blob, dict) else None
    attempts = blob.get("source_attempts") if isinstance(blob, dict) else None
    attempts = attempts if isinstance(attempts, list) else []
    usable_attempts = [
        row for row in attempts
        if isinstance(row, dict) and str(row.get("status") or "").lower() == "ok"
    ]
    failed_attempts = [
        row for row in attempts
        if isinstance(row, dict) and str(row.get("status") or "").lower() != "ok"
    ]
    attempt_names = [
        str(row.get("provider") or row.get("source") or "")
        for row in attempts
        if isinstance(row, dict) and (row.get("provider") or row.get("source"))
    ]
    attempt_summary = "; ".join(
        f"{row.get('provider') or row.get('source')}:"
        f"{row.get('status') or 'unknown'}"
        f"/{int(_float_value(row.get('rows'), default=0.0))}"
        for row in attempts
        if isinstance(row, dict)
    )
    if not chains:
        return {
            "ok": False,
            "rows": 0,
            "providers_checked": len(attempts),
            "usable_provider_count": len(usable_attempts),
            "failed_provider_count": len(failed_attempts),
            "provider_attempts": ", ".join(attempt_names) if attempt_names else None,
            "provider_attempt_summary": attempt_summary or None,
            "note": "No option-chain rows returned.",
        }
    total = 0
    for df in chains.values():
        if isinstance(df, pd.DataFrame):
            total += len(df)
        elif isinstance(df, list):
            total += len(df)
    return {
        "ok": total > 0,
        "rows": total,
        "source": blob.get("source"),
        "quote_quality": blob.get("quote_quality") or ("free_or_delayed" if blob.get("source") else None),
        "data_delay": blob.get("data_delay"),
        "providers_checked": len(attempts),
        "usable_provider_count": len(usable_attempts),
        "failed_provider_count": len(failed_attempts),
        "provider_attempts": ", ".join(attempt_names) if attempt_names else None,
        "provider_attempt_summary": attempt_summary or None,
        "spot": _clean_value(blob.get("spot")),
        "note": f"{len(chains)} expiration(s) returned.",
    }


def _companyfacts_probe_result(report: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(report, dict):
        return {"ok": False, "rows": 0, "note": "No SEC companyfacts report returned."}
    rows = report.get("rows") if isinstance(report.get("rows"), list) else []
    metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
    watch = report.get("watch_signals") if isinstance(report.get("watch_signals"), list) else []
    metric_count = sum(1 for value in metrics.values() if value is not None)
    row_count = int(_float_value(report.get("count"), default=float(len(rows))))
    ok = row_count > 0 or metric_count > 0
    note = "Official SEC company facts returned."
    if not ok:
        note = str(report.get("error") or "No usable company facts returned for this symbol.")
    return {
        "ok": ok,
        "rows": row_count,
        "metric_count": metric_count,
        "source": report.get("source") or "sec_companyfacts",
        "company_name": report.get("company_name"),
        "cik": report.get("cik"),
        "watch_signals": "; ".join(str(item) for item in watch[:3]) if watch else None,
        "note": note,
    }


def _fetch_option_chain_for_provider_status(symbol: str) -> dict[str, Any]:
    try:
        return _fetch_option_chain(symbol, cache_age=600, include_diagnostics=True)
    except TypeError:
        return _fetch_option_chain(symbol, cache_age=600)


def build_free_data_sources(data_dir: Path = DATA_DIR) -> dict[str, Any]:
    """Return the built-in free/no-key data source map used by the local cockpit."""
    sec_meta = sec_company_cache_meta(Path(data_dir) / "sec_company_tickers.json")
    nasdaq_meta = nasdaq_symbol_cache_meta(Path(data_dir) / "nasdaq_symbol_directory.json")
    cache = data_provider.cache_stats()
    rows: list[dict[str, Any]] = []
    categories: dict[str, int] = {}
    quality_counts: dict[str, int] = {}
    no_key_count = 0
    for idx, raw in enumerate(FREE_DATA_SOURCE_REGISTRY, start=1):
        row = dict(raw)
        row["rank"] = idx
        row["status_hint"] = "active"
        if row.get("name") == "SEC EDGAR":
            row["local_cache_status"] = sec_meta.get("status")
            row["local_cache_rows"] = sec_meta.get("row_count")
        elif row.get("name") == "Nasdaq Trader symbol directory":
            row["local_cache_status"] = nasdaq_meta.get("status")
            row["local_cache_rows"] = nasdaq_meta.get("row_count")
        else:
            row["local_cache_status"] = None
            row["local_cache_rows"] = None
        if row.get("credential") == "none":
            no_key_count += 1
        category = str(row.get("category") or "unknown")
        quality = str(row.get("quality") or "unknown")
        categories[category] = categories.get(category, 0) + 1
        quality_counts[quality] = quality_counts.get(quality, 0) + 1
        rows.append({k: _clean_value(v) for k, v in row.items()})
    return {
        "generated_at": _now_iso(),
        "source_count": len(rows),
        "no_key_count": no_key_count,
        "primary_count": sum(1 for row in rows if row.get("primary")),
        "category_counts": categories,
        "quality_counts": quality_counts,
        "sec_cache": sec_meta,
        "nasdaq_symbol_cache": nasdaq_meta,
        "ram_cache": cache,
        "rows": rows,
        "notes": [
            "This registry lists free/no-key sources currently wired into Optedge.",
            "Use Provider Status for a live symbol-level probe; this map explains coverage and caveats.",
            "Free sources are research-grade and may be delayed, rate-limited, partial, or unavailable.",
        ],
    }


def build_provider_status(
    data_dir: Path = DATA_DIR,
    query: str = "AAPL",
    include_chain: bool = True,
) -> dict[str, Any]:
    """Check the health of free/no-key providers without running a scan."""
    resolution = resolve_symbol(query or "AAPL")
    symbol = str(resolution.get("symbol") or query or "AAPL").upper()
    rows = [
        _provider_probe(
            "Yahoo chart",
            "history",
            lambda: _history_probe_result(data_provider._yahoo_v8_history(symbol, "1mo", "1d")),
        ),
        _provider_probe(
            "Nasdaq historical",
            "history",
            lambda: _history_probe_result(data_provider._nasdaq_history(symbol, "1mo", "1d")),
        ),
        _provider_probe(
            "Stooq CSV",
            "history",
            lambda: _history_probe_result(
                data_provider._stooq_history(symbol, "1mo", "1d"),
                "Last-resort fallback; can be blocked by browser verification.",
            ),
        ),
    ]

    if include_chain and not symbol.endswith("=F") and not symbol.startswith("^"):
        rows.append(_provider_probe(
            "Option chain stack",
            "options",
            lambda: _chain_probe_result(_fetch_option_chain_for_provider_status(symbol)),
        ))
    elif include_chain:
        rows.append({
            "provider": "Option chain stack",
            "category": "options",
            "status": "warn",
            "latency_ms": 0,
            "rows": 0,
            "note": "Skipped because this symbol is not an equity/ETF option-chain request.",
        })

    if not symbol.endswith("=F") and not symbol.startswith("^"):
        rows.append(_provider_probe(
            "SEC company facts",
            "fundamentals",
            lambda: _companyfacts_probe_result(companyfacts_for_symbol(symbol, limit=8)),
        ))
    else:
        rows.append({
            "provider": "SEC company facts",
            "category": "fundamentals",
            "status": "warn",
            "latency_ms": 0,
            "rows": 0,
            "note": "Skipped because this symbol is not a company equity fundamentals request.",
        })

    sec_meta = sec_company_cache_meta(data_dir / "sec_company_tickers.json")
    rows.append({
        "provider": "SEC company ticker cache",
        "category": "symbol_search",
        "status": "ok" if sec_meta.get("status") in {"fresh", "stale"} else "warn",
        "latency_ms": 0,
        "rows": sec_meta.get("row_count"),
        "source": sec_meta.get("status"),
        "note": "Local free company-name search cache.",
    })
    nasdaq_meta = nasdaq_symbol_cache_meta(data_dir / "nasdaq_symbol_directory.json")
    rows.append({
        "provider": "Nasdaq symbol directory cache",
        "category": "symbol_search",
        "status": "ok" if nasdaq_meta.get("status") in {"fresh", "stale"} else "warn",
        "latency_ms": 0,
        "rows": nasdaq_meta.get("row_count"),
        "source": nasdaq_meta.get("status"),
        "note": "Official no-key symbol directory for broader ticker search and ETF flags.",
    })

    history_rows = [row for row in rows if row.get("category") == "history"]
    working_history = [row for row in history_rows if row.get("status") == "ok"]
    chain_rows = [row for row in rows if row.get("category") == "options"]
    chain_ok = (not include_chain) or any(row.get("status") == "ok" for row in chain_rows)
    facts_rows = [row for row in rows if row.get("category") == "fundamentals"]
    facts_ok = any(row.get("status") == "ok" for row in facts_rows)
    symbol_cache_ok = sum(1 for row in rows if row.get("category") == "symbol_search" and row.get("status") == "ok")
    history_sources = [
        str(row.get("history_source") or row.get("provider") or "").strip()
        for row in working_history
        if row.get("history_source") or row.get("provider")
    ]
    history_quality_counts: dict[str, int] = {}
    for row in working_history:
        quality = str(row.get("history_quality") or "unknown")
        history_quality_counts[quality] = history_quality_counts.get(quality, 0) + 1
    history_score = min(55, round((len(working_history) / max(1, len(history_rows))) * 55))
    chain_score = 25 if chain_ok else 0
    cache_score = round((min(symbol_cache_ok, 2) / 2) * 20)
    trust_score = int(history_score + chain_score + cache_score)
    chain_probe = chain_rows[0] if chain_rows else {}
    chain_quote_quality = str(chain_probe.get("quote_quality") or "")
    chain_rows_count = int(_float_value(chain_probe.get("rows"), default=0.0))
    chain_warning = ""
    if include_chain and not chain_ok:
        chain_warning = "option chain did not return usable rows"
    elif include_chain and chain_quote_quality and chain_quote_quality != "live_or_broker":
        chain_warning = "option chain is delayed/research-grade; verify live quotes before manual paper entry"
    if not working_history:
        trust_label = "blocked"
    elif chain_ok:
        trust_label = "ready"
    else:
        trust_label = "partial"

    ok_count = sum(1 for row in rows if row.get("status") == "ok")
    warnings = [
        f"{row.get('provider')} did not return usable data."
        for row in rows if row.get("status") != "ok"
    ]
    return {
        "generated_at": _now_iso(),
        "query": query,
        "symbol": symbol,
        "resolution": resolution,
        "status": "ok" if ok_count >= 2 and not warnings else "warn",
        "ok_count": ok_count,
        "provider_count": len(rows),
        "data_trust": {
            "label": trust_label,
            "score": trust_score,
            "history_ok_count": len(working_history),
            "history_provider_count": len(history_rows),
            "history_sources": history_sources,
            "history_source_summary": ", ".join(history_sources) if history_sources else "none",
            "history_quality_counts": history_quality_counts,
            "option_chain_status": (
                "skipped" if not include_chain else "ok" if chain_ok else "warn"
            ),
            "option_chain_source": _clean_value(chain_probe.get("source")),
            "option_chain_rows": chain_rows_count,
            "option_chain_quote_quality": _clean_value(chain_quote_quality),
            "option_chain_data_delay": _clean_value(chain_probe.get("data_delay")),
            "option_chain_providers_checked": int(_float_value(chain_probe.get("providers_checked"), default=0.0)),
            "option_chain_usable_provider_count": int(_float_value(chain_probe.get("usable_provider_count"), default=0.0)),
            "option_chain_failed_provider_count": int(_float_value(chain_probe.get("failed_provider_count"), default=0.0)),
            "option_chain_provider_summary": _clean_value(chain_probe.get("provider_attempt_summary")),
            "option_chain_warning": chain_warning,
            "sec_companyfacts_status": "ok" if facts_ok else "warn",
            "sec_companyfacts_rows": sum(int(_float_value(row.get("rows"), default=0.0)) for row in facts_rows),
            "symbol_cache_ok_count": symbol_cache_ok,
            "notes": [
                "Ready means at least one free history source worked and the option-chain check passed or was skipped.",
                "This is source trust only; it is not a signal-quality or profitability score.",
            ],
        },
        "rows": rows,
        "warnings": warnings,
        "notes": [
            "Provider status checks public/free sources only.",
            "History checks are research-grade delayed data, not live execution quotes.",
            "Use this before a focused scan when free providers look flaky.",
        ],
    }


def _queue_item(priority: int, category: str, label: str, detail: str,
                action: str, symbol: Any = None, query: Any = None) -> dict[str, Any]:
    return {
        "priority": int(priority),
        "category": category,
        "label": label,
        "detail": detail,
        "action": action,
        "symbol": _clean_value(symbol),
        "query": _clean_value(query or symbol),
    }


def _queue_dedupe_key(item: dict[str, Any]) -> tuple[str, str, str]:
    category = str(item.get("category") or "")
    action = str(item.get("action") or "")
    symbol = str(item.get("symbol") or item.get("query") or "").upper()
    if symbol:
        return category, action, symbol
    return category, action, str(item.get("label") or item.get("detail") or "")


def _dedupe_queue_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    counts: dict[tuple[str, str, str], int] = {}
    for item in items:
        key = _queue_dedupe_key(item)
        counts[key] = counts.get(key, 0) + 1
        current = grouped.get(key)
        if current is None or int(item.get("priority") or 0) > int(current.get("priority") or 0):
            grouped[key] = dict(item)
    out = []
    for key, item in grouped.items():
        count = counts.get(key, 1)
        if count > 1:
            item["grouped_count"] = count
            item["detail"] = f"{item.get('detail', '')} ({count} related items grouped.)"
        else:
            item["grouped_count"] = 1
        out.append(item)
    return out


def build_action_queue(data_dir: Path = DATA_DIR, limit: int = 20) -> dict[str, Any]:
    """Prioritize the next research actions from local cockpit state."""
    items: list[dict[str, Any]] = []
    refresh_warnings: list[str] = []

    health = build_data_health(data_dir)
    for check in health.get("checks", []):
        level = str(check.get("level") or "ok")
        label = str(check.get("label") or "Data health warning")
        if level == "bad":
            items.append(_queue_item(
                100, "data_health", check.get("label") or "Data health issue",
                check.get("detail") or "A dashboard data-health check failed.",
                "refresh_or_fix_artifact",
            ))
        elif level == "warn":
            if _is_refreshable_market_warning(label):
                refresh_warnings.append(label)
                continue
            action = (
                "warm_symbol_caches"
                if label.startswith(("SEC ticker cache", "Nasdaq symbol directory"))
                else "review_data_health"
            )
            items.append(_queue_item(
                75 if action == "warm_symbol_caches" else 70,
                "data_health", check.get("label") or "Data health warning",
                check.get("detail") or "A dashboard data-health warning is active.",
                action,
            ))
    if refresh_warnings:
        preview = ", ".join(refresh_warnings[:4])
        if len(refresh_warnings) > 4:
            preview += f", +{len(refresh_warnings) - 4} more"
        items.append(_queue_item(
            82,
            "data_health",
            "Refresh stale market snapshots",
            f"{len(refresh_warnings)} freshness warning(s): {preview}.",
            "run_refresh_scan",
        ))

    attention = build_positions(data_dir, status="attention", limit=8).get("rows", [])
    for row in attention:
        pressure = _float_value(row.get("latest_exit_pressure"))
        pnl = _float_value(row.get("pnl_pct"))
        priority = 95 if pressure >= 80 else 85 if pressure >= 60 else 75
        if pnl <= -0.30:
            priority = max(priority, 90)
        symbol = row.get("ticker_or_symbol")
        detail = (
            f"{row.get('position_label')} has exit pressure "
            f"{row.get('latest_exit_pressure') or 0} and open P&L {pnl * 100:+.1f}%."
        )
        items.append(_queue_item(
            priority, "open_position", "Review open position",
            detail, "open_position_monitor", symbol=symbol, query=symbol,
        ))

    try:
        paper = build_paper_candidates(data_dir, max_new=5, dry_run=False)
        for row in paper.get("rows", [])[:5]:
            symbol = row.get("ticker_or_symbol")
            asset = row.get("asset")
            confidence = row.get("confidence")
            detail = f"{asset} candidate {symbol} is eligible for manual paper review"
            if confidence not in (None, ""):
                detail += f" at confidence {confidence}"
            detail += "."
            items.append(_queue_item(
                55, "paper_candidate", "Review paper candidate",
                detail, "preview_paper_candidate", symbol=symbol, query=symbol,
            ))
    except Exception as exc:
        items.append(_queue_item(
            65, "paper_candidate", "Paper candidate build failed",
            f"Could not build paper candidates: {str(exc)[:160]}",
            "review_paper_export",
        ))

    try:
        watchlist = load_watchlist(data_dir, enrich=True).get("entries", [])
        for row in watchlist:
            local_hits = int(_float_value(row.get("local_hits"), 0.0))
            if local_hits == 0:
                items.append(_queue_item(
                    45, "watchlist", "Run focused watchlist scan",
                    f"{row.get('query') or row.get('symbol')} has no current local scan rows.",
                    "run_focused_scan", symbol=row.get("symbol"), query=row.get("query") or row.get("symbol"),
                ))
                continue
            readiness = str(row.get("paper_readiness_status") or "").lower()
            score = _float_value(row.get("paper_readiness_score"), 0.0)
            query = row.get("query") or row.get("symbol")
            if readiness == "ready":
                items.append(_queue_item(
                    62, "watchlist", "Review ready watchlist idea",
                    f"{query} has paper-readiness score {score:.0f}/100.",
                    "preview_paper_candidate", symbol=row.get("symbol"), query=query,
                ))
            elif readiness in {"caution", "blocked"}:
                items.append(_queue_item(
                    48 if readiness == "caution" else 58,
                    "watchlist", "Recheck watchlist idea",
                    f"{query} readiness is {readiness} at {score:.0f}/100.",
                    "run_focused_scan", symbol=row.get("symbol"), query=query,
                ))
    except Exception as exc:
        items.append(_queue_item(
            40, "watchlist", "Watchlist enrichment failed",
            f"Could not enrich watchlist: {str(exc)[:160]}",
            "review_watchlist",
        ))

    if not items:
        items.append(_queue_item(
            10, "system", "No urgent local actions",
            "Data health is clean and no high-priority open-position or paper-candidate items surfaced.",
            "continue_monitoring",
        ))

    items = sorted(_dedupe_queue_items(items), key=lambda item: item["priority"], reverse=True)[:limit]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(items),
        "rows": items,
        "notes": [
            "Action queue is local decision support only; it does not place trades.",
            "Highest priority goes to bad data health and open-position exit risk.",
            "Paper candidates remain manual review items.",
        ],
    }


def _can_scan_option_chain_symbol(symbol: Any, asset: Any = "") -> bool:
    clean = str(symbol or "").strip().upper()
    kind = str(asset or "").strip().lower()
    if not clean or kind == "futures":
        return False
    return not clean.endswith("=F") and not clean.startswith("^")


def _today_review_item(
    priority: float,
    category: str,
    label: str,
    detail: str,
    action: str,
    route: str,
    *,
    symbol: Any = None,
    query: Any = None,
    source: str | None = None,
    asset: Any = None,
) -> dict[str, Any]:
    return {
        "priority": int(max(0, min(100, round(_float_value(priority))))),
        "category": category,
        "label": label,
        "detail": detail,
        "action": action,
        "route": route,
        "symbol": _clean_value(symbol),
        "query": _clean_value(query or symbol),
        "asset": _clean_value(asset),
        "source": source or category,
    }


def _today_route_for_queue_action(action: Any) -> str:
    clean = str(action or "").strip().lower()
    if clean in {"open_position_monitor"}:
        return "positions"
    if clean in {"preview_paper_candidate", "review_paper_export"}:
        return "paper"
    if clean in {
        "review_data_health", "refresh_or_fix_artifact", "warm_sec_ticker_cache",
        "warm_symbol_caches", "run_refresh_scan",
    }:
        return "data_health"
    if clean in {"run_focused_scan", "review_watchlist"}:
        return "research"
    return "research"


def build_today_review(data_dir: Path = DATA_DIR, limit: int = 12) -> dict[str, Any]:
    """Compose the first-screen review queue from setups, saved contracts, and open risk."""
    limit = max(1, min(int(limit or 12), 40))
    items: list[dict[str, Any]] = []
    notes: list[str] = []
    climate_label = None
    climate_score = None
    climate_posture = None

    try:
        gated = build_climate_gated_setups(data_dir, per_asset=4, limit=12, include_held=True)
        climate_label = gated.get("climate_label")
        climate_score = gated.get("climate_score")
        climate_posture = gated.get("posture")
        for idx, row in enumerate((gated.get("rows") or [])[:8]):
            symbol = row.get("ticker_or_symbol")
            asset = row.get("asset")
            action = "scan_swing_chain" if _can_scan_option_chain_symbol(symbol, asset) else "open_research"
            route = "chains" if action == "scan_swing_chain" else "research"
            reasons = row.get("climate_gate_reasons")
            if isinstance(reasons, list):
                reason_text = "; ".join(str(x) for x in reasons[:3])
            else:
                reason_text = str(reasons or "passes current climate gates")
            detail = (
                f"{row.get('setup') or symbol} passed at gate score "
                f"{row.get('climate_gate_score')} with readiness {row.get('readiness_score')}. "
                f"{reason_text}"
            )
            items.append(_today_review_item(
                94 - idx,
                "setup",
                "Review climate-cleared setup",
                detail,
                action,
                route,
                symbol=symbol,
                query=symbol,
                source="climate_gated_setups",
                asset=asset,
            ))
        if not gated.get("rows") and gated.get("held"):
            held = gated.get("held", [])[0]
            symbol = held.get("ticker_or_symbol")
            reasons = held.get("climate_gate_reasons")
            reason_text = ", ".join(str(x) for x in reasons[:3]) if isinstance(reasons, list) else str(reasons or "")
            items.append(_today_review_item(
                66,
                "setup",
                "Best setup is held",
                f"{held.get('setup') or symbol} is closest, but held by: {reason_text or 'current gates'}.",
                "open_research",
                "research",
                symbol=symbol,
                query=symbol,
                source="climate_gated_setups",
                asset=held.get("asset"),
            ))
    except Exception as exc:
        notes.append(f"Climate-gated setup review failed: {str(exc)[:160]}")
        items.append(_today_review_item(
            60,
            "setup",
            "Setup review unavailable",
            f"Could not build climate-gated setup review: {str(exc)[:160]}",
            "review_data_health",
            "data_health",
            source="climate_gated_setups",
        ))

    try:
        saved = build_saved_option_contracts(data_dir, enrich=True, limit=40, refresh_quotes=False)
        for row in (saved.get("rows") or [])[:14]:
            review_action = str(row.get("review_action") or "").lower()
            score = _float_value(row.get("review_score"), default=0.0)
            if review_action == "review_now":
                priority = 96 + score / 100.0
                label = "Review saved option contract"
                action = "scan_swing_chain"
                route = "chains"
            elif review_action == "refresh_quote":
                priority = 84 + score / 200.0
                label = "Refresh saved option quote"
                action = "refresh_saved_quote"
                route = "chains"
            elif review_action == "watch":
                priority = 58 + score / 200.0
                label = "Watch saved option contract"
                action = "scan_swing_chain"
                route = "chains"
            else:
                continue
            query = row.get("query") or row.get("symbol")
            reasons = row.get("review_reasons")
            reason_text = ", ".join(str(x) for x in reasons[:4]) if isinstance(reasons, list) else str(reasons or "")
            detail = (
                f"{row.get('symbol')} {row.get('expiry')} {row.get('side_code') or row.get('side')} "
                f"{row.get('strike')} has review score {row.get('review_score')}. "
                f"{reason_text or row.get('status') or 'saved for review'}"
            )
            items.append(_today_review_item(
                priority,
                "saved_contract",
                label,
                detail,
                action,
                route,
                symbol=row.get("symbol"),
                query=query,
                source="saved_option_contracts",
                asset="option",
            ))
    except Exception as exc:
        notes.append(f"Saved-contract review failed: {str(exc)[:160]}")

    try:
        risk = build_risk_summary(data_dir)
        for idx, row in enumerate((risk.get("highest_exit_pressure") or [])[:8]):
            pressure = _float_value(row.get("latest_exit_pressure"), default=0.0)
            if pressure < 40:
                continue
            symbol = row.get("ticker_or_symbol")
            priority = 98 if pressure >= 80 else 88 if pressure >= 60 else 72
            detail = (
                f"{row.get('position_label') or symbol} has exit pressure {row.get('latest_exit_pressure')} "
                f"and open P&L {row.get('pnl_pct')}."
            )
            items.append(_today_review_item(
                priority - idx,
                "position_risk",
                "Review open-position exit risk",
                detail,
                "open_position_monitor",
                "positions",
                symbol=symbol,
                query=symbol,
                source="risk_summary",
                asset=row.get("asset"),
            ))
        for warning in (risk.get("warnings") or [])[:3]:
            items.append(_today_review_item(
                76,
                "position_risk",
                "Portfolio risk warning",
                str(warning),
                "open_position_monitor",
                "positions",
                source="risk_summary",
            ))
    except Exception as exc:
        notes.append(f"Risk review failed: {str(exc)[:160]}")

    try:
        queue = build_action_queue(data_dir, limit=12)
        for row in (queue.get("rows") or [])[:10]:
            priority = min(_float_value(row.get("priority"), default=0.0), 82.0)
            items.append(_today_review_item(
                priority,
                str(row.get("category") or "action_item"),
                str(row.get("label") or "Review action item"),
                str(row.get("detail") or ""),
                str(row.get("action") or "open_research"),
                _today_route_for_queue_action(row.get("action")),
                symbol=row.get("symbol"),
                query=row.get("query"),
                source="action_queue",
            ))
    except Exception as exc:
        notes.append(f"Action queue merge failed: {str(exc)[:160]}")

    items = sorted(_dedupe_queue_items(items), key=lambda item: int(item.get("priority") or 0), reverse=True)
    rows = [{k: _clean_value(v) for k, v in item.items()} for item in items[:limit]]
    category_counts: dict[str, int] = {}
    action_counts: dict[str, int] = {}
    for row in rows:
        category = str(row.get("category") or "unknown")
        action = str(row.get("action") or "unknown")
        category_counts[category] = category_counts.get(category, 0) + 1
        action_counts[action] = action_counts.get(action, 0) + 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(rows),
        "climate_label": _clean_value(climate_label),
        "climate_score": _clean_value(climate_score),
        "climate_posture": _clean_value(climate_posture),
        "category_counts": category_counts,
        "action_counts": action_counts,
        "review_now_count": sum(1 for row in rows if row.get("action") in {"scan_swing_chain", "refresh_saved_quote"}),
        "risk_count": category_counts.get("position_risk", 0),
        "setup_count": category_counts.get("setup", 0),
        "saved_contract_count": category_counts.get("saved_contract", 0),
        "rows": rows,
        "notes": notes + [
            "Today Review merges local setup gates, saved contracts, open-position risk, and action queue items.",
            "Open moves are routing actions only; no broker execution is performed.",
        ],
    }


def _command_center_status(health_status: str, risk_level: str, review_count: int) -> tuple[str, str]:
    health = str(health_status or "unknown").lower()
    risk = str(risk_level or "unknown").lower()
    if health == "bad" or risk in {"high", "critical"}:
        return "fix_first", "Fix data or risk before adding new ideas."
    if health == "warn" or risk in {"elevated", "medium"}:
        return "review_first", "Review warnings, then only act on the cleanest setup."
    if review_count > 0:
        return "ready_to_review", "Review the top queue item before opening anything new."
    return "quiet", "No urgent queue items surfaced; wait or run a fresh scan."


def _command_swing_action(row: dict[str, Any]) -> dict[str, Any]:
    symbol = row.get("ticker_or_symbol")
    asset = row.get("asset")
    action = (
        "scan_swing_chain"
        if str(asset or "").lower() == "option" and _can_scan_option_chain_symbol(symbol, asset)
        else "run_focused_scan"
    )
    route = "chains" if action == "scan_swing_chain" else "research"
    reasons = row.get("reasons") if isinstance(row.get("reasons"), list) else []
    warnings = row.get("warnings") if isinstance(row.get("warnings"), list) else []
    factor_breakdown = row.get("factor_breakdown") if isinstance(row.get("factor_breakdown"), list) else []
    factor_summary = row.get("factor_summary") or _swing_factor_summary(factor_breakdown)
    reason_text = ", ".join(str(item) for item in reasons[:3]) or str(row.get("lane") or "swing radar")
    factor_text = f" Factors: {factor_summary}." if factor_summary else ""
    warning_text = f" Warnings: {', '.join(str(item) for item in warnings[:2])}." if warnings else ""
    score = _float_value(row.get("swing_scout_score"), default=0.0)
    detail = (
        f"{row.get('setup') or symbol} scored {score:.0f}/100 in {str(row.get('lane') or 'swing radar').replace('_', ' ')}. "
        f"{reason_text}.{factor_text}{warning_text}"
    )
    return {
        "priority": int(round(_clamp(score, 0.0, 100.0))),
        "asset": _clean_value(asset),
        "label": _clean_value(row.get("setup") or symbol or "Swing radar item"),
        "detail": detail,
        "action": action,
        "route": route,
        "symbol": _clean_value(symbol),
        "query": _clean_value(symbol),
        "source": "swing_scout",
        "lane": _clean_value(row.get("lane")),
        "score": _clean_value(row.get("swing_scout_score")),
        "readiness": _clean_value(row.get("readiness_label")),
        "snapshot_freshness": _clean_value(row.get("snapshot_freshness")),
        "factor_summary": _clean_value(factor_summary),
        "factor_breakdown": factor_breakdown[:4],
    }


def build_command_center(data_dir: Path = DATA_DIR) -> dict[str, Any]:
    """Build a first-screen decision summary from local cockpit artifacts."""
    health = build_data_health(data_dir)
    today = build_today_review(data_dir, limit=8)
    risk = build_risk_summary(data_dir)
    sources = build_free_data_sources(data_dir)
    performance = build_performance_summary(data_dir)
    chain = _build_chain_shortlist_summary(data_dir)
    notes = [
        "Command Center is a first-pass review surface built from local Optedge artifacts.",
        "It does not place trades and does not replace the detailed panels below.",
        "If data trust is warn/bad, refresh or inspect artifacts before acting on any setup.",
    ]
    try:
        swing = build_swing_scout(data_dir, limit=5, include_wait=False, include_nasdaq_movers=False)
    except Exception as exc:
        swing = {"rows": [], "count": 0}
        notes.append(f"Swing radar unavailable: {str(exc)[:160]}")
    swing_actions = [_command_swing_action(row) for row in (swing.get("rows") or [])[:5]]

    checks = health.get("checks") if isinstance(health.get("checks"), list) else []
    health_counts = {
        "ok": sum(1 for row in checks if row.get("level") == "ok"),
        "warn": sum(1 for row in checks if row.get("level") == "warn"),
        "bad": sum(1 for row in checks if row.get("level") == "bad"),
    }
    first_action = (today.get("rows") or [{}])[0] if isinstance(today.get("rows"), list) else {}
    status, status_detail = _command_center_status(
        str(health.get("status") or "unknown"),
        str(risk.get("risk_level") or "unknown"),
        int(_float_value(today.get("count"), default=0.0)),
    )
    cards = [
        {
            "label": "Market posture",
            "value": today.get("climate_label") or "-",
            "detail": today.get("climate_posture") or "Use the swing climate panel for the full playbook.",
            "tone": "good" if _float_value(today.get("climate_score"), default=50.0) >= 60 else "warn",
        },
        {
            "label": "Data trust",
            "value": health.get("status") or "-",
            "detail": f"{health_counts['bad']} bad / {health_counts['warn']} warning health checks.",
            "tone": "bad" if health_counts["bad"] else "warn" if health_counts["warn"] else "good",
        },
        {
            "label": "Open risk",
            "value": risk.get("risk_level") or "-",
            "detail": f"{risk.get('attention_count', 0)} attention item(s), {risk.get('high_exit_pressure_count', 0)} high-pressure exit(s).",
            "tone": "bad" if str(risk.get("risk_level") or "").lower() in {"high", "critical"} else "warn" if risk.get("attention_count") else "good",
        },
        {
            "label": "Free source stack",
            "value": f"{sources.get('no_key_count', 0)}/{sources.get('source_count', 0)}",
            "detail": "No-key sources currently wired into the cockpit.",
            "tone": "good",
        },
        {
            "label": "Runtime",
            "value": f"{performance.get('total_latest_engine_sec', 0)}s",
            "detail": f"{len(performance.get('warnings') or [])} speed/data warning(s).",
            "tone": "warn" if performance.get("warnings") else "good",
        },
        {
            "label": "Swing radar",
            "value": len(swing_actions),
            "detail": (
                f"Top local candidate: {swing_actions[0].get('label')}"
                if swing_actions else "No local swing-radar candidates cleared the current filters."
            ),
            "tone": "good" if swing_actions else "warn",
        },
    ]
    next_action = {
        "priority": first_action.get("priority"),
        "label": first_action.get("label") or "No urgent action",
        "detail": first_action.get("detail") or status_detail,
        "action": first_action.get("action") or "open_research",
        "route": first_action.get("route") or "research",
        "symbol": first_action.get("symbol"),
        "query": first_action.get("query") or first_action.get("symbol"),
        "source": first_action.get("source"),
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "status_detail": status_detail,
        "climate_label": today.get("climate_label"),
        "climate_score": today.get("climate_score"),
        "review_count": today.get("count", 0),
        "review_now_count": today.get("review_now_count", 0),
        "data_health_status": health.get("status"),
        "health_counts": health_counts,
        "risk_level": risk.get("risk_level"),
        "total_open": risk.get("total_open", health.get("total_open", 0)),
        "source_count": sources.get("source_count", 0),
        "no_key_count": sources.get("no_key_count", 0),
        "primary_source_count": sources.get("primary_count", 0),
        "trust_ribbon": _build_trust_ribbon(health, sources, performance, chain),
        "next_action": {k: _clean_value(v) for k, v in next_action.items()},
        "cards": [{k: _clean_value(v) for k, v in row.items()} for row in cards],
        "top_queue": today.get("rows", [])[:4],
        "swing_radar_count": len(swing_actions),
        "swing_actions": [{k: _clean_value(v) for k, v in row.items()} for row in swing_actions],
        "notes": notes,
    }


def artifact_path(name: str, data_dir: Path = DATA_DIR) -> Path | None:
    spec = ARTIFACTS.get(name)
    if spec is None:
        return None
    pattern, _ = spec
    if "*" in pattern:
        return _latest_file(data_dir, pattern)
    path = data_dir / pattern
    return path if path.exists() and path.is_file() else None


def _packet_call(
    notes: list[str],
    label: str,
    default: Any,
    func: Any,
    *args: Any,
    **kwargs: Any,
) -> Any:
    try:
        result = func(*args, **kwargs)
        return result if result is not None else default
    except Exception as exc:
        notes.append(f"{label} unavailable: {str(exc)[:160]}")
        return default


def _packet_rows(
    rows: list[dict[str, Any]] | None,
    fields: list[str],
    limit: int = 6,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in (rows or [])[: max(1, int(limit or 6))]:
        if not isinstance(raw, dict):
            continue
        row = {field: _clean_value(raw.get(field)) for field in fields if field in raw}
        if row:
            out.append(row)
    return out


def _build_chain_shortlist_summary(data_dir: Path) -> dict[str, Any]:
    df = _load_option_chain_shortlist(data_dir)
    csv_path = artifact_path("option-chain-shortlist", data_dir)
    json_path = artifact_path("option-chain-shortlist-json", data_dir)
    if df is None or df.empty:
        return {
            "status": "missing",
            "count": 0,
            "csv_path": str(csv_path) if csv_path else None,
            "json_path": str(json_path) if json_path else None,
            "rows": [],
            "notes": ["Run the shortlist chain sweep, then write shortlist files to feed this packet."],
        }
    top = df.copy()
    sort_cols = [col for col in ("rank_score", "confidence", "dte") if col in top.columns]
    if sort_cols:
        top = top.sort_values(sort_cols, ascending=[False] * len(sort_cols))
    quality_counts: dict[str, int] = {}
    for col in ("quote_quality", "chain_source", "swing_fit_label"):
        if col not in df.columns:
            continue
        counts = df[col].fillna("").astype(str).str.strip()
        quality_counts[col] = int((counts != "").sum())
    quality_summary = _chain_shortlist_quality_summary(top)
    return {
        "status": "ready",
        "count": int(len(df)),
        "csv_path": str(csv_path) if csv_path else None,
        "json_path": str(json_path) if json_path else None,
        "source_file": _clean_value(df["_source_file"].iloc[0]) if "_source_file" in df.columns else None,
        "source_mtime": _clean_value(df["_source_mtime"].iloc[0]) if "_source_mtime" in df.columns else None,
        "field_coverage": quality_counts,
        "quality_summary": quality_summary,
        "rows": _records_from_frame(top[[
            col for col in [
                "ticker", "contract", "side", "strike", "expiry", "dte", "mid", "actual_dollars",
                "bid", "ask", "confidence", "rank_score", "spread_pct", "openInterest", "volume",
                "impliedVolatility", "delta", "breakeven_price", "breakeven_move_pct",
                "breakeven_direction", "budget_usage_pct", "contracts_for_budget",
                "stop_price", "target_price", "risk_dollars_reference", "reward_dollars_reference",
                "reward_risk_reference", "budget_fit", "contract_grade", "review_lane",
                "readiness_label", "readiness_score", "swing_fit_label", "swing_fit_score",
                "swing_fit_reasons", "swing_fit_warnings", "breakeven_move_label",
                "liquidity_label", "review_thesis", "grade_reasons", "risk_flags", "chain_source",
                "quote_quality", "data_delay",
            ]
            if col in top.columns
        ]], limit=8),
        "notes": [
            "Shortlist rows come from the saved 3m+ option-chain sweep.",
            "Free chain quotes can be delayed or incomplete; refresh before manual paper entry.",
        ],
    }


def _chain_shortlist_quality_summary(df: pd.DataFrame) -> dict[str, Any]:
    if df is None or df.empty:
        return {
            "status": "missing",
            "score": 0,
            "contract_count": 0,
            "warnings": ["No saved 3m+ chain contracts are available."],
            "confirmations": [],
        }
    out = df.copy()
    grade = out.get("contract_grade", pd.Series("", index=out.index)).fillna("").astype(str).str.upper()
    lane = out.get("review_lane", pd.Series("", index=out.index)).fillna("").astype(str).str.lower()
    swing = out.get("swing_fit_label", pd.Series("", index=out.index)).fillna("").astype(str).str.lower()
    budget = out.get("budget_fit", pd.Series("", index=out.index)).fillna("").astype(str).str.lower()
    readiness = pd.to_numeric(out.get("readiness_score", pd.Series(float("nan"), index=out.index)), errors="coerce")
    spread = pd.to_numeric(out.get("spread_pct", pd.Series(float("nan"), index=out.index)), errors="coerce")
    oi = pd.to_numeric(out.get("openInterest", pd.Series(0, index=out.index)), errors="coerce").fillna(0)
    volume = pd.to_numeric(out.get("volume", pd.Series(0, index=out.index)), errors="coerce").fillna(0)

    grade_counts = {label: int((grade == label).sum()) for label in ("A", "B", "C", "D") if int((grade == label).sum())}
    primary_count = int((lane == "primary_review").sum())
    clean_swing_count = int((swing == "clean_swing").sum())
    reviewable_swing_count = int(swing.isin({"clean_swing", "reviewable_swing"}).sum())
    inside_budget_count = int((budget == "inside_budget").sum())
    stretch_budget_count = int((budget == "stretch").sum())
    over_budget_count = int((budget == "over_budget").sum())
    tight_spread_count = int((spread <= 0.12).fillna(False).sum())
    liquid_count = int(((oi >= 100) & (spread <= 0.15)).fillna(False).sum())
    active_volume_count = int((volume > 0).sum())
    avg_readiness = _clean_value(readiness.dropna().mean() if not readiness.dropna().empty else None)
    median_spread = _clean_value(spread.dropna().median() if not spread.dropna().empty else None)

    score = 35
    score += min(15, int((grade == "A").sum()) * 10 + int((grade == "B").sum()) * 6)
    score += min(20, primary_count * 10)
    score += min(15, clean_swing_count * 8)
    score += min(10, inside_budget_count * 5)
    score += min(10, liquid_count * 5)
    if avg_readiness is not None:
        score += int(max(0, min(10, (_float_value(avg_readiness) - 70) / 3)))
    if median_spread is not None and _float_value(median_spread) <= 0.08:
        score += 5
    score -= min(15, over_budget_count * 5)
    score = int(_clamp(score, 0, 100))

    warnings: list[str] = []
    confirmations: list[str] = []
    if primary_count:
        confirmations.append(f"{primary_count} primary-review contract(s).")
    else:
        warnings.append("No primary-review contract in the saved shortlist.")
    if clean_swing_count:
        confirmations.append(f"{clean_swing_count} clean-swing contract(s).")
    else:
        warnings.append("No contract is currently labeled clean_swing.")
    if inside_budget_count:
        confirmations.append(f"{inside_budget_count} contract(s) fit inside budget.")
    elif stretch_budget_count:
        warnings.append("Only stretch-budget contracts are available.")
    elif over_budget_count:
        warnings.append("Saved shortlist appears over budget.")
    if liquid_count:
        confirmations.append(f"{liquid_count} contract(s) have acceptable OI/spread liquidity.")
    else:
        warnings.append("No contract clears the OI/spread liquidity check.")
    if active_volume_count == 0:
        warnings.append("No saved contract has same-day volume in the snapshot.")

    if primary_count and clean_swing_count and inside_budget_count and liquid_count:
        status = "clean"
    elif primary_count or reviewable_swing_count or inside_budget_count:
        status = "review"
    else:
        status = "weak"

    best = out.iloc[0].to_dict()
    return {
        "status": status,
        "score": score,
        "contract_count": int(len(out)),
        "grade_counts": grade_counts,
        "primary_review_count": primary_count,
        "clean_swing_count": clean_swing_count,
        "reviewable_swing_count": reviewable_swing_count,
        "inside_budget_count": inside_budget_count,
        "stretch_budget_count": stretch_budget_count,
        "over_budget_count": over_budget_count,
        "tight_spread_count": tight_spread_count,
        "liquid_count": liquid_count,
        "active_volume_count": active_volume_count,
        "avg_readiness_score": avg_readiness,
        "median_spread_pct": median_spread,
        "best_contract": _clean_value(best.get("contract") or best.get("contract_query")),
        "best_grade": _clean_value(best.get("contract_grade")),
        "best_review_lane": _clean_value(best.get("review_lane")),
        "best_readiness_score": _clean_value(best.get("readiness_score")),
        "best_swing_fit_label": _clean_value(best.get("swing_fit_label")),
        "best_spread_pct": _clean_value(best.get("spread_pct")),
        "best_open_interest": _clean_value(best.get("openInterest")),
        "best_budget_fit": _clean_value(best.get("budget_fit")),
        "warnings": warnings[:6],
        "confirmations": confirmations[:6],
    }


OFFERING_RISK_FORMS = {"S-1", "S-3", "F-1", "F-3", "424B2", "424B3", "424B4", "424B5"}


def _is_offering_risk_row(row: dict[str, Any]) -> bool:
    form = str(row.get("form") or "").upper().strip()
    signal = str(row.get("signal") or row.get("filing_signal") or "").lower()
    description = str(row.get("description") or "").lower()
    return (
        form in OFFERING_RISK_FORMS
        or "dilution" in signal
        or "offering" in signal
        or "shelf registration" in description
        or "prospectus" in description
    )


def _build_sec_dilution_risk_summary(data_dir: Path, limit: int = 12) -> dict[str, Any]:
    """Summarize official no-key SEC offering risk for the swing packet."""
    limit = max(1, min(int(limit or 12), 25))
    report = build_watchlist_sec_filings(data_dir, limit=max(40, limit * 3))
    rows = report.get("rows", []) if isinstance(report, dict) else []
    risk_rows: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict) or not _is_offering_risk_row(raw):
            continue
        days_old = _float_value(raw.get("days_old"), default=9999.0)
        if days_old <= 14:
            risk_action = "avoid_new_bullish_options_until_review"
        elif days_old <= 45:
            risk_action = "review_before_new_bullish_options"
        else:
            risk_action = "monitor_only"
        risk_rows.append({
            "priority": raw.get("priority"),
            "ticker": raw.get("ticker"),
            "company_name": raw.get("company_name"),
            "form": raw.get("form"),
            "filing_date": raw.get("filing_date"),
            "days_old": raw.get("days_old"),
            "signal": raw.get("signal"),
            "risk_action": risk_action,
            "description": raw.get("description"),
            "url": raw.get("url"),
        })
    risk_rows = sorted(
        risk_rows,
        key=lambda row: (
            _float_value(row.get("priority"), default=0.0),
            -_float_value(row.get("days_old"), default=9999.0),
        ),
        reverse=True,
    )[:limit]
    fresh_symbols = sorted({
        str(row.get("ticker") or "").upper()
        for row in risk_rows
        if row.get("ticker") and _float_value(row.get("days_old"), default=9999.0) <= 14
    })
    watch_symbols = sorted({
        str(row.get("ticker") or "").upper()
        for row in risk_rows
        if row.get("ticker") and _float_value(row.get("days_old"), default=9999.0) <= 45
    })
    if fresh_symbols:
        status = "block_new_bullish_options"
    elif risk_rows:
        status = "watch_offering_risk"
    else:
        status = "clear"
    notes = [
        "Uses the official no-key SEC submissions feed already powering the filing monitor.",
        "Recent shelf/prospectus/offering filings should be reviewed before opening bullish calls.",
    ]
    if not report.get("symbols_checked"):
        notes.append("Add symbols to the research watchlist to monitor SEC filing risk.")
    return {
        "generated_at": _now_iso(),
        "status": status,
        "count": len(risk_rows),
        "fresh_symbol_count": len(fresh_symbols),
        "symbols": watch_symbols,
        "fresh_symbols": fresh_symbols,
        "rows": [{k: _clean_value(v) for k, v in row.items()} for row in risk_rows],
        "source_filing_count": report.get("filing_count", 0),
        "symbols_checked": report.get("symbols_checked", 0),
        "error_count": report.get("error_count", 0),
        "notes": notes,
    }


def _packet_focus_query(
    command: dict[str, Any],
    today: dict[str, Any],
    gated: dict[str, Any],
    paper: dict[str, Any],
    chain: dict[str, Any],
) -> str | None:
    action = command.get("next_action") if isinstance(command.get("next_action"), dict) else {}
    candidates: list[Any] = [
        action.get("symbol"),
        action.get("query"),
    ]
    for block, fields in (
        (paper.get("rows") if isinstance(paper, dict) else [], ["ticker_or_symbol", "contract"]),
        (gated.get("rows") if isinstance(gated, dict) else [], ["ticker_or_symbol", "ticker", "symbol"]),
        (today.get("rows") if isinstance(today, dict) else [], ["symbol", "query"]),
        (chain.get("rows") if isinstance(chain, dict) else [], ["ticker", "symbol", "contract"]),
    ):
        for row in block or []:
            if not isinstance(row, dict):
                continue
            candidates.extend(row.get(field) for field in fields)
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text and text.lower() != "nan":
            return text
    return None


def _build_packet_data_trust_summary(data_dir: Path, query: str | None) -> dict[str, Any]:
    """Compact provider-readiness check for the swing packet focus symbol."""
    if not query:
        return {
            "status": "missing",
            "query": None,
            "symbol": None,
            "data_trust": {"label": "unknown", "score": 0, "history_source_summary": "none"},
            "rows": [],
            "warnings": ["No focus symbol was available for a data-trust probe."],
            "notes": ["The packet can show source trust once a setup, saved contract, or next action has a symbol."],
        }
    report = build_provider_status(data_dir, query=query, include_chain=False)
    trust = report.get("data_trust") if isinstance(report.get("data_trust"), dict) else {}
    return {
        "generated_at": report.get("generated_at"),
        "status": report.get("status"),
        "query": report.get("query"),
        "symbol": report.get("symbol"),
        "ok_count": report.get("ok_count", 0),
        "provider_count": report.get("provider_count", 0),
        "data_trust": {
            "label": trust.get("label"),
            "score": trust.get("score"),
            "history_ok_count": trust.get("history_ok_count"),
            "history_provider_count": trust.get("history_provider_count"),
            "history_source_summary": trust.get("history_source_summary"),
            "history_quality_counts": trust.get("history_quality_counts") or {},
            "option_chain_status": "handled_by_chain_shortlist",
            "symbol_cache_ok_count": trust.get("symbol_cache_ok_count"),
        },
        "rows": _packet_rows(report.get("rows"), [
            "provider", "category", "status", "latency_ms", "rows",
            "history_source", "history_quality", "last_close", "note",
        ], limit=6),
        "warnings": report.get("warnings") or [],
        "notes": [
            "This quick check probes no-key/free history and symbol-search sources for the packet focus.",
            "The packet chain shortlist is the option-contract source check; this card skips a duplicate chain pull.",
        ],
    }


def _symbol_from_packet_candidate(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if not text or text == "NAN":
        return None
    match = re.match(r"^\^?[A-Z][A-Z0-9.\-]{0,9}(?:=F)?", text)
    if not match:
        return None
    symbol = match.group(0).strip()
    return symbol if symbol else None


def _packet_event_symbols(
    focus_query: str | None,
    command: dict[str, Any],
    today: dict[str, Any],
    gated: dict[str, Any],
    paper: dict[str, Any],
    chain: dict[str, Any],
) -> list[str]:
    action = command.get("next_action") if isinstance(command.get("next_action"), dict) else {}
    raw_values: list[Any] = [focus_query, action.get("symbol"), action.get("query")]
    for block, fields in (
        (paper.get("rows") if isinstance(paper, dict) else [], ["ticker_or_symbol", "contract"]),
        (gated.get("rows") if isinstance(gated, dict) else [], ["ticker_or_symbol", "ticker", "symbol"]),
        (today.get("rows") if isinstance(today, dict) else [], ["symbol", "query"]),
        (chain.get("rows") if isinstance(chain, dict) else [], ["ticker", "symbol", "contract"]),
    ):
        for row in block or []:
            if not isinstance(row, dict):
                continue
            raw_values.extend(row.get(field) for field in fields)
    seen: set[str] = set()
    out: list[str] = []
    for raw in raw_values:
        symbol = _symbol_from_packet_candidate(raw)
        if not symbol or symbol in seen or symbol.endswith("=F"):
            continue
        seen.add(symbol)
        out.append(symbol)
    return out[:12]


def _days_until_date(value: Any, fallback: Any = None) -> float:
    parsed = pd.to_datetime(str(value or ""), errors="coerce", utc=True)
    if not pd.isna(parsed):
        return float((parsed.date() - datetime.now(timezone.utc).date()).days)
    return _float_value(fallback, default=math.nan)


def _event_risk_level(days_to_earnings: float, days_to_catalyst: float, whisper_gap: float) -> tuple[str, str]:
    has_earnings = math.isfinite(days_to_earnings)
    has_catalyst = math.isfinite(days_to_catalyst)
    if has_earnings and 0 <= days_to_earnings <= 5:
        return "high", "avoid_new_option_entry_until_after_earnings_review"
    if has_catalyst and 0 <= days_to_catalyst <= 7:
        return "high", "avoid_new_option_entry_until_catalyst_review"
    if has_earnings and 6 <= days_to_earnings <= 14:
        return "medium", "require_earnings_plan_before_entry"
    if has_catalyst and 8 <= days_to_catalyst <= 21:
        return "medium", "require_catalyst_plan_before_entry"
    if has_earnings and -7 <= days_to_earnings < 0:
        return "medium", "review_post_earnings_iv_reset"
    if has_earnings and 15 <= days_to_earnings <= 30:
        return "watch", "monitor_earnings_window"
    if has_catalyst and 22 <= days_to_catalyst <= 45:
        return "watch", "monitor_catalyst_window"
    if math.isfinite(whisper_gap) and abs(whisper_gap) >= 0.08:
        return "watch", "review_whisper_gap_context"
    return "clear", "no_near_term_event_flag"


def _event_sort_distance(row: pd.Series) -> float:
    distances = [
        abs(_days_until_date(row.get("next_earnings_date") or row.get("earnings_date"), row.get("days_to_earnings"))),
        abs(_days_until_date(row.get("next_catalyst_date"), row.get("days_to_catalyst"))),
    ]
    clean = [value for value in distances if math.isfinite(value)]
    return min(clean) if clean else 9999.0


def _event_record(row: pd.Series, asset: str) -> dict[str, Any] | None:
    symbol = str(row.get("ticker") or row.get("symbol") or "").upper().strip()
    if not symbol:
        return None
    earnings_date = row.get("next_earnings_date") or row.get("earnings_date")
    days_to_earnings = _days_until_date(earnings_date, row.get("days_to_earnings"))
    catalyst_date = row.get("next_catalyst_date")
    days_to_catalyst = _days_until_date(catalyst_date, row.get("days_to_catalyst"))
    whisper_gap = _float_value(row.get("whisper_gap_pct"), default=math.nan)
    risk_level, action = _event_risk_level(days_to_earnings, days_to_catalyst, whisper_gap)
    if risk_level == "clear" and not any(
        math.isfinite(value) for value in (days_to_earnings, days_to_catalyst, whisper_gap)
    ):
        return None
    return {
        "symbol": symbol,
        "asset": asset,
        "event_risk": risk_level,
        "action": action,
        "next_earnings_date": _clean_value(earnings_date),
        "days_to_earnings": _clean_value(days_to_earnings if math.isfinite(days_to_earnings) else None),
        "earnings_score": _clean_value(row.get("earnings_score")),
        "whisper_score": _clean_value(row.get("whisper_score")),
        "whisper_gap_pct": _clean_value(whisper_gap if math.isfinite(whisper_gap) else None),
        "next_catalyst_date": _clean_value(catalyst_date),
        "days_to_catalyst": _clean_value(days_to_catalyst if math.isfinite(days_to_catalyst) else None),
        "catalyst_type": _clean_value(row.get("catalyst_type")),
        "contract": _clean_value(row.get("contract")),
        "dte": _clean_value(row.get("dte")),
        "confidence": _clean_value(row.get("confidence")),
        "rank_score": _clean_value(row.get("rank_score")),
        "source_file": _clean_value(row.get("_source_file")),
        "snapshot_freshness": _clean_value(row.get("snapshot_freshness")),
        "snapshot_age_min": _clean_value(row.get("snapshot_age_min")),
    }


def _build_packet_event_risk_summary(data_dir: Path, symbols: list[str]) -> dict[str, Any]:
    """Summarize earnings/catalyst timing for the current swing packet candidates."""
    clean_symbols = [str(sym or "").upper().strip() for sym in symbols if str(sym or "").strip()]
    clean_symbols = list(dict.fromkeys(clean_symbols))[:12]
    if not clean_symbols:
        return {
            "status": "missing",
            "symbols": [],
            "count": 0,
            "rows": [],
            "warnings": ["No packet symbols were available for event-risk review."],
            "notes": ["Event risk uses existing local scan snapshots; run a scan to populate earnings context."],
        }
    records: list[dict[str, Any]] = []
    for asset, pattern in (("option", "top_options_*.parquet"), ("share", "top_shares_*.parquet")):
        df = _read_parquet(_latest_file(data_dir, pattern))
        if df.empty or "ticker" not in df.columns:
            continue
        out = df[df["ticker"].astype(str).str.upper().isin(clean_symbols)].copy()
        if out.empty:
            continue
        out["_event_sort"] = out.apply(_event_sort_distance, axis=1)
        out = out.sort_values(["ticker", "_event_sort"], kind="mergesort")
        for _, row in out.groupby(out["ticker"].astype(str).str.upper(), sort=False).head(1).iterrows():
            record = _event_record(row, asset)
            if record:
                records.append(record)
    severity = {"high": 3, "medium": 2, "watch": 1, "clear": 0}
    records = sorted(
        records,
        key=lambda row: (
            severity.get(str(row.get("event_risk") or "clear"), 0),
            -abs(_float_value(row.get("days_to_earnings"), default=9999.0)),
            _float_value(row.get("rank_score")),
        ),
        reverse=True,
    )
    high_count = sum(1 for row in records if row.get("event_risk") == "high")
    medium_count = sum(1 for row in records if row.get("event_risk") == "medium")
    watch_count = sum(1 for row in records if row.get("event_risk") == "watch")
    if high_count:
        status = "high_event_risk"
    elif medium_count:
        status = "watch_event_risk"
    elif watch_count:
        status = "monitor"
    elif records:
        status = "clear"
    else:
        status = "missing"
    warnings: list[str] = []
    if high_count:
        warnings.append(f"{high_count} packet symbol(s) have earnings/catalyst risk inside the high-risk window.")
    if any(str(row.get("snapshot_freshness") or "") == "stale" for row in records):
        warnings.append("Some event-risk rows came from stale scan snapshots; refresh before acting.")
    return {
        "generated_at": _now_iso(),
        "status": status,
        "symbols": clean_symbols,
        "count": len(records),
        "high_count": high_count,
        "medium_count": medium_count,
        "watch_count": watch_count,
        "rows": records[:12],
        "warnings": warnings,
        "notes": [
            "Event risk is built from existing local earnings, whisper, and catalyst fields.",
            "Near earnings, prefer waiting for the report or using a smaller/manual-reviewed options plan.",
        ],
    }


def _build_packet_decision_gate(
    command: dict[str, Any],
    climate: dict[str, Any],
    chain: dict[str, Any],
    data_trust: dict[str, Any],
    event_risk: dict[str, Any],
    sec_risk: dict[str, Any],
    paper: dict[str, Any],
) -> dict[str, Any]:
    """Collapse packet checks into one conservative review gate."""
    blockers: list[str] = []
    warnings: list[str] = []
    confirmations: list[str] = []
    next_steps: list[str] = []

    command_status = str(command.get("status") or "").lower()
    data_health = str(command.get("data_health_status") or "").lower()
    risk_level = str(command.get("risk_level") or "").lower()
    climate_score = _float_value(climate.get("climate_score") or command.get("climate_score"), default=50.0)
    trust = data_trust.get("data_trust") if isinstance(data_trust.get("data_trust"), dict) else {}
    trust_label = str(trust.get("label") or "unknown").lower()
    event_status = str(event_risk.get("status") or "unknown").lower()
    sec_status = str(sec_risk.get("status") or "unknown").lower()
    chain_status = str(chain.get("status") or "missing").lower()
    chain_count = int(_float_value(chain.get("count")))
    chain_quality = chain.get("quality_summary") if isinstance(chain.get("quality_summary"), dict) else {}
    chain_quality_status = str(chain_quality.get("status") or "unknown").lower()
    chain_quality_score = _float_value(chain_quality.get("score"), default=0.0)
    paper_count = int(_float_value(paper.get("selected_count")))

    if command_status == "fix_first":
        blockers.append(str(command.get("status_detail") or "Command center says fix first before adding new ideas."))
        next_steps.append("Resolve command-center fix-first item before considering a new entry.")
    elif command_status:
        confirmations.append(f"Command status is {command_status.replace('_', ' ')}.")

    if data_health == "bad":
        blockers.append("Data health is bad.")
        next_steps.append("Open Data health and repair stale or mismatched artifacts.")
    elif data_health == "warn":
        warnings.append("Data health has warnings.")
    elif data_health:
        confirmations.append("Data health is ok.")

    if risk_level == "high":
        warnings.append("Open-position risk is high; review exits before adding risk.")
        next_steps.append("Review open positions before adding a new swing idea.")
    elif risk_level in {"low", "normal", "medium"}:
        confirmations.append(f"Portfolio risk level is {risk_level}.")

    if climate_score < 40:
        blockers.append(f"Swing climate score is weak ({climate_score:g}).")
        next_steps.append("Wait for a cleaner market climate or use a much stricter setup.")
    elif climate_score < 55:
        warnings.append(f"Swing climate is mixed ({climate_score:g}).")
    else:
        confirmations.append(f"Swing climate supports selective review ({climate_score:g}).")

    if trust_label in {"blocked", "missing", "unknown"}:
        blockers.append("Focus data trust is not ready.")
        next_steps.append("Run Provider Status or refresh the focused scan before reviewing the setup.")
    elif trust_label == "partial":
        warnings.append("Focus data trust is partial.")
    elif trust_label == "ready":
        confirmations.append("Focus data trust is ready.")

    if event_status == "high_event_risk":
        blockers.append("High earnings or catalyst event risk is active.")
        next_steps.append("Wait through the report/catalyst or build a separate event-risk plan.")
    elif event_status in {"watch_event_risk", "monitor"}:
        warnings.append("Earnings or catalyst event risk needs review.")
    elif event_status == "clear":
        confirmations.append("No near-term earnings/catalyst blocker surfaced.")

    if sec_status == "block_new_bullish_options":
        blockers.append("Recent SEC offering/dilution risk is active.")
        next_steps.append("Review SEC filing risk before opening bullish calls.")
    elif sec_status == "watch_offering_risk":
        warnings.append("SEC offering/dilution risk should be reviewed.")
    elif sec_status == "clear":
        confirmations.append("No SEC offering/dilution blocker surfaced.")

    if chain_status == "ready" and chain_count > 0:
        if chain_quality_status == "clean":
            confirmations.append(
                f"Chain quality is clean ({chain_quality_score:g}/100) with "
                f"{chain_quality.get('primary_review_count') or 0} primary-review candidate(s)."
            )
        elif chain_quality_status == "review":
            warnings.append(
                f"Saved chain candidates need quality review ({chain_quality_score:g}/100)."
            )
            next_steps.append("Review spread, budget fit, and liquidity before choosing a contract.")
        elif chain_quality_status == "weak":
            blockers.append(
                "Saved chain shortlist exists but lacks a clean liquid/budget-fit contract."
            )
            next_steps.append("Refresh the 3m+ chain scan or choose a cleaner contract.")
        else:
            warnings.append("Saved chain quality is unknown.")
            next_steps.append("Refresh the 3m+ chain scan before choosing an options contract.")
        confirmations.append(f"{chain_count} saved 3m+ chain candidate(s) are available.")
    else:
        warnings.append("No saved 3m+ chain shortlist is ready.")
        next_steps.append("Run the 3m+ chain scan before picking an options contract.")

    if paper_count > 0:
        confirmations.append(f"{paper_count} external paper candidate(s) are available.")
    else:
        warnings.append("No filtered external paper candidate is currently selected.")

    score = max(0, min(100, int(round(100 - len(blockers) * 22 - len(warnings) * 7))))
    if blockers:
        status = "wait"
        label = "Wait"
        primary_action = next_steps[0] if next_steps else "Clear blockers before reviewing a new entry."
    elif score >= 85:
        status = "ready_to_review"
        label = "Ready to review"
        primary_action = "Review the highest-quality contract manually; no orders are placed."
    elif score >= 65:
        status = "selective_review"
        label = "Selective review"
        primary_action = next_steps[0] if next_steps else "Review warnings before choosing a contract."
    else:
        status = "caution"
        label = "Caution"
        primary_action = next_steps[0] if next_steps else "Refresh data and reduce risk before reviewing."

    return {
        "status": status,
        "label": label,
        "score": score,
        "primary_action": primary_action,
        "blocker_count": len(blockers),
        "warning_count": len(warnings),
        "confirmation_count": len(confirmations),
        "blockers": blockers[:8],
        "warnings": warnings[:8],
        "confirmations": confirmations[:8],
        "next_steps": list(dict.fromkeys(next_steps))[:6],
        "does_not_place_orders": True,
        "gate_version": "swing_packet_gate_v1",
        "notes": [
            "The decision gate is conservative decision support only.",
            "It combines packet readiness, not expected profitability.",
        ],
    }


def _swing_packet_headline(command: dict[str, Any], today: dict[str, Any]) -> str:
    action = command.get("next_action") if isinstance(command.get("next_action"), dict) else {}
    label = str(action.get("label") or "Review local research").strip()
    climate = str(command.get("climate_label") or today.get("climate_label") or "unknown climate").strip()
    status = str(command.get("status") or "review").strip().replace("_", " ")
    return f"{label} under {climate} ({status})."


def render_swing_packet_markdown(packet: dict[str, Any]) -> str:
    command = packet.get("command_center") if isinstance(packet.get("command_center"), dict) else {}
    action = command.get("next_action") if isinstance(command.get("next_action"), dict) else {}
    decision_gate = packet.get("decision_gate") if isinstance(packet.get("decision_gate"), dict) else {}
    climate = packet.get("swing_climate") if isinstance(packet.get("swing_climate"), dict) else {}
    chain = packet.get("chain_shortlist") if isinstance(packet.get("chain_shortlist"), dict) else {}
    chain_quality = chain.get("quality_summary") if isinstance(chain.get("quality_summary"), dict) else {}
    sec_risk = packet.get("sec_dilution_risk") if isinstance(packet.get("sec_dilution_risk"), dict) else {}
    event_risk = packet.get("event_risk") if isinstance(packet.get("event_risk"), dict) else {}
    data_trust = packet.get("data_trust_check") if isinstance(packet.get("data_trust_check"), dict) else {}
    trust = data_trust.get("data_trust") if isinstance(data_trust.get("data_trust"), dict) else {}
    lines = [
        "# Optedge Swing Packet",
        "",
        f"Generated: {packet.get('generated_at') or '-'}",
        "",
        "Research and decision-support only. No broker execution is performed.",
        "",
        "## State",
        f"- Headline: {packet.get('headline') or '-'}",
        f"- Decision gate: {decision_gate.get('label') or '-'} "
        f"({decision_gate.get('score') or 0}/100)",
        f"- Primary action: {decision_gate.get('primary_action') or '-'}",
        f"- Command status: {command.get('status') or '-'}",
        f"- Data health: {command.get('data_health_status') or '-'}",
        f"- Risk level: {command.get('risk_level') or '-'}",
        f"- Open positions: {command.get('total_open') or 0}",
        f"- Swing climate: {climate.get('climate_label') or command.get('climate_label') or '-'}",
        f"- Climate score: {climate.get('climate_score') or command.get('climate_score') or '-'}",
        "",
        "## Decision Gate",
        f"- Status: {decision_gate.get('status') or '-'}",
        f"- Blockers: {decision_gate.get('blocker_count') or 0}",
        f"- Warnings: {decision_gate.get('warning_count') or 0}",
    ]
    for row in decision_gate.get("blockers", [])[:6]:
        lines.append(f"- Blocker: {row}")
    for row in decision_gate.get("warnings", [])[:6]:
        lines.append(f"- Warning: {row}")
    for row in decision_gate.get("confirmations", [])[:6]:
        lines.append(f"- Confirmed: {row}")
    lines.extend([
        "",
        "## Next Review",
        f"- Action: {action.get('label') or '-'}",
        f"- Symbol/query: {action.get('query') or action.get('symbol') or '-'}",
        f"- Why: {action.get('detail') or command.get('status_detail') or '-'}",
        "",
        "## Today Review",
    ])
    for row in packet.get("today_review", {}).get("rows", [])[:8]:
        lines.append(
            f"- {row.get('priority', '-')}: {row.get('label', '-')} "
            f"({row.get('symbol') or row.get('query') or row.get('source') or '-'})"
        )
    lines.extend(["", "## Climate-Gated Setups"])
    for row in packet.get("climate_gated_setups", {}).get("rows", [])[:8]:
        lines.append(
            f"- {row.get('ticker_or_symbol', '-')}: {row.get('setup') or row.get('asset') or '-'} "
            f"[gate {row.get('climate_gate_score', '-')}]"
        )
    lines.extend(["", "## External Paper Candidates"])
    for row in packet.get("paper_candidates", {}).get("rows", [])[:8]:
        lines.append(
            f"- {row.get('asset', '-')}: {row.get('ticker_or_symbol', '-')} "
            f"{row.get('action', '')} qty {row.get('quantity', '-')}"
        )
    lines.extend([
        "",
        "## Focus Data Trust",
        f"- Symbol: {data_trust.get('symbol') or data_trust.get('query') or '-'}",
        f"- Status: {data_trust.get('status') or '-'}",
        f"- Trust: {trust.get('label') or '-'} ({trust.get('score') or 0}/100)",
        f"- History sources: {trust.get('history_source_summary') or '-'}",
    ])
    for row in data_trust.get("rows", [])[:6]:
        lines.append(
            f"- {row.get('provider', '-')}: {row.get('status', '-')} "
            f"{row.get('history_source') or row.get('note') or ''}"
        )
    lines.extend([
        "",
        "## Earnings / Catalyst Event Risk",
        f"- Status: {event_risk.get('status') or '-'}",
        f"- Symbols reviewed: {', '.join(event_risk.get('symbols') or []) or '-'}",
        f"- High / medium / watch: {event_risk.get('high_count') or 0} / "
        f"{event_risk.get('medium_count') or 0} / {event_risk.get('watch_count') or 0}",
    ])
    for row in event_risk.get("rows", [])[:8]:
        lines.append(
            f"- {row.get('symbol', '-')}: {row.get('event_risk', '-')} "
            f"earnings {row.get('next_earnings_date') or '-'} "
            f"({row.get('days_to_earnings', '-')}d), action {row.get('action', '-')}"
        )
    lines.extend([
        "",
        "## Chain Shortlist",
        f"- Status: {chain.get('status') or '-'}",
        f"- Rows: {chain.get('count') or 0}",
        f"- Quality: {chain_quality.get('status') or '-'} ({chain_quality.get('score') or 0}/100)",
        f"- Best: {chain_quality.get('best_contract') or '-'} "
        f"{chain_quality.get('best_grade') or '-'} / {chain_quality.get('best_review_lane') or '-'}",
        f"- Primary / clean / budget / liquid: {chain_quality.get('primary_review_count') or 0} / "
        f"{chain_quality.get('clean_swing_count') or 0} / "
        f"{chain_quality.get('inside_budget_count') or 0} / "
        f"{chain_quality.get('liquid_count') or 0}",
    ])
    for row in chain_quality.get("warnings", [])[:5]:
        lines.append(f"- Chain warning: {row}")
    for row in chain.get("rows", [])[:8]:
        lines.append(
            f"- {row.get('contract', '-')} mid {row.get('mid', '-')} "
            f"DTE {row.get('dte', '-')} fit {row.get('swing_fit_label', '-')}"
        )
    lines.extend([
        "",
        "## SEC Dilution / Offering Risk",
        f"- Status: {sec_risk.get('status') or '-'}",
        f"- Symbols checked: {sec_risk.get('symbols_checked') or 0}",
        f"- Risk rows: {sec_risk.get('count') or 0}",
    ])
    for row in sec_risk.get("rows", [])[:8]:
        lines.append(
            f"- {row.get('ticker', '-')}: {row.get('form', '-')} "
            f"{row.get('filing_date', '-')} -> {row.get('risk_action', '-')}"
        )
    lines.extend(["", "## Notes"])
    for note in packet.get("notes", []):
        lines.append(f"- {note}")
    return "\n".join(lines).strip() + "\n"


def write_swing_packet(packet: dict[str, Any], data_dir: Path = DATA_DIR) -> dict[str, str]:
    data_dir.mkdir(parents=True, exist_ok=True)
    json_path = data_dir / "swing_packet.json"
    md_path = data_dir / "swing_packet.md"
    payload = dict(packet)
    payload["wrote_files"] = True
    payload["paths"] = {"json": str(json_path), "markdown": str(md_path)}
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render_swing_packet_markdown(payload), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path)}


def _refresh_swing_packet_chain_shortlist(
    data_dir: Path,
    symbols_limit: int = 6,
    contracts_per_symbol: int = 4,
) -> dict[str, Any]:
    symbols_limit = max(1, min(int(symbols_limit or 6), 12))
    contracts_per_symbol = max(1, min(int(contracts_per_symbol or 4), 8))
    report = build_option_chain_batch(
        data_dir,
        query="",
        side="all",
        min_dte=MIN_SWING_OPTION_DTE,
        max_dte=180,
        max_spread_pct=0.20,
        max_premium=500.0,
        min_open_interest=25,
        preset="swing",
        symbols_limit=symbols_limit,
        contracts_per_symbol=contracts_per_symbol,
        limit=max(12, symbols_limit * contracts_per_symbol),
    )
    export = {"ok": False, "count": 0}
    if report.get("ok") and report.get("rows"):
        export = write_option_chain_shortlist(report, data_dir)
    error = report.get("error") or export.get("error")
    return {
        "attempted": True,
        "ok": bool(report.get("ok")) and (bool(export.get("ok")) or not report.get("rows")),
        "symbols_limit": symbols_limit,
        "contracts_per_symbol": contracts_per_symbol,
        "symbols_scanned": report.get("symbols_scanned", 0),
        "successful_scans": report.get("successful_scans", 0),
        "row_count": report.get("row_count", len(report.get("rows") or [])),
        "exported": bool(export.get("ok")),
        "export_count": export.get("count", 0),
        "error": _clean_value(error),
        "notes": [
            "Chain refresh uses the same free/provider-stack 3m+ swing preset.",
            "Quotes may be delayed or incomplete; refresh before manual paper entry.",
        ],
    }


def build_swing_packet(
    data_dir: Path = DATA_DIR,
    write: bool = False,
    refresh_chains: bool = False,
    chain_symbols_limit: int = 6,
    chain_contracts_per_symbol: int = 4,
) -> dict[str, Any]:
    """Build a compact daily swing-review handoff from existing cockpit panels."""
    notes = [
        "This packet packages existing cockpit outputs; it does not create a new trading model.",
        "It is local research only and does not place trades.",
    ]
    command = _packet_call(notes, "Command Center", {}, build_command_center, data_dir)
    today = _packet_call(notes, "Today Review", {}, build_today_review, data_dir, limit=10)
    climate = _packet_call(notes, "Swing Climate", {}, build_swing_climate, data_dir)
    gated = _packet_call(
        notes,
        "Climate-gated setups",
        {"rows": [], "held": []},
        build_climate_gated_setups,
        data_dir,
        per_asset=4,
        limit=12,
        include_held=True,
    )
    paper = _packet_call(
        notes,
        "Paper candidates",
        {"rows": [], "selected_count": 0, "excluded_count": 0},
        build_paper_candidates,
        data_dir,
        max_new=5,
        max_open=30,
        include_watch=False,
        allow_zero_size_placeholder=False,
        asset="all",
        dry_run=False,
        write=False,
    )
    sec_risk = _packet_call(
        notes,
        "SEC dilution risk",
        {"status": "unknown", "count": 0, "rows": [], "notes": []},
        _build_sec_dilution_risk_summary,
        data_dir,
    )
    if sec_risk.get("status") == "block_new_bullish_options":
        symbols = ", ".join(sec_risk.get("fresh_symbols") or sec_risk.get("symbols") or [])
        notes.append(
            "SEC offering risk active; review before opening new bullish options"
            + (f" on {symbols}." if symbols else ".")
        )
    chain_refresh = {"attempted": False}
    if refresh_chains:
        chain_refresh = _packet_call(
            notes,
            "3m+ chain refresh",
            {"attempted": True, "ok": False, "error": "chain refresh failed"},
            _refresh_swing_packet_chain_shortlist,
            data_dir,
            symbols_limit=chain_symbols_limit,
            contracts_per_symbol=chain_contracts_per_symbol,
        )
        if chain_refresh.get("attempted") and not chain_refresh.get("ok") and chain_refresh.get("error"):
            notes.append(f"3m+ chain refresh warning: {chain_refresh.get('error')}")
    chain = _packet_call(notes, "Chain shortlist", {"status": "missing", "rows": []}, _build_chain_shortlist_summary, data_dir)
    focus_query = _packet_focus_query(command, today, gated, paper, chain)
    data_trust = _packet_call(
        notes,
        "Focus data trust",
        {"status": "unknown", "rows": [], "data_trust": {"label": "unknown", "score": 0}},
        _build_packet_data_trust_summary,
        data_dir,
        focus_query,
    )
    event_risk = _packet_call(
        notes,
        "Event risk",
        {"status": "unknown", "count": 0, "rows": [], "warnings": []},
        _build_packet_event_risk_summary,
        data_dir,
        _packet_event_symbols(focus_query, command, today, gated, paper, chain),
    )
    for warning in (event_risk.get("warnings") or [])[:2]:
        notes.append(f"Event risk: {warning}")
    decision_gate = _build_packet_decision_gate(
        command,
        climate,
        chain,
        data_trust,
        event_risk,
        sec_risk,
        paper,
    )
    artifacts = {
        "dashboard": str(artifact_path("latest-dashboard", data_dir)) if artifact_path("latest-dashboard", data_dir) else None,
        "validation_report": str(artifact_path("validation-report", data_dir)) if artifact_path("validation-report", data_dir) else None,
        "chain_shortlist": str(artifact_path("option-chain-shortlist", data_dir)) if artifact_path("option-chain-shortlist", data_dir) else None,
        "paper_orders": str(artifact_path("external-paper-orders", data_dir)) if artifact_path("external-paper-orders", data_dir) else None,
    }
    packet = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "does_not_place_orders": True,
        "status": command.get("status") or "review",
        "headline": _swing_packet_headline(command, today),
        "decision_gate": decision_gate,
        "command_center": {
            "status": command.get("status"),
            "status_detail": command.get("status_detail"),
            "data_health_status": command.get("data_health_status"),
            "risk_level": command.get("risk_level"),
            "total_open": command.get("total_open"),
            "source_count": command.get("source_count"),
            "no_key_count": command.get("no_key_count"),
            "climate_label": command.get("climate_label"),
            "climate_score": command.get("climate_score"),
            "next_action": command.get("next_action") or {},
            "cards": command.get("cards") or [],
        },
        "today_review": {
            "count": today.get("count", 0),
            "review_now_count": today.get("review_now_count", 0),
            "rows": _packet_rows(today.get("rows"), [
                "priority", "category", "label", "detail", "action", "route", "symbol", "query", "source", "asset",
            ], limit=10),
        },
        "swing_climate": {
            "climate_label": climate.get("climate_label"),
            "climate_score": climate.get("climate_score"),
            "posture": climate.get("posture"),
            "warnings": climate.get("warnings") or [],
            "trade_gates": climate.get("trade_gates") or [],
            "asset_bias": climate.get("asset_bias") or [],
        },
        "climate_gated_setups": {
            "selected_count": gated.get("selected_count", len(gated.get("rows") or [])),
            "held_count": gated.get("held_count", len(gated.get("held") or [])),
            "rows": _packet_rows(gated.get("rows"), [
                "asset", "ticker_or_symbol", "setup", "readiness_score", "climate_gate_score",
                "climate_gate_status", "climate_gate_reasons", "trade_status", "confidence",
                "rank_score", "dte", "spread_pct",
            ], limit=10),
            "held": _packet_rows(gated.get("held"), [
                "asset", "ticker_or_symbol", "setup", "readiness_score", "climate_gate_score",
                "climate_gate_reasons",
            ], limit=5),
        },
        "paper_candidates": {
            "selected_count": paper.get("selected_count", 0),
            "excluded_count": paper.get("excluded_count", 0),
            "top_rejection_reasons": paper.get("top_rejection_reasons") or [],
            "rows": _packet_rows(paper.get("rows"), [
                "asset", "ticker_or_symbol", "action", "direction", "quantity", "contract",
                "option_side", "strike", "expiry", "entry_price", "stop_price", "target_price",
                "confidence", "rank_score", "trade_status", "reason_selected",
            ], limit=8),
        },
        "data_trust_check": data_trust,
        "event_risk": event_risk,
        "chain_refresh": chain_refresh,
        "chain_shortlist": chain,
        "sec_dilution_risk": sec_risk,
        "artifacts": artifacts,
        "notes": notes,
        "wrote_files": False,
        "paths": {},
    }
    packet = {k: _clean_value(v) for k, v in packet.items()}
    if write:
        paths = write_swing_packet(packet, data_dir)
        packet["wrote_files"] = True
        packet["paths"] = paths
    return packet


def _int_param(value: str | None, default: int, low: int, high: int) -> int:
    try:
        out = int(float(value or default))
    except Exception:
        return default
    return max(low, min(high, out))


def _float_param(value: str | None, default: float, low: float, high: float) -> float:
    try:
        out = float(value or default)
    except Exception:
        return default
    if not math.isfinite(out):
        return default
    return max(low, min(high, out))


def _bool_param(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "on"}


def _health_check(level: str, label: str, detail: str) -> dict[str, str]:
    return {"level": level, "label": label, "detail": detail}


def _health_status(checks: list[dict[str, str]]) -> str:
    order = {"ok": 0, "warn": 1, "bad": 2}
    worst = max((order.get(row.get("level", "ok"), 0) for row in checks), default=0)
    return {0: "ok", 1: "warn", 2: "bad"}[worst]


def _count_duplicate_open_positions(data_dir: Path) -> tuple[int, int]:
    raw_count = 0
    deduped_count = 0
    for filename in POSITION_FILES.values():
        rows = _read_json(data_dir / filename)
        if not isinstance(rows, list):
            continue
        dict_rows = [row for row in rows if isinstance(row, dict)]
        raw_count += len(dict_rows)
        deduped_count += len(_dedupe_position_rows(dict_rows))
    return raw_count, deduped_count


def _missing_required_columns(df: pd.DataFrame, required: list[str]) -> list[str]:
    return [col for col in required if col not in df.columns]


def _has_any_column(df: pd.DataFrame, columns: list[str]) -> bool:
    return any(col in df.columns for col in columns)


def _opportunity_identity_columns(asset: str, df: pd.DataFrame) -> list[str]:
    candidates = {
        "option": ["ticker", "side", "strike", "expiry"],
        "share": ["ticker"],
        "futures": ["symbol", "direction", "contract"],
        "value": ["ticker"],
    }.get(asset, [])
    cols = [col for col in candidates if col in df.columns]
    if asset == "futures" and "contract" not in cols:
        cols = [col for col in cols if col != "contract"]
    return cols


def _count_duplicate_opportunities(asset: str, df: pd.DataFrame) -> int:
    cols = _opportunity_identity_columns(asset, df)
    if not cols or df.empty:
        return 0
    normalized = df[cols].fillna("").astype(str).apply(lambda col: col.str.upper().str.strip())
    return int(normalized.duplicated(keep="first").sum())


def _opportunity_quality_audit(data_dir: Path) -> dict[str, Any]:
    required = {
        "option": ["ticker", "side", "strike", "expiry"],
        "share": ["ticker"],
        "futures": ["symbol", "direction"],
        "value": ["ticker"],
    }
    price_any = {
        "option": ["mid", "entry_price"],
        "share": ["spot", "entry_price", "current_price"],
        "futures": ["entry_price", "spot"],
        "value": ["value_score", "rank_score"],
    }
    rows: dict[str, dict[str, Any]] = {}
    checks: list[dict[str, str]] = []

    for asset, spec in OPPORTUNITY_SPECS.items():
        path = _latest_file(data_dir, spec["pattern"])
        meta = _file_meta(path)
        df = _read_parquet(path)
        row_count = int(len(df))
        missing = _missing_required_columns(df, required.get(asset, [])) if not df.empty else []
        missing_price = bool(not df.empty and not _has_any_column(df, price_any.get(asset, [])))
        duplicate_rows = _count_duplicate_opportunities(asset, df)
        actionable_count = 0
        if not df.empty:
            out = df.copy()
            out["asset"] = asset
            actionable_count = int(out.apply(_is_actionable, axis=1).sum())
        quote_quality_counts: dict[str, int] = {}
        if not df.empty and "quote_quality" in df.columns:
            quote_quality_counts = {
                str(key): int(value)
                for key, value in df["quote_quality"].fillna("unknown").astype(str).value_counts().to_dict().items()
            }
        rows[asset] = {
            "asset": asset,
            "file": meta.get("name") if meta else None,
            "rows": row_count,
            "actionable_rows": actionable_count,
            "duplicate_rows": duplicate_rows,
            "missing_required_columns": missing,
            "missing_price_or_score": missing_price,
            "quote_quality_counts": quote_quality_counts,
        }

        if path is None:
            continue
        if df.empty:
            checks.append(_health_check(
                "warn", f"{asset} opportunity snapshot empty",
                f"{spec['pattern']} exists but has no rows for search/explorer/paper review.",
            ))
            continue
        if missing:
            checks.append(_health_check(
                "bad", f"{asset} opportunity columns",
                f"Latest {asset} snapshot is missing required column(s): {', '.join(missing)}.",
            ))
        if missing_price:
            checks.append(_health_check(
                "warn", f"{asset} opportunity pricing",
                f"Latest {asset} snapshot has no usable price/score column for ranking or paper readiness.",
            ))
        if duplicate_rows:
            checks.append(_health_check(
                "warn", f"{asset} opportunity duplicates",
                f"Latest {asset} snapshot has {duplicate_rows} duplicate row(s) by trade identity.",
            ))
        elif not missing:
            checks.append(_health_check(
                "ok", f"{asset} opportunity duplicates",
                f"Latest {asset} snapshot has no duplicate trade identities across {row_count} row(s).",
            ))
        if asset in {"option", "share", "futures"} and row_count and actionable_count == 0:
            checks.append(_health_check(
                "warn", f"{asset} actionable opportunities",
                f"Latest {asset} snapshot has 0 actionable row(s) after Watch/Skip and sizing checks.",
            ))

    return {
        "rows": rows,
        "checks": checks,
    }


def build_data_health(data_dir: Path = DATA_DIR) -> dict[str, Any]:
    """Check whether the dashboard-facing artifacts agree with current state."""
    open_counts = _direct_open_counts(data_dir)
    total_open = sum(open_counts.values())
    validation = _read_json(data_dir / "validation_summary.json")
    aging = _read_json(data_dir / "position_aging_summary.json")
    checks: list[dict[str, str]] = []

    if isinstance(validation, dict):
        reported_open = int(_float_value(validation.get("open_positions")))
        if reported_open == total_open:
            checks.append(_health_check(
                "ok", "Validation open count",
                f"Validation and current open files both show {total_open} open position(s).",
            ))
        else:
            checks.append(_health_check(
                "bad", "Validation open count mismatch",
                f"Validation shows {reported_open}, but current open files show {total_open}.",
            ))
        asset_map = {"option": "options", "share": "shares", "futures": "futures"}
        assets = validation.get("assets") if isinstance(validation.get("assets"), dict) else {}
        for asset_name, count_key in asset_map.items():
            reported = assets.get(asset_name, {}) if isinstance(assets.get(asset_name), dict) else {}
            reported_count = int(_float_value(reported.get("open_positions")))
            direct_count = open_counts[count_key]
            if reported_count != direct_count:
                checks.append(_health_check(
                    "warn", f"{asset_name} open count mismatch",
                    f"Validation shows {reported_count}; current {count_key} state shows {direct_count}.",
                ))
    else:
        checks.append(_health_check(
            "warn", "Validation summary missing",
            "Run python run.py --validation-report to refresh validation_summary.json.",
        ))

    if isinstance(aging, dict):
        aging_open = int(_float_value(aging.get("open_count")))
        level = "ok" if aging_open == total_open else "warn"
        detail = (
            f"Position aging and current open files both show {total_open} open position(s)."
            if aging_open == total_open
            else f"Position aging shows {aging_open}, but current open files show {total_open}."
        )
        checks.append(_health_check(level, "Position aging count", detail))
    else:
        checks.append(_health_check(
            "warn", "Position aging missing",
            "position_aging_summary.json was not found or could not be read.",
        ))

    raw_open, deduped_open = _count_duplicate_open_positions(data_dir)
    duplicate_count = max(0, raw_open - deduped_open)
    if duplicate_count:
        checks.append(_health_check(
            "warn", "Duplicate open positions",
            f"{duplicate_count} duplicate open position row(s) were detected across lifecycle files.",
        ))
    else:
        checks.append(_health_check(
            "ok", "Duplicate open positions",
            f"No duplicate open rows detected across {raw_open} current position row(s).",
        ))

    equity_curve = artifact_path("equity-curve", data_dir)
    png_error = _png_validation_error(equity_curve)
    if png_error is None:
        checks.append(_health_check("ok", "Equity curve image", "equity_curve.png passed PNG integrity checks."))
    elif png_error == "missing":
        checks.append(_health_check("warn", "Equity curve image missing", "No equity curve image is available yet."))
    else:
        checks.append(_health_check("bad", "Equity curve image corrupt", f"equity_curve.png appears invalid: {png_error}."))

    latest_dashboard = artifact_path("latest-dashboard", data_dir)
    validation_path = artifact_path("validation-summary", data_dir)
    dashboard_meta = _file_meta(latest_dashboard)
    validation_meta = _file_meta(validation_path)
    if dashboard_meta:
        if _float_value(dashboard_meta.get("age_minutes")) > 24 * 60:
            checks.append(_health_check(
                "warn", "Dashboard is old",
                f"Latest dashboard is {dashboard_meta['age_minutes']} minutes old.",
            ))
        else:
            checks.append(_health_check("ok", "Dashboard freshness", f"Latest dashboard: {dashboard_meta['name']}."))
    else:
        checks.append(_health_check("warn", "Dashboard missing", "No dashboard_*.html file was found."))

    if dashboard_meta and validation_meta:
        dash_mtime = Path(dashboard_meta["path"]).stat().st_mtime
        val_mtime = Path(validation_meta["path"]).stat().st_mtime
        if dash_mtime - val_mtime > 3600:
            checks.append(_health_check(
                "warn", "Validation older than dashboard",
                "validation_summary.json is more than 60 minutes older than the latest dashboard.",
            ))

    snapshot_meta: dict[str, Any] = {}
    for asset_name, pattern in {
        "options": "top_options_*.parquet",
        "shares": "top_shares_*.parquet",
        "futures": "top_futures_*.parquet",
        "value": "top_value_*.parquet",
    }.items():
        meta = _file_meta(_latest_file(data_dir, pattern))
        snapshot_meta[asset_name] = meta
        if meta is None:
            checks.append(_health_check("warn", f"{asset_name} snapshot missing", f"No {pattern} file was found."))
        elif _float_value(meta.get("age_minutes")) > 24 * 60:
            checks.append(_health_check("warn", f"{asset_name} snapshot old", f"{meta['name']} is more than 24 hours old."))

    opportunity_quality = _opportunity_quality_audit(data_dir)
    checks.extend(opportunity_quality["checks"])

    sec_cache = sec_company_cache_meta(data_dir / "sec_company_tickers.json")
    nasdaq_cache = nasdaq_symbol_cache_meta(data_dir / "nasdaq_symbol_directory.json")
    if sec_cache.get("status") == "fresh":
        checks.append(_health_check(
            "ok", "SEC ticker cache",
            f"Free SEC company-name search cache has {sec_cache.get('row_count', 0)} ticker row(s).",
        ))
    elif sec_cache.get("status") == "stale":
        checks.append(_health_check(
            "warn", "SEC ticker cache stale",
            f"Free SEC ticker cache is {sec_cache.get('age_days')} days old; run any company lookup to refresh it.",
        ))
    elif sec_cache.get("status") == "corrupt":
        checks.append(_health_check(
            "warn", "SEC ticker cache corrupt",
            "Free SEC ticker cache could not be read; run a company lookup to rebuild it.",
        ))
    else:
        checks.append(_health_check(
            "warn", "SEC ticker cache missing",
            "Company-name autocomplete uses the free SEC ticker cache after the first company lookup warms it.",
        ))

    if nasdaq_cache.get("status") == "fresh":
        checks.append(_health_check(
            "ok", "Nasdaq symbol directory",
            f"Official Nasdaq Trader symbol directory has {nasdaq_cache.get('row_count', 0)} row(s).",
        ))
    elif nasdaq_cache.get("status") == "stale":
        checks.append(_health_check(
            "warn", "Nasdaq symbol directory stale",
            f"Nasdaq symbol directory cache is {nasdaq_cache.get('age_days')} days old; run any company lookup to refresh it.",
        ))
    elif nasdaq_cache.get("status") == "corrupt":
        checks.append(_health_check(
            "warn", "Nasdaq symbol directory corrupt",
            "Nasdaq symbol directory cache could not be read; run a company lookup to rebuild it.",
        ))
    else:
        checks.append(_health_check(
            "warn", "Nasdaq symbol directory missing",
            "Autocomplete can use Nasdaq Trader's free symbol directory after the first company lookup warms it.",
        ))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": _health_status(checks),
        "open_counts": open_counts,
        "total_open": total_open,
        "duplicate_open_rows": duplicate_count,
        "checks": checks,
        "artifacts": {
            "dashboard": dashboard_meta,
            "validation_summary": validation_meta,
            "validation_report": _file_meta(artifact_path("validation-report", data_dir)),
            "equity_curve": _file_meta(equity_curve),
        },
        "snapshots": snapshot_meta,
        "opportunity_quality": opportunity_quality["rows"],
        "free_data_caches": {
            "sec_company_tickers": sec_cache,
            "nasdaq_symbol_directory": nasdaq_cache,
        },
        "notes": [
            "Data health reads the same local files used by the cockpit.",
            "Open-position counts come directly from current open position JSON files.",
            "Free data caches improve search coverage without paid APIs.",
            "Warnings mean review the artifacts before trusting the displayed analytics.",
        ],
    }


def build_summary(data_dir: Path = DATA_DIR) -> dict[str, Any]:
    validation = _read_json(data_dir / "validation_summary.json")
    aging = _read_json(data_dir / "position_aging_summary.json")
    open_counts = _direct_open_counts(data_dir)
    data_health = build_data_health(data_dir)
    latest = {
        "dashboard": artifact_path("latest-dashboard", data_dir),
        "validation_report": artifact_path("validation-report", data_dir),
        "external_paper_orders": artifact_path("external-paper-orders", data_dir),
        "robinhood_agentic_queue": artifact_path("robinhood-agentic-queue", data_dir),
        "robinhood_agentic_prompt": artifact_path("robinhood-agentic-prompt", data_dir),
        "equity_curve": artifact_path("equity-curve", data_dir),
    }
    snapshots = {
        "options": _latest_file(data_dir, "top_options_*.parquet"),
        "shares": _latest_file(data_dir, "top_shares_*.parquet"),
        "value": _latest_file(data_dir, "top_value_*.parquet"),
        "futures": _latest_file(data_dir, "top_futures_*.parquet"),
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_dir": str(data_dir),
        "open_counts": open_counts,
        "total_open": sum(open_counts.values()),
        "validation": validation if isinstance(validation, dict) else {},
        "position_aging": aging if isinstance(aging, dict) else {},
        "data_health": data_health,
        "latest_artifacts": {k: (str(v) if v else None) for k, v in latest.items()},
        "snapshots": {k: (v.name if v else None) for k, v in snapshots.items()},
        "notes": [
            "This cockpit reads local Optedge artifacts only.",
            "Search uses the latest scan snapshots and open position files.",
            "No trades are placed from this UI.",
        ],
    }


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, indent=2, default=str).encode("utf-8")


def render_cockpit_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Optedge Local Cockpit</title>
<style>
:root {
  color-scheme: dark;
  --bg:#090a0a;
  --panel:#121414;
  --panel2:#191c1d;
  --panel3:#0f1111;
  --border:#2c3330;
  --border-soft:#202522;
  --text:#f4f4f2;
  --muted:#a5aaa5;
  --soft:#d6d9d3;
  --accent:#20c997;
  --accent-strong:#4ade80;
  --good:#22c55e;
  --warn:#f59e0b;
  --bad:#ef4444;
  --shadow:0 16px 38px rgba(0,0,0,.24);
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font-family:Inter,Segoe UI,Arial,sans-serif; }
.wrap { max-width:1280px; margin:0 auto; padding:24px 16px 72px; }
header { display:flex; justify-content:space-between; gap:16px; align-items:flex-end; border-bottom:1px solid var(--border); padding-bottom:16px; }
h1 { margin:0; font-size:28px; font-weight:650; }
.muted { color:var(--muted); }
.grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:18px 0; }
.tile, .panel { border:1px solid var(--border); background:var(--panel); border-radius:8px; padding:14px; box-shadow:var(--shadow); }
.tile { min-height:96px; border-left:3px solid var(--border); }
.tile:nth-child(1) { border-left-color:var(--accent); }
.tile:nth-child(2) { border-left-color:var(--good); }
.tile:nth-child(3) { border-left-color:#a3e635; }
.tile:nth-child(5) { border-left-color:var(--warn); }
.tile span { display:block; color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.5px; }
.tile strong { display:block; font-size:26px; margin-top:6px; }
.actions { display:flex; flex-wrap:wrap; gap:8px; margin:16px 0; }
a, button { color:var(--text); }
.btn { display:inline-flex; align-items:center; gap:8px; border:1px solid var(--border); background:var(--panel2); border-radius:8px; padding:8px 12px; text-decoration:none; font-size:13px; cursor:pointer; transition:border-color .16s ease, background .16s ease, transform .16s ease; }
.btn:hover { border-color:var(--accent); background:#1d2321; }
.btn:active { transform:translateY(1px); }
.view-nav { position:sticky; top:0; z-index:20; display:flex; gap:8px; overflow:auto; padding:10px 0 12px; margin:0 0 8px; background:rgba(9,10,10,.94); backdrop-filter:blur(12px); border-bottom:1px solid rgba(44,51,48,.72); }
.view-tab { white-space:nowrap; border:1px solid var(--border); background:var(--panel3); color:var(--muted); border-radius:8px; padding:9px 13px; font-size:13px; cursor:pointer; transition:border-color .16s ease, background .16s ease, color .16s ease; }
.view-tab.active { color:var(--text); border-color:var(--accent); background:rgba(32,201,151,.13); }
body:not(.view-all) .panel[data-view] { display:none; }
body.view-overview .panel[data-view="overview"],
body.view-positions .panel[data-view="positions"],
body.view-explore .panel[data-view="explore"],
body.view-chains .panel[data-view="chains"],
body.view-providers .panel[data-view="providers"],
body.view-paper .panel[data-view="paper"],
body.view-research .panel[data-view="research"] { display:block; }
.search { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:10px; margin-top:10px; }
.scan-controls { display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; align-items:center; }
input, select { background:var(--panel2); color:var(--text); border:1px solid var(--border); border-radius:8px; padding:12px 14px; font-size:15px; }
input { width:100%; }
input:focus, select:focus { outline:none; border-color:var(--accent); box-shadow:0 0 0 3px rgba(32,201,151,.14); }
.check { display:inline-flex; align-items:center; gap:6px; color:var(--muted); font-size:13px; }
.check input { width:auto; }
.search-actions { display:flex; gap:8px; flex-wrap:wrap; }
.status { margin-top:8px; font-size:12px; color:var(--muted); min-height:18px; }
.sections { display:grid; grid-template-columns:1fr; gap:12px; margin-top:14px; }
.section { border:1px solid var(--border); border-radius:8px; background:var(--panel3); overflow:hidden; }
.section h3 { margin:0; padding:12px 14px; font-size:14px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; }
.brief-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:8px; }
.brief-tile { border:1px solid var(--border); background:var(--panel3); border-radius:8px; padding:10px; }
.brief-tile span { display:block; color:var(--muted); font-size:10px; text-transform:uppercase; letter-spacing:.4px; }
.brief-tile strong { display:block; margin-top:5px; font-size:14px; }
.setup-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:10px; margin-top:12px; }
.setup-card { border:1px solid var(--border); background:var(--panel3); border-radius:8px; padding:12px; display:flex; flex-direction:column; gap:10px; min-height:176px; }
.setup-card header { border:0; padding:0; display:flex; align-items:flex-start; justify-content:space-between; gap:10px; }
.setup-card h3 { border:0; padding:0; margin:0; font-size:16px; line-height:1.25; display:block; }
.setup-card small { color:var(--muted); display:block; margin-top:3px; }
.setup-card .row { display:flex; justify-content:space-between; gap:10px; color:var(--muted); font-size:12px; }
.setup-card .row b { color:var(--text); font-weight:600; text-align:right; }
.factor-stack { display:grid; gap:6px; }
.factor-row { border:1px solid var(--border-soft); background:rgba(255,255,255,.025); border-radius:8px; padding:7px 8px; display:grid; grid-template-columns:minmax(74px,.42fr) minmax(0,1fr) auto; gap:8px; align-items:start; }
.factor-row strong { color:var(--text); font-size:12px; line-height:1.2; }
.factor-row span { color:var(--muted); font-size:11px; line-height:1.35; }
.factor-row em { justify-self:end; font-style:normal; color:#bbf7d0; font-size:11px; }
.pill { display:inline-flex; align-items:center; white-space:nowrap; border:1px solid var(--border); border-radius:999px; padding:4px 8px; color:var(--muted); font-size:11px; background:var(--panel2); }
.pill.ready { border-color:rgba(16,185,129,.7); color:#bbf7d0; background:rgba(16,185,129,.12); }
.pill.review { border-color:rgba(245,158,11,.7); color:#fde68a; background:rgba(245,158,11,.12); }
.pill.wait { border-color:rgba(239,68,68,.7); color:#fecaca; background:rgba(239,68,68,.12); }
.pill.pass { border-color:rgba(16,185,129,.75); color:#bbf7d0; background:rgba(16,185,129,.14); }
.pill.hold { border-color:rgba(245,158,11,.75); color:#fde68a; background:rgba(245,158,11,.14); }
.setup-card .btn { justify-content:center; margin-top:auto; width:100%; }
.decision-strip { border:1px solid rgba(32,201,151,.38); background:#101312; border-radius:8px; padding:12px; margin-bottom:12px; display:grid; grid-template-columns:minmax(0,1.15fr) minmax(260px,.85fr); gap:12px; }
.decision-main { display:grid; gap:10px; }
.decision-main h3 { margin:0; font-size:18px; }
.decision-main p { margin:0; color:var(--soft); line-height:1.45; font-size:13px; }
.decision-metrics { display:grid; grid-template-columns:repeat(auto-fit,minmax(110px,1fr)); gap:8px; }
.decision-metric { border:1px solid var(--border); background:var(--panel3); border-radius:8px; padding:9px; }
.decision-metric span { display:block; color:var(--muted); font-size:10px; text-transform:uppercase; letter-spacing:.4px; }
.decision-metric strong { display:block; margin-top:5px; font-size:14px; }
.decision-side { display:grid; gap:10px; align-content:start; }
.decision-side ul { margin:0; padding-left:18px; color:var(--soft); font-size:12px; line-height:1.45; }
.decision-alt { display:flex; flex-wrap:wrap; gap:6px; }
.decision-alt .pill { max-width:100%; overflow:hidden; text-overflow:ellipsis; }
.review-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:10px; margin-top:12px; }
.review-card { border:1px solid var(--border); background:var(--panel3); border-radius:8px; padding:12px; display:grid; gap:10px; min-height:168px; }
.review-card.setup { border-left:4px solid var(--accent); }
.review-card.saved_contract { border-left:4px solid var(--good); }
.review-card.position_risk { border-left:4px solid var(--warn); }
.review-card.data_health { border-left:4px solid var(--bad); }
.review-card header { border:0; padding:0; display:flex; align-items:flex-start; justify-content:space-between; gap:10px; }
.review-card h3 { margin:0; font-size:15px; line-height:1.25; }
.review-card p { margin:0; color:var(--soft); font-size:12px; line-height:1.45; }
.review-meta { display:flex; flex-wrap:wrap; gap:6px; align-items:center; color:var(--muted); font-size:11px; }
.command-shell { border:1px solid var(--border); background:#101211; border-radius:8px; padding:14px; margin-top:14px; display:grid; gap:12px; }
.command-top { display:grid; grid-template-columns:minmax(0,1.3fr) minmax(280px,.7fr); gap:12px; align-items:stretch; }
.command-hero { border:1px solid var(--border); background:#151816; border-radius:8px; padding:16px; display:flex; flex-direction:column; gap:12px; min-height:190px; }
.command-eyebrow { color:var(--muted); text-transform:uppercase; letter-spacing:.7px; font-size:11px; }
.command-title { font-size:26px; line-height:1.08; font-weight:700; margin:0; }
.command-detail { color:var(--soft); line-height:1.45; margin:0; }
.command-action { border:1px solid var(--border); background:var(--panel3); border-radius:8px; padding:14px; display:grid; gap:10px; }
.command-action h3 { margin:0; font-size:16px; }
.command-action p { margin:0; color:var(--soft); font-size:12px; line-height:1.45; }
.command-action .pill { justify-self:start; }
.command-card-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:8px; }
.command-card { border:1px solid var(--border); background:var(--panel3); border-radius:8px; padding:12px; min-height:104px; }
.command-card span { display:block; color:var(--muted); font-size:10px; text-transform:uppercase; letter-spacing:.4px; }
.command-card strong { display:block; margin-top:6px; font-size:18px; }
.command-card p { margin:8px 0 0; color:var(--soft); font-size:12px; line-height:1.35; }
.command-card.good { border-color:rgba(16,185,129,.45); }
.command-card.warn { border-color:rgba(245,158,11,.55); }
.command-card.bad { border-color:rgba(239,68,68,.55); }
.trust-ribbon { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:8px; }
.trust-card { border:1px solid var(--border); background:var(--panel3); border-radius:8px; padding:10px; min-height:94px; }
.trust-card span { display:block; color:var(--muted); font-size:10px; text-transform:uppercase; letter-spacing:.4px; }
.trust-card strong { display:block; margin-top:5px; font-size:15px; }
.trust-card p { margin:7px 0 0; color:var(--soft); font-size:11px; line-height:1.35; }
.trust-card.good { border-color:rgba(16,185,129,.55); }
.trust-card.warn { border-color:rgba(245,158,11,.65); }
.trust-card.bad { border-color:rgba(239,68,68,.65); }
.priority-badge { display:inline-flex; align-items:center; justify-content:center; min-width:34px; border:1px solid var(--border); border-radius:999px; padding:4px 8px; font-size:12px; font-weight:700; color:var(--text); background:var(--panel2); }
.priority-badge.hot { border-color:rgba(239,68,68,.75); color:#fecaca; background:rgba(239,68,68,.12); }
.priority-badge.warm { border-color:rgba(245,158,11,.75); color:#fde68a; background:rgba(245,158,11,.12); }
.priority-badge.cool { border-color:rgba(32,201,151,.65); color:#a7f3d0; background:rgba(32,201,151,.10); }
.review-card .btn { justify-content:center; width:100%; align-self:end; }
.chain-preset.active { border-color:var(--accent); background:rgba(32,201,151,.13); color:var(--text); }
.brief-cols { display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:10px; margin-top:10px; }
.brief-list { border:1px solid var(--border); background:var(--panel3); border-radius:8px; padding:10px; }
.brief-list h4 { margin:0 0 8px; font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.4px; }
.brief-list ul { margin:0; padding-left:18px; color:var(--soft); font-size:12px; }
.table-wrap { overflow:auto; }
table { width:100%; border-collapse:collapse; font-size:12px; }
th, td { padding:8px 10px; border-bottom:1px solid var(--border-soft); text-align:left; vertical-align:top; }
th { color:var(--muted); text-transform:uppercase; font-size:10px; letter-spacing:.4px; }
tr.clickable-row { cursor:pointer; }
tr.clickable-row:hover { background:#18201d; }
.empty { padding:14px; color:var(--muted); font-style:italic; }
.risk { border-left:4px solid var(--warn); }
.job-list { display:grid; gap:8px; margin-top:10px; }
.job { display:flex; justify-content:space-between; gap:10px; align-items:center; border:1px solid var(--border); background:var(--panel3); border-radius:8px; padding:10px 12px; font-size:13px; }
.job code { color:var(--accent); }
.job small { color:var(--muted); display:block; margin-top:3px; }
.logbox { display:none; white-space:pre-wrap; overflow:auto; max-height:280px; border:1px solid var(--border); background:#070807; border-radius:8px; padding:12px; margin-top:10px; font:12px/1.45 Consolas,monospace; color:var(--soft); }
.logbox.active { display:block; }
.global-command { border:1px solid rgba(32,201,151,.28); background:#111412; border-radius:8px; padding:12px; margin:14px 0 4px; display:grid; grid-template-columns:minmax(0,1fr) auto; gap:10px; align-items:start; box-shadow:var(--shadow); }
.global-command-main { display:grid; grid-template-columns:170px minmax(0,1fr); gap:10px; align-items:start; }
.global-command-label span { display:block; color:var(--muted); font-size:10px; text-transform:uppercase; letter-spacing:.5px; }
.global-command-label strong { display:block; margin-top:5px; font-size:16px; }
.global-command-actions { display:flex; flex-wrap:wrap; gap:8px; justify-content:flex-end; }
.global-command .status { grid-column:1 / -1; margin-top:0; }
.global-command .status:empty { display:none; }
.suggestions { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; min-height:30px; }
.suggestions:empty { display:none; }
.suggestion { border:1px solid var(--border); background:var(--panel3); border-radius:8px; padding:7px 10px; cursor:pointer; font-size:12px; color:var(--text); }
.suggestion:hover { border-color:var(--accent); }
.suggestion span { color:var(--muted); margin-left:6px; }
.good { color:var(--good); } .warn { color:var(--warn); } .bad { color:var(--bad); }
@media (max-width:900px) { header { align-items:flex-start; flex-direction:column; } .grid { grid-template-columns:repeat(2,minmax(0,1fr)); } .search { grid-template-columns:1fr; } .command-top { grid-template-columns:1fr; } .global-command { grid-template-columns:1fr; } .global-command-main { grid-template-columns:1fr; } .global-command-actions { justify-content:flex-start; } .decision-strip { grid-template-columns:1fr; } }
</style>
</head>
<body class="view-overview">
<div class="wrap">
  <header>
    <div>
      <h1>Optedge Local Cockpit</h1>
      <div class="muted">Interactive local research view. No broker execution.</div>
    </div>
    <div class="muted" id="asof">Loading...</div>
  </header>
  <div class="grid">
    <div class="tile"><span>Open options</span><strong id="open-options">-</strong></div>
    <div class="tile"><span>Open shares</span><strong id="open-shares">-</strong></div>
    <div class="tile"><span>Open futures</span><strong id="open-futures">-</strong></div>
    <div class="tile risk"><span>Total open</span><strong id="total-open">-</strong></div>
    <div class="tile"><span>Data health</span><strong id="data-health">-</strong></div>
  </div>
  <div class="actions">
    <a class="btn" href="/artifact/latest-dashboard" target="_blank">Latest dashboard</a>
    <a class="btn" href="/artifact/validation-report" target="_blank">Validation report</a>
    <a class="btn" href="/artifact/validation-summary" target="_blank">Validation JSON</a>
    <a class="btn" href="/artifact/equity-curve" target="_blank">Equity curve</a>
    <a class="btn" href="/artifact/external-paper-orders" target="_blank">Paper orders</a>
    <a class="btn" href="/artifact/option-chain-shortlist" target="_blank">Chain shortlist</a>
    <a class="btn" href="/artifact/swing-packet-json" target="_blank">Swing packet JSON</a>
    <a class="btn" href="/artifact/swing-packet-md" target="_blank">Swing packet MD</a>
    <a class="btn" href="/artifact/robinhood-agentic-queue" target="_blank">Agentic queue</a>
    <a class="btn" href="/artifact/robinhood-agentic-prompt" target="_blank">Agent prompt</a>
    <button class="btn" type="button" id="refresh">Refresh status</button>
  </div>
  <section class="global-command" aria-label="Quick research command">
    <div class="global-command-main">
      <div class="global-command-label"><span>Command</span><strong>Search, scan, save</strong></div>
      <div>
        <input id="global-query" placeholder="Ticker, company, or option idea, e.g. AAPL, Nvidia, SPY 20261218 C 600" autocomplete="off">
        <div class="suggestions" id="global-suggestions"></div>
      </div>
    </div>
    <div class="global-command-actions">
      <button class="btn" type="button" id="global-lookup">Lookup</button>
      <button class="btn" type="button" id="global-workspace">Review workspace</button>
      <button class="btn" type="button" id="global-run">Run scan</button>
      <button class="btn" type="button" id="global-chain">3m+ chain</button>
      <button class="btn" type="button" id="global-save">Save</button>
    </div>
    <div class="status" id="global-status"></div>
  </section>
  <nav class="view-nav" aria-label="Cockpit sections">
    <button class="view-tab active" type="button" data-view="overview">Overview</button>
    <button class="view-tab" type="button" data-view="positions">Positions</button>
    <button class="view-tab" type="button" data-view="explore">Explore</button>
    <button class="view-tab" type="button" data-view="chains">Chains</button>
    <button class="view-tab" type="button" data-view="providers">Providers</button>
    <button class="view-tab" type="button" data-view="paper">Paper queue</button>
    <button class="view-tab" type="button" data-view="research">Research</button>
    <button class="view-tab" type="button" data-view="all">All</button>
  </nav>
  <section class="panel" data-view="overview">
    <h2 style="margin:0 0 8px;font-size:18px">Command center</h2>
    <div class="muted">Fast first read: market posture, data trust, open risk, free-source coverage, and the next review action.</div>
    <div class="status" id="command-center-status-text"></div>
    <div id="command-center-results"></div>
  </section>
  <section class="panel" data-view="overview">
    <h2 style="margin:0 0 8px;font-size:18px">Swing packet</h2>
    <div class="muted">A compact daily handoff built from command center, today review, climate gates, paper candidates, and saved chain shortlist.</div>
    <div class="scan-controls">
      <button class="btn" type="button" id="swing-packet-preview">Preview packet</button>
      <button class="btn" type="button" id="swing-packet-write">Write packet files</button>
      <button class="btn" type="button" id="swing-packet-chain">Write + 3m+ chain scan</button>
      <a class="btn" href="/artifact/swing-packet-json" target="_blank">Open JSON</a>
      <a class="btn" href="/artifact/swing-packet-md" target="_blank">Open Markdown</a>
    </div>
    <div class="status" id="swing-packet-status-text"></div>
    <div class="section" style="margin-top:12px"><div id="swing-packet-results"></div></div>
  </section>
  <section class="panel" data-view="overview">
    <h2 style="margin:0 0 8px;font-size:18px">Today review</h2>
    <div class="muted">One clean review queue from climate-cleared setups, saved option contracts, open-position risk, and local action items.</div>
    <div class="status" id="today-review-status-text"></div>
    <div class="section" style="margin-top:12px"><div id="today-review-results"></div></div>
  </section>
  <section class="panel" data-view="overview">
    <h2 style="margin:0 0 8px;font-size:18px">Swing climate</h2>
    <div class="muted">One-page posture from free market, breadth, and sector context. Use this to decide how strict to be with setup readiness.</div>
    <div class="status" id="swing-climate-status-text"></div>
    <div class="section" style="margin-top:12px"><div id="swing-climate-results"></div></div>
  </section>
  <section class="panel" data-view="overview">
    <h2 style="margin:0 0 8px;font-size:18px">Best setups</h2>
    <div class="muted">Decision-first shortlist from the latest option, share, futures, and value snapshots. Click a setup to open the research brief.</div>
    <div class="status" id="best-setups-status-text"></div>
    <div class="section" style="margin-top:12px"><div id="best-setups-results"></div></div>
  </section>
  <section class="panel" data-view="overview">
    <h2 style="margin:0 0 8px;font-size:18px">Small-cap + futures swing scout</h2>
    <div class="muted">High-upside review list from free local factors: small/mid-cap asymmetry, short pressure, retail attention, momentum, value, execution quality, and futures/macro swings.</div>
    <div class="scan-controls">
      <select id="swing-scout-asset" aria-label="Swing scout asset">
        <option value="all">All assets</option>
        <option value="option">Options</option>
        <option value="share">Shares</option>
        <option value="value">Value</option>
        <option value="futures">Futures</option>
      </select>
      <select id="swing-scout-lane" aria-label="Swing scout lane">
        <option value="all">All lanes</option>
        <option value="small_cap_squeeze_watch">Small-cap squeeze</option>
        <option value="retail_attention_swing">Retail attention</option>
        <option value="small_cap_value_dislocation">Value dislocation</option>
        <option value="small_cap_share_swing">Share swing</option>
        <option value="small_cap_options_momentum">Options momentum</option>
        <option value="long_dated_option_swing">Long-dated options</option>
        <option value="futures_macro_swing">Futures macro</option>
        <option value="nasdaq_small_cap_mover">Nasdaq small-cap movers</option>
      </select>
      <input id="swing-scout-query" placeholder="Filter ticker, lane, warning, or thesis">
      <input id="swing-scout-min-score" type="number" min="0" max="100" step="1" value="45" aria-label="Minimum scout score">
      <label class="check"><input id="swing-scout-nasdaq" type="checkbox" checked> Nasdaq movers</label>
      <label class="check"><input id="swing-scout-hide-wait" type="checkbox"> hide wait rows</label>
      <button class="btn" type="button" id="swing-scout-load">Apply filters</button>
    </div>
    <div class="status" id="swing-scout-status-text"></div>
    <div class="section" style="margin-top:12px"><div id="swing-scout-results"></div></div>
  </section>
  <section class="panel" data-view="overview">
    <h2 style="margin:0 0 8px;font-size:18px">Climate-gated setups</h2>
    <div class="muted">Best setups filtered through the current swing climate gates, including DTE, spread, readiness, sizing, and candidate-count limits.</div>
    <div class="status" id="climate-gated-status-text"></div>
    <div class="section" style="margin-top:12px"><div id="climate-gated-results"></div></div>
  </section>
  <section class="panel" data-view="overview">
    <h2 style="margin:0 0 8px;font-size:18px">Action queue</h2>
    <div class="muted">Highest-priority local research items from data health, open positions, paper candidates, and watchlist context.</div>
    <div class="status" id="queue-status-text"></div>
    <div class="section" style="margin-top:12px"><div id="queue-results" class="table-wrap"></div></div>
  </section>
  <section class="panel" data-view="overview">
    <h2 style="margin:0 0 8px;font-size:18px">Market pulse</h2>
    <div class="muted">Free no-key regime context for swing-trade review: indexes, volatility, rates, dollar, gold, and oil proxies.</div>
    <div class="status" id="market-pulse-status-text"></div>
    <div class="section" style="margin-top:12px"><div id="market-pulse-results"></div></div>
  </section>
  <section class="panel" data-view="overview">
    <h2 style="margin:0 0 8px;font-size:18px">Breadth pulse</h2>
    <div class="muted">Free ETF-pair confirmation for broad participation, small caps, credit, growth, and defensive pressure.</div>
    <div class="status" id="breadth-pulse-status-text"></div>
    <div class="section" style="margin-top:12px"><div id="breadth-pulse-results"></div></div>
  </section>
  <section class="panel" data-view="overview">
    <h2 style="margin:0 0 8px;font-size:18px">Sector pulse</h2>
    <div class="muted">Free ETF strength map for checking whether option, share, and futures ideas have sector support.</div>
    <div class="status" id="sector-pulse-status-text"></div>
    <div class="section" style="margin-top:12px"><div id="sector-pulse-results"></div></div>
  </section>
  <section class="panel" data-view="overview">
    <h2 style="margin:0 0 8px;font-size:18px">Macro stress</h2>
    <div class="muted">Keyless FRED credit, curve, labor, inflation, growth, and liquidity context for swing-trade review.</div>
    <div class="status" id="macro-stress-status-text"></div>
    <div class="section" style="margin-top:12px"><div id="macro-stress-results"></div></div>
  </section>
  <section class="panel" data-view="overview">
    <h2 style="margin:0 0 8px;font-size:18px">Portfolio risk</h2>
    <div class="muted">Current open-position risk: concentration, exit pressure, repricing trouble, and worst open P&amp;L.</div>
    <div class="status" id="risk-status-text"></div>
    <div class="section" style="margin-top:12px"><div id="risk-results"></div></div>
  </section>
  <section class="panel" data-view="overview">
    <h2 style="margin:0 0 8px;font-size:18px">Performance</h2>
    <div class="muted">Local speed telemetry: slow engines, RAM cache, GPU sentiment status, and cache hit rates.</div>
    <div class="status" id="performance-status-text"></div>
    <div class="section" style="margin-top:12px"><div id="performance-results"></div></div>
  </section>
  <section class="panel" data-view="overview">
    <h2 style="margin:0 0 8px;font-size:18px">Data health</h2>
    <div class="muted">Checks whether validation, open positions, snapshots, and images line up before you trust the screen.</div>
    <div class="section" style="margin-top:12px"><div id="health-results"></div></div>
  </section>
  <section class="panel" data-view="positions">
    <h2 style="margin:0 0 8px;font-size:18px">Exit review cockpit</h2>
    <div class="muted">Recent hold, watch, tighten, close, stop, target, and expiry decisions from every scan.</div>
    <div class="status" id="exit-review-status-text"></div>
    <div class="section" style="margin-top:12px"><div id="exit-review-results"></div></div>
  </section>
  <section class="panel" data-view="positions">
    <h2 style="margin:0 0 8px;font-size:18px">Open position monitor</h2>
    <div class="muted">Review current lifecycle positions across options, shares, and futures. Click a row to look it up.</div>
    <div class="scan-controls">
      <select id="positions-asset" aria-label="Position asset">
        <option value="all">All assets</option>
        <option value="option">Options</option>
        <option value="share">Shares</option>
        <option value="futures">Futures</option>
      </select>
      <select id="positions-status" aria-label="Position status">
        <option value="all">All statuses</option>
        <option value="attention">Needs attention</option>
        <option value="trade">Trade</option>
        <option value="watch">Watch</option>
        <option value="hold">Hold exit action</option>
        <option value="tighten_stop">Tighten-stop action</option>
        <option value="close_early">Close-early action</option>
      </select>
      <input id="positions-query" placeholder="Filter ticker/contract">
      <button class="btn" type="button" id="positions-load">Apply filters</button>
    </div>
    <div class="status" id="positions-status-text"></div>
    <div class="section" style="margin-top:12px"><div id="positions-results" class="table-wrap"></div></div>
  </section>
  <section class="panel" data-view="explore">
    <h2 style="margin:0 0 8px;font-size:18px">Opportunity explorer</h2>
    <div class="muted">Filter the latest ranked options, shares, futures, and value ideas. Click a row to look it up.</div>
    <div class="scan-controls">
      <select id="explorer-asset" aria-label="Explorer asset">
        <option value="all">All assets</option>
        <option value="option">Options</option>
        <option value="share">Shares</option>
        <option value="futures">Futures</option>
        <option value="value">Value</option>
      </select>
      <select id="explorer-status" aria-label="Explorer status">
        <option value="all">All statuses</option>
        <option value="actionable">Actionable only</option>
        <option value="trade">Trade</option>
        <option value="watch">Watch</option>
        <option value="skip">Skip</option>
      </select>
      <input id="explorer-query" placeholder="Filter ticker/headline">
      <input id="explorer-confidence" type="number" min="0" max="100" step="1" placeholder="Min confidence">
      <button class="btn" type="button" id="explorer-load">Apply filters</button>
    </div>
    <div class="status" id="explorer-status-text"></div>
    <div class="section" style="margin-top:12px"><div id="explorer-results" class="table-wrap"></div></div>
  </section>
  <section class="panel" data-view="paper">
    <h2 style="margin:0 0 8px;font-size:18px">External paper candidates</h2>
    <div class="muted">Create a small executable candidate list for manual paper tracking. This is intentionally filtered down from the full internal signal stream.</div>
    <div class="scan-controls">
      <select id="paper-asset" aria-label="Paper asset">
        <option value="all">All assets</option>
        <option value="option">Options</option>
        <option value="share">Shares</option>
        <option value="futures">Futures</option>
      </select>
      <input id="paper-max-new" type="number" min="1" max="30" step="1" value="5" aria-label="Max new orders">
      <input id="paper-max-open" type="number" min="1" max="200" step="1" value="30" aria-label="Max open positions">
      <input id="paper-query" placeholder="Filter ticker/contract">
      <label class="check"><input id="paper-include-watch" type="checkbox"> include Watch</label>
      <label class="check"><input id="paper-zero-size" type="checkbox"> allow zero-size placeholders</label>
      <label class="check"><input id="paper-dry-run" type="checkbox"> review exclusions</label>
      <button class="btn" type="button" id="paper-preview">Preview candidates</button>
      <button class="btn" type="button" id="paper-export">Write export files</button>
    </div>
    <div class="status" id="paper-status-text"></div>
    <div class="brief-grid" style="margin-top:12px" id="paper-summary"></div>
    <div class="section" style="margin-top:12px"><div id="paper-results" class="table-wrap"></div></div>
  </section>
  <section class="panel" data-view="paper">
    <h2 style="margin:0 0 8px;font-size:18px">Agentic options queue</h2>
    <div class="muted">Build a long-dated options shortlist for Codex/Robinhood agent review. This creates queue and prompt files only; it does not place trades.</div>
    <div class="scan-controls">
      <input id="rh-budget" type="number" min="1" step="25" value="500" aria-label="Robinhood budget">
      <input id="rh-max-candidates" type="number" min="1" max="20" step="1" value="5" aria-label="Max candidates">
      <input id="rh-max-orders" type="number" min="1" max="10" step="1" value="2" aria-label="Max orders">
      <input id="rh-min-dte" type="number" min="0" max="1200" step="1" value="180" aria-label="Minimum DTE">
      <input id="rh-min-confidence" type="number" min="0" max="100" step="1" value="55" aria-label="Minimum confidence">
      <input id="rh-query" placeholder="Filter ticker/contract">
      <button class="btn" type="button" id="rh-preview">Preview queue</button>
      <button class="btn" type="button" id="rh-write">Write queue files</button>
    </div>
    <div class="status" id="rh-status-text"></div>
    <div class="brief-grid" style="margin-top:12px" id="rh-summary"></div>
    <div class="brief-cols">
      <div class="brief-list"><h4>Candidate orders</h4><div id="rh-results" class="table-wrap"></div></div>
      <div class="brief-list"><h4>Rejected</h4><div id="rh-rejected" class="table-wrap"></div></div>
    </div>
  </section>
  <section class="panel" data-view="chains">
    <h2 style="margin:0 0 8px;font-size:18px">Option chain scan</h2>
    <div class="muted">Inspect current contracts for any equity or ETF using Optedge's existing option-chain provider stack. This is read-only research, not execution.</div>
    <div class="scan-controls" aria-label="Option-chain presets">
      <input id="chain-preset" type="hidden" value="custom">
      <button class="btn chain-preset active" type="button" data-preset="custom">Custom</button>
      <button class="btn chain-preset" type="button" data-preset="swing">3m+ swing preset</button>
      <button class="btn chain-preset" type="button" data-preset="leaps">Long-dated preset</button>
      <button class="btn chain-preset" type="button" data-preset="liquid">Liquid preset</button>
    </div>
    <div class="scan-controls">
      <input id="chain-query" placeholder="Ticker or company, e.g. AAPL, Nvidia, SPY">
      <select id="chain-side" aria-label="Option side">
        <option value="all">Calls + puts</option>
        <option value="call">Calls</option>
        <option value="put">Puts</option>
      </select>
      <input id="chain-min-dte" type="number" min="0" max="1200" step="1" value="90" aria-label="Minimum DTE">
      <input id="chain-max-dte" type="number" min="1" max="1600" step="1" value="900" aria-label="Maximum DTE">
      <input id="chain-max-spread" type="number" min="0" max="100" step="1" value="25" aria-label="Maximum spread percent">
      <input id="chain-max-premium" type="number" min="0" step="25" value="500" aria-label="Maximum premium dollars">
      <input id="chain-min-oi" type="number" min="0" step="1" value="0" aria-label="Minimum open interest">
      <button class="btn" type="button" id="chain-scan">Scan chain</button>
    </div>
    <div class="status" id="chain-status-text"></div>
    <div class="brief-grid" style="margin-top:12px" id="chain-summary"></div>
    <div class="section" style="margin-top:12px"><div id="chain-results" class="table-wrap"></div></div>
    <div class="section" style="margin-top:12px">
      <h3><span>Shortlist chain sweep</span><span>Free/delayed</span></h3>
      <div class="muted" style="padding:0 12px 10px">Scan a small ticker list, or leave blank to use the latest Optedge option/share/value setups. This keeps free chain sources lighter and cleaner.</div>
      <div class="scan-controls" style="padding:0 12px 12px">
        <input id="chain-bulk-symbols" placeholder="Optional tickers: AAPL, NVDA, SPY">
        <input id="chain-bulk-symbol-limit" type="number" min="1" max="20" step="1" value="6" aria-label="Symbols to scan">
        <input id="chain-bulk-contract-limit" type="number" min="1" max="12" step="1" value="4" aria-label="Contracts per symbol">
        <button class="btn" type="button" id="chain-bulk-scan">Scan shortlist</button>
      </div>
      <div class="status" id="chain-bulk-status-text"></div>
      <div class="brief-grid" style="margin-top:12px" id="chain-bulk-summary"></div>
      <div id="chain-bulk-results" class="table-wrap"></div>
    </div>
  </section>
  <section class="panel" data-view="chains">
    <h2 style="margin:0 0 8px;font-size:18px">Saved option contracts</h2>
    <div class="muted">Exact option requests saved from chain scans and research search. Review DTE, readiness, and refresh the underlying chain before acting.</div>
    <div class="scan-controls">
      <button class="btn" type="button" id="saved-contracts-refresh">Refresh saved contracts</button>
      <button class="btn" type="button" id="saved-contracts-quotes">Refresh quotes</button>
      <button class="btn" type="button" id="saved-contracts-run">Run saved scans</button>
    </div>
    <div class="status" id="saved-contracts-status-text"></div>
    <div class="brief-grid" style="margin-top:12px" id="saved-contracts-summary"></div>
    <div class="section" style="margin-top:12px"><h3><span>Saved contract triage</span><span>Review queue</span></h3><div id="saved-contracts-triage"></div></div>
    <div class="section" style="margin-top:12px"><div id="saved-contracts-results" class="table-wrap"></div></div>
  </section>
  <section class="panel" data-view="providers">
    <h2 style="margin:0 0 8px;font-size:18px">Provider status</h2>
    <div class="muted">Check free/no-key data sources before trusting a scan. This does not run engines or place trades.</div>
    <div class="scan-controls">
      <input id="provider-query" placeholder="Ticker or company, e.g. AAPL, SPY, Nvidia" value="AAPL">
      <label class="check"><input id="provider-chain" type="checkbox" checked> include option-chain check</label>
      <button class="btn" type="button" id="provider-check">Check providers</button>
    </div>
    <div class="status" id="provider-status-text"></div>
    <div class="brief-grid" style="margin-top:12px" id="provider-summary"></div>
    <div class="section" style="margin-top:12px"><div id="provider-results" class="table-wrap"></div></div>
    <div class="section" style="margin-top:12px">
      <h3><span>Free source map</span><span>No-key registry</span></h3>
      <div class="status" id="free-sources-status-text"></div>
      <div class="brief-grid" style="margin-top:12px" id="free-sources-summary"></div>
      <div id="free-sources-results" class="table-wrap"></div>
    </div>
  </section>
  <section class="panel" data-view="research">
    <h2 style="margin:0 0 8px;font-size:18px">Research watchlist</h2>
    <div class="muted">Save tickers, company names, futures, or option requests you want checked again. Run focused scans from the list when you are ready.</div>
    <div class="search">
      <input id="watchlist-query" placeholder="Add symbol/company/option, e.g. Apple, natural gas, NVDA 20260618 C 200" autocomplete="off">
      <div class="search-actions">
        <button class="btn" type="button" id="watchlist-add">Add</button>
        <button class="btn" type="button" id="watchlist-run">Run watchlist scans</button>
      </div>
    </div>
    <div class="suggestions" id="watchlist-suggestions"></div>
    <div class="scan-controls">
      <select id="watchlist-scan-mode" aria-label="Watchlist scan mode">
        <option value="full">Full scan</option>
        <option value="quick">Quick scan</option>
      </select>
      <input id="watchlist-bankroll" type="number" min="1" step="100" placeholder="Bankroll override">
      <label class="check"><input id="watchlist-aggressive" type="checkbox"> aggressive sizing</label>
    </div>
    <div class="status" id="watchlist-status-text"></div>
    <div class="section" style="margin-top:12px"><div id="watchlist-results" class="table-wrap"></div></div>
  </section>
  <section class="panel" data-view="research">
    <h2 style="margin:0 0 8px;font-size:18px">SEC filing monitor</h2>
    <div class="muted">Official no-key SEC recent filings for saved watchlist symbols. Use this as event-risk context before reviewing a setup.</div>
    <div class="scan-controls">
      <button class="btn" type="button" id="sec-filings-refresh">Refresh SEC filings</button>
    </div>
    <div class="status" id="sec-filings-status-text"></div>
    <div class="brief-grid" style="margin-top:12px" id="sec-filings-summary"></div>
    <div class="section" style="margin-top:12px"><div id="sec-filings-results" class="table-wrap"></div></div>
  </section>
  <section class="panel" data-view="research">
    <h2 style="margin:0 0 8px;font-size:18px">Symbol lookup</h2>
    <div class="muted">Search the latest local scan snapshots and open positions. For a new symbol, run a focused scan first.</div>
    <div class="search">
      <input id="symbol" placeholder="Type ticker, company, or option idea, e.g. Nvidia, TSLA, AAPL 20260618 C 200" autocomplete="off">
      <div class="search-actions">
        <button class="btn" type="button" id="lookup">Lookup</button>
        <button class="btn" type="button" id="run-symbol">Run focused scan</button>
      </div>
    </div>
    <div class="suggestions" id="symbol-suggestions"></div>
    <div class="scan-controls">
      <select id="scan-mode" aria-label="Scan mode">
        <option value="full">Full scan</option>
        <option value="quick">Quick scan</option>
      </select>
      <input id="scan-bankroll" type="number" min="1" step="100" placeholder="Bankroll override">
      <label class="check"><input id="scan-aggressive" type="checkbox"> aggressive sizing</label>
    </div>
    <div class="status" id="lookup-status"></div>
    <div class="sections" id="lookup-results"></div>
  </section>
  <section class="panel" data-view="research">
    <h2 style="margin:0 0 8px;font-size:18px">Focused scan jobs</h2>
    <div class="muted">Runs started from this cockpit use <code>python run.py --universe SYMBOL --no-open</code> in the background.</div>
    <div class="job-list" id="jobs"></div>
    <pre class="logbox" id="job-log"></pre>
  </section>
  <section class="panel" data-view="overview">
    <h2 style="margin:0 0 8px;font-size:18px">System notes</h2>
    <ul class="muted" id="notes"></ul>
  </section>
</div>
<script>
const $ = (id) => document.getElementById(id);
function escHtml(v) { return String(v || '').replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll("'", '&#39;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'); }
function cell(v) { return v === null || v === undefined || v === '' ? '-' : escHtml(String(v).slice(0, 220)); }
function labelText(v) {
  const text = String(v || '').replaceAll('_', ' ').trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : '-';
}
function escAttr(v) { return escHtml(v); }
function rowSymbol(r) { return r.ticker || r.symbol || ''; }
function rowLookupSymbol(r) { return r.ticker || r.symbol || r.ticker_or_symbol || ''; }
function pct(v) {
  if (v === null || v === undefined || v === '') return '-';
  const n = Number(v);
  return Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : '-';
}
function ratio(v) {
  const n = Number(v);
  return Number.isFinite(n) ? `${n.toFixed(2)}x` : '-';
}
function moneyShort(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return '-';
  const a = Math.abs(n);
  if (a >= 1e9) return `$${(n / 1e9).toFixed(1)}B`;
  if (a >= 1e6) return `$${(n / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(0)}`;
}
function optionContractQuery(row) {
  const symbol = String(row.symbol || row.ticker || row.ticker_or_symbol || '').trim().toUpperCase();
  const expiry = String(row.expiry || '').trim();
  const side = String(row.side || '').toLowerCase().startsWith('p') ? 'P' : 'C';
  const strike = row.strike === null || row.strike === undefined ? '' : String(row.strike).trim();
  return [symbol, expiry, side, strike].filter(Boolean).join(' ');
}
function briefHtml(brief) {
  if (!brief) return '';
  const idea = brief.best_idea || {};
  const requested = brief.requested_option || {};
  const readiness = brief.paper_readiness || {};
  const open = brief.open_positions || {};
  const val = brief.validation || {};
  const action = brief.research_action || {};
  const sec = brief.recent_sec_filings || {};
  const secFund = brief.sec_fundamentals || {};
  const source = brief.resolution_source || '-';
  const resolvedFrom = brief.resolved_from || '';
  const resolvedText = source + (resolvedFrom ? ' from ' + resolvedFrom : '');
  const list = (rows) => (rows && rows.length ? rows.slice(0, 5).map(x => `<li>${escHtml(x.factor)} <b>${cell(x.value)}</b></li>`).join('') : '<li>None surfaced</li>');
  const warnings = (brief.risk_warnings && brief.risk_warnings.length)
    ? brief.risk_warnings.slice(0, 5).map(w => `<li>${escHtml(w)}</li>`).join('')
    : '<li>No local warnings found</li>';
  return `<div class="section"><h3><span>Research brief</span><span>${escHtml(brief.symbol || '')}</span></h3>
    <div style="padding:12px">
      <div class="brief-grid">
        <div class="brief-tile"><span>Best local idea</span><strong>${escHtml(idea.label || 'None')}</strong></div>
        <div class="brief-tile"><span>Requested option</span><strong>${escHtml(requested.label || '-')}</strong></div>
        <div class="brief-tile"><span>Requested match</span><strong>${escHtml(requested.match_quality || '-')}</strong></div>
        <div class="brief-tile"><span>Matched contract</span><strong>${escHtml(requested.matched_contract || '-')}</strong></div>
        <div class="brief-tile"><span>Paper readiness</span><strong>${escHtml(readiness.label || '-')}</strong></div>
        <div class="brief-tile"><span>Readiness score</span><strong>${cell(readiness.score)}</strong></div>
        <div class="brief-tile"><span>Quote source</span><strong>${escHtml(idea.quote_source_label || '-')}</strong></div>
        <div class="brief-tile"><span>Snapshot age</span><strong>${cell(idea.snapshot_age_min)} min</strong></div>
        <div class="brief-tile"><span>Freshness</span><strong>${escHtml(idea.snapshot_freshness || '-')}</strong></div>
        <div class="brief-tile"><span>Research action</span><strong>${escHtml(action.label || 'Review')}</strong></div>
        <div class="brief-tile"><span>Action risk</span><strong>${escHtml(action.risk_level || '-')}</strong></div>
        <div class="brief-tile"><span>Status</span><strong>${escHtml(idea.trade_status || '-')}</strong></div>
        <div class="brief-tile"><span>Spread</span><strong>${pct(idea.spread_pct)}</strong></div>
        <div class="brief-tile"><span>Net edge</span><strong>${pct(idea.net_edge_pct)}</strong></div>
        <div class="brief-tile"><span>Resolved via</span><strong>${escHtml(resolvedText)}</strong></div>
        <div class="brief-tile"><span>Open exposure</span><strong>${cell(open.count || 0)}</strong></div>
        <div class="brief-tile"><span>Recent SEC filings</span><strong>${cell(sec.count || 0)}</strong></div>
        <div class="brief-tile"><span>SEC cash</span><strong>${moneyShort(secFund.cash)}</strong></div>
        <div class="brief-tile"><span>SEC cash/debt</span><strong>${ratio(secFund.cash_to_debt)}</strong></div>
        <div class="brief-tile"><span>SEC debt/assets</span><strong>${pct(secFund.debt_to_assets)}</strong></div>
        <div class="brief-tile"><span>SEC net margin</span><strong>${pct(secFund.net_margin)}</strong></div>
        <div class="brief-tile"><span>Avg unrealized</span><strong>${pct(open.avg_unrealized_pct)}</strong></div>
        <div class="brief-tile"><span>Validation win rate</span><strong>${pct(val.win_rate)}</strong></div>
        <div class="brief-tile"><span>Validation avg return</span><strong>${pct(val.avg_return)}</strong></div>
      </div>
      <div class="brief-cols">
        <div class="brief-list"><h4>Positive factors</h4><ul>${list(brief.top_positive_factors)}</ul></div>
        <div class="brief-list"><h4>Negative factors</h4><ul>${list(brief.top_negative_factors)}</ul></div>
        <div class="brief-list"><h4>Readiness checklist</h4><ul>${(readiness.checks && readiness.checks.length) ? readiness.checks.slice(0, 6).map(c => `<li>${escHtml(c.label)}: ${escHtml(c.detail)}</li>`).join('') : '<li>No readiness checks available.</li>'}</ul></div>
        <div class="brief-list"><h4>Next steps</h4><ul>${(action.next_steps && action.next_steps.length) ? action.next_steps.slice(0, 5).map(s => `<li>${escHtml(s)}</li>`).join('') : '<li>Review local factors and exposure.</li>'}</ul></div>
        <div class="brief-list"><h4>Warnings</h4><ul>${warnings}</ul></div>
      </div>
    </div>
  </div>`;
}
function table(rows, clickRows=false) {
  if (!rows || rows.length === 0) return '<div class="empty">No matching rows.</div>';
  const cols = [...new Set(rows.flatMap(r => Object.keys(r)))];
  const head = cols.map(c => `<th>${escHtml(c)}</th>`).join('');
  const body = rows.map(r => {
    const sym = clickRows ? rowLookupSymbol(r) : '';
    const attrs = sym ? ` class="clickable-row" data-symbol="${escAttr(sym)}"` : '';
    return `<tr${attrs}>${cols.map(c => `<td>${cell(r[c])}</td>`).join('')}</tr>`;
  }).join('');
  return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}
function bestSetupCard(row) {
  const symbol = row.ticker_or_symbol || '';
  const action = row.action || row.asset || '';
  const status = row.trade_status || 'Review';
  const readiness = row.readiness_label || 'review';
  const flags = Array.isArray(row.risk_flags) ? row.risk_flags.join(', ') : (row.risk_flags || '');
  const swingReasons = Array.isArray(row.swing_fit_reasons) ? row.swing_fit_reasons.join(', ') : (row.swing_fit_reasons || '');
  const swingWarnings = Array.isArray(row.swing_fit_warnings) ? row.swing_fit_warnings.join(', ') : (row.swing_fit_warnings || '');
  const chainBtn = canScanOptionChainSymbol(symbol, row.asset)
    ? `<button class="btn setup-chain-btn" type="button" data-symbol="${escAttr(symbol)}">Scan 3m+ chain</button>`
    : '';
  const scanBtn = symbol
    ? `<button class="btn setup-scan-btn" type="button" data-symbol="${escAttr(symbol)}">Run focused scan</button>`
    : '';
  const swingRows = row.asset === 'option' && (row.swing_fit_label || row.swing_fit_score)
    ? `<div class="row"><span>Swing fit</span><b>${cell(labelText(row.swing_fit_label))} / ${cell(row.swing_fit_score)}</b></div>
       <div class="row"><span>Swing why</span><b>${cell(swingReasons || '-')}</b></div>
       <div class="row"><span>Swing warnings</span><b>${cell(swingWarnings || 'clear')}</b></div>`
    : '';
  return `<article class="setup-card">
    <header>
      <div><h3>${cell(row.setup || symbol)}</h3><small>${cell(row.reason_selected || '')}</small></div>
      <span class="pill ${escAttr(readiness)}">${cell(readiness)}</span>
    </header>
    <div class="row"><span>Asset</span><b>${cell(row.asset)}</b></div>
    <div class="row"><span>Readiness</span><b>${cell(row.readiness_score)}</b></div>
    <div class="row"><span>Action</span><b>${cell(action)}</b></div>
    <div class="row"><span>Score</span><b>${cell(row.score)}</b></div>
    <div class="row"><span>Confidence</span><b>${cell(row.confidence)}</b></div>
    <div class="row"><span>Entry</span><b>${cell(row.entry_price)}</b></div>
    <div class="row"><span>Stop / target</span><b>${cell(row.stop_price)} / ${cell(row.target_price)}</b></div>
    <div class="row"><span>Size</span><b>${cell(row.size)}</b></div>
    <div class="row"><span>Quality</span><b>${cell(row.quality)}</b></div>
    ${swingRows}
    <div class="row"><span>Flags</span><b>${cell(flags || 'clear')}</b></div>
    <div class="row"><span>Status</span><b>${cell(status)}</b></div>
    <button class="btn setup-lookup-btn" type="button" data-symbol="${escAttr(symbol)}">Open research</button>
    ${scanBtn}
    ${chainBtn}
  </article>`;
}
function bestSetupsHtml(data) {
  const summaries = data.asset_summaries || [];
  const summaryTiles = summaries.map(row => `<div class="brief-tile">
    <span>${cell(row.asset)}</span>
    <strong>${cell(row.actionable_rows || 0)} / ${cell(row.rows || 0)}</strong>
    <small class="muted">${cell(row.status || '-')} ${row.chain_shortlist_rows ? ' | chain ' + cell(row.chain_shortlist_rows) : ''} ${row.snapshot_freshness ? ' | ' + cell(row.snapshot_freshness) : ''}</small>
  </div>`).join('');
  const rows = data.rows || [];
  const cards = rows.length
    ? `<div class="setup-grid">${rows.map(bestSetupCard).join('')}</div>`
    : '<div class="empty">No best setups found yet. Run a scan to create top_* snapshots.</div>';
  const notes = (data.notes || []).length
    ? `<div class="brief-list" style="margin-top:10px"><h4>Notes</h4><ul>${data.notes.map(n => `<li>${escHtml(n)}</li>`).join('')}</ul></div>`
    : '';
  return `<div style="padding:12px">
    <div class="brief-grid">${summaryTiles}</div>
    ${cards}
    ${notes}
    <div class="brief-list" style="margin-top:10px"><h4>Detail</h4>${table(rows, true)}</div>
  </div>`;
}
function swingScoutCard(row) {
  const symbol = row.ticker_or_symbol || '';
  const reasons = Array.isArray(row.reasons) ? row.reasons.join(', ') : (row.reasons || '');
  const warnings = Array.isArray(row.warnings) ? row.warnings.join(', ') : (row.warnings || '');
  const breakdown = Array.isArray(row.factor_breakdown) ? row.factor_breakdown.slice(0, 4) : [];
  const factorRows = breakdown.length
    ? `<div class="factor-stack">${breakdown.map(f => `<div class="factor-row">
        <strong>${cell(f.factor || '-')}</strong>
        <span>${cell(f.detail || '')}</span>
        <em>${cell(f.score)}</em>
      </div>`).join('')}</div>`
    : '';
  const chainBtn = canScanOptionChainSymbol(symbol, row.asset)
    ? `<button class="btn setup-chain-btn" type="button" data-symbol="${escAttr(symbol)}">Scan 3m+ chain</button>`
    : '';
  const scanBtn = symbol
    ? `<button class="btn setup-scan-btn" type="button" data-symbol="${escAttr(symbol)}">Run focused scan</button>`
    : '';
  return `<article class="setup-card">
    <header>
      <div><h3>${cell(row.setup || symbol)}</h3><small>${cell(labelText(row.lane || 'swing scout'))}</small></div>
      <span class="pill ${escAttr(row.readiness_label || 'review')}">${cell(row.swing_scout_score)}/100</span>
    </header>
    <div class="row"><span>Asset</span><b>${cell(row.asset)}</b></div>
    <div class="row"><span>Cap bucket</span><b>${cell(row.market_cap_bucket)}</b></div>
    <div class="row"><span>Squeeze / attention</span><b>${cell(row.squeeze_score)} / ${cell(row.attention_score)}</b></div>
    <div class="row"><span>Momentum / value</span><b>${cell(row.momentum_score)} / ${cell(row.value_score_component)}</b></div>
    <div class="row"><span>Execution</span><b>${cell(row.execution_score)}</b></div>
    <div class="row"><span>Readiness</span><b>${cell(row.readiness_label)} ${cell(row.readiness_score)}</b></div>
    ${factorRows}
    <div class="row"><span>Entry</span><b>${cell(row.entry_price)}</b></div>
    <div class="row"><span>Stop / target</span><b>${cell(row.stop_price)} / ${cell(row.target_price)}</b></div>
    <div class="row"><span>Size</span><b>${cell(row.size)}</b></div>
    <div class="row"><span>Why</span><b>${cell(reasons || '-')}</b></div>
    <div class="row"><span>Warnings</span><b>${cell(warnings || 'clear')}</b></div>
    <button class="btn setup-lookup-btn" type="button" data-symbol="${escAttr(symbol)}">Open research</button>
    ${scanBtn}
    ${chainBtn}
  </article>`;
}
function swingScoutHtml(data) {
  if (!data) return '<div class="empty">No swing scout data available.</div>';
  const rows = data.rows || [];
  const filters = data.filters || {};
  const tiles = `<div class="brief-grid">
    <div class="brief-tile"><span>Candidates</span><strong>${cell(data.count || 0)}</strong></div>
    <div class="brief-tile"><span>Reviewed</span><strong>${cell(data.reviewed_count || 0)}</strong></div>
    <div class="brief-tile"><span>Min option DTE</span><strong>${cell(data.min_option_dte || 90)}d</strong></div>
    <div class="brief-tile"><span>Min scout score</span><strong>${cell(filters.min_score ?? 45)}</strong></div>
    <div class="brief-tile"><span>Assets</span><strong>${countMapText(data.asset_counts || {})}</strong></div>
    <div class="brief-tile"><span>Lanes</span><strong>${countMapText(data.lane_counts || {})}</strong></div>
  </div>`;
  const cards = rows.length
    ? `<div class="setup-grid">${rows.map(swingScoutCard).join('')}</div>`
    : '<div class="empty">No high-upside scout rows cleared the local score floor. Refresh scans or loosen filters in the explorer.</div>';
  const notes = (data.notes || []).length
    ? `<div class="brief-list" style="margin-top:10px"><h4>Notes</h4><ul>${data.notes.map(n => `<li>${escHtml(n)}</li>`).join('')}</ul></div>`
    : '';
  return `<div style="padding:12px">
    ${tiles}
    ${cards}
    ${notes}
    <div class="brief-list" style="margin-top:10px"><h4>Detail</h4>${table(rows, true)}</div>
  </div>`;
}
function climateGatedCard(row) {
  const symbol = row.ticker_or_symbol || '';
  const gate = row.climate_gate_status || 'hold';
  const reasons = Array.isArray(row.climate_gate_reasons) ? row.climate_gate_reasons.join(', ') : (row.climate_gate_reasons || '');
  const swingReasons = Array.isArray(row.swing_fit_reasons) ? row.swing_fit_reasons.join(', ') : (row.swing_fit_reasons || '');
  const chainBtn = canScanOptionChainSymbol(symbol, row.asset)
    ? `<button class="btn setup-chain-btn" type="button" data-symbol="${escAttr(symbol)}">Scan 3m+ chain</button>`
    : '';
  const scanBtn = symbol
    ? `<button class="btn setup-scan-btn" type="button" data-symbol="${escAttr(symbol)}">Run focused scan</button>`
    : '';
  const swingRows = row.asset === 'option' && (row.swing_fit_label || row.swing_fit_score)
    ? `<div class="row"><span>Swing fit</span><b>${cell(labelText(row.swing_fit_label))} / ${cell(row.swing_fit_score)}</b></div>
       <div class="row"><span>Swing why</span><b>${cell(swingReasons || '-')}</b></div>`
    : '';
  return `<article class="setup-card">
    <header>
      <div><h3>${cell(row.setup || symbol)}</h3><small>${cell(row.reason_selected || '')}</small></div>
      <span class="pill ${escAttr(gate)}">${cell(gate)}</span>
    </header>
    <div class="row"><span>Asset</span><b>${cell(row.asset)}</b></div>
    <div class="row"><span>Gate score</span><b>${cell(row.climate_gate_score)}</b></div>
    <div class="row"><span>Readiness</span><b>${cell(row.readiness_score)} / ${cell(row.playbook_min_readiness)}</b></div>
    <div class="row"><span>Action</span><b>${cell(row.action)}</b></div>
    <div class="row"><span>Confidence</span><b>${cell(row.confidence)}</b></div>
    <div class="row"><span>Entry</span><b>${cell(row.entry_price)}</b></div>
    <div class="row"><span>Stop / target</span><b>${cell(row.stop_price)} / ${cell(row.target_price)}</b></div>
    <div class="row"><span>Size</span><b>${cell(row.size)}</b></div>
    ${swingRows}
    <div class="row"><span>Gate reason</span><b>${cell(reasons || 'passes climate gates')}</b></div>
    <button class="btn setup-lookup-btn" type="button" data-symbol="${escAttr(symbol)}">Open research</button>
    ${scanBtn}
    ${chainBtn}
  </article>`;
}
function climateGatedSetupsHtml(data) {
  if (!data) return '<div class="empty">No climate-gated setup data available.</div>';
  const playbook = data.playbook || {};
  const rows = data.rows || [];
  const held = data.held || [];
  const counts = data.asset_counts || {};
  const countRows = Object.keys(counts).sort().map(asset => ({
    asset,
    pass: counts[asset].pass || 0,
    hold: counts[asset].hold || 0
  }));
  const tiles = `<div class="brief-grid">
    <div class="brief-tile"><span>Climate</span><strong>${cell(data.climate_label)}</strong></div>
    <div class="brief-tile"><span>Score</span><strong>${cell(data.climate_score)}/100</strong></div>
    <div class="brief-tile"><span>Passed</span><strong>${cell(data.selected_count || 0)} / ${cell(data.source_setup_count || 0)}</strong></div>
    <div class="brief-tile"><span>Held</span><strong>${cell(data.held_count || 0)}</strong></div>
    <div class="brief-tile"><span>Min readiness</span><strong>${cell(playbook.min_readiness_score)}</strong></div>
    <div class="brief-tile"><span>Option gates</span><strong>${cell(playbook.option_min_dte)}d / ${pct(playbook.option_max_spread_pct)}</strong></div>
    <div class="brief-tile"><span>Candidate cap</span><strong>${cell(data.max_new_candidates)}</strong></div>
    <div class="brief-tile"><span>Posture</span><strong>${cell(data.posture)}</strong></div>
  </div>`;
  const selectedCards = rows.length
    ? `<div class="setup-grid">${rows.map(climateGatedCard).join('')}</div>`
    : '<div class="empty">No setup clears today\\'s climate gates. That is useful information: wait for cleaner rows or a better tape.</div>';
  const heldTable = held.length
    ? table(held.map(row => ({
        asset: row.asset,
        setup: row.setup,
        gate_score: row.climate_gate_score,
        readiness: row.readiness_score,
        reasons: Array.isArray(row.climate_gate_reasons) ? row.climate_gate_reasons.join(', ') : row.climate_gate_reasons,
        swing_fit: row.swing_fit_label,
        swing_score: row.swing_fit_score,
        status: row.trade_status,
        source_file: row.source_file
      })), true)
    : '<div class="empty">No held setup rows.</div>';
  const notes = (data.notes || []).length
    ? `<div class="brief-list" style="margin-top:10px"><h4>Notes</h4><ul>${data.notes.map(n => `<li>${escHtml(n)}</li>`).join('')}</ul></div>`
    : '';
  return `<div style="padding:12px">
    ${tiles}
    ${selectedCards}
    <div class="brief-cols">
      <div class="brief-list"><h4>Pass / hold by asset</h4>${table(countRows)}</div>
      <div class="brief-list"><h4>Climate gates</h4>${table(data.trade_gates || [])}</div>
    </div>
    <div class="brief-list" style="margin-top:10px"><h4>Held for review</h4>${heldTable}</div>
    ${notes}
  </div>`;
}
function trustRibbonHtml(rows) {
  if (!rows || !rows.length) return '';
  return `<div class="trust-ribbon" aria-label="Data trust ribbon">${rows.map(row => `<article class="trust-card ${escAttr(row.tone || '')}">
    <span>${cell(row.label)}</span>
    <strong>${cell(labelText(row.value || '-'))}</strong>
    <p>${cell(row.detail || '')}</p>
  </article>`).join('')}</div>`;
}
function commandCenterHtml(data) {
  if (!data) return '<div class="empty">No command-center data available.</div>';
  const action = data.next_action || {};
  const trustRibbon = trustRibbonHtml(data.trust_ribbon || []);
  const cards = (data.cards || []).map(card => `<article class="command-card ${escAttr(card.tone || '')}">
    <span>${cell(card.label)}</span>
    <strong>${cell(labelText(card.value))}</strong>
    <p>${cell(card.detail)}</p>
  </article>`).join('');
  const queue = (data.top_queue || []).length
    ? `<div class="brief-list"><h4>Top queue</h4>${todayReviewTable(data.top_queue || [])}</div>`
    : '<div class="empty">No top queue items surfaced.</div>';
  const swingActions = data.swing_actions || [];
  const swingRadar = swingActions.length
    ? `<div class="brief-list"><h4>Best swing radar</h4>
        <div class="review-grid">${swingActions.slice(0, 4).map(r => `<article class="review-card">
          <header><span class="priority-badge ${escAttr(reviewPriorityClass(r.priority))}">${cell(r.score || r.priority)}</span><b>${cell(r.label)}</b></header>
          <p>${cell(r.detail)}</p>
          <div class="review-meta">
            <span class="pill">${cell(r.asset || '-')}</span>
            ${r.factor_summary ? `<span class="pill">${cell(r.factor_summary)}</span>` : ''}
            <span>${cell(labelText(r.lane || '-'))}</span>
            <span>${cell(r.snapshot_freshness || '-')}</span>
          </div>
          <button class="btn command-center-action-btn" type="button" data-action="${escAttr(r.action || '')}" data-route="${escAttr(r.route || '')}" data-query="${escAttr(r.query || '')}" data-symbol="${escAttr(r.symbol || '')}">${escHtml(todayReviewActionLabel(r.action, r.route))}</button>
        </article>`).join('')}</div>
      </div>`
    : '<div class="brief-list"><h4>Best swing radar</h4><div class="empty">No local swing-radar candidates cleared the current filters.</div></div>';
  const notes = (data.notes || []).length
    ? `<div class="brief-list"><h4>Notes</h4><ul>${data.notes.map(n => `<li>${escHtml(n)}</li>`).join('')}</ul></div>`
    : '';
  const title = `${cell(labelText(data.status || 'review'))} - ${cell(labelText(data.climate_label || 'unknown climate'))}`;
  return `<div class="command-shell">
    <div class="command-top">
      <div class="command-hero">
        <div class="command-eyebrow">Local decision state</div>
        <h3 class="command-title">${title}</h3>
        <p class="command-detail">${cell(data.status_detail || '')}</p>
        <div class="brief-grid">
          <div class="brief-tile"><span>Climate score</span><strong>${cell(data.climate_score || '-')}</strong></div>
          <div class="brief-tile"><span>Review queue</span><strong>${cell(data.review_count || 0)}</strong></div>
          <div class="brief-tile"><span>Total open</span><strong>${cell(data.total_open || 0)}</strong></div>
          <div class="brief-tile"><span>No-key sources</span><strong>${cell(data.no_key_count || 0)} / ${cell(data.source_count || 0)}</strong></div>
        </div>
      </div>
      <aside class="command-action">
        <span class="pill ${escAttr(reviewPriorityClass(action.priority))}">${cell(action.priority || 'next')}</span>
        <h3>${cell(action.label || 'No urgent action')}</h3>
        <p>${cell(action.detail || data.status_detail || '')}</p>
        <div class="review-meta">
          <span class="pill">${cell(action.source || 'local')}</span>
          <span>${cell(action.symbol || action.query || '-')}</span>
        </div>
        <button class="btn command-center-action-btn" type="button" data-action="${escAttr(action.action || '')}" data-route="${escAttr(action.route || '')}" data-query="${escAttr(action.query || '')}" data-symbol="${escAttr(action.symbol || '')}">${escHtml(todayReviewActionLabel(action.action, action.route))}</button>
      </aside>
    </div>
    ${trustRibbon}
    <div class="command-card-grid">${cards}</div>
    <div class="brief-cols">${queue}${swingRadar}${notes}</div>
  </div>`;
}
function swingPacketHtml(data) {
  if (!data) return '<div class="empty">No swing packet built yet.</div>';
  const command = data.command_center || {};
  const action = command.next_action || {};
  const decisionGate = data.decision_gate || {};
  const climate = data.swing_climate || {};
  const chain = data.chain_shortlist || {};
  const chainQuality = chain.quality_summary || {};
  const chainRefresh = data.chain_refresh || {};
  const paper = data.paper_candidates || {};
  const secRisk = data.sec_dilution_risk || {};
  const trustCheck = data.data_trust_check || {};
  const trust = trustCheck.data_trust || {};
  const eventRisk = data.event_risk || {};
  const todayRows = (data.today_review && data.today_review.rows) || [];
  const setupRows = (data.climate_gated_setups && data.climate_gated_setups.rows) || [];
  const paperRows = paper.rows || [];
  const chainRows = chain.rows || [];
  const secRows = secRisk.rows || [];
  const eventRows = (eventRisk.rows || []).slice(0, 8).map(r => ({
    symbol: r.symbol,
    asset: r.asset,
    risk: r.event_risk,
    earnings: r.next_earnings_date || '-',
    days: r.days_to_earnings,
    catalyst: r.next_catalyst_date || '-',
    action: r.action,
    fresh: r.snapshot_freshness
  }));
  const trustRows = (trustCheck.rows || []).slice(0, 6).map(r => ({
    provider: r.provider,
    status: r.status,
    source: r.history_source || '-',
    quality: r.history_quality || '-',
    rows: r.rows,
    ms: r.latency_ms,
    note: r.note
  }));
  const chainCards = chainRows.length
    ? `<div class="setup-grid">${chainRows.slice(0, 6).map(optionContractCard).join('')}</div>
       <div class="brief-list" style="margin-top:10px"><h4>Comparison table</h4>${table(chainRows.slice(0, 8), true)}</div>`
    : '<div class="empty">No saved 3m+ chain shortlist yet. Use Write + 3m+ chain scan to build one.</div>';
  const chainQualityRows = [
    {check: 'Primary review', value: chainQuality.primary_review_count || 0},
    {check: 'Clean swing', value: chainQuality.clean_swing_count || 0},
    {check: 'Inside budget', value: chainQuality.inside_budget_count || 0},
    {check: 'Liquid OI/spread', value: chainQuality.liquid_count || 0},
    {check: 'Active volume', value: chainQuality.active_volume_count || 0},
    {check: 'Median spread', value: pct(chainQuality.median_spread_pct)}
  ];
  const secRiskRows = secRows.slice(0, 8).map(r => ({
    ticker: r.ticker,
    form: r.form,
    date: r.filing_date,
    age: r.days_old,
    action: r.risk_action,
    signal: r.signal,
    description: r.description
  }));
  const secRiskBlock = `<div class="brief-list">
    <h4>SEC dilution / offering risk</h4>
    ${secRiskRows.length ? table(secRiskRows, true) : '<div class="empty">No recent offering or dilution filings surfaced for saved research targets.</div>'}
    ${(secRisk.notes || []).length ? `<ul>${secRisk.notes.slice(0, 3).map(n => `<li>${escHtml(n)}</li>`).join('')}</ul>` : ''}
  </div>`;
  const trustBlock = `<div class="brief-list">
    <h4>Focus data trust</h4>
    <div class="review-meta">
      <span class="pill">${cell(trustCheck.symbol || trustCheck.query || '-')}</span>
      <span>${cell(trust.label || 'unknown')} ${cell(trust.score || 0)}/100</span>
      <span>${cell(trust.history_source_summary || 'no history source')}</span>
    </div>
    ${trustRows.length ? table(trustRows, true) : '<div class="empty">No focus data-trust probe available yet.</div>'}
  </div>`;
  const chainQualityBlock = `<div class="brief-list">
    <h4>Chain quality</h4>
    <div class="review-meta">
      <span class="pill">${cell(chainQuality.status || 'unknown')}</span>
      <span>${cell(chainQuality.score || 0)}/100</span>
      <span>${cell(chainQuality.best_contract || '-')}</span>
      <span>${cell(chainQuality.best_grade || '-')} / ${cell(chainQuality.best_review_lane || '-')}</span>
    </div>
    ${table(chainQualityRows, true)}
    ${(chainQuality.warnings || []).length ? `<ul>${chainQuality.warnings.slice(0, 4).map(n => `<li>${escHtml(n)}</li>`).join('')}</ul>` : ''}
  </div>`;
  const eventBlock = `<div class="brief-list">
    <h4>Earnings / catalyst event risk</h4>
    <div class="review-meta">
      <span class="pill">${cell(eventRisk.status || 'unknown')}</span>
      <span>high ${cell(eventRisk.high_count || 0)}</span>
      <span>medium ${cell(eventRisk.medium_count || 0)}</span>
      <span>watch ${cell(eventRisk.watch_count || 0)}</span>
    </div>
    ${eventRows.length ? table(eventRows, true) : '<div class="empty">No near-term earnings, whisper, or catalyst risk found in local packet snapshots.</div>'}
    ${(eventRisk.warnings || []).length ? `<ul>${eventRisk.warnings.slice(0, 3).map(n => `<li>${escHtml(n)}</li>`).join('')}</ul>` : ''}
  </div>`;
  const gateBlock = `<div class="decision-strip" style="margin-top:12px">
    <div class="decision-main">
      <div class="command-eyebrow">Decision gate</div>
      <h3>${cell(decisionGate.label || 'Review')} ${cell(decisionGate.score || 0)}/100</h3>
      <p>${cell(decisionGate.primary_action || 'Review packet checks before acting.')}</p>
      <div class="review-meta">
        <span class="pill ${escAttr(decisionGate.status || 'review')}">${cell(decisionGate.status || 'review')}</span>
        <span>${cell(decisionGate.blocker_count || 0)} blocker(s)</span>
        <span>${cell(decisionGate.warning_count || 0)} warning(s)</span>
        <span>${cell(data.does_not_place_orders ? 'no broker execution' : 'review')}</span>
      </div>
    </div>
    <div class="decision-side">
      <a class="btn" href="/artifact/swing-packet-md" target="_blank">Open Markdown packet</a>
      <button class="btn command-center-action-btn" type="button" data-action="${escAttr(action.action || '')}" data-route="${escAttr(action.route || '')}" data-query="${escAttr(action.query || '')}" data-symbol="${escAttr(action.symbol || '')}">${escHtml(todayReviewActionLabel(action.action, action.route))}</button>
    </div>
  </div>
  <div class="brief-cols">
    <div class="brief-list"><h4>Blockers</h4>${(decisionGate.blockers || []).length ? `<ul>${decisionGate.blockers.slice(0, 6).map(x => `<li>${escHtml(x)}</li>`).join('')}</ul>` : '<div class="empty">No hard packet blockers.</div>'}</div>
    <div class="brief-list"><h4>Warnings</h4>${(decisionGate.warnings || []).length ? `<ul>${decisionGate.warnings.slice(0, 6).map(x => `<li>${escHtml(x)}</li>`).join('')}</ul>` : '<div class="empty">No packet warnings.</div>'}</div>
    <div class="brief-list"><h4>Confirmed</h4>${(decisionGate.confirmations || []).length ? `<ul>${decisionGate.confirmations.slice(0, 6).map(x => `<li>${escHtml(x)}</li>`).join('')}</ul>` : '<div class="empty">No confirmations yet.</div>'}</div>
  </div>`;
  const tiles = `<div class="brief-grid">
    <div class="brief-tile"><span>Decision gate</span><strong>${cell(decisionGate.label || 'review')} ${cell(decisionGate.score || 0)}</strong></div>
    <div class="brief-tile"><span>Status</span><strong>${cell(data.status || '-')}</strong></div>
    <div class="brief-tile"><span>Data health</span><strong>${cell(command.data_health_status || '-')}</strong></div>
    <div class="brief-tile"><span>Risk</span><strong>${cell(command.risk_level || '-')}</strong></div>
    <div class="brief-tile"><span>Climate</span><strong>${cell(climate.climate_label || command.climate_label || '-')}</strong></div>
    <div class="brief-tile"><span>Review queue</span><strong>${cell((data.today_review || {}).count || 0)}</strong></div>
    <div class="brief-tile"><span>Focus trust</span><strong>${cell(trust.label || 'unknown')} ${cell(trust.score || 0)}</strong></div>
    <div class="brief-tile"><span>Event risk</span><strong>${cell(eventRisk.status || 'unknown')}</strong></div>
    <div class="brief-tile"><span>Chain quality</span><strong>${cell(chainQuality.status || 'unknown')} ${cell(chainQuality.score || 0)}</strong></div>
    <div class="brief-tile"><span>Chain rows</span><strong>${cell(chain.count || 0)}</strong></div>
    <div class="brief-tile"><span>Chain refreshed</span><strong>${cell(chainRefresh.attempted ? ((chainRefresh.row_count || 0) + ' rows') : 'no')}</strong></div>
    <div class="brief-tile"><span>SEC offering risk</span><strong>${cell(secRisk.status || 'unknown')}</strong></div>
    <div class="brief-tile"><span>Paper candidates</span><strong>${cell(paper.selected_count || 0)}</strong></div>
    <div class="brief-tile"><span>Files written</span><strong>${cell(data.wrote_files ? 'yes' : 'no')}</strong></div>
  </div>`;
  const notes = (data.notes || []).length
    ? `<div class="brief-list"><h4>Notes</h4><ul>${data.notes.map(n => `<li>${escHtml(n)}</li>`).join('')}</ul></div>`
    : '';
  return `<div style="padding:12px">
    ${tiles}
    ${gateBlock}
    <div class="decision-strip" style="margin-top:12px">
      <div class="decision-main">
        <h3>${cell(data.headline || 'Review local research')}</h3>
        <p>${cell(action.detail || command.status_detail || '')}</p>
        <div class="review-meta">
          <span class="pill">${cell(action.source || 'local')}</span>
          <span>${cell(action.query || action.symbol || '-')}</span>
          <span>${cell(data.does_not_place_orders ? 'no broker execution' : 'review')}</span>
        </div>
      </div>
      <div class="decision-side">
        <button class="btn command-center-action-btn" type="button" data-action="${escAttr(action.action || '')}" data-route="${escAttr(action.route || '')}" data-query="${escAttr(action.query || '')}" data-symbol="${escAttr(action.symbol || '')}">${escHtml(todayReviewActionLabel(action.action, action.route))}</button>
        <a class="btn" href="/artifact/swing-packet-md" target="_blank">Open Markdown packet</a>
      </div>
    </div>
    <div class="section" style="margin-top:12px">
      <h3><span>Saved 3m+ chain contracts</span><span>${cell(chain.count || 0)} row(s)</span></h3>
      <div style="padding:12px">${chainCards}</div>
    </div>
    <div class="brief-cols">
      <div class="brief-list"><h4>Today review</h4>${todayReviewTable(todayRows.slice(0, 6))}</div>
      <div class="brief-list"><h4>Climate-gated setups</h4>${table(setupRows.slice(0, 6), true)}</div>
      ${trustBlock}
      ${chainQualityBlock}
      ${eventBlock}
      ${secRiskBlock}
      <div class="brief-list"><h4>Paper candidates</h4>${table(paperRows.slice(0, 6), true)}</div>
      ${notes}
    </div>
  </div>`;
}
function todayReviewHtml(data) {
  if (!data) return '<div class="empty">No today-review data available.</div>';
  const rows = data.rows || [];
  const counts = data.category_counts || {};
  const tiles = `<div class="brief-grid">
    <div class="brief-tile"><span>Queue</span><strong>${cell(data.count || 0)}</strong></div>
    <div class="brief-tile"><span>Climate</span><strong>${cell(data.climate_label || '-')}</strong></div>
    <div class="brief-tile"><span>Climate score</span><strong>${cell(data.climate_score || '-')}</strong></div>
    <div class="brief-tile"><span>Setups</span><strong>${cell(data.setup_count || counts.setup || 0)}</strong></div>
    <div class="brief-tile"><span>Saved contracts</span><strong>${cell(data.saved_contract_count || counts.saved_contract || 0)}</strong></div>
    <div class="brief-tile"><span>Risk items</span><strong>${cell(data.risk_count || counts.position_risk || 0)}</strong></div>
  </div>`;
  const cards = rows.length
    ? `<div class="review-grid">${rows.slice(0, 6).map(todayReviewCard).join('')}</div>`
    : '<div class="empty">No urgent review items surfaced. Check the detailed panels or run a fresh scan.</div>';
  const tableRows = rows.length
    ? todayReviewTable(rows)
    : '<div class="empty">No today-review rows.</div>';
  const notes = (data.notes || []).length
    ? `<div class="brief-list" style="margin-top:10px"><h4>Notes</h4><ul>${data.notes.map(n => `<li>${escHtml(n)}</li>`).join('')}</ul></div>`
    : '';
  return `<div style="padding:12px">${tiles}${cards}<div class="brief-list" style="margin-top:10px"><h4>Full queue</h4>${tableRows}</div>${notes}</div>`;
}
function reviewPriorityClass(priority) {
  const p = Number(priority || 0);
  if (p >= 90) return 'hot';
  if (p >= 70) return 'warm';
  return 'cool';
}
function reviewCategoryClass(category) {
  return String(category || 'action_item').replace(/[^a-zA-Z0-9_-]/g, '_');
}
function todayReviewActionLabel(action, route) {
  if (action === 'scan_swing_chain') return 'Scan 3m+ chain';
  if (action === 'refresh_saved_quote') return 'Refresh quote';
  if (action === 'run_refresh_scan') return 'Start refresh';
  if (action === 'open_position_monitor' || route === 'positions') return 'Review position';
  if (route === 'paper') return 'Open paper queue';
  if (route === 'data_health') return 'Check health';
  return 'Open research';
}
function todayReviewCard(r) {
  const q = r.query || r.symbol || '';
  const category = reviewCategoryClass(r.category);
  const priorityClass = reviewPriorityClass(r.priority);
  return `<article class="review-card ${escAttr(category)}">
    <header>
      <div><h3>${cell(r.label || 'Review item')}</h3><small class="muted">${cell(r.symbol || r.query || r.source || '-')}</small></div>
      <span class="priority-badge ${priorityClass}">${cell(r.priority)}</span>
    </header>
    <div class="review-meta">
      <span class="pill">${cell(r.category || 'action')}</span>
      <span>${cell(r.source || '-')}</span>
    </div>
    <p>${cell(r.detail || '')}</p>
    <button class="btn today-review-action-btn" type="button" data-action="${escAttr(r.action || '')}" data-route="${escAttr(r.route || '')}" data-query="${escAttr(q)}" data-symbol="${escAttr(r.symbol || '')}">${escHtml(todayReviewActionLabel(r.action, r.route))}</button>
  </article>`;
}
function todayReviewTable(rows) {
  if (!rows || rows.length === 0) return '<div class="empty">No today-review rows.</div>';
  const body = rows.map(r => {
    const q = r.query || r.symbol || '';
    return `<tr>
      <td><button class="btn today-review-action-btn" type="button" data-action="${escAttr(r.action || '')}" data-route="${escAttr(r.route || '')}" data-query="${escAttr(q)}" data-symbol="${escAttr(r.symbol || '')}">Open</button></td>
      <td><strong>${cell(r.priority)}</strong></td>
      <td>${cell(r.category)}</td>
      <td>${cell(r.label)}</td>
      <td>${cell(r.detail)}</td>
      <td>${cell(r.action)}</td>
      <td>${cell(r.symbol || '-')}</td>
      <td>${cell(r.source || '-')}</td>
    </tr>`;
  }).join('');
  return `<div class="table-wrap"><table><thead><tr><th></th><th>Priority</th><th>Type</th><th>Item</th><th>Why</th><th>Action</th><th>Symbol</th><th>Source</th></tr></thead><tbody>${body}</tbody></table></div>`;
}
function actionQueueTable(rows) {
  if (!rows || rows.length === 0) return '<div class="empty">No action queue items.</div>';
  const body = rows.map(r => {
    const sym = r.query || r.symbol || '';
    const attrs = sym ? ` class="clickable-row" data-symbol="${escAttr(sym)}"` : '';
    return `<tr${attrs}><td><button class="btn queue-action-btn" type="button" data-action="${escAttr(r.action || '')}" data-query="${escAttr(r.query || r.symbol || '')}" data-symbol="${escAttr(r.symbol || '')}">Open</button></td><td><strong>${cell(r.priority)}</strong></td><td>${cell(r.category)}</td><td>${cell(r.label)}</td><td>${cell(r.detail)}</td><td>${cell(r.action)}</td><td>${cell(r.symbol || '-')}</td></tr>`;
  }).join('');
  return `<div class="table-wrap"><table><thead><tr><th></th><th>Priority</th><th>Category</th><th>Item</th><th>Detail</th><th>Action</th><th>Symbol</th></tr></thead><tbody>${body}</tbody></table></div>`;
}
function swingClimateHtml(data) {
  if (!data) return '<div class="empty">No swing climate available.</div>';
  const coverage = data.coverage || {};
  const positives = (data.positives || []).length
    ? `<ul>${data.positives.map(x => `<li>${escHtml(x)}</li>`).join('')}</ul>`
    : '<div class="empty">No supportive context surfaced.</div>';
  const warnings = (data.warnings || []).length
    ? `<ul>${data.warnings.map(x => `<li>${escHtml(x)}</li>`).join('')}</ul>`
    : '<div class="empty">No climate warnings surfaced.</div>';
  const focusRows = (data.focus || []).map(row => ({ label: row.label, detail: row.detail }));
  const tradeGates = data.trade_gates || [];
  const assetBias = data.asset_bias || [];
  const components = data.components || {};
  const optionsSentiment = data.options_sentiment || {};
  const tiles = `<div class="brief-grid">
    <div class="brief-tile"><span>Climate</span><strong>${cell(data.climate_label)}</strong></div>
    <div class="brief-tile"><span>Score</span><strong>${cell(data.climate_score)}/100</strong></div>
    <div class="brief-tile"><span>Posture</span><strong>${cell(data.posture)}</strong></div>
    <div class="brief-tile"><span>Market / breadth</span><strong>${cell(data.market_regime)} / ${cell(data.breadth_regime)}</strong></div>
    <div class="brief-tile"><span>Options sentiment</span><strong>${cell(data.options_sentiment_regime || optionsSentiment.regime || '-')}</strong></div>
    <div class="brief-tile"><span>P/C total/equity/index</span><strong>${cell(optionsSentiment.total_pc || '-')} / ${cell(optionsSentiment.equity_pc || '-')} / ${cell(optionsSentiment.index_pc || '-')}</strong></div>
    <div class="brief-tile"><span>Top group</span><strong>${cell(data.top_sector_symbol)} ${cell(data.top_sector)}</strong></div>
    <div class="brief-tile"><span>Weak group</span><strong>${cell(data.weak_sector_symbol)} ${cell(data.weak_sector)}</strong></div>
    <div class="brief-tile"><span>Coverage</span><strong>${cell(coverage.market)} | ${cell(coverage.breadth)} | ${cell(coverage.sector)} | ${cell(coverage.options_sentiment || '-')}</strong></div>
    <div class="brief-tile"><span>Components</span><strong>M ${cell(components.market)} / B ${cell(components.breadth)} / S ${cell(components.sector)} / O ${cell(components.options_sentiment || 0)}</strong></div>
  </div>`;
  return `<div style="padding:12px">
    ${tiles}
    <div class="brief-cols">
      <div class="brief-list"><h4>Supportive</h4>${positives}</div>
      <div class="brief-list"><h4>Warnings</h4>${warnings}</div>
    </div>
    <div class="brief-cols">
      <div class="brief-list"><h4>Trade gates</h4>${table(tradeGates)}</div>
      <div class="brief-list"><h4>Asset bias</h4>${table(assetBias)}</div>
    </div>
    <div class="brief-list" style="margin-top:10px"><h4>Review focus</h4>${table(focusRows)}</div>
  </div>`;
}
function marketPulseHtml(data) {
  if (!data) return '<div class="empty">No market pulse available.</div>';
  const optionsSentiment = data.options_sentiment || {};
  const warnings = (data.warnings && data.warnings.length)
    ? `<div class="brief-list" style="margin-top:10px"><h4>Provider warnings</h4><ul>${data.warnings.slice(0, 5).map(w => `<li>${escHtml(w)}</li>`).join('')}</ul></div>`
    : '';
  const tiles = `<div class="brief-grid">
    <div class="brief-tile"><span>Regime</span><strong>${cell(data.regime)}</strong></div>
    <div class="brief-tile"><span>Risk score</span><strong>${cell(data.risk_score)}</strong></div>
    <div class="brief-tile"><span>Options sentiment</span><strong>${cell(optionsSentiment.regime || '-')}</strong></div>
    <div class="brief-tile"><span>Total P/C</span><strong>${cell(optionsSentiment.total_pc || '-')}</strong></div>
    <div class="brief-tile"><span>Coverage</span><strong>${cell(data.coverage)}</strong></div>
    <div class="brief-tile"><span>Period</span><strong>${cell(data.period)}</strong></div>
  </div>`;
  return `<div style="padding:12px">
    ${tiles}${warnings}
    <div class="brief-list" style="margin-top:10px"><h4>Cboe options sentiment</h4>${table(optionsSentiment.rows || [])}</div>
    <div class="brief-cols">
      <div class="brief-list"><h4>Leaders 20d</h4>${table(data.leaders || [])}</div>
      <div class="brief-list"><h4>Laggards 20d</h4>${table(data.laggards || [])}</div>
    </div>
    <div class="brief-list" style="margin-top:10px"><h4>Full pulse</h4>${table(data.rows || [])}</div>
  </div>`;
}
function breadthPulseHtml(data) {
  if (!data) return '<div class="empty">No breadth pulse available.</div>';
  const warnings = (data.warnings && data.warnings.length)
    ? `<div class="brief-list" style="margin-top:10px"><h4>Provider warnings</h4><ul>${data.warnings.slice(0, 5).map(w => `<li>${escHtml(w)}</li>`).join('')}</ul></div>`
    : '';
  const tiles = `<div class="brief-grid">
    <div class="brief-tile"><span>Regime</span><strong>${cell(data.regime)}</strong></div>
    <div class="brief-tile"><span>Breadth score</span><strong>${cell(data.breadth_score)}</strong></div>
    <div class="brief-tile"><span>Coverage</span><strong>${cell(data.coverage)}</strong></div>
    <div class="brief-tile"><span>Supportive / warning</span><strong>${cell(data.supportive_count || 0)} / ${cell(data.warning_count || 0)}</strong></div>
  </div>`;
  return `<div style="padding:12px">
    ${tiles}${warnings}
    <div class="brief-cols">
      <div class="brief-list"><h4>Supportive checks</h4>${table(data.supportive || [])}</div>
      <div class="brief-list"><h4>Warnings</h4>${table(data.warnings_list || [])}</div>
    </div>
    <div class="brief-list" style="margin-top:10px"><h4>Full breadth map</h4>${table(data.rows || [])}</div>
  </div>`;
}
function sectorPulseHtml(data) {
  if (!data) return '<div class="empty">No sector pulse available.</div>';
  const leaders = data.leaders || [];
  const laggards = data.laggards || [];
  const top = leaders.length ? `${leaders[0].symbol} ${leaders[0].sector || ''}` : '-';
  const weak = laggards.length ? `${laggards[0].symbol} ${laggards[0].sector || ''}` : '-';
  const warnings = (data.warnings && data.warnings.length)
    ? `<div class="brief-list" style="margin-top:10px"><h4>Provider warnings</h4><ul>${data.warnings.slice(0, 5).map(w => `<li>${escHtml(w)}</li>`).join('')}</ul></div>`
    : '';
  const tiles = `<div class="brief-grid">
    <div class="brief-tile"><span>Coverage</span><strong>${cell(data.coverage)}</strong></div>
    <div class="brief-tile"><span>Period</span><strong>${cell(data.period)}</strong></div>
    <div class="brief-tile"><span>Top group</span><strong>${cell(top)}</strong></div>
    <div class="brief-tile"><span>Weakest group</span><strong>${cell(weak)}</strong></div>
  </div>`;
  return `<div style="padding:12px">
    ${tiles}${warnings}
    <div class="brief-cols">
      <div class="brief-list"><h4>Strongest groups</h4>${table(leaders)}</div>
      <div class="brief-list"><h4>Weakest groups</h4>${table(laggards)}</div>
    </div>
    <div class="brief-list" style="margin-top:10px"><h4>Full sector map</h4>${table(data.rows || [])}</div>
  </div>`;
}
function macroStressHtml(data) {
  if (!data) return '<div class="empty">No macro stress data available.</div>';
  const warnings = (data.warnings && data.warnings.length)
    ? `<div class="brief-list" style="margin-top:10px"><h4>Macro warnings</h4><ul>${data.warnings.slice(0, 6).map(w => `<li>${escHtml(w)}</li>`).join('')}</ul></div>`
    : '<div class="empty">No macro stress warnings surfaced.</div>';
  const tiles = `<div class="brief-grid">
    <div class="brief-tile"><span>Regime</span><strong>${cell(data.regime || '-')}</strong></div>
    <div class="brief-tile"><span>Stress score</span><strong>${cell(data.stress_score || 0)}/100</strong></div>
    <div class="brief-tile"><span>Coverage</span><strong>${cell(data.coverage || '-')}</strong></div>
    <div class="brief-tile"><span>Signals</span><strong>${countMapText(data.signal_counts || {})}</strong></div>
  </div>`;
  return `<div style="padding:12px">
    ${tiles}
    <div class="brief-list" style="margin-top:10px"><h4>Posture</h4><ul><li>${cell(data.posture || '-')}</li></ul></div>
    ${warnings}
    <div class="brief-list" style="margin-top:10px"><h4>FRED macro checks</h4>${table(data.rows || [], true)}</div>
  </div>`;
}
function countMapText(map) {
  if (!map || typeof map !== 'object') return '-';
  const entries = Object.entries(map).filter(([k, v]) => k && Number(v) > 0);
  return entries.length ? entries.map(([k, v]) => `${k}:${v}`).join(', ') : '-';
}
function opportunityQualityTable(health) {
  const quality = (health && health.opportunity_quality) || {};
  const rows = Object.values(quality).map(r => ({
    asset: r.asset,
    file: r.file || '-',
    rows: r.rows || 0,
    actionable_rows: r.actionable_rows || 0,
    duplicate_rows: r.duplicate_rows || 0,
    missing_columns: (r.missing_required_columns || []).join(', ') || '-',
    price_or_score: r.missing_price_or_score ? 'missing' : 'ok',
    quote_quality: countMapText(r.quote_quality_counts)
  }));
  if (!rows.length) return '<div class="empty">No opportunity quality audit available.</div>';
  return table(rows);
}
function riskSummaryHtml(risk) {
  if (!risk) return '<div class="empty">No risk summary available.</div>';
  const tiles = `<div class="brief-grid">
    <div class="brief-tile"><span>Risk level</span><strong>${cell(risk.risk_level)}</strong></div>
    <div class="brief-tile"><span>Total open</span><strong>${cell(risk.total_open)}</strong></div>
    <div class="brief-tile"><span>Needs attention</span><strong>${cell(risk.attention_count)}</strong></div>
    <div class="brief-tile"><span>High exit pressure</span><strong>${cell(risk.high_exit_pressure_count)}</strong></div>
    <div class="brief-tile"><span>Reprice trouble</span><strong>${cell(risk.reprice_trouble_count)}</strong></div>
    <div class="brief-tile"><span>Avg open P&amp;L</span><strong>${pct(risk.avg_open_pnl_pct)}</strong></div>
  </div>`;
  const warnings = (risk.warnings && risk.warnings.length)
    ? `<div class="brief-list" style="margin-top:10px"><h4>Warnings</h4><ul>${risk.warnings.map(w => `<li>${escHtml(w)}</li>`).join('')}</ul></div>`
    : '<div class="empty">No concentration or exit-pressure warnings surfaced.</div>';
  return `${tiles}${warnings}
    <div class="brief-cols">
      <div class="brief-list"><h4>Asset breakdown</h4>${table(risk.asset_breakdown || [])}</div>
      <div class="brief-list"><h4>Concentration</h4>${table(risk.concentration || [], true)}</div>
    </div>
    <div class="brief-cols">
      <div class="brief-list"><h4>Worst open P&amp;L</h4>${table(risk.worst_positions || [], true)}</div>
      <div class="brief-list"><h4>Highest exit pressure</h4>${table(risk.highest_exit_pressure || [], true)}</div>
    </div>`;
}
function exitReviewSummaryHtml(data) {
  if (!data) return '<div class="empty">No exit review summary available.</div>';
  const tiles = `<div class="brief-grid">
    <div class="brief-tile"><span>Reviews</span><strong>${cell(data.total_before_limit || data.count || 0)}</strong></div>
    <div class="brief-tile"><span>Current decisions</span><strong>${cell(data.current_decision_count || 0)}</strong></div>
    <div class="brief-tile"><span>High pressure</span><strong>${cell(data.high_pressure_count || 0)}</strong></div>
    <div class="brief-tile"><span>Current high pressure</span><strong>${cell(data.current_high_pressure_count || 0)}</strong></div>
    <div class="brief-tile"><span>Avg pressure</span><strong>${cell(data.avg_exit_pressure ?? '-')}</strong></div>
    <div class="brief-tile"><span>Learned policy</span><strong>${cell(data.learned_policy_count || 0)}</strong></div>
    <div class="brief-tile"><span>Current actions</span><strong>${cell(countMapText(data.current_action_counts || {}))}</strong></div>
    <div class="brief-tile"><span>Actions</span><strong>${cell(countMapText(data.action_counts || {}))}</strong></div>
    <div class="brief-tile"><span>Assets</span><strong>${cell(countMapText(data.asset_counts || {}))}</strong></div>
  </div>`;
  const empty = !data.rows || data.rows.length === 0;
  if (empty) {
    return `${tiles}<div class="empty">No exit-review rows matched this filter yet. Run a scan after lifecycle tracking opens positions.</div>`;
  }
  return `${tiles}
    <div class="brief-list" style="margin-top:10px"><h4>Current exit decisions</h4>${table(data.current_decisions || [], true)}</div>
    <div class="brief-cols">
      <div class="brief-list"><h4>Highest pressure by symbol</h4>${table(data.by_symbol || [], true)}</div>
      <div class="brief-list"><h4>Latest exit reviews</h4>${table(data.rows || [], true)}</div>
    </div>`;
}
function performanceSummaryHtml(perf) {
  if (!perf) return '<div class="empty">No performance summary available.</div>';
  const ram = perf.ram_cache || {};
  const finbert = perf.finbert || {};
  const warnings = (perf.warnings && perf.warnings.length)
    ? `<div class="brief-list" style="margin-top:10px"><h4>Warnings</h4><ul>${perf.warnings.map(w => `<li>${escHtml(w)}</li>`).join('')}</ul></div>`
    : '<div class="empty">No local performance warnings surfaced.</div>';
  const command = perf.recommended_command
    ? `<div class="brief-list" style="margin-top:10px"><h4>Fast loop command</h4><code>${escHtml(perf.recommended_command)}</code></div>`
    : '';
  const tiles = `<div class="brief-grid">
    <div class="brief-tile"><span>Latest engine seconds</span><strong>${cell(perf.total_latest_engine_sec)}</strong></div>
    <div class="brief-tile"><span>RAM cache</span><strong>${ram.ram_cache_enabled ? 'on' : 'off'}</strong></div>
    <div class="brief-tile"><span>RAM cache items</span><strong>${cell(ram.ram_cache_items || 0)}</strong></div>
    <div class="brief-tile"><span>FinBERT</span><strong>${escHtml(finbert.device || finbert.status || 'unknown')}</strong></div>
  </div>`;
  return `${tiles}${warnings}${command}
    <div class="brief-cols">
      <div class="brief-list"><h4>Latest slowest engines</h4>${table(perf.latest_slowest || [])}</div>
      <div class="brief-list"><h4>Rolling slowest engines</h4>${table(perf.rolling_slowest || [])}</div>
    </div>
    <div class="brief-cols">
      <div class="brief-list"><h4>Cache hit rates</h4>${table(perf.cache_prefixes || [])}</div>
      <div class="brief-list"><h4>Weakest engine health</h4>${table(perf.engine_health || [])}</div>
    </div>`;
}
function providerSummaryHtml(data) {
  const trust = data.data_trust || {};
  const fields = [
    ['Status', data.status || '-'],
    ['Data trust', trust.label || '-'],
    ['Trust score', trust.score ?? '-'],
    ['Symbol', data.symbol || '-'],
    ['History source', trust.history_source_summary || '-'],
    ['Option chain', trust.option_chain_status || '-'],
    ['Chain source', trust.option_chain_source || '-'],
    ['Chain rows', trust.option_chain_rows ?? '-'],
    ['Chain providers', trust.option_chain_provider_summary || '-'],
    ['SEC facts', `${trust.sec_companyfacts_status || '-'} / ${trust.sec_companyfacts_rows || 0}`],
    ['Working', `${data.ok_count || 0}/${data.provider_count || 0}`],
    ['Warnings', (data.warnings || []).length]
  ];
  const warning = trust.option_chain_warning
    ? `<div class="brief-list" style="margin-top:10px"><h4>Chain warning</h4><ul><li>${cell(trust.option_chain_warning)}</li></ul></div>`
    : '';
  return fields.map(([label, value]) => `<div class="brief-tile"><span>${escHtml(label)}</span><strong>${cell(value)}</strong></div>`).join('') + warning;
}
function freeSourcesSummaryHtml(data) {
  const fields = [
    ['Sources', data.source_count || 0],
    ['No-key', data.no_key_count || 0],
    ['Primary', data.primary_count || 0],
    ['Categories', countMapText(data.category_counts || {})],
    ['Quality', countMapText(data.quality_counts || {})],
    ['SEC cache', `${(data.sec_cache && data.sec_cache.status) || '-'} / ${(data.sec_cache && data.sec_cache.row_count) || 0}`],
    ['RAM cache', `${(data.ram_cache && data.ram_cache.ram_cache_items) || 0} item(s)`]
  ];
  return fields.map(([label, value]) => `<div class="brief-tile"><span>${escHtml(label)}</span><strong>${cell(value)}</strong></div>`).join('');
}
function freeSourcesTable(data) {
  const rows = (data.rows || []).map(r => ({
    source: r.name,
    category: r.category,
    coverage: r.coverage,
    credential: r.credential,
    quality: r.quality,
    primary: r.primary ? 'yes' : 'fallback',
    used_by: r.used_by,
    caveat: r.caveat,
    cache: r.local_cache_status ? `${r.local_cache_status} / ${r.local_cache_rows || 0}` : '-'
  }));
  return rows.length ? table(rows, true) : '<div class="empty">No source registry rows available.</div>';
}
function healthClass(level) {
  if (level === 'bad') return 'bad';
  if (level === 'warn') return 'warn';
  return 'good';
}
function healthTable(health) {
  const checks = (health && health.checks) || [];
  if (!checks.length) return '<div class="empty">No health checks available.</div>';
  const body = checks.map(c => `<tr><td><strong class="${healthClass(c.level)}">${cell(c.level)}</strong></td><td>${cell(c.label)}</td><td>${cell(c.detail)}</td></tr>`).join('');
  return `<div class="table-wrap"><table><thead><tr><th>Status</th><th>Check</th><th>Detail</th></tr></thead><tbody>${body}</tbody></table></div>
    <div class="brief-list" style="margin-top:10px"><h4>Opportunity quality</h4>${opportunityQualityTable(health)}</div>`;
}
function suggestionHtml(rows) {
  if (!rows || rows.length === 0) return '';
  return rows.slice(0, 10).map(r => `<button class="suggestion" type="button" data-query="${escAttr(r.query || r.symbol || '')}"><b>${cell(r.symbol)}</b><span>${cell(r.kind)} - ${cell(r.source)}</span></button>`).join('');
}
let suggestionTimer = null;
async function loadSuggestions(inputId, targetId, autoLookup=false) {
  const q = $(inputId).value.trim();
  const target = $(targetId);
  if (!target) return;
  if (q.length < 1) {
    target.innerHTML = '';
    return;
  }
  const res = await fetch('/api/suggestions?query=' + encodeURIComponent(q) + '&limit=10');
  const data = await res.json();
  target.innerHTML = suggestionHtml(data.rows || []);
  target.querySelectorAll('.suggestion').forEach(btn => {
    btn.addEventListener('click', async () => {
      $(inputId).value = btn.dataset.query || '';
      target.innerHTML = '';
      if (autoLookup) await lookup();
    });
  });
}
function scheduleSuggestions(inputId, targetId, autoLookup=false) {
  clearTimeout(suggestionTimer);
  suggestionTimer = setTimeout(() => {
    loadSuggestions(inputId, targetId, autoLookup).catch(() => {});
  }, 160);
}
function watchlistTable(rows) {
  if (!rows || rows.length === 0) return '<div class="empty">No saved research targets yet.</div>';
  const body = rows.map(r => {
    const request = r.request ? `${r.request.side || ''} ${r.request.expiry || ''} ${r.request.strike || ''}` : '';
    return `<tr>
      <td><button class="btn watch-lookup-btn" type="button" data-query="${escAttr(r.query || r.symbol || '')}">Lookup</button></td>
      <td><strong>${cell(r.symbol)}</strong></td>
      <td>${cell(r.query)}</td>
      <td>${cell(r.best_idea || '-')}</td>
      <td>${cell(r.best_status || '-')}</td>
      <td>${cell(r.best_confidence || '-')}</td>
      <td>${cell(r.paper_readiness_label || '-')}</td>
      <td>${cell(r.paper_readiness_score)}</td>
      <td>${cell(r.open_count || 0)}</td>
      <td>${pct(r.avg_unrealized_pct)}</td>
      <td>${cell(r.warning_count || 0)}</td>
      <td>${cell(request)}</td>
      <td><button class="btn watch-remove-btn" type="button" data-id="${escAttr(r.id)}">Remove</button></td>
    </tr>`;
  }).join('');
  return `<div class="table-wrap"><table><thead><tr>
    <th></th><th>Symbol</th><th>Query</th><th>Best local idea</th><th>Status</th><th>Conf</th><th>Readiness</th><th>Score</th><th>Open</th><th>Avg open P&amp;L</th><th>Warnings</th><th>Request</th><th></th>
  </tr></thead><tbody>${body}</tbody></table></div>`;
}
function secFilingsSummaryHtml(data) {
  const fields = [
    ['Symbols checked', data.symbols_checked || 0],
    ['Filings', data.filing_count || 0],
    ['Fresh <=14d', data.fresh_count || 0],
    ['High impact', data.high_impact_count || 0],
    ['Errors', data.error_count || 0],
    ['Forms', countMapText(data.form_counts || {})]
  ];
  return fields.map(([label, value]) => `<div class="brief-tile"><span>${escHtml(label)}</span><strong>${cell(value)}</strong></div>`).join('');
}
function secFilingsTable(rows) {
  if (!rows || rows.length === 0) return '<div class="empty">No important recent SEC filings found for saved watchlist symbols.</div>';
  const body = rows.map(r => {
    const secLink = r.url ? `<a class="btn" href="${escAttr(r.url)}" target="_blank">SEC</a>` : '';
    return `<tr>
      <td>
        <button class="btn sec-filing-lookup-btn" type="button" data-symbol="${escAttr(r.ticker || '')}">Lookup</button>
        ${secLink}
      </td>
      <td><strong>${cell(r.priority)}</strong></td>
      <td><strong>${cell(r.ticker)}</strong><br><small>${cell(r.company_name || '')}</small></td>
      <td>${cell(r.form)}</td>
      <td>${cell(r.filing_date)}</td>
      <td>${cell(r.days_old)}</td>
      <td>${cell(labelText(r.freshness))}</td>
      <td>${cell(labelText(r.signal))}</td>
      <td>${cell(r.description)}</td>
    </tr>`;
  }).join('');
  return `<div class="table-wrap"><table><thead><tr><th></th><th>Priority</th><th>Ticker</th><th>Form</th><th>Filed</th><th>Days</th><th>Freshness</th><th>Signal</th><th>Description</th></tr></thead><tbody>${body}</tbody></table></div>`;
}
function savedContractsSummary(data) {
  const status = data.status_counts || {};
  const quote = data.quote_status_counts || {};
  const review = data.review_action_counts || {};
  const grades = data.saved_grade_counts || {};
  const triage = data.triage_counts || {};
  const fields = [
    ['Saved contracts', data.count || 0],
    ['3m+ swing', data.swing_count || 0],
    ['Calls / puts', `${data.call_count || 0} / ${data.put_count || 0}`],
    ['Saved A / B', `${grades.A || 0} / ${grades.B || 0}`],
    ['Ready / shortlist', `${triage.ready_now || 0} / ${triage.shortlist || 0}`],
    ['Refresh quotes', triage.refresh_quote || 0],
    ['Review now', review.review_now || 0],
    ['Quotes checked', data.quote_checked_count || 0],
    ['Quote matched', quote.matched || 0],
    ['Ready review', status.ready_review || 0],
    ['Below 3m', status.below_3m || 0],
    ['Expired', status.expired || 0]
  ];
  return fields.map(([label, value]) => `<div class="brief-tile"><span>${escHtml(label)}</span><strong>${cell(value)}</strong></div>`).join('');
}
function savedContractTriageCards(rows) {
  const top = (rows || []).filter(r => !['expired', 'wait'].includes(String(r.triage_bucket || ''))).slice(0, 8);
  if (!top.length) return '<div class="empty">No saved contracts are ready for triage yet. Refresh quotes after saving A/B contracts from the chain sweep.</div>';
  return `<div class="setup-grid">${top.map(r => {
    const reasons = Array.isArray(r.triage_reasons) ? r.triage_reasons.join(', ') : (r.triage_reasons || '');
    return `<article class="setup-card">
      <header>
        <div><h3>${cell(r.symbol)} ${cell(r.side_code)} ${cell(r.strike)}</h3><small>${cell(r.expiry)} | ${cell(r.dte)} DTE</small></div>
        <span class="pill ${escAttr(r.triage_bucket || '')}">${cell(r.triage_label || r.triage_bucket)}</span>
      </header>
      <div class="row"><span>Triage score</span><b>${cell(r.triage_score)}</b></div>
      <div class="row"><span>Saved grade</span><b>${cell(r.saved_contract_grade || '-')} / ${cell(r.saved_review_lane || '-')}</b></div>
      <div class="row"><span>Quote</span><b>${cell(r.quote_status || 'not_checked')}</b></div>
      <div class="row"><span>Saved mid/spread</span><b>${cell(r.saved_mid)} / ${pct(r.saved_spread_pct)}</b></div>
      <div class="row"><span>Current mid/spread</span><b>${cell(r.current_mid)} / ${pct(r.current_spread_pct)}</b></div>
      <div class="row"><span>Why</span><b>${cell(reasons || r.saved_review_thesis || '-')}</b></div>
      <div class="row"><span>Request</span><b>${cell(r.query)}</b></div>
      <button class="btn saved-contract-lookup-btn" type="button" data-query="${escAttr(r.query || '')}">Lookup</button>
      <button class="btn saved-contract-chain-btn" type="button" data-symbol="${escAttr(r.symbol || '')}">Chain</button>
    </article>`;
  }).join('')}</div>`;
}
function savedContractsTable(rows) {
  if (!rows || rows.length === 0) return '<div class="empty">No saved option contracts yet. Save one from an option-chain card.</div>';
  const body = rows.map(r => `<tr>
    <td>
      <button class="btn saved-contract-lookup-btn" type="button" data-query="${escAttr(r.query || '')}">Lookup</button>
      <button class="btn saved-contract-chain-btn" type="button" data-symbol="${escAttr(r.symbol || '')}">Chain</button>
    </td>
    <td><strong>${cell(r.symbol)}</strong></td>
    <td>${cell(r.side_code)} ${cell(r.strike)}</td>
    <td>${cell(r.expiry)}</td>
    <td>${cell(r.dte)}</td>
    <td><strong>${cell(r.saved_contract_grade || '-')}</strong><br><small>${cell(r.saved_review_lane || '')}</small></td>
    <td>${cell(r.saved_review_thesis || '-')}</td>
    <td>${cell(r.review_action)}</td>
    <td>${cell(r.review_score)}</td>
    <td>${cell(Array.isArray(r.review_reasons) ? r.review_reasons.join(', ') : r.review_reasons)}</td>
    <td>${cell(r.status)}</td>
    <td>${cell(r.quote_status || 'not_checked')}</td>
    <td>${cell(r.saved_mid)} / ${pct(r.saved_spread_pct)}</td>
    <td>${cell(r.current_mid)}</td>
    <td>${pct(r.current_spread_pct)}</td>
    <td>${moneyShort(r.current_premium_dollars)}</td>
    <td>${cell(r.quote_readiness_label || '-')}</td>
    <td>${cell(r.quote_readiness_score)}</td>
    <td>${cell(r.paper_readiness || '-')}</td>
    <td>${cell(r.paper_readiness_score)}</td>
    <td>${cell(r.best_idea || '-')}</td>
    <td>${cell(r.open_count || 0)}</td>
    <td>${cell(r.warning_count || 0)}</td>
    <td>${cell(r.query)}</td>
  </tr>`).join('');
  return `<div class="table-wrap"><table><thead><tr>
    <th></th><th>Symbol</th><th>Side/strike</th><th>Expiry</th><th>DTE</th><th>Saved grade</th><th>Saved thesis</th><th>Action</th><th>Review score</th><th>Reasons</th><th>Status</th><th>Quote</th><th>Saved mid/spread</th><th>Mid</th><th>Spread</th><th>Premium</th><th>Quote ready</th><th>Quote score</th><th>Readiness</th><th>Score</th><th>Best local idea</th><th>Open</th><th>Warnings</th><th>Query</th>
  </tr></thead><tbody>${body}</tbody></table></div>`;
}
function wireClickableRows(root=document) {
  root.querySelectorAll('.clickable-row').forEach(row => {
    row.addEventListener('click', async () => {
      setView('research');
      $('symbol').value = row.dataset.symbol || '';
      await lookup();
      window.location.hash = 'lookup';
    });
  });
}
function canScanOptionChainSymbol(symbol, asset='') {
  const clean = String(symbol || '').trim().toUpperCase();
  const kind = String(asset || '').trim().toLowerCase();
  if (!clean || kind === 'futures') return false;
  if (clean.endsWith('=F') || clean.startsWith('^')) return false;
  return true;
}
function wireSetupCards(root=document) {
  root.querySelectorAll('.setup-lookup-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const symbol = btn.dataset.symbol || '';
      if (!symbol) return;
      setView('research');
      $('symbol').value = symbol;
      await lookup();
      window.location.hash = 'lookup';
    });
  });
  root.querySelectorAll('.setup-chain-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const symbol = btn.dataset.symbol || '';
      if (!symbol) return;
      setView('chains');
      $('chain-query').value = symbol;
      applyChainPreset('swing');
      window.location.hash = 'chains';
      await scanOptionChain();
    });
  });
  root.querySelectorAll('.setup-scan-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const symbol = btn.dataset.symbol || '';
      if (!symbol) return;
      setView('research');
      $('symbol').value = symbol;
      window.location.hash = 'lookup';
      await runSymbol();
      scrollToId('jobs');
    });
  });
}
function wireOptionChainActions(root=document) {
  root.querySelectorAll('.contract-watchlist-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const query = btn.dataset.query || '';
      if (!query) return;
      let context = {};
      try {
        context = JSON.parse(btn.dataset.context || '{}');
      } catch (err) {
        context = {};
      }
      btn.disabled = true;
      $('chain-status-text').textContent = `Saving ${query} to research watchlist...`;
      const res = await fetch('/api/watchlist-add', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ query, context })
      });
      const data = await res.json();
      btn.disabled = false;
      if (!res.ok || data.ok === false) {
        $('chain-status-text').textContent = 'Could not save contract: ' + (data.error || 'unknown error');
        return;
      }
      $('watchlist-query').value = '';
      $('chain-status-text').textContent = `${query} saved to research watchlist.`;
      await loadWatchlist();
      await loadSavedContracts();
    });
  });
}
function wireSavedContractRows() {
  document.querySelectorAll('.saved-contract-lookup-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const query = btn.dataset.query || '';
      if (!query) return;
      setView('research');
      $('symbol').value = query;
      await lookup();
      window.location.hash = 'lookup';
    });
  });
  document.querySelectorAll('.saved-contract-chain-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const symbol = btn.dataset.symbol || '';
      if (!symbol) return;
      setView('chains');
      $('chain-query').value = symbol;
      applyChainPreset('swing');
      window.location.hash = 'chains';
      await scanOptionChain();
    });
  });
}
function setView(view) {
  const target = view || 'overview';
  document.body.className = document.body.className
    .split(/\\s+/)
    .filter(c => c && !c.startsWith('view-'))
    .concat(['view-' + target])
    .join(' ');
  document.querySelectorAll('.view-tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.view === target);
  });
}
function scrollToId(id) {
  const el = $(id);
  const panel = el ? el.closest('[data-view]') : null;
  if (panel && panel.dataset.view) setView(panel.dataset.view);
  if (el) el.scrollIntoView({behavior:'smooth', block:'start'});
}
async function routeQueueAction(action, query, symbol) {
  const q = query || symbol || '';
  if (action === 'warm_sec_ticker_cache' || action === 'warm_symbol_caches') {
    $('queue-status-text').textContent = 'Warming free symbol search caches...';
    const res = await fetch('/api/warm-symbol-caches', {method: 'POST'});
    const data = await res.json();
    $('queue-status-text').textContent = data.message || (data.ok ? 'Symbol search caches warmed.' : 'Symbol cache warm failed.');
    await loadSummary();
    await loadActionQueue();
    scrollToId('health-results');
    return;
  }
  if (action === 'run_refresh_scan') {
    $('queue-status-text').textContent = 'Starting full market refresh...';
    const res = await fetch('/api/run-refresh-scan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mode: 'full'})
    });
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      $('queue-status-text').textContent = 'Refresh job failed to start: ' + (data.error || 'unknown error');
      return;
    }
    $('queue-status-text').textContent = `Started full refresh job ${data.job_id || ''}.`;
    await loadJobs();
    scrollToId('jobs');
    return;
  }
  if (action === 'refresh_or_fix_artifact' || action === 'review_data_health') {
    await loadSummary();
    scrollToId('health-results');
    return;
  }
  if (action === 'open_position_monitor') {
    $('positions-asset').value = 'all';
    $('positions-status').value = 'attention';
    $('positions-query').value = q;
    await loadPositions();
    scrollToId('positions-results');
    return;
  }
  if (action === 'preview_paper_candidate' || action === 'review_paper_export') {
    $('paper-dry-run').checked = false;
    $('paper-query').value = q || symbol || $('symbol').value.trim();
    await loadPaperCandidates(false);
    if (q) $('symbol').value = q;
    scrollToId('paper-results');
    return;
  }
  if (action === 'run_focused_scan') {
    setView('research');
    $('symbol').value = q;
    await runSymbol();
    scrollToId('jobs');
    return;
  }
  if (action === 'review_watchlist') {
    if (q) $('watchlist-query').value = q;
    await loadWatchlist();
    scrollToId('watchlist-results');
    return;
  }
  if (q) {
    $('symbol').value = q;
    await lookup();
    scrollToId('lookup-results');
  }
}
async function routeTodayReviewAction(action, route, query, symbol) {
  const q = query || symbol || '';
  if (action === 'run_refresh_scan') {
    await routeQueueAction(action, query, symbol);
    return;
  }
  if (action === 'scan_swing_chain' || route === 'chains') {
    setView('chains');
    if (symbol || q) $('chain-query').value = symbol || q;
    applyChainPreset('swing');
    window.location.hash = 'chains';
    if (action === 'refresh_saved_quote') {
      await loadSavedContracts(true);
      scrollToId('saved-contracts-results');
      return;
    }
    await scanOptionChain();
    return;
  }
  if (route === 'positions' || action === 'open_position_monitor') {
    $('positions-asset').value = 'all';
    $('positions-status').value = 'attention';
    $('positions-query').value = q;
    await loadPositions();
    scrollToId('positions-results');
    return;
  }
  if (route === 'paper') {
    $('paper-query').value = q;
    await loadPaperCandidates(false);
    scrollToId('paper-results');
    return;
  }
  if (route === 'data_health') {
    await loadSummary();
    scrollToId('health-results');
    return;
  }
  if (action === 'open_research' || route === 'research') {
    setView('research');
    $('symbol').value = q;
    await lookup();
    scrollToId('lookup-results');
    return;
  }
  await routeQueueAction(action, query, symbol);
}
function wireTodayReviewRows() {
  document.querySelectorAll('.today-review-action-btn').forEach(btn => {
    btn.addEventListener('click', async (event) => {
      event.stopPropagation();
      await routeTodayReviewAction(
        btn.dataset.action || '',
        btn.dataset.route || '',
        btn.dataset.query || '',
        btn.dataset.symbol || ''
      );
    });
  });
}
function wireCommandCenter() {
  document.querySelectorAll('.command-center-action-btn').forEach(btn => {
    btn.addEventListener('click', async (event) => {
      event.stopPropagation();
      await routeTodayReviewAction(
        btn.dataset.action || '',
        btn.dataset.route || '',
        btn.dataset.query || '',
        btn.dataset.symbol || ''
      );
    });
  });
}
function wireActionQueueRows() {
  document.querySelectorAll('.queue-action-btn').forEach(btn => {
    btn.addEventListener('click', async (event) => {
      event.stopPropagation();
      await routeQueueAction(btn.dataset.action || '', btn.dataset.query || '', btn.dataset.symbol || '');
    });
  });
}
function wireWatchlistRows() {
  document.querySelectorAll('.watch-lookup-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      setView('research');
      $('symbol').value = btn.dataset.query || '';
      await lookup();
    });
  });
  document.querySelectorAll('.watch-remove-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      await removeWatchlist(btn.dataset.id || '');
    });
  });
}
function wireSecFilingRows() {
  document.querySelectorAll('.sec-filing-lookup-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      setView('research');
      $('symbol').value = btn.dataset.symbol || '';
      await lookup();
    });
  });
}
function syncGlobalQueryTargets(query) {
  const q = String(query || '').trim();
  ['symbol', 'watchlist-query', 'chain-query', 'explorer-query', 'paper-query', 'rh-query', 'swing-scout-query', 'provider-query'].forEach(id => {
    const el = $(id);
    if (el) el.value = q;
  });
}
function globalCommandQuery() {
  const q = $('global-query').value.trim();
  syncGlobalQueryTargets(q);
  return q;
}
async function globalLookup() {
  const q = globalCommandQuery();
  if (!q) {
    $('global-status').textContent = 'Type a ticker, company, or option idea first.';
    return;
  }
  $('global-status').textContent = `Opening research for ${q}...`;
  setView('research');
  await lookup();
}
async function globalReviewWorkspace() {
  const q = globalCommandQuery();
  if (!q) {
    $('global-status').textContent = 'Type a ticker, company, or option idea first.';
    return;
  }
  $('global-status').textContent = `Building review workspace for ${q}...`;
  setView('research');
  applyChainPreset('swing');
  if ($('provider-chain')) $('provider-chain').checked = canScanOptionChainSymbol(q);
  if ($('chain-status-text')) {
    $('chain-status-text').textContent = canScanOptionChainSymbol(q)
      ? `Ready for a 3m+ option-chain scan on ${q}.`
      : `${q} does not look like an equity/ETF option-chain symbol.`;
  }
  const tasks = [
    ['research', lookup()],
    ['provider trust', loadProviderStatus()],
    ['paper preview', loadPaperCandidates(false)],
    ['opportunity explorer', loadExplorer()],
  ];
  const results = await Promise.allSettled(tasks.map(([, task]) => task));
  const failed = results
    .map((result, idx) => result.status === 'rejected' ? tasks[idx][0] : '')
    .filter(Boolean);
  $('global-status').textContent = failed.length
    ? `Review workspace loaded for ${q}; ${failed.join(', ')} needs a retry. Chain scan is staged, not auto-run.`
    : `Review workspace loaded for ${q}. Research, provider trust, paper preview, and explorer are ready; chain scan is staged.`;
  scrollToId('lookup-results');
}
async function globalRunScan() {
  const q = globalCommandQuery();
  if (!q) {
    $('global-status').textContent = 'Type a ticker, company, or option idea first.';
    return;
  }
  $('global-status').textContent = `Starting focused scan for ${q}...`;
  setView('research');
  await runSymbol();
}
async function globalScanChain() {
  const q = globalCommandQuery();
  if (!q) {
    $('global-status').textContent = 'Type an equity or ETF ticker/company first.';
    return;
  }
  $('global-status').textContent = `Scanning 3m+ option chain for ${q}...`;
  setView('chains');
  applyChainPreset('swing');
  await scanOptionChain();
}
async function globalSaveWatchlist() {
  const q = globalCommandQuery();
  if (!q) {
    $('global-status').textContent = 'Type a ticker, company, or option idea first.';
    return;
  }
  $('global-status').textContent = `Saving ${q} to the research watchlist...`;
  setView('research');
  await addWatchlist();
}
async function loadSummary() {
  const res = await fetch('/api/summary');
  const data = await res.json();
  $('asof').textContent = new Date(data.generated_at).toLocaleString();
  $('open-options').textContent = data.open_counts.options;
  $('open-shares').textContent = data.open_counts.shares;
  $('open-futures').textContent = data.open_counts.futures;
  $('total-open').textContent = data.total_open;
  $('data-health').textContent = (data.data_health && data.data_health.status) || '-';
  $('data-health').className = healthClass((data.data_health && data.data_health.status) || 'ok');
  $('health-results').innerHTML = healthTable(data.data_health);
  $('notes').innerHTML = (data.notes || []).map(n => `<li>${n}</li>`).join('');
}
async function loadCommandCenter() {
  $('command-center-status-text').textContent = 'Building command center...';
  const res = await fetch('/api/command-center');
  const data = await res.json();
  const health = labelText(data.data_health_status || 'unknown').toLowerCase();
  $('command-center-status-text').textContent = `${labelText(data.status || 'review')} with ${data.review_count || 0} review item(s), data health ${health}.`;
  $('command-center-results').innerHTML = commandCenterHtml(data);
  wireCommandCenter();
}
async function loadSwingPacket(write=false, refreshChains=false) {
  $('swing-packet-status-text').textContent = refreshChains ? 'Scanning 3m+ option chains and writing packet...' : write ? 'Writing swing packet files...' : 'Building swing packet...';
  const res = (write || refreshChains)
    ? await fetch('/api/build-swing-packet', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ refresh_chains: refreshChains, chain_symbols_limit: 6, chain_contracts_per_symbol: 4 })
      })
    : await fetch('/api/swing-packet');
  const data = await res.json();
  if (!res.ok || data.error) {
    $('swing-packet-status-text').textContent = 'Swing packet failed: ' + (data.error || 'unknown error');
    return;
  }
  const fileNote = data.wrote_files ? ` Files written: ${data.paths.markdown || data.paths.json || ''}` : '';
  const chainNote = data.chain_refresh && data.chain_refresh.attempted
    ? ` Chain scan: ${data.chain_refresh.row_count || 0} contract(s), ${data.chain_refresh.export_count || 0} exported.`
    : '';
  $('swing-packet-status-text').textContent = `${labelText(data.status || 'review')} packet built.${chainNote}${fileNote}`;
  $('swing-packet-results').innerHTML = swingPacketHtml(data);
  wireClickableRows($('swing-packet-results'));
  wireOptionChainActions($('swing-packet-results'));
  wireCommandCenter();
}
function jobClass(status) {
  if (status === 'completed') return 'good';
  if (status === 'failed') return 'bad';
  if (status === 'running') return 'warn';
  return '';
}
function jobHtml(job) {
  const dash = job.dashboard_path ? `<a class="btn" href="/job-dashboard?id=${encodeURIComponent(job.job_id)}" target="_blank">Dashboard</a>` : '';
  const lookup = job.lookup_html_path ? `<a class="btn" href="/job-lookup?id=${encodeURIComponent(job.job_id)}" target="_blank">Lookup</a>` : '';
  const match = job.request ? `<button class="btn job-match-btn" type="button" data-query="${escAttr(job.query)}">Match</button>` : '';
  const req = job.request_label ? ` | ${job.request_label}` : job.request ? ` | ${job.request.side} ${job.request.expiry} ${job.request.strike}` : '';
  const matchText = job.requested_match_quality ? ` | ${job.requested_match_quality} match` : (job.request && job.status === 'completed' ? ` | ${cell(job.requested_match_count || 0)} matches` : '');
  const mode = job.scan_mode ? ` | ${job.scan_mode}` : '';
  return `<div class="job"><div><code>${job.symbol || job.query}</code> <span class="${jobClass(job.status)}">${job.status}</span><small>${job.name || job.query || ''}${req}${matchText}${mode} ${job.updated_at || ''}</small></div><div>${dash}${lookup}${match}<button class="btn job-log-btn" type="button" data-job="${job.job_id}">Log</button></div></div>`;
}
async function loadJobs() {
  const res = await fetch('/api/jobs');
  const data = await res.json();
  $('jobs').innerHTML = (data.jobs || []).map(jobHtml).join('') || '<div class="empty">No focused scan jobs yet.</div>';
  document.querySelectorAll('.job-log-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const id = btn.dataset.job;
      const res = await fetch('/api/job-log?id=' + encodeURIComponent(id));
      const data = await res.json();
      $('job-log').textContent = (data.lines || []).join('\\n') || 'No log output yet.';
      $('job-log').classList.add('active');
    });
  });
  document.querySelectorAll('.job-match-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      setView('research');
      $('symbol').value = btn.dataset.query || '';
      await lookup();
    });
  });
}
async function loadExplorer() {
  $('explorer-status-text').textContent = 'Loading ranked opportunities...';
  const params = new URLSearchParams({
    asset: $('explorer-asset').value,
    status: $('explorer-status').value,
    query: $('explorer-query').value.trim(),
    min_confidence: $('explorer-confidence').value || '0',
    limit: '80'
  });
  const res = await fetch('/api/opportunities?' + params.toString());
  const data = await res.json();
  $('explorer-status-text').textContent = `${data.count || 0} latest local opportunity row(s).`;
  $('explorer-results').innerHTML = table(data.rows || [], true);
  wireClickableRows($('explorer-results'));
}
async function loadBestSetups() {
  $('best-setups-status-text').textContent = 'Loading best local setups...';
  const res = await fetch('/api/best-setups?per_asset=3&limit=12');
  const data = await res.json();
  $('best-setups-status-text').textContent = `${data.count || 0} highlighted setup(s) from latest local snapshots.`;
  $('best-setups-results').innerHTML = bestSetupsHtml(data);
  wireClickableRows($('best-setups-results'));
  wireSetupCards($('best-setups-results'));
}
async function loadSwingScout() {
  $('swing-scout-status-text').textContent = 'Scouting high-upside local swing candidates...';
  const params = new URLSearchParams({
    limit: '18',
    asset: $('swing-scout-asset').value,
    lane: $('swing-scout-lane').value,
    query: $('swing-scout-query').value.trim(),
    min_score: $('swing-scout-min-score').value || '45',
    include_wait: $('swing-scout-hide-wait').checked ? 'false' : 'true',
    include_nasdaq_movers: $('swing-scout-nasdaq').checked ? 'true' : 'false'
  });
  const res = await fetch('/api/swing-scout?' + params.toString());
  const data = await res.json();
  const reviewed = data.reviewed_count === undefined ? '' : ` from ${data.reviewed_count} reviewed row(s)`;
  $('swing-scout-status-text').textContent = `${data.count || 0} scout candidate(s)${reviewed}; options require ${data.min_option_dte || 90}+ DTE.`;
  $('swing-scout-results').innerHTML = swingScoutHtml(data);
  wireClickableRows($('swing-scout-results'));
  wireSetupCards($('swing-scout-results'));
}
async function loadClimateGatedSetups() {
  $('climate-gated-status-text').textContent = 'Checking setups against swing climate gates...';
  const res = await fetch('/api/climate-gated-setups?per_asset=4&limit=12&include_held=true');
  const data = await res.json();
  $('climate-gated-status-text').textContent = `${data.selected_count || 0} passed, ${data.held_count || 0} held under ${data.climate_label || 'unknown'} climate.`;
  $('climate-gated-results').innerHTML = climateGatedSetupsHtml(data);
  wireClickableRows($('climate-gated-results'));
  wireSetupCards($('climate-gated-results'));
}
async function loadActionQueue() {
  $('queue-status-text').textContent = 'Building local action queue...';
  const res = await fetch('/api/action-queue?limit=20');
  const data = await res.json();
  $('queue-status-text').textContent = `${data.count || 0} prioritized local action item(s).`;
  $('queue-results').innerHTML = actionQueueTable(data.rows || []);
  wireClickableRows($('queue-results'));
  wireActionQueueRows();
}
async function loadTodayReview() {
  $('today-review-status-text').textContent = 'Building today review queue...';
  const res = await fetch('/api/today-review?limit=12');
  const data = await res.json();
  $('today-review-status-text').textContent = `${data.count || 0} priority item(s) from setups, saved contracts, risk, and data health.`;
  $('today-review-results').innerHTML = todayReviewHtml(data);
  wireTodayReviewRows();
}
async function loadSwingClimate() {
  $('swing-climate-status-text').textContent = 'Loading swing climate...';
  const res = await fetch('/api/swing-climate');
  const data = await res.json();
  const warnings = (data.warnings || []).length ? ` ${data.warnings.length} warning(s).` : '';
  $('swing-climate-status-text').textContent = `${data.climate_label || 'unknown'} at ${data.climate_score || 0}/100.${warnings}`;
  $('swing-climate-results').innerHTML = swingClimateHtml(data);
}
async function loadMarketPulse() {
  $('market-pulse-status-text').textContent = 'Loading free market context...';
  const res = await fetch('/api/market-pulse');
  const data = await res.json();
  const warningText = (data.warnings || []).length ? ` ${data.warnings.length} provider warning(s).` : '';
  $('market-pulse-status-text').textContent = `Regime: ${data.regime || 'unknown'}; coverage ${data.coverage || '0/0'}.${warningText}`;
  $('market-pulse-results').innerHTML = marketPulseHtml(data);
}
async function loadBreadthPulse() {
  $('breadth-pulse-status-text').textContent = 'Loading free breadth context...';
  const res = await fetch('/api/breadth-pulse');
  const data = await res.json();
  const warningText = (data.warnings || []).length ? ` ${data.warnings.length} provider warning(s).` : '';
  $('breadth-pulse-status-text').textContent = `Regime: ${data.regime || 'unknown'}; coverage ${data.coverage || '0/0'}.${warningText}`;
  $('breadth-pulse-results').innerHTML = breadthPulseHtml(data);
}
async function loadSectorPulse() {
  $('sector-pulse-status-text').textContent = 'Loading free sector context...';
  const res = await fetch('/api/sector-pulse');
  const data = await res.json();
  const warningText = (data.warnings || []).length ? ` ${data.warnings.length} provider warning(s).` : '';
  const top = (data.leaders && data.leaders[0]) ? `${data.leaders[0].symbol} ${data.leaders[0].sector || ''}` : 'unknown';
  $('sector-pulse-status-text').textContent = `Coverage ${data.coverage || '0/0'}; strongest ${top}.${warningText}`;
  $('sector-pulse-results').innerHTML = sectorPulseHtml(data);
}
async function loadMacroStress() {
  $('macro-stress-status-text').textContent = 'Loading keyless FRED macro stress...';
  const res = await fetch('/api/macro-stress');
  const data = await res.json();
  const warningText = (data.warnings || []).length ? ` ${data.warnings.length} warning(s).` : '';
  $('macro-stress-status-text').textContent = `${data.regime || 'unknown'} at ${data.stress_score || 0}/100; coverage ${data.coverage || '0/0'}.${warningText}`;
  $('macro-stress-results').innerHTML = macroStressHtml(data);
}
async function loadRiskSummary() {
  $('risk-status-text').textContent = 'Loading portfolio risk...';
  const res = await fetch('/api/risk-summary');
  const data = await res.json();
  $('risk-status-text').textContent = `Risk level: ${data.risk_level || 'unknown'} across ${data.total_open || 0} open position(s).`;
  $('risk-results').innerHTML = riskSummaryHtml(data);
  wireClickableRows($('risk-results'));
}
async function loadExitReviews() {
  $('exit-review-status-text').textContent = 'Loading exit reviews...';
  const params = new URLSearchParams({
    asset: $('positions-asset') ? $('positions-asset').value : 'all',
    query: $('positions-query') ? $('positions-query').value.trim() : '',
    limit: '80'
  });
  const res = await fetch('/api/exit-reviews?' + params.toString());
  const data = await res.json();
  $('exit-review-status-text').textContent = `${data.total_before_limit || 0} exit review row(s); latest ${data.latest_timestamp || 'none'}.`;
  $('exit-review-results').innerHTML = exitReviewSummaryHtml(data);
  wireClickableRows($('exit-review-results'));
}
async function loadPerformanceSummary() {
  $('performance-status-text').textContent = 'Loading performance telemetry...';
  const res = await fetch('/api/performance-summary');
  const data = await res.json();
  const ram = data.ram_cache || {};
  $('performance-status-text').textContent = `RAM cache ${ram.ram_cache_enabled ? 'on' : 'off'}; latest engine seconds ${data.total_latest_engine_sec || 0}.`;
  $('performance-results').innerHTML = performanceSummaryHtml(data);
}
async function loadProviderStatus() {
  $('provider-status-text').textContent = 'Checking free providers...';
  const params = new URLSearchParams({
    query: $('provider-query').value.trim() || 'AAPL',
    include_chain: $('provider-chain').checked ? 'true' : 'false'
  });
  const res = await fetch('/api/provider-status?' + params.toString());
  const data = await res.json();
  if (!res.ok || data.error) {
    $('provider-status-text').textContent = 'Provider check failed: ' + (data.error || 'unknown error');
    return;
  }
  const warningText = (data.warnings || []).length ? ` ${data.warnings.length} warning(s).` : '';
  $('provider-status-text').textContent = `${data.ok_count || 0}/${data.provider_count || 0} provider checks usable.${warningText}`;
  $('provider-summary').innerHTML = providerSummaryHtml(data);
  $('provider-results').innerHTML = table(data.rows || []);
  $('provider-results').dataset.loaded = '1';
}
async function loadFreeDataSources() {
  $('free-sources-status-text').textContent = 'Loading free source map...';
  const res = await fetch('/api/free-data-sources');
  const data = await res.json();
  $('free-sources-status-text').textContent = `${data.no_key_count || 0}/${data.source_count || 0} source(s) require no key.`;
  $('free-sources-summary').innerHTML = freeSourcesSummaryHtml(data);
  $('free-sources-results').innerHTML = freeSourcesTable(data);
}
async function loadWatchlist() {
  const res = await fetch('/api/watchlist?enrich=1');
  const data = await res.json();
  const suffix = data.enriched ? ' with latest local context' : '';
  $('watchlist-status-text').textContent = `${data.count || 0} saved research target(s)${suffix}.`;
  $('watchlist-results').innerHTML = watchlistTable(data.entries || []);
  wireWatchlistRows();
}
async function loadWatchlistSecFilings() {
  $('sec-filings-status-text').textContent = 'Checking SEC filings for saved watchlist symbols...';
  const res = await fetch('/api/watchlist-sec-filings?limit=40');
  const data = await res.json();
  $('sec-filings-status-text').textContent = `${data.filing_count || 0} important filing(s) across ${data.symbols_checked || 0} saved symbol(s).`;
  $('sec-filings-summary').innerHTML = secFilingsSummaryHtml(data);
  $('sec-filings-results').innerHTML = secFilingsTable(data.rows || []);
  $('sec-filings-results').dataset.loaded = '1';
  wireSecFilingRows();
}
async function loadSavedContracts(refreshQuotes=false) {
  $('saved-contracts-status-text').textContent = refreshQuotes ? 'Refreshing saved contract quotes...' : 'Loading saved option contracts...';
  const params = new URLSearchParams({
    enrich: '1',
    limit: '80',
    refresh_quotes: refreshQuotes ? '1' : '0',
    quote_limit: '20'
  });
  const res = await fetch('/api/saved-option-contracts?' + params.toString());
  const data = await res.json();
  const quoteText = refreshQuotes ? ` ${data.quote_checked_count || 0} quote(s) checked.` : '';
  $('saved-contracts-status-text').textContent = `${data.count || 0} saved option contract(s), ${data.swing_count || 0} at 3m+ DTE.${quoteText}`;
  $('saved-contracts-summary').innerHTML = savedContractsSummary(data);
  $('saved-contracts-triage').innerHTML = savedContractTriageCards(data.rows || []);
  $('saved-contracts-results').innerHTML = savedContractsTable(data.rows || []);
  wireSavedContractRows();
}
async function addWatchlist() {
  const query = $('watchlist-query').value.trim() || $('symbol').value.trim();
  if (!query) return;
  $('watchlist-status-text').textContent = 'Resolving and saving watchlist target...';
  const res = await fetch('/api/watchlist-add', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ query })
  });
  const data = await res.json();
  if (!res.ok || data.ok === false) {
    $('watchlist-status-text').textContent = 'Could not add: ' + (data.error || 'unknown error');
    return;
  }
  $('watchlist-query').value = '';
  $('watchlist-status-text').textContent = `${data.entry.symbol} saved to watchlist.`;
  await loadWatchlist();
  await loadWatchlistSecFilings();
  await loadSavedContracts();
}
async function removeWatchlist(id) {
  if (!id) return;
  const res = await fetch('/api/watchlist-remove', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ id })
  });
  const data = await res.json();
  $('watchlist-status-text').textContent = data.removed ? 'Removed watchlist target.' : 'Target was not found.';
  await loadWatchlist();
  await loadWatchlistSecFilings();
  await loadSavedContracts();
}
async function runWatchlist() {
  $('watchlist-status-text').textContent = 'Starting focused scans for saved targets...';
  const res = await fetch('/api/watchlist-run', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      mode: $('watchlist-scan-mode').value,
      bankroll: $('watchlist-bankroll').value,
      aggressive: $('watchlist-aggressive').checked
    })
  });
  const data = await res.json();
  if (!res.ok || data.ok === false) {
    $('watchlist-status-text').textContent = 'Could not run watchlist: ' + (data.error || 'unknown error');
    return;
  }
  $('watchlist-status-text').textContent = `Started ${data.count || 0} focused scan job(s).`;
  await loadJobs();
}
async function loadPaperCandidates(write=false) {
  $('paper-status-text').textContent = write ? 'Writing export files...' : 'Building paper candidate preview...';
  const dryRun = $('paper-dry-run').checked;
  const payload = {
    asset: $('paper-asset').value,
    max_new: $('paper-max-new').value || 5,
    max_open: $('paper-max-open').value || 30,
    query: $('paper-query').value.trim(),
    include_watch: $('paper-include-watch').checked,
    allow_zero_size_placeholder: $('paper-zero-size').checked,
    dry_run: dryRun
  };
  const res = write
    ? await fetch('/api/export-paper', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      })
    : await fetch('/api/paper-candidates?' + new URLSearchParams(payload).toString());
  const data = await res.json();
  if (!res.ok || data.error) {
    $('paper-status-text').textContent = 'Paper candidate export failed: ' + (data.error || 'unknown error');
    return;
  }
  const fileNote = data.wrote_files ? ` Files written: ${data.paths.csv || ''}` : '';
  const dryNote = data.dry_run ? `, ${data.excluded_count || 0} excluded rows reviewed` : '';
  $('paper-status-text').textContent = `${data.selected_count || 0} selected candidate(s)${dryNote}.${fileNote}`;
  if ($('paper-summary')) $('paper-summary').innerHTML = paperCandidateSummary(data);
  $('paper-results').innerHTML = table(data.rows || [], true);
  wireClickableRows($('paper-results'));
}
function paperCandidateSummary(data) {
  const fields = [
    ['Selected', data.selected_count || 0],
    ['Excluded', data.excluded_count || 0],
    ['Reviewed rows', data.count || 0],
    ['Asset', data.asset || 'all'],
    ['Dry run', data.dry_run ? 'yes' : 'no'],
    ['Top rejects', countMapText(data.rejection_reason_counts || {})]
  ];
  return fields.map(([label, value]) => `<div class="brief-tile"><span>${escHtml(label)}</span><strong>${cell(value)}</strong></div>`).join('');
}
function robinhoodQueueSummary(data) {
  const readiness = data.readiness || {};
  const fields = [
    ['Status', data.status || '-'],
    ['Readiness', readiness.label || data.status || '-'],
    ['Candidates', data.candidate_count || 0],
    ['Rejected', data.rejected_count || 0],
    ['Submit cap', readiness.ready_to_submit_count ?? data.max_orders_to_submit ?? 0],
    ['Max orders', data.max_orders_to_submit || 0],
    ['Min DTE', data.min_dte || 0],
    ['Budget', '$' + (data.account_budget || 0)],
    ['Max premium', '$' + (data.max_total_premium || 0)],
    ['Premium left', '$' + (readiness.premium_cap_remaining ?? '-')],
    ['Top rejects', countMapText(data.rejection_reason_counts || readiness.rejection_reason_counts || {})]
  ];
  return fields.map(([label, value]) => `<div class="brief-tile"><span>${escHtml(label)}</span><strong>${cell(value)}</strong></div>`).join('');
}
async function loadRobinhoodQueue(write=false) {
  $('rh-status-text').textContent = write ? 'Writing agentic queue files...' : 'Building agentic options queue...';
  const payload = {
    account_budget: $('rh-budget').value || 500,
    max_candidates: $('rh-max-candidates').value || 5,
    max_orders: $('rh-max-orders').value || 2,
    min_dte: $('rh-min-dte').value || 180,
    min_confidence: $('rh-min-confidence').value || 55,
    query: $('rh-query').value.trim()
  };
  const res = write
    ? await fetch('/api/build-robinhood-queue', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      })
    : await fetch('/api/robinhood-queue?' + new URLSearchParams(payload).toString());
  const data = await res.json();
  if (!res.ok || data.error) {
    $('rh-status-text').textContent = 'Agentic queue failed: ' + (data.error || 'unknown error');
    return;
  }
  const fileNote = data.wrote_files ? ` Files written: ${data.paths.queue || ''}` : '';
  $('rh-status-text').textContent = `${data.candidate_count || 0} candidate(s), ${data.rejected_count || 0} rejected.${fileNote}`;
  $('rh-summary').innerHTML = robinhoodQueueSummary(data);
  $('rh-results').innerHTML = table(data.orders || [], true);
  $('rh-rejected').innerHTML = table(data.rejected || [], true);
  wireClickableRows($('rh-results'));
  wireClickableRows($('rh-rejected'));
}
function applyChainPreset(preset) {
  const configs = {
    custom: null,
    swing: { side: 'all', minDte: 90, maxDte: 180, maxSpread: 20, maxPremium: 500, minOi: 25 },
    leaps: { side: 'all', minDte: 180, maxDte: 900, maxSpread: 25, maxPremium: 750, minOi: 10 },
    liquid: { side: 'all', minDte: 90, maxDte: 365, maxSpread: 12, maxPremium: 0, minOi: 100 }
  };
  const name = configs[preset] === undefined ? 'custom' : preset;
  $('chain-preset').value = name;
  document.querySelectorAll('.chain-preset').forEach(btn => btn.classList.toggle('active', btn.dataset.preset === name));
  const cfg = configs[name];
  if (!cfg) return;
  $('chain-side').value = cfg.side;
  $('chain-min-dte').value = cfg.minDte;
  $('chain-max-dte').value = cfg.maxDte;
  $('chain-max-spread').value = cfg.maxSpread;
  $('chain-max-premium').value = cfg.maxPremium;
  $('chain-min-oi').value = cfg.minOi;
}
function optionChainSummary(data) {
  const filters = data.filters || {};
  const scan = data.scan_summary || {};
  const decision = data.decision || {};
  const primary = decision.primary || {};
  const tradePlan = decision.trade_plan || {};
  const exposure = data.open_exposure || decision.open_exposure || {};
  const fields = [
    ['Symbol', data.symbol || '-'],
    ['Preset', data.preset_label || data.preset || '-'],
    ['Source', data.source || '-'],
    ['Quality', data.quote_quality || '-'],
    ['Delay', data.data_delay || '-'],
    ['Providers checked', data.providers_checked || 0],
    ['Provider trail', (data.source_attempts || []).map(r => r.provider || r.source).filter(Boolean).join(' -> ') || '-'],
    ['Spot', data.spot || '-'],
    ['Expirations', data.total_expirations || 0],
    ['Contracts', data.total_contracts || 0],
    ['Shown', data.filtered_count || 0],
    ['Rejected', data.rejected_count || 0],
    ['Decision', `${decision.label || '-'} ${decision.status || ''}`.trim()],
    ['Primary contract', primary.contract_query || '-'],
    ['Trade action', labelText(tradePlan.action || 'watch_only')],
    ['Open exposure', exposure.has_open ? `${exposure.open_count || 0} open` : 'clear'],
    ['A/B saveable', decision.saveable_count || 0],
    ['Top rejects', countMapText(data.rejection_reason_counts || {})],
    ['Median spread', scan.median_spread_pct === null || scan.median_spread_pct === undefined ? '-' : ((Number(scan.median_spread_pct || 0) * 100).toFixed(1)) + '%'],
    ['Under budget', scan.under_budget_count || 0],
    ['Liquid', scan.liquid_count || 0],
    ['Grades', countMapText(scan.grade_counts)],
    ['Primary review', scan.primary_review_count || 0],
    ['3m+ swing', scan.swing_count || 0],
    ['Long dated', scan.long_dated_count || 0],
    ['Ready', scan.ready_count || 0],
    ['Review', scan.review_count || 0],
    ['Wait', scan.wait_count || 0],
    ['Swing fit', countMapText(scan.swing_fit_counts || {})],
    ['Clean swings', scan.clean_swing_count || 0],
    ['Reviewable swings', scan.reviewable_swing_count || 0],
    ['Best swing fit', scan.best_swing_fit || '-'],
    ['Best call', scan.best_call || '-'],
    ['Best put', scan.best_put || '-'],
    ['Best reviewable', scan.best_reviewable || '-'],
    ['Best budget', scan.best_budget || '-'],
    ['Best liquid', scan.best_liquid || '-'],
    ['Best long-dated', scan.best_long_dated || '-'],
    ['Max spread', ((Number(filters.max_spread_pct || 0) * 100).toFixed(0)) + '%']
  ];
  return fields.map(([label, value]) => `<div class="brief-tile"><span>${escHtml(label)}</span><strong>${cell(value)}</strong></div>`).join('');
}
function optionContractContext(row) {
  return {
    source: 'option_chain_scan',
    chain_source: row.chain_source || row.batch_source || '',
    quote_quality: row.quote_quality || row.batch_quote_quality || '',
    data_delay: row.data_delay || row.batch_data_delay || '',
    scan_symbol: row.symbol || row.ticker || row.ticker_or_symbol || '',
    scan_preset: $('chain-preset') ? $('chain-preset').value : '',
    contract_grade: row.contract_grade || '',
    review_lane: row.review_lane || '',
    review_thesis: row.review_thesis || '',
    grade_reasons: Array.isArray(row.grade_reasons) ? row.grade_reasons.slice(0, 8) : [],
    readiness_label: row.readiness_label || '',
    readiness_score: row.readiness_score,
    risk_flags: Array.isArray(row.risk_flags) ? row.risk_flags.slice(0, 8) : [],
    contract_quality_score: row.contract_quality_score,
    swing_fit_score: row.swing_fit_score,
    swing_fit_label: row.swing_fit_label,
    chain_factor_summary: row.chain_factor_summary,
    chain_factor_breakdown: Array.isArray(row.chain_factor_breakdown) ? row.chain_factor_breakdown.slice(0, 8) : [],
    swing_fit_reasons: Array.isArray(row.swing_fit_reasons) ? row.swing_fit_reasons.slice(0, 8) : [],
    swing_fit_warnings: Array.isArray(row.swing_fit_warnings) ? row.swing_fit_warnings.slice(0, 8) : [],
    breakeven_move_label: row.breakeven_move_label,
    liquidity_label: row.liquidity_label,
    bid: row.bid,
    ask: row.ask,
    mid: row.mid,
    spread_pct: row.spread_pct,
    premium_dollars: row.premium_dollars ?? row.actual_dollars,
    volume: row.volume,
    openInterest: row.openInterest,
    impliedVolatility: row.impliedVolatility,
    delta: row.delta,
    moneyness_pct: row.moneyness_pct,
    breakeven_price: row.breakeven_price,
    breakeven_move_pct: row.breakeven_move_pct,
    breakeven_direction: row.breakeven_direction,
    budget_usage_pct: row.budget_usage_pct,
    contracts_for_budget: row.contracts_for_budget,
    stop_price_reference: row.stop_price_reference,
    target_price_reference: row.target_price_reference,
    risk_dollars_reference: row.risk_dollars_reference,
    reward_dollars_reference: row.reward_dollars_reference,
    reward_risk_reference: row.reward_risk_reference,
    budget_fit: row.budget_fit,
    dte: row.dte,
    dte_bucket: row.dte_bucket,
    open_exposure_count: row.open_exposure_count || 0,
    open_exposure_assets: row.open_exposure_assets || '',
    open_exposure_summary: row.open_exposure_summary || '',
    open_exposure_attention_count: row.open_exposure_attention_count || 0
  };
}
function optionContractSavePayload(row) {
  const query = row.contract_query || row.contract || optionContractQuery(row);
  return query ? { query, context: optionContractContext(row) } : null;
}
function bestChainBatchSaveRows(rows) {
  const good = (rows || []).filter(row => {
    const grade = String(row.contract_grade || '').toUpperCase();
    const lane = String(row.review_lane || '').toLowerCase();
    return optionContractSavePayload(row) && ['A', 'B'].includes(grade) && lane !== 'wait';
  });
  return (good.length ? good : (rows || []).filter(row => optionContractSavePayload(row))).slice(0, 8);
}
function optionContractCard(row) {
  const readiness = row.readiness_label || 'review';
  const flags = Array.isArray(row.risk_flags) ? row.risk_flags.join(', ') : (row.risk_flags || '');
  const gradeReasons = Array.isArray(row.grade_reasons) ? row.grade_reasons.join(', ') : (row.grade_reasons || '');
  const swingReasons = Array.isArray(row.swing_fit_reasons) ? row.swing_fit_reasons.join(', ') : (row.swing_fit_reasons || '');
  const swingWarnings = Array.isArray(row.swing_fit_warnings) ? row.swing_fit_warnings.join(', ') : (row.swing_fit_warnings || '');
  const breakdown = Array.isArray(row.chain_factor_breakdown) ? row.chain_factor_breakdown.slice(0, 5) : [];
  const factorRows = breakdown.length
    ? `<div class="factor-stack">${breakdown.map(f => `<div class="factor-row">
        <strong>${cell(f.factor || '-')}</strong>
        <span>${cell(f.detail || '')}</span>
        <em>${cell(f.score)}</em>
      </div>`).join('')}</div>`
    : '';
  const title = `${row.symbol || row.ticker || ''} ${(row.side || '').toUpperCase()} ${row.strike || ''}`;
  const query = row.contract_query || row.contract || optionContractQuery(row);
  const context = optionContractContext(row);
  const premium = row.premium_dollars ?? row.actual_dollars;
  const stopRef = row.stop_price_reference ?? row.stop_price;
  const targetRef = row.target_price_reference ?? row.target_price;
  return `<article class="setup-card">
    <header>
      <div><h3>${cell(title)}</h3><small>${cell(row.expiry)} | ${cell(row.dte)} DTE | ${cell(row.dte_bucket)}</small></div>
      <span class="pill ${escAttr(readiness)}">${cell(row.contract_grade || readiness)}</span>
    </header>
    <div class="row"><span>Grade / lane</span><b>${cell(row.contract_grade)} / ${cell(row.review_lane)}</b></div>
    <div class="row"><span>Mid / premium</span><b>${cell(row.mid)} / ${moneyShort(premium)}</b></div>
    <div class="row"><span>Break-even</span><b>${cell(row.breakeven_price)} (${cell(row.breakeven_direction)} ${pct(row.breakeven_move_pct)})</b></div>
    <div class="row"><span>Budget fit</span><b>${cell(labelText(row.budget_fit))} / ${pct(row.budget_usage_pct)}</b></div>
    <div class="row"><span>Spread</span><b>${pct(row.spread_pct)}</b></div>
    <div class="row"><span>Open interest</span><b>${cell(row.openInterest)}</b></div>
    <div class="row"><span>Volume</span><b>${cell(row.volume)}</b></div>
    <div class="row"><span>Delta</span><b>${cell(row.delta)}</b></div>
    <div class="row"><span>Open exposure</span><b>${Number(row.open_exposure_count || 0) > 0 ? cell(row.open_exposure_summary || `${row.open_exposure_count} open`) : 'clear'}</b></div>
    <div class="row"><span>Stop / target ref</span><b>${cell(stopRef)} / ${cell(targetRef)}</b></div>
    <div class="row"><span>Risk / reward ref</span><b>${moneyShort(row.risk_dollars_reference)} / ${moneyShort(row.reward_dollars_reference)} (${ratio(row.reward_risk_reference)})</b></div>
    <div class="row"><span>Quality score</span><b>${cell(row.contract_quality_score)}</b></div>
    <div class="row"><span>Swing fit</span><b>${cell(labelText(row.swing_fit_label))} / ${cell(row.swing_fit_score)}</b></div>
    ${factorRows}
    <div class="row"><span>Swing why</span><b>${cell(swingReasons || '-')}</b></div>
    <div class="row"><span>Swing warnings</span><b>${cell(swingWarnings || 'clear')}</b></div>
    <div class="row"><span>Readiness</span><b>${cell(row.readiness_score)}</b></div>
    <div class="row"><span>Why</span><b>${cell(gradeReasons || row.review_thesis || '-')}</b></div>
    <div class="row"><span>Flags</span><b>${cell(flags || 'clear')}</b></div>
    <div class="row"><span>Request</span><b>${cell(query)}</b></div>
    <button class="btn contract-watchlist-btn" type="button" data-query="${escAttr(query)}" data-context="${escAttr(JSON.stringify(context))}">Save contract</button>
  </article>`;
}
function optionChainTradePlanHtml(plan) {
  if (!plan) return '';
  const checklist = (plan.checklist || []).slice(0, 5).map(item => `<li>${cell(item)}</li>`).join('');
  return `<div class="brief-list"><h4>Trade plan</h4>
    <ul>
      <li>${cell(plan.summary || '-')}</li>
      <li>Action: ${cell(labelText(plan.action || 'watch_only'))}</li>
      <li>Stop / target: ${cell(plan.stop_price_reference)} / ${cell(plan.target_price_reference)}</li>
      <li>Max loss ref: ${moneyShort(plan.max_loss_dollars_reference)}; stop loss ref: ${moneyShort(plan.stop_loss_dollars_reference)}</li>
      <li>Target gain ref: ${moneyShort(plan.target_gain_dollars_reference)}; R/R ${ratio(plan.reward_risk_reference)}</li>
      <li>Break-even: ${cell(plan.breakeven_price)} / ${pct(plan.breakeven_move_pct)}</li>
    </ul>
    <h4>Checks</h4><ul>${checklist || '<li>No checks listed.</li>'}</ul>
  </div>`;
}
function optionChainDecisionHtml(data) {
  const decision = data.decision || {};
  const primary = decision.primary || null;
  if (!primary) return '';
  const query = primary.contract_query || optionContractQuery(primary);
  const context = optionContractContext(primary);
  const exposure = decision.open_exposure || {};
  const breakdown = Array.isArray(primary.chain_factor_breakdown) ? primary.chain_factor_breakdown.slice(0, 5) : [];
  const factorRows = breakdown.length
    ? `<div class="factor-stack">${breakdown.map(f => `<div class="factor-row">
        <strong>${cell(f.factor || '-')}</strong>
        <span>${cell(f.detail || '')}</span>
        <em>${cell(f.score)}</em>
      </div>`).join('')}</div>`
    : '';
  const risks = (decision.risk_notes || []).length
    ? decision.risk_notes.map(note => `<li>${cell(note)}</li>`).join('')
    : '<li>No major chain-level warnings surfaced.</li>';
  const alternatives = (decision.alternatives || []).slice(0, 3).map(row => {
    const label = row.contract_query || optionContractQuery(row);
    return `<span class="pill">${cell(row.contract_grade || row.readiness_label || '-')}: ${cell(label)}</span>`;
  }).join('');
  return `<div class="decision-strip">
    <div class="decision-main">
      <div class="review-meta">
        <span class="pill ${escAttr(primary.readiness_label || 'review')}">${cell(decision.label || 'Chain decision')}</span>
        <span class="pill">${cell(decision.status || '-')}</span>
        <span class="pill">${cell(decision.quote_quality || data.quote_quality || '-')}</span>
        ${exposure.has_open ? `<span class="pill review">${cell(exposure.open_count || 0)} open exposure</span>` : ''}
        ${primary.chain_factor_summary ? `<span class="pill">${cell(primary.chain_factor_summary)}</span>` : ''}
      </div>
      <h3>${cell(query || `${primary.symbol || ''} ${primary.side || ''} ${primary.strike || ''}`)}</h3>
      <p>${cell(primary.review_thesis || decision.next_step || 'Review this contract before saving.')}</p>
      <div class="decision-metrics">
        <div class="decision-metric"><span>Grade</span><strong>${cell(primary.contract_grade || '-')} / ${cell(primary.review_lane || '-')}</strong></div>
        <div class="decision-metric"><span>Premium</span><strong>${moneyShort(primary.premium_dollars)}</strong></div>
        <div class="decision-metric"><span>Break-even</span><strong>${cell(primary.breakeven_price)} / ${pct(primary.breakeven_move_pct)}</strong></div>
        <div class="decision-metric"><span>Budget</span><strong>${cell(labelText(primary.budget_fit))} / ${pct(primary.budget_usage_pct)}</strong></div>
        <div class="decision-metric"><span>Spread</span><strong>${pct(primary.spread_pct)}</strong></div>
        <div class="decision-metric"><span>Risk ref</span><strong>${moneyShort(primary.risk_dollars_reference)} / ${ratio(primary.reward_risk_reference)}</strong></div>
        <div class="decision-metric"><span>DTE</span><strong>${cell(primary.dte)}</strong></div>
        <div class="decision-metric"><span>Open interest</span><strong>${cell(primary.openInterest)}</strong></div>
        <div class="decision-metric"><span>Quality</span><strong>${cell(primary.contract_quality_score)}</strong></div>
        <div class="decision-metric"><span>Swing fit</span><strong>${cell(labelText(primary.swing_fit_label))} / ${cell(primary.swing_fit_score)}</strong></div>
      </div>
      ${factorRows}
      <button class="btn contract-watchlist-btn" type="button" data-query="${escAttr(query)}" data-context="${escAttr(JSON.stringify(context))}">Save primary contract</button>
    </div>
    <div class="decision-side">
      ${optionChainTradePlanHtml(decision.trade_plan)}
      <div class="brief-list"><h4>Review risks</h4><ul>${risks}</ul></div>
      <div class="brief-list"><h4>Next step</h4><ul><li>${cell(decision.next_step || 'Refresh and compare before acting.')}</li><li>${cell(decision.saveable_count || 0)} A/B contract(s) saveable under these filters.</li></ul></div>
      <div class="decision-alt">${alternatives || '<span class="pill">No close alternatives</span>'}</div>
    </div>
  </div>`;
}
function optionChainResultsHtml(data) {
  const rows = data.rows || [];
  const rejected = data.rejection_examples || [];
  const rejectedPanel = rejected.length
    ? `<div class="brief-list" style="margin-top:10px"><h4>Why contracts were filtered out</h4>${table(rejected.slice(0, 12), true)}</div>`
    : '';
  if (!rows.length) {
    const topRejects = countMapText(data.rejection_reason_counts || {});
    const reasonText = topRejects ? ` Top filters: ${escHtml(topRejects)}.` : '';
    return `<div class="empty">No contracts matched these filters.${reasonText}</div>${rejectedPanel}`;
  }
  const decision = optionChainDecisionHtml(data);
  const topCards = rows.slice(0, 6).map(optionContractCard).join('');
  const expiryRows = data.expiry_summary || [];
  return `<div style="padding:12px">
    ${decision}
    <div class="setup-grid">${topCards}</div>
    <div class="brief-cols">
      <div class="brief-list"><h4>Expiration quality / budget ladder</h4>${table(expiryRows)}</div>
      <div class="brief-list"><h4>Top contracts</h4>${table(rows, true)}</div>
    </div>
    ${rejectedPanel}
  </div>`;
}
function optionChainBatchSummary(data) {
  const fields = [
    ['Candidates', data.candidate_count || 0],
    ['Scanned', data.symbols_scanned || 0],
    ['Successful', data.successful_scans || 0],
    ['Contracts shown', data.row_count || 0],
    ['Open exposure', data.open_exposure_count ? `${data.open_exposure_count} open: ${(data.open_exposure_symbols || []).join(', ')}` : 'clear'],
    ['Grades', countMapText(data.grade_counts || {})],
    ['Sources', countMapText(data.source_counts || {})],
    ['Errors', data.error_count || 0],
    ['Preset', data.preset || '-']
  ];
  return fields.map(([label, value]) => `<div class="brief-tile"><span>${escHtml(label)}</span><strong>${cell(value)}</strong></div>`).join('');
}
function optionChainBatchResultsHtml(data) {
  const rows = data.rows || [];
  if (!rows.length) {
    const errors = (data.errors || []).length ? `<div class="brief-list"><h4>Errors</h4>${table(data.errors || [], true)}</div>` : '';
    return `<div class="empty">No shortlist contracts matched these filters.</div>${errors}`;
  }
  const topCards = rows.slice(0, 8).map(optionContractCard).join('');
  const saveCount = bestChainBatchSaveRows(rows).length;
  return `<div style="padding:12px">
    <div class="scan-controls" style="padding:0 0 12px">
      <button class="btn" type="button" id="chain-bulk-save-best">Save best A/B contracts</button>
      <button class="btn" type="button" id="chain-bulk-export">Write shortlist files</button>
      <span class="muted">${saveCount} contract(s) eligible for one-click save</span>
    </div>
    <div class="setup-grid">${topCards}</div>
    <div class="brief-cols">
      <div class="brief-list"><h4>Symbol coverage</h4>${table(data.symbol_summaries || [], true)}</div>
      <div class="brief-list"><h4>Ranked contracts</h4>${table(rows, true)}</div>
    </div>
  </div>`;
}
async function saveChainPayloads(payloads, statusId) {
  const clean = (payloads || []).filter(Boolean).slice(0, 12);
  if (!clean.length) {
    $(statusId).textContent = 'No saveable contracts found.';
    return;
  }
  $(statusId).textContent = `Saving ${clean.length} contract(s) to research watchlist...`;
  const res = await fetch('/api/watchlist-add-many', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ items: clean })
  });
  const data = await res.json();
  if (!res.ok || data.saved_count === 0) {
    $(statusId).textContent = 'Could not save contracts: ' + (data.error || (data.errors || []).map(e => e.error).join(', ') || 'unknown error');
    return;
  }
  const err = data.error_count ? `, ${data.error_count} failed` : '';
  $(statusId).textContent = `${data.saved_count || 0} contract(s) saved${err}.`;
  await loadWatchlist();
  await loadSavedContracts();
}
function wireChainBatchActions(root=document) {
  const saveBtn = root.querySelector('#chain-bulk-save-best');
  if (saveBtn) saveBtn.addEventListener('click', async () => {
    const rows = window.latestChainBatchRows || [];
    const payloads = bestChainBatchSaveRows(rows).map(optionContractSavePayload);
    await saveChainPayloads(payloads, 'chain-bulk-status-text');
  });
  const exportBtn = root.querySelector('#chain-bulk-export');
  if (exportBtn) exportBtn.addEventListener('click', exportChainBatchShortlist);
}
async function exportChainBatchShortlist() {
  const report = window.latestChainBatchReport || null;
  if (!report || !(report.rows || []).length) {
    $('chain-bulk-status-text').textContent = 'Run a shortlist scan first.';
    return;
  }
  $('chain-bulk-status-text').textContent = 'Writing chain shortlist files...';
  const res = await fetch('/api/export-chain-shortlist', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ report })
  });
  const data = await res.json();
  if (!res.ok || data.ok === false) {
    $('chain-bulk-status-text').textContent = 'Could not write shortlist: ' + (data.error || 'unknown error');
    return;
  }
  $('chain-bulk-status-text').innerHTML = `${data.count || 0} contract(s) written to <a href="/artifact/option-chain-shortlist" target="_blank">CSV</a> and <a href="/artifact/option-chain-shortlist-json" target="_blank">JSON</a>.`;
}
async function scanOptionChain() {
  const query = $('chain-query').value.trim() || $('symbol').value.trim() || $('rh-query').value.trim();
  if (!query) {
    $('chain-status-text').textContent = 'Type a ticker or company first.';
    return;
  }
  $('chain-query').value = query;
  $('chain-status-text').textContent = 'Fetching option chain...';
  $('chain-summary').innerHTML = '';
  $('chain-results').innerHTML = '';
  const params = new URLSearchParams({
    query,
    preset: $('chain-preset').value || 'custom',
    side: $('chain-side').value,
    min_dte: $('chain-min-dte').value || 90,
    max_dte: $('chain-max-dte').value || 900,
    max_spread_pct: String((Number($('chain-max-spread').value || 0) / 100)),
    max_premium: $('chain-max-premium').value || 0,
    min_open_interest: $('chain-min-oi').value || 0,
    limit: '120'
  });
  const res = await fetch('/api/option-chain-scan?' + params.toString());
  const data = await res.json();
  if (!res.ok || data.ok === false) {
    $('chain-status-text').textContent = 'Option-chain scan failed: ' + (data.error || 'unknown error');
    return;
  }
  $('chain-status-text').textContent = `${data.filtered_count || 0} contract(s) matched from ${data.total_contracts || 0} total.`;
  $('chain-summary').innerHTML = optionChainSummary(data);
  $('chain-results').innerHTML = optionChainResultsHtml(data);
  wireClickableRows($('chain-results'));
  wireOptionChainActions($('chain-results'));
}
async function scanOptionChainBatch() {
  const query = $('chain-bulk-symbols').value.trim();
  $('chain-bulk-status-text').textContent = query ? 'Scanning typed shortlist...' : 'Scanning latest Optedge shortlist...';
  $('chain-bulk-summary').innerHTML = '';
  $('chain-bulk-results').innerHTML = '';
  window.latestChainBatchReport = null;
  window.latestChainBatchRows = [];
  const params = new URLSearchParams({
    query,
    preset: $('chain-preset').value || 'swing',
    side: $('chain-side').value,
    min_dte: $('chain-min-dte').value || 90,
    max_dte: $('chain-max-dte').value || 900,
    max_spread_pct: String((Number($('chain-max-spread').value || 0) / 100)),
    max_premium: $('chain-max-premium').value || 500,
    min_open_interest: $('chain-min-oi').value || 0,
    symbols_limit: $('chain-bulk-symbol-limit').value || 6,
    contracts_per_symbol: $('chain-bulk-contract-limit').value || 4,
    limit: '32'
  });
  const res = await fetch('/api/option-chain-batch?' + params.toString());
  const data = await res.json();
  if (!res.ok || data.ok === false) {
    $('chain-bulk-status-text').textContent = 'Shortlist chain scan failed: ' + (data.error || 'unknown error');
    return;
  }
  $('chain-bulk-status-text').textContent = `${data.row_count || 0} contract(s) from ${data.successful_scans || 0}/${data.symbols_scanned || 0} successful symbol scan(s).`;
  window.latestChainBatchReport = data;
  window.latestChainBatchRows = data.rows || [];
  $('chain-bulk-summary').innerHTML = optionChainBatchSummary(data);
  $('chain-bulk-results').innerHTML = optionChainBatchResultsHtml(data);
  wireClickableRows($('chain-bulk-results'));
  wireOptionChainActions($('chain-bulk-results'));
  wireChainBatchActions($('chain-bulk-results'));
}
async function loadPositions() {
  $('positions-status-text').textContent = 'Loading open positions...';
  const params = new URLSearchParams({
    asset: $('positions-asset').value,
    status: $('positions-status').value,
    query: $('positions-query').value.trim(),
    limit: '250'
  });
  const res = await fetch('/api/positions?' + params.toString());
  const data = await res.json();
  $('positions-status-text').textContent = `${data.count || 0} open position row(s).`;
  $('positions-results').innerHTML = table(data.rows || [], true);
  wireClickableRows($('positions-results'));
}
async function reloadPositionWorkspace() {
  await Promise.all([loadExitReviews(), loadPositions()]);
}
async function lookup() {
  const symbol = $('symbol').value.trim();
  if (!symbol) return;
  $('lookup-status').textContent = 'Searching local artifacts...';
  $('lookup-results').innerHTML = '';
  const res = await fetch('/api/lookup?symbol=' + encodeURIComponent(symbol));
  const data = await res.json();
  const resolved = data.lookup_symbol && data.lookup_symbol !== data.query ? ` (${data.lookup_symbol})` : '';
  $('lookup-status').textContent = `${data.total_hits} hit(s) for ${data.query}${resolved}.`;
  const brief = briefHtml(data.brief);
  const sections = Object.entries(data.sections).map(([name, rows]) => {
    return `<div class="section"><h3><span>${name.replaceAll('_', ' ')}</span><span>${rows.length}</span></h3>${table(rows)}</div>`;
  }).join('');
  $('lookup-results').innerHTML = brief + sections;
}
async function runSymbol() {
  const query = $('symbol').value.trim();
  if (!query) return;
  $('lookup-status').textContent = 'Resolving symbol and starting focused scan...';
  const res = await fetch('/api/run-symbol', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      query,
      mode: $('scan-mode').value,
      bankroll: $('scan-bankroll').value,
      aggressive: $('scan-aggressive').checked
    })
  });
  const data = await res.json();
  if (!res.ok || data.ok === false) {
    $('lookup-status').textContent = 'Could not start scan: ' + (data.error || 'unknown error');
    return;
  }
  $('lookup-status').textContent = `Started focused scan for ${data.symbol}. You can keep using the cockpit while it runs.`;
  await loadJobs();
}
$('lookup').addEventListener('click', lookup);
$('run-symbol').addEventListener('click', runSymbol);
$('symbol').addEventListener('keydown', (e) => { if (e.key === 'Enter') lookup(); });
$('symbol').addEventListener('input', () => scheduleSuggestions('symbol', 'symbol-suggestions', true));
$('global-lookup').addEventListener('click', globalLookup);
$('global-workspace').addEventListener('click', globalReviewWorkspace);
$('global-run').addEventListener('click', globalRunScan);
$('global-chain').addEventListener('click', globalScanChain);
$('global-save').addEventListener('click', globalSaveWatchlist);
$('global-query').addEventListener('keydown', (e) => { if (e.key === 'Enter') globalLookup(); });
$('global-query').addEventListener('input', () => scheduleSuggestions('global-query', 'global-suggestions', false));
$('refresh').addEventListener('click', () => { loadSummary(); loadCommandCenter(); if ($('swing-packet-results').innerHTML.trim()) loadSwingPacket(false); loadTodayReview(); loadSwingClimate(); loadBestSetups(); loadSwingScout(); loadClimateGatedSetups(); loadActionQueue(); loadMarketPulse(); loadBreadthPulse(); loadSectorPulse(); loadMacroStress(); loadRiskSummary(); loadExitReviews(); loadPerformanceSummary(); loadFreeDataSources(); loadWatchlistSecFilings(); loadSavedContracts(); });
$('swing-packet-preview').addEventListener('click', () => loadSwingPacket(false));
$('swing-packet-write').addEventListener('click', () => loadSwingPacket(true));
$('swing-packet-chain').addEventListener('click', () => loadSwingPacket(true, true));
$('positions-load').addEventListener('click', reloadPositionWorkspace);
$('positions-query').addEventListener('keydown', (e) => { if (e.key === 'Enter') reloadPositionWorkspace(); });
$('explorer-load').addEventListener('click', loadExplorer);
$('explorer-query').addEventListener('keydown', (e) => { if (e.key === 'Enter') loadExplorer(); });
$('swing-scout-load').addEventListener('click', loadSwingScout);
$('swing-scout-query').addEventListener('keydown', (e) => { if (e.key === 'Enter') loadSwingScout(); });
['swing-scout-asset', 'swing-scout-lane', 'swing-scout-min-score', 'swing-scout-hide-wait', 'swing-scout-nasdaq'].forEach(id => {
  $(id).addEventListener('change', loadSwingScout);
});
$('paper-preview').addEventListener('click', () => loadPaperCandidates(false));
$('paper-export').addEventListener('click', () => loadPaperCandidates(true));
$('rh-preview').addEventListener('click', () => loadRobinhoodQueue(false));
$('rh-write').addEventListener('click', () => loadRobinhoodQueue(true));
document.querySelectorAll('.chain-preset').forEach(btn => {
  btn.addEventListener('click', () => applyChainPreset(btn.dataset.preset || 'custom'));
});
['chain-side', 'chain-min-dte', 'chain-max-dte', 'chain-max-spread', 'chain-max-premium', 'chain-min-oi'].forEach(id => {
  $(id).addEventListener('change', () => {
    if ($('chain-preset').value !== 'custom') applyChainPreset('custom');
  });
});
$('chain-scan').addEventListener('click', scanOptionChain);
$('chain-query').addEventListener('keydown', (e) => { if (e.key === 'Enter') scanOptionChain(); });
$('chain-bulk-scan').addEventListener('click', scanOptionChainBatch);
$('chain-bulk-symbols').addEventListener('keydown', (e) => { if (e.key === 'Enter') scanOptionChainBatch(); });
$('saved-contracts-refresh').addEventListener('click', () => loadSavedContracts(false));
$('saved-contracts-quotes').addEventListener('click', () => loadSavedContracts(true));
$('saved-contracts-run').addEventListener('click', runWatchlist);
$('provider-check').addEventListener('click', loadProviderStatus);
$('provider-query').addEventListener('keydown', (e) => { if (e.key === 'Enter') loadProviderStatus(); });
document.querySelectorAll('.view-tab').forEach(btn => {
  btn.addEventListener('click', () => {
    const view = btn.dataset.view || 'overview';
    setView(view);
    if (view === 'providers' && !$('provider-results').dataset.loaded) {
      loadProviderStatus().catch(err => {
        $('provider-status-text').textContent = 'Provider check failed';
        console.error(err);
      });
    }
    if (view === 'research' && !$('sec-filings-results').dataset.loaded) {
      loadWatchlistSecFilings().catch(err => {
        $('sec-filings-status-text').textContent = 'SEC filings monitor failed';
        console.error(err);
      });
    }
  });
});
$('watchlist-add').addEventListener('click', addWatchlist);
$('watchlist-run').addEventListener('click', runWatchlist);
$('sec-filings-refresh').addEventListener('click', loadWatchlistSecFilings);
$('watchlist-query').addEventListener('keydown', (e) => { if (e.key === 'Enter') addWatchlist(); });
$('watchlist-query').addEventListener('input', () => scheduleSuggestions('watchlist-query', 'watchlist-suggestions', false));
loadSummary().catch(err => { $('asof').textContent = 'Status failed'; console.error(err); });
loadCommandCenter().catch(err => { $('command-center-status-text').textContent = 'Command center failed'; console.error(err); });
loadJobs().catch(err => console.error(err));
loadPositions().catch(err => { $('positions-status-text').textContent = 'Position monitor failed'; console.error(err); });
loadExitReviews().catch(err => { $('exit-review-status-text').textContent = 'Exit review cockpit failed'; console.error(err); });
loadTodayReview().catch(err => { $('today-review-status-text').textContent = 'Today review failed'; console.error(err); });
loadSwingClimate().catch(err => { $('swing-climate-status-text').textContent = 'Swing climate failed'; console.error(err); });
loadBestSetups().catch(err => { $('best-setups-status-text').textContent = 'Best setups failed'; console.error(err); });
loadSwingScout().catch(err => { $('swing-scout-status-text').textContent = 'Swing scout failed'; console.error(err); });
loadClimateGatedSetups().catch(err => { $('climate-gated-status-text').textContent = 'Climate-gated setups failed'; console.error(err); });
loadActionQueue().catch(err => { $('queue-status-text').textContent = 'Action queue failed'; console.error(err); });
loadMarketPulse().catch(err => { $('market-pulse-status-text').textContent = 'Market pulse failed'; console.error(err); });
loadBreadthPulse().catch(err => { $('breadth-pulse-status-text').textContent = 'Breadth pulse failed'; console.error(err); });
loadSectorPulse().catch(err => { $('sector-pulse-status-text').textContent = 'Sector pulse failed'; console.error(err); });
loadMacroStress().catch(err => { $('macro-stress-status-text').textContent = 'Macro stress failed'; console.error(err); });
loadRiskSummary().catch(err => { $('risk-status-text').textContent = 'Risk summary failed'; console.error(err); });
loadPerformanceSummary().catch(err => { $('performance-status-text').textContent = 'Performance summary failed'; console.error(err); });
loadFreeDataSources().catch(err => { $('free-sources-status-text').textContent = 'Free source map failed'; console.error(err); });
loadExplorer().catch(err => { $('explorer-status-text').textContent = 'Explorer failed'; console.error(err); });
loadPaperCandidates(false).catch(err => { $('paper-status-text').textContent = 'Paper candidate preview failed'; console.error(err); });
loadRobinhoodQueue(false).catch(err => { $('rh-status-text').textContent = 'Agentic queue preview failed'; console.error(err); });
loadWatchlist().catch(err => { $('watchlist-status-text').textContent = 'Watchlist failed'; console.error(err); });
loadSavedContracts().catch(err => { $('saved-contracts-status-text').textContent = 'Saved contracts failed'; console.error(err); });
setInterval(() => { loadJobs().catch(() => {}); }, 5000);
</script>
</body>
</html>"""


class CockpitHandler(BaseHTTPRequestHandler):
    data_dir = DATA_DIR

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj: Any, status: int = 200) -> None:
        self._send(status, _json_bytes(obj), "application/json; charset=utf-8")

    def _send_file(self, path: Path, content_type: str | None = None) -> None:
        if not path.exists() or not path.is_file():
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        if content_type is None:
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._send(200, path.read_bytes(), content_type)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, render_cockpit_html().encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/summary":
            self._send_json(build_summary(self.data_dir))
            return
        if parsed.path == "/api/command-center":
            self._send_json(build_command_center(self.data_dir))
            return
        if parsed.path == "/api/swing-packet":
            self._send_json(build_swing_packet(self.data_dir, write=False))
            return
        if parsed.path == "/api/data-health":
            self._send_json(build_data_health(self.data_dir))
            return
        if parsed.path == "/api/action-queue":
            params = parse_qs(parsed.query)
            limit = _int_param(params.get("limit", ["20"])[0], 20, 1, 100)
            self._send_json(build_action_queue(self.data_dir, limit=limit))
            return
        if parsed.path == "/api/today-review":
            params = parse_qs(parsed.query)
            limit = _int_param(params.get("limit", ["12"])[0], 12, 1, 40)
            self._send_json(build_today_review(self.data_dir, limit=limit))
            return
        if parsed.path == "/api/swing-climate":
            params = parse_qs(parsed.query)
            period = params.get("period", ["6mo"])[0]
            self._send_json(build_swing_climate(self.data_dir, period=period))
            return
        if parsed.path == "/api/market-pulse":
            params = parse_qs(parsed.query)
            period = params.get("period", ["6mo"])[0]
            self._send_json(build_market_pulse(self.data_dir, period=period))
            return
        if parsed.path == "/api/breadth-pulse":
            params = parse_qs(parsed.query)
            period = params.get("period", ["6mo"])[0]
            self._send_json(build_breadth_pulse(self.data_dir, period=period))
            return
        if parsed.path == "/api/sector-pulse":
            params = parse_qs(parsed.query)
            period = params.get("period", ["6mo"])[0]
            self._send_json(build_sector_pulse(self.data_dir, period=period))
            return
        if parsed.path == "/api/macro-stress":
            self._send_json(build_macro_stress_pulse(self.data_dir))
            return
        if parsed.path == "/api/risk-summary":
            self._send_json(build_risk_summary(self.data_dir))
            return
        if parsed.path == "/api/performance-summary":
            self._send_json(build_performance_summary(self.data_dir))
            return
        if parsed.path == "/api/free-data-sources":
            self._send_json(build_free_data_sources(self.data_dir))
            return
        if parsed.path == "/api/provider-status":
            params = parse_qs(parsed.query)
            query = params.get("query", ["AAPL"])[0]
            include_chain = _bool_param(params.get("include_chain", ["true"])[0], True)
            self._send_json(build_provider_status(
                self.data_dir, query=query, include_chain=include_chain,
            ))
            return
        if parsed.path == "/api/lookup":
            symbol = parse_qs(parsed.query).get("symbol", [""])[0]
            if not symbol.strip():
                self._send_json({"error": "symbol is required"}, status=400)
                return
            self._send_json(lookup_symbol(symbol, self.data_dir))
            return
        if parsed.path == "/api/suggestions":
            params = parse_qs(parsed.query)
            query = params.get("query", [""])[0]
            limit = _int_param(params.get("limit", ["16"])[0], 16, 1, 50)
            self._send_json(build_symbol_suggestions(self.data_dir, query=query, limit=limit))
            return
        if parsed.path == "/api/best-setups":
            params = parse_qs(parsed.query)
            per_asset = _int_param(params.get("per_asset", ["3"])[0], 3, 1, 10)
            limit = _int_param(params.get("limit", ["12"])[0], 12, 1, 40)
            self._send_json(build_best_setups(self.data_dir, per_asset=per_asset, limit=limit))
            return
        if parsed.path == "/api/swing-scout":
            params = parse_qs(parsed.query)
            limit = _int_param(params.get("limit", ["18"])[0], 18, 1, 60)
            asset = params.get("asset", ["all"])[0].strip().lower()
            lane = params.get("lane", ["all"])[0].strip().lower()
            query = params.get("query", [""])[0]
            min_score = _float_param(params.get("min_score", ["45"])[0], 45.0, 0.0, 100.0)
            include_wait = _bool_param(params.get("include_wait", ["true"])[0], True)
            include_nasdaq_movers = _bool_param(params.get("include_nasdaq_movers", ["true"])[0], True)
            self._send_json(build_swing_scout(
                self.data_dir,
                limit=limit,
                asset=asset,
                query=query,
                lane=lane,
                min_score=min_score,
                include_wait=include_wait,
                include_nasdaq_movers=include_nasdaq_movers,
            ))
            return
        if parsed.path == "/api/option-chain-batch":
            params = parse_qs(parsed.query)
            query = params.get("query", [""])[0]
            preset = params.get("preset", ["swing"])[0]
            side = params.get("side", ["all"])[0]
            min_dte = _int_param(
                params.get("min_dte", [str(MIN_SWING_OPTION_DTE)])[0],
                MIN_SWING_OPTION_DTE,
                0,
                1200,
            )
            max_dte = _int_param(params.get("max_dte", ["900"])[0], 900, 1, 1600)
            max_spread = _float_param(params.get("max_spread_pct", ["0.25"])[0], 0.25, 0.0, 5.0)
            max_premium = _float_param(params.get("max_premium", ["500"])[0], 500.0, 0.0, 1_000_000.0)
            min_oi = _int_param(params.get("min_open_interest", ["0"])[0], 0, 0, 1_000_000)
            symbols_limit = _int_param(params.get("symbols_limit", ["6"])[0], 6, 1, 20)
            contracts_per_symbol = _int_param(params.get("contracts_per_symbol", ["4"])[0], 4, 1, 12)
            limit = _int_param(params.get("limit", ["18"])[0], 18, 1, 80)
            self._send_json(build_option_chain_batch(
                self.data_dir,
                query=query,
                side=side,
                min_dte=min_dte,
                max_dte=max_dte,
                max_spread_pct=max_spread,
                max_premium=max_premium,
                min_open_interest=min_oi,
                preset=preset,
                symbols_limit=symbols_limit,
                contracts_per_symbol=contracts_per_symbol,
                limit=limit,
            ))
            return
        if parsed.path == "/api/climate-gated-setups":
            params = parse_qs(parsed.query)
            per_asset = _int_param(params.get("per_asset", ["4"])[0], 4, 1, 10)
            limit = _int_param(params.get("limit", ["12"])[0], 12, 1, 40)
            include_held = _bool_param(params.get("include_held", ["true"])[0], True)
            self._send_json(build_climate_gated_setups(
                self.data_dir, per_asset=per_asset, limit=limit, include_held=include_held,
            ))
            return
        if parsed.path == "/api/opportunities":
            params = parse_qs(parsed.query)
            asset = params.get("asset", ["all"])[0].strip().lower()
            if asset not in {"all", *OPPORTUNITY_SPECS.keys()}:
                self._send_json({"error": "invalid asset"}, status=400)
                return
            status = params.get("status", ["all"])[0].strip().lower()
            query = params.get("query", [""])[0]
            min_conf = _float_param(params.get("min_confidence", ["0"])[0], 0.0, 0.0, 100.0)
            limit = _int_param(params.get("limit", ["80"])[0], 80, 1, 250)
            self._send_json(build_opportunities(
                self.data_dir, asset=asset, query=query, status=status,
                min_confidence=min_conf, limit=limit,
            ))
            return
        if parsed.path == "/api/positions":
            params = parse_qs(parsed.query)
            asset = params.get("asset", ["all"])[0].strip().lower()
            if asset not in {"all", *POSITION_FILES.keys()}:
                self._send_json({"error": "invalid asset"}, status=400)
                return
            status = params.get("status", ["all"])[0].strip().lower()
            query = params.get("query", [""])[0]
            limit = _int_param(params.get("limit", ["250"])[0], 250, 1, 500)
            self._send_json(build_positions(
                self.data_dir, asset=asset, query=query, status=status, limit=limit,
            ))
            return
        if parsed.path == "/api/exit-reviews":
            params = parse_qs(parsed.query)
            asset = params.get("asset", ["all"])[0].strip().lower()
            if asset not in {"all", "option", "share", "futures"}:
                self._send_json({"error": "invalid asset"}, status=400)
                return
            query = params.get("query", [""])[0]
            limit = _int_param(params.get("limit", ["80"])[0], 80, 1, 500)
            self._send_json(build_exit_review_summary(
                self.data_dir, asset=asset, query=query, limit=limit,
            ))
            return
        if parsed.path == "/api/paper-candidates":
            params = parse_qs(parsed.query)
            asset = params.get("asset", ["all"])[0].strip().lower()
            if asset not in {"all", "option", "share", "futures"}:
                self._send_json({"error": "invalid asset"}, status=400)
                return
            max_new = _int_param(params.get("max_new", ["5"])[0], 5, 1, 30)
            max_open = _int_param(params.get("max_open", ["30"])[0], 30, 1, 200)
            include_watch = _bool_param(params.get("include_watch", ["false"])[0])
            allow_zero = _bool_param(params.get("allow_zero_size_placeholder", ["false"])[0])
            dry_run = _bool_param(params.get("dry_run", ["false"])[0])
            query = params.get("query", [""])[0]
            self._send_json(build_paper_candidates(
                self.data_dir,
                max_new=max_new,
                max_open=max_open,
                include_watch=include_watch,
                allow_zero_size_placeholder=allow_zero,
                asset=asset,
                dry_run=dry_run,
                write=False,
                query=query,
            ))
            return
        if parsed.path == "/api/robinhood-queue":
            params = parse_qs(parsed.query)
            account_budget = _float_param(params.get("account_budget", ["500"])[0], 500.0, 1.0, 1_000_000.0)
            max_candidates = _int_param(params.get("max_candidates", ["5"])[0], 5, 1, 20)
            max_orders = _int_param(params.get("max_orders", ["2"])[0], 2, 1, 10)
            min_dte = _int_param(params.get("min_dte", ["180"])[0], 180, 0, 1200)
            min_conf = _float_param(params.get("min_confidence", ["55"])[0], 55.0, 0.0, 100.0)
            query = params.get("query", [""])[0]
            self._send_json(build_robinhood_agentic_queue_report(
                self.data_dir,
                account_budget=account_budget,
                max_candidates=max_candidates,
                max_orders=max_orders,
                min_dte=min_dte,
                min_confidence=min_conf,
                query=query,
                write=False,
            ))
            return
        if parsed.path == "/api/option-chain-scan":
            params = parse_qs(parsed.query)
            query = params.get("query", [""])[0]
            preset = params.get("preset", ["custom"])[0]
            side = params.get("side", ["all"])[0]
            min_dte = _int_param(
                params.get("min_dte", [str(MIN_SWING_OPTION_DTE)])[0],
                MIN_SWING_OPTION_DTE,
                0,
                1200,
            )
            max_dte = _int_param(params.get("max_dte", ["900"])[0], 900, 1, 1600)
            max_spread = _float_param(params.get("max_spread_pct", ["0.25"])[0], 0.25, 0.0, 5.0)
            max_premium = _float_param(params.get("max_premium", ["500"])[0], 500.0, 0.0, 1_000_000.0)
            min_oi = _int_param(params.get("min_open_interest", ["0"])[0], 0, 0, 1_000_000)
            limit = _int_param(params.get("limit", ["80"])[0], 80, 1, 500)
            report = build_option_chain_scan(
                query,
                self.data_dir,
                side=side,
                min_dte=min_dte,
                max_dte=max_dte,
                max_spread_pct=max_spread,
                max_premium=max_premium,
                min_open_interest=min_oi,
                limit=limit,
                preset=preset,
            )
            self._send_json(report, status=200 if report.get("ok") else 400)
            return
        if parsed.path == "/api/watchlist":
            params = parse_qs(parsed.query)
            enrich = _bool_param(params.get("enrich", ["false"])[0])
            self._send_json(load_watchlist(self.data_dir, enrich=enrich))
            return
        if parsed.path == "/api/watchlist-sec-filings":
            params = parse_qs(parsed.query)
            limit = _int_param(params.get("limit", ["40"])[0], 40, 1, 120)
            self._send_json(build_watchlist_sec_filings(self.data_dir, limit=limit))
            return
        if parsed.path == "/api/saved-option-contracts":
            params = parse_qs(parsed.query)
            enrich = _bool_param(params.get("enrich", ["true"])[0], True)
            limit = _int_param(params.get("limit", ["80"])[0], 80, 1, 250)
            refresh_quotes = _bool_param(params.get("refresh_quotes", ["false"])[0], False)
            quote_limit = _int_param(params.get("quote_limit", ["20"])[0], 20, 0, 80)
            self._send_json(build_saved_option_contracts(
                self.data_dir,
                enrich=enrich,
                limit=limit,
                refresh_quotes=refresh_quotes,
                quote_limit=quote_limit,
            ))
            return
        if parsed.path == "/api/jobs":
            self._send_json({"jobs": list_jobs(self.data_dir)})
            return
        if parsed.path == "/api/job":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            job = read_job(job_id, self.data_dir) if job_id else None
            if not job:
                self._send_json({"error": "job not found"}, status=404)
                return
            self._send_json(job)
            return
        if parsed.path == "/api/job-log":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            if not job_id:
                self._send_json({"error": "id is required"}, status=400)
                return
            self._send_json(read_job_log(job_id, self.data_dir))
            return
        if parsed.path == "/lookup":
            symbol = parse_qs(parsed.query).get("symbol", [""])[0]
            if not symbol.strip():
                self._send(400, b"symbol is required", "text/plain; charset=utf-8")
                return
            self._send(200, render_html(lookup_symbol(symbol, self.data_dir)).encode("utf-8"),
                       "text/html; charset=utf-8")
            return
        if parsed.path == "/job-dashboard":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            path = job_dashboard_path(job_id, self.data_dir) if job_id else None
            if path is None:
                self._send(404, b"Job dashboard not found", "text/plain; charset=utf-8")
                return
            self._send_file(path, "text/html; charset=utf-8")
            return
        if parsed.path == "/job-lookup":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            path = job_lookup_path(job_id, self.data_dir) if job_id else None
            if path is None:
                self._send(404, b"Job lookup not found", "text/plain; charset=utf-8")
                return
            self._send_file(path, "text/html; charset=utf-8")
            return
        if parsed.path.startswith("/artifact/"):
            name = parsed.path.rsplit("/", 1)[-1]
            path = artifact_path(name, self.data_dir)
            if path is None:
                self._send(404, b"Artifact not found", "text/plain; charset=utf-8")
                return
            content_type = ARTIFACTS.get(name, ("", None))[1]
            self._send_file(path, content_type)
            return
        self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        if parsed.path not in {
            "/api/run-symbol", "/api/export-paper", "/api/build-robinhood-queue",
            "/api/export-chain-shortlist", "/api/build-swing-packet",
            "/api/watchlist-add", "/api/watchlist-add-many", "/api/watchlist-remove", "/api/watchlist-run",
            "/api/warm-sec-cache", "/api/warm-symbol-caches", "/api/run-refresh-scan",
        }:
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except Exception:
            length = 0
        raw = self.rfile.read(min(length, 60000)) if length > 0 else b"{}"
        try:
            body = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            body = {}
        if parsed.path in {"/api/warm-sec-cache", "/api/warm-symbol-caches"}:
            result = warm_symbol_caches(self.data_dir)
            self._send_json(result, status=200 if result.get("ok") else 502)
            return
        if parsed.path == "/api/watchlist-add":
            context = body.get("context") if isinstance(body.get("context"), dict) else None
            result = add_watchlist_query(str(body.get("query") or ""), self.data_dir, context=context)
            self._send_json(result, status=200 if result.get("ok") else 400)
            return
        if parsed.path == "/api/watchlist-add-many":
            result = add_watchlist_queries(
                body.get("items"),
                self.data_dir,
                limit=_int_param(str(body.get("limit") or "12"), 12, 1, 25),
            )
            status = 200 if result.get("saved_count", 0) > 0 else 400
            self._send_json(result, status=status)
            return
        if parsed.path == "/api/watchlist-remove":
            result = remove_watchlist_entry(str(body.get("id") or ""), self.data_dir)
            self._send_json(result, status=200 if result.get("ok") else 404)
            return
        if parsed.path == "/api/watchlist-run":
            result = run_watchlist_scans(
                self.data_dir,
                mode=str(body.get("mode") or "full"),
                bankroll=body.get("bankroll"),
                aggressive=_bool_param(body.get("aggressive"), False),
                launch=True,
            )
            self._send_json(result)
            return
        if parsed.path == "/api/run-refresh-scan":
            mode = str(body.get("mode") or "full").strip().lower()
            scan_args = _scan_args_from_controls(
                mode,
                body.get("bankroll"),
                _bool_param(body.get("aggressive"), False),
            )
            result = create_refresh_job(
                self.data_dir,
                launch=True,
                extra_scan_args=scan_args,
                scan_mode=mode or "full",
            )
            self._send_json(result, status=200 if result.get("ok") else 400)
            return
        if parsed.path == "/api/export-paper":
            asset = str(body.get("asset") or "all").strip().lower()
            if asset not in {"all", "option", "share", "futures"}:
                self._send_json({"error": "invalid asset"}, status=400)
                return
            dry_run = _bool_param(body.get("dry_run"), False)
            report = build_paper_candidates(
                self.data_dir,
                max_new=_int_param(str(body.get("max_new") or "5"), 5, 1, 30),
                max_open=_int_param(str(body.get("max_open") or "30"), 30, 1, 200),
                include_watch=_bool_param(body.get("include_watch"), False),
                allow_zero_size_placeholder=_bool_param(body.get("allow_zero_size_placeholder"), False),
                asset=asset,
                dry_run=dry_run,
                write=not dry_run,
                query=str(body.get("query") or ""),
            )
            self._send_json(report)
            return
        if parsed.path == "/api/export-chain-shortlist":
            report = body.get("report") if isinstance(body.get("report"), dict) else {}
            result = write_option_chain_shortlist(report, self.data_dir)
            self._send_json(result, status=200 if result.get("ok") else 400)
            return
        if parsed.path == "/api/build-swing-packet":
            self._send_json(build_swing_packet(
                self.data_dir,
                write=True,
                refresh_chains=_bool_param(body.get("refresh_chains"), False),
                chain_symbols_limit=_int_param(str(body.get("chain_symbols_limit") or "6"), 6, 1, 12),
                chain_contracts_per_symbol=_int_param(str(body.get("chain_contracts_per_symbol") or "4"), 4, 1, 8),
            ))
            return
        if parsed.path == "/api/build-robinhood-queue":
            report = build_robinhood_agentic_queue_report(
                self.data_dir,
                account_budget=_float_param(str(body.get("account_budget") or "500"), 500.0, 1.0, 1_000_000.0),
                max_candidates=_int_param(str(body.get("max_candidates") or "5"), 5, 1, 20),
                max_orders=_int_param(str(body.get("max_orders") or "2"), 2, 1, 10),
                min_dte=_int_param(str(body.get("min_dte") or "180"), 180, 0, 1200),
                min_confidence=_float_param(str(body.get("min_confidence") or "55"), 55.0, 0.0, 100.0),
                query=str(body.get("query") or ""),
                write=True,
            )
            self._send_json(report)
            return
        query = str(body.get("query") or "").strip()
        if not query:
            self._send_json({"ok": False, "error": "query is required"}, status=400)
            return
        mode = str(body.get("mode") or "full").strip().lower()
        scan_args = _scan_args_from_controls(
            mode,
            body.get("bankroll"),
            _bool_param(body.get("aggressive"), False),
        )
        result = create_job(query, self.data_dir, launch=True,
                            extra_scan_args=scan_args, scan_mode=mode or "full")
        self._send_json(result, status=200 if result.get("ok") else 400)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def run_server(host: str = "127.0.0.1", port: int = 8765,
               data_dir: Path = DATA_DIR, open_browser: bool = True) -> None:
    handler = type("OptedgeCockpitHandler", (CockpitHandler,), {"data_dir": data_dir})
    server = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}"
    print(f"Optedge cockpit: {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nCockpit stopped.")
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the free local Optedge interactive cockpit.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--data-dir", default=str(DATA_DIR))
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args(argv)
    run_server(args.host, args.port, Path(args.data_dir), open_browser=not args.no_open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
