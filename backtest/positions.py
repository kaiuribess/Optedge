# Purpose: Track reprice expire and close option recommendations.
"""Position-level P&L tracking — v20.7.

Distinguishes "still-open recommendation" from "expired worthless" so the
forward-test win-rate / avg P&L is computed against the right denominator.

Design:
  - `data/open_positions.json` — current view of every still-open
    recommendation (entry date, side, strike, expiry, entry_price, dte).
    Updated each iter when new signals arrive AND when positions expire / close.
  - On each iter:
      1. Add new top-of-board signals as new positions (one row per
         unique (ticker, side, strike, expiry) tuple — dedups across iters).
      2. Re-price every still-open position via chain_provider; compute
         unrealized P&L vs entry_price.
      3. Move expired (DTE ≤ 0) or filled-at-target / hit-stop positions
         to `data/closed_positions.json` with realized P&L.
  - Forward test sees both files; dashboard can show open MTM and closed
    realised P&L separately.

This is independent from `backtest/track.py` (which keeps a row-per-iter log
of EVERY signal regardless of dedup). track.py = signal stream;
positions.py = portfolio state.
"""

from __future__ import annotations

import json
import logging
import math
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.option_expiry import (  # noqa: E402
    expiry_exit_time,
    resolve_expiry_valuations,
    valuation_for_position,
)

log = logging.getLogger("optedge.positions")

DATA_DIR = ROOT / "data"
OPEN_FILE = DATA_DIR / "open_positions.json"
CLOSED_FILE = DATA_DIR / "closed_positions.json"

REENTRY_COOLDOWN_HOURS = 24.0
TRACKED_SIGNAL_PREFIXES = ("z_", "factor_")
TRACKED_SIGNAL_COLS = {
    "rank_score",
    "fused_score",
    "confidence",
    "ev_pct",
    "kelly_pct",
    "prob_win",
    "setup_quality_mult",
    "trade_score",
    "bucket",
    "mispricing_pct",
    "theo_price",
    "buyer_edge_pct",
    "seller_edge_pct",
    "pricing_direction",
    "pricing_edge_ok",
    "pricing_edge_penalty_pct",
    "spread_to_edge_ratio",
    "trade_gate_reason",
    "chain_source",
    "quote_quality",
    "underlying_type",
    "settlement_style",
    "official_settlement_style",
    "official_settlement_value",
    "official_settlement_source",
    "official_settlement_source_id",
    "official_settlement_record_id",
    "official_settlement_published_at",
    "official_settlement_verified",
    "contract_multiplier",
    "trade_value_multiplier",
    "deliverable",
    "deliverable_description",
    "deliverable_type",
    "deliverable_units",
    "is_adjusted_contract",
    "corporate_action_ambiguous",
}


def _truthy(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "trade", "actionable"}
    try:
        if pd.isna(value):
            return False
    except Exception:
        pass
    return bool(value)


def _positive_float(value) -> bool:
    try:
        if pd.isna(value):
            return False
    except Exception:
        pass
    try:
        return float(value) > 0
    except Exception:
        return False


def _is_actionable_signal(s: pd.Series) -> bool:
    """Only promote executable option recommendations into lifecycle tracking."""
    status = str(s.get("trade_status") or "").strip().lower()
    if status and status not in {"trade", "buy", "long"}:
        return False

    guard = str(s.get("research_guard_status") or "").strip().lower()
    if guard == "blocked" or guard.startswith("blocked"):
        return False

    if "is_actionable" in s.index and not _truthy(s.get("is_actionable")):
        return False

    if not _positive_float(s.get("suggested_contracts")):
        return False

    price = s.get("mid")
    if price is None:
        price = s.get("entry_price")
    if not _positive_float(price):
        return False

    return _positive_float(s.get("stop_price")) and _positive_float(s.get("target_price"))


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception as e:
        log.debug("positions load %s: %s", path.name, e)
        return []


def _save(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
        temp.replace(path)
    except Exception as e:
        log.warning("positions save %s: %s", path.name, e)
        raise


def _option_key(row: dict) -> tuple | None:
    ticker = str(row.get("ticker") or "").strip().upper()
    side = str(row.get("side") or "").strip().lower()
    expiry = str(row.get("expiry") or "").strip()
    try:
        strike = round(float(row.get("strike")), 4)
    except Exception:
        return None
    if not ticker or side not in {"call", "put"} or not expiry:
        return None
    return ticker, side, strike, expiry


def _signal_map(current_signals: pd.DataFrame | None) -> dict[tuple, dict]:
    if current_signals is None or current_signals.empty:
        return {}
    out = {}
    for _, r in current_signals.iterrows():
        row = r.to_dict()
        key = _option_key(row)
        if key is not None:
            out[key] = row
    return out


def _recently_closed_option_keys(asof: datetime, cooldown_hours: float) -> set[tuple]:
    if cooldown_hours <= 0:
        return set()
    now = pd.Timestamp(_asof_utc(asof))
    recent: set[tuple] = set()
    for row in _load(CLOSED_FILE):
        exit_time = pd.to_datetime(row.get("exit_time"), errors="coerce", utc=True)
        if pd.isna(exit_time):
            continue
        age_hours = (now - exit_time).total_seconds() / 3600.0
        if 0 <= age_hours < cooldown_hours:
            key = _option_key(row)
            if key is not None:
                recent.add(key)
    return recent


def _option_position_id(key: tuple, entry_time: str) -> str:
    ticker, side, strike, expiry = key
    return f"option|{ticker}|{side}|{float(strike):g}|{expiry}|{entry_time}"


def add_new_signals(
    new_signals: pd.DataFrame,
    asof: datetime,
    reentry_cooldown_hours: float = REENTRY_COOLDOWN_HOURS,
) -> int:
    """Insert any new (ticker, side, strike, expiry) tuples not already open.
    Returns the number of new positions added."""
    if new_signals is None or new_signals.empty:
        return 0
    now = _asof_utc(asof)
    open_rows = _load(OPEN_FILE)
    existing = {key for row in open_rows if (key := _option_key(row)) is not None}
    recently_closed = _recently_closed_option_keys(now, reentry_cooldown_hours)
    added = 0
    for _, s in new_signals.iterrows():
        if not _is_actionable_signal(s):
            continue
        key = _option_key(s.to_dict())
        if key is None or key in existing or key in recently_closed:
            continue
        expiry_dt = _expiry_datetime(s.to_dict())
        if expiry_dt is not None and now.date() > expiry_dt.date():
            continue
        entry_time = now.isoformat()
        entry_price = s.get("mid")
        if not _positive_float(entry_price):
            entry_price = s.get("entry_price")
        row = {
            "asset": "option",
            "position_id": _option_position_id(key, entry_time),
            "ticker": s.get("ticker"),
            "side": s.get("side"),
            "strike": float(s.get("strike") or 0),
            "expiry": s.get("expiry"),
            "dte_at_entry": int(s.get("dte") or 0),
            "entry_price": float(entry_price or 0),
            "entry_spot": float(s.get("spot") or 0),
            "entry_iv": float(s.get("iv_market") or 0),
            "entry_delta": float(s.get("delta") or 0),
            "spread_pct": float(s.get("spread_pct") or 0),
            "net_edge_pct": float(s.get("net_edge_pct") or 0),
            "entry_time": entry_time,
            "entry_is_actionable": True,
            "entry_trade_status": s.get("trade_status"),
            "entry_research_guard_status": s.get("research_guard_status"),
            "fused_score": float(s.get("fused_score") or 0),
            "confidence": float(s.get("confidence") or 0),
            "suggested_contracts": int(float(s.get("suggested_contracts") or 0)),
            "trade_status": s.get("trade_status"),
            "research_guard_status": s.get("research_guard_status"),
            "research_guard_warnings": s.get("research_guard_warnings"),
            "stop_price": float(s.get("stop_price") or 0),
            "target_price": float(s.get("target_price") or 0),
        }
        for col in s.index:
            if col in row:
                continue
            if col in TRACKED_SIGNAL_COLS or any(
                str(col).startswith(p) for p in TRACKED_SIGNAL_PREFIXES
            ):
                value = s.get(col)
                try:
                    if pd.isna(value):
                        continue
                except Exception:
                    pass
                if hasattr(value, "item"):
                    value = value.item()
                row[str(col)] = value
        open_rows.append(row)
        existing.add(key)
        added += 1
    if added:
        _save(OPEN_FILE, open_rows)
        log.info("positions: +%d new opens (total open=%d)", added, len(open_rows))
    return added


def _current_mid_for_position(pos: dict, chain_blobs: dict[str, dict]) -> float | None:
    blob = chain_blobs.get((pos.get("ticker") or "").upper())
    if not blob:
        return None
    df = blob.get("chains", {}).get(str(pos.get("expiry")))
    if df is None or getattr(df, "empty", True):
        return None
    hit = df[
        (df["strike"].round(2) == round(float(pos.get("strike") or 0), 2))
        & (df["side"] == pos.get("side"))
    ]
    if hit.empty:
        return None
    r = hit.iloc[0]
    bid, ask = float(r.get("bid") or 0), float(r.get("ask") or 0)
    if bid > 0 and ask > 0 and ask >= bid:
        return (bid + ask) / 2
    last = float(r.get("lastPrice") or 0)
    return last if last > 0 else None


def _asof_utc(asof: datetime | None = None) -> datetime:
    if isinstance(asof, datetime):
        if asof.tzinfo is None:
            return asof.replace(tzinfo=UTC)
        return asof.astimezone(UTC)
    return datetime.now(UTC)


def _expiry_datetime(pos: dict) -> datetime | None:
    raw = str(pos.get("expiry") or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=UTC)
        except Exception:
            pass
    try:
        parsed = pd.to_datetime(raw, errors="coerce", utc=True)
        if pd.isna(parsed):
            return None
        return parsed.to_pydatetime()
    except Exception:
        return None


def _is_expired(pos: dict, now: datetime) -> bool:
    exp_dt = _expiry_datetime(pos)
    if exp_dt is None:
        return False
    raw = str(pos.get("expiry") or "").strip()
    if raw and "T" not in raw:
        return now.date() > exp_dt.date()
    return now >= exp_dt


def _age_days(pos: dict, now: datetime) -> float | None:
    try:
        entry_ts = pd.to_datetime(pos.get("entry_time"), errors="coerce", utc=True)
        if pd.isna(entry_ts):
            return None
        return max(0.0, (pd.Timestamp(now) - entry_ts).total_seconds() / 86400.0)
    except Exception:
        return None


def _closed_identity(row: dict) -> tuple:
    position_id = str(row.get("position_id") or "").strip()
    if position_id:
        return ("position_id", position_id)
    return ("contract_entry", _option_key(row), str(row.get("entry_time") or ""))


def merge_closed_rows(
    existing: list[dict], incoming: list[dict]
) -> tuple[list[dict], list[dict], int]:
    """Append closed positions without duplicating an existing lifecycle row."""
    merged = list(existing)
    seen = {_closed_identity(row) for row in merged if isinstance(row, dict)}
    added: list[dict] = []
    duplicate_count = 0
    for row in incoming:
        identity = _closed_identity(row)
        if identity in seen:
            duplicate_count += 1
            continue
        seen.add(identity)
        merged.append(row)
        added.append(row)
    return merged, added, duplicate_count


def build_expired_close_row(
    pos: dict,
    recorded_at: datetime,
    expiry_valuations: dict[str, dict[str, Any]] | None = None,
) -> dict:
    valuation = valuation_for_position(pos, expiry_valuations)
    final = valuation.get("option_value")
    source = valuation.get("price_source") or "unresolved_no_expiry_market_data"
    try:
        entry = float(pos.get("entry_price") or 0)
    except Exception:
        entry = 0.0
    pnl_pct = ((float(final) - entry) / entry) if final is not None and entry > 0 else None
    contracts = max(0, int(float(pos.get("suggested_contracts") or 0)))
    pnl_dollars = (
        (float(final) - entry) * 100.0 * contracts
        if final is not None and entry > 0 and contracts > 0
        else None
    )
    effective_exit_time = expiry_exit_time(pos) or recorded_at
    if source == "broker_option_trade_bar_on_expiry":
        outcome_quality = "broker_market_observed"
    elif source == "intrinsic_from_official_settlement_value":
        outcome_quality = "official_expiry_settlement"
    elif source == "intrinsic_proxy_from_underlying_expiry_close":
        outcome_quality = "expiry_intrinsic_proxy"
    else:
        outcome_quality = "unresolved"
    return {
        **pos,
        "underlying_type": valuation.get("underlying_type") or pos.get("underlying_type"),
        "settlement_style": valuation.get("settlement_style") or pos.get("settlement_style"),
        "contract_multiplier": valuation.get("contract_multiplier"),
        "deliverable": valuation.get("deliverable"),
        "exit_time": effective_exit_time.isoformat(),
        "lifecycle_recorded_at": recorded_at.isoformat(),
        "exit_price": final,
        "exit_reason": "expired",
        "pnl_pct": pnl_pct,
        "pnl_dollars": pnl_dollars,
        "age_days": _age_days(pos, effective_exit_time),
        "trade_status": "Closed",
        "latest_exit_action": "expired",
        "latest_exit_pressure": 100.0,
        "expiry_close_price_source": source,
        "expiry_valuation_date": valuation.get("valuation_date"),
        "expiry_underlying_price": valuation.get("underlying_price"),
        "expiry_underlying_price_date": valuation.get("underlying_price_date"),
        "expiry_underlying_session_gap_days": valuation.get("underlying_session_gap_days"),
        "expiry_underlying_session_provenance": valuation.get("underlying_session_provenance"),
        "expiry_underlying_type": valuation.get("underlying_type"),
        "expiry_settlement_style": valuation.get("settlement_style"),
        "expiry_official_settlement_value": valuation.get("official_settlement_value"),
        "expiry_official_settlement_source": valuation.get("official_settlement_source"),
        "expiry_official_settlement_source_id": valuation.get("official_settlement_source_id"),
        "expiry_official_settlement_published_at": valuation.get(
            "official_settlement_published_at"
        ),
        "expiry_official_settlement_verified": valuation.get("official_settlement_verified"),
        "expiry_contract_multiplier": valuation.get("contract_multiplier"),
        "expiry_deliverable": valuation.get("deliverable"),
        "expiry_deliverable_is_standard": valuation.get("deliverable_is_standard"),
        "expiry_corporate_action_ambiguous": valuation.get("corporate_action_ambiguous"),
        "expiry_underlying_price_basis": valuation.get("underlying_price_basis"),
        "expiry_underlying_history_source": valuation.get("underlying_history_source"),
        "expiry_underlying_history_quality": valuation.get("underlying_history_quality"),
        "expiry_option_bar_date": valuation.get("option_bar_date"),
        "expiry_option_instrument_id": valuation.get("option_instrument_id"),
        "expiry_settlement_is_proxy": bool(valuation.get("settlement_is_proxy", True)),
        "outcome_quality": outcome_quality,
        "validation_eligible": bool(valuation.get("validation_eligible", False)),
        "validation_exclusion_reason": valuation.get("validation_exclusion_reason"),
    }


def _expiry_final_value(
    pos: dict,
    expiry_valuations: dict[str, dict[str, Any]] | None = None,
) -> tuple[float | None, str]:
    valuation = valuation_for_position(pos, expiry_valuations)
    return valuation.get("option_value"), str(valuation.get("price_source") or "")


def _expired_close_row(
    pos: dict,
    now: datetime,
    expiry_valuations: dict[str, dict[str, Any]] | None = None,
) -> dict:
    return build_expired_close_row(pos, now, expiry_valuations)


def close_expired_positions(
    asof: datetime | None = None,
    chain_blobs: dict[str, dict] | None = None,
    log_reviews: bool = True,
    expiry_valuations: dict[str, dict[str, Any]] | None = None,
    history_fetcher: Callable[..., pd.DataFrame] | None = None,
    option_history_path: Path | None = None,
    fetch_expiry_history: bool = True,
) -> dict[str, float]:
    """Move expired local option positions from open to closed.

    This is a lightweight safety fallback used by normal runs after MTM. It does
    not need option-chain access, so stale expired records cannot survive just
    because repricing failed.
    """
    open_rows = _load(OPEN_FILE)
    if not open_rows:
        return {"open": 0, "closed_this_iter": 0, "mean_unrealized_pct": 0.0}

    now = _asof_utc(asof)
    still_open: list[dict] = []
    expired_open: list[dict] = []
    for pos in open_rows:
        if _is_expired(pos, now):
            expired_open.append(pos)
        else:
            still_open.append(pos)

    if not expired_open:
        return {"open": len(open_rows), "closed_this_iter": 0, "mean_unrealized_pct": 0.0}

    valuations = dict(expiry_valuations or {})
    if fetch_expiry_history:
        try:
            resolved = resolve_expiry_valuations(
                expired_open,
                asof=now,
                history_fetcher=history_fetcher,
                option_history_path=option_history_path
                or (DATA_DIR / "robinhood_option_history_snapshot.json"),
            )
            for key, value in resolved.items():
                valuations.setdefault(key, value)
        except Exception as exc:
            log.debug("expiry valuation lookup skipped: %s", exc)
    newly_closed = [build_expired_close_row(pos, now, valuations) for pos in expired_open]
    prev_closed = _load(CLOSED_FILE)
    merged_closed, added_closed, duplicate_count = merge_closed_rows(prev_closed, newly_closed)

    # Durably append the outcome before removing it from the open book. If the
    # closed-history write fails, _save raises and OPEN_FILE remains untouched.
    if added_closed:
        _save(CLOSED_FILE, merged_closed)
    _save(OPEN_FILE, still_open)

    if log_reviews:
        try:
            from backtest.exit_rules import compute_exit_pressure, log_exit_review

            for closed in added_closed:
                review = compute_exit_pressure(closed, None, asset="option")
                review.update(
                    {
                        "action": "expired",
                        "current_price": closed.get("exit_price"),
                        "current_pnl_pct": closed.get("pnl_pct"),
                        "reasons": list(
                            dict.fromkeys([*review.get("reasons", []), "option expired"])
                        ),
                    }
                )
                log_exit_review(review)
        except Exception as e:
            log.debug("expired position review logging skipped: %s", e)
    log.info(
        "positions: removed %d expired option(s) from open; added %d closed (%d already closed)",
        len(expired_open),
        len(added_closed),
        duplicate_count,
    )
    return {
        "open": len(still_open),
        "closed_this_iter": len(added_closed),
        "expired_removed_from_open": len(expired_open),
        "deduped_existing_closed": duplicate_count,
        "mean_unrealized_pct": 0.0,
    }


def mark_to_market(
    asof: datetime, max_chain_fetch: int = 60, current_signals: pd.DataFrame | None = None
) -> dict[str, float]:
    """Re-fetch the current chain for each unique open-position ticker and
    compute per-position unrealized P&L. Move expired/triggered positions
    to closed. Returns a small summary dict for logging."""
    expiry_summary = close_expired_positions(asof)
    expired_closed = int(expiry_summary.get("closed_this_iter", 0) or 0)
    expired_removed = int(expiry_summary.get("expired_removed_from_open", 0) or 0)
    open_rows = _load(OPEN_FILE)
    if not open_rows:
        return {
            "open": 0,
            "closed_this_iter": expired_closed,
            "expired_closed": expired_closed,
            "expired_removed_from_open": expired_removed,
            "mean_unrealized_pct": 0.0,
        }
    try:
        from backtest.exit_rules import (
            apply_dynamic_exit_action,
            compute_exit_pressure,
            log_exit_review,
        )
    except Exception:
        apply_dynamic_exit_action = compute_exit_pressure = log_exit_review = None
    signal_lookup = _signal_map(current_signals)

    try:
        import chain_provider
    except Exception:
        log.debug("positions: chain_provider unavailable, skipping MTM")
        return {
            "open": len(open_rows),
            "closed_this_iter": expired_closed,
            "expired_closed": expired_closed,
            "expired_removed_from_open": expired_removed,
            "mean_unrealized_pct": 0.0,
        }

    tickers = sorted({(r.get("ticker") or "").upper() for r in open_rows if r.get("ticker")})
    if len(tickers) > max_chain_fetch:
        # Prioritize the freshest entries (most recent entry_time)
        recent_tk = (
            pd.DataFrame(open_rows)
            .sort_values("entry_time", ascending=False)
            .head(max_chain_fetch)["ticker"]
            .astype(str)
            .str.upper()
            .tolist()
        )
        tickers = list(dict.fromkeys(recent_tk))

    chains: dict[str, dict] = {}
    for tk in tickers:
        try:
            b = chain_provider.fetch_chain(tk, cache_age=600)
            if b and b.get("chains"):
                chains[tk] = b
        except Exception:
            continue

    still_open: list[dict] = []
    newly_closed: list[dict] = []
    terminal_reviews: list[dict] = []
    unrealized_pcts: list[float] = []
    now = asof if isinstance(asof, datetime) else datetime.now(UTC)
    for pos in open_rows:
        current_signal = signal_lookup.get(_option_key(pos))
        try:
            entry_ts = pd.to_datetime(pos.get("entry_time"), errors="coerce", utc=True)
            age_days = max(0.0, (pd.Timestamp(now) - entry_ts).total_seconds() / 86400.0)
        except Exception:
            age_days = None
        cur_mid = _current_mid_for_position(pos, chains)
        if cur_mid is None:
            # Couldn't reprice — keep open, no MTM update
            pos2 = {
                **pos,
                "age_days": age_days,
                "reprice_failed_count": int(pos.get("reprice_failed_count") or 0) + 1,
            }
            if compute_exit_pressure and apply_dynamic_exit_action and log_exit_review:
                review = compute_exit_pressure(pos2, current_signal, asset="option")
                log_exit_review(review)
                pos2 = apply_dynamic_exit_action(pos2, review)
            still_open.append(pos2)
            continue
        entry = float(pos.get("entry_price") or 0)
        if entry <= 0:
            still_open.append(
                {
                    **pos,
                    "current_mid": cur_mid,
                    "age_days": age_days,
                    "last_reprice_source": "chain",
                }
            )
            continue
        pnl_pct = (cur_mid - entry) / entry
        unrealized_pcts.append(pnl_pct)
        # Stop / target triggers
        stop = float(pos.get("stop_price") or 0)
        target = float(pos.get("target_price") or 0)
        if stop > 0 and cur_mid <= stop:
            closed = {
                **pos,
                "exit_time": now.isoformat(),
                "exit_price": cur_mid,
                "exit_reason": "hard_stop",
                "pnl_pct": pnl_pct,
                "age_days": age_days,
            }
            if compute_exit_pressure and log_exit_review:
                review = compute_exit_pressure(closed, current_signal, asset="option")
                review.update(
                    {"action": "hard_stop", "current_price": cur_mid, "current_pnl_pct": pnl_pct}
                )
                terminal_reviews.append(review)
            newly_closed.append(closed)
            continue
        if target > 0 and cur_mid >= target:
            closed = {
                **pos,
                "exit_time": now.isoformat(),
                "exit_price": cur_mid,
                "exit_reason": "hard_target",
                "pnl_pct": pnl_pct,
                "age_days": age_days,
            }
            if compute_exit_pressure and log_exit_review:
                review = compute_exit_pressure(closed, current_signal, asset="option")
                review.update(
                    {"action": "hard_target", "current_price": cur_mid, "current_pnl_pct": pnl_pct}
                )
                terminal_reviews.append(review)
            newly_closed.append(closed)
            continue
        pos2 = {
            **pos,
            "current_mid": cur_mid,
            "current_price": cur_mid,
            "unrealized_pct": pnl_pct,
            "age_days": age_days,
            "last_reprice_source": "chain",
        }
        if compute_exit_pressure and apply_dynamic_exit_action and log_exit_review:
            review = compute_exit_pressure(pos2, current_signal, asset="option")
            if review["action"] == "close_early":
                terminal_reviews.append(review)
                newly_closed.append(
                    {
                        **pos2,
                        "exit_time": now.isoformat(),
                        "exit_price": cur_mid,
                        "exit_reason": "dynamic_exit",
                        "pnl_pct": pnl_pct,
                    }
                )
                continue
            log_exit_review(review)
            pos2 = apply_dynamic_exit_action(pos2, review, current_price=cur_mid)
        still_open.append(pos2)

    added_closed: list[dict] = []
    duplicate_count = 0
    if newly_closed:
        prev_closed = _load(CLOSED_FILE)
        merged_closed, added_closed, duplicate_count = merge_closed_rows(
            prev_closed,
            newly_closed,
        )
        if added_closed:
            _save(CLOSED_FILE, merged_closed)
    _save(OPEN_FILE, still_open)
    if log_exit_review:
        for review in terminal_reviews:
            try:
                log_exit_review(review)
            except Exception as exc:
                log.debug("terminal option exit review logging skipped: %s", exc)
    mean_un = (sum(unrealized_pcts) / len(unrealized_pcts)) if unrealized_pcts else 0.0
    total_closed = expired_closed + len(added_closed)
    log.info(
        "positions: %d open (mean unrealized %+.1f%%), %d closed this iter",
        len(still_open),
        mean_un * 100,
        total_closed,
    )
    return {
        "open": len(still_open),
        "closed_this_iter": total_closed,
        "expired_closed": expired_closed,
        "expired_removed_from_open": expired_removed,
        "deduped_existing_closed": duplicate_count,
        "mean_unrealized_pct": mean_un,
    }


def summary() -> dict[str, float]:
    """Roll-up of open + closed positions. Useful for the dashboard."""
    open_rows = _load(OPEN_FILE)
    closed_rows = _load(CLOSED_FILE)
    closed_pnls = []
    unresolved_count = 0
    for row in closed_rows:
        pnl = row.get("pnl_pct")
        if pnl is None or row.get("validation_eligible") is False:
            unresolved_count += 1
            continue
        try:
            value = float(pnl)
        except (TypeError, ValueError):
            unresolved_count += 1
            continue
        if math.isfinite(value):
            closed_pnls.append(value)
        else:
            unresolved_count += 1
    realized_win_rate = (
        (sum(1 for p in closed_pnls if p > 0) / len(closed_pnls)) if closed_pnls else 0.0
    )
    realized_avg = (sum(closed_pnls) / len(closed_pnls)) if closed_pnls else 0.0
    return {
        "open_count": len(open_rows),
        "closed_count": len(closed_rows),
        "priced_closed_count": len(closed_pnls),
        "unresolved_closed_count": unresolved_count,
        "realized_win_rate": realized_win_rate,
        "realized_avg_pnl_pct": realized_avg,
    }


def aging_summary(asof: datetime | None = None) -> dict[str, object]:
    """Summarize open recommendation age so stale theses are visible."""
    rows = _load(OPEN_FILE)
    if not rows:
        return {"open_count": 0, "buckets": [], "oldest": []}
    now = asof or datetime.now(UTC)
    df = pd.DataFrame(rows)
    df["entry_time"] = pd.to_datetime(df.get("entry_time"), errors="coerce", utc=True)
    df["age_days"] = (pd.Timestamp(now) - df["entry_time"]).dt.total_seconds() / 86400.0
    df["age_days"] = df["age_days"].clip(lower=0)
    bins = [-0.01, 1, 3, 7, 14, 30, float("inf")]
    labels = ["0-1d", "1-3d", "3-7d", "7-14d", "14-30d", "30d+"]
    df["age_bucket"] = pd.cut(df["age_days"], bins=bins, labels=labels)
    buckets = []
    for label, sub in df.groupby("age_bucket", observed=True):
        avg_unrealized = None
        if "unrealized_pct" in sub.columns:
            avg = pd.to_numeric(sub["unrealized_pct"], errors="coerce").mean()
            avg_unrealized = None if pd.isna(avg) else float(avg)
        buckets.append(
            {
                "bucket": str(label),
                "count": int(len(sub)),
                "avg_unrealized_pct": avg_unrealized,
            }
        )
    oldest_cols = [
        c
        for c in (
            "ticker",
            "side",
            "expiry",
            "entry_time",
            "age_days",
            "unrealized_pct",
            "confidence",
            "trade_status",
        )
        if c in df.columns
    ]
    oldest = (
        df.sort_values("age_days", ascending=False).head(20)[oldest_cols].to_dict(orient="records")
    )
    return {"open_count": int(len(df)), "buckets": buckets, "oldest": oldest}
