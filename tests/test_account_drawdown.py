# Purpose: Verify the fail-closed Robinhood account drawdown interlock.
from __future__ import annotations

import json
import tempfile
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import risk.account_drawdown as drawdown
import scripts.normalize_robinhood_broker_snapshot as normalizer

ACCOUNT_KEY = "acct_0123456789abcdef"


def _snapshot(
    observed_at: datetime,
    equity: float,
    *,
    account_key: str = ACCOUNT_KEY,
    normalized_at: str = "2026-07-13T20:00:01+00:00",
) -> dict:
    return {
        "schema": drawdown.BROKER_SNAPSHOT_SCHEMA,
        "generated_at": observed_at.astimezone(UTC).isoformat(),
        "normalized_at": normalized_at,
        "source": "read_only_robinhood_agentic_mcp_export",
        "raw_bundle_schema": "optedge_robinhood_mcp_read_bundle_v2",
        "does_not_place_orders": True,
        "normalization_blockers": [],
        "accounts": [
            {
                "account_key": account_key,
                "portfolio": {"total_value": equity},
            }
        ],
        "option_positions": [],
        "equity_positions": [],
        "option_orders": [],
        "equity_orders": [],
        "counts": {
            "accounts": 1,
            "option_positions": 0,
            "equity_positions": 0,
            "option_orders": 0,
            "equity_orders": 0,
            "missing_option_contracts": 0,
        },
    }


def _append(ledger: dict | None, snapshot: dict) -> dict:
    out, appended = drawdown.append_snapshot_observation(ledger, snapshot, ACCOUNT_KEY)
    assert appended is True
    return out


def _ledger_for_values(values: list[float], *, start: datetime | None = None) -> tuple[dict, dict]:
    start = start or datetime(2026, 7, 10, 20, tzinfo=UTC)
    ledger = None
    latest = None
    for offset, value in enumerate(values):
        latest = _snapshot(start + timedelta(days=offset), value)
        ledger = _append(ledger, latest)
    assert ledger is not None and latest is not None
    return ledger, latest


def _evaluate(ledger: dict, snapshot: dict, *, now: datetime | None = None) -> dict:
    observed = datetime.fromisoformat(snapshot["generated_at"])
    return drawdown.evaluate_account_drawdown(
        ledger,
        snapshot,
        account_key=ACCOUNT_KEY,
        now=now or observed + timedelta(minutes=1),
    )


def _reseal(ledger: dict) -> None:
    previous_hash = drawdown.GENESIS_HASH
    for index, observation in enumerate(ledger["observations"]):
        observation["sequence"] = index + 1
        observation["previous_observation_hash_sha256"] = previous_hash
        observation["observation_hash_sha256"] = drawdown._observation_digest(observation)
        previous_hash = observation["observation_hash_sha256"]
    ledger["ledger_digest_sha256"] = drawdown._ledger_digest(ledger)


@pytest.mark.parametrize(
    ("drawdown_fraction", "expected_status", "expected_multiplier"),
    [
        (0.0499, "ready", 1.0),
        (0.05, "reduced", 0.5),
        (0.0799, "reduced", 0.5),
        (0.08, "reduced", 0.25),
        (0.0999, "reduced", 0.25),
        (0.10, "blocked", 0.0),
    ],
)
def test_high_water_drawdown_policy_boundaries(
    drawdown_fraction: float,
    expected_status: str,
    expected_multiplier: float,
):
    current = 10_000 * (1.0 - drawdown_fraction)
    # Repeat current equity on the prior session so the test isolates the
    # high-water rule from the separate New York-session loss rule.
    ledger, snapshot = _ledger_for_values([10_000, current, current])

    result = _evaluate(ledger, snapshot)

    assert result["status"] == expected_status
    assert result["risk_multiplier"] == expected_multiplier
    assert result["risk_multiplier"] <= 1.0
    assert result["high_water_equity_dollars"] == 10_000
    assert result["high_water_drawdown_fraction"] == pytest.approx(-drawdown_fraction)
    assert result["review_ready"] is (expected_status != "blocked")
    if drawdown_fraction == 0.10:
        assert any("at least 10%" in value for value in result["blockers"])


def test_ny_session_loss_blocks_at_exact_three_percent_but_not_just_above_boundary():
    base_time = datetime(2026, 7, 10, 20, tzinfo=UTC)
    blocked_ledger, blocked_snapshot = _ledger_for_values([10_000, 9_700], start=base_time)
    allowed_ledger, allowed_snapshot = _ledger_for_values([10_000, 9_701], start=base_time)

    blocked = _evaluate(blocked_ledger, blocked_snapshot)
    allowed = _evaluate(allowed_ledger, allowed_snapshot)

    assert blocked["status"] == "blocked"
    assert blocked["ny_session_loss_fraction"] == -0.03
    assert any("New York-session" in value for value in blocked["blockers"])
    assert allowed["status"] == "ready"
    assert allowed["ny_session_loss_fraction"] == -0.0299


def test_new_york_calendar_boundary_uses_prior_session_observation():
    # 03:30 UTC is still the prior NY calendar date; 04:30 UTC is the next.
    first = _snapshot(datetime(2026, 7, 13, 3, 30, tzinfo=UTC), 10_000)
    second = _snapshot(datetime(2026, 7, 13, 4, 30, tzinfo=UTC), 9_700)
    ledger = _append(None, first)
    ledger = _append(ledger, second)

    result = _evaluate(ledger, second)

    assert result["ny_session_date"] == "2026-07-13"
    assert result["ny_session_reference_equity_dollars"] == 10_000
    assert result["ny_session_loss_fraction"] == -0.03
    assert result["status"] == "blocked"


def test_large_unexplained_adjacent_jump_blocks_at_25_percent():
    blocked_ledger, blocked_snapshot = _ledger_for_values([10_000, 12_500])
    allowed_ledger, allowed_snapshot = _ledger_for_values([10_000, 12_499])

    blocked = _evaluate(blocked_ledger, blocked_snapshot)
    allowed = _evaluate(allowed_ledger, allowed_snapshot)

    assert blocked["status"] == "blocked"
    assert any("possible cash flow" in value for value in blocked["blockers"])
    assert allowed["status"] == "ready"


def test_one_observation_is_only_a_blocked_baseline():
    snapshot = _snapshot(datetime(2026, 7, 13, 20, tzinfo=UTC), 10_000)
    ledger = _append(None, snapshot)

    result = _evaluate(ledger, snapshot)

    assert result["status"] == "blocked"
    assert result["observation_count"] == 1
    assert result["risk_multiplier"] == 0
    assert any("at least two" in value for value in result["blockers"])


def test_quick_reset_cannot_create_a_ready_baseline_across_midnight():
    # These observations cross a New York calendar boundary but are only one
    # hour apart, so deleting state and quickly taking two new samples cannot
    # recreate a trusted drawdown history.
    first = _snapshot(datetime(2026, 7, 13, 3, 30, tzinfo=UTC), 10_000)
    second = _snapshot(datetime(2026, 7, 13, 4, 30, tzinfo=UTC), 10_000)
    ledger = _append(None, first)
    ledger = _append(ledger, second)

    result = _evaluate(ledger, second)

    assert result["status"] == "blocked"
    assert result["baseline_started_at"] == "2026-07-13T03:30:00+00:00"
    assert result["baseline_span_hours"] == 1.0
    assert result["baseline_ny_calendar_date_count"] == 2
    assert result["policy"]["minimum_baseline_observations"] == 2
    assert result["policy"]["minimum_baseline_ny_calendar_dates"] == 2
    assert result["policy"]["minimum_baseline_span_hours"] == 18.0
    assert any("span at least 18 hours" in value for value in result["blockers"])


def test_cross_day_eighteen_hour_baseline_is_review_ready():
    first = _snapshot(datetime(2026, 7, 13, 20, tzinfo=UTC), 10_000)
    second = _snapshot(datetime(2026, 7, 14, 14, tzinfo=UTC), 10_000)
    ledger = _append(None, first)
    ledger = _append(ledger, second)

    result = _evaluate(ledger, second)

    assert result["status"] == "ready"
    assert result["allowed"] is True
    assert result["baseline_span_hours"] == 18.0
    assert result["baseline_ny_calendar_date_count"] == 2
    assert result["blockers"] == []


@pytest.mark.parametrize("unsafe_ledger", [None, {}, [], "ledger"])
def test_missing_or_malformed_ledger_fails_closed(unsafe_ledger):
    snapshot = _snapshot(datetime(2026, 7, 13, 20, tzinfo=UTC), 10_000)

    result = drawdown.evaluate_account_drawdown(
        unsafe_ledger,
        snapshot,
        account_key=ACCOUNT_KEY,
        now=datetime(2026, 7, 13, 20, 1, tzinfo=UTC),
    )

    assert result["status"] == "blocked"
    assert result["risk_multiplier"] == 0
    assert result["ledger_digest_sha256"] is None


def test_tampered_observation_or_ledger_digest_fails_closed():
    ledger, snapshot = _ledger_for_values([10_000, 10_010])
    tampered_observation = deepcopy(ledger)
    tampered_observation["observations"][0]["equity_dollars"] = 999_999
    tampered_digest = deepcopy(ledger)
    tampered_digest["ledger_digest_sha256"] = "f" * 64

    for unsafe in (tampered_observation, tampered_digest):
        result = _evaluate(unsafe, snapshot)
        assert result["status"] == "blocked"
        assert result["risk_multiplier"] == 0
        assert any(
            "digest" in value.lower() or "hash" in value.lower() for value in result["blockers"]
        )


def test_structurally_sealed_mixed_account_or_nonmonotonic_time_is_rejected():
    ledger, _ = _ledger_for_values([10_000, 10_010])
    mixed = deepcopy(ledger)
    mixed["observations"][1]["account_key"] = "acct_fedcba9876543210"
    _reseal(mixed)
    backwards = deepcopy(ledger)
    backwards["observations"][1]["observed_at"] = "2026-07-09T20:00:00+00:00"
    _reseal(backwards)

    mixed_validation = drawdown.validate_equity_ledger(mixed)
    backwards_validation = drawdown.validate_equity_ledger(backwards)

    assert mixed_validation["valid"] is False
    assert any("single account" in value for value in mixed_validation["blockers"])
    assert backwards_validation["valid"] is False
    assert any("strictly increasing" in value for value in backwards_validation["blockers"])


def test_current_snapshot_must_match_latest_observation_and_source_digest():
    ledger, snapshot = _ledger_for_values([10_000, 10_010])
    normalized_later = deepcopy(snapshot)
    normalized_later["normalized_at"] = "2099-01-01T00:00:00+00:00"
    changed_equity = deepcopy(snapshot)
    changed_equity["accounts"][0]["portfolio"]["total_value"] = 10_011
    changed_source = deepcopy(snapshot)
    changed_source["counts"]["equity_orders"] = 1

    assert _evaluate(ledger, normalized_later)["status"] == "ready"
    for mismatch in (changed_equity, changed_source):
        result = _evaluate(ledger, mismatch)
        assert result["status"] == "blocked"
        assert any("does not match" in value for value in result["blockers"])


def test_requested_account_must_match_single_account_ledger():
    ledger, snapshot = _ledger_for_values([10_000, 10_010])

    result = drawdown.evaluate_account_drawdown(
        ledger,
        snapshot,
        account_key="acct_fedcba9876543210",
        now=datetime(2026, 7, 11, 20, 1, tzinfo=UTC),
    )

    assert result["status"] == "blocked"
    assert result["risk_multiplier"] == 0
    assert any("requested account" in value for value in result["blockers"])


def test_stale_or_materially_future_latest_observation_blocks():
    ledger, snapshot = _ledger_for_values([10_000, 10_010])
    latest = datetime.fromisoformat(snapshot["generated_at"])

    stale = _evaluate(ledger, snapshot, now=latest + timedelta(minutes=90, seconds=1))
    exact_age = _evaluate(ledger, snapshot, now=latest + timedelta(minutes=90))
    future = _evaluate(ledger, snapshot, now=latest - timedelta(seconds=61))

    assert stale["status"] == "blocked"
    assert any("stale" in value for value in stale["blockers"])
    assert exact_age["status"] == "ready"
    assert future["status"] == "blocked"
    assert any("future" in value for value in future["blockers"])


def test_append_deduplicates_identical_snapshot_and_rejects_time_conflicts():
    first = _snapshot(datetime(2026, 7, 13, 20, tzinfo=UTC), 10_000)
    ledger = _append(None, first)

    deduped, appended = drawdown.append_snapshot_observation(ledger, first, ACCOUNT_KEY)
    assert appended is False
    assert deduped == ledger
    assert len(deduped["observations"]) == 1

    contradiction = _snapshot(datetime(2026, 7, 13, 20, tzinfo=UTC), 10_001)
    older = _snapshot(datetime(2026, 7, 13, 19, tzinfo=UTC), 10_000)
    with pytest.raises(ValueError, match="contradicts"):
        drawdown.append_snapshot_observation(ledger, contradiction, ACCOUNT_KEY)
    with pytest.raises(ValueError, match="older"):
        drawdown.append_snapshot_observation(ledger, older, ACCOUNT_KEY)


def test_snapshot_observation_requires_trusted_positive_account_state():
    base = _snapshot(datetime(2026, 7, 13, 20, tzinfo=UTC), 10_000)
    blocked = deepcopy(base)
    blocked["normalization_blockers"] = ["capture incomplete"]
    zero = deepcopy(base)
    zero["accounts"][0]["portfolio"]["total_value"] = 0
    duplicate = deepcopy(base)
    duplicate["accounts"].append(deepcopy(duplicate["accounts"][0]))

    for unsafe in (blocked, zero, duplicate):
        with pytest.raises(ValueError):
            drawdown.append_snapshot_observation(None, unsafe, ACCOUNT_KEY)


def _raw_normalizer_bundle(generated_at: str, total_value: str = "510.00") -> dict:
    return {
        "generated_at": generated_at,
        "accounts": {
            "accounts": [
                {
                    "account_number": "FAKE123456",
                    "state": "active",
                    "agentic_allowed": True,
                    "option_level": "option_level_2",
                }
            ]
        },
        "portfolio": {
            "account_number": "FAKE123456",
            "total_value": total_value,
            "buying_power": "450.00",
            "unleveraged_buying_power": "450.00",
        },
        "equity_positions": [],
        "option_positions": [],
        "equity_orders": [],
        "option_orders": [],
    }


def test_explicit_normalizer_write_appends_chain_and_deduplicates_but_dry_run_does_not():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        raw_path = tmp_path / "raw.json"
        output_path = tmp_path / "snapshot.json"
        dry_output = tmp_path / "dry.json"
        ledger_dir = tmp_path / "ledgers"
        raw = _raw_normalizer_bundle("2026-07-13T20:00:00+00:00")
        raw_path.write_text(json.dumps(raw), encoding="utf-8")

        assert (
            normalizer.main(
                [
                    "--input",
                    str(raw_path),
                    "--output",
                    str(dry_output),
                    "--equity-ledger-dir",
                    str(ledger_dir),
                    "--dry-run",
                ]
            )
            == 0
        )
        assert not dry_output.exists()
        assert not ledger_dir.exists()

        args = [
            "--input",
            str(raw_path),
            "--output",
            str(output_path),
            "--equity-ledger-dir",
            str(ledger_dir),
        ]
        assert normalizer.main(args) == 0
        ledger_paths = list(ledger_dir.glob("*.json"))
        assert len(ledger_paths) == 1
        first_ledger = json.loads(ledger_paths[0].read_text(encoding="utf-8"))
        assert drawdown.validate_equity_ledger(first_ledger)["valid"] is True
        assert len(first_ledger["observations"]) == 1
        encoded = json.dumps(first_ledger, sort_keys=True)
        assert "FAKE123456" not in encoded

        assert normalizer.main(args) == 0
        deduped = json.loads(ledger_paths[0].read_text(encoding="utf-8"))
        assert deduped == first_ledger

        raw = _raw_normalizer_bundle("2026-07-14T20:00:00+00:00", "515.00")
        raw_path.write_text(json.dumps(raw), encoding="utf-8")
        assert normalizer.main(args) == 0
        extended = json.loads(ledger_paths[0].read_text(encoding="utf-8"))
        assert len(extended["observations"]) == 2
        assert drawdown.validate_equity_ledger(extended)["valid"] is True
        backup_path = normalizer.account_equity_ledger_backup_path(ledger_paths[0])
        backup = json.loads(backup_path.read_text(encoding="utf-8"))
        assert len(backup["observations"]) == 2
        assert backup == extended
        assert drawdown.validate_equity_ledger(backup)["valid"] is True


def test_missing_primary_with_backup_blocks_instead_of_recreating_history():
    with tempfile.TemporaryDirectory() as tmp:
        ledger_dir = Path(tmp) / "ledgers"
        first = _snapshot(datetime(2026, 7, 13, 20, tzinfo=UTC), 10_000)
        second = _snapshot(datetime(2026, 7, 14, 20, tzinfo=UTC), 10_010)
        third = _snapshot(datetime(2026, 7, 15, 20, tzinfo=UTC), 10_020)

        assert normalizer.append_account_equity_ledgers(first, ledger_dir)["status"] == "updated"
        assert normalizer.append_account_equity_ledgers(second, ledger_dir)["status"] == "updated"
        primary = normalizer.account_equity_ledger_path(ledger_dir, ACCOUNT_KEY)
        backup = normalizer.account_equity_ledger_backup_path(primary)
        assert primary.exists() and backup.exists()
        primary.unlink()

        update = normalizer.append_account_equity_ledgers(third, ledger_dir)

        assert update["status"] == "blocked"
        assert update["observations_appended"] == 0
        assert not primary.exists()
        assert backup.exists()
        assert any("primary is missing" in value for value in update["blockers"])
        assert any("restore" in value for value in update["blockers"])


def test_backup_with_newer_tail_blocks_rolled_back_primary():
    with tempfile.TemporaryDirectory() as tmp:
        ledger_dir = Path(tmp) / "ledgers"
        snapshots = [
            _snapshot(datetime(2026, 7, 13 + offset, 20, tzinfo=UTC), 10_000 + offset)
            for offset in range(4)
        ]
        normalizer.append_account_equity_ledgers(snapshots[0], ledger_dir)
        primary = normalizer.account_equity_ledger_path(ledger_dir, ACCOUNT_KEY)
        first_ledger = json.loads(primary.read_text(encoding="utf-8"))
        normalizer.append_account_equity_ledgers(snapshots[1], ledger_dir)
        normalizer.append_account_equity_ledgers(snapshots[2], ledger_dir)
        backup = normalizer.account_equity_ledger_backup_path(primary)
        backup_before = backup.read_text(encoding="utf-8")
        primary.write_text(json.dumps(first_ledger), encoding="utf-8")

        update = normalizer.append_account_equity_ledgers(snapshots[3], ledger_dir)

        assert update["status"] == "blocked"
        assert update["observations_appended"] == 0
        assert backup.read_text(encoding="utf-8") == backup_before
        assert len(json.loads(primary.read_text(encoding="utf-8"))["observations"]) == 1
        assert any("rollback" in value for value in update["blockers"])


def test_latest_backup_blocks_one_observation_primary_rollback():
    with tempfile.TemporaryDirectory() as tmp:
        ledger_dir = Path(tmp) / "ledgers"
        first = _snapshot(datetime(2026, 7, 13, 20, tzinfo=UTC), 10_000)
        second = _snapshot(datetime(2026, 7, 14, 20, tzinfo=UTC), 10_010)

        normalizer.append_account_equity_ledgers(first, ledger_dir)
        primary = normalizer.account_equity_ledger_path(ledger_dir, ACCOUNT_KEY)
        previous = json.loads(primary.read_text(encoding="utf-8"))
        normalizer.append_account_equity_ledgers(second, ledger_dir)
        backup = normalizer.account_equity_ledger_backup_path(primary)
        newest = json.loads(backup.read_text(encoding="utf-8"))
        assert len(newest["observations"]) == 2

        primary.write_text(json.dumps(previous), encoding="utf-8")

        with pytest.raises(ValueError, match="rollback"):
            normalizer.load_consistent_account_equity_ledger(primary)


def test_review_blocks_lagging_backup_until_explicit_normalization_reseals_it():
    with tempfile.TemporaryDirectory() as tmp:
        ledger_dir = Path(tmp) / "ledgers"
        first = _snapshot(datetime(2026, 7, 13, 20, tzinfo=UTC), 10_000)
        second = _snapshot(datetime(2026, 7, 14, 20, tzinfo=UTC), 10_010)

        normalizer.append_account_equity_ledgers(first, ledger_dir)
        primary = normalizer.account_equity_ledger_path(ledger_dir, ACCOUNT_KEY)
        first_ledger = json.loads(primary.read_text(encoding="utf-8"))
        normalizer.append_account_equity_ledgers(second, ledger_dir)
        backup = normalizer.account_equity_ledger_backup_path(primary)
        backup.write_text(json.dumps(first_ledger), encoding="utf-8")

        with pytest.raises(ValueError, match="lags the primary"):
            normalizer.load_consistent_account_equity_ledger(primary)

        update = normalizer.append_account_equity_ledgers(second, ledger_dir)
        assert update["status"] == "unchanged"
        assert json.loads(backup.read_text(encoding="utf-8")) == json.loads(
            primary.read_text(encoding="utf-8")
        )
        assert normalizer.load_consistent_account_equity_ledger(primary) is not None


def test_public_ledger_loader_blocks_missing_backup_after_chain_is_established():
    with tempfile.TemporaryDirectory() as tmp:
        ledger_dir = Path(tmp) / "ledgers"
        first = _snapshot(datetime(2026, 7, 13, 20, tzinfo=UTC), 10_000)
        second = _snapshot(datetime(2026, 7, 14, 20, tzinfo=UTC), 10_010)

        normalizer.append_account_equity_ledgers(first, ledger_dir)
        primary = normalizer.account_equity_ledger_path(ledger_dir, ACCOUNT_KEY)
        assert normalizer.load_consistent_account_equity_ledger(primary) is not None

        normalizer.append_account_equity_ledgers(second, ledger_dir)
        backup = normalizer.account_equity_ledger_backup_path(primary)
        assert backup.exists()
        backup.unlink()

        with pytest.raises(ValueError, match=r"\.bak is missing"):
            normalizer.load_consistent_account_equity_ledger(primary)


def test_default_ledger_dir_is_external_for_real_data_and_local_for_custom_dirs(
    monkeypatch,
):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        override = tmp_path / "durable-override"
        monkeypatch.setenv("OPTEDGE_STATE_DIR", str(override))

        assert normalizer.default_account_equity_ledger_dir(normalizer.DATA_DIR) == override
        custom_data = tmp_path / "custom-data"
        assert normalizer.default_account_equity_ledger_dir(custom_data) == (
            custom_data / normalizer.EQUITY_LEDGER_DIRNAME
        )

        monkeypatch.delenv("OPTEDGE_STATE_DIR")
        if normalizer.os.name == "nt":
            local_app_data = tmp_path / "LocalAppData"
            monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
            expected = local_app_data / "Optedge" / "risk"
        else:
            xdg_state = tmp_path / "xdg-state"
            monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state))
            expected = xdg_state / "optedge" / "risk"
        assert normalizer.default_account_equity_ledger_dir(normalizer.DATA_DIR) == expected


def test_failed_snapshot_write_cannot_create_equity_ledger(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        raw_path = tmp_path / "raw.json"
        output_path = tmp_path / "snapshot.json"
        ledger_dir = tmp_path / "ledgers"
        raw_path.write_text(
            json.dumps(_raw_normalizer_bundle("2026-07-13T20:00:00+00:00")),
            encoding="utf-8",
        )

        def fail_snapshot_write(path, payload):
            raise OSError("simulated snapshot write failure")

        monkeypatch.setattr(normalizer, "_write_json", fail_snapshot_write)
        with pytest.raises(OSError, match="simulated"):
            normalizer.main(
                [
                    "--input",
                    str(raw_path),
                    "--output",
                    str(output_path),
                    "--equity-ledger-dir",
                    str(ledger_dir),
                ]
            )

        assert not ledger_dir.exists()
