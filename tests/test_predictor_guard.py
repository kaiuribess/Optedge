# Purpose: Test predictor evidence freshness and diversity gates.
"""Regression tests for adaptive predictor and runtime-weight trust guards."""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest import predictor  # noqa: E402


def _outcomes(samples: int, days: int) -> pd.DataFrame:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    values = np.linspace(-1.0, 1.0, samples)
    frame = pd.DataFrame({
        "entry_time": [(start + timedelta(days=index % days)).isoformat()
                       for index in range(samples)],
        "pnl_pct": values * 0.05,
        "pnl_pct_after_slippage": values * 0.05 - 0.01,
    })
    for column in predictor.Z_COLS:
        frame[column] = 0.0
    frame["z_mispricing"] = values
    return frame


def _write_runtime(path: Path, weights: dict, *, generated_at: datetime,
                   samples: int = 500, days: int = 10,
                   latest_outcome_at: datetime | None = None) -> None:
    metadata = {
        "source": "test",
        "generated_at": generated_at.isoformat(),
        "latest_outcome_at": (latest_outcome_at or generated_at).isoformat(),
        "sample_count": samples,
        "unique_days": days,
    }
    path.write_text(
        f"RUNTIME_WEIGHT_META = {metadata!r}\nSIGNAL_WEIGHTS = {weights!r}\n",
        encoding="utf-8",
    )


def test_walk_forward_guard_requires_samples_and_days():
    assert not predictor._has_enough_history_for_lasso(_outcomes(600, 7))
    assert not predictor._has_enough_history_for_lasso(_outcomes(499, 10))
    assert predictor._has_enough_history_for_lasso(_outcomes(500, 10))


def test_insufficient_history_does_not_persist_runtime_override():
    with tempfile.TemporaryDirectory() as temp_dir:
        old_path = predictor.RUNTIME_CONFIG_PATH
        runtime_path = Path(temp_dir) / "config_runtime.py"
        predictor.RUNTIME_CONFIG_PATH = runtime_path
        try:
            historical_ic = pd.DataFrame({
                "horizon_days": [7],
                "factor": ["value_score"],
                "ic": [0.9],
                "n": [9999],
            })
            result = predictor.update_runtime_weights(_outcomes(600, 7), historical_ic)
            assert result is None
            assert not runtime_path.exists()
        finally:
            predictor.RUNTIME_CONFIG_PATH = old_path


def test_predictor_does_not_fit_or_bootstrap_from_weak_history():
    with tempfile.TemporaryDirectory() as temp_dir:
        old_path = predictor.COEFS_PATH
        predictor.COEFS_PATH = Path(temp_dir) / "predictor.json"
        try:
            historical_ic = pd.DataFrame({
                "horizon_days": [7],
                "factor": ["value_score"],
                "spread": [0.9],
                "n": [9999],
            })
            payload = predictor.fit_return_predictor(_outcomes(600, 7), historical_ic)
            assert payload["meta"]["source"] == "zero_init"
            assert payload["meta"]["reason"] == "insufficient_walk_forward_history"
            assert all(value == 0.0 for value in payload["coefs"].values())
        finally:
            predictor.COEFS_PATH = old_path


def test_runtime_status_accepts_fresh_diversified_full_coverage_file():
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "config_runtime.py"
        weights = predictor._normalize_and_cap_weights(
            predictor._configured_signal_weights()
        )
        predictor._persist_runtime_weights(
            weights,
            source="test",
            sample_count=500,
            unique_days=10,
            path=path,
        )
        status = predictor.runtime_weight_status(path)
        assert status["usable"] is True
        assert status["reasons"] == []
        assert len(predictor.load_runtime_weights(path)) == len(weights)


def test_runtime_status_rejects_stale_concentrated_and_incomplete_files():
    priors = predictor._normalize_and_cap_weights(predictor._configured_signal_weights())
    now = datetime.now(UTC)
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)

        stale = root / "stale.py"
        _write_runtime(stale, priors, generated_at=now - timedelta(days=30))
        assert not predictor.runtime_weight_status(stale)["usable"]
        assert any("days old" in reason for reason in predictor.runtime_weight_status(stale)["reasons"])

        stale_outcomes = root / "stale_outcomes.py"
        _write_runtime(
            stale_outcomes,
            priors,
            generated_at=now,
            latest_outcome_at=now - timedelta(days=30),
        )
        stale_outcome_status = predictor.runtime_weight_status(stale_outcomes)
        assert not stale_outcome_status["usable"]
        assert any("training outcome" in reason for reason in stale_outcome_status["reasons"])

        concentrated_weights = dict(priors)
        concentrated_weights["macro"] = 0.50
        remainder_keys = [key for key in concentrated_weights if key != "macro"]
        remainder_total = sum(priors[key] for key in remainder_keys)
        for key in remainder_keys:
            concentrated_weights[key] = 0.50 * priors[key] / remainder_total
        concentrated = root / "concentrated.py"
        _write_runtime(concentrated, concentrated_weights, generated_at=now)
        concentrated_status = predictor.runtime_weight_status(concentrated)
        assert not concentrated_status["usable"]
        assert any("concentration" in reason for reason in concentrated_status["reasons"])

        incomplete_weights = dict(list(priors.items())[:8])
        incomplete_total = sum(incomplete_weights.values())
        incomplete_weights = {
            key: value / incomplete_total for key, value in incomplete_weights.items()
        }
        incomplete = root / "incomplete.py"
        _write_runtime(incomplete, incomplete_weights, generated_at=now)
        incomplete_status = predictor.runtime_weight_status(incomplete)
        assert not incomplete_status["usable"]
        assert any("coverage" in reason for reason in incomplete_status["reasons"])


def test_runtime_status_rejects_legacy_and_malformed_files():
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        legacy = root / "legacy.py"
        legacy.write_text("SIGNAL_WEIGHTS = {'macro': 1.0}\n", encoding="utf-8")
        assert predictor.load_runtime_weights(legacy) is None
        assert "missing trust metadata" in predictor.runtime_weight_status(legacy)["reasons"]

        malformed = root / "malformed.py"
        malformed.write_text("SIGNAL_WEIGHTS = {\n", encoding="utf-8")
        assert predictor.load_runtime_weights(malformed) is None
        assert any("malformed" in reason for reason in
                   predictor.runtime_weight_status(malformed)["reasons"])


def test_adaptive_outcomes_exclude_same_scan_dynamic_churn():
    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = Path(temp_dir)
        rows = [
            {
                "position_id": "churn",
                "entry_time": "2026-01-01T10:00:00+00:00",
                "exit_time": "2026-01-01T10:10:00+00:00",
                "exit_reason": "dynamic_exit",
                "pnl_pct": -0.10,
                "z_mispricing": 1.0,
            },
            {
                "position_id": "swing",
                "entry_time": "2026-01-01T10:00:00+00:00",
                "exit_time": "2026-01-02T10:00:00+00:00",
                "exit_reason": "hard_target",
                "pnl_pct": 0.10,
                "z_mispricing": 1.0,
            },
        ]
        (data_dir / "closed_positions.json").write_text(
            json.dumps(rows), encoding="utf-8"
        )
        outcomes = predictor.load_adaptive_outcomes(data_dir)
        assert outcomes["position_id"].tolist() == ["swing"]
        assert abs(float(outcomes.iloc[0]["pnl_pct_after_slippage"]) - 0.06) < 1e-9


def test_learned_weights_preserve_full_factor_set_and_ignore_negative_coefficients():
    priors = predictor._normalize_and_cap_weights(predictor._configured_signal_weights())
    learned = predictor._normalize_to_signal_weights(
        {"z_macro": 10.0, "z_mispricing": -50.0}, priors
    )
    assert set(learned) == set(priors)
    assert abs(sum(learned.values()) - 1.0) < 1e-9
    assert max(learned.values()) <= predictor.MAX_RUNTIME_FACTOR_WEIGHT + 1e-9
    assert learned["macro"] > priors["macro"]
    assert learned["mispricing"] < priors["mispricing"]


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"predictor guard tests passed ({len(tests)})")
