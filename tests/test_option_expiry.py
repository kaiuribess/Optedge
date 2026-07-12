# Purpose: Test option expiry with verified settlement evidence.
"""Deterministic tests for expiration-session option valuation."""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.option_expiry import (  # noqa: E402, I001
    expiry_exit_time, resolve_expiry_valuations, valuation_key,
)


ASOF = datetime(2026, 6, 22, tzinfo=UTC)


def _position(**overrides):
    row = {
        "ticker": "AAPL",
        "side": "call",
        "strike": 200.0,
        "expiry": "2026-06-18",
        "entry_price": 2.0,
        "underlying_type": "equity",
        "contract_multiplier": 100,
        "deliverable": "100 shares",
        "settlement_style": "pm_physical",
    }
    row.update(overrides)
    return row


def _history(rows, source="test_history", price_basis="unadjusted_close"):
    frame = pd.DataFrame(
        {"Close": [price for _, price in rows]},
        index=pd.to_datetime([day for day, _ in rows], utc=True),
    )
    frame.attrs["history_source"] = source
    frame.attrs["history_quality"] = "observed_test"
    if price_basis:
        frame.attrs["price_basis"] = price_basis
    return frame


def test_expiry_intrinsic_uses_underlying_close_on_expiration_session():
    position = _position()

    def fetcher(*_args, **_kwargs):
        return _history([("2026-06-17", 203.0), ("2026-06-18", 205.0)])

    with tempfile.TemporaryDirectory() as td:
        values = resolve_expiry_valuations(
            [position], asof=ASOF, history_fetcher=fetcher,
            option_history_path=Path(td) / "missing.json",
        )
    row = values[valuation_key(position)]
    assert row["option_value"] == 5.0
    assert row["underlying_price"] == 205.0
    assert row["underlying_price_date"] == "2026-06-18"
    assert row["underlying_session_provenance"] == "expiry_exchange_session"
    assert row["price_source"] == "intrinsic_proxy_from_underlying_expiry_close"
    assert row["validation_eligible"] is True


def test_expiry_intrinsic_allows_prior_session_for_market_holiday():
    position = _position(expiry="2026-06-19", side="put", strike=210.0)

    def fetcher(*_args, **_kwargs):
        return _history([("2026-06-17", 204.0), ("2026-06-18", 205.0)])

    with tempfile.TemporaryDirectory() as td:
        values = resolve_expiry_valuations(
            [position], asof=ASOF, history_fetcher=fetcher,
            option_history_path=Path(td) / "missing.json",
        )
    row = values[valuation_key(position)]
    assert row["option_value"] == 5.0
    assert row["underlying_session_gap_days"] == 1
    assert row["underlying_session_provenance"] == "recognized_prior_exchange_session"
    assert row["validation_eligible"] is True
    assert row["validation_exclusion_reason"] is None


def test_expiry_intrinsic_stale_gap_remains_telemetry_but_not_validation():
    position = _position()

    def fetcher(*_args, **_kwargs):
        # June 18 was a normal Thursday session, so June 17 is merely a data gap.
        return _history([("2026-06-16", 202.0), ("2026-06-17", 203.0)])

    with tempfile.TemporaryDirectory() as td:
        values = resolve_expiry_valuations(
            [position], asof=ASOF, history_fetcher=fetcher,
            option_history_path=Path(td) / "missing.json",
        )
    row = values[valuation_key(position)]
    assert row["option_value"] == 3.0
    assert row["price_source"] == "intrinsic_proxy_from_stale_underlying_close"
    assert row["underlying_price_date"] == "2026-06-17"
    assert row["underlying_session_gap_days"] == 1
    assert row["underlying_session_provenance"] == "stale_gap_before_expiry"
    assert row["validation_eligible"] is False
    assert (
        row["validation_exclusion_reason"]
        == "underlying_close_not_expiry_or_recognized_prior_session"
    )


def test_exact_non_interpolated_option_bar_is_fallback_when_underlying_missing():
    position = _position()
    with tempfile.TemporaryDirectory() as td:
        snapshot = Path(td) / "option_history.json"
        snapshot.write_text(json.dumps({
            "schema": "optedge_robinhood_option_history_snapshot_v1",
            "contracts": [{
                "symbol": "AAPL", "expiry": "2026-06-18", "side": "call",
                "strike": 200.0, "instrument_id": "option-aapl",
                "bars": [{
                    "begins_at": "2026-06-18T00:00:00Z",
                    "close_price": 1.25, "interpolated": False,
                }],
            }],
        }), encoding="utf-8")
        values = resolve_expiry_valuations(
            [position], asof=ASOF, history_fetcher=lambda *_args, **_kwargs: pd.DataFrame(),
            option_history_path=snapshot,
        )
    row = values[valuation_key(position)]
    assert row["option_value"] == 1.25
    assert row["price_source"] == "broker_option_trade_bar_on_expiry"
    assert row["option_instrument_id"] == "option-aapl"
    assert row["option_bar_date"] == "2026-06-18"
    assert row["validation_eligible"] is False
    assert row["validation_exclusion_reason"] == "option_trade_bar_is_not_expiration_settlement"


def test_interpolated_option_bar_does_not_create_expiry_pnl():
    position = _position()
    with tempfile.TemporaryDirectory() as td:
        snapshot = Path(td) / "option_history.json"
        snapshot.write_text(json.dumps({
            "contracts": [{
                "symbol": "AAPL", "expiry": "2026-06-18", "side": "call",
                "strike": 200.0,
                "bars": [{
                    "begins_at": "2026-06-18T00:00:00Z",
                    "close_price": 1.25, "interpolated": True,
                }],
            }],
        }), encoding="utf-8")
        values = resolve_expiry_valuations(
            [position], asof=ASOF, history_fetcher=lambda *_args, **_kwargs: pd.DataFrame(),
            option_history_path=snapshot,
        )
    row = values[valuation_key(position)]
    assert row["option_value"] is None
    assert row["validation_eligible"] is False
    assert row["price_source"] == "unresolved_no_expiry_market_data"


def test_am_settled_index_never_uses_four_pm_close_for_validation():
    position = _position(
        ticker="SPX",
        underlying_type="equity",  # Known index roots override unsafe upstream labels.
        settlement_style="am_cash",
        strike=5000.0,
        deliverable="cash settlement",
    )

    def fetcher(*_args, **_kwargs):
        return _history([("2026-06-18", 5100.0)])

    with tempfile.TemporaryDirectory() as td:
        values = resolve_expiry_valuations(
            [position], asof=ASOF, history_fetcher=fetcher,
            option_history_path=Path(td) / "missing.json",
        )
    row = values[valuation_key(position)]
    assert row["option_value"] == 100.0
    assert row["underlying_type"] == "index"
    assert row["settlement_style"] == "am_cash"
    assert row["validation_eligible"] is False
    assert "official_settlement_value_required" in row["validation_exclusion_reason"]
    assert "am_settled_contract_requires" in row["validation_exclusion_reason"]
    assert expiry_exit_time(position).isoformat() == "2026-06-18T13:30:00+00:00"


def test_official_index_settlement_value_can_be_validation_eligible():
    position = _position(
        ticker="SPX",
        underlying_type="index",
        settlement_style="am_cash",
        strike=5000.0,
        deliverable="cash settlement",
        official_settlement_value=5050.0,
        official_settlement_source="Cboe official SET value",
        official_settlement_source_id="CBOE-SET-SPX-20260618",
        official_settlement_published_at="2026-06-18T13:45:00+00:00",
        official_settlement_verified=True,
    )
    with tempfile.TemporaryDirectory() as td:
        values = resolve_expiry_valuations(
            [position], asof=ASOF, history_fetcher=lambda *_args, **_kwargs: pd.DataFrame(),
            option_history_path=Path(td) / "missing.json",
        )
    row = values[valuation_key(position)]
    assert row["option_value"] == 50.0
    assert row["price_source"] == "intrinsic_from_official_settlement_value"
    assert row["settlement_is_proxy"] is False
    assert row["official_settlement_captured"] is True
    assert row["official_settlement_source_id"] == "CBOE-SET-SPX-20260618"
    assert row["official_settlement_verified"] is True
    assert row["validation_eligible"] is True


def test_official_settlement_without_explicit_verified_true_remains_telemetry():
    position = _position(
        ticker="SPX",
        underlying_type="index",
        settlement_style="am_cash",
        strike=5000.0,
        deliverable="cash settlement",
        official_settlement_value=5050.0,
        official_settlement_source="Cboe official SET value",
    )
    with tempfile.TemporaryDirectory() as td:
        values = resolve_expiry_valuations(
            [position], asof=ASOF, history_fetcher=lambda *_args, **_kwargs: pd.DataFrame(),
            option_history_path=Path(td) / "missing.json",
        )
    row = values[valuation_key(position)]
    assert row["option_value"] == 50.0
    assert row["price_source"] == "intrinsic_from_official_settlement_value"
    assert row["official_settlement_verified"] is False
    assert row["official_settlement_captured"] is False
    assert row["validation_eligible"] is False
    assert "verification" in row["validation_exclusion_reason"]


def test_adjusted_or_unverified_close_contracts_remain_telemetry_only():
    cases = [
        (
            _position(
                contract_multiplier=50,
                deliverable="adjusted 50 shares after corporate action",
                corporate_action_ambiguous=True,
            ),
            _history([("2026-06-18", 205.0)]),
            "contract_multiplier_is_not_verified_standard_100x",
        ),
        (
            _position(),
            _history([("2026-06-18", 205.0)], price_basis=None),
            "underlying_history_close_is_not_verified_raw_unadjusted",
        ),
        (
            _position(underlying_type=""),
            _history([("2026-06-18", 205.0)]),
            "official_settlement_value_required_for_index_or_unknown_underlying",
        ),
        (
            _position(contract_multiplier=None, deliverable=""),
            _history([("2026-06-18", 205.0)]),
            "contract_multiplier_is_not_verified_standard_100x",
        ),
    ]
    for position, history, expected_reason in cases:
        with tempfile.TemporaryDirectory() as td:
            values = resolve_expiry_valuations(
                [position], asof=ASOF,
                history_fetcher=lambda *_args, _history=history, **_kwargs: _history,
                option_history_path=Path(td) / "missing.json",
            )
        row = values[valuation_key(position)]
        assert row["option_value"] == 5.0
        assert row["price_source"] == "intrinsic_proxy_from_underlying_expiry_close"
        assert row["validation_eligible"] is False
        assert expected_reason in row["validation_exclusion_reason"]
        assert "contract_multiplier" in row
        assert "deliverable" in row


def test_date_only_expiry_exit_time_is_new_york_session_close():
    assert expiry_exit_time(_position()).isoformat() == "2026-06-18T20:00:00+00:00"


if __name__ == "__main__":
    tests = [
        value for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
    print(f"option expiry tests passed ({len(tests)})")
