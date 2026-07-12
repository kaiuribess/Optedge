# Purpose: Learn guarded exits from completed position evidence.
"""Conservative self-learning exit policy."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
POLICY_FILE = DATA_DIR / "exit_policy.json"
POLICY_HISTORY_FILE = DATA_DIR / "exit_policy_history.jsonl"
EXIT_REVIEWS_FILE = DATA_DIR / "exit_reviews.jsonl"
MIN_LEARNING_CLOSED_POSITIONS = 100
MIN_LEARNING_EXIT_REVIEWS = 20
MIN_LEARNING_TRADING_DAYS = 10
MIN_LEARNING_HOLD_HOURS = 1.0
MAX_POLICY_AGE_DAYS = 14
NON_ACTIONABLE_ENTRY_STATUSES = {"watch", "skip", "blocked", "reject", "rejected"}

DEFAULT_POLICY = {
    "policy_version": "default",
    "generated_at": None,
    "assets": {
        "option": {
            "learned_active": False,
            "watch_pressure_threshold": 40,
            "tighten_pressure_threshold": 60,
            "close_pressure_threshold": 80,
            "breakeven_after_pnl_pct": 0.40,
            "trail_after_pnl_pct": 0.80,
        },
        "share": {
            "learned_active": False,
            "watch_pressure_threshold": 40,
            "tighten_pressure_threshold": 60,
            "close_pressure_threshold": 80,
            "breakeven_after_pnl_pct": 0.10,
            "trail_after_pnl_pct": 0.18,
        },
        "futures": {
            "learned_active": False,
            "watch_pressure_threshold": 40,
            "tighten_pressure_threshold": 60,
            "close_pressure_threshold": 80,
            "trail_after_atr": 1.0,
        },
    },
}


def _deepcopy_default() -> Dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_POLICY))


def _policy_is_stale(policy: Dict[str, Any]) -> bool:
    generated = pd.to_datetime(policy.get("generated_at"), errors="coerce", utc=True)
    if pd.isna(generated):
        return True
    age_days = (pd.Timestamp.now(tz="UTC") - generated).total_seconds() / 86400.0
    return age_days > MAX_POLICY_AGE_DAYS


def load_exit_policy() -> Dict[str, Any]:
    if not POLICY_FILE.exists():
        return _deepcopy_default()
    try:
        data = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "assets" not in data:
            return _deepcopy_default()
        if _policy_is_stale(data):
            return _deepcopy_default()
        default = _deepcopy_default()
        for asset, defaults in default["assets"].items():
            data.setdefault("assets", {}).setdefault(asset, defaults)
            for key, value in defaults.items():
                data["assets"][asset].setdefault(key, value)
        data.setdefault("policy_version", "unknown")
        return data
    except Exception:
        return _deepcopy_default()


def save_exit_policy(policy: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    POLICY_FILE.write_text(json.dumps(policy, indent=2, default=str), encoding="utf-8")


def _load_json_rows(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        return pd.DataFrame(rows if isinstance(rows, list) else [])
    except Exception:
        return pd.DataFrame()


def _load_jsonl(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except Exception:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _closed_positions() -> pd.DataFrame:
    frames = []
    for asset, path in [
        ("option", DATA_DIR / "closed_positions.json"),
        ("share", DATA_DIR / "closed_share_positions.json"),
        ("futures", DATA_DIR / "closed_futures_positions.json"),
    ]:
        df = _load_json_rows(path)
        if not df.empty:
            df["asset"] = asset
            frames.append(df)
    return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()


def _reviews() -> pd.DataFrame:
    return _load_jsonl(EXIT_REVIEWS_FILE)


def _asset_rows(frame: pd.DataFrame, asset: str) -> pd.DataFrame:
    if frame.empty or "asset" not in frame.columns:
        return pd.DataFrame()
    return frame[frame["asset"].astype(str) == asset].copy()


def _episode_id(row: pd.Series, asset: str) -> str:
    position_id = str(row.get("position_id") or "").strip()
    if position_id and position_id.lower() not in {"none", "nan"}:
        return position_id
    if asset == "option":
        keys = ("ticker", "side", "strike", "expiry", "entry_time")
    elif asset == "futures":
        keys = ("symbol", "direction", "contract", "entry_time")
    else:
        keys = ("ticker", "entry_time")
    return "|".join(str(row.get(key) or "") for key in keys)


def _holding_hours(closed: pd.DataFrame) -> pd.Series:
    missing = pd.Series(pd.NaT, index=closed.index, dtype="datetime64[ns, UTC]")
    entry = pd.to_datetime(closed["entry_time"], errors="coerce", utc=True) \
        if "entry_time" in closed.columns else missing
    exit_time = pd.to_datetime(closed["exit_time"], errors="coerce", utc=True) \
        if "exit_time" in closed.columns else missing
    hours = (exit_time - entry).dt.total_seconds() / 3600.0
    if "age_days" in closed.columns:
        fallback = pd.to_numeric(closed["age_days"], errors="coerce") * 24.0
        hours = hours.where(hours.notna(), fallback)
    return hours


def _coalesced_values(closed: pd.DataFrame, *columns: str) -> pd.Series:
    values = pd.Series(pd.NA, index=closed.index, dtype=object)
    for column in columns:
        if column in closed.columns:
            values = values.where(values.notna(), closed[column])
    return values


def _normalized_text(closed: pd.DataFrame, *columns: str) -> pd.Series:
    return _coalesced_values(closed, *columns).fillna("").astype(str).str.strip().str.lower()


def _explicit_false(closed: pd.DataFrame, *columns: str) -> pd.Series:
    values = _coalesced_values(closed, *columns)
    normalized = values.astype(str).str.strip().str.lower()
    return values.notna() & normalized.isin({"false", "0", "no", "off"})


def _non_positive_entry_size(asset: str, closed: pd.DataFrame) -> pd.Series:
    columns = {
        "option": ("entry_suggested_contracts", "suggested_contracts"),
        "share": ("entry_suggested_dollars", "suggested_dollars", "quantity"),
        "futures": ("entry_contracts", "contracts", "suggested_contracts"),
    }.get(asset, ())
    values = pd.Series(float("nan"), index=closed.index, dtype=float)
    found = False
    for column in columns:
        if column in closed.columns:
            found = True
            numeric = pd.to_numeric(closed[column], errors="coerce")
            values = values.where(values.notna(), numeric)
    if found:
        return values.fillna(0.0) <= 0.0
    # Legacy rows without a sizing field remain auditable and eligible. New
    # lifecycle rows always persist sizing, so unknown size is not introduced
    # going forward.
    return pd.Series(False, index=closed.index, dtype=bool)


def execution_eligibility_flags(asset: str, closed_positions: pd.DataFrame) -> pd.DataFrame:
    """Classify whether closed rows were executable recommendations at entry."""
    closed = _asset_rows(closed_positions, asset)
    if closed.empty:
        return pd.DataFrame(index=closed.index)
    status = _normalized_text(closed, "entry_trade_status", "trade_status")
    guard = _normalized_text(
        closed, "entry_research_guard_status", "research_guard_status",
    )
    flags = pd.DataFrame(index=closed.index)
    flags["explicit_not_actionable"] = _explicit_false(
        closed, "entry_is_actionable", "is_actionable",
    )
    flags["non_actionable_status"] = status.isin(NON_ACTIONABLE_ENTRY_STATUSES)
    flags["guard_blocked"] = guard.eq("blocked")
    flags["non_positive_size"] = _non_positive_entry_size(asset, closed)
    flags["execution_eligible"] = ~flags.any(axis=1)
    return flags


def execution_eligibility_summary(asset: str,
                                  closed_positions: pd.DataFrame) -> Dict[str, int]:
    """Return transparent entry-eligibility counts for validation reporting."""
    closed = _asset_rows(closed_positions, asset)
    flags = execution_eligibility_flags(asset, closed_positions)
    if closed.empty or flags.empty:
        return {
            "execution_eligible_closed_positions": 0,
            "non_executable_closed_positions": 0,
            "excluded_explicit_not_actionable": 0,
            "excluded_non_actionable_status": 0,
            "excluded_guard_blocked": 0,
            "excluded_non_positive_size": 0,
        }
    return {
        "execution_eligible_closed_positions": int(flags["execution_eligible"].sum()),
        "non_executable_closed_positions": int((~flags["execution_eligible"]).sum()),
        "excluded_explicit_not_actionable": int(flags["explicit_not_actionable"].sum()),
        "excluded_non_actionable_status": int(flags["non_actionable_status"].sum()),
        "excluded_guard_blocked": int(flags["guard_blocked"].sum()),
        "excluded_non_positive_size": int(flags["non_positive_size"].sum()),
    }


def eligible_closed_for_learning(asset: str, closed_positions: pd.DataFrame) -> pd.DataFrame:
    """Return independent outcomes suitable for adapting exit thresholds.

    A row must first represent an executable recommendation at entry. Same-scan
    dynamic exits are execution churn, not swing-trade evidence. Hard stops and
    targets remain eligible because they are price-triggered risk exits.
    """
    closed = _asset_rows(closed_positions, asset)
    if closed.empty:
        return closed
    flags = execution_eligibility_flags(asset, closed)
    closed = closed[flags["execution_eligible"]].copy()
    if closed.empty:
        return closed
    pnl = pd.to_numeric(
        closed.get("pnl_pct", pd.Series(float("nan"), index=closed.index)),
        errors="coerce",
    )
    resolved_outcome = pnl.map(lambda value: pd.notna(value) and math.isfinite(float(value)))
    if "validation_eligible" in closed.columns:
        resolved_outcome &= ~_explicit_false(closed, "validation_eligible")
    closed = closed[resolved_outcome].copy()
    if closed.empty:
        return closed
    closed["pnl_pct"] = pnl.loc[closed.index]
    closed["_holding_hours"] = _holding_hours(closed)
    reasons = closed.get("exit_reason", pd.Series("", index=closed.index)).fillna("").astype(str)
    immediate_dynamic = reasons.eq("dynamic_exit") & (
        closed["_holding_hours"].isna()
        | (closed["_holding_hours"] < MIN_LEARNING_HOLD_HOURS)
    )
    closed = closed[~immediate_dynamic].copy()
    if closed.empty:
        return closed
    closed["_learning_episode_id"] = closed.apply(lambda row: _episode_id(row, asset), axis=1)
    return closed.drop_duplicates("_learning_episode_id", keep="last")


def _distinct_trading_days(rows: pd.DataFrame, primary: str, fallback: str) -> int:
    if rows.empty:
        return 0
    source = rows.get(primary)
    if source is None:
        source = rows.get(fallback, pd.Series(dtype=str))
    parsed = pd.to_datetime(source, errors="coerce", utc=True)
    return int(parsed.dt.date.nunique())


def enough_data_for_learning(asset: str, closed_positions: pd.DataFrame,
                             exit_reviews: pd.DataFrame) -> bool:
    closed = eligible_closed_for_learning(asset, closed_positions)
    reviews = _asset_rows(exit_reviews, asset)
    if len(closed) < MIN_LEARNING_CLOSED_POSITIONS or len(reviews) < MIN_LEARNING_EXIT_REVIEWS:
        return False
    closed_days = _distinct_trading_days(closed, "entry_time", "exit_time")
    review_days = _distinct_trading_days(reviews, "timestamp", "entry_time")
    return closed_days >= MIN_LEARNING_TRADING_DAYS and review_days >= MIN_LEARNING_TRADING_DAYS


def _clamp_thresholds(asset_policy: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(asset_policy)
    out["watch_pressure_threshold"] = int(max(35, min(55, out["watch_pressure_threshold"])))
    out["tighten_pressure_threshold"] = int(max(55, min(75, out["tighten_pressure_threshold"])))
    out["close_pressure_threshold"] = int(max(70, min(90, out["close_pressure_threshold"])))
    if out["tighten_pressure_threshold"] <= out["watch_pressure_threshold"]:
        out["tighten_pressure_threshold"] = out["watch_pressure_threshold"] + 10
    if out["close_pressure_threshold"] <= out["tighten_pressure_threshold"]:
        out["close_pressure_threshold"] = out["tighten_pressure_threshold"] + 10
    return out


def _step(old: int, proposed: int) -> int:
    return int(old + max(-5, min(5, proposed - old)))


def _learn_asset(asset: str, current: Dict[str, Any],
                 closed: pd.DataFrame, reviews: pd.DataFrame) -> tuple[Dict[str, Any], list[str]]:
    policy = dict(current)
    reasons = []
    c = eligible_closed_for_learning(asset, closed)
    r = _asset_rows(reviews, asset)
    if not enough_data_for_learning(asset, closed, reviews):
        policy = dict(_deepcopy_default()["assets"].get(asset, {}))
        return policy, ["insufficient independent sample size"]

    r["exit_pressure"] = pd.to_numeric(r.get("exit_pressure"), errors="coerce")
    c["pnl_pct"] = pd.to_numeric(c.get("pnl_pct"), errors="coerce")
    high_holds = r[(r["exit_pressure"] >= 70) & (r.get("action") == "hold")]
    loss_rate = float((c["pnl_pct"] < 0).mean()) if not c.empty else 0.0
    close_success = r[(r.get("action") == "close_early") & (r["current_pnl_pct"] < 0)] if "current_pnl_pct" in r else pd.DataFrame()

    proposed_close = int(policy["close_pressure_threshold"])
    proposed_tighten = int(policy["tighten_pressure_threshold"])
    if len(high_holds) >= 10 and loss_rate > 0.55:
        proposed_close -= 5
        proposed_tighten -= 3
        reasons.append("high-pressure holds often lost")
    if len(close_success) >= 10:
        proposed_close -= 3
        reasons.append("early closes mostly reduced losses")
    winners = float((c["pnl_pct"] > 0).mean()) if not c.empty else 0.0
    if winners > 0.55:
        proposed_tighten += 3
        proposed_close += 3
        reasons.append("closed sample has enough winners to avoid over-tightening")

    policy["tighten_pressure_threshold"] = _step(int(policy["tighten_pressure_threshold"]), proposed_tighten)
    policy["close_pressure_threshold"] = _step(int(policy["close_pressure_threshold"]), proposed_close)
    policy["learned_active"] = True
    policy = _clamp_thresholds(policy)
    return policy, reasons or ["thresholds retained"]


def refit_exit_policy() -> Dict[str, Any]:
    current = load_exit_policy()
    default = _deepcopy_default()
    closed = _closed_positions()
    reviews = _reviews()
    new_policy = _deepcopy_default()
    old_assets = current.get("assets", {})
    history = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "policy_version": datetime.now(timezone.utc).strftime("exit_%Y%m%d_%H%M%S"),
        "assets": {},
    }
    new_policy["policy_version"] = history["policy_version"]
    new_policy["generated_at"] = history["timestamp"]
    for asset, defaults in default["assets"].items():
        old = old_assets.get(asset, defaults)
        learned, reasons = _learn_asset(asset, old, closed, reviews)
        raw_closed = _asset_rows(closed, asset)
        eligible_closed = eligible_closed_for_learning(asset, closed)
        new_policy["assets"][asset] = learned
        history["assets"][asset] = {
            "closed_positions": int(len(raw_closed)),
            "learning_eligible_closed_positions": int(len(eligible_closed)),
            "excluded_closed_positions": int(max(0, len(raw_closed) - len(eligible_closed))),
            "closed_trading_days": _distinct_trading_days(eligible_closed, "entry_time", "exit_time"),
            "exit_reviews": int(len(_asset_rows(reviews, asset))),
            "old_thresholds": {
                "watch": old.get("watch_pressure_threshold"),
                "tighten": old.get("tighten_pressure_threshold"),
                "close": old.get("close_pressure_threshold"),
            },
            "new_thresholds": {
                "watch": learned.get("watch_pressure_threshold"),
                "tighten": learned.get("tighten_pressure_threshold"),
                "close": learned.get("close_pressure_threshold"),
            },
            "learned_active": learned.get("learned_active", False),
            "reasons": reasons,
        }
    save_exit_policy(new_policy)
    POLICY_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with POLICY_HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(history, default=str) + "\n")
    return new_policy


def get_policy_for_asset(asset: str) -> Dict[str, Any]:
    policy = load_exit_policy()
    defaults = _deepcopy_default()["assets"].get(asset, {})
    out = dict(policy.get("assets", {}).get(asset, defaults))
    if not out.get("learned_active", False):
        out = dict(defaults)
        out["policy_version"] = "default"
        return out
    out["policy_version"] = policy.get("policy_version", "unknown")
    return out
