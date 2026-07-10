"""Leakage-resistant fixed-horizon forward outcomes.

The live forward monitor answers "where is this signal now?" and therefore
mixes many holding periods. This module answers a different research question:
"what happened after exactly N completed market sessions?"

Shares and futures use observed historical closes. Options use a deliberately
labeled Black-Scholes proxy with entry IV held constant because free historical
option bid/ask data is not reliably available for the full universe.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import sys
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_provider  # noqa: E402
from utils import bs_price  # noqa: E402

log = logging.getLogger("optedge.fixed_horizon")
DATA_DIR = ROOT / "data"
OUTCOMES_PATH = DATA_DIR / "fixed_horizon_outcomes.parquet"
SUMMARY_PATH = DATA_DIR / "fixed_horizon_summary.json"

METHODOLOGY_VERSION = 4
DEFAULT_HORIZONS = (1, 3, 5, 10, 20)
HEADLINE_HORIZON = 10
MIN_RELIABLE_SAMPLES = 100
MIN_RELIABLE_DAYS = 10
SHARE_SLIPPAGE_PCT = 0.002
FUTURES_SLIPPAGE_PCT = 0.001
DEFAULT_SIGNAL_ALLOCATION_PCT = 0.01

HistoryLoader = Callable[[str, str], pd.DataFrame]


def _float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _optional_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _first_number(*values: Any) -> float | None:
    for value in values:
        number = _optional_float(value)
        if number is not None:
            return number
    return None


def _text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        try:
            if pd.isna(value):
                continue
        except (TypeError, ValueError):
            pass
        text = str(value).strip()
        if text and text.lower() not in {"nan", "none", "null"}:
            return text
    return ""


def _truthy(value: Any) -> bool | None:
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "long", "buy"}:
        return True
    if text in {"0", "false", "no", "n", "short", "sell"}:
        return False
    return None


def _option_slippage_pct() -> float:
    try:
        from config import FILL_SLIPPAGE_PCT

        return max(0.0, float(FILL_SLIPPAGE_PCT))
    except Exception:
        return 0.04


def _asset_symbol(row: pd.Series) -> str:
    return _text(row.get("ticker"), row.get("symbol")).upper()


def _direction(row: pd.Series, asset: str) -> str:
    if asset == "option":
        side = _text(row.get("side")).lower()
        bought = _truthy(row.get("is_buy"))
        prefix = "long" if bought is not False else "short"
        return f"{prefix}_{side}" if side in {"call", "put"} else prefix
    if asset == "share":
        side = (_text(row.get("side"), row.get("direction")) or "long").lower()
        return "short" if side in {"short", "sell"} else "long"
    direction = _text(row.get("direction"), row.get("side")).lower()
    if direction in {"long", "buy", "long futures"}:
        return "long"
    if direction in {"short", "sell", "short futures"}:
        return "short"
    is_long = _truthy(row.get("is_long"))
    return "long" if is_long is not False else "short"


def _contract_key(row: pd.Series, asset: str) -> str:
    symbol = _asset_symbol(row)
    if asset == "option":
        contract = _text(row.get("contract"))
        if contract:
            return contract
        return "|".join([
            symbol,
            str(row.get("expiry") or ""),
            str(row.get("side") or ""),
            str(row.get("strike") or ""),
        ])
    if asset == "futures":
        return _text(row.get("contract"), row.get("micro_contract"), symbol)
    return symbol


def _signal_id(row: pd.Series) -> str:
    asset = str(row.get("asset") or "").lower()
    entry = pd.to_datetime(row.get("entry_time"), errors="coerce", utc=True)
    payload = "|".join([
        asset,
        _contract_key(row, asset),
        _direction(row, asset),
        entry.isoformat() if not pd.isna(entry) else str(row.get("entry_time") or ""),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _execution_eligibility(row: pd.Series, asset: str) -> tuple[bool, str]:
    status = _text(row.get("trade_status")).lower()
    if status in {"watch", "skip", "blocked", "rejected"}:
        return False, f"status_{status}"
    guard = _text(row.get("research_guard_status")).lower()
    if guard in {"blocked", "block", "fail", "failed"}:
        return False, "research_guard_blocked"
    actionable = _truthy(row.get("is_actionable"))
    if actionable is False:
        return False, "explicit_not_actionable"

    if asset == "option":
        buyer_edge = _optional_float(row.get("buyer_edge_pct"))
        pricing_edge_ok = _truthy(row.get("pricing_edge_ok"))
        if buyer_edge is None:
            return False, "missing_directional_buyer_edge"
        if buyer_edge < 0 or pricing_edge_ok is False:
            return False, "negative_buyer_edge_after_spread"

    if asset in {"option", "futures"}:
        size = _first_number(row.get("suggested_contracts"), row.get("n_contracts"))
        if size is None or size <= 0:
            return False, "non_positive_contracts"
    elif asset == "share":
        quantity = _optional_float(row.get("quantity"))
        dollars = _optional_float(row.get("suggested_dollars"))
        if not ((quantity is not None and quantity > 0) or (dollars is not None and dollars > 0)):
            return False, "non_positive_share_size"

    if status and status != "trade":
        return False, f"status_{status}"
    if not status and actionable is not True:
        return False, "legacy_unverified_actionability"
    return True, "passed"


def _shadow_eligibility(row: pd.Series, asset: str) -> tuple[bool, str]:
    """Current strategy qualification before portfolio-level safety blocks."""
    if _truthy(row.get("strategy_qualified_pre_guard")) is not True:
        return False, "not_strategy_qualified_pre_guard"
    if asset == "option":
        buyer_edge = _optional_float(row.get("buyer_edge_pct"))
        if buyer_edge is None:
            return False, "missing_directional_buyer_edge"
        if buyer_edge < 0 or _truthy(row.get("pricing_edge_ok")) is False:
            return False, "negative_buyer_edge_after_spread"
        size = _first_number(
            row.get("pre_guard_suggested_contracts"), row.get("suggested_contracts"),
        )
    elif asset == "futures":
        size = _first_number(
            row.get("pre_guard_suggested_contracts"), row.get("suggested_contracts"),
        )
    else:
        size = _first_number(
            row.get("pre_guard_suggested_dollars"), row.get("suggested_dollars"),
        )
    if size is None or size <= 0:
        return False, "non_positive_pre_guard_size"
    return True, "passed"


def prepare_signals(signals: pd.DataFrame) -> pd.DataFrame:
    """Normalize logs and mark one independent thesis per asset/direction/day."""
    if signals is None or signals.empty:
        return pd.DataFrame()
    out = signals.copy()
    if "asset" not in out.columns:
        out["asset"] = np.where(
            out.get("side", pd.Series("", index=out.index)).isin(["call", "put"]),
            "option",
            "share",
        )
    out["asset"] = out["asset"].fillna("").astype(str).str.lower()
    out["entry_time"] = pd.to_datetime(out.get("entry_time"), errors="coerce", utc=True)
    out = out[out["asset"].isin(["option", "share", "futures"])].copy()
    out = out.dropna(subset=["entry_time"])
    if out.empty:
        return out
    out["symbol"] = out.apply(_asset_symbol, axis=1)
    out = out[out["symbol"].ne("")].copy()
    out["direction"] = out.apply(lambda row: _direction(row, str(row["asset"])), axis=1)
    out["contract_key"] = out.apply(
        lambda row: _contract_key(row, str(row["asset"])), axis=1,
    )
    out["signal_id"] = out.apply(_signal_id, axis=1)
    out = out.drop_duplicates("signal_id", keep="first")
    out["entry_date"] = out["entry_time"].dt.date.astype(str)
    out["independent_key"] = (
        out["asset"] + "|" + out["symbol"] + "|" + out["direction"] + "|" + out["entry_date"]
    )
    out = out.sort_values(["entry_time", "signal_id"]).reset_index(drop=True)
    out["is_independent"] = ~out.duplicated("independent_key", keep="first")
    eligibility = [
        _execution_eligibility(row, str(row["asset"])) for _, row in out.iterrows()
    ]
    out["eligible_for_executable_metrics"] = [value[0] for value in eligibility]
    out["execution_eligibility_reason"] = [value[1] for value in eligibility]
    shadow = [
        _shadow_eligibility(row, str(row["asset"])) for _, row in out.iterrows()
    ]
    out["eligible_for_shadow_metrics"] = [value[0] for value in shadow]
    out["shadow_eligibility_reason"] = [value[1] for value in shadow]
    return out


def _normalize_history(history: pd.DataFrame, asof: datetime) -> pd.DataFrame:
    if history is None or history.empty or "Close" not in history.columns:
        return pd.DataFrame()
    out = history.copy()
    idx = pd.to_datetime(out.index, errors="coerce", utc=True)
    valid = ~idx.isna()
    out = out.loc[valid].copy()
    out.index = idx[valid]
    out["Close"] = pd.to_numeric(out["Close"], errors="coerce")
    out = out.dropna(subset=["Close"]).sort_index()
    if out.empty:
        return out
    out["_session_date"] = [timestamp.date() for timestamp in out.index]
    out = out[out["_session_date"] < asof.date()].copy()
    return out.drop_duplicates("_session_date", keep="last")


def _target_bar(history: pd.DataFrame, entry_date: date, horizon: int) -> pd.Series | None:
    after = history[history["_session_date"] > entry_date]
    if len(after) < horizon:
        return None
    return after.iloc[horizon - 1]


def _close_on_or_before(history: pd.DataFrame, target_date: date) -> float | None:
    rows = history[history["_session_date"] <= target_date]
    if rows.empty:
        return None
    return _optional_float(rows.iloc[-1].get("Close"))


def _benchmark_return(history: pd.DataFrame, entry_date: date, target_date: date) -> float | None:
    if history is None or history.empty:
        return None
    entry_rows = history[history["_session_date"] == entry_date]
    if entry_rows.empty:
        entry_rows = history[history["_session_date"] < entry_date]
    target_rows = history[history["_session_date"] <= target_date]
    if entry_rows.empty or target_rows.empty:
        return None
    entry = _optional_float(entry_rows.iloc[-1].get("Close"))
    target = _optional_float(target_rows.iloc[-1].get("Close"))
    if entry is None or target is None or entry <= 0:
        return None
    return target / entry - 1.0


def _entry_price(row: pd.Series, asset: str) -> float:
    candidates = (
        ("mid", "entry_price", "current_price")
        if asset == "option"
        else ("entry_price", "entry", "spot", "current_price")
    )
    for column in candidates:
        value = _optional_float(row.get(column))
        if value is not None and value > 0:
            return value
    return 0.0


def _option_outcome(
    row: pd.Series,
    target_underlying: float,
    target_date: date,
) -> tuple[float, float, str] | tuple[None, None, str]:
    entry = _entry_price(row, "option")
    strike = _float(row.get("strike"))
    iv = _float(row.get("iv_market"))
    side = _text(row.get("side")).lower()
    expiry = pd.to_datetime(row.get("expiry"), errors="coerce")
    if entry <= 0 or strike <= 0 or iv <= 0 or side not in {"call", "put"} or pd.isna(expiry):
        return None, None, "invalid_option_inputs"
    expiry_date = expiry.date()
    if expiry_date < target_date:
        return None, None, "expiry_before_horizon"
    remaining_days = max(0, (expiry_date - target_date).days)
    if remaining_days == 0:
        target_price = max(
            0.0,
            target_underlying - strike if side == "call" else strike - target_underlying,
        )
    else:
        target_price = bs_price(
            target_underlying,
            strike,
            remaining_days / 365.25,
            0.045,
            iv,
            0.0,
            call=side == "call",
        )
    bought = _truthy(row.get("is_buy")) is not False
    pnl = (target_price - entry) / entry if bought else (entry - target_price) / entry
    return float(target_price), float(pnl), "bs_constant_entry_iv_proxy"


def _market_outcome(
    row: pd.Series,
    asset: str,
    target_price: float,
) -> tuple[float, float, str] | tuple[None, None, str]:
    entry = _entry_price(row, asset)
    if entry <= 0 or target_price <= 0:
        return None, None, "invalid_entry_or_target_price"
    raw_return = target_price / entry - 1.0
    direction = _direction(row, asset)
    pnl = -raw_return if direction == "short" else raw_return
    method = "observed_equity_close" if asset == "share" else "observed_futures_close"
    return target_price, pnl, method


def _proxy_futures_outcome(
    row: pd.Series,
    history: pd.DataFrame,
    entry_date: date,
    target_date: date,
) -> tuple[float, float, str] | tuple[None, None, str]:
    entry_proxy = _benchmark_return(history, entry_date, target_date)
    entry = _entry_price(row, "futures")
    if entry_proxy is None or entry <= 0:
        return None, None, "invalid_futures_proxy"
    raw_return = entry_proxy
    pnl = -raw_return if _direction(row, "futures") == "short" else raw_return
    target = entry * (1.0 + raw_return)
    return target, pnl, "etf_proxy_return"


def _slippage(asset: str) -> float:
    if asset == "option":
        return _option_slippage_pct()
    if asset == "share":
        return SHARE_SLIPPAGE_PCT
    return FUTURES_SLIPPAGE_PCT


def _carry_fields(row: pd.Series, outcome: dict[str, Any]) -> None:
    exact = {
        "confidence", "rank_score", "fused_score", "share_score", "futures_score",
        "ev_pct", "kelly_pct", "trade_status", "is_actionable",
        "suggested_contracts", "suggested_dollars", "research_guard_status",
        "buyer_edge_pct", "pricing_direction", "dte", "spread_pct",
    }
    for column in row.index:
        if column in exact or str(column).startswith(("z_", "factor_")):
            outcome[column] = row.get(column)


def evaluate_fixed_horizons(
    signals: pd.DataFrame,
    histories: dict[str, pd.DataFrame],
    *,
    horizons: Iterable[int] = DEFAULT_HORIZONS,
    asof: datetime | None = None,
    existing: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict[str, int], dict[str, int]]:
    """Evaluate matured signal/horizon pairs from pre-fetched histories."""
    now = asof or datetime.now(UTC)
    prepared = (
        signals.copy()
        if signals is not None and {"signal_id", "is_independent"}.issubset(signals.columns)
        else prepare_signals(signals)
    )
    horizon_values = tuple(sorted({int(value) for value in horizons if int(value) > 0}))
    old = existing.copy() if existing is not None and not existing.empty else pd.DataFrame()
    if not old.empty:
        old = old[pd.to_numeric(old.get("methodology_version"), errors="coerce") == METHODOLOGY_VERSION]
    known = set(old.get("outcome_id", pd.Series(dtype=str)).dropna().astype(str))
    pending = {str(horizon): 0 for horizon in horizon_values}
    exclusions: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    spy = histories.get("SPY", pd.DataFrame())
    qqq = histories.get("QQQ", pd.DataFrame())

    for _, row in prepared.iterrows():
        asset = str(row["asset"])
        symbol = str(row["symbol"])
        entry_date = pd.Timestamp(row["entry_time"]).date()
        primary = histories.get(symbol, pd.DataFrame())
        source_symbol = symbol
        proxy_used = False
        if primary is None or primary.empty:
            proxy = _text(row.get("etf")).upper()
            if asset == "futures" and proxy:
                primary = histories.get(proxy, pd.DataFrame())
                source_symbol = proxy
                proxy_used = primary is not None and not primary.empty
        if primary is None or primary.empty:
            exclusions["missing_history"] = exclusions.get("missing_history", 0) + len(horizon_values)
            continue

        for horizon in horizon_values:
            outcome_id = f"{row['signal_id']}:{horizon}"
            if outcome_id in known:
                continue
            target_bar = _target_bar(primary, entry_date, horizon)
            if target_bar is None:
                pending[str(horizon)] += 1
                continue
            target_date = target_bar["_session_date"]
            target_underlying = _float(target_bar.get("Close"))
            if asset == "option":
                target_price, pnl, method = _option_outcome(row, target_underlying, target_date)
            elif asset == "futures" and proxy_used:
                target_price, pnl, method = _proxy_futures_outcome(
                    row, primary, entry_date, target_date,
                )
            else:
                target_price, pnl, method = _market_outcome(
                    row, asset, target_underlying,
                )
            if target_price is None or pnl is None:
                resolution = {
                    "methodology_version": METHODOLOGY_VERSION,
                    "outcome_id": outcome_id,
                    "signal_id": row["signal_id"],
                    "independent_key": row["independent_key"],
                    "is_independent": bool(row["is_independent"]),
                    "eligible_for_executable_metrics": bool(
                        row["eligible_for_executable_metrics"]
                    ),
                    "execution_eligibility_reason": row["execution_eligibility_reason"],
                    "eligible_for_shadow_metrics": bool(row["eligible_for_shadow_metrics"]),
                    "shadow_eligibility_reason": row["shadow_eligibility_reason"],
                    "asset": asset,
                    "symbol": symbol,
                    "ticker": symbol,
                    "direction": row["direction"],
                    "contract": row["contract_key"],
                    "entry_time": pd.Timestamp(row["entry_time"]).isoformat(),
                    "entry_date": entry_date.isoformat(),
                    "target_date": target_date.isoformat(),
                    "horizon_sessions": horizon,
                    "is_scored": False,
                    "resolution_status": "excluded",
                    "resolution_reason": method,
                    "generated_at": now.isoformat(),
                }
                _carry_fields(row, resolution)
                rows.append(resolution)
                known.add(outcome_id)
                continue

            entry_underlying = _first_number(
                row.get("spot"), row.get("entry"), row.get("entry_price"),
            ) or 0.0
            underlying_return = (
                target_underlying / entry_underlying - 1.0
                if entry_underlying > 0 else None
            )
            directional_underlying = underlying_return
            if underlying_return is not None and _direction(row, asset) in {"short", "long_put"}:
                directional_underlying = -underlying_return
            spy_return = _benchmark_return(spy, entry_date, target_date)
            qqq_return = _benchmark_return(qqq, entry_date, target_date)
            slippage = _slippage(asset)
            contracts = max(0, int(
                _first_number(row.get("suggested_contracts"), row.get("n_contracts")) or 0
            ))
            point_value = _float(row.get("point_value"))
            pnl_points = None
            pnl_dollars = None
            if asset == "futures":
                entry = _entry_price(row, asset)
                signed_points = target_price - entry
                if _direction(row, asset) == "short":
                    signed_points = -signed_points
                pnl_points = signed_points
                if point_value > 0 and contracts > 0:
                    pnl_dollars = signed_points * point_value * contracts

            outcome = {
                "methodology_version": METHODOLOGY_VERSION,
                "outcome_id": outcome_id,
                "signal_id": row["signal_id"],
                "independent_key": row["independent_key"],
                "is_independent": bool(row["is_independent"]),
                "eligible_for_executable_metrics": bool(row["eligible_for_executable_metrics"]),
                "execution_eligibility_reason": row["execution_eligibility_reason"],
                "eligible_for_shadow_metrics": bool(row["eligible_for_shadow_metrics"]),
                "shadow_eligibility_reason": row["shadow_eligibility_reason"],
                "asset": asset,
                "symbol": symbol,
                "ticker": symbol,
                "direction": row["direction"],
                "contract": row["contract_key"],
                "entry_time": pd.Timestamp(row["entry_time"]).isoformat(),
                "entry_date": entry_date.isoformat(),
                "target_date": target_date.isoformat(),
                "horizon_sessions": horizon,
                "entry_price": _entry_price(row, asset),
                "target_price": target_price,
                "pnl_pct": pnl,
                "slippage_assumption_pct": slippage,
                "pnl_pct_after_slippage": pnl - slippage,
                "underlying_return_pct": underlying_return,
                "directional_underlying_return_pct": directional_underlying,
                "spy_return_pct": spy_return,
                "qqq_return_pct": qqq_return,
                "excess_vs_spy_pct": pnl - slippage - spy_return if spy_return is not None else None,
                "excess_vs_qqq_pct": pnl - slippage - qqq_return if qqq_return is not None else None,
                "valuation_method": method,
                "outcome_quality": (
                    "modeled_option_proxy" if asset == "option"
                    else "market_proxy" if proxy_used else "market_observed"
                ),
                "history_symbol": source_symbol,
                "history_source": primary.attrs.get("history_source", "unknown"),
                "history_quality": primary.attrs.get("history_quality", "unknown"),
                "pnl_points": pnl_points,
                "pnl_dollars": pnl_dollars,
                "is_scored": True,
                "resolution_status": "scored",
                "resolution_reason": "",
                "generated_at": now.isoformat(),
            }
            _carry_fields(row, outcome)
            rows.append(outcome)
            known.add(outcome_id)

    new = pd.DataFrame(rows)
    frames = [frame for frame in (old, new) if frame is not None and not frame.empty]
    combined = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()
    if not combined.empty:
        combined = combined.drop_duplicates("outcome_id", keep="last")
        combined = combined.sort_values(
            ["target_date", "horizon_sessions", "asset", "symbol", "signal_id"]
        ).reset_index(drop=True)
    return combined, pending, exclusions


def _profit_factor(returns: pd.Series) -> float | None:
    values = pd.to_numeric(returns, errors="coerce").dropna()
    if values.empty:
        return None
    gross_profit = float(values[values > 0].sum())
    gross_loss = abs(float(values[values < 0].sum()))
    if gross_loss <= 0:
        return None if gross_profit <= 0 else float("inf")
    return gross_profit / gross_loss


def _wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    p = wins / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    margin = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * n)) / n) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def _max_drawdown(returns: pd.Series) -> float | None:
    values = pd.to_numeric(returns, errors="coerce").dropna()
    if values.empty:
        return None
    account_returns = values.clip(-1.0, 5.0) * DEFAULT_SIGNAL_ALLOCATION_PCT
    equity = pd.concat([pd.Series([1.0]), (1.0 + account_returns).cumprod()], ignore_index=True)
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def _stats(frame: pd.DataFrame) -> dict[str, Any]:
    if frame is None or frame.empty or "pnl_pct_after_slippage" not in frame.columns:
        return {
            "n": 0, "unique_entry_days": 0, "win_rate": None,
            "win_rate_ci_low": None, "win_rate_ci_high": None,
            "avg_return": None, "median_return": None, "profit_factor": None,
            "max_drawdown": None, "avg_excess_vs_spy": None,
            "avg_excess_vs_qqq": None,
        }
    values = pd.to_numeric(frame["pnl_pct_after_slippage"], errors="coerce")
    valid = frame.loc[values.notna()].copy()
    values = values.dropna()
    if values.empty:
        return _stats(pd.DataFrame())
    wins = int((values > 0).sum())
    ci_low, ci_high = _wilson_interval(wins, len(values))

    def mean_column(column: str) -> float | None:
        if column not in valid.columns:
            return None
        data = pd.to_numeric(valid[column], errors="coerce").dropna()
        return float(data.mean()) if not data.empty else None

    entry_days = pd.to_datetime(valid.get("entry_time"), errors="coerce", utc=True)
    return {
        "n": int(len(values)),
        "unique_entry_days": int(entry_days.dt.date.nunique()),
        "win_rate": float(wins / len(values)),
        "win_rate_ci_low": ci_low,
        "win_rate_ci_high": ci_high,
        "avg_return": float(values.mean()),
        "median_return": float(values.median()),
        "profit_factor": _profit_factor(values),
        "max_drawdown": _max_drawdown(values),
        "avg_excess_vs_spy": mean_column("excess_vs_spy_pct"),
        "avg_excess_vs_qqq": mean_column("excess_vs_qqq_pct"),
        "best": float(values.max()),
        "worst": float(values.min()),
    }


def _factor_ic(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    target = "pnl_pct_after_slippage"
    factors = [column for column in frame.columns if str(column).startswith(("z_", "factor_"))]
    rows: list[dict[str, Any]] = []
    for horizon in sorted(pd.to_numeric(frame["horizon_sessions"], errors="coerce").dropna().unique()):
        horizon_frame = frame[pd.to_numeric(frame["horizon_sessions"], errors="coerce") == horizon]
        for factor in factors:
            sample = horizon_frame[[factor, target, "entry_time"]].copy()
            sample[factor] = pd.to_numeric(sample[factor], errors="coerce")
            sample[target] = pd.to_numeric(sample[target], errors="coerce")
            sample = sample.dropna(subset=[factor, target])
            if len(sample) < 5 or sample[factor].nunique() < 2:
                continue
            ic = sample[factor].corr(sample[target])
            if pd.isna(ic):
                continue
            days = int(pd.to_datetime(sample["entry_time"], utc=True).dt.date.nunique())
            reliable = len(sample) >= MIN_RELIABLE_SAMPLES and days >= MIN_RELIABLE_DAYS
            rows.append({
                "factor": factor,
                "horizon_sessions": int(horizon),
                "n": int(len(sample)),
                "unique_entry_days": days,
                "ic": float(ic),
                "is_reliable": reliable,
            })
    return sorted(rows, key=lambda row: (row["horizon_sessions"], -abs(row["ic"])))


def build_summary(
    outcomes: pd.DataFrame,
    signals: pd.DataFrame,
    pending: dict[str, int],
    exclusions: dict[str, int],
    *,
    horizons: Iterable[int] = DEFAULT_HORIZONS,
    asof: datetime | None = None,
) -> dict[str, Any]:
    now = asof or datetime.now(UTC)
    prepared = prepare_signals(signals)
    if outcomes is not None and not outcomes.empty:
        scored_mask = outcomes.get(
            "is_scored", pd.Series(True, index=outcomes.index),
        ).fillna(False).astype(bool)
        scored = outcomes[scored_mask].copy()
    else:
        scored = pd.DataFrame()
    independent = scored[
        scored.get("is_independent", pd.Series(False, index=scored.index)).fillna(False).astype(bool)
    ].copy() if not scored.empty else pd.DataFrame()
    executable = independent[
        independent.get(
            "eligible_for_executable_metrics", pd.Series(False, index=independent.index),
        ).fillna(False).astype(bool)
    ].copy() if not independent.empty else pd.DataFrame()
    shadow = independent[
        independent.get(
            "eligible_for_shadow_metrics", pd.Series(False, index=independent.index),
        ).fillna(False).astype(bool)
    ].copy() if not independent.empty else pd.DataFrame()

    by_horizon = []
    by_asset_horizon = []
    for horizon in sorted({int(value) for value in horizons}):
        all_h = independent[
            pd.to_numeric(independent.get("horizon_sessions"), errors="coerce") == horizon
        ] if not independent.empty else pd.DataFrame()
        exe_h = executable[
            pd.to_numeric(executable.get("horizon_sessions"), errors="coerce") == horizon
        ] if not executable.empty else pd.DataFrame()
        shadow_h = shadow[
            pd.to_numeric(shadow.get("horizon_sessions"), errors="coerce") == horizon
        ] if not shadow.empty else pd.DataFrame()
        by_horizon.append({
            "horizon_sessions": horizon,
            "all_recommendations": _stats(all_h),
            "executable": _stats(exe_h),
            "shadow_current_method": _stats(shadow_h),
        })
        for asset in ("option", "share", "futures"):
            asset_all = all_h[all_h.get("asset", "") == asset] if not all_h.empty else pd.DataFrame()
            asset_exe = exe_h[exe_h.get("asset", "") == asset] if not exe_h.empty else pd.DataFrame()
            asset_shadow = (
                shadow_h[shadow_h.get("asset", "") == asset]
                if not shadow_h.empty else pd.DataFrame()
            )
            by_asset_horizon.append({
                "asset": asset,
                "horizon_sessions": horizon,
                "all_recommendations": _stats(asset_all),
                "executable": _stats(asset_exe),
                "shadow_current_method": _stats(asset_shadow),
            })

    headline_rows = [
        row for row in by_horizon if row["horizon_sessions"] == HEADLINE_HORIZON
    ]
    headline = headline_rows[0]["executable"] if headline_rows else _stats(pd.DataFrame())
    headline_shadow = (
        headline_rows[0]["shadow_current_method"] if headline_rows else _stats(pd.DataFrame())
    )
    headline_all = headline_rows[0]["all_recommendations"] if headline_rows else _stats(pd.DataFrame())
    quality = (
        scored.get("outcome_quality", pd.Series(dtype=str)).fillna("unknown").astype(str).value_counts().to_dict()
        if not scored.empty else {}
    )
    persisted_exclusions = (
        outcomes.loc[~outcomes.get(
            "is_scored", pd.Series(True, index=outcomes.index),
        ).fillna(False).astype(bool), "resolution_reason"]
        .fillna("unknown").astype(str).value_counts().to_dict()
        if outcomes is not None and not outcomes.empty and "resolution_reason" in outcomes.columns
        else {}
    )
    combined_exclusions = dict(persisted_exclusions)
    for reason, count in exclusions.items():
        combined_exclusions[reason] = combined_exclusions.get(reason, 0) + int(count)
    warnings = []
    if headline["n"] < MIN_RELIABLE_SAMPLES:
        if headline["n"] == 0:
            warnings.append(
                "No independent current-method executable outcomes have matured yet. "
                "Legacy options without directional buyer-edge evidence stay telemetry-only."
            )
        else:
            warnings.append(
                f"Only {headline['n']} independent executable {HEADLINE_HORIZON}-session outcomes; "
                f"need at least {MIN_RELIABLE_SAMPLES}."
            )
    if headline_shadow["n"] < MIN_RELIABLE_SAMPLES:
        warnings.append(
            f"Current-method shadow sample has {headline_shadow['n']} independent "
            f"{HEADLINE_HORIZON}-session outcomes; need at least {MIN_RELIABLE_SAMPLES}."
        )
    if headline_shadow["unique_entry_days"] < MIN_RELIABLE_DAYS:
        warnings.append(
            f"Current-method shadow outcomes span {headline_shadow['unique_entry_days']} entry days; "
            f"need at least {MIN_RELIABLE_DAYS}."
        )
    if quality.get("modeled_option_proxy", 0):
        warnings.append(
            "Option fixed-horizon outcomes are constant-entry-IV model proxies, not historical option fills."
        )

    return {
        "generated_at": now.isoformat(),
        "methodology_version": METHODOLOGY_VERSION,
        "basis": "independent_fixed_session_outcomes_after_slippage",
        "headline_horizon_sessions": HEADLINE_HORIZON,
        "raw_logged_signals": int(len(prepared)),
        "independent_logged_signals": int(prepared.get("is_independent", pd.Series(dtype=bool)).sum()),
        "resolved_outcome_pairs": int(len(outcomes) if outcomes is not None else 0),
        "matured_outcome_pairs": int(len(scored)),
        "independent_matured_outcome_pairs": int(len(independent)),
        "executable_matured_outcome_pairs": int(len(executable)),
        "shadow_matured_outcome_pairs": int(len(shadow)),
        "matured_signals": int(scored["signal_id"].nunique()) if not scored.empty else 0,
        "pending_by_horizon": pending,
        "exclusions_by_reason": combined_exclusions,
        "outcome_quality": quality,
        "slippage_assumptions": {
            "option": _option_slippage_pct(),
            "share": SHARE_SLIPPAGE_PCT,
            "futures": FUTURES_SLIPPAGE_PCT,
        },
        "headline": headline,
        "headline_shadow": headline_shadow,
        "headline_all_recommendations": headline_all,
        "by_horizon": by_horizon,
        "by_asset_horizon": by_asset_horizon,
        "factor_ic": _factor_ic(shadow),
        "warnings": warnings,
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        value = float(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    return value


def _load_existing(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        frame = pd.read_parquet(path)
        if "methodology_version" not in frame.columns:
            return pd.DataFrame()
        version = pd.to_numeric(frame["methodology_version"], errors="coerce")
        return frame[version == METHODOLOGY_VERSION].copy()
    except Exception as exc:
        log.warning("fixed-horizon outcomes unreadable; rebuilding: %s", exc)
        return pd.DataFrame()


def _default_history_loader(symbol: str, period: str) -> pd.DataFrame:
    return data_provider.get_history(symbol, period=period, interval="1d")


def _fetch_histories(
    signals: pd.DataFrame,
    loader: HistoryLoader,
    *,
    asof: datetime,
    max_workers: int,
) -> dict[str, pd.DataFrame]:
    prepared = prepare_signals(signals)
    symbols = set(prepared.get("symbol", pd.Series(dtype=str)).dropna().astype(str))
    if not prepared.empty and "etf" in prepared.columns:
        symbols.update(prepared["etf"].dropna().astype(str).str.upper())
    symbols.update({"SPY", "QQQ"})
    symbols.discard("")
    histories: dict[str, pd.DataFrame] = {}

    def fetch(symbol: str) -> tuple[str, pd.DataFrame]:
        try:
            history = loader(symbol, "1y")
        except TypeError:
            history = loader(symbol)  # type: ignore[misc,call-arg]
        return symbol, _normalize_history(history, asof)

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        future_map = {executor.submit(fetch, symbol): symbol for symbol in sorted(symbols)}
        for future in as_completed(future_map):
            symbol = future_map[future]
            try:
                key, history = future.result()
                histories[key] = history
            except Exception as exc:
                log.debug("fixed-horizon history failed for %s: %s", symbol, exc)
                histories[symbol] = pd.DataFrame()
    return histories


def _unresolved_candidates(
    prepared: pd.DataFrame,
    existing: pd.DataFrame,
    horizons: Iterable[int],
    asof: datetime,
) -> tuple[pd.DataFrame, dict[str, int]]:
    horizon_values = tuple(sorted({int(value) for value in horizons if int(value) > 0}))
    known = set(
        existing.get("outcome_id", pd.Series(dtype=str)).dropna().astype(str)
        if existing is not None and not existing.empty else []
    )
    pending = {str(horizon): 0 for horizon in horizon_values}
    candidate_indexes = set()
    for index, row in prepared.iterrows():
        entry_date = pd.Timestamp(row["entry_time"]).date()
        try:
            completed_weekdays = int(np.busday_count(entry_date, asof.date()))
        except Exception:
            completed_weekdays = 0
        for horizon in horizon_values:
            outcome_id = f"{row['signal_id']}:{horizon}"
            if outcome_id in known:
                continue
            pending[str(horizon)] += 1
            if completed_weekdays >= horizon:
                candidate_indexes.add(index)
    if not candidate_indexes:
        return prepared.iloc[0:0].copy(), pending
    return prepared.loc[sorted(candidate_indexes)].copy(), pending


def run_fixed_horizon_test(
    signals: pd.DataFrame | None = None,
    *,
    horizons: Iterable[int] = DEFAULT_HORIZONS,
    asof: datetime | None = None,
    history_loader: HistoryLoader | None = None,
    outcomes_path: Path = OUTCOMES_PATH,
    summary_path: Path = SUMMARY_PATH,
    max_workers: int = 12,
    write: bool = True,
) -> dict[str, Any]:
    """Incrementally settle every newly matured fixed-session outcome."""
    now = asof or datetime.now(UTC)
    if signals is None:
        from backtest.forward import _load_all_logs

        signals = _load_all_logs()
    signals = signals if signals is not None else pd.DataFrame()
    existing = _load_existing(outcomes_path)
    if signals.empty:
        summary = build_summary(existing, signals, {}, {}, horizons=horizons, asof=now)
        if write:
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(json.dumps(_json_safe(summary), indent=2), encoding="utf-8")
        return {"outcomes": existing, "summary": summary, "new_outcomes": 0}

    prepared = prepare_signals(signals)
    candidates, pending = _unresolved_candidates(prepared, existing, horizons, now)
    before = len(existing)
    if candidates.empty:
        outcomes = existing
        exclusions = {}
    else:
        histories = _fetch_histories(
            candidates,
            history_loader or _default_history_loader,
            asof=now,
            max_workers=max_workers,
        )
        outcomes, _, exclusions = evaluate_fixed_horizons(
            candidates,
            histories,
            horizons=horizons,
            asof=now,
            existing=existing,
        )
        _, pending = _unresolved_candidates(prepared, outcomes, horizons, now)
    summary = build_summary(
        outcomes, signals, pending, exclusions, horizons=horizons, asof=now,
    )
    if write:
        outcomes_path.parent.mkdir(parents=True, exist_ok=True)
        if not outcomes.empty:
            outcomes.to_parquet(outcomes_path, index=False)
        summary_path.write_text(json.dumps(_json_safe(summary), indent=2), encoding="utf-8")
    new_count = max(0, len(outcomes) - before)
    log.info(
        "fixed horizon: %d new / %d total; headline %ds executed n=%d shadow n=%d",
        new_count,
        len(outcomes),
        HEADLINE_HORIZON,
        summary.get("headline", {}).get("n", 0),
        summary.get("headline_shadow", {}).get("n", 0),
    )
    return {"outcomes": outcomes, "summary": summary, "new_outcomes": new_count}


if __name__ == "__main__":
    result = run_fixed_horizon_test()
    print(json.dumps(_json_safe(result["summary"]), indent=2))
