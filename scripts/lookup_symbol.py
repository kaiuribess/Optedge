"""Local Optedge ticker lookup.

This does not call a broker or paid API. It reads the latest generated Optedge
snapshots and open-position state, then writes a compact ticker report.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from scripts.sec_filings import companyfacts_for_symbol, recent_filings_for_symbol
from scripts.symbol_resolver import resolve_symbol

DATA_DIR = ROOT / "data"
FRESH_SNAPSHOT_MINUTES = 90.0
STALE_SNAPSHOT_MINUTES = 360.0

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
    "requested_option_matches": [
        "ticker", "side", "strike", "expiry", "dte", "mid", "spot", "confidence",
        "rank_score", "fused_score", "trade_status", "suggested_contracts",
        "stop_price", "target_price", "spread_pct", "ev_pct", "net_edge_pct",
        "chain_source", "quote_quality", "snapshot_age_min", "snapshot_freshness",
        "match_quality", "strike_diff", "requested_side", "requested_expiry",
        "requested_strike", "top_headline",
    ],
    "recent_sec_filings": [
        "ticker", "company_name", "form", "filing_date", "report_date",
        "filing_signal", "description", "url",
    ],
    "sec_companyfacts": [
        "ticker", "company_name", "metric", "label", "value", "unit",
        "period_end", "filed", "form", "concept",
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


def _read_json_rows(path: Path) -> pd.DataFrame:
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return pd.DataFrame()
    if not isinstance(rows, list) or not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


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


def _match(df: pd.DataFrame, column: str, query: str) -> pd.DataFrame:
    if df is None or df.empty or column not in df.columns:
        return pd.DataFrame()
    q = query.strip().upper()
    values = df[column].astype(str).str.upper().str.strip()
    return df[values == q].copy()


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
        "matched_spread_pct": _clean_value(best.get("spread_pct")),
        "matched_quote_quality": _clean_value(best.get("quote_quality")),
        "matched_chain_source": _clean_value(best.get("chain_source")),
        "strike_diff": _clean_value(best.get("strike_diff")),
    }


def _best_idea_dict(section: str | None, row: pd.Series | None) -> dict[str, Any] | None:
    if row is None or section is None:
        return None
    symbol = row.get("ticker", row.get("symbol"))
    side = row.get("side", row.get("direction", section))
    label = str(symbol or "-")
    if section == "options":
        label = f"{symbol} {str(side).upper()[:1]} {row.get('strike', '-')} {row.get('expiry', '-')}"
    elif section == "futures":
        label = f"{symbol} {str(side).upper()} {row.get('contract', '')}".strip()
    quote_source = _quote_source_info(row)
    return {
        "asset": section.rstrip("s"),
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
        "headline": _clean_value(row.get("top_headline")),
    }


def _status_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _research_action(
    symbol: str,
    best_idea: dict[str, Any] | None,
    open_summary: dict[str, Any],
    warnings: list[str],
    total_hits: int,
) -> dict[str, Any]:
    """Conservative next-step guidance for a lookup screen."""
    reasons: list[str] = []
    next_steps: list[str] = []
    risk_level = "low"
    action = "review"
    label = "Review local research"

    if total_hits <= 0:
        return {
            "action": "run_focused_scan",
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
    status = _status_text((best_idea or {}).get("trade_status"))

    if open_count > 0:
        reasons.append(f"{open_count} open lifecycle position(s) already exist for {symbol}.")
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
    else:
        action = "watchlist_or_rescan"
        label = "Watchlist or rescan"
        reasons.append("No ranked current idea was found, only position or historical context.")
        next_steps.append("Add it to the watchlist or run a focused scan for a current ranked view.")

    if avg_unreal is not None and open_count > 0:
        reasons.append(f"Average open unrealized P&L is {avg_unreal * 100:+.1f}%.")

    if not next_steps:
        next_steps.append("Read the factor drivers and open exposure before making any manual decision.")

    can_export = action == "paper_candidate_review" and risk_level != "high"
    return {
        "action": action,
        "label": label,
        "risk_level": risk_level,
        "reasons": list(dict.fromkeys(reasons))[:6],
        "next_steps": list(dict.fromkeys(next_steps))[:5],
        "can_export_paper_candidate": can_export,
    }


def _paper_readiness(
    best_idea: dict[str, Any] | None,
    requested_option: dict[str, Any] | None,
    open_summary: dict[str, Any],
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

    max_pressure = _float_value(open_summary.get("max_exit_pressure"), 0.0) or 0.0
    if max_pressure >= 80:
        add("bad", "Open exposure", f"Existing position exit pressure is high ({max_pressure:.0f}/100).", 35)
    elif max_pressure >= 60:
        add("warn", "Open exposure", f"Existing position exit pressure is elevated ({max_pressure:.0f}/100).", 20)
    else:
        add("ok", "Open exposure", "No high exit-pressure open position conflict surfaced.")

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
    sec_rows = sections.get("recent_sec_filings", [])
    sec_facts = sections.get("sec_companyfacts", [])
    sec_fact_report = sections.get("_sec_companyfacts_report", [])
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
    best_idea = _best_idea_dict(best_section, best)
    requested_option = _requested_option_summary(
        resolution.get("request"),
        sections.get("requested_option_matches", []),
    )
    if requested_option:
        quality = str(requested_option.get("match_quality") or "missing").lower()
        if quality == "missing":
            warnings.append(f"Requested option {requested_option.get('label')} was not found in latest local option rows.")
        elif quality != "exact":
            warnings.append(
                f"Requested option {requested_option.get('label')} matched as {quality}; verify before using it."
            )
    if best_idea:
        snapshot_age = _float_value(best_idea.get("snapshot_age_min"))
        if snapshot_age is not None and snapshot_age > STALE_SNAPSHOT_MINUTES:
            warnings.append(
                f"Best idea snapshot is stale ({snapshot_age:.0f} minutes old); run a fresh focused scan."
            )
    deduped_warnings = list(dict.fromkeys(warnings))[:5]
    open_summary = _open_position_summary(open_rows)
    research_action = _research_action(
        symbol, best_idea, open_summary, deduped_warnings, local_hit_count
    )
    paper_readiness = _paper_readiness(
        best_idea, requested_option, open_summary, deduped_warnings,
        research_action, local_hit_count,
    )
    brief = {
        "symbol": symbol,
        "resolved_from": resolution.get("query"),
        "resolution_source": resolution.get("source"),
        "request": resolution.get("request"),
        "requested_option": requested_option,
        "best_idea": best_idea,
        "open_positions": open_summary,
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
    df = _read_parquet(path)
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


def lookup_symbol(query: str, data_dir: Path = DATA_DIR, include_sec: bool = True) -> dict[str, Any]:
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

    for section, (filename, column) in OPEN_FILES.items():
        path = data_dir / filename
        sources[section] = filename if path.exists() else None
        sections[section] = _frame_records(_match(_read_json_rows(path), column, q), section)

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
        sources["requested_option_matches"] = sources.get("options")

    public_sections = {name: rows for name, rows in sections.items() if not name.startswith("_")}
    local_hit_count = sum(
        len(rows) for name, rows in public_sections.items()
        if not name.startswith("sec_") and name != "recent_sec_filings"
    )
    total_hits = sum(len(rows) for rows in public_sections.values())
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
            "Lookup uses latest local Optedge snapshots only.",
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
    readiness = brief.get("paper_readiness") or {}
    open_pos = brief.get("open_positions") or {}
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
    sec_signals = ", ".join(str(x) for x in sec.get("watch_signals", [])[:4]) or "-"
    sec_fund_signals = ", ".join(str(x) for x in sec_fund.get("watch_signals", [])[:4]) or "-"
    return f"""
<section>
  <h2>Research Brief</h2>
  <div class="brief-grid">
    <div><span class="muted">Symbol</span><strong>{html.escape(str(brief.get('symbol') or '-'))}</strong></div>
    <div><span class="muted">Resolved via</span><strong>{html.escape(str(brief.get('resolution_source') or '-'))}</strong></div>
    <div><span class="muted">Requested option</span><strong>{html.escape(str(requested.get('label') or '-'))}</strong></div>
    <div><span class="muted">Requested match</span><strong>{html.escape(str(requested.get('match_quality') or '-'))}</strong></div>
    <div><span class="muted">Matched contract</span><strong>{html.escape(str(requested.get('matched_contract') or '-'))}</strong></div>
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
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args(argv)

    report = lookup_symbol(args.symbol, Path(args.data_dir))
    paths = save_lookup(report, Path(args.data_dir))
    print(json.dumps(report, indent=2, default=str) if args.json_only else f"Lookup report: {paths['html']}\nLookup JSON: {paths['json']}\nHits: {report['total_hits']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
