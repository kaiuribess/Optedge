import binascii
import json
import struct
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reports import validation_report


def _write_json(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows), encoding="utf-8")


def test_validation_counts_open_options_and_futures():
    with tempfile.TemporaryDirectory() as td:
        old_data = validation_report.DATA_DIR
        old_logs = validation_report.LOGS_DIR
        validation_report.DATA_DIR = Path(td) / "data"
        validation_report.LOGS_DIR = Path(td) / "logs"
        try:
            _write_json(
                validation_report.DATA_DIR / "open_positions.json",
                [{"ticker": "AAA", "entry_time": "2020-01-01T00:00:00+00:00"}],
            )
            _write_json(
                validation_report.DATA_DIR / "open_futures_positions.json",
                [{"symbol": "ES=F", "entry_time": "2020-01-01T00:00:00+00:00"}],
            )
            summary = validation_report.build_summary(
                scope="current_model",
                since="2099-01-01T00:00:00+00:00",
            )
            assert summary["open_positions"] == 2
            assert summary["assets"]["option"]["open_positions"] == 1
            assert summary["assets"]["futures"]["open_positions"] == 1
        finally:
            validation_report.DATA_DIR = old_data
            validation_report.LOGS_DIR = old_logs


def test_validation_current_model_keeps_old_open_positions():
    with tempfile.TemporaryDirectory() as td:
        old_data = validation_report.DATA_DIR
        old_logs = validation_report.LOGS_DIR
        validation_report.DATA_DIR = Path(td) / "data"
        validation_report.LOGS_DIR = Path(td) / "logs"
        try:
            _write_json(
                validation_report.DATA_DIR / "open_share_positions.json",
                [{"ticker": "OLD", "entry_time": "2020-01-01T00:00:00+00:00"}],
            )
            _write_json(
                validation_report.DATA_DIR / "closed_share_positions.json",
                [{"ticker": "OLD", "entry_time": "2020-01-01T00:00:00+00:00", "pnl_pct": 0.1}],
            )
            summary = validation_report.build_summary(
                scope="current_model",
                since="2099-01-01T00:00:00+00:00",
            )
            assert summary["open_positions"] == 1
            assert summary["assets"]["share"]["open_positions"] == 1
            assert summary["closed_positions"] == 0
        finally:
            validation_report.DATA_DIR = old_data
            validation_report.LOGS_DIR = old_logs


def test_position_aging_counts_open_positions_by_asset():
    with tempfile.TemporaryDirectory() as td:
        old_data = validation_report.DATA_DIR
        validation_report.DATA_DIR = Path(td) / "data"
        try:
            _write_json(
                validation_report.DATA_DIR / "open_positions.json",
                [{"ticker": "AAA", "entry_time": "2020-01-01T00:00:00+00:00"}],
            )
            _write_json(
                validation_report.DATA_DIR / "open_share_positions.json",
                [{"ticker": "BBB", "entry_time": "2020-01-02T00:00:00+00:00"}],
            )
            open_df, _ = validation_report.load_positions()
            aging = validation_report._position_aging(open_df)
            assert aging["open_count"] == 2
            assert aging["asset_breakdown"]["option"] == 1
            assert aging["asset_breakdown"]["share"] == 1
        finally:
            validation_report.DATA_DIR = old_data


def test_current_model_does_not_hide_active_signal_logs():
    with tempfile.TemporaryDirectory() as td:
        old_data = validation_report.DATA_DIR
        old_logs = validation_report.LOGS_DIR
        old_loader = validation_report.load_signal_logs
        validation_report.DATA_DIR = Path(td) / "data"
        validation_report.LOGS_DIR = Path(td) / "logs"
        try:
            validation_report.load_signal_logs = lambda: pd.DataFrame([{
                "ticker": "AAA",
                "entry_time": "2026-05-18T14:42:18+00:00",
            }])
            summary = validation_report.build_summary(
                scope="current_model",
                since="2099-01-01T00:00:00+00:00",
            )
            assert summary["total_signals"] == 1
        finally:
            validation_report.DATA_DIR = old_data
            validation_report.LOGS_DIR = old_logs
            validation_report.load_signal_logs = old_loader


def test_current_model_uses_latest_archive_reset_before_model_mtime():
    with tempfile.TemporaryDirectory() as td:
        old_root = validation_report.ROOT
        old_data = validation_report.DATA_DIR
        old_logs = validation_report.LOGS_DIR
        old_archive = validation_report.ARCHIVE_DIR
        root = Path(td)
        validation_report.ROOT = root
        validation_report.DATA_DIR = root / "data"
        validation_report.LOGS_DIR = root / "logs"
        validation_report.ARCHIVE_DIR = root / "archive"
        try:
            archive_run = validation_report.ARCHIVE_DIR / "run_20260518_074153"
            archive_run.mkdir(parents=True)
            model_file = validation_report.DATA_DIR / "model_weights.json"
            model_file.parent.mkdir(parents=True)
            model_file.write_text("{}", encoding="utf-8")
            old_time = pd.Timestamp("2026-05-18T14:41:53+00:00").timestamp()
            new_time = pd.Timestamp("2026-05-22T20:28:53+00:00").timestamp()
            import os

            os.utime(archive_run, (old_time, old_time))
            os.utime(model_file, (new_time, new_time))
            _write_json(
                validation_report.DATA_DIR / "closed_positions.json",
                [{
                    "ticker": "AAA",
                    "entry_time": "2026-05-18T14:42:18+00:00",
                    "exit_time": "2026-05-22T20:27:24+00:00",
                    "pnl_pct": 0.25,
                }],
            )
            summary = validation_report.build_summary(scope="current_model")
            assert summary["closed_positions"] == 1
            assert summary["current_model_cutoff"].startswith("2026-05-18T14:41:53")
            assert summary["validation_scope_basis"] == "latest_archive_reset"
        finally:
            validation_report.ROOT = old_root
            validation_report.DATA_DIR = old_data
            validation_report.LOGS_DIR = old_logs
            validation_report.ARCHIVE_DIR = old_archive


def test_model_file_mtime_does_not_erase_unarchived_closed_results():
    with tempfile.TemporaryDirectory() as td:
        old_root = validation_report.ROOT
        old_data = validation_report.DATA_DIR
        old_logs = validation_report.LOGS_DIR
        old_archive = validation_report.ARCHIVE_DIR
        root = Path(td)
        validation_report.ROOT = root
        validation_report.DATA_DIR = root / "data"
        validation_report.LOGS_DIR = root / "logs"
        validation_report.ARCHIVE_DIR = root / "archive"
        try:
            model_file = validation_report.DATA_DIR / "predictor_coefs.json"
            model_file.parent.mkdir(parents=True)
            model_file.write_text("{}", encoding="utf-8")
            _write_json(
                validation_report.DATA_DIR / "closed_positions.json",
                [{
                    "ticker": "AAA",
                    "entry_time": "2026-05-18T14:42:18+00:00",
                    "exit_time": "2026-05-22T20:27:24+00:00",
                    "pnl_pct": 0.25,
                }],
            )

            summary = validation_report.build_summary(scope="current_model")

            assert summary["closed_positions"] == 1
            assert summary["stale_closed_positions_excluded"] == 0
            assert summary["current_model_cutoff"] is None
            assert summary["current_experiment_cutoff"] is None
            assert summary["validation_scope_basis"] == "all_unarchived_history"
            assert any("all unarchived local outcomes" in warning for warning in summary["warnings"])
        finally:
            validation_report.ROOT = old_root
            validation_report.DATA_DIR = old_data
            validation_report.LOGS_DIR = old_logs
            validation_report.ARCHIVE_DIR = old_archive


def test_total_signals_preserves_existing_count_when_parquet_unreadable():
    with tempfile.TemporaryDirectory() as td:
        old_data = validation_report.DATA_DIR
        old_logs = validation_report.LOGS_DIR
        validation_report.DATA_DIR = Path(td) / "data"
        validation_report.LOGS_DIR = Path(td) / "logs"
        try:
            validation_report.DATA_DIR.mkdir(parents=True)
            validation_report.LOGS_DIR.mkdir(parents=True)
            (validation_report.LOGS_DIR / "signals_20260101_000000.parquet").write_bytes(b"not parquet")
            (validation_report.DATA_DIR / "validation_summary.json").write_text(
                json.dumps({"total_signals": 123}),
                encoding="utf-8",
            )
            summary = validation_report.build_summary(scope="all_time")
            assert summary["total_signals"] == 123
        finally:
            validation_report.DATA_DIR = old_data
            validation_report.LOGS_DIR = old_logs


def test_max_drawdown_includes_starting_equity():
    dd = validation_report._max_drawdown(pd.Series([-0.10, 0.05]))
    assert round(dd, 6) == -0.10


def test_validation_drawdown_uses_normalized_signal_allocation():
    closed = pd.DataFrame({
        "pnl_pct": [-1.0, 1.0],
        "pnl_pct_after_slippage": [-1.0, 1.0],
        "kelly_pct": [0.0, 0.0],
    })
    stats = validation_report._stats(closed, "pnl_pct")
    equity_returns = validation_report._equity_return_series(closed, "pnl_pct")

    assert equity_returns.tolist() == [-0.01, 0.01]
    assert round(stats["max_drawdown"], 6) == -0.01
    assert stats["max_drawdown_mode"] == "normalized_signal_allocation"
    assert stats["worst"] == -1.0


def test_summary_exposes_equity_curve_assumption():
    with tempfile.TemporaryDirectory() as td:
        old_data = validation_report.DATA_DIR
        old_logs = validation_report.LOGS_DIR
        validation_report.DATA_DIR = Path(td) / "data"
        validation_report.LOGS_DIR = Path(td) / "logs"
        try:
            _write_json(
                validation_report.DATA_DIR / "closed_positions.json",
                [{
                    "ticker": "AAA",
                    "entry_time": "2026-01-01T00:00:00+00:00",
                    "exit_time": "2026-01-02T00:00:00+00:00",
                    "pnl_pct": -1.0,
                }],
            )
            summary = validation_report.build_summary(scope="all_time")
            assert summary["equity_curve"]["mode"] == "normalized_signal_allocation"
            assert summary["equity_curve"]["default_allocation_pct"] == 0.01
            assert round(summary["overall"]["max_drawdown"], 6) == -0.01
            assert summary["overall"]["worst"] == -1.0
        finally:
            validation_report.DATA_DIR = old_data
            validation_report.LOGS_DIR = old_logs


def test_validation_keeps_churn_in_performance_but_excludes_it_from_learning():
    with tempfile.TemporaryDirectory() as td:
        old_data = validation_report.DATA_DIR
        old_logs = validation_report.LOGS_DIR
        validation_report.DATA_DIR = Path(td) / "data"
        validation_report.LOGS_DIR = Path(td) / "logs"
        try:
            _write_json(
                validation_report.DATA_DIR / "closed_positions.json",
                [
                    {
                        "position_id": "same-scan",
                        "ticker": "AAA",
                        "entry_time": "2026-01-02T15:00:00+00:00",
                        "exit_time": "2026-01-02T15:00:00+00:00",
                        "exit_reason": "dynamic_exit",
                        "pnl_pct": 0.0,
                    },
                    {
                        "position_id": "real-swing",
                        "ticker": "BBB",
                        "entry_time": "2026-01-02T15:00:00+00:00",
                        "exit_time": "2026-01-04T15:00:00+00:00",
                        "exit_reason": "hard_target",
                        "pnl_pct": 0.5,
                    },
                ],
            )
            summary = validation_report.build_summary(scope="all_time")
            option = summary["assets"]["option"]
            assert summary["closed_positions"] == 2
            assert option["closed_positions"] == 2
            assert option["learning_eligible_closed_positions"] == 1
            assert option["learning_excluded_closed_positions"] == 1
            assert option["same_scan_dynamic_exits"] == 1
            assert summary["validation_basis"] == "executable_swing_after_slippage"
            assert summary["swing_eligible_closed_positions"] == 1
            assert summary["swing_excluded_closed_positions"] == 1
            assert summary["swing_eligible_after_slippage"]["n"] == 1
            assert summary["swing_eligible_after_slippage"]["win_rate"] == 1.0
            assert any("same-scan dynamic option exit" in warning for warning in summary["warnings"])
            assert any("Executable swing sample too small" in warning for warning in summary["warnings"])
            html = validation_report.render_html(summary)
            assert "Learnable" in html
            assert "Non-executable" in html
            assert "Executable Swing Sample" in html
        finally:
            validation_report.DATA_DIR = old_data
            validation_report.LOGS_DIR = old_logs


def test_validation_headline_uses_only_executable_closed_rows():
    with tempfile.TemporaryDirectory() as td:
        old_data = validation_report.DATA_DIR
        old_logs = validation_report.LOGS_DIR
        validation_report.DATA_DIR = Path(td) / "data"
        validation_report.LOGS_DIR = Path(td) / "logs"
        try:
            base = {
                "ticker": "AAA",
                "entry_time": "2026-01-02T15:00:00+00:00",
                "exit_time": "2026-01-04T15:00:00+00:00",
                "exit_reason": "hard_target",
                "pnl_pct": 0.25,
                "trade_status": "Trade",
                "suggested_contracts": 1,
                "research_guard_status": "review",
            }
            rows = [
                {**base, "position_id": "trade"},
                {**base, "position_id": "watch", "trade_status": "Watch"},
                {**base, "position_id": "zero", "suggested_contracts": 0},
                {**base, "position_id": "blocked", "research_guard_status": "blocked"},
                {**base, "position_id": "explicit", "entry_is_actionable": False},
            ]
            _write_json(validation_report.DATA_DIR / "closed_positions.json", rows)
            summary = validation_report.build_summary(scope="all_time")
            option = summary["assets"]["option"]
            assert summary["closed_positions"] == 5
            assert summary["overall"]["n"] == 5
            assert summary["swing_eligible_closed_positions"] == 1
            assert summary["swing_eligible_after_slippage"]["n"] == 1
            assert option["execution_eligible_closed_positions"] == 1
            assert option["non_executable_closed_positions"] == 4
            assert option["excluded_non_actionable_status"] == 1
            assert option["excluded_non_positive_size"] == 1
            assert option["excluded_guard_blocked"] == 1
            assert option["excluded_explicit_not_actionable"] == 1
        finally:
            validation_report.DATA_DIR = old_data
            validation_report.LOGS_DIR = old_logs


def test_validation_headline_excludes_unresolved_expiry_outcomes_from_sample_count():
    with tempfile.TemporaryDirectory() as td:
        old_data = validation_report.DATA_DIR
        old_logs = validation_report.LOGS_DIR
        validation_report.DATA_DIR = Path(td) / "data"
        validation_report.LOGS_DIR = Path(td) / "logs"
        try:
            base = {
                "asset": "option",
                "ticker": "AAA",
                "entry_time": "2026-01-02T15:00:00+00:00",
                "exit_time": "2026-01-04T15:00:00+00:00",
                "exit_reason": "expired",
                "trade_status": "Trade",
                "suggested_contracts": 1,
                "research_guard_status": "review",
            }
            _write_json(validation_report.DATA_DIR / "closed_positions.json", [
                {**base, "position_id": "resolved", "pnl_pct": 0.25, "validation_eligible": True},
                {**base, "position_id": "unresolved", "pnl_pct": None, "validation_eligible": False},
            ])

            summary = validation_report.build_summary(scope="all_time")

            assert summary["closed_positions"] == 2
            assert summary["priced_closed_positions"] == 1
            assert summary["unresolved_closed_positions"] == 1
            assert summary["swing_eligible_closed_positions"] == 1
            assert summary["swing_eligible_after_slippage"]["n"] == 1
        finally:
            validation_report.DATA_DIR = old_data
            validation_report.LOGS_DIR = old_logs


def test_numeric_validation_ineligible_rows_never_enter_performance_analytics():
    with tempfile.TemporaryDirectory() as td:
        old_data = validation_report.DATA_DIR
        old_logs = validation_report.LOGS_DIR
        old_period_return = validation_report._period_return
        validation_report.DATA_DIR = Path(td) / "data"
        validation_report.LOGS_DIR = Path(td) / "logs"
        benchmark_windows = []
        try:
            rows = []
            for index, pnl in enumerate((0.05, 0.10, 0.15, 0.20, 0.25)):
                rows.append({
                    "asset": "option",
                    "position_id": f"eligible-{index}",
                    "ticker": "AAA",
                    "side": "call",
                    "entry_time": f"2026-01-{10 + index:02d}T15:00:00+00:00",
                    "exit_time": f"2026-01-{11 + index:02d}T15:00:00+00:00",
                    "exit_reason": "dynamic_exit" if index == 0 else "hard_target",
                    "pnl_pct": pnl,
                    "pnl_dollars": pnl * 100,
                    "dte_at_entry": 10,
                    "spread_pct": 0.04,
                    "confidence": 75,
                    "z_alpha": float(index),
                    "validation_eligible": True,
                })
            rows.append({
                "asset": "option",
                "position_id": "numeric-but-ineligible",
                "ticker": "BAD",
                "side": "put",
                "entry_time": "2000-01-01T15:00:00+00:00",
                "exit_time": "2099-01-01T15:00:00+00:00",
                "exit_reason": "hard_stop",
                "pnl_pct": -99.0,
                "pnl_dollars": -9900.0,
                "dte_at_entry": 400,
                "spread_pct": 0.90,
                "confidence": 1,
                "z_alpha": 999.0,
                "validation_eligible": False,
                "validation_exclusion_reason": "proxy_outcome",
            })
            _write_json(validation_report.DATA_DIR / "closed_positions.json", rows)

            def fake_period_return(symbol, start, end):
                benchmark_windows.append((symbol, start, end))
                return 0.01

            validation_report._period_return = fake_period_return
            summary = validation_report.build_summary(scope="all_time")
            closed = validation_report._closed_with_slippage(pd.DataFrame(rows))
            equity_returns = validation_report._equity_return_series(
                closed, "pnl_pct_after_slippage"
            )
        finally:
            validation_report.DATA_DIR = old_data
            validation_report.LOGS_DIR = old_logs
            validation_report._period_return = old_period_return

    assert summary["closed_positions"] == 6
    assert summary["raw_priced_closed_positions"] == 6
    assert summary["priced_closed_positions"] == 5
    assert summary["validation_eligible_closed_positions"] == 5
    assert summary["validation_excluded_closed_positions"] == 1
    assert summary["overall"]["n"] == 5
    assert summary["random_baseline"]["n"] == 5
    assert len(equity_returns) == 5
    assert summary["all_closure_factor_ic"][0]["n"] == 5
    assert summary["all_closure_factor_ic"][0]["ic"] > 0.99
    assert [row["bucket"] for row in summary["dte_buckets"]] == ["8-14 DTE"]
    assert [row["bucket"] for row in summary["spread_buckets"]] == ["0-5%"]
    assert [row["bucket"] for row in summary["confidence_buckets"]] == ["70-84"]
    side_rows = {row["bucket"]: row for row in summary["calls_vs_puts"]}
    assert side_rows["call"]["n"] == 5
    assert side_rows["put"]["n"] == 0
    option = summary["assets"]["option"]
    assert option["closed_positions"] == 6
    assert option["validation_excluded_closed_positions"] == 1
    assert option["pnl_dollars"] == 75.0
    exits = summary["exit_effectiveness"]["option"]
    assert exits["hard_exit_count"] == 4
    assert exits["raw_hard_exit_count"] == 5
    assert exits["validation_excluded_count"] == 1
    assert len(benchmark_windows) == 2
    assert all(str(start.date()) >= "2026-01-10" for _, start, _ in benchmark_windows)
    assert all(str(end.date()) <= "2026-01-15" for _, _, end in benchmark_windows)


def test_factor_ic_uses_independent_swing_sample_and_labels_short_history():
    with tempfile.TemporaryDirectory() as td:
        old_data = validation_report.DATA_DIR
        old_logs = validation_report.LOGS_DIR
        validation_report.DATA_DIR = Path(td) / "data"
        validation_report.LOGS_DIR = Path(td) / "logs"
        try:
            rows = []
            for index in range(6):
                rows.append({
                    "position_id": f"swing-{index}",
                    "ticker": "AAA",
                    "entry_time": f"2026-01-0{1 + index % 2}T15:00:00+00:00",
                    "exit_time": f"2026-01-0{1 + index % 2}T17:00:00+00:00",
                    "exit_reason": "hard_target",
                    "pnl_pct": float(index),
                    "z_alpha": float(index),
                })
                rows.append({
                    "position_id": f"churn-{index}",
                    "ticker": "BBB",
                    "entry_time": f"2026-01-0{1 + index % 2}T15:00:00+00:00",
                    "exit_time": f"2026-01-0{1 + index % 2}T15:00:00+00:00",
                    "exit_reason": "dynamic_exit",
                    "pnl_pct": float(index * -10),
                    "z_alpha": float(index),
                })
            _write_json(validation_report.DATA_DIR / "closed_positions.json", rows)

            summary = validation_report.build_summary(scope="all_time")

            assert summary["factor_ic_basis"] == "independent_swing_outcomes"
            assert summary["factor_ic_reliable_count"] == 0
            assert summary["factor_ic"][0]["factor"] == "alpha"
            assert summary["factor_ic"][0]["n"] == 6
            assert summary["factor_ic"][0]["trading_days"] == 2
            assert summary["factor_ic"][0]["ic"] > 0.99
            assert summary["factor_ic"][0]["reliability"] == "insufficient_history"
            assert summary["factor_ic"][0]["is_reliable"] is False
            assert summary["all_closure_factor_ic"][0]["n"] == 12
            assert summary["all_closure_factor_ic"][0]["ic"] < 0
        finally:
            validation_report.DATA_DIR = old_data
            validation_report.LOGS_DIR = old_logs


def _assert_valid_png(path: Path):
    data = path.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    offset = 8
    while offset < len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        crc_expected = struct.unpack(">I", data[offset + 8 + length:offset + 12 + length])[0]
        crc_actual = binascii.crc32(kind + payload) & 0xFFFFFFFF
        assert crc_actual == crc_expected
        offset += 12 + length
        if kind == b"IEND":
            break


def test_validation_embeds_fixed_horizon_evidence():
    with tempfile.TemporaryDirectory() as td:
        old_data = validation_report.DATA_DIR
        old_logs = validation_report.LOGS_DIR
        validation_report.DATA_DIR = Path(td) / "data"
        validation_report.LOGS_DIR = Path(td) / "logs"
        validation_report.DATA_DIR.mkdir(parents=True)
        validation_report.LOGS_DIR.mkdir(parents=True)
        try:
            fixed = {
                "headline_horizon_sessions": 10,
                "headline": {
                    "n": 0, "unique_entry_days": 0,
                },
                "headline_shadow": {
                    "n": 12, "unique_entry_days": 4, "win_rate": 0.58,
                    "win_rate_ci_low": 0.31, "win_rate_ci_high": 0.81,
                    "avg_return": 0.04, "avg_excess_vs_spy": 0.02,
                    "profit_factor": 1.4,
                },
                "by_horizon": [{
                    "horizon_sessions": 10,
                    "executable": {"n": 0, "unique_entry_days": 0},
                    "shadow_current_method": {
                        "n": 12, "unique_entry_days": 4, "win_rate": 0.58,
                        "win_rate_ci_low": 0.31, "win_rate_ci_high": 0.81,
                        "avg_return": 0.04, "avg_excess_vs_spy": 0.02,
                        "profit_factor": 1.4,
                    },
                }],
                "warnings": ["sample is still small"],
            }
            (validation_report.DATA_DIR / "fixed_horizon_summary.json").write_text(
                json.dumps(fixed), encoding="utf-8",
            )
            summary = validation_report.build_summary(scope="all_time")
            assert summary["fixed_horizon"]["headline_shadow"]["n"] == 12
            assert any("Fixed-horizon: sample is still small" in item for item in summary["warnings"])
            html = validation_report.render_html(summary)
            assert "Independent Fixed-Session Forward Test" in html
            assert "95% interval" in html
        finally:
            validation_report.DATA_DIR = old_data
            validation_report.LOGS_DIR = old_logs


def test_empty_equity_curve_writes_valid_png():
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "equity_curve.png"
        validation_report._write_equity_curve(pd.DataFrame(), out)
        _assert_valid_png(out)


def test_closed_equity_curve_writes_real_png_without_matplotlib():
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "equity_curve.png"
        closed = pd.DataFrame({
            "exit_time": [
                "2026-06-01T00:00:00+00:00",
                "2026-06-02T00:00:00+00:00",
                "2026-06-03T00:00:00+00:00",
            ],
            "pnl_pct_after_slippage": [0.10, -0.05, 0.20],
        })
        validation_report._write_equity_curve(closed, out)
        _assert_valid_png(out)
        assert out.stat().st_size > 1000


if __name__ == "__main__":
    test_validation_counts_open_options_and_futures()
    test_validation_current_model_keeps_old_open_positions()
    test_position_aging_counts_open_positions_by_asset()
    test_current_model_does_not_hide_active_signal_logs()
    test_current_model_uses_latest_archive_reset_before_model_mtime()
    test_model_file_mtime_does_not_erase_unarchived_closed_results()
    test_total_signals_preserves_existing_count_when_parquet_unreadable()
    test_max_drawdown_includes_starting_equity()
    test_validation_drawdown_uses_normalized_signal_allocation()
    test_summary_exposes_equity_curve_assumption()
    test_validation_keeps_churn_in_performance_but_excludes_it_from_learning()
    test_validation_headline_uses_only_executable_closed_rows()
    test_factor_ic_uses_independent_swing_sample_and_labels_short_history()
    test_validation_embeds_fixed_horizon_evidence()
    test_empty_equity_curve_writes_valid_png()
    test_closed_equity_curve_writes_real_png_without_matplotlib()
    print("16/16 validation report tests passed")
