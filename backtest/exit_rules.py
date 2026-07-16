# Purpose: Shared dynamic exit review logic for options, shares, and futures.
"""Shared dynamic exit review logic for options, shares, and futures."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
EXIT_REVIEWS_FILE = DATA_DIR / "exit_reviews.jsonl"
MIN_DYNAMIC_EXIT_AGE_HOURS = 1.0


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _position_id(position: dict[str, Any], asset: str) -> str:
    if position.get("position_id"):
        return str(position["position_id"])
    if asset == "option":
        return "|".join(str(position.get(k, "")) for k in ("ticker", "side", "strike", "expiry"))
    if asset == "futures":
        return f"{position.get('symbol')}|{position.get('direction')}|{position.get('entry_time')}"
    return f"{position.get('ticker')}|{position.get('entry_time')}"


def _current_score(position: dict[str, Any], current_signal: dict[str, Any] | None) -> float | None:
    if not current_signal:
        return None
    for key in ("fused_score", "rank_score", "share_score", "futures_score"):
        if key in current_signal and current_signal.get(key) is not None:
            return _num(current_signal.get(key))
    return None


def _entry_score(position: dict[str, Any]) -> float:
    for key in ("fused_score", "rank_score", "share_score", "futures_score"):
        if key in position and position.get(key) is not None:
            return _num(position.get(key))
    return 0.0


def _tightened_stop(
    position: dict[str, Any], asset: str, current_price: float | None
) -> float | None:
    if current_price is None:
        return None
    old_stop = _num(position.get("stop_price"), 0.0)
    entry = _num(position.get("entry_price"), _num(position.get("entry_spot"), current_price))
    pnl = _num(position.get("unrealized_pct"), _num(position.get("pnl_pct"), 0.0))
    if asset == "futures" and str(position.get("direction", "long")).lower() == "short":
        candidate = min(old_stop or float("inf"), current_price + abs(current_price - entry) * 0.35)
        if pnl > 0:
            candidate = min(candidate, entry)
        return candidate if candidate > current_price else old_stop
    if asset == "option":
        candidate = old_stop
        if pnl > 0:
            candidate = max(old_stop, min(current_price * 0.85, entry))
        if pnl > 0.5:
            candidate = max(candidate, current_price * 0.70)
        return candidate if candidate < current_price else old_stop
    candidate = old_stop
    if pnl > 0:
        candidate = max(old_stop, min(current_price * 0.94, entry))
    if pnl > 0.12:
        candidate = max(candidate, current_price * 0.90)
    return candidate if candidate < current_price else old_stop


def compute_exit_pressure(
    position: dict[str, Any], current_signal: dict[str, Any] | None = None, asset: str = "option"
) -> dict[str, Any]:
    from backtest.exit_learning import get_policy_for_asset

    policy = get_policy_for_asset(asset)
    learned = bool(policy.get("learned_active"))
    pressure = 0.0
    reasons: list[str] = []
    current_price = position.get("current_price", position.get("current_mid"))
    if current_price is not None:
        current_price = _num(current_price)

    entry_conf = _num(position.get("confidence"), 50.0)
    current_conf = current_signal.get("confidence") if current_signal else None
    if current_conf is not None:
        drop = entry_conf - _num(current_conf)
        if drop >= 25:
            pressure += 60
            reasons.append("confidence collapsed")
        elif drop >= 12:
            pressure += 15
            reasons.append("confidence dropped")

    entry_score = _entry_score(position)
    score_now = _current_score(position, current_signal)
    if score_now is not None:
        if score_now < 0:
            pressure += 25
            reasons.append("current score turned negative")
        elif entry_score and score_now < entry_score * 0.50:
            pressure += 15
            reasons.append("score deteriorated")

    for key, label in [
        ("news_delta", "negative news flip"),
        ("sentiment_delta", "negative sentiment flip"),
        ("sentiment_decay", "negative sentiment decay"),
    ]:
        val = current_signal.get(key) if current_signal else None
        if val is not None and _num(val) < -0.10:
            pressure += 10
            reasons.append(label)
        old_val = position.get(key)
        if val is not None and old_val is not None and (_num(old_val) - _num(val)) > 0.25:
            pressure += 8
            reasons.append(f"{label} vs entry")

    old_regime = str(position.get("regime") or "")
    new_regime = str((current_signal or {}).get("regime") or "")
    if old_regime and new_regime and old_regime != new_regime:
        pressure += 12
        reasons.append("macro regime changed")

    guard_status = str(
        position.get("research_guard_status")
        or (current_signal or {}).get("research_guard_status")
        or ""
    )
    guard_warnings = str(
        position.get("research_guard_warnings")
        or (current_signal or {}).get("research_guard_warnings")
        or ""
    )
    if "blocked" in guard_status:
        pressure += 90
        reasons.append("research guard blocked")
    elif guard_warnings:
        pressure += 8
        reasons.append("research guard warning")

    age = _num(position.get("age_days"), 0.0)
    if age >= 30:
        pressure += 10
        reasons.append("position age over 30d")
    if _num(position.get("reprice_failed_count"), 0) >= 3:
        pressure += 10
        reasons.append("repeated reprice failures")

    if asset == "option":
        if _num(position.get("spread_pct"), 0.0) >= 0.15:
            pressure += 15
            reasons.append("wide option spread")
        dte = _num(position.get("dte_at_entry"), 99) - age
        if dte <= 7:
            pressure += 12
            reasons.append("short remaining DTE")
        if _num(position.get("days_to_earnings"), 99) <= 3:
            pressure += 8
            reasons.append("near earnings IV-crush risk")
    elif asset == "share":
        if (current_signal or {}).get("tech_score") is not None and _num(
            (current_signal or {}).get("tech_score")
        ) < -0.5:
            pressure += 12
            reasons.append("trend deteriorated")
        for key, label in [
            ("earnings_score", "earnings setup deteriorated"),
            ("analyst_score", "analyst support deteriorated"),
            ("fund_score", "fundamental score deteriorated"),
            ("value_score", "value support deteriorated"),
        ]:
            if (current_signal or {}).get(key) is not None and position.get(key) is not None:
                if (_num(position.get(key)) - _num((current_signal or {}).get(key))) > 0.35:
                    pressure += 6
                    reasons.append(label)
    elif asset == "futures":
        direction = str(position.get("direction", "long")).lower()
        fs = (current_signal or {}).get("futures_score")
        if fs is not None and (
            (direction == "long" and _num(fs) < -0.3) or (direction == "short" and _num(fs) > 0.3)
        ):
            pressure += 35
            reasons.append("futures score reversed")
        if (current_signal or {}).get("hv20") is not None and _num(
            (current_signal or {}).get("hv20")
        ) > _num(position.get("hv20"), 0) * 1.5:
            pressure += 10
            reasons.append("volatility spike")
        if (current_signal or {}).get("range_pos") is not None and position.get(
            "range_pos"
        ) is not None:
            old_range = _num(position.get("range_pos"), 0.5)
            new_range = _num((current_signal or {}).get("range_pos"), 0.5)
            if direction == "long" and old_range < 0.35 and new_range > 0.85:
                pressure += 10
                reasons.append("range position reversed high")
            if direction == "short" and old_range > 0.65 and new_range < 0.20:
                pressure += 10
                reasons.append("range position reversed low")

    pressure = max(0, min(100, int(round(pressure))))
    watch_t = int(policy.get("watch_pressure_threshold", 40))
    tighten_t = int(policy.get("tighten_pressure_threshold", 60))
    close_t = int(policy.get("close_pressure_threshold", 80))
    if pressure >= close_t:
        action = "close_early"
    elif pressure >= tighten_t:
        action = "tighten_stop"
    elif pressure >= watch_t:
        action = "watch"
    else:
        action = "hold"

    age_value = position.get("age_days")
    grace_period_active = False
    if age_value is not None:
        age_hours = max(0.0, _num(age_value)) * 24.0
        hard_guard_block = "research guard blocked" in reasons
        if (
            age_hours < MIN_DYNAMIC_EXIT_AGE_HOURS
            and action in {"tighten_stop", "close_early"}
            and not hard_guard_block
        ):
            action = "watch"
            grace_period_active = True
            reasons.append("dynamic exit grace period")

    old_stop = position.get("stop_price")
    new_stop = _tightened_stop(position, asset, current_price) if action == "tighten_stop" else None
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "asset": asset,
        "position_id": _position_id(position, asset),
        "ticker": position.get("ticker") or position.get("symbol"),
        "symbol": position.get("symbol"),
        "action": action,
        "exit_pressure": pressure,
        "old_stop": old_stop,
        "new_stop": new_stop,
        "old_target": position.get("target_price"),
        "new_target": position.get("target_price"),
        "current_price": current_price,
        "current_pnl_pct": position.get("unrealized_pct", position.get("pnl_pct")),
        "current_pnl_dollars": position.get("pnl_dollars"),
        "entry_confidence": position.get("confidence"),
        "current_confidence": current_conf,
        "entry_fused_score": entry_score,
        "current_fused_score": score_now,
        "reasons": reasons,
        "used_learned_policy": learned,
        "policy_version": policy.get("policy_version", "default"),
        "grace_period_active": grace_period_active,
    }


def apply_dynamic_exit_action(
    position: dict[str, Any], exit_review: dict[str, Any], current_price: float | None = None
) -> dict[str, Any]:
    out = dict(position)
    action = exit_review.get("action")
    if action == "watch":
        out["trade_status"] = "Watch"
    elif action == "tighten_stop" and exit_review.get("new_stop") is not None:
        old = _num(out.get("stop_price"), 0.0)
        new = _num(exit_review.get("new_stop"), old)
        direction = str(out.get("direction", "long")).lower()
        if direction == "short":
            if old <= 0 or new < old:
                out["stop_price"] = new
        elif new > old:
            out["stop_price"] = new
    out["latest_exit_pressure"] = exit_review.get("exit_pressure")
    out["latest_exit_action"] = action
    if current_price is not None:
        out["current_price"] = current_price
    return out


def log_exit_review(review: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with EXIT_REVIEWS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(review, default=str) + "\n")
