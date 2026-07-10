"""Deterministic tests for fixed-session forward validation."""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest import fixed_horizon, forward, track  # noqa: E402, I001


ASOF = datetime(2026, 2, 15, tzinfo=UTC)


def _history(start: str, closes: list[float], source: str = "test") -> pd.DataFrame:
    index = pd.date_range(start, periods=len(closes), freq="B", tz="UTC")
    frame = pd.DataFrame({
        "Open": closes,
        "High": [value * 1.01 for value in closes],
        "Low": [value * 0.99 for value in closes],
        "Close": closes,
        "Volume": [1000] * len(closes),
    }, index=index)
    frame.attrs["history_source"] = source
    frame.attrs["history_quality"] = "test"
    return fixed_horizon._normalize_history(frame, ASOF)


def _benchmarks() -> dict[str, pd.DataFrame]:
    return {
        "SPY": _history("2026-01-02", [100, 101, 102, 103, 104, 105]),
        "QQQ": _history("2026-01-02", [200, 202, 204, 206, 208, 210]),
    }


def test_fixed_horizon_uses_completed_sessions_and_one_daily_thesis():
    signals = pd.DataFrame([
        {
            "asset": "share", "ticker": "AAA", "entry_time": "2026-01-02T16:00:00Z",
            "entry_price": 10.0, "trade_status": "Trade", "is_actionable": True,
            "suggested_dollars": 500.0, "confidence": 80,
        },
        {
            "asset": "share", "ticker": "AAA", "entry_time": "2026-01-02T18:00:00Z",
            "entry_price": 10.2, "trade_status": "Trade", "is_actionable": True,
            "suggested_dollars": 500.0, "confidence": 82,
        },
    ])
    histories = {**_benchmarks(), "AAA": _history("2026-01-02", [10, 11, 12, 13, 14, 15])}
    outcomes, pending, excluded = fixed_horizon.evaluate_fixed_horizons(
        signals, histories, horizons=(1, 3), asof=ASOF,
    )
    assert len(outcomes) == 4
    assert outcomes["is_independent"].sum() == 2
    first = outcomes[
        outcomes["is_independent"] & (outcomes["horizon_sessions"] == 1)
    ].iloc[0]
    assert first["target_date"] == "2026-01-05"
    assert np.isclose(first["pnl_pct_after_slippage"], 0.10 - 0.002)
    assert np.isclose(first["spy_return_pct"], 0.01)
    assert pending == {"1": 0, "3": 0}
    assert excluded == {}

    summary = fixed_horizon.build_summary(
        outcomes, signals, pending, excluded, horizons=(1, 3), asof=ASOF,
    )
    one_day = summary["by_horizon"][0]["executable"]
    assert one_day["n"] == 1
    assert one_day["unique_entry_days"] == 1
    assert np.isclose(one_day["avg_excess_vs_spy"], 0.088)


def test_option_outcomes_are_labeled_proxies_and_expiry_is_not_stretched():
    signals = pd.DataFrame([
        {
            "asset": "option", "ticker": "OPT", "entry_time": "2026-01-02T17:00:00Z",
            "mid": 5.0, "spot": 100.0, "strike": 100.0, "side": "call",
            "expiry": "2026-03-20", "iv_market": 0.30, "is_buy": True,
            "trade_status": "Trade", "is_actionable": True, "suggested_contracts": 1,
            "buyer_edge_pct": 0.08, "pricing_edge_ok": True,
            "strategy_qualified_pre_guard": True, "pre_guard_suggested_contracts": 1,
        },
        {
            "asset": "option", "ticker": "OPT", "entry_time": "2026-01-05T17:00:00Z",
            "mid": 2.0, "spot": 101.0, "strike": 100.0, "side": "call",
            "expiry": "2026-01-06", "iv_market": 0.30, "is_buy": True,
            "trade_status": "Trade", "is_actionable": True, "suggested_contracts": 1,
        },
    ])
    histories = {**_benchmarks(), "OPT": _history("2026-01-02", [100, 105, 106, 107, 108, 109])}
    outcomes, _, excluded = fixed_horizon.evaluate_fixed_horizons(
        signals, histories, horizons=(1, 3), asof=ASOF,
    )
    proxy = outcomes[outcomes["entry_date"] == "2026-01-02"]
    assert not proxy.empty
    assert set(proxy["valuation_method"]) == {"bs_constant_entry_iv_proxy"}
    assert set(proxy["outcome_quality"]) == {"modeled_option_proxy"}
    assert proxy["eligible_for_shadow_metrics"].all()
    assert proxy["pnl_pct"].notna().all()
    assert excluded == {}
    expiry_rows = outcomes[
        outcomes.get("resolution_reason", pd.Series("", index=outcomes.index))
        == "expiry_before_horizon"
    ]
    assert len(expiry_rows) == 1
    assert not bool(expiry_rows.iloc[0]["is_scored"])


def test_execution_eligibility_is_strict_and_transparent():
    rows = pd.DataFrame([
        {
            "asset": "option", "ticker": "AAA", "entry_time": "2026-01-02T16:00:00Z",
            "trade_status": "Watch", "suggested_contracts": 1,
        },
        {
            "asset": "option", "ticker": "BBB", "entry_time": "2026-01-02T16:00:00Z",
            "trade_status": "Trade", "suggested_contracts": 1,
            "research_guard_status": "blocked",
        },
        {
            "asset": "futures", "symbol": "ES=F", "entry_time": "2026-01-02T16:00:00Z",
            "trade_status": "Trade", "is_actionable": True, "suggested_contracts": 0,
        },
        {
            "asset": "share", "ticker": "CCC", "entry_time": "2026-01-02T16:00:00Z",
            "trade_status": "Trade", "is_actionable": True, "suggested_dollars": 250,
        },
        {
            "asset": "option", "ticker": "DDD", "entry_time": "2026-01-02T16:00:00Z",
            "trade_status": "Trade", "is_actionable": True, "suggested_contracts": 1,
        },
        {
            "asset": "option", "ticker": "EEE", "entry_time": "2026-01-02T16:00:00Z",
            "trade_status": "Trade", "is_actionable": True, "suggested_contracts": 1,
            "buyer_edge_pct": 0.05, "pricing_edge_ok": True,
            "strategy_qualified_pre_guard": True, "pre_guard_suggested_contracts": 1,
        },
    ])
    prepared = fixed_horizon.prepare_signals(rows)
    status = dict(zip(prepared["symbol"], prepared["execution_eligibility_reason"], strict=True))
    assert status == {
        "AAA": "status_watch",
        "BBB": "research_guard_blocked",
        "ES=F": "non_positive_contracts",
        "CCC": "passed",
        "DDD": "missing_directional_buyer_edge",
        "EEE": "passed",
    }
    eligible = dict(zip(
        prepared["symbol"], prepared["eligible_for_executable_metrics"], strict=True,
    ))
    assert bool(eligible["CCC"])
    assert bool(eligible["EEE"])
    assert not any(bool(eligible[symbol]) for symbol in ("AAA", "BBB", "ES=F", "DDD"))
    shadow = dict(zip(
        prepared["symbol"], prepared["eligible_for_shadow_metrics"], strict=True,
    ))
    assert bool(shadow["EEE"])
    assert not bool(shadow["CCC"])


def test_futures_observed_close_tracks_direction_points_and_dollars():
    signals = pd.DataFrame([{
        "asset": "futures", "symbol": "ES=F", "entry_time": "2026-01-02T16:00:00Z",
        "entry_price": 100.0, "direction": "long", "point_value": 5.0,
        "trade_status": "Trade", "is_actionable": True, "suggested_contracts": 2,
    }])
    histories = {**_benchmarks(), "ES=F": _history("2026-01-02", [100, 105, 106, 107])}
    outcomes, _, _ = fixed_horizon.evaluate_fixed_horizons(
        signals, histories, horizons=(1,), asof=ASOF,
    )
    row = outcomes.iloc[0]
    assert row["valuation_method"] == "observed_futures_close"
    assert row["outcome_quality"] == "market_observed"
    assert np.isclose(row["pnl_pct_after_slippage"], 0.05 - 0.001)
    assert np.isclose(row["pnl_points"], 5.0)
    assert np.isclose(row["pnl_dollars"], 50.0)


def test_run_is_incremental_and_writes_machine_readable_artifacts():
    signals = pd.DataFrame([{
        "asset": "share", "ticker": "AAA", "entry_time": "2026-01-02T16:00:00Z",
        "entry_price": 10.0, "trade_status": "Trade", "is_actionable": True,
        "suggested_dollars": 500.0,
    }])
    histories = {
        **_benchmarks(),
        "AAA": _history("2026-01-02", [10, 11, 12, 13]),
    }
    calls: list[str] = []

    def loader(symbol: str, _period: str) -> pd.DataFrame:
        calls.append(symbol)
        return histories.get(symbol, pd.DataFrame())

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        outcomes_path = root / "fixed_horizon_outcomes.parquet"
        summary_path = root / "fixed_horizon_summary.json"
        first = fixed_horizon.run_fixed_horizon_test(
            signals,
            horizons=(1, 3),
            asof=ASOF,
            history_loader=loader,
            outcomes_path=outcomes_path,
            summary_path=summary_path,
            max_workers=2,
        )
        first_call_count = len(calls)
        second = fixed_horizon.run_fixed_horizon_test(
            signals,
            horizons=(1, 3),
            asof=ASOF,
            history_loader=loader,
            outcomes_path=outcomes_path,
            summary_path=summary_path,
            max_workers=2,
        )
        assert first["new_outcomes"] == 2
        assert second["new_outcomes"] == 0
        assert len(calls) == first_call_count
        assert len(second["outcomes"]) == 2
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
        assert payload["basis"] == "independent_fixed_session_outcomes_after_slippage"
        assert payload["outcome_quality"] == {"market_observed": 2}


def test_forward_loader_combines_all_asset_logs_for_standalone_mode():
    with tempfile.TemporaryDirectory() as temp_dir:
        old_logs = forward.LOGS_DIR
        forward.LOGS_DIR = Path(temp_dir)
        try:
            pd.DataFrame([{
                "ticker": "OPT", "contract": "OPT C 100", "side": "call",
                "entry_time": "2026-01-02T16:00:00Z",
            }]).to_parquet(forward.LOGS_DIR / "signals_20260102_160000.parquet")
            pd.DataFrame([{
                "ticker": "SHR", "entry_time": "2026-01-02T16:00:00Z",
            }]).to_parquet(forward.LOGS_DIR / "shares_signals_20260102_160000.parquet")
            pd.DataFrame([{
                "symbol": "ES=F", "entry_time": "2026-01-02T16:00:00Z",
            }]).to_parquet(forward.LOGS_DIR / "futures_signals_20260102_160000.parquet")
            combined = forward._load_all_logs()
            assert set(combined["asset"]) == {"option", "share", "futures"}
            assert len(combined) == 3
        finally:
            forward.LOGS_DIR = old_logs


def test_current_mark_prefetch_fetches_each_symbol_once():
    calls: list[str] = []
    original = forward.data_provider.get_history

    def fake_history(symbol: str, period: str = "1y") -> pd.DataFrame:
        calls.append(f"{symbol}:{period}")
        return _history("2026-01-02", [10, 11])

    forward.data_provider.get_history = fake_history
    try:
        histories = forward._prefetch_current_histories(
            pd.DataFrame({"ticker": ["AAA", "AAA", "BBB"]}),
            pd.DataFrame({"ticker": ["AAA"]}),
            pd.DataFrame({"symbol": ["ES=F", "ES=F"], "etf": ["SPY", "SPY"]}),
            max_workers=4,
        )
        assert set(histories) == {"AAA", "BBB", "ES=F", "SPY"}
        assert len(calls) == 4
    finally:
        forward.data_provider.get_history = original


def test_signal_logs_preserve_pre_guard_shadow_fields():
    with tempfile.TemporaryDirectory() as temp_dir:
        old_log_dir = track.LOG_DIR
        track.LOG_DIR = Path(temp_dir)
        try:
            path = track.log_signals(pd.DataFrame([{
                "ticker": "AAA", "contract": "AAA 2026-06-19 C 100",
                "side": "call", "strike": 100, "expiry": "2026-06-19",
                "mid": 2.0, "buyer_edge_pct": 0.08, "pricing_edge_ok": True,
                "trade_status": "Watch", "is_actionable": False,
                "suggested_contracts": 0,
                "strategy_qualified_pre_guard": True,
                "pre_guard_trade_status": "Trade",
                "pre_guard_is_actionable": True,
                "pre_guard_suggested_contracts": 1,
            }]), datetime(2026, 1, 2, 16, 0, tzinfo=UTC))
            logged = pd.read_parquet(path)
            assert bool(logged.loc[0, "strategy_qualified_pre_guard"])
            assert logged.loc[0, "pre_guard_trade_status"] == "Trade"
            assert logged.loc[0, "pre_guard_suggested_contracts"] == 1
            assert logged.loc[0, "suggested_contracts"] == 0
        finally:
            track.LOG_DIR = old_log_dir


if __name__ == "__main__":
    tests = [
        value for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
    print(f"fixed horizon tests passed ({len(tests)})")
