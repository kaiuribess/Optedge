import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.local_cockpit import artifact_path, build_opportunities, build_summary, render_cockpit_html


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
    assert "Opportunity explorer" in html
    assert "/api/opportunities" in html
    assert "Symbol lookup" in html
    assert "/api/lookup" in html
    assert "Run focused scan" in html
    assert "/api/run-symbol" in html
    assert "/api/job-log" in html
    assert "/job-dashboard" in html
    assert "job-match-btn" in html
    assert "Quick scan" in html
    assert "Bankroll override" in html


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


if __name__ == "__main__":
    test_cockpit_summary_counts_open_positions()
    test_cockpit_artifact_path_finds_latest_dashboard()
    test_cockpit_html_contains_lookup_controls()
    test_opportunity_explorer_reads_and_filters_latest_snapshots()
    print("4/4 local cockpit tests passed")
