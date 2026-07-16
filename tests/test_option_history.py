# Purpose: Test observed option history and proxy boundaries.
"""Tests for the read-only Robinhood option-history validation bridge."""

from __future__ import annotations

import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest import fixed_horizon, option_history  # noqa: E402, I001


ASOF = datetime(2026, 2, 15, tzinfo=UTC)


def _underlying_history() -> pd.DataFrame:
    index = pd.date_range("2026-01-02", periods=8, freq="B", tz="UTC")
    frame = pd.DataFrame({"Close": [100, 101, 102, 103, 104, 105, 106, 107]}, index=index)
    frame.attrs["history_source"] = "test"
    frame.attrs["history_quality"] = "test"
    return fixed_horizon._normalize_history(frame, ASOF)


def _signal() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "asset": "option",
                "ticker": "AAA",
                "contract": "AAA 2026-03-20 C 100",
                "entry_time": "2026-01-02T16:00:00Z",
                "mid": 2.0,
                "spot": 100.0,
                "strike": 100.0,
                "side": "call",
                "expiry": "2026-03-20",
                "iv_market": 0.30,
                "is_buy": True,
                "trade_status": "Trade",
                "is_actionable": True,
                "suggested_contracts": 1,
                "buyer_edge_pct": 0.08,
                "pricing_edge_ok": True,
                "strategy_qualified_pre_guard": True,
                "pre_guard_suggested_contracts": 1,
            }
        ]
    )


def _snapshot(*, interpolated: bool = False, close: float = 3.0) -> dict:
    return {
        "schema": option_history.SNAPSHOT_SCHEMA,
        "contracts": [
            {
                "symbol": "AAA",
                "expiry": "2026-03-20",
                "side": "call",
                "strike": 100,
                "instrument_id": "option-aaa",
                "occ_symbol": "AAA   260320C00100000",
                "bars": [
                    {
                        "begins_at": "2026-01-05T00:00:00Z",
                        "open_price": "2.50",
                        "high_price": "3.10",
                        "low_price": "2.40",
                        "close_price": str(close),
                        "session": "reg",
                        "interpolated": interpolated,
                    }
                ],
            }
        ],
    }


def test_contract_normalization_and_occ_parser():
    parsed = option_history.parse_occ_symbol("GOOG  260717C00450000")
    assert parsed == {
        "symbol": "GOOG",
        "expiry": "2026-07-17",
        "side": "call",
        "strike": 450.0,
    }
    assert option_history.contract_key("goog", "2026-07-17", "C", 450) == (
        "GOOG|2026-07-17|call|450"
    )
    assert (
        option_history.contract_key_from_row(
            pd.Series(
                {
                    "ticker": pd.NA,
                    "symbol": "GOOG",
                    "expiry": "2026-07-17",
                    "side": "call",
                    "strike": 450,
                }
            )
        )
        == "GOOG|2026-07-17|call|450"
    )


def test_snapshot_merge_preserves_observed_bar_over_interpolated_refresh():
    first = _snapshot(close=2.75)
    second = _snapshot(interpolated=True, close=3.25)
    merged = option_history.merge_snapshot_payload(first, second, asof=ASOF)
    assert len(merged["contracts"]) == 1
    assert len(merged["contracts"][0]["bars"]) == 1
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "snapshot.json"
        option_history._write_json(path, merged)
        histories = option_history.load_option_histories(path)
        key = "AAA|2026-03-20|call|100"
        observed = option_history.observed_option_close(
            histories[key],
            datetime(2026, 1, 5).date(),
        )
        assert observed is not None
        assert observed[0] == 2.75


def test_interpolated_only_bar_is_not_observed_evidence():
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "snapshot.json"
        option_history._write_json(path, _snapshot(interpolated=True))
        histories = option_history.load_option_histories(path)
        key = "AAA|2026-03-20|call|100"
        assert (
            option_history.observed_option_close(
                histories[key],
                datetime(2026, 1, 5).date(),
            )
            is None
        )


def test_request_queue_is_bounded_and_skips_complete_contract_cache():
    signals = pd.concat(
        [
            _signal(),
            _signal().assign(
                ticker="BBB",
                contract="BBB 2026-03-20 C 50",
                strike=50,
                strategy_qualified_pre_guard=False,
                is_actionable=False,
                trade_status="Watch",
            ),
        ],
        ignore_index=True,
    )
    snapshot = _snapshot()
    snapshot["contracts"][0]["bars"] = [
        {"begins_at": "2026-01-02T00:00:00Z", "close_price": "2.0"},
        {"begins_at": "2026-02-13T00:00:00Z", "close_price": "2.2"},
    ]
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "snapshot.json"
        option_history._write_json(path, snapshot)
        packet = option_history.build_requests(
            signals,
            snapshot_path=path,
            asof=ASOF,
            max_requests=1,
        )
        assert packet["request_count"] == 1
        assert packet["requests"][0]["symbol"] == "BBB"
        assert packet["read_only"] is True


def test_fixed_horizon_prefers_exact_non_interpolated_option_bar():
    normalized = option_history.merge_snapshot_payload({}, _snapshot(), asof=ASOF)
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "snapshot.json"
        option_history._write_json(path, normalized)
        option_histories = option_history.load_option_histories(path)
        history = _underlying_history()
        outcomes, _, _ = fixed_horizon.evaluate_fixed_horizons(
            _signal(),
            {"AAA": history, "SPY": history, "QQQ": history},
            horizons=(1,),
            asof=ASOF,
            option_histories=option_histories,
        )
        row = outcomes.iloc[0]
        assert row["valuation_method"] == "robinhood_observed_option_trade_close"
        assert row["outcome_quality"] == "broker_market_observed"
        assert row["option_instrument_id"] == "option-aaa"
        assert row["target_price"] == 3.0
        assert row["pnl_pct"] == 0.5


def test_existing_proxy_is_upgraded_when_observed_bar_arrives():
    history = _underlying_history()
    proxy, _, _ = fixed_horizon.evaluate_fixed_horizons(
        _signal(),
        {"AAA": history, "SPY": history, "QQQ": history},
        horizons=(1,),
        asof=ASOF,
    )
    assert proxy.iloc[0]["outcome_quality"] == "modeled_option_proxy"
    normalized = option_history.merge_snapshot_payload({}, _snapshot(), asof=ASOF)
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "snapshot.json"
        option_history._write_json(path, normalized)
        observed = option_history.load_option_histories(path)
        prepared = fixed_horizon.prepare_signals(_signal())
        candidates, _ = fixed_horizon._unresolved_candidates(
            prepared,
            proxy,
            (1,),
            ASOF,
            observed,
        )
        assert len(candidates) == 1
        upgraded, _, _ = fixed_horizon.evaluate_fixed_horizons(
            candidates,
            {"AAA": history, "SPY": history, "QQQ": history},
            horizons=(1,),
            asof=ASOF,
            existing=proxy,
            option_histories=observed,
        )
        assert len(upgraded) == 1
        assert upgraded.iloc[0]["outcome_quality"] == "broker_market_observed"


if __name__ == "__main__":
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
    print(f"option history tests passed ({len(tests)})")
