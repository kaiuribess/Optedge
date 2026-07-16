# Purpose: Verify the research sizing breaker uses real drawdown and fails conservatively.
from __future__ import annotations

import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest import drawdown_breaker  # noqa: E402


def test_drawdown_state_uses_equity_drawdown_not_average_trade_return():
    state = drawdown_breaker._state_from_drawdown(-0.2626, n=10)
    assert state["multiplier"] == 0.25
    assert state["max_drawdown"] == -0.2626
    assert state["rolling_pnl_pct"] is None


def test_missing_and_stale_validation_fail_conservatively():
    with tempfile.TemporaryDirectory() as temp_dir:
        old_path = drawdown_breaker.VALIDATION_PATH
        path = Path(temp_dir) / "validation_summary.json"
        drawdown_breaker.VALIDATION_PATH = path
        try:
            missing = drawdown_breaker.compute_breaker_state()
            assert missing["status"] == "unavailable"
            assert missing["multiplier"] == 0.5

            path.write_text(
                json.dumps(
                    {
                        "generated_at": (datetime.now(UTC) - timedelta(days=30)).isoformat(),
                        "after_slippage": {"n": 100, "max_drawdown": -0.02},
                    }
                ),
                encoding="utf-8",
            )
            stale = drawdown_breaker.compute_breaker_state(window_days=14)
            assert stale["status"] == "unavailable"
            assert stale["multiplier"] == 0.5
        finally:
            drawdown_breaker.VALIDATION_PATH = old_path


def test_fresh_validation_drives_versioned_thresholds():
    with tempfile.TemporaryDirectory() as temp_dir:
        old_path = drawdown_breaker.VALIDATION_PATH
        path = Path(temp_dir) / "validation_summary.json"
        drawdown_breaker.VALIDATION_PATH = path
        try:
            for drawdown, expected in ((-0.05, 1.0), (-0.10, 0.5), (-0.20, 0.25)):
                path.write_text(
                    json.dumps(
                        {
                            "generated_at": datetime.now(UTC).isoformat(),
                            "after_slippage": {"n": 250, "max_drawdown": drawdown},
                        }
                    ),
                    encoding="utf-8",
                )
                state = drawdown_breaker.compute_breaker_state()
                assert state["status"] == "ready"
                assert state["multiplier"] == expected
                assert state["n"] == 250
        finally:
            drawdown_breaker.VALIDATION_PATH = old_path


def test_breaker_multiplier_cannot_increase_kelly():
    assert drawdown_breaker.apply_breaker_to_kelly(0.10, 2.0) == 0.10
    assert drawdown_breaker.apply_breaker_to_kelly(0.10, -1.0) == 0.0
