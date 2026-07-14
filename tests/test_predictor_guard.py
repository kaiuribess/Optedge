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
        "asset": "share",
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
                   samples: int = 500, days: int = 320,
                   latest_outcome_at: datetime | None = None) -> None:
    metadata = {
        "schema": predictor.RUNTIME_WEIGHT_CHAMPION_SCHEMA,
        "trust_state": predictor.TRUSTED_CHAMPION,
        "source": "test_promotion",
        "asset_scope": "multi_asset",
        "horizon_sessions": 10,
        "target_basis": predictor.RUNTIME_TARGET_BASIS,
        "generated_at": generated_at.isoformat(),
        "promoted_at": generated_at.isoformat(),
        "sample_count": samples,
        "unique_days": days,
        "factor_count": len(weights),
        "max_factor_weight": max(weights.values()),
        "adaptive_blend": predictor.ADAPTIVE_WEIGHT_BLEND,
        "source_evidence": {
            "outcome_digest_sha256": "a" * 64,
            "policy_digest_sha256": predictor._current_policy_digest(),
            "latest_outcome_at": (latest_outcome_at or generated_at).isoformat(),
        },
        "oos": {
            "method": predictor.OOS_METHOD,
            "passed": True,
            "folds": 5,
            "purge_sessions": 10,
            "validated_assets": ["share", "option"],
            "options_target_basis": "broker_observed_option_return",
            "n_predictions_by_asset": {"share": 500, "option": 500},
            "effective_horizon_blocks_by_asset": {"share": 30, "option": 30},
            "champion_delta_ci_low_by_asset": {"share": 0.001, "option": 0.001},
            "cost_stress_2x_mean_by_asset": {"share": 0.001, "option": 0.001},
        },
    }
    metadata["content_digest_sha256"] = predictor._runtime_content_digest(
        weights, metadata
    )
    path.write_text(
        f"RUNTIME_WEIGHT_META = {metadata!r}\nSIGNAL_WEIGHTS = {weights!r}\n",
        encoding="utf-8",
    )


def _predictor_champion_payload(now: datetime) -> dict:
    coefs = {column: 0.0 for column in predictor.Z_COLS}
    coefs["z_mispricing"] = 0.01
    payload = {
        "schema": predictor.PREDICTOR_CHAMPION_SCHEMA,
        "trust_state": predictor.TRUSTED_CHAMPION,
        "model_kind": predictor.PREDICTOR_MODEL_KIND,
        "asset": "share",
        "horizon_sessions": 10,
        "target_basis": predictor.PREDICTOR_TARGET_BASIS,
        "coefs": coefs,
        "oos": {
            "method": predictor.OOS_METHOD,
            "passed": True,
            "folds": 5,
            "unique_entry_days": 320,
            "effective_horizon_blocks": 32,
            "n_predictions": 500,
            "purge_sessions": 10,
            "after_cost_mean": 0.01,
            "champion_delta_ci_low": 0.001,
            "cost_stress_2x_mean": 0.005,
            "recent_half_mean": 0.008,
        },
        "source": {
            "outcome_digest_sha256": "a" * 64,
            "policy_digest_sha256": predictor._current_policy_digest(),
            "latest_outcome_at": now.isoformat(),
        },
        "promoted_at": now.isoformat(),
    }
    payload["content_digest_sha256"] = predictor._predictor_content_digest(payload)
    return payload


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


def test_research_fit_is_shadow_only_and_never_persists_on_an_ordinary_call():
    with tempfile.TemporaryDirectory() as temp_dir:
        old_path = predictor.COEFS_PATH
        path = Path(temp_dir) / "predictor.json"
        predictor.COEFS_PATH = path
        try:
            payload = predictor.fit_return_predictor(_outcomes(500, 320))
            assert payload["schema"] == predictor.PREDICTOR_SHADOW_SCHEMA
            assert payload["trust_state"] == predictor.SHADOW_UNTRUSTED
            assert payload["meta"]["activation_eligible"] is False
            assert payload["meta"]["persistence"] == "disabled"
            assert not path.exists()
        finally:
            predictor.COEFS_PATH = old_path


def test_runtime_weight_research_fit_does_not_persist_a_scan_override():
    with tempfile.TemporaryDirectory() as temp_dir:
        old_path = predictor.RUNTIME_CONFIG_PATH
        path = Path(temp_dir) / "config_runtime.py"
        predictor.RUNTIME_CONFIG_PATH = path
        try:
            candidate = predictor.update_runtime_weights(_outcomes(500, 320))
            assert candidate is not None
            assert not path.exists()
        finally:
            predictor.RUNTIME_CONFIG_PATH = old_path


def test_mixed_assets_and_option_returns_cannot_train_the_stock_predictor():
    mixed = _outcomes(500, 320)
    mixed.loc[mixed.index[::2], "asset"] = "option"
    mixed_payload = predictor.fit_return_predictor(mixed)
    assert mixed_payload["meta"]["reason"] == "mixed_asset_training_rejected"
    assert all(value == 0.0 for value in mixed_payload["coefs"].values())

    options = _outcomes(500, 320)
    options["asset"] = "option"
    options["outcome_quality"] = "broker_market_observed"
    option_payload = predictor.fit_return_predictor(options)
    assert (
        option_payload["meta"]["reason"]
        == "option_adaptation_requires_direct_broker_observed_target"
    )
    assert all(value == 0.0 for value in option_payload["coefs"].values())


def test_legacy_predictor_artifact_fails_closed_to_safe_zeros():
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "predictor.json"
        path.write_text(
            json.dumps({"coefs": {"z_mispricing": 0.05}, "meta": {"source": "legacy"}}),
            encoding="utf-8",
        )

        status = predictor.predictor_artifact_status(path)
        assert status["usable"] is False
        assert any("trusted champion schema" in reason for reason in status["reasons"])
        assert all(value == 0.0 for value in predictor.load_predictor_coefs(path=path).values())


def test_digest_valid_share_champion_loads_only_for_shares_and_tampering_fails():
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "predictor.json"
        now = datetime.now(UTC)
        payload = _predictor_champion_payload(now)
        path.write_text(json.dumps(payload), encoding="utf-8")

        status = predictor.predictor_artifact_status(path, now=now)
        assert status["usable"] is True
        assert predictor.load_predictor_coefs(asset="share", path=path)["z_mispricing"] == 0.01
        assert all(
            value == 0.0
            for value in predictor.load_predictor_coefs(asset="option", path=path).values()
        )

        payload["coefs"]["z_mispricing"] = 0.02
        path.write_text(json.dumps(payload), encoding="utf-8")
        tampered = predictor.predictor_artifact_status(path, now=now)
        assert tampered["usable"] is False
        assert "predictor content digest does not match" in tampered["reasons"]
        assert all(value == 0.0 for value in predictor.load_predictor_coefs(path=path).values())


def test_digest_valid_but_retired_policy_champion_still_fails_closed():
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "predictor.json"
        now = datetime.now(UTC)
        payload = _predictor_champion_payload(now)
        payload["source"]["policy_digest_sha256"] = "c" * 64
        payload["content_digest_sha256"] = predictor._predictor_content_digest(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")

        status = predictor.predictor_artifact_status(path, now=now)
        assert status["usable"] is False
        assert "predictor source policy digest is not current" in status["reasons"]
        assert all(value == 0.0 for value in predictor.load_predictor_coefs(path=path).values())


def test_model_trust_status_exposes_safe_defaults_without_promoted_artifacts():
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        report = predictor.model_trust_status(
            predictor_path=root / "missing_predictor.json",
            runtime_path=root / "missing_runtime.py",
        )

    assert report["schema"] == predictor.MODEL_TRUST_SCHEMA
    assert report["status"] == "source_controlled_defaults"
    assert report["trusted_components"] == []
    assert report["ordinary_scan_training"] == "disabled"
    assert report["safe_default"] == "zero_predictor_and_source_controlled_weights"


def test_runtime_status_accepts_fresh_diversified_full_coverage_file():
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "config_runtime.py"
        weights = predictor._normalize_and_cap_weights(
            predictor._configured_signal_weights()
        )
        now = datetime.now(UTC)
        _write_runtime(path, weights, generated_at=now)
        status = predictor.runtime_weight_status(path)
        assert status["usable"] is True
        assert status["reasons"] == []
        assert len(predictor.load_runtime_weights(path)) == len(weights)


def test_runtime_persistence_helper_writes_untrusted_shadow_by_default():
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "config_runtime.py"
        weights = predictor._normalize_and_cap_weights(
            predictor._configured_signal_weights()
        )
        predictor._persist_runtime_weights(
            weights,
            source="research_test",
            sample_count=500,
            unique_days=320,
            path=path,
        )

        status = predictor.runtime_weight_status(path)
        assert status["usable"] is False
        assert status["schema"] == predictor.RUNTIME_WEIGHT_SHADOW_SCHEMA
        assert status["trust_state"] == predictor.SHADOW_UNTRUSTED
        assert "runtime content digest does not match" not in status["reasons"]
        assert predictor.load_runtime_weights(path) is None


def test_runtime_champion_digest_detects_weight_tampering():
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "config_runtime.py"
        weights = predictor._normalize_and_cap_weights(
            predictor._configured_signal_weights()
        )
        _write_runtime(path, weights, generated_at=datetime.now(UTC))
        assignments = predictor._literal_assignments(
            path, {"SIGNAL_WEIGHTS", "RUNTIME_WEIGHT_META"}
        )
        tampered = dict(assignments["SIGNAL_WEIGHTS"])
        tampered["macro"] += 0.001
        path.write_text(
            "RUNTIME_WEIGHT_META = "
            f"{assignments['RUNTIME_WEIGHT_META']!r}\nSIGNAL_WEIGHTS = {tampered!r}\n",
            encoding="utf-8",
        )

        status = predictor.runtime_weight_status(path)
        assert status["usable"] is False
        assert "runtime content digest does not match" in status["reasons"]
        assert predictor.load_runtime_weights(path) is None


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
