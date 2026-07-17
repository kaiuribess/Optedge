# Purpose: Verify source-bound manual evidence capture for swing and LEAPS lanes.
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from backtest.fixed_horizon import prepare_signals
from optedge.evidence_capture import EvidenceCaptureError, capture_checked_finalist_evidence
from optedge.robinhood_finalist import FINALIST_CHECK_FILE, FINALIST_CHECK_SCHEMA, canonical_digest
from optedge.strategy_profile import LEAPS_SWING_PROFILE

NOW = datetime(2026, 7, 16, 20, 0, tzinfo=UTC)


def _write_sources(data_dir: Path) -> dict:
    candidate = {
        "asset": "option",
        "symbol": "AAA",
        "contract": "AAA 2027-12-17 C 100",
        "option_side": "call",
        "strike": 100,
        "expiry": "2027-12-17",
        "quantity": 1,
        "execution_profile": LEAPS_SWING_PROFILE.name,
        "strategy_evidence_lane": LEAPS_SWING_PROFILE.evidence_lane,
        "profile_policy_version": LEAPS_SWING_PROFILE.policy_version,
        "leaps_execution_ready": True,
        "after_cost_edge_pct": 0.08,
        "planned_hold_sessions": 10,
        "max_hold_sessions": 20,
    }
    queue = {
        "schema": "optedge_robinhood_agentic_options_queue_v1",
        "generated_at": NOW.isoformat(),
        "orders": [candidate],
    }
    cycle = {
        "schema": "optedge_robinhood_agentic_cycle_v1",
        "generated_at": NOW.isoformat(),
        "review_only_entry_candidates": [candidate],
    }
    check = {
        "schema": FINALIST_CHECK_SCHEMA,
        "status": "passed",
        "generated_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(seconds=120)).isoformat(),
        "market_check_passed": True,
        "candidate": {
            "symbol": "AAA",
            "option_type": "call",
            "strike": 100,
            "expiry": "2027-12-17",
            "label": "AAA 2027-12-17 C 100",
            "candidate_digest_sha256": canonical_digest(candidate),
        },
        "quote": {
            "updated_at": (NOW - timedelta(seconds=2)).isoformat(),
            "bid_price": 9.8,
            "ask_price": 10.0,
            "spread_fraction": 0.020202,
            "implied_volatility": 0.31,
            "delta": 0.65,
            "open_interest": 1200,
            "volume": 40,
        },
        "source_bindings": {
            "queue_digest_sha256": canonical_digest(queue),
            "cycle_digest_sha256": canonical_digest(cycle),
        },
        "artifact_digest_sha256": "a" * 64,
    }
    (data_dir / "robinhood_agentic_queue.json").write_text(json.dumps(queue), encoding="utf-8")
    (data_dir / "robinhood_agentic_cycle.json").write_text(json.dumps(cycle), encoding="utf-8")
    (data_dir / FINALIST_CHECK_FILE).write_text(json.dumps(check), encoding="utf-8")
    return candidate


def test_checked_leaps_finalist_becomes_current_shadow_evidence(tmp_path: Path):
    data_dir = tmp_path / "data"
    log_dir = tmp_path / "logs"
    data_dir.mkdir()
    _write_sources(data_dir)
    result = capture_checked_finalist_evidence(
        data_dir=data_dir,
        log_dir=log_dir,
        now=NOW,
    )
    assert result["captured"] is True
    assert result["execution_profile"] == "leaps_swing"
    frame = pd.read_parquet(result["signal_file"])
    prepared = prepare_signals(frame)
    assert prepared.loc[0, "execution_profile"] == "leaps_swing"
    assert bool(prepared.loc[0, "eligible_for_executable_metrics"]) is False
    assert bool(prepared.loc[0, "eligible_for_shadow_metrics"]) is True
    assert frame.loc[0, "mid"] == 10.0
    assert frame.loc[0, "quote_quality"] == "live_broker"
    assert result["broker_reads_performed"] == 0
    assert result["broker_writes_authorized"] == 0

    repeated = capture_checked_finalist_evidence(
        data_dir=data_dir,
        log_dir=log_dir,
        now=NOW,
    )
    assert repeated["captured"] is False
    assert repeated["idempotent"] is True
    assert len(list(log_dir.glob("signals_manual_evidence_*.parquet"))) == 1


def test_changed_queue_blocks_without_writing_signal(tmp_path: Path):
    data_dir = tmp_path / "data"
    log_dir = tmp_path / "logs"
    data_dir.mkdir()
    _write_sources(data_dir)
    queue_path = data_dir / "robinhood_agentic_queue.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    queue["orders"][0]["quantity"] = 2
    queue_path.write_text(json.dumps(queue), encoding="utf-8")
    with pytest.raises(EvidenceCaptureError) as caught:
        capture_checked_finalist_evidence(data_dir=data_dir, log_dir=log_dir, now=NOW)
    assert caught.value.code == "finalist_source_changed"
    assert not log_dir.exists()


def test_expired_check_blocks_capture(tmp_path: Path):
    data_dir = tmp_path / "data"
    log_dir = tmp_path / "logs"
    data_dir.mkdir()
    _write_sources(data_dir)
    with pytest.raises(EvidenceCaptureError) as caught:
        capture_checked_finalist_evidence(
            data_dir=data_dir,
            log_dir=log_dir,
            now=NOW + timedelta(minutes=5),
        )
    assert caught.value.code == "fresh_finalist_check_required"
    assert not log_dir.exists()
