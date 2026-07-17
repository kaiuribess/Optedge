# Purpose: Verify live holdings require an exact normal-Optedge exit decision.
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from optedge.robinhood_exit_analysis import analyze_robinhood_holdings_with_optedge

NOW = datetime(2026, 7, 17, 18, 0, tzinfo=UTC)
ACCOUNT_KEY = "acct_0123456789abcdef"


def _write_inputs(tmp_path: Path, *, include_lifecycle: bool = True) -> dict:
    snapshot = {
        "schema": "optedge_robinhood_broker_snapshot_v1",
        "generated_at": NOW.isoformat(),
        "option_positions": [
            {
                "account_key": ACCOUNT_KEY,
                "instrument_id": "option-1",
                "chain_symbol": "HYG",
                "option_type": "put",
                "strike_price": 75,
                "expiration_date": "2026-12-18",
                "quantity": 1,
            }
        ],
    }
    (tmp_path / "robinhood_broker_snapshot.json").write_text(
        json.dumps(snapshot), encoding="utf-8"
    )
    lifecycle = [
        {
            "ticker": "HYG",
            "side": "put",
            "strike": 75,
            "expiry": "2026-12-18",
            "entry_time": "2026-07-16T18:00:00+00:00",
            "entry_price": 1.0,
            "stop_price": 0.65,
            "target_price": 1.30,
            "confidence": 80,
            "fused_score": 1.2,
            "research_guard_status": "allowed",
        }
    ]
    (tmp_path / "open_positions.json").write_text(
        json.dumps(lifecycle if include_lifecycle else []), encoding="utf-8"
    )
    ranked = pd.DataFrame(
        [
            {
                "ticker": "HYG",
                "side": "put",
                "strike": 75,
                "expiry": "2026-12-18",
                "confidence": 75,
                "fused_score": 1.0,
                "research_guard_status": "allowed",
            }
        ]
    )
    ranked_path = tmp_path / "ranked_options_20260717_180000.parquet"
    ranked.to_parquet(ranked_path, index=False)
    timestamp = NOW.timestamp()
    ranked_path.touch()
    os.utime(ranked_path, (timestamp, timestamp))
    return {
        "account": {"account_key": ACCOUNT_KEY},
        "holdings": [
            {
                "asset": "option",
                "symbol": "HYG",
                "option_id": "option-1",
                "expiry": "2026-12-18",
                "average_price_per_contract": 100,
                "mark": 1.45,
                "spread_fraction": 0.05,
                "broker_close_ready": True,
                "blockers": [],
            }
        ],
    }


def test_normal_optedge_hard_target_authorizes_exact_broker_exit(tmp_path: Path):
    analysis = _write_inputs(tmp_path)

    result = analyze_robinhood_holdings_with_optedge(
        analysis, data_dir=tmp_path, now=NOW
    )

    holding = result["portfolio_analysis"]["holdings"][0]
    assert result["portfolio_analysis"]["exit_decision_source"].endswith(
        "compute_exit_pressure"
    )
    assert holding["optedge_exit_action"] == "hard_target"
    assert holding["auto_exit_eligible"] is True
    assert holding["action"] == "hard_target"


def test_unmanaged_broker_contract_is_analyzed_but_held(tmp_path: Path):
    analysis = _write_inputs(tmp_path, include_lifecycle=False)

    result = analyze_robinhood_holdings_with_optedge(
        analysis, data_dir=tmp_path, now=NOW
    )

    holding = result["portfolio_analysis"]["holdings"][0]
    assert holding["action"] == "hold"
    assert holding["auto_exit_eligible"] is False
    assert any("lifecycle" in blocker for blocker in holding["blockers"])
