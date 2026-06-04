import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.local_cockpit import (
    add_watchlist_query, artifact_path, build_opportunities, build_paper_candidates,
    build_action_queue, build_data_health, build_performance_summary, build_positions,
    build_risk_summary, build_summary, build_symbol_suggestions,
    load_watchlist, remove_watchlist_entry, render_cockpit_html, run_watchlist_scans,
)


def test_cockpit_summary_counts_open_positions():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        (data_dir / "open_positions.json").write_text(json.dumps([{"ticker": "AAPL"}]))
        (data_dir / "open_share_positions.json").write_text(json.dumps([{"ticker": "NVDA"}]))
        (data_dir / "open_futures_positions.json").write_text(json.dumps([
            {"symbol": "CL=F"},
            {"symbol": "NG=F"},
        ]))
        summary = build_summary(data_dir)
        assert summary["open_counts"] == {"options": 1, "shares": 1, "futures": 2}
        assert summary["total_open"] == 4


def test_cockpit_artifact_path_finds_latest_dashboard():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        old = data_dir / "dashboard_20260101_000000.html"
        new = data_dir / "dashboard_20260102_000000.html"
        old.write_text("old")
        new.write_text("new")
        assert artifact_path("latest-dashboard", data_dir) == new


def test_cockpit_html_contains_lookup_controls():
    html = render_cockpit_html()
    assert "Optedge Local Cockpit" in html
    assert "Data health" in html
    assert "Action queue" in html
    assert "/api/action-queue" in html
    assert "queue-action-btn" in html
    assert "routeQueueAction" in html
    assert "Portfolio risk" in html
    assert "/api/risk-summary" in html
    assert "riskSummaryHtml" in html
    assert "Performance" in html
    assert "/api/performance-summary" in html
    assert "performanceSummaryHtml" in html
    assert "Opportunity explorer" in html
    assert "/api/opportunities" in html
    assert "External paper candidates" in html
    assert "/api/paper-candidates" in html
    assert "/api/export-paper" in html
    assert "Write export files" in html
    assert "Research watchlist" in html
    assert "/api/watchlist" in html
    assert "/api/watchlist-add" in html
    assert "/api/watchlist-run" in html
    assert "Open position monitor" in html
    assert "/api/positions" in html
    assert "briefHtml" in html
    assert "Research brief" in html
    assert "Research action" in html
    assert "Recent SEC filings" in html
    assert "SEC cash/debt" in html
    assert "Symbol lookup" in html
    assert "/api/lookup" in html
    assert "/api/suggestions" in html
    assert "symbol-suggestions" in html
    assert "Run focused scan" in html
    assert "/api/run-symbol" in html
    assert "/api/job-log" in html
    assert "/job-dashboard" in html
    assert "job-match-btn" in html
    assert "Quick scan" in html
    assert "Bankroll override" in html


def test_data_health_flags_mismatched_open_counts_duplicates_and_bad_png():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        rows = [
            {
                "position_id": "opt-1",
                "ticker": "AAPL",
                "side": "call",
                "strike": 200,
                "expiry": "2026-06-18",
            },
            {
                "position_id": "opt-1",
                "ticker": "AAPL",
                "side": "call",
                "strike": 200,
                "expiry": "2026-06-18",
            },
        ]
        (data_dir / "open_positions.json").write_text(json.dumps(rows), encoding="utf-8")
        (data_dir / "open_share_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_futures_positions.json").write_text(
            json.dumps([{"position_id": "fut-1", "symbol": "CL=F"}]), encoding="utf-8",
        )
        (data_dir / "validation_summary.json").write_text(
            json.dumps({
                "open_positions": 0,
                "assets": {
                    "option": {"open_positions": 0},
                    "share": {"open_positions": 0},
                    "futures": {"open_positions": 0},
                },
            }),
            encoding="utf-8",
        )
        (data_dir / "position_aging_summary.json").write_text(
            json.dumps({"open_count": 1}), encoding="utf-8",
        )
        (data_dir / "equity_curve.png").write_bytes(b"not a real png")

        health = build_data_health(data_dir)
        labels = {row["label"]: row for row in health["checks"]}
        assert health["status"] == "bad"
        assert health["total_open"] == 3
        assert labels["Validation open count mismatch"]["level"] == "bad"
        assert labels["Position aging count"]["level"] == "warn"
        assert labels["Duplicate open positions"]["level"] == "warn"
        assert labels["Equity curve image corrupt"]["level"] == "bad"


def test_action_queue_prioritizes_health_and_exit_risk_over_paper_candidates():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        (data_dir / "validation_summary.json").write_text(json.dumps({
            "open_positions": 0,
            "assets": {
                "option": {"open_positions": 0},
                "share": {"open_positions": 0},
                "futures": {"open_positions": 0},
            },
        }), encoding="utf-8")
        (data_dir / "position_aging_summary.json").write_text(
            json.dumps({"open_count": 1}), encoding="utf-8",
        )
        (data_dir / "equity_curve.png").write_bytes(b"bad png")
        (data_dir / "open_positions.json").write_text(json.dumps([
            {
                "ticker": "AAPL",
                "side": "call",
                "strike": 200,
                "expiry": "2026-06-18",
                "entry_price": 2.0,
                "current_mid": 1.0,
                "unrealized_pct": -0.50,
                "latest_exit_pressure": 85,
                "trade_status": "Trade",
            },
            {
                "ticker": "AAPL",
                "side": "call",
                "strike": 210,
                "expiry": "2026-06-18",
                "entry_price": 1.5,
                "current_mid": 0.9,
                "unrealized_pct": -0.40,
                "latest_exit_pressure": 82,
                "trade_status": "Trade",
            },
        ]), encoding="utf-8")
        (data_dir / "open_share_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_futures_positions.json").write_text("[]", encoding="utf-8")
        pd.DataFrame([{
            "ticker": "NVDA",
            "contract": "NVDA 2026-06-18 C 200",
            "side": "call",
            "strike": 200,
            "expiry": "2026-06-18",
            "mid": 2.5,
            "suggested_contracts": 1,
            "actual_dollars": 250,
            "stop_price": 1.25,
            "target_price": 5.0,
            "confidence": 80,
            "rank_score": 2.0,
            "trade_status": "Trade",
            "spread_pct": 0.04,
        }]).to_parquet(data_dir / "top_options_20260603_120000.parquet")
        pd.DataFrame().to_parquet(data_dir / "top_shares_20260603_120000.parquet")
        pd.DataFrame().to_parquet(data_dir / "top_futures_20260603_120000.parquet")

        queue = build_action_queue(data_dir)
        assert queue["rows"][0]["category"] == "data_health"
        assert queue["rows"][0]["priority"] == 100
        assert any(row["category"] == "open_position" and row["symbol"] == "AAPL" for row in queue["rows"])
        aapl_rows = [
            row for row in queue["rows"]
            if row["category"] == "open_position" and row["symbol"] == "AAPL"
        ]
        assert len(aapl_rows) == 1
        assert aapl_rows[0]["grouped_count"] == 2
        assert any(row["category"] == "paper_candidate" and row["symbol"] == "NVDA" for row in queue["rows"])


def test_symbol_suggestions_include_local_contracts_positions_and_aliases():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        pd.DataFrame([{
            "ticker": "NVDA",
            "side": "call",
            "strike": 200.0,
            "expiry": "2026-06-18",
            "confidence": 82,
            "rank_score": 2.5,
            "trade_status": "Trade",
        }]).to_parquet(data_dir / "top_options_20260603_120000.parquet")
        pd.DataFrame([{
            "ticker": "AAPL",
            "confidence": 70,
            "rank_score": 1.0,
            "trade_status": "Trade",
            "suggested_dollars": 500,
        }]).to_parquet(data_dir / "top_shares_20260603_120000.parquet")
        pd.DataFrame([{
            "symbol": "CL=F",
            "name": "Crude Oil WTI",
            "direction": "long",
            "contract": "/MCL",
            "futures_score": 1.4,
            "trade_status": "Trade",
        }]).to_parquet(data_dir / "top_futures_20260603_120000.parquet")
        (data_dir / "open_futures_positions.json").write_text(
            json.dumps([{"symbol": "NG=F", "direction": "long", "contract": "/MNG"}]),
            encoding="utf-8",
        )

        nvda = build_symbol_suggestions(data_dir, query="nvda")
        assert any(row["query"] == "NVDA 2026-06-18 C 200" for row in nvda["rows"])

        oil = build_symbol_suggestions(data_dir, query="oil")
        assert any(row["symbol"] == "CL=F" for row in oil["rows"])

        apple = build_symbol_suggestions(data_dir, query="apple")
        assert any(row["symbol"] == "AAPL" and row["kind"] == "alias" for row in apple["rows"])

        gas = build_symbol_suggestions(data_dir, query="NG")
        assert any(row["symbol"] == "NG=F" and row["kind"] == "open_futures" for row in gas["rows"])


def test_opportunity_explorer_reads_and_filters_latest_snapshots():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        pd.DataFrame([
            {
                "ticker": "AAPL",
                "side": "call",
                "strike": 200,
                "expiry": "2026-06-18",
                "confidence": 75,
                "rank_score": 1.5,
                "trade_status": "Trade",
                "suggested_contracts": 1,
            },
            {
                "ticker": "TSLA",
                "side": "put",
                "strike": 300,
                "expiry": "2026-06-18",
                "confidence": 50,
                "rank_score": 3.0,
                "trade_status": "Watch",
                "suggested_contracts": 0,
            },
        ]).to_parquet(data_dir / "top_options_20260603_120000.parquet")
        pd.DataFrame([
            {
                "ticker": "NVDA",
                "confidence": 90,
                "rank_score": 2.0,
                "trade_status": "Trade",
                "suggested_dollars": 500,
            },
        ]).to_parquet(data_dir / "top_shares_20260603_120000.parquet")

        report = build_opportunities(data_dir, asset="all", status="actionable", limit=10)
        symbols = {row.get("ticker") or row.get("symbol") for row in report["rows"]}
        assert symbols == {"AAPL", "NVDA"}
        assert report["count"] == 2

        filtered = build_opportunities(data_dir, asset="option", query="AAPL", limit=10)
        assert filtered["rows"][0]["ticker"] == "AAPL"
        assert filtered["rows"][0]["actionable"] is True


def test_position_monitor_reads_dedupes_and_filters_open_state():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        rows = [
            {
                "ticker": "AAPL",
                "side": "call",
                "strike": 280,
                "expiry": "2026-06-18",
                "entry_time": "2026-06-01T00:00:00+00:00",
                "entry_price": 2.0,
                "current_mid": 1.0,
                "unrealized_pct": -0.5,
                "trade_status": "Trade",
                "latest_exit_pressure": 65,
                "latest_exit_action": "tighten_stop",
                "stop_price": 1.0,
                "target_price": 4.0,
            },
            {
                "ticker": "AAPL",
                "side": "call",
                "strike": 280,
                "expiry": "2026-06-18",
                "entry_time": "2026-06-01T00:00:00+00:00",
                "entry_price": 2.0,
                "current_mid": 1.0,
                "unrealized_pct": -0.5,
                "trade_status": "Trade",
                "latest_exit_pressure": 65,
                "latest_exit_action": "tighten_stop",
            },
            {
                "ticker": "MSFT",
                "side": "call",
                "strike": 400,
                "expiry": "2026-06-18",
                "entry_time": "2026-06-01T01:00:00+00:00",
                "entry_price": 2.0,
                "current_mid": 2.1,
                "unrealized_pct": 0.05,
                "trade_status": "Watch",
                "latest_exit_pressure": 10,
                "latest_exit_action": "hold",
            },
        ]
        (data_dir / "open_positions.json").write_text(json.dumps(rows), encoding="utf-8")
        (data_dir / "open_futures_positions.json").write_text(json.dumps([{
            "symbol": "ETH=F",
            "direction": "short",
            "contract": "/MET",
            "entry_time": "2026-06-01T02:00:00+00:00",
            "entry_price": 1800,
            "current_price": 1750,
            "pnl_pct": 0.03,
            "trade_status": "Trade",
            "latest_exit_pressure": 20,
        }]), encoding="utf-8")

        all_report = build_positions(data_dir, asset="all", limit=10)
        assert all_report["count"] == 3
        labels = {row["position_label"] for row in all_report["rows"]}
        assert "AAPL C 280 06-18" in labels
        assert "ETH=F SHORT /MET" in labels

        attention = build_positions(data_dir, asset="option", status="attention", limit=10)
        assert attention["count"] == 1
        assert attention["rows"][0]["ticker_or_symbol"] == "AAPL"
        assert attention["rows"][0]["attention"] is True


def test_risk_summary_surfaces_concentration_and_exit_pressure():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        (data_dir / "open_positions.json").write_text(json.dumps([
            {
                "ticker": "AAPL",
                "side": "call",
                "strike": 280,
                "expiry": "2026-06-18",
                "entry_price": 2.0,
                "current_mid": 1.0,
                "latest_exit_pressure": 85,
                "trade_status": "Trade",
            },
            {
                "ticker": "AAPL",
                "side": "call",
                "strike": 300,
                "expiry": "2026-06-18",
                "entry_price": 1.5,
                "current_mid": 1.8,
                "latest_exit_pressure": 20,
                "trade_status": "Trade",
            },
        ]), encoding="utf-8")
        (data_dir / "open_share_positions.json").write_text(json.dumps([{
            "ticker": "NVDA",
            "entry_price": 100.0,
            "current_price": 90.0,
            "latest_exit_pressure": 65,
            "reprice_failed_count": 2,
            "trade_status": "Watch",
        }]), encoding="utf-8")
        (data_dir / "open_futures_positions.json").write_text(json.dumps([{
            "symbol": "CL=F",
            "direction": "long",
            "contract": "/MCL",
            "entry_price": 70.0,
            "current_price": 73.5,
            "latest_exit_pressure": 10,
            "trade_status": "Trade",
        }]), encoding="utf-8")

        risk = build_risk_summary(data_dir)

        assert risk["total_open"] == 4
        assert risk["risk_level"] == "high"
        assert risk["high_exit_pressure_count"] == 1
        assert risk["reprice_trouble_count"] == 1
        assert {row["asset"]: row["count"] for row in risk["asset_breakdown"]} == {
            "option": 2,
            "share": 1,
            "futures": 1,
        }
        assert risk["concentration"][0]["symbol"] == "AAPL"
        assert risk["concentration"][0]["count"] == 2
        assert risk["worst_positions"][0]["ticker_or_symbol"] == "AAPL"


def test_performance_summary_reads_engine_perf_health_cache_and_finbert_state():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        import data_provider
        from telemetry import cache_stats, engine_health, perf

        old_perf_log = perf.PERF_LOG
        old_health_json = engine_health.HEALTH_JSON
        old_health_jsonl = engine_health.HEALTH_JSONL
        old_ram_enabled = data_provider.RAM_CACHE_ENABLED
        old_ram_max = data_provider.RAM_CACHE_MAX_ITEMS
        try:
            perf.PERF_LOG = data_dir / "engine_perf.parquet"
            engine_health.HEALTH_JSON = data_dir / "engine_health.json"
            engine_health.HEALTH_JSONL = data_dir / "engine_health_history.jsonl"
            pd.DataFrame([
                {"ts": "2026-06-03T20:00:00+00:00", "engine": "insider", "elapsed_sec": 121.0, "rows": 10, "ok": True, "error": ""},
                {"ts": "2026-06-03T20:00:01+00:00", "engine": "mispricing", "elapsed_sec": 44.0, "rows": 500, "ok": True, "error": ""},
            ]).to_parquet(perf.PERF_LOG)
            engine_health.record({
                "insider": {"ok": True, "rows": 10, "elapsed": 121.0},
                "mispricing": {"ok": True, "rows": 500, "elapsed": 44.0},
            })
            cache_stats.record_hit("history:AAPL")
            cache_stats.record_miss("history:MSFT")
            data_provider.configure_ram_cache(enabled=True, max_items=100)
            data_provider.cache_put("test:cockpit-performance", {"ok": True})
            pd.DataFrame([{
                "ticker": "AAPL",
                "finbert_device": "cuda",
            }]).to_parquet(data_dir / "top_options_20260603_120000.parquet")

            summary = build_performance_summary(data_dir)
        finally:
            perf.PERF_LOG = old_perf_log
            engine_health.HEALTH_JSON = old_health_json
            engine_health.HEALTH_JSONL = old_health_jsonl
            data_provider.configure_ram_cache(enabled=old_ram_enabled, max_items=old_ram_max)

        assert summary["ram_cache"]["ram_cache_enabled"] is True
        assert summary["finbert"]["status"] == "gpu"
        assert summary["latest_slowest"][0]["engine"] == "insider"
        assert "fast-insider" in summary["latest_slowest"][0]["tip"]
        assert any(row["prefix"] == "history" for row in summary["cache_prefixes"])
        assert summary["recommended_command"].endswith("--turbo --no-open")


def test_paper_candidate_panel_builds_and_writes_filtered_exports():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        pd.DataFrame([
            {
                "ticker": "AAPL",
                "contract": "AAPL 2026-06-18 C 200",
                "side": "call",
                "strike": 200,
                "expiry": "2026-06-18",
                "mid": 2.5,
                "suggested_contracts": 1,
                "actual_dollars": 250,
                "stop_price": 1.25,
                "target_price": 5.0,
                "confidence": 70,
                "rank_score": 2.0,
                "fused_score": 1.5,
                "trade_status": "Trade",
                "spread_pct": 0.04,
            },
            {
                "ticker": "MSFT",
                "contract": "MSFT 2026-06-18 C 500",
                "side": "call",
                "strike": 500,
                "expiry": "2026-06-18",
                "mid": 1.0,
                "suggested_contracts": 0,
                "stop_price": 0.5,
                "target_price": 2.0,
                "confidence": 60,
                "rank_score": 1.0,
                "trade_status": "Trade",
                "spread_pct": 0.04,
            },
        ]).to_parquet(data_dir / "top_options_20260603_120000.parquet")
        pd.DataFrame([
            {
                "ticker": "NVDA",
                "spot": 100.0,
                "suggested_dollars": 500,
                "stop_pct": -0.08,
                "target_pct": 0.18,
                "confidence": 75,
                "rank_score": 1.5,
                "trade_status": "Trade",
            }
        ]).to_parquet(data_dir / "top_shares_20260603_120000.parquet")
        pd.DataFrame().to_parquet(data_dir / "top_futures_20260603_120000.parquet")
        (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_share_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_futures_positions.json").write_text("[]", encoding="utf-8")

        preview = build_paper_candidates(data_dir, max_new=5)
        assert preview["selected_count"] == 2
        symbols = {row["ticker_or_symbol"] for row in preview["rows"]}
        assert symbols == {"AAPL", "NVDA"}

        dry = build_paper_candidates(data_dir, dry_run=True)
        assert dry["excluded_count"] == 1
        assert any("suggested_contracts <= 0" in row["reason_excluded"] for row in dry["rows"])

        written = build_paper_candidates(data_dir, max_new=5, write=True)
        assert written["wrote_files"] is True
        assert (data_dir / "external_paper_orders.csv").exists()
        assert (data_dir / "external_paper_orders.json").exists()


def test_research_watchlist_adds_dedupes_removes_and_builds_jobs():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        added = add_watchlist_query("Apple", data_dir)
        assert added["ok"] is True
        assert added["entry"]["symbol"] == "AAPL"
        assert added["count"] == 1

        again = add_watchlist_query("Apple", data_dir)
        assert again["ok"] is True
        assert again["updated_existing"] is True
        assert again["count"] == 1

        opt = add_watchlist_query("Nvidia 20260618 C 200", data_dir)
        assert opt["ok"] is True
        assert opt["count"] == 2
        assert opt["entry"]["request"]["ticker"] == "NVDA"

        pd.DataFrame([{
            "ticker": "NVDA",
            "side": "call",
            "strike": 200.0,
            "expiry": "2026-06-18",
            "mid": 4.2,
            "confidence": 82,
            "rank_score": 2.5,
            "trade_status": "Trade",
        }]).to_parquet(data_dir / "top_options_20260603_120000.parquet")
        (data_dir / "open_positions.json").write_text(json.dumps([{
            "ticker": "NVDA",
            "side": "call",
            "strike": 200,
            "expiry": "2026-06-18",
            "entry_price": 3.0,
            "current_mid": 4.5,
            "unrealized_pct": 0.5,
        }]), encoding="utf-8")
        enriched = load_watchlist(data_dir, enrich=True)
        nvda = [row for row in enriched["entries"] if row["symbol"] == "NVDA"][0]
        assert nvda["local_hits"] >= 2
        assert nvda["best_idea"] == "NVDA C 200.0 2026-06-18"
        assert nvda["best_status"] == "Trade"
        assert nvda["open_count"] == 1
        assert nvda["avg_unrealized_pct"] == 0.5

        jobs = run_watchlist_scans(data_dir, mode="quick", bankroll=25000, aggressive=True, launch=False)
        assert jobs["count"] == 2
        assert jobs["scan_args"] == ["--minimal", "--aggressive", "--bankroll", "25000.0"]
        assert all(job["ok"] for job in jobs["jobs"])

        removed = remove_watchlist_entry(added["entry"]["id"], data_dir)
        assert removed["removed"] is True
        remaining = load_watchlist(data_dir)
        assert remaining["count"] == 1
        assert remaining["entries"][0]["symbol"] == "NVDA"


if __name__ == "__main__":
    test_cockpit_summary_counts_open_positions()
    test_cockpit_artifact_path_finds_latest_dashboard()
    test_cockpit_html_contains_lookup_controls()
    test_data_health_flags_mismatched_open_counts_duplicates_and_bad_png()
    test_action_queue_prioritizes_health_and_exit_risk_over_paper_candidates()
    test_symbol_suggestions_include_local_contracts_positions_and_aliases()
    test_opportunity_explorer_reads_and_filters_latest_snapshots()
    test_position_monitor_reads_dedupes_and_filters_open_state()
    test_risk_summary_surfaces_concentration_and_exit_pressure()
    test_performance_summary_reads_engine_perf_health_cache_and_finbert_state()
    test_paper_candidate_panel_builds_and_writes_filtered_exports()
    test_research_watchlist_adds_dedupes_removes_and_builds_jobs()
    print("12/12 local cockpit tests passed")
