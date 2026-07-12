"""Auditable option-expiry valuation for lifecycle cleanup.

Expired recommendations must leave the open book, but their outcome should not
be invented. This module resolves expiration value from the free underlying
history stack, retains an exact non-interpolated Robinhood option bar as
validation-excluded telemetry, and otherwise returns an explicitly unresolved
valuation.
"""
from __future__ import annotations

import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from backtest.option_history import (
    SNAPSHOT_PATH as DEFAULT_OPTION_HISTORY_PATH,
    contract_key_from_row,
    load_option_histories,
    observed_option_close,
)
from optedge.strategy_profile import is_known_index_option_symbol

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
MAX_EXPIRY_SESSION_GAP_DAYS = 7


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    """Return an ordinal weekday without relying on an optional calendar package."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + (occurrence - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    candidate = next_month - timedelta(days=1)
    return candidate - timedelta(days=(candidate.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    """Gregorian Easter, used to identify the Good Friday exchange closure."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = (h + ell - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _observed_fixed_holiday(day: date, *, observe_saturday: bool = True) -> date:
    if day.weekday() == 5 and observe_saturday:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _recognized_us_equity_holidays(year: int) -> set[date]:
    """Return regular full-day US equity exchange closures for ``year``.

    The deliberately finite calendar recognizes scheduled closures only. An
    unexpected data gap or extraordinary closure therefore cannot silently be
    promoted into a validation-quality settlement proxy.
    """
    holidays = {
        _observed_fixed_holiday(date(year, 1, 1), observe_saturday=False),
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),  # Memorial Day
        _observed_fixed_holiday(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed_fixed_holiday(date(year, 12, 25)),
    }
    if year >= 1998:
        holidays.add(_nth_weekday(year, 1, 0, 3))  # Martin Luther King Jr. Day
    holidays.add(_nth_weekday(year, 2, 0, 3))  # Washington's Birthday
    if year >= 2022:
        holidays.add(_observed_fixed_holiday(date(year, 6, 19)))

    return {holiday for holiday in holidays if holiday.year == year}


def _is_recognized_exchange_session(day: date) -> bool:
    return day.weekday() < 5 and day not in _recognized_us_equity_holidays(day.year)


def _previous_recognized_exchange_session(day: date) -> date:
    candidate = day - timedelta(days=1)
    while not _is_recognized_exchange_session(candidate):
        candidate -= timedelta(days=1)
    return candidate


def _underlying_session_provenance(target: date, observed: date) -> str:
    if observed == target and _is_recognized_exchange_session(target):
        return "expiry_exchange_session"
    if (
        not _is_recognized_exchange_session(target)
        and observed == _previous_recognized_exchange_session(target)
    ):
        return "recognized_prior_exchange_session"
    return "stale_gap_before_expiry"


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _first_present(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key not in row:
            continue
        value = row.get(key)
        if _text(value):
            return value
    return None


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    return bool(value)


def _underlying_type(position: dict[str, Any]) -> str:
    symbol = _text(position.get("ticker") or position.get("symbol")).upper()
    if is_known_index_option_symbol(symbol):
        return "index"
    raw = _text(_first_present(
        position, "underlying_type", "underlying_asset_type", "asset_class"
    )).lower()
    if raw in {"equity", "stock", "common_stock"}:
        return "equity"
    if raw in {"etf", "fund", "exchange_traded_fund"}:
        return "etf"
    if raw in {"index", "market_index", "cash_index"}:
        return "index"
    return "unknown"


def _settlement_style(position: dict[str, Any]) -> str:
    return _text(_first_present(
        position,
        "official_settlement_style",
        "settlement_style",
        "exercise_settlement_style",
    )).lower()


def _is_am_settled(style: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(style or "").lower()).strip("_")
    return normalized == "am" or normalized.startswith("am_") or normalized.endswith("_am")


def _contract_provenance(position: dict[str, Any], underlying_type: str) -> dict[str, Any]:
    multiplier = _number(_first_present(
        position, "contract_multiplier", "trade_value_multiplier", "multiplier"
    ))
    raw_deliverable = _first_present(
        position, "deliverable", "deliverable_description", "contract_deliverable"
    )
    deliverable_type = _text(position.get("deliverable_type")).lower()
    deliverable_units = _number(position.get("deliverable_units"))
    if isinstance(raw_deliverable, dict):
        deliverable_type = _text(
            raw_deliverable.get("type") or raw_deliverable.get("asset_type")
        ).lower() or deliverable_type
        deliverable_units = _number(
            raw_deliverable.get("units") or raw_deliverable.get("quantity")
        ) or deliverable_units
        deliverable = _text(
            raw_deliverable.get("description") or raw_deliverable.get("label")
        )
    else:
        deliverable = _text(raw_deliverable)
    normalized_deliverable = re.sub(
        r"[^a-z0-9]+", "_", deliverable.lower()
    ).strip("_")
    adjusted = any(
        _truthy(position.get(key))
        for key in (
            "is_adjusted_contract",
            "adjusted_contract",
            "non_standard_deliverable",
            "corporate_action_ambiguous",
        )
    ) or any(
        token in normalized_deliverable
        for token in ("adjusted", "non_standard", "corporate_action", "special_deliverable")
    )
    if underlying_type == "index" or "cash" in _settlement_style(position):
        deliverable_ok = (
            "cash" in normalized_deliverable or deliverable_type in {"cash", "cash_settlement"}
        )
    else:
        deliverable_ok = (
            ("100" in normalized_deliverable and "share" in normalized_deliverable)
            or (
                deliverable_units == 100
                and deliverable_type in {"share", "shares", "equity", "stock"}
            )
        )
    reasons: list[str] = []
    if multiplier != 100:
        reasons.append("contract_multiplier_is_not_verified_standard_100x")
    if not deliverable_ok:
        reasons.append("contract_deliverable_is_not_verified_standard")
    if adjusted:
        reasons.append("adjusted_or_corporate_action_contract_is_ambiguous")
    return {
        "contract_multiplier": multiplier,
        "deliverable": deliverable or None,
        "deliverable_type": deliverable_type or None,
        "deliverable_units": deliverable_units,
        "deliverable_is_standard": not reasons,
        "corporate_action_ambiguous": adjusted,
        "contract_exclusion_reasons": reasons,
    }


def _history_price_basis(frame: pd.DataFrame) -> str:
    for key in ("close_price_basis", "price_basis", "history_price_basis"):
        value = _text(frame.attrs.get(key)).lower()
        if value:
            return value
    return "unknown"


def _raw_close_basis_verified(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    return normalized in {
        "raw_close",
        "unadjusted_close",
        "unadjusted_raw_close",
        "official_unadjusted_close",
    }


def _official_settlement(position: dict[str, Any]) -> dict[str, Any]:
    value = _number(_first_present(
        position,
        "official_underlying_settlement_value",
        "official_settlement_value",
        "settlement_value",
    ))
    style = _settlement_style(position)
    source = _text(position.get("official_settlement_source"))
    source_id = _text(_first_present(
        position, "official_settlement_source_id", "official_settlement_record_id"
    ))
    published_at = _text(position.get("official_settlement_published_at"))
    explicit_verified = position.get("official_settlement_verified")
    captured = bool(
        value is not None
        and value > 0
        and style
        and source
        and explicit_verified is True
    )
    return {
        "official_settlement_value": value,
        "official_settlement_style": style or None,
        "official_settlement_source": source or None,
        "official_settlement_source_id": source_id or None,
        "official_settlement_published_at": published_at or None,
        "official_settlement_verified": explicit_verified is True,
        "official_settlement_captured": captured,
    }


def expiry_date(position: dict[str, Any]) -> date | None:
    parsed = pd.to_datetime(
        position.get("expiry") or position.get("expiration_date"),
        errors="coerce",
        utc=True,
    )
    return None if pd.isna(parsed) else parsed.date()


def expiry_exit_time(position: dict[str, Any]) -> datetime | None:
    """Return the regular-session expiration close in UTC.

    Date-only equity/ETF expirations are represented as 4:00 PM New York time;
    explicitly AM-settled contracts use 9:30 AM. Explicit timestamps are preserved.
    """
    raw = _text(position.get("expiry") or position.get("expiration_date"))
    if not raw:
        return None
    parsed = pd.to_datetime(raw, errors="coerce", utc=True)
    if pd.isna(parsed):
        return None
    if "T" in raw:
        return parsed.to_pydatetime()
    local_time = time(9, 30) if _is_am_settled(_settlement_style(position)) else time(16, 0)
    local_close = datetime.combine(
        parsed.date(), local_time, tzinfo=ZoneInfo("America/New_York")
    )
    return local_close.astimezone(UTC)


def valuation_key(position: dict[str, Any]) -> str:
    return contract_key_from_row(position)


def _valuation_provenance(
    position: dict[str, Any], frame: pd.DataFrame | None = None
) -> dict[str, Any]:
    underlying_type = _underlying_type(position)
    contract = _contract_provenance(position, underlying_type)
    official = _official_settlement(position)
    return {
        "underlying_type": underlying_type,
        "settlement_style": _settlement_style(position) or None,
        "underlying_price_basis": (
            _history_price_basis(frame) if isinstance(frame, pd.DataFrame) else None
        ),
        **contract,
        **official,
    }


def unresolved_valuation(position: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract_key": valuation_key(position),
        "option_value": None,
        "price_source": "unresolved_no_expiry_market_data",
        "valuation_date": (
            expiry_date(position).isoformat() if expiry_date(position) else None
        ),
        "underlying_price": None,
        "underlying_price_date": None,
        "underlying_session_gap_days": None,
        "underlying_session_provenance": None,
        "underlying_history_source": None,
        "underlying_history_quality": None,
        "option_bar_date": None,
        "option_instrument_id": None,
        "settlement_is_proxy": True,
        "validation_eligible": False,
        "validation_exclusion_reason": "missing_expiration_value",
        **_valuation_provenance(position),
    }


def _history_period(expiries: list[date], asof: datetime) -> str:
    oldest = min(expiries) if expiries else asof.date()
    age_days = max(0, (asof.date() - oldest).days)
    if age_days <= 360:
        return "1y"
    if age_days <= 1_800:
        return "5y"
    return "max"


def _call_history_fetcher(
    fetcher: Callable[..., pd.DataFrame], ticker: str, period: str
) -> pd.DataFrame:
    try:
        frame = fetcher(ticker, period=period, interval="1d", cache_age=3600)
    except TypeError:
        frame = fetcher(ticker, period=period, interval="1d")
    except Exception:
        return pd.DataFrame()
    return frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()


def _history_close(
    frame: pd.DataFrame, target: date
) -> dict[str, Any] | None:
    if frame is None or frame.empty:
        return None
    close_col = next(
        (column for column in frame.columns if str(column).strip().lower() == "close"),
        None,
    )
    if close_col is None:
        return None
    timestamps = pd.to_datetime(frame.index, errors="coerce", utc=True)
    closes = pd.to_numeric(frame[close_col], errors="coerce")
    work = pd.DataFrame({"timestamp": timestamps, "close": closes}).dropna()
    work = work[(work["close"] > 0) & (work["timestamp"].dt.date <= target)]
    if work.empty:
        return None
    row = work.sort_values("timestamp").iloc[-1]
    session_date = row["timestamp"].date()
    gap_days = (target - session_date).days
    if gap_days < 0 or gap_days > MAX_EXPIRY_SESSION_GAP_DAYS:
        return None
    return {
        "price": float(row["close"]),
        "date": session_date,
        "gap_days": gap_days,
        "session_provenance": _underlying_session_provenance(target, session_date),
    }


def _intrinsic_value(position: dict[str, Any], spot: float) -> float | None:
    strike = _number(position.get("strike") or position.get("strike_price"))
    side = _text(position.get("side") or position.get("option_side")).lower()
    if strike is None or strike <= 0 or spot <= 0:
        return None
    if side.startswith("c"):
        return max(0.0, spot - strike)
    if side.startswith("p"):
        return max(0.0, strike - spot)
    return None


def resolve_expiry_valuations(
    positions: list[dict[str, Any]],
    *,
    asof: datetime | None = None,
    history_fetcher: Callable[..., pd.DataFrame] | None = None,
    option_history_path: Path | None = None,
    max_workers: int = 8,
) -> dict[str, dict[str, Any]]:
    """Resolve one expiration valuation per exact option contract."""
    now = asof or datetime.now(UTC)
    normalized: dict[str, dict[str, Any]] = {}
    expiries_by_ticker: dict[str, list[date]] = {}
    for raw in positions:
        if not isinstance(raw, dict):
            continue
        key = valuation_key(raw)
        exp = expiry_date(raw)
        ticker = _text(raw.get("ticker") or raw.get("symbol")).upper()
        if not key or exp is None or not ticker:
            continue
        normalized[key] = raw
        expiries_by_ticker.setdefault(ticker, []).append(exp)
    if not normalized:
        return {}

    if history_fetcher is None:
        from data_provider import get_history

        history_fetcher = get_history

    history_frames: dict[str, pd.DataFrame] = {}
    worker_count = max(1, min(int(max_workers or 1), len(expiries_by_ticker), 12))
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {
            pool.submit(
                _call_history_fetcher,
                history_fetcher,
                ticker,
                _history_period(expiries, now),
            ): ticker
            for ticker, expiries in expiries_by_ticker.items()
        }
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                history_frames[ticker] = future.result()
            except Exception:
                history_frames[ticker] = pd.DataFrame()

    history_path = option_history_path or DEFAULT_OPTION_HISTORY_PATH
    option_histories = load_option_histories(history_path)
    results: dict[str, dict[str, Any]] = {}
    for key, position in normalized.items():
        ticker = _text(position.get("ticker") or position.get("symbol")).upper()
        exp = expiry_date(position)
        frame = history_frames.get(ticker, pd.DataFrame())
        provenance = _valuation_provenance(position, frame)
        official_value = provenance.get("official_settlement_value")
        if official_value is not None:
            intrinsic = _intrinsic_value(position, float(official_value))
            if intrinsic is not None:
                official_reasons: list[str] = []
                if not provenance.get("official_settlement_captured"):
                    official_reasons.append(
                        "official_settlement_value_style_source_and_verification_are_not_fully_captured"
                    )
                official_reasons.extend(provenance.get("contract_exclusion_reasons") or [])
                official_eligible = not official_reasons
                results[key] = {
                    "contract_key": key,
                    "option_value": intrinsic,
                    "price_source": "intrinsic_from_official_settlement_value",
                    "valuation_date": exp.isoformat(),
                    "underlying_price": float(official_value),
                    "underlying_price_date": exp.isoformat(),
                    "underlying_session_gap_days": None,
                    "underlying_session_provenance": "official_settlement_value",
                    "underlying_history_source": None,
                    "underlying_history_quality": None,
                    "option_bar_date": None,
                    "option_instrument_id": None,
                    "settlement_is_proxy": False,
                    "validation_eligible": official_eligible,
                    "validation_exclusion_reason": (
                        None if official_eligible else ";".join(official_reasons)
                    ),
                    **provenance,
                }
                continue
        close = _history_close(frame, exp) if exp is not None else None
        if close is not None:
            intrinsic = _intrinsic_value(position, close["price"])
            if intrinsic is not None:
                session_provenance = close["session_provenance"]
                exclusion_reasons: list[str] = []
                if session_provenance not in {
                    "expiry_exchange_session",
                    "recognized_prior_exchange_session",
                }:
                    exclusion_reasons.append(
                        "underlying_close_not_expiry_or_recognized_prior_session"
                    )
                underlying_type = provenance.get("underlying_type")
                settlement_style = str(provenance.get("settlement_style") or "")
                if underlying_type not in {"equity", "etf"}:
                    exclusion_reasons.append(
                        "official_settlement_value_required_for_index_or_unknown_underlying"
                    )
                if _is_am_settled(settlement_style):
                    exclusion_reasons.append(
                        "am_settled_contract_requires_official_settlement_value"
                    )
                exclusion_reasons.extend(provenance.get("contract_exclusion_reasons") or [])
                if not _raw_close_basis_verified(
                    str(provenance.get("underlying_price_basis") or "")
                ):
                    exclusion_reasons.append(
                        "underlying_history_close_is_not_verified_raw_unadjusted"
                    )
                validation_eligible = not exclusion_reasons
                results[key] = {
                    "contract_key": key,
                    "option_value": intrinsic,
                    "price_source": (
                        "intrinsic_proxy_from_underlying_expiry_close"
                        if session_provenance in {
                            "expiry_exchange_session",
                            "recognized_prior_exchange_session",
                        }
                        else "intrinsic_proxy_from_stale_underlying_close"
                    ),
                    "valuation_date": exp.isoformat(),
                    "underlying_price": close["price"],
                    "underlying_price_date": close["date"].isoformat(),
                    "underlying_session_gap_days": close["gap_days"],
                    "underlying_session_provenance": session_provenance,
                    "underlying_history_source": frame.attrs.get("history_source", "unknown"),
                    "underlying_history_quality": frame.attrs.get("history_quality", "unknown"),
                    "option_bar_date": None,
                    "option_instrument_id": None,
                    "settlement_is_proxy": True,
                    "validation_eligible": validation_eligible,
                    "validation_exclusion_reason": (
                        None if validation_eligible else ";".join(exclusion_reasons)
                    ),
                    **provenance,
                }
                continue

        observed = observed_option_close(option_histories.get(key), exp) if exp else None
        if observed is not None:
            price, metadata = observed
            results[key] = {
                "contract_key": key,
                "option_value": price,
                "price_source": "broker_option_trade_bar_on_expiry",
                "valuation_date": exp.isoformat(),
                "underlying_price": None,
                "underlying_price_date": None,
                "underlying_session_gap_days": None,
                "underlying_session_provenance": None,
                "underlying_history_source": None,
                "underlying_history_quality": None,
                "option_bar_date": metadata.get("option_bar_date"),
                "option_instrument_id": metadata.get("option_instrument_id"),
                "occ_symbol": metadata.get("occ_symbol"),
                "settlement_is_proxy": True,
                "validation_eligible": False,
                "validation_exclusion_reason": "option_trade_bar_is_not_expiration_settlement",
                **provenance,
            }
            continue
        results[key] = unresolved_valuation(position)
    return results


def valuation_for_position(
    position: dict[str, Any], valuations: dict[str, dict[str, Any]] | None
) -> dict[str, Any]:
    key = valuation_key(position)
    if key and isinstance(valuations, dict) and isinstance(valuations.get(key), dict):
        return valuations[key]
    return unresolved_valuation(position)
