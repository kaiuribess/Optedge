"""Local Optedge ticker lookup.

This does not call a broker or paid API. It reads the latest generated Optedge
snapshots and open-position state, then writes a compact ticker report.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

import data_provider
from engines import cboe_symbol_data as cboe_symbol_data_engine
from engines import sec_ftd as sec_ftd_engine
from engines.regsho_threshold import threshold_rows_for_symbols
from engines.short_sale_circuit import circuit_rows_for_symbols
from engines.trading_halts import halt_rows_for_symbols
from scripts.export_external_paper_track import _load_option_chain_shortlist
from scripts.sec_filings import companyfacts_for_symbol, recent_filings_for_symbol
from scripts.symbol_resolver import resolve_symbol

DATA_DIR = ROOT / "data"
FRESH_SNAPSHOT_MINUTES = 90.0
STALE_SNAPSHOT_MINUTES = 360.0


def rich_lookup_kwargs() -> dict[str, bool]:
    """Default free context layers for interactive/saved lookup reports."""
    return {
        "include_price": True,
        "include_market_structure": True,
        "include_cboe_activity": True,
    }

SNAPSHOTS = {
    "options": ("top_options_*.parquet", "ticker"),
    "shares": ("top_shares_*.parquet", "ticker"),
    "value": ("top_value_*.parquet", "ticker"),
    "futures": ("top_futures_*.parquet", "symbol"),
}

OPEN_FILES = {
    "open_options": ("open_positions.json", "ticker"),
    "open_shares": ("open_share_positions.json", "ticker"),
    "open_futures": ("open_futures_positions.json", "symbol"),
}

DISPLAY_COLUMNS = {
    "options": [
        "ticker", "side", "strike", "expiry", "dte", "mid", "spot", "confidence",
        "rank_score", "fused_score", "trade_status", "suggested_contracts",
        "stop_price", "target_price", "spread_pct", "ev_pct", "net_edge_pct",
        "chain_source", "quote_quality", "snapshot_age_min", "snapshot_freshness",
        "top_headline",
    ],
    "shares": [
        "ticker", "spot", "confidence", "rank_score", "fused_score", "trade_status",
        "suggested_dollars", "stop_price", "target_price", "ev_pct",
        "snapshot_age_min", "snapshot_freshness", "top_headline",
    ],
    "value": [
        "ticker", "value_score", "value_bucket", "pe", "fcf_yield", "earnings_yield",
        "insider_score", "snapshot_age_min", "snapshot_freshness", "top_headline",
    ],
    "futures": [
        "symbol", "name", "direction", "contract", "using_micro", "futures_score",
        "rank_score", "trade_status", "suggested_contracts", "entry_price",
        "stop_price", "target_price", "risk_dollars", "reward_dollars",
        "snapshot_age_min", "snapshot_freshness",
    ],
    "chain_shortlist": [
        "ticker", "contract", "side", "strike", "expiry", "dte", "mid", "bid",
        "ask", "spread_pct", "premium_dollars", "actual_dollars", "confidence",
        "rank_score", "trade_status", "suggested_contracts", "stop_price",
        "target_price", "contract_grade", "review_lane", "readiness_score",
        "swing_fit_label", "openInterest", "volume", "chain_source",
        "quote_quality", "snapshot_age_min", "snapshot_freshness", "review_thesis",
    ],
    "open_options": [
        "ticker", "side", "strike", "expiry", "entry_time", "entry_price",
        "current_mid", "unrealized_pct", "trade_status", "stop_price", "target_price",
        "latest_exit_pressure", "latest_exit_action", "chain_source", "quote_quality",
        "last_reprice_source",
    ],
    "open_shares": [
        "ticker", "entry_time", "entry_price", "current_price", "unrealized_pct",
        "trade_status", "stop_price", "target_price", "latest_exit_pressure",
        "latest_exit_action",
    ],
    "open_futures": [
        "symbol", "direction", "entry_time", "entry_price", "current_price",
        "pnl_pct", "pnl_dollars", "trade_status", "stop_price", "target_price",
        "latest_exit_pressure", "latest_exit_action",
    ],
    "broker_positions": [
        "account_mask", "account_label", "asset", "symbol", "contract",
        "option_side", "strike", "expiry", "quantity", "average_price",
        "current_price", "unrealized_pct",
        "market_value", "bid_price", "ask_price", "quote_updated_at",
        "agentic_allowed", "option_level", "snapshot_age_min",
        "snapshot_freshness", "status",
    ],
    "requested_option_matches": [
        "ticker", "side", "strike", "expiry", "dte", "mid", "spot", "confidence",
        "rank_score", "fused_score", "trade_status", "suggested_contracts",
        "stop_price", "target_price", "spread_pct", "premium_dollars",
        "actual_dollars", "ev_pct", "net_edge_pct",
        "chain_source", "quote_quality", "snapshot_age_min", "snapshot_freshness",
        "match_quality", "strike_diff", "requested_side", "requested_expiry",
        "requested_strike", "match_source", "contract_grade", "review_lane",
        "readiness_score", "top_headline",
    ],
    "option_alternatives": [
        "ticker", "contract", "side", "strike", "expiry", "dte", "mid", "bid",
        "ask", "spread_pct", "premium_dollars", "actual_dollars", "confidence",
        "rank_score", "trade_status", "suggested_contracts", "stop_price",
        "target_price", "contract_grade", "review_lane", "readiness_score",
        "swing_fit_score", "swing_fit_label", "openInterest", "volume",
        "chain_source", "quote_quality", "snapshot_age_min", "snapshot_freshness",
        "strike_diff", "dte_diff", "alternative_score", "alternative_reason",
    ],
    "cboe_option_activity": [
        "ticker", "option_side", "strike", "expiry", "cboe_activity_volume",
        "cboe_activity_matched", "cboe_activity_routed", "cboe_activity_bid",
        "cboe_activity_ask", "cboe_activity_mid", "cboe_activity_spread_pct",
        "cboe_activity_last", "cboe_activity_contract", "cboe_activity_venues",
        "cboe_activity_source", "match_quality", "strike_diff",
    ],
    "recent_sec_filings": [
        "ticker", "company_name", "form", "filing_date", "report_date",
        "filing_signal", "description", "url",
    ],
    "sec_companyfacts": [
        "ticker", "company_name", "metric", "label", "value", "unit",
        "period_end", "filed", "form", "concept",
    ],
    "price_snapshot": [
        "symbol", "last_price", "last_date", "ret_5d", "ret_20d", "ret_60d",
        "sma20", "sma50", "range_6mo_pos", "hv20", "trend_label",
        "history_source", "history_quality", "rows",
    ],
    "market_structure": [
        "symbol", "check", "status", "flag", "risk_score", "detail",
        "source", "source_url",
    ],
    "data_coverage": [
        "layer", "group", "status", "rows", "source", "freshness", "detail",
    ],
}


def _latest_file(data_dir: Path, pattern: str) -> Path | None:
    files = [p for p in data_dir.glob(pattern) if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: (p.stat().st_mtime, p.name))


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


def _worst_snapshot_freshness(left: Any, right: Any) -> str:
    severity = {"unknown": 0, "fresh": 1, "aging": 2, "stale": 3}
    left_norm = str(left or "unknown").strip().lower()
    right_norm = str(right or "unknown").strip().lower()
    if left_norm not in severity:
        left_norm = "unknown"
    if right_norm not in severity:
        right_norm = "unknown"
    return left_norm if severity[left_norm] >= severity[right_norm] else right_norm


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
    file_freshness = _snapshot_freshness(age)
    out["_source_file"] = path.name
    if "snapshot_age_min" in out.columns:
        row_age = pd.to_numeric(out["snapshot_age_min"], errors="coerce")
        out["snapshot_age_min"] = row_age.fillna(age).clip(lower=age).round(1)
    else:
        out["snapshot_age_min"] = round(age, 1)
    if "snapshot_freshness" in out.columns:
        out["snapshot_freshness"] = out["snapshot_freshness"].apply(
            lambda value: _worst_snapshot_freshness(value, file_freshness)
        )
    else:
        out["snapshot_freshness"] = file_freshness
    return out


def _read_json_rows(path: Path) -> pd.DataFrame:
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return pd.DataFrame()
    if not isinstance(rows, list) or not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _chain_shortlist_frame(data_dir: Path) -> pd.DataFrame:
    """Read the saved deep option-chain shortlist with lookup freshness fields."""
    try:
        df = _load_option_chain_shortlist(data_dir)
    except Exception:
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    if "ticker" in out.columns:
        out["ticker"] = out["ticker"].astype(str).str.upper().str.strip()
    if "premium_dollars" not in out.columns and "actual_dollars" in out.columns:
        out["premium_dollars"] = out["actual_dollars"]

    source_path = None
    for name in ("option_chain_shortlist.json", "option_chain_shortlist.csv"):
        candidate = data_dir / name
        if candidate.exists():
            source_path = candidate
            break
    if source_path is not None:
        age = _snapshot_age_minutes(source_path)
        file_freshness = _snapshot_freshness(age)
        out["_source_file"] = source_path.name
        if "snapshot_age_min" in out.columns:
            row_age = pd.to_numeric(out["snapshot_age_min"], errors="coerce")
            out["snapshot_age_min"] = row_age.fillna(age).clip(lower=age).round(1)
        else:
            out["snapshot_age_min"] = round(age, 1)
        if "snapshot_freshness" in out.columns:
            out["snapshot_freshness"] = out["snapshot_freshness"].apply(
                lambda value: _worst_snapshot_freshness(value, file_freshness)
            )
        else:
            out["snapshot_freshness"] = file_freshness
    elif "_source_file" not in out.columns:
        out["_source_file"] = "option_chain_shortlist"
    return out


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


def _float_value(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
    except Exception:
        return default
    return out if math.isfinite(out) else default


def _frame_records(df: pd.DataFrame, section: str) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    cols = [c for c in DISPLAY_COLUMNS.get(section, []) if c in df.columns]
    if not cols:
        cols = list(df.columns[:20])
    records = []
    for _, row in df[cols].head(100).iterrows():
        records.append({str(k): _clean_value(v) for k, v in row.to_dict().items()})
    return records


def _coverage_source_status(source: Any) -> str:
    text = str(source or "").strip().lower()
    if not text:
        return ""
    if "unavailable" in text or "failed" in text or "error" in text:
        return "unavailable"
    return "available"


def _coverage_row(
    layer: str,
    group: str,
    rows: int,
    source: Any,
    *,
    status: str | None = None,
    detail: str = "",
    freshness: Any = None,
) -> dict[str, Any]:
    source_status = _coverage_source_status(source)
    if status is None:
        if rows > 0:
            status = "hit"
        elif source_status == "unavailable":
            status = "unavailable"
        elif source:
            status = "checked_clear"
        else:
            status = "missing_snapshot"
    if not detail:
        if rows > 0:
            detail = f"{rows} matching row(s) found."
        elif status == "checked_clear":
            detail = "Layer checked; no matching exposure or ticker row."
        elif status == "missing_snapshot":
            detail = "No local artifact was available for this layer."
        elif status == "not_requested":
            detail = "Layer was not requested for this lookup."
        elif status == "not_applicable":
            detail = "Layer is not applicable for this symbol."
        else:
            detail = status.replace("_", " ")
    return {
        "layer": layer,
        "group": group,
        "status": status,
        "rows": int(rows or 0),
        "source": _clean_value(source),
        "freshness": _clean_value(freshness),
        "detail": detail,
    }


def _section_freshness(rows: list[dict[str, Any]]) -> Any:
    values = [
        row.get("snapshot_freshness")
        for row in rows
        if isinstance(row, dict) and row.get("snapshot_freshness")
    ]
    if values:
        rank = {"fresh": 0, "aging": 1, "stale": 2, "unknown": 3}
        return max(values, key=lambda value: rank.get(str(value), 4))
    return None


def _build_data_coverage(
    symbol: str,
    sections: dict[str, list[dict[str, Any]]],
    sources: dict[str, str | None],
    *,
    include_price: bool,
    include_market_structure: bool,
    include_sec: bool,
    requested_option: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for section, group, label in [
        ("options", "ranked_ideas", "Ranked options"),
        ("shares", "ranked_ideas", "Ranked shares"),
        ("value", "ranked_ideas", "Value ideas"),
        ("futures", "ranked_ideas", "Futures ideas"),
        ("chain_shortlist", "option_chain", "Saved chain shortlist"),
    ]:
        sec_rows = sections.get(section, [])
        rows.append(_coverage_row(
            label,
            group,
            len(sec_rows),
            sources.get(section),
            freshness=_section_freshness(sec_rows),
        ))

    for section, label in [
        ("open_options", "Open options"),
        ("open_shares", "Open shares"),
        ("open_futures", "Open futures"),
        ("broker_positions", "Broker snapshot positions"),
    ]:
        sec_rows = sections.get(section, [])
        rows.append(_coverage_row(
            label,
            "position_state",
            len(sec_rows),
            sources.get(section),
            freshness=_section_freshness(sec_rows),
        ))

    if requested_option:
        sec_rows = sections.get("requested_option_matches", [])
        rows.append(_coverage_row(
            "Requested option match",
            "option_request",
            len(sec_rows),
            sources.get("requested_option_matches"),
            status=None if sec_rows else "missing_snapshot",
            detail=(
                "Requested option was matched to latest option/chain artifacts."
                if sec_rows else "Requested option was not found in latest option/chain artifacts."
            ),
            freshness=_section_freshness(sec_rows),
        ))

    if include_price:
        sec_rows = sections.get("price_snapshot", [])
        rows.append(_coverage_row(
            "Free price snapshot",
            "price",
            len(sec_rows),
            sources.get("price_snapshot"),
            status="hit" if sec_rows else "unavailable",
            detail="Free history stack returned a trend snapshot." if sec_rows else "Free history stack did not return price data.",
        ))
    else:
        rows.append(_coverage_row("Free price snapshot", "price", 0, None, status="not_requested"))

    if include_market_structure:
        report = sections.get("_market_structure_report", [{}])[0] if sections.get("_market_structure_report") else {}
        market_status = str(report.get("status") or "clear")
        market_rows = len(sections.get("market_structure", []))
        rows.append(_coverage_row(
            "Market-structure risk",
            "official_risk",
            market_rows,
            sources.get("market_structure"),
            status=market_status,
            detail=(
                "Official no-key risk lists checked; no symbol-specific flags."
                if market_status == "clear" else "; ".join(str(x) for x in (report.get("flags") or [])[:4])
            ),
        ))
    else:
        rows.append(_coverage_row("Market-structure risk", "official_risk", 0, None, status="not_requested"))

    if include_sec and not symbol.endswith("=F") and not symbol.startswith("^"):
        for section, label in [
            ("recent_sec_filings", "Recent SEC filings"),
            ("sec_companyfacts", "SEC companyfacts"),
        ]:
            sec_rows = sections.get(section, [])
            rows.append(_coverage_row(
                label,
                "official_filings",
                len(sec_rows),
                sources.get(section),
                status=None if sources.get(section) else "unavailable",
            ))
    elif include_sec:
        rows.append(_coverage_row(
            "SEC filings/facts",
            "official_filings",
            0,
            None,
            status="not_applicable",
        ))
    else:
        rows.append(_coverage_row("SEC filings/facts", "official_filings", 0, None, status="not_requested"))

    bad_statuses = {"blocked", "unavailable"}
    warn_statuses = {"risk_review", "missing_snapshot"}
    scored = [row for row in rows if row["status"] not in {"not_requested", "not_applicable"}]
    bad = [row for row in scored if row["status"] in bad_statuses]
    warn = [row for row in scored if row["status"] in warn_statuses]
    hits = [row for row in scored if row["status"] in {"hit", "checked_clear", "clear"}]
    score = max(0, min(100, 100 - 22 * len(bad) - 7 * len(warn)))
    if any(row["status"] == "blocked" for row in scored) or score < 45:
        status = "blocked"
        label = "Coverage blocked"
    elif score < 75:
        status = "caution"
        label = "Coverage caution"
    else:
        status = "ready"
        label = "Coverage ready"
    warnings = []
    if bad:
        warnings.append(f"{len(bad)} lookup data layer(s) unavailable or blocked.")
    if warn:
        warnings.append(f"{len(warn)} lookup data layer(s) missing local artifacts or need review.")
    if not any(row["group"] == "ranked_ideas" and row["status"] == "hit" for row in rows):
        warnings.append("No ranked local option/share/value/futures idea matched this symbol.")
    report = {
        "score": score,
        "status": status,
        "label": label,
        "hit_count": len(hits),
        "warn_count": len(warn),
        "bad_count": len(bad),
        "checked_layers": len(scored),
        "warnings": warnings[:4],
    }
    return rows, report


def _match(df: pd.DataFrame, column: str, query: str) -> pd.DataFrame:
    if df is None or df.empty or column not in df.columns:
        return pd.DataFrame()
    q = query.strip().upper()
    values = df[column].astype(str).str.upper().str.strip()
    return df[values == q].copy()


def _all_frame_records(df: pd.DataFrame | None, limit: int = 1000) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    rows: list[dict[str, Any]] = []
    for _, row in df.head(limit).iterrows():
        rows.append({str(k): _clean_value(v) for k, v in row.to_dict().items()})
    return rows


def _score_row(row: pd.Series) -> float:
    for col in ("rank_score", "fused_score", "futures_score", "value_score", "confidence"):
        value = _float_value(row.get(col))
        if value is not None:
            return value / 100.0 if col == "confidence" else value
    return 0.0


def _quote_source_info(row: pd.Series | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    getter = row.get
    source = str(
        getter("chain_source")
        or getter("quote_source")
        or getter("last_reprice_source")
        or ""
    ).strip()
    quality = str(getter("quote_quality") or "").strip().lower()
    if not source or source.lower() in {"nan", "none"}:
        if not quality:
            return None
        source = "unknown"

    source_key = source.lower()
    src_label = source.replace("_", " ").title()
    is_live = source_key == "tradier" or quality in {"live_or_broker", "live", "broker"}
    if is_live:
        label = f"Live {src_label}"
        warning = None
    elif source_key.startswith("cboe"):
        label = "CBOE delayed"
        warning = "Option quote source is free/delayed; verify bid/ask before paper tracking."
    elif source_key.startswith("nasdaq"):
        label = "NASDAQ free"
        warning = "Option quote source is free/non-live; verify bid/ask before paper tracking."
    elif source_key.startswith("yfinance"):
        label = "Yahoo fallback"
        warning = "Option quote source is a free fallback and may be delayed or partial."
    else:
        label = src_label
        warning = "Option quote source quality is unknown; verify bid/ask before paper tracking."

    return {
        "source": source,
        "quality": quality or None,
        "label": label,
        "is_live_or_broker": is_live,
        "warning": warning,
    }


def _series_return(close: pd.Series, lookback: int) -> float | None:
    if close is None or len(close) <= lookback:
        return None
    current = _float_value(close.iloc[-1])
    previous = _float_value(close.iloc[-lookback - 1])
    if current is None or previous in {None, 0}:
        return None
    return (current - previous) / previous


def _price_trend_label(ret_20d: float | None, sma20: float | None, sma50: float | None, last: float | None) -> str:
    if last is None:
        return "unknown"
    above20 = sma20 is not None and last >= sma20
    above50 = sma50 is not None and last >= sma50
    if ret_20d is not None and ret_20d >= 0.08 and above20:
        return "strong_uptrend"
    if ret_20d is not None and ret_20d > 0 and (above20 or above50):
        return "uptrend"
    if ret_20d is not None and ret_20d <= -0.08 and not above20:
        return "strong_downtrend"
    if ret_20d is not None and ret_20d < 0 and not above20:
        return "downtrend"
    return "rangebound"


def _price_snapshot(symbol: str) -> dict[str, Any] | None:
    clean = str(symbol or "").strip().upper()
    if not clean:
        return None
    try:
        hist = data_provider.get_history(clean, period="6mo", interval="1d", cache_age=1800)
    except Exception:
        return None
    if hist is None or hist.empty or "Close" not in hist.columns:
        return None

    out = hist.copy()
    out["Close"] = pd.to_numeric(out["Close"], errors="coerce")
    out = out.dropna(subset=["Close"])
    if out.empty:
        return None
    close = out["Close"]
    last = _float_value(close.iloc[-1])
    high = pd.to_numeric(out.get("High", close), errors="coerce")
    low = pd.to_numeric(out.get("Low", close), errors="coerce")
    high_6mo = _float_value(high.max())
    low_6mo = _float_value(low.min())
    range_pos = (
        (last - low_6mo) / (high_6mo - low_6mo)
        if last is not None and high_6mo is not None and low_6mo is not None and high_6mo > low_6mo
        else None
    )
    returns = close.pct_change().dropna()
    hv20 = None
    if len(returns) >= 5:
        sample = returns.tail(20)
        hv20 = _float_value(sample.std() * math.sqrt(252))
    sma20 = _float_value(close.tail(20).mean()) if len(close) >= 5 else None
    sma50 = _float_value(close.tail(50).mean()) if len(close) >= 20 else None
    ret_20d = _series_return(close, 20)
    try:
        last_date = pd.to_datetime(out.index[-1], errors="coerce")
        last_date_text = None if pd.isna(last_date) else last_date.strftime("%Y-%m-%d")
    except Exception:
        last_date_text = None
    return {
        "symbol": clean,
        "last_price": _clean_value(last),
        "last_date": last_date_text,
        "ret_5d": _clean_value(_series_return(close, 5)),
        "ret_20d": _clean_value(ret_20d),
        "ret_60d": _clean_value(_series_return(close, 60)),
        "sma20": _clean_value(sma20),
        "sma50": _clean_value(sma50),
        "high_6mo": _clean_value(high_6mo),
        "low_6mo": _clean_value(low_6mo),
        "range_6mo_pos": _clean_value(range_pos),
        "hv20": _clean_value(hv20),
        "trend_label": _price_trend_label(ret_20d, sma20, sma50, last),
        "history_source": _clean_value(hist.attrs.get("history_source")),
        "history_quality": _clean_value(hist.attrs.get("history_quality")),
        "rows": int(len(out)),
    }


def _market_structure_row(symbol: str, check: str, status: str, flag: str, risk_score: Any, detail: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "check": check,
        "status": status,
        "flag": flag,
        "risk_score": _clean_value(risk_score),
        "detail": detail,
        "source": _clean_value(row.get("source")),
        "source_url": _clean_value(row.get("source_url")),
    }


def _market_structure_snapshot(symbol: str) -> dict[str, Any]:
    """Official no-key market-structure context for one symbol."""
    clean = str(symbol or "").strip().upper()
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    flags: list[str] = []
    max_risk = 0

    def add_warning(flag: str, message: str, risk_score: Any) -> None:
        nonlocal max_risk
        flags.append(flag)
        warnings.append(message)
        risk = _float_value(risk_score, 0.0) or 0.0
        max_risk = max(max_risk, int(risk))

    if not clean or clean.startswith("^") or clean.endswith("=F"):
        return {
            "status": "not_applicable",
            "risk_score": 0,
            "flags": [],
            "warning_count": 0,
            "warnings": [],
            "rows": [],
        }

    try:
        halts = halt_rows_for_symbols([clean], cache_age=60)
    except Exception:
        halts = pd.DataFrame()
    for raw in _all_frame_records(halts, limit=10):
        active = bool(raw.get("active_halt"))
        flag = "active_trading_halt" if active else "recent_trading_halt"
        risk = raw.get("halt_risk_score")
        detail = f"Nasdaq halt {raw.get('reason_code') or ''}".strip()
        status = "blocked" if active else "risk_review"
        rows.append(_market_structure_row(clean, "trade_halt", status, flag, risk, detail, raw))
        if active:
            add_warning(flag, f"Market structure blocked: active trading halt for {clean}.", risk)
        else:
            add_warning(flag, f"Market structure risk: recent trading halt for {clean}.", risk)

    try:
        thresholds = threshold_rows_for_symbols([clean], cache_age=6 * 3600)
    except Exception:
        thresholds = pd.DataFrame()
    for raw in _all_frame_records(thresholds, limit=10):
        flag = "regsho_threshold"
        risk = raw.get("settlement_risk_score")
        detail = "Reg SHO threshold security" if raw.get("is_threshold") else "Reg SHO monitor row"
        rows.append(_market_structure_row(clean, "regsho_threshold", "risk_review", flag, risk, detail, raw))
        add_warning(flag, f"Market structure risk: {clean} is on the Reg SHO threshold list.", risk)

    try:
        circuits = circuit_rows_for_symbols([clean], cache_age=30 * 60)
    except Exception:
        circuits = pd.DataFrame()
    for raw in _all_frame_records(circuits, limit=10):
        flag = "short_sale_circuit_breaker"
        risk = raw.get("ssr_risk_score")
        detail = f"Short-sale circuit breaker triggered {raw.get('trigger_time') or ''}".strip()
        rows.append(_market_structure_row(clean, "short_sale_circuit", "risk_review", flag, risk, detail, raw))
        add_warning(flag, f"Market structure risk: {clean} is under a short-sale circuit breaker.", risk)

    try:
        ftd = sec_ftd_engine.run([clean], max_files=1)
    except Exception:
        ftd = pd.DataFrame()
    for raw in _all_frame_records(ftd, limit=3):
        ftd_score = _float_value(raw.get("sec_ftd_score"), 0.0) or 0.0
        fails = int(_float_value(raw.get("sec_ftd_fails"), 0.0) or 0)
        dollars = _float_value(raw.get("sec_ftd_dollars"), 0.0) or 0.0
        active_days = int(_float_value(raw.get("sec_ftd_active_days"), 0.0) or 0)
        elevated = bool(ftd_score >= 1.25 or fails >= 500_000 or dollars >= 1_000_000)
        flag = "sec_ftd_pressure" if elevated else "sec_ftd_context"
        risk = int(max(0, min(88, 45 + ftd_score * 18)))
        detail = (
            f"Delayed SEC FTD: {fails:,} fails, ${dollars:,.0f}, "
            f"{active_days} active day(s); not proof of abusive shorting"
        )
        source_row = {
            **raw,
            "source": raw.get("sec_ftd_source") or "sec_fails_to_deliver",
            "source_url": "https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data",
        }
        rows.append(_market_structure_row(
            clean,
            "sec_fails_to_deliver",
            "risk_review" if elevated else "clear",
            flag,
            risk,
            detail,
            source_row,
        ))
        if elevated:
            add_warning(
                flag,
                f"Market structure risk: {clean} has elevated delayed SEC fails-to-deliver context.",
                risk,
            )

    clean_flags = list(dict.fromkeys(flags))
    status = (
        "blocked" if "active_trading_halt" in clean_flags
        else "risk_review" if clean_flags
        else "clear"
    )
    return {
        "status": status,
        "risk_score": max_risk,
        "flags": clean_flags,
        "warning_count": len(warnings),
        "warnings": list(dict.fromkeys(warnings)),
        "rows": rows,
    }


def _best_row(matches: dict[str, pd.DataFrame]) -> tuple[str | None, pd.Series | None]:
    candidates: list[tuple[float, str, pd.Series]] = []
    for section, df in matches.items():
        if df is None or df.empty or section.startswith("open_"):
            continue
        for _, row in df.iterrows():
            candidates.append((_score_row(row), section, row))
    if not candidates:
        return None, None
    _, section, row = max(candidates, key=lambda item: item[0])
    return section, row


def _factor_drivers(row: pd.Series | None, limit: int = 6) -> dict[str, list[dict[str, Any]]]:
    if row is None:
        return {"positive": [], "negative": []}
    items = []
    for col, value in row.items():
        name = str(col)
        if not (name.startswith("z_") or name.endswith("_score") or name in {"rank_score", "fused_score"}):
            continue
        val = _float_value(value)
        if val is None or abs(val) < 0.05:
            continue
        items.append({
            "factor": name.replace("z_", "").replace("_score", "").replace("_", " "),
            "column": name,
            "value": round(val, 4),
        })
    positive = sorted([x for x in items if x["value"] > 0], key=lambda x: x["value"], reverse=True)
    negative = sorted([x for x in items if x["value"] < 0], key=lambda x: x["value"])
    return {"positive": positive[:limit], "negative": negative[:limit]}


def _open_position_summary(open_rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = []
    pressures = []
    for row in open_rows:
        pnl = _float_value(row.get("unrealized_pct", row.get("pnl_pct")))
        pressure = _float_value(row.get("latest_exit_pressure"))
        if pnl is not None:
            pnls.append(pnl)
        if pressure is not None:
            pressures.append(pressure)
    return {
        "count": len(open_rows),
        "avg_unrealized_pct": (sum(pnls) / len(pnls)) if pnls else None,
        "worst_unrealized_pct": min(pnls) if pnls else None,
        "best_unrealized_pct": max(pnls) if pnls else None,
        "max_exit_pressure": max(pressures) if pressures else None,
        "attention_count": sum(1 for p in pressures if p >= 60),
    }


def _broker_snapshot_age_minutes(snapshot: dict[str, Any]) -> float | None:
    try:
        generated_at = pd.to_datetime(snapshot.get("generated_at"), errors="coerce", utc=True)
        if pd.isna(generated_at):
            return None
        return max(0.0, (pd.Timestamp(datetime.now(timezone.utc)) - generated_at).total_seconds() / 60.0)
    except Exception:
        return None


def _broker_snapshot_positions(symbol: str, data_dir: Path) -> list[dict[str, Any]]:
    snapshot = _load_json_obj(data_dir / "robinhood_broker_snapshot.json")
    if not snapshot:
        return []
    q = str(symbol or "").upper().strip()
    age = _broker_snapshot_age_minutes(snapshot)
    freshness = _snapshot_freshness(age)
    rows: list[dict[str, Any]] = []
    for account in snapshot.get("accounts") or []:
        account_mask = account.get("account_mask")
        account_label = account.get("label") or account.get("nickname")
        for pos in account.get("option_positions") or []:
            sym = str(pos.get("chain_symbol") or pos.get("symbol") or "").upper().strip()
            if sym != q:
                continue
            quantity = _float_value(pos.get("quantity"), 0.0) or 0.0
            average_price = _float_value(pos.get("average_price"))
            current_price = _float_value(pos.get("current_price") or pos.get("mark_price"))
            multiplier = _float_value(pos.get("trade_value_multiplier"), 100.0) or 100.0
            market_value = (
                current_price * quantity * multiplier
                if current_price is not None else None
            )
            unrealized = (
                (current_price - average_price) / average_price
                if current_price is not None and average_price not in {None, 0}
                else None
            )
            option_type = str(pos.get("option_type") or pos.get("type") or "").lower()
            side = "C" if option_type.startswith("call") else "P" if option_type.startswith("put") else option_type.upper()
            expiry = str(pos.get("expiration_date") or "")
            strike = pos.get("strike_price")
            contract = " ".join(
                part for part in [
                    sym,
                    expiry,
                    side,
                    str(strike or ""),
                ] if part
            )
            rows.append({
                "account_mask": account_mask,
                "account_label": account_label,
                "asset": "option",
                "symbol": sym,
                "contract": contract,
                "option_side": side,
                "side": "call" if side == "C" else "put" if side == "P" else side,
                "strike": _clean_value(strike),
                "expiry": expiry,
                "quantity": quantity,
                "average_price": average_price,
                "current_price": current_price,
                "unrealized_pct": unrealized,
                "market_value": market_value,
                "bid_price": _clean_value(pos.get("bid_price")),
                "ask_price": _clean_value(pos.get("ask_price")),
                "quote_updated_at": _clean_value(pos.get("quote_updated_at")),
                "agentic_allowed": bool(account.get("agentic_allowed")),
                "option_level": _clean_value(account.get("option_level")),
                "snapshot_age_min": None if age is None else round(age, 1),
                "snapshot_freshness": freshness,
                "status": "broker_snapshot",
            })
        for pos in account.get("equity_positions") or []:
            sym = str(pos.get("symbol") or pos.get("ticker") or "").upper().strip()
            if sym != q:
                continue
            quantity = _float_value(pos.get("quantity"), 0.0) or 0.0
            average_price = _float_value(pos.get("average_buy_price") or pos.get("average_price"))
            current_price = _float_value(pos.get("current_price") or pos.get("mark_price"))
            rows.append({
                "account_mask": account_mask,
                "account_label": account_label,
                "asset": "equity",
                "symbol": sym,
                "contract": sym,
                "quantity": quantity,
                "average_price": average_price,
                "current_price": current_price,
                "unrealized_pct": (
                    (current_price - average_price) / average_price
                    if current_price is not None and average_price not in {None, 0}
                    else None
                ),
                "market_value": current_price * quantity if current_price is not None else None,
                "agentic_allowed": bool(account.get("agentic_allowed")),
                "option_level": _clean_value(account.get("option_level")),
                "snapshot_age_min": None if age is None else round(age, 1),
                "snapshot_freshness": freshness,
                "status": "broker_snapshot",
            })
    return rows


def _side_code(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"c", "call", "calls"}:
        return "C"
    if raw in {"p", "put", "puts"}:
        return "P"
    return raw.upper()[:1]


def _expiry_key(value: Any) -> str:
    if value is None:
        return ""
    try:
        ts = pd.to_datetime(value, errors="coerce")
        if not pd.isna(ts):
            return ts.strftime("%Y-%m-%d")
    except Exception:
        pass
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else text


def _strike_key(value: Any) -> str:
    try:
        return f"{float(value):g}"
    except Exception:
        return str(value or "").strip()


def _option_contract_key(symbol: Any, side: Any, expiry: Any, strike: Any) -> str:
    return "|".join([
        str(symbol or "").strip().upper(),
        _side_code(side),
        _expiry_key(expiry),
        _strike_key(strike),
    ])


def _row_contract_key(row: dict[str, Any]) -> str:
    contract = str(row.get("contract") or "").strip()
    symbol = row.get("ticker") or row.get("symbol")
    side = row.get("side") or row.get("option_side") or row.get("right")
    expiry = row.get("expiry") or row.get("expiration_date")
    strike = row.get("strike") or row.get("strike_price")
    if contract and (not side or not expiry or strike in (None, "")):
        parts = contract.replace("_", " ").split()
        for part in parts:
            if not symbol and part.isalpha():
                symbol = part
            if not expiry and re.match(r"^\d{4}-\d{2}-\d{2}$", part):
                expiry = part
            if not side and part.upper() in {"C", "P", "CALL", "PUT"}:
                side = part
        if strike in (None, "") and side:
            side_index = next(
                (idx for idx, part in enumerate(parts) if part.upper() in {"C", "P", "CALL", "PUT"}),
                None,
            )
            if side_index is not None and side_index + 1 < len(parts):
                strike = parts[side_index + 1]
    return _option_contract_key(symbol, side, expiry, strike)


def _request_contract_key(request: dict[str, Any] | None) -> str:
    if not request or request.get("asset") != "option":
        return ""
    return _option_contract_key(
        request.get("ticker"), request.get("side"), request.get("expiry"), request.get("strike")
    )


def _contract_exposure_summary(
    request: dict[str, Any] | None,
    open_option_rows: list[dict[str, Any]],
    broker_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    key = _request_contract_key(request)
    if not key:
        return None
    ticker = str((request or {}).get("ticker") or "").upper().strip()
    exact_open = [row for row in open_option_rows if _row_contract_key(row) == key]
    broker_options = [row for row in broker_rows if row.get("asset") == "option"]
    exact_broker = [row for row in broker_options if _row_contract_key(row) == key]
    same_ticker_open = [
        row for row in open_option_rows
        if str(row.get("ticker") or row.get("symbol") or "").upper().strip() == ticker
    ]
    same_ticker_broker = [
        row for row in broker_options
        if str(row.get("ticker") or row.get("symbol") or "").upper().strip() == ticker
    ]
    exact_count = len(exact_open) + len(exact_broker)
    same_ticker_count = len(same_ticker_open) + len(same_ticker_broker)
    status = (
        "exact_exposure" if exact_count
        else "same_ticker_exposure" if same_ticker_count
        else "clear"
    )
    return {
        "requested_contract_key": key,
        "status": status,
        "exact_open_positions": len(exact_open),
        "exact_broker_positions": len(exact_broker),
        "exact_total": exact_count,
        "same_ticker_open_options": len(same_ticker_open),
        "same_ticker_broker_options": len(same_ticker_broker),
        "same_ticker_total": same_ticker_count,
        "matched_open_labels": [
            str(row.get("position_id") or row.get("contract") or row.get("ticker") or "")
            for row in exact_open[:5]
        ],
        "matched_broker_labels": [
            str(row.get("contract") or row.get("symbol") or "")
            for row in exact_broker[:5]
        ],
    }


def _broker_position_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [
        _float_value(row.get("unrealized_pct"))
        for row in rows
        if _float_value(row.get("unrealized_pct")) is not None
    ]
    market_values = [
        _float_value(row.get("market_value"))
        for row in rows
        if _float_value(row.get("market_value")) is not None
    ]
    stale = any(str(row.get("snapshot_freshness") or "").lower() == "stale" for row in rows)
    return {
        "count": len(rows),
        "option_count": sum(1 for row in rows if row.get("asset") == "option"),
        "equity_count": sum(1 for row in rows if row.get("asset") == "equity"),
        "market_value": sum(market_values) if market_values else None,
        "avg_unrealized_pct": (sum(pnls) / len(pnls)) if pnls else None,
        "snapshot_freshness": "stale" if stale else (rows[0].get("snapshot_freshness") if rows else None),
        "max_snapshot_age_min": max(
            (_float_value(row.get("snapshot_age_min"), 0.0) or 0.0) for row in rows
        ) if rows else None,
    }


def _requested_option_summary(
    request: dict[str, Any] | None,
    matches: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not request or request.get("asset") != "option":
        return None
    best = matches[0] if matches else {}
    side = str(request.get("side") or "").strip().lower()
    side_code = "C" if side.startswith("c") else "P" if side.startswith("p") else side.upper()
    try:
        strike_text = f"{float(request.get('strike')):g}"
    except Exception:
        strike_text = str(request.get("strike") or "").strip()
    label = " ".join(
        part for part in [
            str(request.get("ticker") or "").upper(),
            str(request.get("expiry") or ""),
            side_code,
            strike_text,
        ] if part
    )
    quality = str(best.get("match_quality") or "missing")
    return {
        "label": label,
        "match_count": len(matches),
        "match_quality": quality,
        "matched_contract": (
            f"{best.get('ticker')} {str(best.get('side') or '').upper()[:1]} "
            f"{best.get('strike')} {best.get('expiry')}"
            if best else None
        ),
        "matched_mid": _clean_value(best.get("mid")),
        "matched_premium_dollars": _clean_value(
            best.get("premium_dollars", best.get("actual_dollars"))
        ),
        "matched_spread_pct": _clean_value(best.get("spread_pct")),
        "matched_quote_quality": _clean_value(best.get("quote_quality")),
        "matched_chain_source": _clean_value(best.get("chain_source")),
        "matched_confidence": _clean_value(best.get("confidence")),
        "matched_readiness_score": _clean_value(best.get("readiness_score")),
        "matched_swing_fit_score": _clean_value(best.get("swing_fit_score")),
        "strike_diff": _clean_value(best.get("strike_diff")),
    }


def _contract_comparison(
    requested: dict[str, Any] | None,
    alternatives: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compare the searched option against the best nearby chain alternative."""
    if not requested:
        return {
            "status": "not_requested",
            "label": "No option contract requested",
            "winner": "none",
            "edge_score": None,
            "reasons": ["Search a specific option contract to compare alternatives."],
        }
    alternatives = alternatives or {}
    if not alternatives.get("count") or not alternatives.get("best_label"):
        return {
            "status": "no_alternative",
            "label": "No nearby alternative",
            "winner": "requested",
            "edge_score": None,
            "requested_label": requested.get("label"),
            "alternative_label": None,
            "reasons": ["No saved chain-shortlist alternative matched the same ticker and side."],
        }

    quality = str(requested.get("match_quality") or "missing").lower()
    req_readiness = _float_value(
        requested.get("matched_readiness_score"),
        _float_value(requested.get("matched_confidence"), 0.0),
    ) or 0.0
    req_swing = _float_value(requested.get("matched_swing_fit_score"), req_readiness) or 0.0
    alt_readiness = _float_value(alternatives.get("best_readiness_score"), 0.0) or 0.0
    alt_swing = _float_value(alternatives.get("best_swing_fit_score"), alt_readiness) or 0.0
    req_spread = _float_value(requested.get("matched_spread_pct"))
    alt_spread = _float_value(alternatives.get("best_spread_pct"))
    req_premium = _float_value(requested.get("matched_premium_dollars"))
    alt_premium = _float_value(alternatives.get("best_premium_dollars"))

    edge = 0.0
    reasons: list[str] = []
    if quality != "exact":
        edge += 35.0
        reasons.append(f"Requested contract match is {quality}, not exact.")
    else:
        reasons.append("Requested contract exists in local data.")

    readiness_delta = alt_readiness - req_readiness
    if abs(readiness_delta) >= 5:
        edge += readiness_delta * 0.45
        side = "alternative" if readiness_delta > 0 else "requested"
        reasons.append(f"{side.title()} readiness is {abs(readiness_delta):.0f} points better.")

    swing_delta = alt_swing - req_swing
    if abs(swing_delta) >= 5:
        edge += swing_delta * 0.25
        side = "alternative" if swing_delta > 0 else "requested"
        reasons.append(f"{side.title()} swing fit is {abs(swing_delta):.0f} points better.")

    spread_delta = None
    if req_spread is not None and alt_spread is not None:
        spread_delta = req_spread - alt_spread
        if abs(spread_delta) >= 0.03:
            edge += spread_delta * 140.0
            side = "alternative" if spread_delta > 0 else "requested"
            reasons.append(f"{side.title()} spread is {abs(spread_delta):.1%} tighter.")

    premium_delta_pct = None
    if req_premium and alt_premium is not None and req_premium > 0:
        premium_delta_pct = (req_premium - alt_premium) / req_premium
        if abs(premium_delta_pct) >= 0.10:
            edge += premium_delta_pct * 18.0
            side = "alternative" if premium_delta_pct > 0 else "requested"
            reasons.append(f"{side.title()} premium is {abs(premium_delta_pct):.0%} cheaper.")

    if edge >= 8.0:
        winner = "alternative"
        status = "alternative_preferred"
        label = "Alternative looks cleaner"
    elif edge <= -8.0:
        winner = "requested"
        status = "requested_preferred"
        label = "Requested contract looks cleaner"
    else:
        winner = "inconclusive"
        status = "mixed"
        label = "Mixed contract evidence"
        if len(reasons) == 1:
            reasons.append("No nearby alternative was materially cleaner on readiness, spread, or premium.")

    return {
        "status": status,
        "label": label,
        "winner": winner,
        "edge_score": round(max(0.0, min(100.0, 50.0 + edge)), 1),
        "requested_label": requested.get("matched_contract") or requested.get("label"),
        "alternative_label": alternatives.get("best_label"),
        "requested_readiness_score": _clean_value(req_readiness),
        "alternative_readiness_score": _clean_value(alt_readiness),
        "requested_spread_pct": _clean_value(req_spread),
        "alternative_spread_pct": _clean_value(alt_spread),
        "requested_premium_dollars": _clean_value(req_premium),
        "alternative_premium_dollars": _clean_value(alt_premium),
        "readiness_delta": round(readiness_delta, 2),
        "swing_fit_delta": round(swing_delta, 2),
        "spread_delta": _clean_value(None if spread_delta is None else round(spread_delta, 4)),
        "premium_delta_pct": _clean_value(
            None if premium_delta_pct is None else round(premium_delta_pct, 4)
        ),
        "reasons": list(dict.fromkeys(reasons))[:6],
    }


def _best_idea_dict(section: str | None, row: pd.Series | None) -> dict[str, Any] | None:
    if row is None or section is None:
        return None
    symbol = row.get("ticker", row.get("symbol"))
    side = row.get("side", row.get("direction", section))
    label = str(symbol or "-")
    asset = section.rstrip("s")
    if section in {"options", "chain_shortlist"}:
        asset = "option"
        label = f"{symbol} {str(side).upper()[:1]} {row.get('strike', '-')} {row.get('expiry', '-')}"
    elif section == "futures":
        label = f"{symbol} {str(side).upper()} {row.get('contract', '')}".strip()
    quote_source = _quote_source_info(row)
    return {
        "asset": asset,
        "label": label,
        "trade_status": _clean_value(row.get("trade_status")),
        "confidence": _clean_value(row.get("confidence")),
        "score": _score_row(row),
        "entry_price": _clean_value(row.get("mid", row.get("spot", row.get("entry_price")))),
        "stop_price": _clean_value(row.get("stop_price")),
        "target_price": _clean_value(row.get("target_price")),
        "spread_pct": _clean_value(row.get("spread_pct")),
        "ev_pct": _clean_value(row.get("ev_pct")),
        "net_edge_pct": _clean_value(row.get("net_edge_pct")),
        "suggested_contracts": _clean_value(row.get("suggested_contracts")),
        "suggested_dollars": _clean_value(row.get("suggested_dollars")),
        "chain_source": _clean_value(row.get("chain_source")),
        "quote_quality": _clean_value(row.get("quote_quality")),
        "quote_source": quote_source,
        "quote_source_label": (quote_source or {}).get("label"),
        "quote_source_warning": (quote_source or {}).get("warning"),
        "source_file": _clean_value(row.get("_source_file")),
        "snapshot_age_min": _clean_value(row.get("snapshot_age_min")),
        "snapshot_freshness": _clean_value(row.get("snapshot_freshness")),
        "contract_grade": _clean_value(row.get("contract_grade")),
        "review_lane": _clean_value(row.get("review_lane")),
        "readiness_score": _clean_value(row.get("readiness_score")),
        "headline": _clean_value(row.get("top_headline")),
    }


def _status_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _request_chain_scan_fields(symbol: str, request: dict[str, Any] | None) -> dict[str, Any]:
    request = request or {}
    chain_symbol = str(request.get("ticker") or symbol or "").strip().upper()
    side = str(request.get("side") or "all").strip().lower()
    if side.startswith("c"):
        side = "call"
    elif side.startswith("p"):
        side = "put"
    else:
        side = "all"

    expiry = str(request.get("expiry") or "").strip()[:10]
    dte: int | None = None
    if expiry:
        try:
            expiry_dt = datetime.fromisoformat(expiry).date()
            dte = (expiry_dt - datetime.now(timezone.utc).date()).days
        except ValueError:
            dte = None

    if dte is None:
        min_dte = 90
        max_dte = 900
    else:
        min_dte = max(0, int(dte) - 7)
        max_dte = max(min_dte + 14, int(dte) + 7)

    return {
        "chain_symbol": chain_symbol,
        "chain_side": side,
        "chain_target_expiry": expiry or None,
        "chain_min_dte": min_dte,
        "chain_max_dte": min(max_dte, 1200),
    }


def _research_action(
    symbol: str,
    best_idea: dict[str, Any] | None,
    open_summary: dict[str, Any],
    broker_summary: dict[str, Any],
    request: dict[str, Any] | None,
    requested_option: dict[str, Any] | None,
    contract_exposure: dict[str, Any] | None,
    warnings: list[str],
    total_hits: int,
) -> dict[str, Any]:
    """Conservative next-step guidance for a lookup screen."""
    reasons: list[str] = []
    next_steps: list[str] = []
    risk_level = "low"
    action = "review"
    label = "Review local research"
    requested_quality = str((requested_option or {}).get("match_quality") or "").strip().lower()
    requested_label = str((requested_option or {}).get("label") or symbol).strip()
    chain_fields = _request_chain_scan_fields(symbol, request)

    if total_hits <= 0:
        if requested_option:
            return {
                "action": "scan_swing_chain",
                "route": "chains",
                "label": "Scan option chain",
                "risk_level": "unknown",
                "reasons": [f"No current local rows matched requested option {requested_label}."],
                "next_steps": [
                    "Run the option-chain scanner for the requested ticker, side, and expiration window.",
                    "Use only exact or clearly reviewed contracts; avoid treating a ticker-only result as a contract match.",
                ],
                "can_export_paper_candidate": False,
                **chain_fields,
            }
        return {
            "action": "run_focused_scan",
            "route": "research",
            "label": "Run focused scan",
            "risk_level": "unknown",
            "reasons": [f"No current local rows were found for {symbol}."],
            "next_steps": [
                "Run a focused scan from the cockpit before making any judgment.",
                "Avoid using stale dashboard rows from another symbol as a substitute.",
            ],
            "can_export_paper_candidate": False,
        }

    warning_text = " ".join(str(w).lower() for w in warnings)
    blocked = "blocked" in warning_text or "do not trust" in warning_text
    sample_small = "sample size" in warning_text or "too small" in warning_text
    open_count = int(open_summary.get("count") or 0)
    max_pressure = _float_value(open_summary.get("max_exit_pressure"), 0.0) or 0.0
    avg_unreal = _float_value(open_summary.get("avg_unrealized_pct"))
    broker_count = int(broker_summary.get("count") or 0)
    exact_contract_count = int((contract_exposure or {}).get("exact_total") or 0)
    status = _status_text((best_idea or {}).get("trade_status"))

    if open_count > 0:
        reasons.append(f"{open_count} open lifecycle position(s) already exist for {symbol}.")
    if broker_count > 0:
        reasons.append(f"{broker_count} broker snapshot position(s) already exist for {symbol}.")
    if exact_contract_count > 0:
        action = "review_existing_contract"
        label = "Review existing contract"
        risk_level = "medium"
        reasons.append(f"The requested exact option contract already has {exact_contract_count} open exposure row(s).")
        next_steps.append("Review or manage the existing exact contract before considering any new entry.")
    if max_pressure >= 80:
        action = "review_exit_now"
        label = "Review exit now"
        risk_level = "high"
        reasons.append(f"Open-position exit pressure is high ({max_pressure:.0f}/100).")
        next_steps.append("Check the open position monitor before adding anything new.")
    elif max_pressure >= 60:
        action = "tighten_or_watch"
        label = "Tighten or watch"
        risk_level = "medium"
        reasons.append(f"Open-position exit pressure is elevated ({max_pressure:.0f}/100).")
        next_steps.append("Review stop/target and thesis deterioration before sizing new exposure.")

    if blocked:
        action = "blocked_by_guardrails"
        label = "Blocked by guardrails"
        risk_level = "high"
        reasons.append("Research guardrail warnings include blocked/do-not-trust language.")
        next_steps.append("Do not paper-export this idea until validation/guardrail warnings clear.")
    elif sample_small:
        risk_level = "medium" if risk_level == "low" else risk_level
        reasons.append("Validation sample-size warning is active.")
        next_steps.append("Treat any signal as early research until more closed outcomes exist.")

    if (
        requested_option
        and requested_quality in {"missing", "closest", "ticker_only"}
        and action not in {"blocked_by_guardrails", "review_existing_contract", "review_exit_now"}
    ):
        action = "scan_swing_chain"
        label = "Scan option chain"
        risk_level = "medium" if risk_level == "low" else risk_level
        if requested_quality == "missing":
            reasons.append(f"Requested option {requested_label} was not found in current local rows.")
        elif requested_quality == "closest":
            reasons.append(f"Requested option {requested_label} only has a closest-contract match.")
        else:
            reasons.append(f"Requested option {requested_label} only has same-ticker option context.")
        next_steps.insert(0, "Run the option-chain scanner around the requested contract before judging the setup.")

    if best_idea:
        reasons.append(f"Best local idea status is {best_idea.get('trade_status') or 'unknown'}.")
        snapshot_age = _float_value(best_idea.get("snapshot_age_min"))
        if snapshot_age is not None and snapshot_age > STALE_SNAPSHOT_MINUTES:
            risk_level = "medium" if risk_level == "low" else risk_level
            reasons.append(f"Best local idea comes from a stale snapshot ({snapshot_age:.0f} minutes old).")
            next_steps.append("Run a fresh focused scan before using this as a paper candidate.")
        quote_label = best_idea.get("quote_source_label")
        quote_warning = best_idea.get("quote_source_warning")
        if quote_warning:
            risk_level = "medium" if risk_level == "low" else risk_level
            reasons.append(str(quote_warning))
            next_steps.append("Verify the latest option bid/ask before paper tracking or sizing.")
        elif quote_label:
            reasons.append(f"Option quote source: {quote_label}.")
        if action == "review" and status in {"trade", "actionable", "buy", "long", "short"}:
            action = "paper_candidate_review"
            label = "Candidate for paper review"
            next_steps.append("Review spread, sizing, stop/target, and guardrails before paper tracking.")
        elif action == "review" and status in {"watch", "skip", "blocked"}:
            action = "watch_only"
            label = "Watch only"
            risk_level = "medium" if status == "watch" else "high"
            next_steps.append("Keep this on the research watchlist unless a fresh scan upgrades it.")
    elif action == "review":
        action = "watchlist_or_rescan"
        label = "Watchlist or rescan"
        reasons.append("No ranked current idea was found, only position or historical context.")
        next_steps.append("Add it to the watchlist or run a focused scan for a current ranked view.")

    if broker_count > 0 and action not in {"review_exit_now", "blocked_by_guardrails", "review_existing_contract"}:
        action = "review_broker_position"
        label = "Review broker position"
        risk_level = "medium" if risk_level == "low" else risk_level
        next_steps.insert(0, "Reconcile Robinhood broker snapshot against local Optedge open positions before adding exposure.")
        if open_count <= 0:
            reasons.append("Broker exposure exists but no matching local open position was found in this lookup.")

    if avg_unreal is not None and open_count > 0:
        reasons.append(f"Average open unrealized P&L is {avg_unreal * 100:+.1f}%.")

    if not next_steps:
        next_steps.append("Read the factor drivers and open exposure before making any manual decision.")

    can_export = action == "paper_candidate_review" and risk_level != "high" and broker_count <= 0
    result = {
        "action": action,
        "route": "chains" if action == "scan_swing_chain" else "research",
        "label": label,
        "risk_level": risk_level,
        "reasons": list(dict.fromkeys(reasons))[:6],
        "next_steps": list(dict.fromkeys(next_steps))[:5],
        "can_export_paper_candidate": can_export,
    }
    if action == "scan_swing_chain":
        result.update(chain_fields)
    return result


def _paper_readiness(
    best_idea: dict[str, Any] | None,
    requested_option: dict[str, Any] | None,
    open_summary: dict[str, Any],
    broker_summary: dict[str, Any],
    contract_exposure: dict[str, Any] | None,
    warnings: list[str],
    action: dict[str, Any],
    total_hits: int,
) -> dict[str, Any]:
    """Conservative manual paper-review readiness checklist."""
    score = 100
    checks: list[dict[str, Any]] = []

    def add(level: str, label: str, detail: str, penalty: int = 0) -> None:
        nonlocal score
        score -= max(0, int(penalty))
        checks.append({"level": level, "label": label, "detail": detail, "penalty": penalty})

    if total_hits <= 0:
        add("bad", "No local rows", "Run a focused scan before treating this as a candidate.", 60)
    if not best_idea:
        add("bad", "No ranked idea", "No current ranked local idea was found.", 45)
    else:
        status = _status_text(best_idea.get("trade_status"))
        if status in {"watch", "skip", "blocked"}:
            add("warn" if status == "watch" else "bad", "Trade status", f"Best idea status is {status}.", 30)
        else:
            add("ok", "Trade status", f"Best idea status is {best_idea.get('trade_status') or 'unknown'}.")

        freshness = str(best_idea.get("snapshot_freshness") or "unknown").lower()
        age = _float_value(best_idea.get("snapshot_age_min"))
        if freshness == "stale":
            add("bad", "Snapshot freshness", f"Snapshot is stale ({age or 0:.0f} minutes old).", 30)
        elif freshness == "aging":
            add("warn", "Snapshot freshness", f"Snapshot is aging ({age or 0:.0f} minutes old).", 10)
        else:
            add("ok", "Snapshot freshness", f"Snapshot freshness is {freshness}.")

        quote_warning = best_idea.get("quote_source_warning")
        quote_label = best_idea.get("quote_source_label") or "unknown"
        if quote_warning:
            add("warn", "Quote source", str(quote_warning), 15)
        else:
            add("ok", "Quote source", f"Quote source: {quote_label}.")

    if requested_option:
        quality = str(requested_option.get("match_quality") or "missing").lower()
        if quality == "exact":
            add("ok", "Requested option match", "Requested contract matched exactly.")
        elif quality == "closest":
            add("warn", "Requested option match", "Only a closest contract match was found.", 25)
        elif quality == "ticker_only":
            add("warn", "Requested option match", "Only ticker-level option rows matched.", 35)
        else:
            add("bad", "Requested option match", "Requested option was not found.", 45)

    exact_contract_count = int((contract_exposure or {}).get("exact_total") or 0)
    same_ticker_count = int((contract_exposure or {}).get("same_ticker_total") or 0)
    if exact_contract_count > 0:
        add(
            "warn",
            "Exact contract exposure",
            f"{exact_contract_count} open local/broker row(s) already match this exact contract.",
            30,
        )
    elif same_ticker_count > 0:
        add(
            "warn",
            "Same ticker option exposure",
            f"{same_ticker_count} open option row(s) already exist for this ticker.",
            15,
        )

    max_pressure = _float_value(open_summary.get("max_exit_pressure"), 0.0) or 0.0
    if max_pressure >= 80:
        add("bad", "Open exposure", f"Existing position exit pressure is high ({max_pressure:.0f}/100).", 35)
    elif max_pressure >= 60:
        add("warn", "Open exposure", f"Existing position exit pressure is elevated ({max_pressure:.0f}/100).", 20)
    else:
        add("ok", "Open exposure", "No high exit-pressure open position conflict surfaced.")

    broker_count = int(broker_summary.get("count") or 0)
    if broker_count > 0:
        if int(open_summary.get("count") or 0) <= 0:
            add("warn", "Broker exposure", "Robinhood snapshot has exposure that is not matched by local open positions.", 30)
        else:
            add("warn", "Broker exposure", "Robinhood snapshot already has exposure for this symbol.", 20)

    warning_text = " ".join(str(w).lower() for w in warnings)
    if "blocked" in warning_text or "do not trust" in warning_text:
        add("bad", "Guardrails", "Guardrail warning is active.", 45)
    elif "sample size" in warning_text or "too small" in warning_text:
        add("warn", "Validation sample", "Validation sample-size warning is active.", 15)
    elif warnings:
        add("warn", "Warnings", "Lookup has active warnings to review.", 10)
    else:
        add("ok", "Warnings", "No lookup warnings surfaced.")

    if action.get("risk_level") == "high":
        add("bad", "Action risk", "Research action risk is high.", 30)
    elif action.get("can_export_paper_candidate"):
        add("ok", "Paper export", "Candidate can be considered for manual paper review.")
    else:
        add("warn", "Paper export", "Candidate is not currently marked as paper-export ready.", 15)

    score = max(0, min(100, score))
    if any(row["level"] == "bad" for row in checks) or score < 45:
        status = "blocked"
        label = "Needs fresh review"
    elif score < 75:
        status = "caution"
        label = "Caution"
    else:
        status = "ready"
        label = "Manual paper review ready"
    return {
        "score": score,
        "status": status,
        "label": label,
        "checks": checks[:10],
    }


def _setup_bias(request: dict[str, Any] | None, best_idea: dict[str, Any] | None) -> str:
    text = " ".join(
        str(x or "").lower()
        for x in [
            (request or {}).get("side"),
            (request or {}).get("asset"),
            (best_idea or {}).get("asset"),
            (best_idea or {}).get("label"),
        ]
    )
    padded = f" {text.replace('-', ' ')} "
    if " put" in padded or " p " in padded or " short" in padded:
        return "bearish"
    if " call" in padded or " c " in padded or " long" in padded or "share" in padded or "option" in padded:
        return "bullish"
    return "neutral"


def _risk_reward(entry: Any, stop: Any, target: Any) -> float | None:
    entry_v = _float_value(entry)
    stop_v = _float_value(stop)
    target_v = _float_value(target)
    if entry_v is None or stop_v is None or target_v is None:
        return None
    risk = abs(entry_v - stop_v)
    reward = abs(target_v - entry_v)
    if risk <= 0:
        return None
    return reward / risk


def _swing_verdict(
    request: dict[str, Any] | None,
    best_idea: dict[str, Any] | None,
    requested_option: dict[str, Any] | None,
    price_snapshot: dict[str, Any] | None,
    open_summary: dict[str, Any],
    broker_summary: dict[str, Any],
    market_structure: dict[str, Any],
    cboe_activity: dict[str, Any],
    data_coverage: dict[str, Any],
    readiness: dict[str, Any],
    action: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    """Condense the lookup into a conservative swing-trade research verdict."""
    score = 50
    reasons: list[str] = []
    blockers: list[str] = []

    action_name = str(action.get("action") or "").lower()
    action_risk = str(action.get("risk_level") or "").lower()
    readiness_status = str(readiness.get("status") or "").lower()
    coverage_score = _float_value(data_coverage.get("score"))
    market_status = str(market_structure.get("status") or "").lower()
    market_risk = _float_value(market_structure.get("risk_score"), 0.0) or 0.0
    bias = _setup_bias(request, best_idea)

    if best_idea:
        status = _status_text(best_idea.get("trade_status"))
        confidence = _float_value(best_idea.get("confidence"))
        idea_score = _float_value(best_idea.get("score"))
        if status in {"trade", "actionable", "buy", "long", "short"}:
            score += 12
            reasons.append(f"Best local idea is marked {best_idea.get('trade_status') or 'actionable'}.")
        elif status in {"watch", "skip", "blocked"}:
            score -= 18 if status == "watch" else 30
            reasons.append(f"Best local idea is only {status}.")
        if confidence is not None:
            score += max(-10, min(15, (confidence - 55.0) / 3.0))
            reasons.append(f"Local confidence is {confidence:.0f}/100.")
        elif idea_score is not None:
            score += max(-6, min(10, idea_score * 4.0))
        if best_idea.get("quote_source_warning"):
            score -= 8
            reasons.append(str(best_idea.get("quote_source_warning")))
        elif best_idea.get("quote_source_label"):
            score += 4
            reasons.append(f"Quote source: {best_idea.get('quote_source_label')}.")
        spread = _float_value(best_idea.get("spread_pct"))
        if spread is not None:
            if spread <= 0.15:
                score += 6
                reasons.append(f"Spread is usable at {spread * 100:.1f}%.")
            elif spread >= 0.35:
                score -= 14
                reasons.append(f"Spread is wide at {spread * 100:.1f}%.")
        edge = _float_value(best_idea.get("net_edge_pct") or best_idea.get("ev_pct"))
        if edge is not None:
            score += max(-10, min(12, edge * 35.0))
            reasons.append(f"Estimated edge is {edge * 100:+.1f}%.")
    else:
        score -= 28
        blockers.append("No current ranked local idea is available for this symbol.")

    if price_snapshot:
        trend = str(price_snapshot.get("trend_label") or "unknown")
        ret_20d = _float_value(price_snapshot.get("ret_20d"))
        if bias == "bullish" and trend in {"uptrend", "strong_uptrend"}:
            score += 8
            reasons.append(f"Price trend supports bullish swing bias ({trend}).")
        elif bias == "bearish" and trend in {"downtrend", "strong_downtrend"}:
            score += 8
            reasons.append(f"Price trend supports bearish swing bias ({trend}).")
        elif bias in {"bullish", "bearish"} and trend not in {"unknown", "rangebound"}:
            score -= 6
            reasons.append(f"Price trend conflicts with {bias} bias ({trend}).")
        if ret_20d is not None and abs(ret_20d) > 0.18:
            score -= 4
            reasons.append("20d move is extended; avoid chasing without a fresh trigger.")
    else:
        score -= 8
        reasons.append("No free price snapshot is attached to this lookup.")

    if requested_option:
        quality = str(requested_option.get("match_quality") or "missing").lower()
        if quality == "exact":
            score += 8
            reasons.append("Requested option contract matched exactly.")
        elif quality == "closest":
            score -= 10
            reasons.append("Only a closest-contract option match was found.")
        elif quality in {"ticker_only", "missing"}:
            score -= 18
            blockers.append("Requested option needs a focused chain scan before review.")

    activity_status = str(cboe_activity.get("status") or "").lower()
    if activity_status == "matched":
        score += 5
        reasons.append("Public Cboe activity matched the requested contract.")
    elif requested_option and activity_status == "no_exact_match":
        score -= 5
        reasons.append("No exact public Cboe activity matched the requested contract.")

    if market_status == "blocked":
        score -= 45
        blockers.append("Official market-structure check is blocked.")
    elif market_risk >= 80:
        score -= 20
        blockers.append("Official market-structure risk is high.")
    elif market_risk >= 40:
        score -= 8
        reasons.append("Market-structure risk needs review.")

    if coverage_score is not None:
        if coverage_score >= 80:
            score += 5
        elif coverage_score < 55:
            score -= 12
            reasons.append("Lookup data coverage is thin.")

    if int(open_summary.get("count") or 0) > 0:
        score -= 12
        reasons.append("Local open exposure already exists for this symbol.")
    if int(broker_summary.get("count") or 0) > 0:
        score -= 18
        reasons.append("Broker snapshot already has exposure for this symbol.")

    max_pressure = _float_value(open_summary.get("max_exit_pressure"), 0.0) or 0.0
    if max_pressure >= 80:
        score -= 35
        blockers.append("Existing open position has high exit pressure.")
    elif max_pressure >= 60:
        score -= 15
        reasons.append("Existing open position has elevated exit pressure.")

    if readiness_status == "ready":
        score += 7
    elif readiness_status == "blocked":
        score -= 20
        blockers.append("Paper-readiness checklist is blocked.")
    elif readiness_status == "caution":
        score -= 6

    warning_text = " ".join(str(w).lower() for w in warnings)
    if "sample size" in warning_text or "too small" in warning_text:
        score -= 6
        reasons.append("Validation sample-size warning is active.")
    if action_risk == "high" or action_name in {"blocked_by_guardrails", "review_exit_now"}:
        score -= 30
        blockers.append(f"Research action is {action.get('label') or action_name}.")

    score = int(max(0, min(100, round(score))))
    if blockers:
        decision = "blocked" if action_name != "scan_swing_chain" else "scan_chain"
        label = "Blocked / needs review" if decision == "blocked" else "Scan chain first"
    elif action_name == "scan_swing_chain":
        decision = "scan_chain"
        label = "Scan chain first"
    elif action_name in {"review_existing_contract", "review_broker_position", "tighten_or_watch"}:
        decision = "manage_existing"
        label = "Manage existing exposure"
    elif action_name == "paper_candidate_review" and readiness_status == "ready" and score >= 75:
        decision = "paper_review"
        label = "High-quality paper review"
    elif score >= 65:
        decision = "selective_review"
        label = "Selective swing review"
    elif score >= 45:
        decision = "watch"
        label = "Watchlist / wait"
    else:
        decision = "fresh_scan"
        label = "Fresh scan needed"

    rr = _risk_reward(
        (best_idea or {}).get("entry_price"),
        (best_idea or {}).get("stop_price"),
        (best_idea or {}).get("target_price"),
    )
    playbook = {
        "blocked": "Do not add exposure until blockers clear.",
        "scan_chain": "Run the 3m+ option-chain scan before judging the contract.",
        "manage_existing": "Review existing local/broker exposure and exits first.",
        "paper_review": "Eligible for manual paper review after quote/spread verification.",
        "selective_review": "Review manually; size small unless fresh data improves.",
        "watch": "Keep on watchlist and wait for a cleaner trigger.",
        "fresh_scan": "Run a fresh focused scan before making a decision.",
    }.get(decision, "Review manually.")

    return {
        "score": score,
        "label": label,
        "decision": decision,
        "bias": bias,
        "playbook": playbook,
        "entry_price": (best_idea or {}).get("entry_price"),
        "stop_price": (best_idea or {}).get("stop_price"),
        "target_price": (best_idea or {}).get("target_price"),
        "risk_reward": _clean_value(None if rr is None else round(rr, 2)),
        "blockers": list(dict.fromkeys(blockers))[:5],
        "reasons": list(dict.fromkeys(reasons))[:8],
    }


def _load_json_obj(path: Path) -> dict[str, Any]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _research_brief(
    symbol: str,
    resolution: dict[str, Any],
    raw_matches: dict[str, pd.DataFrame],
    sections: dict[str, list[dict[str, Any]]],
    data_dir: Path,
    local_hit_count: int,
) -> dict[str, Any]:
    best_section, best = _best_row(raw_matches)
    requested_rows = sections.get("requested_option_matches", [])
    if resolution.get("request") and requested_rows:
        best_section = "options"
        best = pd.Series(requested_rows[0])
    drivers = _factor_drivers(best)
    open_rows = (
        sections.get("open_options", [])
        + sections.get("open_shares", [])
        + sections.get("open_futures", [])
    )
    open_option_rows = _all_frame_records(raw_matches.get("open_options"))
    broker_rows = sections.get("broker_positions", [])
    sec_rows = sections.get("recent_sec_filings", [])
    sec_facts = sections.get("sec_companyfacts", [])
    sec_fact_report = sections.get("_sec_companyfacts_report", [])
    price_rows = sections.get("price_snapshot", [])
    price_snapshot = price_rows[0] if price_rows else None
    market_structure = sections.get("_market_structure_report", [{}])[0] or {}
    cboe_activity = sections.get("_cboe_activity_report", [{}])[0] or {}
    data_coverage = sections.get("_data_coverage_report", [{}])[0] or {}
    sec_metrics = sec_fact_report[0].get("metrics", {}) if sec_fact_report else {}
    sec_fact_signals = sec_fact_report[0].get("watch_signals", []) if sec_fact_report else []
    validation = _load_json_obj(data_dir / "validation_summary.json")
    guard = _load_json_obj(data_dir / "research_guard_report.json") or _load_json_obj(data_dir / "research_guard.json")
    warnings = []
    warnings.extend(str(w) for w in (validation.get("warnings") or [])[:3])
    warnings.extend(
        str(w.get("message", w)) for w in (guard.get("warnings") or [])[:3]
        if isinstance(w, (dict, str))
    )
    warnings.extend(f"SEC companyfacts: {signal}" for signal in sec_fact_signals[:3])
    warnings.extend(str(w) for w in (market_structure.get("warnings") or [])[:5])
    if cboe_activity and cboe_activity.get("status") == "no_exact_match":
        warnings.append(str(cboe_activity.get("note") or "No exact public Cboe activity matched the requested option."))
    warnings.extend(str(w) for w in (data_coverage.get("warnings") or [])[:2])
    best_idea = _best_idea_dict(best_section, best)
    requested_option = _requested_option_summary(
        resolution.get("request"),
        sections.get("requested_option_matches", []),
    )
    option_alternatives = sections.get("option_alternatives", [])
    best_alternative = option_alternatives[0] if option_alternatives else {}
    alternative_summary = {
        "count": len(option_alternatives),
        "best_label": (
            f"{best_alternative.get('ticker')} {str(best_alternative.get('side') or '').upper()[:1]} "
            f"{best_alternative.get('strike')} {best_alternative.get('expiry')}"
            if best_alternative else None
        ),
        "best_reason": best_alternative.get("alternative_reason") if best_alternative else None,
        "best_score": best_alternative.get("alternative_score") if best_alternative else None,
        "best_readiness_score": best_alternative.get("readiness_score") if best_alternative else None,
        "best_swing_fit_score": best_alternative.get("swing_fit_score") if best_alternative else None,
        "best_spread_pct": best_alternative.get("spread_pct") if best_alternative else None,
        "best_mid": best_alternative.get("mid") if best_alternative else None,
        "best_premium_dollars": (
            best_alternative.get("premium_dollars", best_alternative.get("actual_dollars"))
            if best_alternative else None
        ),
        "best_quote_quality": best_alternative.get("quote_quality") if best_alternative else None,
    }
    contract_comparison = _contract_comparison(requested_option, alternative_summary)
    contract_exposure = _contract_exposure_summary(
        resolution.get("request"), open_option_rows, broker_rows
    )
    if requested_option:
        quality = str(requested_option.get("match_quality") or "missing").lower()
        if quality == "missing":
            warnings.append(f"Requested option {requested_option.get('label')} was not found in latest local option rows.")
        elif quality != "exact":
            warnings.append(
                f"Requested option {requested_option.get('label')} matched as {quality}; verify before using it."
            )
        if quality != "exact" and best_alternative:
            warnings.append("Nearby chain alternatives exist; compare them before acting on the requested contract.")
    if contract_comparison.get("winner") == "alternative":
        warnings.append("Best nearby contract looks cleaner than the requested contract on local comparison.")
    if contract_exposure and int(contract_exposure.get("exact_total") or 0) > 0:
        warnings.append(
            f"Requested option already has {contract_exposure.get('exact_total')} exact local/broker exposure row(s)."
        )
    if best_idea:
        snapshot_age = _float_value(best_idea.get("snapshot_age_min"))
        if snapshot_age is not None and snapshot_age > STALE_SNAPSHOT_MINUTES:
            warnings.append(
                f"Best idea snapshot is stale ({snapshot_age:.0f} minutes old); run a fresh focused scan."
            )
    open_summary = _open_position_summary(open_rows)
    broker_summary = _broker_position_summary(broker_rows)
    if broker_summary.get("count") and not open_summary.get("count"):
        warnings.append(
            f"Broker snapshot has {broker_summary.get('count')} position(s) for {symbol}, but no matching local open position was found."
        )
    if broker_summary.get("snapshot_freshness") == "stale":
        warnings.append("Broker snapshot is stale; refresh the Robinhood read-only snapshot before acting.")
    deduped_warnings = list(dict.fromkeys(warnings))[:5]
    research_action = _research_action(
        symbol, best_idea, open_summary, broker_summary, resolution.get("request"),
        requested_option, contract_exposure,
        deduped_warnings, local_hit_count
    )
    paper_readiness = _paper_readiness(
        best_idea, requested_option, open_summary, broker_summary, contract_exposure,
        deduped_warnings, research_action, local_hit_count,
    )
    swing_verdict = _swing_verdict(
        resolution.get("request"), best_idea, requested_option, price_snapshot,
        open_summary, broker_summary, market_structure, cboe_activity, data_coverage,
        paper_readiness, research_action, deduped_warnings,
    )
    brief = {
        "symbol": symbol,
        "resolved_from": resolution.get("query"),
        "resolution_source": resolution.get("source"),
        "request": resolution.get("request"),
        "requested_option": requested_option,
        "option_alternatives": alternative_summary,
        "contract_comparison": contract_comparison,
        "best_idea": best_idea,
        "contract_exposure": contract_exposure,
        "price_snapshot": price_snapshot,
        "data_coverage": {
            "status": data_coverage.get("status"),
            "label": data_coverage.get("label"),
            "score": data_coverage.get("score"),
            "hit_count": data_coverage.get("hit_count"),
            "warn_count": data_coverage.get("warn_count"),
            "bad_count": data_coverage.get("bad_count"),
            "checked_layers": data_coverage.get("checked_layers"),
            "warnings": (data_coverage.get("warnings") or [])[:4],
        },
        "market_structure": {
            "status": market_structure.get("status"),
            "risk_score": market_structure.get("risk_score"),
            "flags": market_structure.get("flags") or [],
            "warning_count": market_structure.get("warning_count") or 0,
            "warnings": (market_structure.get("warnings") or [])[:5],
        },
        "cboe_option_activity": {
            "status": cboe_activity.get("status"),
            "label": cboe_activity.get("label"),
            "source": cboe_activity.get("source"),
            "source_row_count": cboe_activity.get("source_row_count"),
            "exact_match_count": cboe_activity.get("exact_match_count"),
            "total_volume": cboe_activity.get("total_volume"),
            "matched_contract": cboe_activity.get("matched_contract"),
            "bid": cboe_activity.get("bid"),
            "ask": cboe_activity.get("ask"),
            "mid": cboe_activity.get("mid"),
            "spread_pct": cboe_activity.get("spread_pct"),
            "last": cboe_activity.get("last"),
            "venues": cboe_activity.get("venues"),
            "note": cboe_activity.get("note"),
        },
        "open_positions": open_summary,
        "broker_positions": broker_summary,
        "recent_sec_filings": {
            "count": len(sec_rows),
            "latest_forms": [row.get("form") for row in sec_rows[:5]],
            "watch_signals": list(dict.fromkeys(
                str(row.get("filing_signal")) for row in sec_rows
                if row.get("filing_signal")
            ))[:5],
        },
        "sec_fundamentals": {
            "count": len(sec_facts),
            "watch_signals": sec_fact_signals[:5],
            "cash": sec_metrics.get("cash"),
            "debt": sec_metrics.get("debt"),
            "assets": sec_metrics.get("assets"),
            "liabilities_to_assets": sec_metrics.get("liabilities_to_assets"),
            "debt_to_assets": sec_metrics.get("debt_to_assets"),
            "cash_to_debt": sec_metrics.get("cash_to_debt"),
            "net_margin": sec_metrics.get("net_margin"),
            "cash_per_share": sec_metrics.get("cash_per_share"),
        },
        "top_positive_factors": drivers["positive"],
        "top_negative_factors": drivers["negative"],
        "risk_warnings": deduped_warnings,
        "research_action": research_action,
        "paper_readiness": paper_readiness,
        "swing_verdict": swing_verdict,
        "validation": {
            "scope": validation.get("validation_scope"),
            "closed_positions": validation.get("closed_positions"),
            "open_positions": validation.get("open_positions"),
            "win_rate": (validation.get("overall") or {}).get("win_rate"),
            "avg_return": (validation.get("overall") or {}).get("avg_return"),
        },
    }
    return brief


def _norm_side(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"c", "call", "calls"}:
        return "call"
    if raw in {"p", "put", "puts"}:
        return "put"
    return raw


def _norm_expiry(value: Any) -> str:
    if value is None:
        return ""
    try:
        ts = pd.to_datetime(value, errors="coerce")
        if not pd.isna(ts):
            return ts.strftime("%Y-%m-%d")
    except Exception:
        pass
    return str(value).strip()[:10]


def _sort_option_matches(df: pd.DataFrame) -> pd.DataFrame:
    sort_cols: list[str] = []
    ascending: list[bool] = []
    for col, asc in [
        ("_side_match", False),
        ("_expiry_match", False),
        ("strike_diff", True),
        ("rank_score", False),
        ("confidence", False),
        ("fused_score", False),
    ]:
        if col in df.columns:
            sort_cols.append(col)
            ascending.append(asc)
    if not sort_cols:
        return df
    return df.sort_values(sort_cols, ascending=ascending, kind="mergesort")


def match_option_request(
    request: dict[str, Any] | None,
    data_dir: Path = DATA_DIR,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Find the exact or closest latest option rows for an option-style query."""
    if not request or request.get("asset") != "option":
        return []
    path = _latest_file(data_dir, "top_options_*.parquet")
    top_df = _read_parquet(path)
    if not top_df.empty:
        top_df = top_df.copy()
        top_df["match_source"] = "top_options"
    chain_df = _chain_shortlist_frame(data_dir)
    if not chain_df.empty:
        chain_df = chain_df.copy()
        chain_df["match_source"] = "option_chain_shortlist"
    frames = [df for df in (top_df, chain_df) if df is not None and not df.empty]
    df = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()
    if df.empty or "ticker" not in df.columns:
        return []

    ticker = str(request.get("ticker") or "").upper().strip()
    side = _norm_side(request.get("side"))
    expiry = _norm_expiry(request.get("expiry"))
    try:
        strike = float(request.get("strike"))
    except Exception:
        strike = math.nan

    candidates = _match(df, "ticker", ticker)
    if candidates.empty:
        return []

    out = candidates.copy()
    if "side" in out.columns:
        out["_side_norm"] = out["side"].map(_norm_side)
        out["_side_match"] = out["_side_norm"] == side
    else:
        out["_side_match"] = False
    if "expiry" in out.columns:
        out["_expiry_norm"] = out["expiry"].map(_norm_expiry)
        out["_expiry_match"] = out["_expiry_norm"] == expiry
    else:
        out["_expiry_match"] = False
    if "strike" in out.columns and math.isfinite(strike):
        out["strike_diff"] = (pd.to_numeric(out["strike"], errors="coerce") - strike).abs()
    else:
        out["strike_diff"] = math.nan

    exact = out[out["_side_match"] & out["_expiry_match"]].copy()
    if exact.empty:
        exact = out[out["_side_match"]].copy()
    if exact.empty:
        exact = out
    exact = _sort_option_matches(exact).head(limit).copy()
    exact["requested_side"] = side
    exact["requested_expiry"] = expiry
    exact["requested_strike"] = strike if math.isfinite(strike) else None
    exact["match_quality"] = exact.apply(
        lambda row: (
            "exact"
            if bool(row.get("_side_match")) and bool(row.get("_expiry_match"))
            and float(row.get("strike_diff") or 0) == 0
            else "closest"
            if bool(row.get("_side_match")) or bool(row.get("_expiry_match"))
            else "ticker_only"
        ),
        axis=1,
    )
    return _frame_records(exact, "requested_option_matches")


def _alternative_reason(row: pd.Series) -> str:
    pieces: list[str] = []
    if bool(row.get("_expiry_match")):
        pieces.append("same expiry")
    else:
        dte_diff = _float_value(row.get("dte_diff"))
        if dte_diff is not None:
            pieces.append(f"{dte_diff:.0f}d expiry offset")
        else:
            pieces.append("nearby expiry")

    strike_diff = _float_value(row.get("strike_diff"))
    if strike_diff is not None:
        pieces.append(f"{strike_diff:g} strike away")

    readiness = _float_value(row.get("readiness_score"))
    if readiness is not None and readiness >= 80:
        pieces.append("high readiness")

    swing_fit = _float_value(row.get("swing_fit_score"))
    if swing_fit is not None and swing_fit >= 80:
        pieces.append("clean swing fit")

    spread = _float_value(row.get("spread_pct"))
    if spread is not None and spread <= 0.12:
        pieces.append("controlled spread")

    return ", ".join(pieces[:5])


def option_alternatives_for_request(
    request: dict[str, Any] | None,
    data_dir: Path = DATA_DIR,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Rank nearby saved chain-shortlist contracts for an option-style query."""
    if not request or request.get("asset") != "option":
        return []

    chain_df = _chain_shortlist_frame(data_dir)
    if chain_df.empty or "ticker" not in chain_df.columns:
        return []

    ticker = str(request.get("ticker") or "").upper().strip()
    side = _norm_side(request.get("side"))
    expiry = _norm_expiry(request.get("expiry"))
    try:
        strike = float(request.get("strike"))
    except Exception:
        strike = math.nan

    out = _match(chain_df, "ticker", ticker)
    if out.empty:
        return []
    out = out.copy()

    if "side" in out.columns and side:
        out["_side_norm"] = out["side"].map(_norm_side)
        out = out[out["_side_norm"] == side].copy()
    if out.empty:
        return []

    out["_expiry_norm"] = out["expiry"].map(_norm_expiry) if "expiry" in out.columns else ""
    out["_expiry_match"] = out["_expiry_norm"] == expiry if expiry else False
    if "strike" in out.columns and math.isfinite(strike):
        out["strike_diff"] = (pd.to_numeric(out["strike"], errors="coerce") - strike).abs()
    else:
        out["strike_diff"] = math.nan

    if expiry:
        request_expiry = pd.to_datetime(expiry, errors="coerce")
        row_expiry = pd.to_datetime(out["_expiry_norm"], errors="coerce")
        if not pd.isna(request_expiry):
            out["dte_diff"] = (row_expiry - request_expiry).dt.days.abs()
        else:
            out["dte_diff"] = math.nan
    else:
        out["dte_diff"] = math.nan

    if math.isfinite(strike) and expiry:
        exact = out["_expiry_match"] & (pd.to_numeric(out["strike_diff"], errors="coerce") <= 0.0001)
        out = out[~exact].copy()
    if out.empty:
        return []

    def num_col(name: str, default: float = 0.0) -> pd.Series:
        if name not in out.columns:
            return pd.Series(default, index=out.index, dtype=float)
        return pd.to_numeric(out[name], errors="coerce").fillna(default)

    def text_col(name: str) -> pd.Series:
        if name not in out.columns:
            return pd.Series("", index=out.index, dtype=str)
        return out[name].astype(str)

    readiness = num_col("readiness_score")
    swing_fit = num_col("swing_fit_score")
    confidence = num_col("confidence")
    rank_score = num_col("rank_score")
    spread = num_col("spread_pct", 0.50).clip(lower=0.0, upper=1.0)
    oi = num_col("openInterest").clip(lower=0.0, upper=5000.0)
    volume = num_col("volume").clip(lower=0.0, upper=1000.0)
    strike_diff = pd.to_numeric(out["strike_diff"], errors="coerce").fillna(999.0).clip(lower=0.0, upper=100.0)
    dte_diff = pd.to_numeric(out["dte_diff"], errors="coerce").fillna(365.0).clip(lower=0.0, upper=365.0)
    trade_status = text_col("trade_status").str.lower()
    freshness = text_col("snapshot_freshness").str.lower()

    out["alternative_score"] = (
        readiness * 0.28
        + swing_fit * 0.24
        + confidence * 0.16
        + rank_score.clip(lower=0.0, upper=5.0) * 4.0
        + (1.0 - spread) * 14.0
        + (oi / 5000.0) * 8.0
        + (volume / 1000.0) * 6.0
        + out["_expiry_match"].astype(float) * 8.0
        + (1.0 - (strike_diff / 100.0)) * 5.0
        + (1.0 - (dte_diff / 365.0)) * 3.0
        + trade_status.isin({"trade", "actionable", "review"}).astype(float) * 5.0
        - freshness.eq("stale").astype(float) * 8.0
    ).round(2)
    out["alternative_reason"] = out.apply(_alternative_reason, axis=1)

    out = out.sort_values(
        ["alternative_score", "_expiry_match", "strike_diff", "dte_diff"],
        ascending=[False, False, True, True],
        kind="mergesort",
    ).head(limit)
    return _frame_records(out, "option_alternatives")


def _requested_cboe_option_activity(
    request: dict[str, Any] | None,
    limit: int = 5,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not request or request.get("asset") != "option":
        return [], {"status": "not_requested", "label": "Not requested"}

    ticker = str(request.get("ticker") or "").strip().upper()
    side = _norm_side(request.get("side"))
    expiry = _norm_expiry(request.get("expiry"))
    try:
        strike = float(request.get("strike"))
    except Exception:
        strike = math.nan
    if not ticker or not side or not expiry or not math.isfinite(strike):
        return [], {
            "status": "invalid_request",
            "label": "Cboe activity unavailable",
            "note": "Requested option is missing ticker, side, expiry, or strike.",
        }

    try:
        activity = cboe_symbol_data_engine.run([ticker], min_volume=1)
    except Exception as exc:
        return [], {
            "status": "unavailable",
            "label": "Cboe activity unavailable",
            "note": f"Public Cboe symbol activity lookup failed: {str(exc)[:120]}",
        }
    if activity is None or activity.empty:
        return [], {
            "status": "no_symbol_activity",
            "label": "No Cboe activity match",
            "source_row_count": 0,
            "exact_match_count": 0,
            "total_volume": 0,
            "note": "No public Cboe symbol activity rows were available for this ticker.",
        }

    work = activity.copy()
    ticker_col = work["ticker"] if "ticker" in work.columns else pd.Series("", index=work.index)
    side_col = work["option_side"] if "option_side" in work.columns else pd.Series("", index=work.index)
    expiry_col = work["expiry"] if "expiry" in work.columns else pd.Series("", index=work.index)
    strike_col = work["strike"] if "strike" in work.columns else pd.Series(math.nan, index=work.index)
    work["_side_norm"] = side_col.map(_norm_side)
    work["_expiry_norm"] = expiry_col.map(_norm_expiry)
    work["strike_diff"] = (pd.to_numeric(strike_col, errors="coerce") - strike).abs()
    exact = work[
        (ticker_col.astype(str).str.upper().str.strip() == ticker)
        & (work["_side_norm"] == side)
        & (work["_expiry_norm"] == expiry)
        & (pd.to_numeric(work["strike_diff"], errors="coerce").fillna(math.inf) <= 0.0001)
    ].copy()
    if exact.empty:
        return [], {
            "status": "no_exact_match",
            "label": "No exact Cboe activity match",
            "source_row_count": int(len(work)),
            "exact_match_count": 0,
            "total_volume": 0,
            "note": "No exact public Cboe activity matched this contract; this is not consolidated OPRA volume.",
        }

    for idx, row in exact.iterrows():
        bid = _float_value(row.get("cboe_activity_bid"))
        ask = _float_value(row.get("cboe_activity_ask"))
        if bid is not None and ask is not None and ask >= bid and (bid + ask) > 0:
            mid = (bid + ask) / 2.0
            exact.loc[idx, "cboe_activity_mid"] = round(mid, 4)
            exact.loc[idx, "cboe_activity_spread_pct"] = round((ask - bid) / mid, 4)
        exact.loc[idx, "match_quality"] = "exact"

    exact = exact.sort_values("cboe_activity_volume", ascending=False).head(limit).copy()
    total_volume = int(pd.to_numeric(exact["cboe_activity_volume"], errors="coerce").fillna(0).sum())
    best = exact.iloc[0]
    summary = {
        "status": "matched",
        "label": "Exact Cboe activity match",
        "source": "cboe_symbol_data",
        "source_row_count": int(len(work)),
        "exact_match_count": int(len(exact)),
        "total_volume": total_volume,
        "matched_contract": _clean_value(best.get("cboe_activity_contract")),
        "bid": _clean_value(best.get("cboe_activity_bid")),
        "ask": _clean_value(best.get("cboe_activity_ask")),
        "mid": _clean_value(best.get("cboe_activity_mid")),
        "spread_pct": _clean_value(best.get("cboe_activity_spread_pct")),
        "last": _clean_value(best.get("cboe_activity_last")),
        "venues": _clean_value(best.get("cboe_activity_venues")),
        "note": "Public Cboe venue activity matched the requested contract; not consolidated OPRA and not an execution quote.",
    }
    return _frame_records(exact, "cboe_option_activity"), summary


def lookup_symbol(
    query: str,
    data_dir: Path = DATA_DIR,
    include_sec: bool = True,
    include_price: bool = False,
    include_market_structure: bool = False,
    include_cboe_activity: bool = False,
) -> dict[str, Any]:
    original_query = query.strip()
    resolution = resolve_symbol(original_query)
    q = str(resolution.get("symbol") or original_query).strip().upper()
    generated_at = datetime.now(timezone.utc).isoformat()
    sections: dict[str, list[dict[str, Any]]] = {}
    sources: dict[str, str | None] = {}
    raw_matches: dict[str, pd.DataFrame] = {}

    for section, (pattern, column) in SNAPSHOTS.items():
        path = _latest_file(data_dir, pattern)
        sources[section] = path.name if path else None
        matched = _match(_read_parquet(path), column, q)
        raw_matches[section] = matched
        sections[section] = _frame_records(matched, section)

    chain_df = _chain_shortlist_frame(data_dir)
    chain_source = _clean_value(chain_df["_source_file"].iloc[0]) if not chain_df.empty and "_source_file" in chain_df.columns else None
    sources["chain_shortlist"] = str(chain_source) if chain_source else None
    chain_matched = _match(chain_df, "ticker", q)
    raw_matches["chain_shortlist"] = chain_matched
    sections["chain_shortlist"] = _frame_records(chain_matched, "chain_shortlist")

    for section, (filename, column) in OPEN_FILES.items():
        path = data_dir / filename
        sources[section] = filename if path.exists() else None
        matched = _match(_read_json_rows(path), column, q)
        raw_matches[section] = matched
        sections[section] = _frame_records(matched, section)

    broker_snapshot_path = data_dir / "robinhood_broker_snapshot.json"
    broker_rows = _broker_snapshot_positions(q, data_dir)
    sources["broker_positions"] = broker_snapshot_path.name if broker_snapshot_path.exists() else None
    sections["broker_positions"] = _frame_records(pd.DataFrame(broker_rows), "broker_positions")

    if include_price:
        price_snapshot = _price_snapshot(q)
        sources["price_snapshot"] = (
            str(price_snapshot.get("history_source") or "data_provider.get_history")
            if price_snapshot else None
        )
        sections["price_snapshot"] = [price_snapshot] if price_snapshot else []

    if include_market_structure:
        market_structure = _market_structure_snapshot(q)
        sections["_market_structure_report"] = [market_structure]
        sections["market_structure"] = market_structure.get("rows") or []
        sources["market_structure"] = "Nasdaq Trader halts / Reg SHO / short-sale circuit; SEC FTD"

    if include_sec and not q.endswith("=F") and not q.startswith("^"):
        try:
            sec_report = recent_filings_for_symbol(q, limit=8)
            sections["recent_sec_filings"] = sec_report.get("rows", [])
            sources["recent_sec_filings"] = "SEC EDGAR submissions API"
        except Exception as exc:
            sections["recent_sec_filings"] = []
            sources["recent_sec_filings"] = f"SEC EDGAR unavailable: {str(exc)[:120]}"
        try:
            facts_report = companyfacts_for_symbol(q, limit=12)
            sections["sec_companyfacts"] = facts_report.get("rows", [])
            sections["_sec_companyfacts_report"] = [facts_report]
            sources["sec_companyfacts"] = "SEC EDGAR companyfacts API"
        except Exception as exc:
            sections["sec_companyfacts"] = []
            sections["_sec_companyfacts_report"] = []
            sources["sec_companyfacts"] = f"SEC companyfacts unavailable: {str(exc)[:120]}"

    if resolution.get("request"):
        sections["requested_option_matches"] = match_option_request(
            resolution.get("request"), data_dir
        )
        sections["option_alternatives"] = option_alternatives_for_request(
            resolution.get("request"), data_dir
        )
        sources["requested_option_matches"] = ", ".join(
            source for source in [sources.get("options"), sources.get("chain_shortlist")]
            if source
        ) or None
        sources["option_alternatives"] = sources.get("chain_shortlist")
        if include_cboe_activity:
            cboe_rows, cboe_report = _requested_cboe_option_activity(resolution.get("request"))
            sections["cboe_option_activity"] = cboe_rows
            sections["_cboe_activity_report"] = [cboe_report]
            sources["cboe_option_activity"] = "Cboe public option symbol activity"

    coverage_rows, coverage_report = _build_data_coverage(
        q,
        sections,
        sources,
        include_price=include_price,
        include_market_structure=include_market_structure,
        include_sec=include_sec,
        requested_option=bool(resolution.get("request")),
    )
    sections["_data_coverage_report"] = [coverage_report]
    sections["data_coverage"] = coverage_rows
    sources["data_coverage"] = "Optedge lookup coverage audit"

    public_sections = {name: rows for name, rows in sections.items() if not name.startswith("_")}
    hit_sections = {
        name: rows for name, rows in public_sections.items()
        if name != "data_coverage"
    }
    local_hit_count = sum(
        len(rows) for name, rows in hit_sections.items()
        if not name.startswith("sec_") and name != "recent_sec_filings"
    )
    total_hits = sum(len(rows) for rows in hit_sections.values())
    brief = _research_brief(q, resolution, raw_matches, sections, data_dir, local_hit_count)
    return {
        "generated_at": generated_at,
        "query": original_query.upper(),
        "lookup_symbol": q,
        "resolution": resolution,
        "brief": brief,
        "total_hits": total_hits,
        "sources": sources,
        "sections": public_sections,
        "notes": [
            "Lookup uses latest local Optedge snapshots, open state, broker snapshot, and saved option-chain shortlist.",
            "Optional price snapshots use the free cached history stack and are not live broker quotes.",
            "Optional market-structure checks use official no-key Nasdaq Trader risk lists.",
            "Run a fresh scan with --universe TICKER if the ticker is missing or stale.",
            "This is research output only, not an order or financial advice.",
        ],
    }


def _render_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p class='muted'>No matching rows.</p>"
    columns = list(dict.fromkeys(k for row in rows for k in row.keys()))
    head = "".join(f"<th>{html.escape(c)}</th>" for c in columns)
    body = []
    for row in rows:
        cells = []
        for col in columns:
            value = row.get(col)
            text = "-" if value is None else str(value)
            cells.append(f"<td>{html.escape(text[:220])}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"<div class='table-wrap'><table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"


def _fmt_brief_pct(value: Any) -> str:
    val = _float_value(value)
    return "-" if val is None else f"{val * 100:+.1f}%"


def _fmt_brief_ratio(value: Any) -> str:
    val = _float_value(value)
    return "-" if val is None else f"{val:.2f}x"


def _fmt_brief_money(value: Any) -> str:
    val = _float_value(value)
    if val is None:
        return "-"
    abs_val = abs(val)
    if abs_val >= 1_000_000_000:
        return f"${val / 1_000_000_000:.1f}B"
    if abs_val >= 1_000_000:
        return f"${val / 1_000_000:.1f}M"
    if abs_val >= 1_000:
        return f"${val / 1_000:.1f}K"
    return f"${val:.0f}"


def _render_brief(brief: dict[str, Any]) -> str:
    if not brief:
        return ""
    idea = brief.get("best_idea") or {}
    requested = brief.get("requested_option") or {}
    alternatives = brief.get("option_alternatives") or {}
    comparison = brief.get("contract_comparison") or {}
    readiness = brief.get("paper_readiness") or {}
    open_pos = brief.get("open_positions") or {}
    broker_pos = brief.get("broker_positions") or {}
    contract_exposure = brief.get("contract_exposure") or {}
    price = brief.get("price_snapshot") or {}
    coverage = brief.get("data_coverage") or {}
    market_structure = brief.get("market_structure") or {}
    cboe_activity = brief.get("cboe_option_activity") or {}
    swing = brief.get("swing_verdict") or {}
    validation = brief.get("validation") or {}
    action = brief.get("research_action") or {}
    sec = brief.get("recent_sec_filings") or {}
    sec_fund = brief.get("sec_fundamentals") or {}
    positives = "".join(
        f"<li>{html.escape(str(x.get('factor')))} <b>{_clean_value(x.get('value'))}</b></li>"
        for x in brief.get("top_positive_factors", [])[:5]
    ) or "<li>None surfaced</li>"
    negatives = "".join(
        f"<li>{html.escape(str(x.get('factor')))} <b>{_clean_value(x.get('value'))}</b></li>"
        for x in brief.get("top_negative_factors", [])[:5]
    ) or "<li>None surfaced</li>"
    warnings = "".join(
        f"<li>{html.escape(str(w))}</li>" for w in brief.get("risk_warnings", [])[:5]
    ) or "<li>No local warnings found</li>"
    next_steps = "".join(
        f"<li>{html.escape(str(step))}</li>" for step in action.get("next_steps", [])[:5]
    ) or "<li>Review local factors and exposure.</li>"
    action_reasons = "".join(
        f"<li>{html.escape(str(reason))}</li>" for reason in action.get("reasons", [])[:6]
    ) or "<li>No action-specific reasons surfaced.</li>"
    readiness_checks = "".join(
        f"<li>{html.escape(str(row.get('label')))}: {html.escape(str(row.get('detail')))}</li>"
        for row in readiness.get("checks", [])[:6]
    ) or "<li>No readiness checks available.</li>"
    swing_reasons = "".join(
        f"<li>{html.escape(str(reason))}</li>" for reason in swing.get("reasons", [])[:6]
    ) or "<li>No swing-specific reasons surfaced.</li>"
    swing_blockers = "".join(
        f"<li>{html.escape(str(blocker))}</li>" for blocker in swing.get("blockers", [])[:5]
    ) or "<li>No swing blockers surfaced.</li>"
    comparison_reasons = "".join(
        f"<li>{html.escape(str(reason))}</li>" for reason in comparison.get("reasons", [])[:6]
    ) or "<li>No contract comparison available.</li>"
    sec_signals = ", ".join(str(x) for x in sec.get("watch_signals", [])[:4]) or "-"
    sec_fund_signals = ", ".join(str(x) for x in sec_fund.get("watch_signals", [])[:4]) or "-"
    return f"""
<section>
  <h2>Research Brief</h2>
  <div class="brief-grid">
    <div><span class="muted">Symbol</span><strong>{html.escape(str(brief.get('symbol') or '-'))}</strong></div>
    <div><span class="muted">Resolved via</span><strong>{html.escape(str(brief.get('resolution_source') or '-'))}</strong></div>
    <div><span class="muted">Swing verdict</span><strong>{html.escape(str(swing.get('label') or '-'))}</strong></div>
    <div><span class="muted">Swing score</span><strong>{html.escape(str(swing.get('score') if swing.get('score') is not None else '-'))}</strong></div>
    <div><span class="muted">Swing bias</span><strong>{html.escape(str(swing.get('bias') or '-'))}</strong></div>
    <div><span class="muted">Swing decision</span><strong>{html.escape(str(swing.get('decision') or '-'))}</strong></div>
    <div><span class="muted">Swing R/R</span><strong>{html.escape(str(swing.get('risk_reward') if swing.get('risk_reward') is not None else '-'))}</strong></div>
    <div><span class="muted">Last price</span><strong>{html.escape(str(price.get('last_price') if price.get('last_price') is not None else '-'))}</strong></div>
    <div><span class="muted">Price trend</span><strong>{html.escape(str(price.get('trend_label') or '-'))}</strong></div>
    <div><span class="muted">20d return</span><strong>{_fmt_brief_pct(price.get('ret_20d'))}</strong></div>
    <div><span class="muted">60d return</span><strong>{_fmt_brief_pct(price.get('ret_60d'))}</strong></div>
    <div><span class="muted">6m range pos</span><strong>{_fmt_brief_pct(price.get('range_6mo_pos'))}</strong></div>
    <div><span class="muted">Price source</span><strong>{html.escape(str(price.get('history_source') or '-'))}</strong></div>
    <div><span class="muted">Data coverage</span><strong>{html.escape(str(coverage.get('label') or '-'))}</strong></div>
    <div><span class="muted">Coverage score</span><strong>{html.escape(str(coverage.get('score') if coverage.get('score') is not None else '-'))}</strong></div>
    <div><span class="muted">Coverage flags</span><strong>{int(coverage.get('bad_count') or 0)} bad / {int(coverage.get('warn_count') or 0)} warn</strong></div>
    <div><span class="muted">Market structure</span><strong>{html.escape(str(market_structure.get('status') or '-'))}</strong></div>
    <div><span class="muted">Market risk score</span><strong>{html.escape(str(market_structure.get('risk_score') if market_structure.get('risk_score') is not None else '-'))}</strong></div>
    <div><span class="muted">Cboe contract activity</span><strong>{html.escape(str(cboe_activity.get('label') or '-'))}</strong></div>
    <div><span class="muted">Cboe volume</span><strong>{html.escape(str(cboe_activity.get('total_volume') if cboe_activity.get('total_volume') is not None else '-'))}</strong></div>
    <div><span class="muted">Cboe spread</span><strong>{_fmt_brief_pct(cboe_activity.get('spread_pct'))}</strong></div>
    <div><span class="muted">Cboe venues</span><strong>{html.escape(str(cboe_activity.get('venues') or '-'))}</strong></div>
    <div><span class="muted">Requested option</span><strong>{html.escape(str(requested.get('label') or '-'))}</strong></div>
    <div><span class="muted">Requested match</span><strong>{html.escape(str(requested.get('match_quality') or '-'))}</strong></div>
    <div><span class="muted">Matched contract</span><strong>{html.escape(str(requested.get('matched_contract') or '-'))}</strong></div>
    <div><span class="muted">Alt contracts</span><strong>{int(alternatives.get('count') or 0)}</strong></div>
    <div><span class="muted">Best alternative</span><strong>{html.escape(str(alternatives.get('best_label') or '-'))}</strong></div>
    <div><span class="muted">Alt readiness</span><strong>{html.escape(str(alternatives.get('best_readiness_score') if alternatives.get('best_readiness_score') is not None else '-'))}</strong></div>
    <div><span class="muted">Alt reason</span><strong>{html.escape(str(alternatives.get('best_reason') or '-'))}</strong></div>
    <div><span class="muted">Contract pick</span><strong>{html.escape(str(comparison.get('label') or '-'))}</strong></div>
    <div><span class="muted">Pick winner</span><strong>{html.escape(str(comparison.get('winner') or '-'))}</strong></div>
    <div><span class="muted">Pick score</span><strong>{html.escape(str(comparison.get('edge_score') if comparison.get('edge_score') is not None else '-'))}</strong></div>
    <div><span class="muted">Premium delta</span><strong>{_fmt_brief_pct(comparison.get('premium_delta_pct'))}</strong></div>
    <div><span class="muted">Paper readiness</span><strong>{html.escape(str(readiness.get('label') or '-'))}</strong></div>
    <div><span class="muted">Readiness score</span><strong>{html.escape(str(readiness.get('score') if readiness.get('score') is not None else '-'))}</strong></div>
    <div><span class="muted">Best local idea</span><strong>{html.escape(str(idea.get('label') or 'None'))}</strong></div>
    <div><span class="muted">Quote source</span><strong>{html.escape(str(idea.get('quote_source_label') or '-'))}</strong></div>
    <div><span class="muted">Snapshot age</span><strong>{html.escape(str(idea.get('snapshot_age_min') if idea.get('snapshot_age_min') is not None else '-'))} min</strong></div>
    <div><span class="muted">Freshness</span><strong>{html.escape(str(idea.get('snapshot_freshness') or '-'))}</strong></div>
    <div><span class="muted">Research action</span><strong>{html.escape(str(action.get('label') or 'Review'))}</strong></div>
    <div><span class="muted">Action risk</span><strong>{html.escape(str(action.get('risk_level') or '-'))}</strong></div>
    <div><span class="muted">Spread</span><strong>{_fmt_brief_pct(idea.get('spread_pct'))}</strong></div>
    <div><span class="muted">Net edge</span><strong>{_fmt_brief_pct(idea.get('net_edge_pct'))}</strong></div>
    <div><span class="muted">Open exposure</span><strong>{int(open_pos.get('count') or 0)}</strong></div>
    <div><span class="muted">Exact contract exposure</span><strong>{int(contract_exposure.get('exact_total') or 0)}</strong></div>
    <div><span class="muted">Same ticker options</span><strong>{int(contract_exposure.get('same_ticker_total') or 0)}</strong></div>
    <div><span class="muted">Broker positions</span><strong>{int(broker_pos.get('count') or 0)}</strong></div>
    <div><span class="muted">Broker value</span><strong>{_fmt_brief_money(broker_pos.get('market_value'))}</strong></div>
    <div><span class="muted">Broker snapshot</span><strong>{html.escape(str(broker_pos.get('snapshot_freshness') or '-'))}</strong></div>
    <div><span class="muted">Recent SEC filings</span><strong>{int(sec.get('count') or 0)}</strong></div>
    <div><span class="muted">SEC watch signals</span><strong>{html.escape(sec_signals)}</strong></div>
    <div><span class="muted">SEC cash</span><strong>{_fmt_brief_money(sec_fund.get('cash'))}</strong></div>
    <div><span class="muted">SEC cash/debt</span><strong>{_fmt_brief_ratio(sec_fund.get('cash_to_debt'))}</strong></div>
    <div><span class="muted">SEC debt/assets</span><strong>{_fmt_brief_pct(sec_fund.get('debt_to_assets'))}</strong></div>
    <div><span class="muted">SEC net margin</span><strong>{_fmt_brief_pct(sec_fund.get('net_margin'))}</strong></div>
    <div><span class="muted">SEC fact flags</span><strong>{html.escape(sec_fund_signals)}</strong></div>
    <div><span class="muted">Avg unrealized</span><strong>{_fmt_brief_pct(open_pos.get('avg_unrealized_pct'))}</strong></div>
    <div><span class="muted">Validation win rate</span><strong>{_fmt_brief_pct(validation.get('win_rate'))}</strong></div>
    <div><span class="muted">Validation avg return</span><strong>{_fmt_brief_pct(validation.get('avg_return'))}</strong></div>
  </div>
  <div class="two-col">
    <div><h3>Positive factors</h3><ul>{positives}</ul></div>
    <div><h3>Negative factors</h3><ul>{negatives}</ul></div>
  </div>
  <div class="two-col">
    <div><h3>Action reasons</h3><ul>{action_reasons}</ul></div>
    <div><h3>Next steps</h3><ul>{next_steps}</ul></div>
  </div>
  <div class="two-col">
    <div><h3>Swing verdict reasons</h3><ul>{swing_reasons}</ul></div>
    <div><h3>Swing blockers</h3><ul>{swing_blockers}</ul></div>
  </div>
  <h3>Contract comparison</h3><ul>{comparison_reasons}</ul>
  <h3>Readiness checklist</h3><ul>{readiness_checks}</ul>
  <h3>Warnings</h3><ul>{warnings}</ul>
</section>"""


def render_html(report: dict[str, Any]) -> str:
    q = html.escape(str(report.get("query", "")))
    sections = report.get("sections", {})
    parts = []
    for name, rows in sections.items():
        parts.append(
            f"<section><h2>{html.escape(name.replace('_', ' ').title())} "
            f"<span>{len(rows)}</span></h2>{_render_table(rows)}</section>"
        )
    notes = "".join(f"<li>{html.escape(str(n))}</li>" for n in report.get("notes", []))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Optedge Lookup - {q}</title>
<style>
body {{ margin:0; background:#090b10; color:#e5e7eb; font-family:Inter,Segoe UI,Arial,sans-serif; }}
.wrap {{ max-width:1280px; margin:0 auto; padding:28px 18px 60px; }}
header {{ display:flex; justify-content:space-between; gap:16px; align-items:flex-end; border-bottom:1px solid #1f2937; padding-bottom:16px; }}
h1 {{ margin:0; font-size:28px; }}
.muted, li {{ color:#94a3b8; }}
.pill {{ border:1px solid #334155; background:#111827; border-radius:999px; padding:6px 10px; font-size:12px; }}
section {{ margin-top:20px; border:1px solid #1f2937; border-radius:8px; background:#0f172a; padding:14px; }}
h2 {{ margin:0 0 12px; font-size:15px; }}
h2 span {{ color:#38bdf8; font-family:monospace; }}
.table-wrap {{ overflow:auto; }}
table {{ width:100%; border-collapse:collapse; font-size:12px; }}
th, td {{ padding:8px 10px; border-bottom:1px solid #1f2937; text-align:left; vertical-align:top; }}
th {{ color:#94a3b8; text-transform:uppercase; font-size:10px; letter-spacing:.4px; }}
.brief-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:10px; }}
.brief-grid div {{ border:1px solid #223044; border-radius:8px; padding:10px; background:#0b1220; }}
.brief-grid span {{ display:block; font-size:11px; }}
.brief-grid strong {{ display:block; margin-top:4px; }}
.two-col {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:12px; margin-top:12px; }}
</style>
</head>
<body><div class="wrap">
<header><div><h1>Optedge Lookup: {q}</h1><div class="muted">Latest local scan snapshot</div></div><div class="pill">{report.get('total_hits', 0)} hits</div></header>
<ul>{notes}</ul>
{_render_brief(report.get('brief', {}))}
{''.join(parts)}
</div></body></html>"""


def save_lookup(report: dict[str, Any], data_dir: Path = DATA_DIR) -> dict[str, Path]:
    data_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in report["query"] if ch.isalnum() or ch in {"_", "-", "="}) or "lookup"
    json_path = data_dir / f"lookup_{safe}.json"
    html_path = data_dir / f"lookup_{safe}.html"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    return {"json": json_path, "html": html_path}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Look up one ticker/symbol in latest local Optedge outputs.")
    ap.add_argument("symbol", help="Ticker or futures symbol to inspect, e.g. NVDA, TSLA, CL=F")
    ap.add_argument("--data-dir", default=str(DATA_DIR))
    ap.add_argument("--price", action="store_true", help="Include a free cached price/trend snapshot.")
    ap.add_argument("--market-structure", action="store_true", help="Include official no-key market-structure risk checks.")
    ap.add_argument("--cboe-activity", action="store_true", help="Include public Cboe option activity for option-style queries.")
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args(argv)

    report = lookup_symbol(
        args.symbol,
        Path(args.data_dir),
        include_price=args.price,
        include_market_structure=args.market_structure,
        include_cboe_activity=args.cboe_activity,
    )
    paths = save_lookup(report, Path(args.data_dir))
    print(json.dumps(report, indent=2, default=str) if args.json_only else f"Lookup report: {paths['html']}\nLookup JSON: {paths['json']}\nHits: {report['total_hits']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
