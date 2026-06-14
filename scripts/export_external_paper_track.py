"""Export a small executable subset for third-party paper tracking.

This script does not place trades and does not use broker credentials. It only
turns the latest ranked Optedge outputs into a compact candidate file that can
be manually entered into a paper broker or imported into a journal.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

NORMALIZED_COLUMNS = [
    "generated_at",
    "asset",
    "ticker_or_symbol",
    "action",
    "direction",
    "quantity",
    "contract",
    "option_side",
    "strike",
    "expiry",
    "entry_price",
    "stop_price",
    "target_price",
    "confidence",
    "rank_score",
    "fused_score",
    "trade_status",
    "risk_dollars",
    "reward_dollars",
    "suggested_dollars",
    "suggested_contracts",
    "swing_fit_score",
    "swing_fit_label",
    "swing_fit_reasons",
    "swing_fit_warnings",
    "breakeven_move_label",
    "liquidity_label",
    "reason_selected",
    "reason_excluded",
    "notes",
]

DEFAULT_MAX_SPREAD = 0.15
DEFAULT_MAX_AGE_HOURS = 24
DEFAULT_MAX_PER_SECTOR = 2
DEFAULT_MIN_OPTION_DTE = 90
CHAIN_SHORTLIST_JSON = "option_chain_shortlist.json"
CHAIN_SHORTLIST_CSV = "option_chain_shortlist.csv"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or pd.isna(value):
            return default
        return int(float(value))
    except Exception:
        return default


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def _latest_file(data_dir: Path, pattern: str) -> Path | None:
    files = [p for p in data_dir.glob(pattern) if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: (p.stat().st_mtime, p.name))


def _load_latest_parquet(data_dir: Path, pattern: str) -> pd.DataFrame:
    path = _latest_file(data_dir, pattern)
    if path is None:
        return pd.DataFrame()
    try:
        df = pd.read_parquet(path)
    except ImportError as exc:
        raise RuntimeError(
            "Parquet support requires pyarrow or fastparquet. Install project "
            "requirements in your Optedge environment before running this export."
        ) from exc
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out["_source_file"] = path.name
    out["_source_mtime"] = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    return out


def _load_json_rows(path: Path) -> list[dict[str, Any]]:
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return rows if isinstance(rows, list) else []


def _load_json_obj(path: Path) -> dict[str, Any]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _series_or_default(df: pd.DataFrame, column: str, default: Any = "") -> pd.Series:
    if column in df.columns:
        return df[column]
    return pd.Series([default] * len(df), index=df.index)


def _load_option_chain_shortlist(data_dir: Path) -> pd.DataFrame:
    json_path = data_dir / CHAIN_SHORTLIST_JSON
    csv_path = data_dir / CHAIN_SHORTLIST_CSV
    source_path: Path | None = None
    rows: Any = []
    generated_at = ""
    if json_path.exists():
        payload = _load_json_obj(json_path)
        rows = payload.get("rows") if isinstance(payload, dict) else []
        generated_at = _text(payload.get("generated_at")) if isinstance(payload, dict) else ""
        source_path = json_path
    elif csv_path.exists():
        try:
            rows = pd.read_csv(csv_path)
        except Exception:
            rows = []
        source_path = csv_path
    if isinstance(rows, pd.DataFrame):
        df = rows.copy()
    elif isinstance(rows, list):
        df = pd.DataFrame([row for row in rows if isinstance(row, dict)])
    else:
        return pd.DataFrame()
    if df.empty or source_path is None:
        return pd.DataFrame()

    out = pd.DataFrame()
    out["ticker"] = _series_or_default(df, "symbol").astype(str).str.upper()
    out["contract"] = _series_or_default(df, "contract_query")
    out["side"] = _series_or_default(df, "side").astype(str).str.lower()
    out["strike"] = _series_or_default(df, "strike")
    out["expiry"] = _series_or_default(df, "expiry")
    out["mid"] = pd.to_numeric(_series_or_default(df, "mid", 0.0), errors="coerce").fillna(0.0)
    premium = pd.to_numeric(_series_or_default(df, "premium_dollars", float("nan")), errors="coerce")
    out["actual_dollars"] = premium.fillna(out["mid"] * 100.0)
    out["suggested_contracts"] = (out["mid"] > 0).astype(int)
    stop_ref = pd.to_numeric(_series_or_default(df, "stop_price_reference", float("nan")), errors="coerce")
    target_ref = pd.to_numeric(_series_or_default(df, "target_price_reference", float("nan")), errors="coerce")
    out["stop_price"] = stop_ref.fillna(out["mid"] * 0.50).round(2)
    out["target_price"] = target_ref.fillna(out["mid"] * 2.00).round(2)
    readiness = pd.to_numeric(_series_or_default(df, "readiness_score", 0), errors="coerce")
    quality = pd.to_numeric(_series_or_default(df, "contract_quality_score", 0), errors="coerce")
    swing_fit = pd.to_numeric(_series_or_default(df, "swing_fit_score", float("nan")), errors="coerce")
    out["confidence"] = readiness.fillna(swing_fit).fillna(quality).fillna(0).clip(lower=0, upper=100)
    out["rank_score"] = (
        quality.fillna(0) / 25.0
        + swing_fit.fillna(0) / 60.0
    ).round(4)
    out["fused_score"] = out["rank_score"]
    grade = _series_or_default(df, "contract_grade").astype(str).str.upper()
    readiness_label = _series_or_default(df, "readiness_label").astype(str).str.lower()
    out["trade_status"] = [
        "Trade" if g in {"A", "B"} or r in {"ready", "review"} else "Watch"
        for g, r in zip(grade, readiness_label, strict=False)
    ]
    out["spread_pct"] = pd.to_numeric(_series_or_default(df, "spread_pct", 0.0), errors="coerce").fillna(0.0)
    out["dte"] = pd.to_numeric(_series_or_default(df, "dte", -1), errors="coerce").fillna(-1).astype(int)
    out["swing_fit_score"] = swing_fit
    out["swing_fit_label"] = _series_or_default(df, "swing_fit_label")
    out["swing_fit_reasons"] = _series_or_default(df, "swing_fit_reasons")
    out["swing_fit_warnings"] = _series_or_default(df, "swing_fit_warnings")
    out["breakeven_move_label"] = _series_or_default(df, "breakeven_move_label")
    out["liquidity_label"] = _series_or_default(df, "liquidity_label")
    out["bid"] = pd.to_numeric(_series_or_default(df, "bid", float("nan")), errors="coerce")
    out["ask"] = pd.to_numeric(_series_or_default(df, "ask", float("nan")), errors="coerce")
    out["openInterest"] = pd.to_numeric(_series_or_default(df, "openInterest", 0), errors="coerce").fillna(0)
    out["volume"] = pd.to_numeric(_series_or_default(df, "volume", 0), errors="coerce").fillna(0)
    out["impliedVolatility"] = pd.to_numeric(_series_or_default(df, "impliedVolatility", float("nan")), errors="coerce")
    out["delta"] = pd.to_numeric(_series_or_default(df, "delta", float("nan")), errors="coerce")
    out["breakeven_price"] = pd.to_numeric(_series_or_default(df, "breakeven_price", float("nan")), errors="coerce")
    out["breakeven_move_pct"] = pd.to_numeric(_series_or_default(df, "breakeven_move_pct", float("nan")), errors="coerce")
    out["breakeven_direction"] = _series_or_default(df, "breakeven_direction")
    out["budget_usage_pct"] = pd.to_numeric(_series_or_default(df, "budget_usage_pct", float("nan")), errors="coerce")
    out["contracts_for_budget"] = pd.to_numeric(_series_or_default(df, "contracts_for_budget", 0), errors="coerce").fillna(0)
    out["risk_dollars_reference"] = pd.to_numeric(_series_or_default(df, "risk_dollars_reference", float("nan")), errors="coerce")
    out["reward_dollars_reference"] = pd.to_numeric(_series_or_default(df, "reward_dollars_reference", float("nan")), errors="coerce")
    out["reward_risk_reference"] = pd.to_numeric(_series_or_default(df, "reward_risk_reference", float("nan")), errors="coerce")
    out["budget_fit"] = _series_or_default(df, "budget_fit")
    out["contract_grade"] = grade
    out["review_lane"] = _series_or_default(df, "review_lane")
    out["readiness_label"] = readiness_label
    out["readiness_score"] = readiness
    out["review_thesis"] = _series_or_default(df, "review_thesis")
    out["grade_reasons"] = _series_or_default(df, "grade_reasons")
    out["risk_flags"] = _series_or_default(df, "risk_flags")
    out["chain_source"] = _series_or_default(df, "chain_source")
    out["quote_quality"] = _series_or_default(df, "quote_quality")
    out["data_delay"] = _series_or_default(df, "data_delay")
    out["top_headline"] = "3m+ option-chain shortlist candidate"
    out["generated_at"] = _series_or_default(df, "generated_at", generated_at)
    out["_source_file"] = source_path.name
    out["_source_mtime"] = datetime.fromtimestamp(source_path.stat().st_mtime, tz=timezone.utc).isoformat()
    out["_chain_shortlist"] = True
    return out


def _parse_time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        return pd.to_datetime(text, utc=True).to_pydatetime()
    except Exception:
        return None


def _option_dte(row: pd.Series, generated_at: str) -> int | None:
    direct = row.get("dte")
    if _text(direct):
        dte = _safe_int(direct, -1)
        if dte >= 0:
            return dte
    expiry = _parse_time(row.get("expiry"))
    asof = _parse_time(generated_at)
    if expiry is None or asof is None:
        return None
    return int((expiry.date() - asof.date()).days)


def _is_stale(row: pd.Series, now: datetime, max_age_hours: int = DEFAULT_MAX_AGE_HOURS) -> bool:
    for key in ("generated_at", "asof", "entry_time", "_source_mtime"):
        ts = _parse_time(row.get(key))
        if ts is not None:
            age_hours = (now - ts).total_seconds() / 3600.0
            return age_hours > max_age_hours
    return False


def _score_row(row: pd.Series) -> float:
    rank = _safe_float(row.get("rank_score"), None)
    if rank is None:
        rank = _safe_float(row.get("fused_score"), None)
    if rank is None:
        rank = _safe_float(row.get("share_score"), None)
    if rank is None:
        rank = abs(_safe_float(row.get("futures_score"), 0.0))
    conf = _safe_float(row.get("confidence"), 0.0) / 100.0
    ev = _safe_float(row.get("ev_pct"), 0.0)
    swing_score = _safe_float(row.get("swing_fit_score"), 0.0) / 100.0
    swing_label = _text(row.get("swing_fit_label")).lower()
    swing_bonus = {
        "clean_swing": 0.85,
        "reviewable_swing": 0.45,
        "speculative_swing": -0.20,
        "avoid": -1.25,
    }.get(swing_label, 0.0)
    return float(rank or 0.0) + 0.25 * conf + 0.10 * ev + 0.50 * swing_score + swing_bonus


def _compact_search_text(value: Any) -> str:
    text = _text(value).upper()
    return "".join(ch for ch in text if ch.isalnum())


def _matches_query(row: pd.Series, normalized: dict[str, Any], query: str) -> bool:
    query_text = _text(query)
    if not query_text:
        return True
    values: list[str] = []
    for key in (
        "ticker_or_symbol", "contract", "option_side", "strike", "expiry", "direction", "action",
        "ticker", "symbol", "name", "company", "company_name", "security_name", "localSymbol",
        "side", "top_headline",
    ):
        if key in normalized:
            values.append(_text(normalized.get(key)))
        if key in row.index:
            values.append(_text(row.get(key)))
    blob = " ".join(v for v in values if v).upper()
    compact_blob = _compact_search_text(blob)
    compact_query = _compact_search_text(query_text)
    if not compact_query:
        return True
    if query_text.upper() in blob or compact_query in compact_blob:
        return True
    tokens = [_compact_search_text(token) for token in query_text.replace("/", " ").split()]
    tokens = [token for token in tokens if token]
    return bool(tokens) and all(token in compact_blob for token in tokens)


def _open_keys(open_rows: list[dict[str, Any]]) -> set[tuple[str, str, str]]:
    keys: set[tuple[str, str, str]] = set()
    for row in open_rows:
        asset = _text(row.get("asset")).lower()
        ticker = _text(row.get("ticker") or row.get("symbol")).upper()
        if not ticker:
            continue
        if asset == "option":
            direction = _text(row.get("side") or row.get("direction")).lower()
        elif asset == "futures":
            direction = _text(row.get("direction") or row.get("side")).lower()
        else:
            asset = "share" if asset in {"shares", "equity"} else asset
            direction = "long"
        keys.add((asset, ticker, direction))
    return keys


def _status_excluded(row: pd.Series, include_watch: bool) -> str:
    status = _text(row.get("trade_status") or "Trade")
    if include_watch:
        return "trade_status is Skip" if status.lower() == "skip" else ""
    if status.lower() in {"watch", "skip"}:
        return f"trade_status is {status}"
    return ""


def _guard_excluded(row: pd.Series) -> str:
    status = _text(row.get("research_guard_status")).lower()
    return "research guard blocked" if status.startswith("blocked") or status == "blocked" else ""


def _sector(row: pd.Series) -> str:
    return _text(row.get("sector") or row.get("sector_etf") or row.get("classification")).upper()


def _base_output(generated_at: str, asset: str) -> dict[str, Any]:
    return {col: "" for col in NORMALIZED_COLUMNS} | {
        "generated_at": generated_at,
        "asset": asset,
    }


def _normalize_option(row: pd.Series, generated_at: str, allow_zero_size_placeholder: bool) -> tuple[dict[str, Any], str]:
    ticker = _text(row.get("ticker")).upper()
    side = _text(row.get("side")).lower()
    strike = row.get("strike")
    expiry = _text(row.get("expiry"))
    entry = _safe_float(row.get("mid") or row.get("entry_price"), 0.0)
    contracts = _safe_int(row.get("suggested_contracts"), 0)
    if not ticker:
        return {}, "missing ticker"
    if side not in {"call", "put"}:
        return {}, "missing option side"
    if _text(strike) == "":
        return {}, "missing strike"
    if not expiry:
        return {}, "missing expiry"
    if entry <= 0:
        return {}, "missing entry/mid price"
    if contracts <= 0 and not allow_zero_size_placeholder:
        return {}, "suggested_contracts <= 0"
    out = _base_output(generated_at, "option")
    suggested_dollars = _safe_float(row.get("actual_dollars") or row.get("suggested_dollars"), entry * contracts * 100)
    stop = _safe_float(row.get("stop_price"), 0.0)
    target = _safe_float(row.get("target_price"), 0.0)
    risk = max(entry - stop, 0.0) * max(contracts, 1) * 100 if stop else suggested_dollars
    reward = max(target - entry, 0.0) * max(contracts, 1) * 100 if target else ""
    swing_label = _text(row.get("swing_fit_label"))
    swing_reasons = _text(row.get("swing_fit_reasons"))
    swing_warnings = _text(row.get("swing_fit_warnings"))
    shortlist = bool(row.get("_chain_shortlist", False))
    reason_selected = "passed external option filters"
    notes = "manual paper-tracking candidate; no broker order placed"
    if shortlist:
        reason_selected = "passed external option filters from 3m+ chain shortlist"
        if swing_label:
            reason_selected += f" ({swing_label.replace('_', ' ')})"
        notes = "3m+ chain-shortlist candidate; provisional stop/target references; no broker order placed"
        if swing_label:
            notes += f"; swing_fit={swing_label}"
        if swing_reasons:
            notes += f"; reasons={swing_reasons}"
        if swing_warnings:
            notes += f"; warnings={swing_warnings}"
    out.update({
        "_sector": _sector(row),
        "ticker_or_symbol": ticker,
        "action": "BUY_TO_OPEN",
        "direction": f"long_{side}",
        "quantity": contracts,
        "contract": _text(row.get("contract")) or f"{ticker} {expiry} {side.upper()} {strike}",
        "option_side": side,
        "strike": strike,
        "expiry": expiry,
        "entry_price": entry,
        "stop_price": stop,
        "target_price": target,
        "confidence": _safe_int(row.get("confidence"), ""),
        "rank_score": _safe_float(row.get("rank_score"), ""),
        "fused_score": _safe_float(row.get("fused_score"), ""),
        "trade_status": _text(row.get("trade_status") or "Trade"),
        "risk_dollars": round(risk, 2) if risk != "" else "",
        "reward_dollars": round(reward, 2) if reward != "" else "",
        "suggested_dollars": round(suggested_dollars, 2),
        "suggested_contracts": contracts,
        "swing_fit_score": _safe_float(row.get("swing_fit_score"), ""),
        "swing_fit_label": swing_label,
        "swing_fit_reasons": swing_reasons,
        "swing_fit_warnings": swing_warnings,
        "breakeven_move_label": _text(row.get("breakeven_move_label")),
        "liquidity_label": _text(row.get("liquidity_label")),
        "reason_selected": reason_selected,
        "notes": notes,
    })
    return out, ""


def _normalize_share(row: pd.Series, generated_at: str, allow_zero_size_placeholder: bool) -> tuple[dict[str, Any], str]:
    ticker = _text(row.get("ticker")).upper()
    entry = _safe_float(row.get("entry_price") or row.get("spot") or row.get("current_price"), 0.0)
    dollars = _safe_float(row.get("suggested_dollars") or row.get("actual_dollars"), 0.0)
    qty = math.floor(dollars / entry) if entry > 0 and dollars > 0 else 0
    if not ticker:
        return {}, "missing ticker"
    if entry <= 0:
        return {}, "missing share entry price"
    if qty <= 0 and not allow_zero_size_placeholder:
        return {}, "share quantity cannot be calculated"
    stop = _safe_float(row.get("stop_price"), 0.0)
    target = _safe_float(row.get("target_price"), 0.0)
    if stop <= 0 and row.get("stop_pct") is not None:
        stop = entry * (1.0 + _safe_float(row.get("stop_pct"), 0.0))
    if target <= 0 and row.get("target_pct") is not None:
        target = entry * (1.0 + _safe_float(row.get("target_pct"), 0.0))
    risk = max(entry - stop, 0.0) * max(qty, 1) if stop else ""
    reward = max(target - entry, 0.0) * max(qty, 1) if target else ""
    out = _base_output(generated_at, "share")
    out.update({
        "_sector": _sector(row),
        "ticker_or_symbol": ticker,
        "action": "BUY",
        "direction": "long",
        "quantity": qty,
        "entry_price": entry,
        "stop_price": round(stop, 4) if stop else "",
        "target_price": round(target, 4) if target else "",
        "confidence": _safe_int(row.get("confidence"), ""),
        "rank_score": _safe_float(row.get("rank_score") or row.get("share_score"), ""),
        "fused_score": _safe_float(row.get("fused_score") or row.get("share_score"), ""),
        "trade_status": _text(row.get("trade_status") or "Trade"),
        "risk_dollars": round(risk, 2) if risk != "" else "",
        "reward_dollars": round(reward, 2) if reward != "" else "",
        "suggested_dollars": round(dollars, 2),
        "suggested_contracts": "",
        "reason_selected": "passed external share filters",
        "notes": "manual paper-tracking candidate; no broker order placed",
    })
    return out, ""


def _normalize_future(row: pd.Series, generated_at: str, allow_zero_size_placeholder: bool) -> tuple[dict[str, Any], str]:
    symbol = _text(row.get("symbol") or row.get("ticker")).upper()
    direction = _text(row.get("direction")).lower()
    if direction not in {"long", "short"}:
        score = _safe_float(row.get("futures_score"), 0.0)
        direction = "long" if score > 0 else "short" if score < 0 else ""
    contracts = _safe_int(row.get("suggested_contracts") or row.get("n_contracts"), 0)
    contract = _text(row.get("contract") or row.get("micro_contract") or row.get("micro_symbol"))
    entry = _safe_float(row.get("entry_price") or row.get("entry") or row.get("spot"), 0.0)
    stop = _safe_float(row.get("stop_price"), 0.0)
    target = _safe_float(row.get("target_price"), 0.0)
    point_value = _safe_float(row.get("point_value"), 0.0)
    if not symbol:
        return {}, "missing symbol"
    if direction not in {"long", "short"}:
        return {}, "missing futures direction"
    if not contract:
        return {}, "missing futures contract"
    if entry <= 0 or stop <= 0 or target <= 0 or point_value <= 0:
        return {}, "missing futures entry/stop/target/point_value"
    if contracts <= 0 and not allow_zero_size_placeholder:
        return {}, "suggested_contracts <= 0"
    risk = _safe_float(row.get("risk_dollars") or row.get("suggested_dollars_risk"), 0.0)
    reward = _safe_float(row.get("reward_dollars"), 0.0)
    if risk <= 0:
        risk = abs(entry - stop) * point_value * max(contracts, 1)
    if reward <= 0:
        reward = abs(target - entry) * point_value * max(contracts, 1)
    out = _base_output(generated_at, "futures")
    out.update({
        "_sector": _sector(row),
        "ticker_or_symbol": symbol,
        "action": "BUY_TO_OPEN" if direction == "long" else "SELL_TO_OPEN",
        "direction": direction,
        "quantity": contracts,
        "contract": contract,
        "entry_price": entry,
        "stop_price": stop,
        "target_price": target,
        "confidence": _safe_int(row.get("confidence"), ""),
        "rank_score": _safe_float(row.get("rank_score") or row.get("futures_score"), ""),
        "fused_score": _safe_float(row.get("fused_score") or row.get("futures_score"), ""),
        "trade_status": _text(row.get("trade_status") or "Trade"),
        "risk_dollars": round(risk, 2),
        "reward_dollars": round(reward, 2),
        "suggested_dollars": "",
        "suggested_contracts": contracts,
        "reason_selected": "passed external futures filters",
        "notes": f"manual paper-tracking candidate; point_value={point_value:g}; no broker order placed",
    })
    return out, ""


def _candidate_rows(
    df: pd.DataFrame,
    asset: str,
    generated_at: str,
    include_watch: bool,
    allow_zero_size_placeholder: bool,
    max_spread: float,
    open_key_set: set[tuple[str, str, str]],
    now: datetime,
    query: str = "",
    min_option_dte: int = DEFAULT_MIN_OPTION_DTE,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    if df is None or df.empty:
        return selected, excluded
    for _, row in df.iterrows():
        reasons = []
        if _is_stale(row, now):
            reasons.append("stale row")
        status_reason = _status_excluded(row, include_watch)
        if status_reason:
            reasons.append(status_reason)
        guard_reason = _guard_excluded(row)
        if guard_reason:
            reasons.append(guard_reason)
        if asset == "option" and "spread_pct" in row.index and _safe_float(row.get("spread_pct"), 0.0) > max_spread:
            reasons.append("option spread above max acceptable spread")
        if asset == "option":
            dte = _option_dte(row, generated_at)
            if dte is None:
                reasons.append("missing option DTE/expiry")
            elif dte < min_option_dte:
                reasons.append(f"dte below {min_option_dte}")
            if _text(row.get("swing_fit_label")).lower() == "avoid":
                reasons.append("option swing fit is avoid")

        if asset == "option":
            normalized, reason = _normalize_option(row, generated_at, allow_zero_size_placeholder)
            direction = _text(normalized.get("direction")).replace("long_", "") if normalized else _text(row.get("side")).lower()
            ticker = _text(normalized.get("ticker_or_symbol") or row.get("ticker")).upper()
        elif asset == "share":
            normalized, reason = _normalize_share(row, generated_at, allow_zero_size_placeholder)
            direction = "long"
            ticker = _text(normalized.get("ticker_or_symbol") or row.get("ticker")).upper()
        else:
            normalized, reason = _normalize_future(row, generated_at, allow_zero_size_placeholder)
            direction = _text(normalized.get("direction") or row.get("direction")).lower()
            ticker = _text(normalized.get("ticker_or_symbol") or row.get("symbol") or row.get("ticker")).upper()
        if reason:
            reasons.append(reason)
        if ticker and (asset, ticker, direction) in open_key_set:
            reasons.append("duplicate ticker/symbol already open in same direction")

        if not _matches_query(row, normalized, query):
            continue

        if reasons:
            out = normalized if normalized else _base_output(generated_at, asset)
            out["ticker_or_symbol"] = out.get("ticker_or_symbol") or ticker
            out["trade_status"] = out.get("trade_status") or _text(row.get("trade_status"))
            out["reason_selected"] = ""
            out["reason_excluded"] = "; ".join(dict.fromkeys(reasons))
            out["_sort_score"] = _score_row(row)
            excluded.append(out)
        else:
            normalized["_sort_score"] = _score_row(row)
            selected.append(normalized)
    return selected, excluded


def export_candidates(
    options: pd.DataFrame | None,
    shares: pd.DataFrame | None,
    futures: pd.DataFrame | None,
    open_positions: list[dict[str, Any]] | None = None,
    open_share_positions: list[dict[str, Any]] | None = None,
    open_futures_positions: list[dict[str, Any]] | None = None,
    validation_summary: dict[str, Any] | None = None,
    research_guard_report: dict[str, Any] | None = None,
    max_new: int = 5,
    max_open: int = 30,
    max_options: int = 3,
    max_shares: int = 2,
    max_futures: int = 2,
    include_watch: bool = False,
    allow_zero_size_placeholder: bool = False,
    asset: str = "all",
    dry_run: bool = False,
    existing_external_open: int = 0,
    generated_at: str | None = None,
    now: datetime | None = None,
    query: str = "",
    min_option_dte: int = DEFAULT_MIN_OPTION_DTE,
) -> pd.DataFrame:
    """Return normalized paper-tracking candidates.

    In normal mode the returned frame contains only selected candidates. In
    dry-run mode it includes selected and excluded rows with `reason_excluded`.
    """
    del validation_summary, research_guard_report  # reserved for future policy hints
    now = now or datetime.now(timezone.utc)
    generated_at = generated_at or now.isoformat()
    open_key_set = _open_keys(
        (open_positions or []) + (open_share_positions or []) + (open_futures_positions or [])
    )
    frames = {
        "option": options if options is not None else pd.DataFrame(),
        "share": shares if shares is not None else pd.DataFrame(),
        "futures": futures if futures is not None else pd.DataFrame(),
    }
    wanted = {"option", "share", "futures"} if asset == "all" else {asset}
    selected: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for asset_name in ("option", "share", "futures"):
        if asset_name not in wanted:
            continue
        picked, dropped = _candidate_rows(
            frames[asset_name],
            asset_name,
            generated_at,
            include_watch,
            allow_zero_size_placeholder,
            DEFAULT_MAX_SPREAD,
            open_key_set,
            now,
            query=query,
            min_option_dte=min_option_dte,
        )
        selected.extend(picked)
        excluded.extend(dropped)

    selected = sorted(selected, key=lambda row: _safe_float(row.get("_sort_score"), 0.0), reverse=True)

    selected = _apply_limits(
        selected,
        max_new=max_new,
        max_open=max_open,
        max_options=max_options,
        max_shares=max_shares,
        max_futures=max_futures,
        existing_external_open=existing_external_open,
    )
    selected = _apply_sector_limit(selected, DEFAULT_MAX_PER_SECTOR)
    result = selected + excluded if dry_run else selected
    return pd.DataFrame(result, columns=NORMALIZED_COLUMNS)


def _apply_limits(
    rows: list[dict[str, Any]],
    max_new: int,
    max_open: int,
    max_options: int,
    max_shares: int,
    max_futures: int,
    existing_external_open: int,
) -> list[dict[str, Any]]:
    slots = max(0, int(max_open) - int(existing_external_open))
    total_cap = min(int(max_new), slots)
    counts = {"option": 0, "share": 0, "futures": 0}
    per_asset = {"option": max_options, "share": max_shares, "futures": max_futures}
    out: list[dict[str, Any]] = []
    for row in rows:
        if len(out) >= total_cap:
            break
        asset = _text(row.get("asset"))
        if counts.get(asset, 0) >= per_asset.get(asset, total_cap):
            continue
        counts[asset] += 1
        out.append(row)
    return out


def _apply_sector_limit(rows: list[dict[str, Any]], max_per_sector: int) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    out = []
    for row in rows:
        sec = _text(row.get("_sector"))
        if not sec:
            out.append(row)
            continue
        if counts.get(sec, 0) >= max_per_sector:
            continue
        counts[sec] = counts.get(sec, 0) + 1
        out.append(row)
    return out


def build_external_orders(
    data_dir: Path = DATA_DIR,
    max_new: int = 5,
    max_open: int = 30,
    max_options: int = 3,
    max_shares: int = 2,
    max_futures: int = 2,
    include_watch: bool = False,
    allow_zero_size_placeholder: bool = False,
    asset: str = "all",
    dry_run: bool = False,
    query: str = "",
    min_option_dte: int = DEFAULT_MIN_OPTION_DTE,
) -> pd.DataFrame:
    data_dir = Path(data_dir)
    options = _load_latest_parquet(data_dir, "top_options_*.parquet")
    chain_options = _load_option_chain_shortlist(data_dir)
    if not chain_options.empty:
        options = pd.concat([options, chain_options], ignore_index=True, sort=False)
    shares = _load_latest_parquet(data_dir, "top_shares_*.parquet")
    futures = _load_latest_parquet(data_dir, "top_futures_*.parquet")
    guard = _load_json_obj(data_dir / "research_guard_report.json")
    if not guard:
        guard = _load_json_obj(data_dir / "research_guard.json")
    return export_candidates(
        options=options,
        shares=shares,
        futures=futures,
        open_positions=_load_json_rows(data_dir / "open_positions.json"),
        open_share_positions=_load_json_rows(data_dir / "open_share_positions.json"),
        open_futures_positions=_load_json_rows(data_dir / "open_futures_positions.json"),
        validation_summary=_load_json_obj(data_dir / "validation_summary.json"),
        research_guard_report=guard,
        max_new=max_new,
        max_open=max_open,
        max_options=max_options,
        max_shares=max_shares,
        max_futures=max_futures,
        include_watch=include_watch,
        allow_zero_size_placeholder=allow_zero_size_placeholder,
        asset=asset,
        dry_run=dry_run,
        query=query,
        min_option_dte=min_option_dte,
    )


def write_outputs(df: pd.DataFrame, data_dir: Path = DATA_DIR) -> tuple[Path, Path]:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    csv_path = data_dir / "external_paper_orders.csv"
    json_path = data_dir / "external_paper_orders.json"
    df.to_csv(csv_path, index=False)
    json_path.write_text(df.to_json(orient="records", indent=2), encoding="utf-8")
    return csv_path, json_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Export filtered paper-tracking candidates")
    parser.add_argument("--max-new", type=int, default=5)
    parser.add_argument("--max-open", type=int, default=30)
    parser.add_argument("--include-watch", action="store_true")
    parser.add_argument("--allow-zero-size-placeholder", action="store_true")
    parser.add_argument("--asset", choices=["option", "share", "futures", "all"], default="all")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--query", default="", help="Optional ticker, symbol, or contract filter")
    parser.add_argument("--min-option-dte", type=int, default=DEFAULT_MIN_OPTION_DTE)
    args = parser.parse_args()
    df = build_external_orders(
        max_new=args.max_new,
        max_open=args.max_open,
        include_watch=args.include_watch,
        allow_zero_size_placeholder=args.allow_zero_size_placeholder,
        asset=args.asset,
        dry_run=args.dry_run,
        query=args.query,
        min_option_dte=args.min_option_dte,
    )
    if args.dry_run:
        print(df.to_csv(index=False))
        print(f"Dry run: {len(df)} rows reviewed; no files written.")
        return 0
    csv_path, json_path = write_outputs(df)
    print(f"Exported {len(df)} external paper candidates")
    print(f"CSV: {csv_path}")
    print(f"JSON: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
