# Purpose: Fail-conservative research sizing from validated account-equity drawdown.
"""Research-sizing drawdown circuit breaker.

The breaker reads the normalized account-equity drawdown produced by the
validation report. It never treats average per-signal P&L as drawdown and it
fails conservatively when the validation artifact is missing, stale, or
malformed. Live Robinhood review has a separate account-equity interlock.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("optedge.breaker")

ROOT = Path(__file__).resolve().parent.parent
VALIDATION_PATH = ROOT / "data" / "validation_summary.json"


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _conservative_unavailable(reason: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "multiplier": 0.5,
        "verdict": f"validation drawdown unavailable — Kelly capped at 0.5x ({reason})",
        "n": 0,
        "max_drawdown": None,
        "rolling_pnl_pct": None,
        "rolling_win_rate": None,
        "source": "validation_summary",
    }


def _state_from_drawdown(drawdown: float, *, n: int = 0) -> dict[str, Any]:
    """Convert a normalized equity drawdown into a research sizing multiplier."""
    if drawdown <= -0.20:
        multiplier = 0.25
        verdict = f"deep validation drawdown {drawdown:.1%} — Kelly cut to 0.25x"
    elif drawdown <= -0.10:
        multiplier = 0.5
        verdict = f"validation drawdown {drawdown:.1%} — Kelly cut to 0.5x"
    else:
        multiplier = 1.0
        verdict = f"validation drawdown within research-sizing limit ({drawdown:.1%})"
    return {
        "status": "ready",
        "multiplier": multiplier,
        "verdict": verdict,
        "n": int(n),
        "max_drawdown": float(drawdown),
        # Retained as null compatibility fields. Average trade P&L is
        # deliberately no longer mislabeled as rolling drawdown.
        "rolling_pnl_pct": None,
        "rolling_win_rate": None,
        "source": "validation_summary",
    }


def compute_breaker_state(window_days: int = 14) -> dict[str, Any]:
    """Return a fail-conservative research sizing state from validated drawdown."""
    try:
        payload = json.loads(VALIDATION_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _conservative_unavailable("summary missing")
    except Exception as exc:
        log.debug("breaker validation load failed: %s", exc)
        return _conservative_unavailable("summary malformed")

    generated = payload.get("generated_at")
    try:
        generated_at = datetime.fromisoformat(str(generated).replace("Z", "+00:00"))
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=UTC)
        age_days = (datetime.now(UTC) - generated_at.astimezone(UTC)).total_seconds() / 86400.0
    except Exception:
        return _conservative_unavailable("timestamp missing")
    if age_days < -1.0 or age_days > max(1, int(window_days)):
        return _conservative_unavailable("summary stale")

    after_cost = payload.get("after_slippage")
    overall = payload.get("overall")
    after_cost = after_cost if isinstance(after_cost, dict) else {}
    overall = overall if isinstance(overall, dict) else {}
    drawdown = _finite(after_cost.get("max_drawdown"))
    if drawdown is None:
        drawdown = _finite(overall.get("max_drawdown"))
    if drawdown is None or drawdown > 0 or drawdown < -1:
        return _conservative_unavailable("drawdown missing or invalid")
    n = _finite(after_cost.get("n"))
    if n is None:
        n = _finite(overall.get("n")) or 0
    return _state_from_drawdown(drawdown, n=int(max(0, n)))


def apply_breaker_to_kelly(kelly_pct: float, breaker_mult: float = 1.0) -> float:
    """Multiply Kelly by a finite multiplier that can never increase risk."""
    try:
        kelly = float(kelly_pct)
        multiplier = float(breaker_mult)
    except (TypeError, ValueError):
        return kelly_pct
    if not math.isfinite(kelly) or not math.isfinite(multiplier):
        return kelly_pct
    return kelly * min(1.0, max(0.0, multiplier))
