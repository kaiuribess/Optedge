"""Conservative self-learning exit policy."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
POLICY_FILE = DATA_DIR / "exit_policy.json"
POLICY_HISTORY_FILE = DATA_DIR / "exit_policy_history.jsonl"
EXIT_REVIEWS_FILE = DATA_DIR / "exit_reviews.jsonl"

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


def load_exit_policy() -> Dict[str, Any]:
    if not POLICY_FILE.exists():
        return _deepcopy_default()
    try:
        data = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "assets" not in data:
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


def enough_data_for_learning(asset: str, closed_positions: pd.DataFrame,
                             exit_reviews: pd.DataFrame) -> bool:
    closed = closed_positions[closed_positions.get("asset", "") == asset] if not closed_positions.empty else pd.DataFrame()
    reviews = exit_reviews[exit_reviews.get("asset", "") == asset] if not exit_reviews.empty else pd.DataFrame()
    if len(closed) < 100 or len(reviews) < 20:
        return False
    date_source = reviews.get("timestamp", reviews.get("entry_time", pd.Series(dtype=str)))
    days = pd.to_datetime(date_source, errors="coerce", utc=True).dt.date.nunique()
    return days >= 10


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
    c = closed[closed["asset"] == asset].copy()
    r = reviews[reviews["asset"] == asset].copy()
    if not enough_data_for_learning(asset, closed, reviews):
        policy["learned_active"] = False
        return policy, ["insufficient sample size"]

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
        new_policy["assets"][asset] = learned
        history["assets"][asset] = {
            "closed_positions": int(len(closed[closed.get("asset", "") == asset])) if not closed.empty else 0,
            "exit_reviews": int(len(reviews[reviews.get("asset", "") == asset])) if not reviews.empty else 0,
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
    out = dict(policy.get("assets", {}).get(asset, _deepcopy_default()["assets"].get(asset, {})))
    out["policy_version"] = policy.get("policy_version", "default")
    return out
