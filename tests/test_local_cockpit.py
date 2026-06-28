import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.local_cockpit as cockpit_module
from scripts.export_robinhood_agentic_queue import robinhood_mcp_option_review_plan
from scripts.local_cockpit import (
    add_watchlist_queries, add_watchlist_query, artifact_path, build_agentic_autopilot_status,
    build_agentic_decision_journal,
    build_broker_reconciliation, build_cboe_option_activity, build_opportunities, build_paper_candidates,
    build_action_queue, build_data_health, build_option_chain_scan, build_performance_summary,
    build_option_chain_batch,
    build_best_setups, build_breadth_pulse, build_climate_gated_setups, build_command_center, build_market_pulse,
    build_macro_stress_pulse, build_options_sentiment,
    build_exit_review_summary, build_free_data_sources, build_positions, build_provider_status, build_risk_summary,
    build_robinhood_agentic_queue_report, build_position_hygiene,
    build_saved_option_contracts, build_sector_pulse, build_summary, build_swing_climate, build_swing_scout, build_symbol_suggestions,
    build_swing_packet, build_watchlist_sec_filings,
    build_today_review, apply_position_hygiene,
    load_watchlist, remove_watchlist_entry, render_cockpit_html, run_watchlist_scans,
    normalize_robinhood_broker_snapshot_file,
    record_agentic_decision, warm_sec_ticker_cache, write_option_chain_shortlist, write_position_hygiene_plan,
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
    assert "--panel3:#0f1111" in html
    assert "--accent:#20c997" in html
    assert "--shadow:0 16px 38px" in html
    assert ".view-tab.active" in html
    assert "Quick research command" in html
    assert "global-query" in html
    assert "globalLookup" in html
    assert "Review workspace" in html
    assert "global-workspace" in html
    assert "globalReviewWorkspace" in html
    assert "globalRunScan" in html
    assert "globalScanChain" in html
    assert "globalSaveWatchlist" in html
    assert "job-chain-btn" in html
    assert "requested_chain_min_dte" in html
    assert "requested_match_status" in html
    assert "brief-chain-btn" in html
    assert "chain_min_dte" in html
    assert "data-min-dte" in html
    assert "data-max-dte" in html
    assert "wireLookupBriefActions" in html
    assert "provider-query" in html
    assert "chain scan is staged" in html
    assert "global-suggestions" in html
    assert "Cockpit sections" in html
    assert 'data-view="overview"' in html
    assert 'data-view="positions"' in html
    assert 'data-view="explore"' in html
    assert 'data-view="chains"' in html
    assert 'data-view="providers"' in html
    assert 'data-view="paper"' in html
    assert 'data-view="research"' in html
    assert "setView" in html
    assert "Data health" in html
    assert "healthSummaryHtml" in html
    assert "healthIssueTable" in html
    assert "Opportunity quality" in html
    assert "opportunityQualityTable" in html
    assert "Command center" in html
    assert "/api/command-center" in html
    assert "commandCenterHtml" in html
    assert "trustRibbonHtml" in html
    assert "Data trust ribbon" in html
    assert "trust-card" in html
    assert "loadCommandCenter" in html
    assert "loadView('overview')" in html
    assert "loaders.slice(0, 1)" in html
    assert "window.setTimeout" in html
    assert "loadPositions().catch" not in html
    assert "loadPaperCandidates(false).catch" not in html
    assert "loadRobinhoodQueue(false).catch" not in html
    assert "loadExplorer().catch" not in html
    assert "loadWatchlist().catch" not in html
    assert "command-center-action-btn" in html
    assert "Best swing radar" in html
    assert "Session plan" in html
    assert "Review window" in html
    assert "7:30 AM-1:00 PM PT" in html
    assert "Position triage" in html
    assert "Swing packet" in html
    assert "/api/swing-packet" in html
    assert "/api/build-swing-packet" in html
    assert "/artifact/swing-packet-json" in html
    assert "/artifact/swing-packet-md" in html
    assert "swingPacketHtml" in html
    assert "loadSwingPacket" in html
    assert "Write packet files" in html
    assert "Write + 3m+ chain scan" in html
    assert "swing-packet-chain" in html
    assert "Saved 3m+ chain contracts" in html
    assert "chainCandidateQueueHtml" in html
    assert "Candidate queue" in html
    assert "Skipped candidates" in html
    assert "wireOptionChainActions($('swing-packet-results'))" in html
    assert "Action queue" in html
    assert "Swing Scout, Nasdaq movers" in html
    assert "/api/action-queue" in html
    assert "queue-action-btn" in html
    assert "routeQueueAction" in html
    assert "Today review" in html
    assert "/api/today-review" in html
    assert "todayReviewHtml" in html
    assert "todayReviewCard" in html
    assert "review-grid" in html
    assert "priority-badge" in html
    assert "Scan 3m+ chain" in html
    assert "loadTodayReview" in html
    assert "today-review-action-btn" in html
    assert "routeTodayReviewAction" in html
    assert "Swing climate" in html
    assert "/api/swing-climate" in html
    assert "swingClimateHtml" in html
    assert "loadSwingClimate" in html
    assert "Trade gates" in html
    assert "Asset bias" in html
    assert "P/C total/equity/index" in html
    assert "Climate-gated setups" in html
    assert "/api/climate-gated-setups" in html
    assert "climateGatedSetupsHtml" in html
    assert "loadClimateGatedSetups" in html
    assert "Scan 3m+ chain" in html
    assert "setup-chain-btn" in html
    assert "setup-scan-btn" in html
    assert "canScanOptionChainSymbol" in html
    assert "Primary contract" in html
    assert "Trade action" in html
    assert "Open exposure" in html
    assert "Save contract" in html
    assert "contract-watchlist-btn" in html
    assert "optionContractQuery" in html
    assert "wireOptionChainActions" in html
    assert "Market pulse" in html
    assert "/api/market-pulse" in html
    assert "marketPulseHtml" in html
    assert "Macro stress" in html
    assert "/api/macro-stress" in html
    assert "macroStressHtml" in html
    assert "Options sentiment" in html
    assert "Cboe options sentiment" in html
    assert "loadMarketPulse" in html
    assert "Breadth pulse" in html
    assert "/api/breadth-pulse" in html
    assert "breadthPulseHtml" in html
    assert "loadBreadthPulse" in html
    assert "Sector pulse" in html
    assert "/api/sector-pulse" in html
    assert "sectorPulseHtml" in html
    assert "loadSectorPulse" in html
    assert "Portfolio risk" in html
    assert "/api/risk-summary" in html
    assert "riskSummaryHtml" in html
    assert "Performance" in html
    assert "/api/performance-summary" in html
    assert "performanceSummaryHtml" in html
    assert "Best setups" in html
    assert "bestSetupsDecisionHtml" in html
    assert "/api/best-setups" in html
    assert "bestSetupsHtml" in html
    assert "loadBestSetups" in html
    assert "readiness_label" in html
    assert "risk_flags" in html
    assert "Small-cap + futures swing scout" in html
    assert "/api/swing-scout" in html
    assert "swingScoutHtml" in html
    assert "loadSwingScout" in html
    assert "swing-scout-asset" in html
    assert "swing-scout-lane" in html
    assert "swing-scout-min-score" in html
    assert "swing-scout-nasdaq" in html
    assert "swing-scout-hide-wait" in html
    assert "Nasdaq small-cap movers" in html
    assert "Review actions" in html
    assert "actionQueueActionLabel" in html
    assert "<th>Gate</th>" in html
    assert "<th>Source</th>" in html
    assert "setup_gate_label" in html
    assert "small/mid-cap asymmetry" in html
    assert "Opportunity explorer" in html
    assert "/api/opportunities" in html
    assert "External paper candidates" in html
    assert "paper-summary" in html
    assert "paperCandidateSummary" in html
    assert "/api/paper-candidates" in html
    assert "/api/export-paper" in html
    assert "Write export files" in html
    assert "Chain shortlist" in html
    assert "/artifact/option-chain-shortlist" in html
    assert "CBOE public activity" in html
    assert "/api/cboe-option-activity" in html
    assert "cboeActivityResultsHtml" in html
    assert "cboe-activity-query" in html
    assert "Decision gate" in html
    assert "Focus data trust" in html
    assert "Data coverage" in html
    assert "Coverage score" in html
    assert "Event risk" in html
    assert "Earnings / catalyst event risk" in html
    assert "Chain quality" in html
    assert "SEC offering risk" in html
    assert "SEC dilution / offering risk" in html
    assert "Agentic options queue" in html
    assert 'id="rh-min-dte" type="number" min="0" max="1200" step="1" value="90"' in html
    assert "/api/robinhood-queue" in html
    assert "/api/build-robinhood-queue" in html
    assert "loadRobinhoodQueue" in html
    assert "Autopilot status" in html
    assert "autopilot-summary" in html
    assert "autopilot-actions" in html
    assert "autopilot-notes" in html
    assert "autopilot-preflight" in html
    assert "Broker / local reconciliation" in html
    assert "autopilot-broker" in html
    assert "/api/broker-reconciliation" in html
    assert "broker-normalize" in html
    assert "/api/normalize-broker-snapshot" in html
    assert "Position hygiene" in html
    assert "hygiene-summary" in html
    assert "/api/position-hygiene" in html
    assert "/api/write-position-hygiene-plan" in html
    assert "Preview expired cleanup" in html
    assert "Apply expired cleanup" in html
    assert "applyPositionHygiene" in html
    assert "/api/apply-position-hygiene" in html
    assert "/artifact/position-hygiene-plan" in html
    assert "autopilot-packet-refresh" in html
    assert "routeAutopilotAction" in html
    assert "loadAgenticAutopilotStatus" in html
    assert "/api/agentic-autopilot-status" in html
    assert "/artifact/robinhood-live-order-tickets" in html
    assert "/artifact/agentic-paper-positions" in html
    assert "Local decision journal" in html
    assert "/api/agentic-decision-journal" in html
    assert "/api/agentic-decision" in html
    assert "loadDecisionJournal" in html
    assert "addDecisionJournalRow" in html
    assert "rh-refresh-chain" in html
    assert "rh-chain-preset" in html
    assert "Premium left" in html
    assert "Top rejects" in html
    assert "Option chain scan" in html
    assert "3m+ swing preset" in html
    assert "Long-dated preset" in html
    assert "Liquid preset" in html
    assert "applyChainPreset" in html
    assert "/api/option-chain-scan" in html
    assert "/api/option-chain-batch" in html
    assert "scanOptionChain" in html
    assert "scanOptionChainBatch" in html
    assert "Shortlist chain sweep" in html
    assert "optionChainResultsHtml" in html
    assert "optionChainBatchResultsHtml" in html
    assert "optionChainDecisionHtml" in html
    assert "optionChainTradePlanHtml" in html
    assert "Save primary contract" in html
    assert "decision-strip" in html
    assert "Trade plan" in html
    assert "Save best A/B contracts" in html
    assert "Write shortlist files" in html
    assert "/api/export-chain-shortlist" in html
    assert "exportChainBatchShortlist" in html
    assert "wireChainBatchActions" in html
    assert "Expiration quality" in html
    assert "budget ladder" in html
    assert "Why contracts were filtered out" in html
    assert "Grade / lane" in html
    assert "Break-even" in html
    assert "Budget fit" in html
    assert "Risk / reward ref" in html
    assert "Primary review" in html
    assert "Best budget" in html
    assert "Provider status" in html
    assert "/api/provider-status" in html
    assert "loadProviderStatus" in html
    assert "providerStatusTable" in html
    assert "Data trust" in html
    assert "History source" in html
    assert "Option chain" in html
    assert "Chain source" in html
    assert "Chain providers" in html
    assert "SEC facts" in html
    assert "Free source map" in html
    assert "/api/free-data-sources" in html
    assert "freeSourcesTable" in html
    assert "loadFreeDataSources" in html
    assert "Research watchlist" in html
    assert "Readiness" in html
    assert "/api/watchlist" in html
    assert "/api/watchlist-add" in html
    assert "/api/watchlist-add-many" in html
    assert "/api/watchlist-run" in html
    assert "SEC filing monitor" in html
    assert "/api/watchlist-sec-filings" in html
    assert "secFilingsTable" in html
    assert "loadWatchlistSecFilings" in html
    assert "review_sec_filings" in html
    assert "review_sec_filing_risk" in html
    assert ".review-card.sec_filing" in html
    assert "review_trading_halt" in html
    assert ".review-card.trading_halt" in html
    assert "review_regsho_threshold" in html
    assert ".review-card.regsho_threshold" in html
    assert "review_short_sale_circuit" in html
    assert ".review-card.short_sale_circuit" in html
    assert "Saved option contracts" in html
    assert "/api/saved-option-contracts" in html
    assert "savedContractsTable" in html
    assert "savedContractTriageCards" in html
    assert "Saved contract triage" in html
    assert "loadSavedContracts" in html
    assert "Refresh quotes" in html
    assert "Review now" in html
    assert "Review score" in html
    assert "Open position monitor" in html
    assert "Exit review cockpit" in html
    assert "/api/exit-reviews" in html
    assert "exitReviewSummaryHtml" in html
    assert "/api/positions" in html
    assert "briefHtml" in html
    assert "Research brief" in html
    assert "Research action" in html
    assert "Requested match" in html
    assert "Exact contract exposure" in html
    assert "Alt contracts" in html
    assert "Best alternative" in html
    assert "Price trend" in html
    assert "Market structure" in html
    assert "Paper readiness" in html
    assert "Recent SEC filings" in html
    assert "SEC cash/debt" in html
    assert "Cboe contract activity" in html
    assert "Cboe volume" in html
    assert "Swing verdict" in html
    assert "Swing score" in html
    assert "Best alt" in html
    assert "Symbol lookup" in html
    assert "/api/lookup" in html
    assert "/api/suggestions" in html
    assert "symbol-suggestions" in html
    assert "Run focused scan" in html
    assert "/api/run-symbol" in html
    assert "/api/run-refresh-scan" in html
    assert "run_refresh_scan" in html
    assert "/api/job-log" in html
    assert "/job-dashboard" in html
    assert "/job-lookup" in html
    assert "/api/warm-symbol-caches" in html
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
        assert labels["Expired local option records"]["level"] == "bad"
        assert labels["Equity curve image corrupt"]["level"] == "bad"
        assert labels["SEC ticker cache missing"]["level"] == "warn"
        assert labels["Nasdaq symbol directory missing"]["level"] == "warn"
        assert health["expired_local_option_rows"] == 2
        assert "AAPL 2026-06-18 CALL 200" in health["expired_local_option_examples"]
        assert health["free_data_caches"]["sec_company_tickers"]["status"] == "missing"
        assert health["free_data_caches"]["nasdaq_symbol_directory"]["status"] == "missing"


def test_data_health_reports_fresh_sec_ticker_cache():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_share_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_futures_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "validation_summary.json").write_text(json.dumps({
            "open_positions": 0,
            "assets": {
                "option": {"open_positions": 0},
                "share": {"open_positions": 0},
                "futures": {"open_positions": 0},
            },
        }), encoding="utf-8")
        (data_dir / "position_aging_summary.json").write_text(
            json.dumps({"open_count": 0}), encoding="utf-8",
        )
        (data_dir / "sec_company_tickers.json").write_text(json.dumps({
            "rows": [
                {"symbol": "SNOW", "name": "Snowflake Inc.", "cik": 1640147},
                {"symbol": "AAPL", "name": "Apple Inc.", "cik": 320193},
            ],
        }), encoding="utf-8")
        (data_dir / "nasdaq_symbol_directory.json").write_text(json.dumps({
            "rows": [
                {"symbol": "SNOW", "name": "Snowflake Inc.", "type": "EQUITY"},
                {"symbol": "QQQ", "name": "Invesco QQQ Trust", "type": "ETF", "is_etf": True},
            ],
        }), encoding="utf-8")

        health = build_data_health(data_dir)
        labels = {row["label"]: row for row in health["checks"]}
        assert labels["SEC ticker cache"]["level"] == "ok"
        assert labels["Nasdaq symbol directory"]["level"] == "ok"
        assert health["free_data_caches"]["sec_company_tickers"]["status"] == "fresh"
        assert health["free_data_caches"]["sec_company_tickers"]["row_count"] == 2
        assert health["free_data_caches"]["nasdaq_symbol_directory"]["status"] == "fresh"
        assert health["free_data_caches"]["nasdaq_symbol_directory"]["row_count"] == 2


def test_data_health_audits_latest_opportunity_duplicates():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_share_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_futures_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "validation_summary.json").write_text(json.dumps({
            "open_positions": 0,
            "assets": {
                "option": {"open_positions": 0},
                "share": {"open_positions": 0},
                "futures": {"open_positions": 0},
            },
        }), encoding="utf-8")
        (data_dir / "position_aging_summary.json").write_text(
            json.dumps({"open_count": 0}), encoding="utf-8",
        )
        pd.DataFrame([
            {
                "ticker": "AAPL",
                "side": "call",
                "strike": 200,
                "expiry": "2026-06-18",
                "mid": 2.5,
                "suggested_contracts": 1,
                "trade_status": "Trade",
            },
            {
                "ticker": "AAPL",
                "side": "call",
                "strike": 200,
                "expiry": "2026-06-18",
                "mid": 2.6,
                "suggested_contracts": 1,
                "trade_status": "Trade",
            },
        ]).to_parquet(data_dir / "top_options_20260603_120000.parquet")
        pd.DataFrame([{
            "ticker": "NVDA",
            "spot": 100.0,
            "suggested_dollars": 500,
            "trade_status": "Trade",
        }]).to_parquet(data_dir / "top_shares_20260603_120000.parquet")
        pd.DataFrame([{
            "symbol": "ES=F",
            "contract": "/MES",
            "direction": "long",
            "entry_price": 5000,
            "suggested_contracts": 1,
            "trade_status": "Trade",
        }]).to_parquet(data_dir / "top_futures_20260603_120000.parquet")

        health = build_data_health(data_dir)
        labels = {row["label"]: row for row in health["checks"]}
        assert labels["option opportunity duplicates"]["level"] == "warn"
        assert "1 duplicate" in labels["option opportunity duplicates"]["detail"]
        assert health["opportunity_quality"]["option"]["duplicate_rows"] == 1
        assert health["opportunity_quality"]["share"]["actionable_rows"] == 1
        assert health["opportunity_quality"]["futures"]["actionable_rows"] == 1


def test_warm_sec_ticker_cache_uses_data_dir_cache():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        old_loader = cockpit_module.load_sec_company_tickers
        old_nasdaq_loader = cockpit_module.load_nasdaq_symbol_directory

        def fake_loader(cache_path, timeout=8.0, fetch_if_stale=True, **kwargs):
            Path(cache_path).write_text(json.dumps({
                "rows": [
                    {"symbol": "SNOW", "name": "Snowflake Inc.", "cik": 1640147},
                ],
            }), encoding="utf-8")
            return [{"symbol": "SNOW", "name": "Snowflake Inc.", "cik": 1640147}]

        def fake_nasdaq_loader(cache_path, timeout=8.0, fetch_if_stale=True, **kwargs):
            Path(cache_path).write_text(json.dumps({
                "rows": [
                    {"symbol": "QQQ", "name": "Invesco QQQ Trust", "type": "ETF", "is_etf": True},
                ],
            }), encoding="utf-8")
            return [{"symbol": "QQQ", "name": "Invesco QQQ Trust", "type": "ETF"}]

        cockpit_module.load_sec_company_tickers = fake_loader
        cockpit_module.load_nasdaq_symbol_directory = fake_nasdaq_loader
        try:
            result = warm_sec_ticker_cache(data_dir)
        finally:
            cockpit_module.load_sec_company_tickers = old_loader
            cockpit_module.load_nasdaq_symbol_directory = old_nasdaq_loader

        assert result["ok"] is True
        assert result["row_count"] == 1
        assert result["nasdaq_row_count"] == 1
        assert result["cache"]["status"] == "fresh"
        assert result["nasdaq_cache"]["status"] == "fresh"
        assert (data_dir / "sec_company_tickers.json").exists()
        assert (data_dir / "nasdaq_symbol_directory.json").exists()


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
            "contract": "NVDA 2026-12-18 C 200",
            "side": "call",
            "strike": 200,
            "expiry": "2026-12-18",
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
        assert any(
            row["label"] == "SEC ticker cache missing"
            and row["action"] == "warm_symbol_caches"
            for row in queue["rows"]
        )
        assert any(
            row["label"] == "Nasdaq symbol directory missing"
            and row["action"] == "warm_symbol_caches"
            for row in queue["rows"]
        )
        assert any(
            row["label"] == "Expired local option records"
            and row["action"] == "preview_position_hygiene_cleanup"
            for row in queue["rows"]
        )
        assert any(row["category"] == "open_position" and row["symbol"] == "AAPL" for row in queue["rows"])
        aapl_rows = [
            row for row in queue["rows"]
            if row["category"] == "open_position" and row["symbol"] == "AAPL"
        ]
        assert len(aapl_rows) == 1
        assert aapl_rows[0]["grouped_count"] == 2
        assert any(row["category"] == "paper_candidate" and row["symbol"] == "NVDA" for row in queue["rows"])


def test_action_queue_groups_stale_snapshots_into_refresh_action():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_share_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_futures_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "validation_summary.json").write_text(json.dumps({
            "open_positions": 0,
            "assets": {
                "option": {"open_positions": 0},
                "share": {"open_positions": 0},
                "futures": {"open_positions": 0},
            },
        }), encoding="utf-8")
        (data_dir / "position_aging_summary.json").write_text(
            json.dumps({"open_count": 0}), encoding="utf-8",
        )
        (data_dir / "dashboard_20260603_120000.html").write_text("<html></html>", encoding="utf-8")
        pd.DataFrame([{
            "ticker": "AAPL",
            "side": "call",
            "strike": 200,
            "expiry": "2026-12-18",
            "mid": 2.5,
            "suggested_contracts": 1,
            "trade_status": "Trade",
        }]).to_parquet(data_dir / "top_options_20260603_120000.parquet")
        pd.DataFrame([{
            "ticker": "NVDA",
            "spot": 100,
            "suggested_dollars": 500,
            "trade_status": "Trade",
        }]).to_parquet(data_dir / "top_shares_20260603_120000.parquet")
        pd.DataFrame([{
            "symbol": "ES=F",
            "direction": "long",
            "entry_price": 5000,
            "suggested_contracts": 1,
            "trade_status": "Trade",
        }]).to_parquet(data_dir / "top_futures_20260603_120000.parquet")
        pd.DataFrame([{
            "ticker": "HMY",
            "value_score": 1.5,
            "trade_status": "Trade",
        }]).to_parquet(data_dir / "top_value_20260603_120000.parquet")

        old_ts = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
        for path in data_dir.glob("top_*_20260603_120000.parquet"):
            os.utime(path, (old_ts, old_ts))

        queue = build_action_queue(data_dir)
        refresh = [
            row for row in queue["rows"]
            if row["action"] == "run_refresh_scan"
        ]
        assert refresh
        assert refresh[0]["label"] == "Refresh stale market snapshots"
        assert "snapshot old" in refresh[0]["detail"]
        assert refresh[0]["priority"] == 82


def test_action_queue_surfaces_cached_sec_filing_risk():
    old_health = cockpit_module.build_data_health
    old_positions = cockpit_module.build_positions
    old_paper = cockpit_module.build_paper_candidates
    old_swing = cockpit_module.build_swing_scout
    old_watchlist = cockpit_module.load_watchlist

    cockpit_module.build_data_health = lambda *args, **kwargs: {"checks": [], "status": "ok"}
    cockpit_module.build_positions = lambda *args, **kwargs: {"rows": []}
    cockpit_module.build_paper_candidates = lambda *args, **kwargs: {"rows": []}
    cockpit_module.build_swing_scout = lambda *args, **kwargs: {"rows": []}
    cockpit_module.load_watchlist = lambda *args, **kwargs: {"entries": []}
    try:
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            (data_dir / "watchlist_sec_filings.json").write_text(json.dumps({
                "generated_at": "2026-06-16T12:00:00+00:00",
                "rows": [{
                    "priority": 94,
                    "ticker": "AAPL",
                    "form": "S-3",
                    "filing_date": "2026-06-16",
                    "days_old": 0,
                    "freshness": "fresh",
                    "signal": "dilution_or_offering_watch",
                    "description": "Shelf registration statement",
                    "url": "https://www.sec.gov/aapl",
                }],
            }), encoding="utf-8")

            queue = build_action_queue(data_dir)
    finally:
        cockpit_module.build_data_health = old_health
        cockpit_module.build_positions = old_positions
        cockpit_module.build_paper_candidates = old_paper
        cockpit_module.build_swing_scout = old_swing
        cockpit_module.load_watchlist = old_watchlist

    sec_rows = [row for row in queue["rows"] if row["category"] == "sec_filing"]
    assert sec_rows
    row = sec_rows[0]
    assert row["label"] == "Review SEC offering risk"
    assert row["action"] == "review_sec_filing_risk"
    assert row["priority"] >= 96
    assert row["symbol"] == "AAPL"
    assert "S-3" in row["detail"]


def test_action_queue_prompts_sec_monitor_refresh_when_cache_missing():
    old_health = cockpit_module.build_data_health
    old_positions = cockpit_module.build_positions
    old_paper = cockpit_module.build_paper_candidates
    old_swing = cockpit_module.build_swing_scout
    old_watchlist = cockpit_module.load_watchlist

    cockpit_module.build_data_health = lambda *args, **kwargs: {"checks": [], "status": "ok"}
    cockpit_module.build_positions = lambda *args, **kwargs: {"rows": []}
    cockpit_module.build_paper_candidates = lambda *args, **kwargs: {"rows": []}
    cockpit_module.build_swing_scout = lambda *args, **kwargs: {"rows": []}
    cockpit_module.load_watchlist = lambda *args, **kwargs: {
        "entries": [{"id": "aapl", "symbol": "AAPL", "query": "AAPL"}]
    }
    try:
        with tempfile.TemporaryDirectory() as td:
            queue = build_action_queue(Path(td))
    finally:
        cockpit_module.build_data_health = old_health
        cockpit_module.build_positions = old_positions
        cockpit_module.build_paper_candidates = old_paper
        cockpit_module.build_swing_scout = old_swing
        cockpit_module.load_watchlist = old_watchlist

    refresh = [
        row for row in queue["rows"]
        if row["category"] == "sec_filing" and row["action"] == "review_sec_filings"
    ]
    assert refresh
    assert refresh[0]["label"] == "Refresh SEC filing monitor"
    assert "missing" in refresh[0]["detail"]


def test_action_queue_surfaces_trade_halt_risk_for_watchlist_symbol():
    old_health = cockpit_module.build_data_health
    old_positions = cockpit_module.build_positions
    old_paper = cockpit_module.build_paper_candidates
    old_swing = cockpit_module.build_swing_scout
    old_watchlist = cockpit_module.load_watchlist
    old_halts = cockpit_module.halt_rows_for_symbols
    seen_symbols = {}

    cockpit_module.build_data_health = lambda *args, **kwargs: {"checks": [], "status": "ok"}
    cockpit_module.build_positions = lambda *args, **kwargs: {"rows": []}
    cockpit_module.build_paper_candidates = lambda *args, **kwargs: {"rows": []}
    cockpit_module.build_swing_scout = lambda *args, **kwargs: {"rows": []}
    cockpit_module.load_watchlist = lambda *args, **kwargs: {
        "entries": [{"id": "move", "symbol": "MOVE", "query": "MOVE"}]
    }

    def fake_halts(symbols, cache_age=60):
        seen_symbols["symbols"] = symbols
        seen_symbols["cache_age"] = cache_age
        return pd.DataFrame([{
            "symbol": "MOVE",
            "name": "Move Corp Cmn",
            "market": "NASDAQ",
            "reason_code": "T1",
            "halted_at": "2026-06-16T14:19:52-04:00",
            "resumption_trade_time": None,
            "active_halt": True,
            "halt_risk_score": 98,
        }])

    cockpit_module.halt_rows_for_symbols = fake_halts
    try:
        with tempfile.TemporaryDirectory() as td:
            queue = build_action_queue(Path(td))
    finally:
        cockpit_module.build_data_health = old_health
        cockpit_module.build_positions = old_positions
        cockpit_module.build_paper_candidates = old_paper
        cockpit_module.build_swing_scout = old_swing
        cockpit_module.load_watchlist = old_watchlist
        cockpit_module.halt_rows_for_symbols = old_halts

    rows = [row for row in queue["rows"] if row["category"] == "trading_halt"]
    assert rows
    assert seen_symbols["symbols"] == ["MOVE"]
    assert seen_symbols["cache_age"] == 60
    assert rows[0]["label"] == "Trading halt active"
    assert rows[0]["action"] == "review_trading_halt"
    assert rows[0]["priority"] >= 98
    assert rows[0]["symbol"] == "MOVE"
    assert "T1 halt" in rows[0]["detail"]


def test_action_queue_surfaces_regsho_threshold_risk_for_watchlist_symbol():
    old_health = cockpit_module.build_data_health
    old_positions = cockpit_module.build_positions
    old_paper = cockpit_module.build_paper_candidates
    old_swing = cockpit_module.build_swing_scout
    old_watchlist = cockpit_module.load_watchlist
    old_halts = cockpit_module.halt_rows_for_symbols
    old_thresholds = cockpit_module.threshold_rows_for_symbols
    seen_symbols = {}

    cockpit_module.build_data_health = lambda *args, **kwargs: {"checks": [], "status": "ok"}
    cockpit_module.build_positions = lambda *args, **kwargs: {"rows": []}
    cockpit_module.build_paper_candidates = lambda *args, **kwargs: {"rows": []}
    cockpit_module.build_swing_scout = lambda *args, **kwargs: {"rows": []}
    cockpit_module.load_watchlist = lambda *args, **kwargs: {
        "entries": [{"id": "move", "symbol": "MOVE", "query": "MOVE"}]
    }
    cockpit_module.halt_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame()

    def fake_thresholds(symbols, cache_age=6 * 3600):
        seen_symbols["symbols"] = symbols
        seen_symbols["cache_age"] = cache_age
        return pd.DataFrame([{
            "symbol": "MOVE",
            "name": "Move Corp Cmn",
            "market_category": "S",
            "reg_sho_threshold_flag": "Y",
            "rule_3210": "N",
            "is_threshold": True,
            "settlement_risk_score": 86,
        }])

    cockpit_module.threshold_rows_for_symbols = fake_thresholds
    try:
        with tempfile.TemporaryDirectory() as td:
            queue = build_action_queue(Path(td))
    finally:
        cockpit_module.build_data_health = old_health
        cockpit_module.build_positions = old_positions
        cockpit_module.build_paper_candidates = old_paper
        cockpit_module.build_swing_scout = old_swing
        cockpit_module.load_watchlist = old_watchlist
        cockpit_module.halt_rows_for_symbols = old_halts
        cockpit_module.threshold_rows_for_symbols = old_thresholds

    rows = [row for row in queue["rows"] if row["category"] == "regsho_threshold"]
    assert rows
    assert seen_symbols["symbols"] == ["MOVE"]
    assert seen_symbols["cache_age"] == 6 * 3600
    assert rows[0]["label"] == "Reg SHO threshold risk"
    assert rows[0]["action"] == "review_regsho_threshold"
    assert rows[0]["priority"] >= 86
    assert rows[0]["symbol"] == "MOVE"
    assert "Reg SHO" in rows[0]["detail"]


def test_action_queue_surfaces_short_sale_circuit_risk_for_watchlist_symbol():
    old_health = cockpit_module.build_data_health
    old_positions = cockpit_module.build_positions
    old_paper = cockpit_module.build_paper_candidates
    old_swing = cockpit_module.build_swing_scout
    old_watchlist = cockpit_module.load_watchlist
    old_halts = cockpit_module.halt_rows_for_symbols
    old_thresholds = cockpit_module.threshold_rows_for_symbols
    old_circuits = cockpit_module.circuit_rows_for_symbols
    seen_symbols = {}

    cockpit_module.build_data_health = lambda *args, **kwargs: {"checks": [], "status": "ok"}
    cockpit_module.build_positions = lambda *args, **kwargs: {"rows": []}
    cockpit_module.build_paper_candidates = lambda *args, **kwargs: {"rows": []}
    cockpit_module.build_swing_scout = lambda *args, **kwargs: {"rows": []}
    cockpit_module.load_watchlist = lambda *args, **kwargs: {
        "entries": [{"id": "move", "symbol": "MOVE", "query": "MOVE"}]
    }
    cockpit_module.halt_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame()
    cockpit_module.threshold_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame()

    def fake_circuits(symbols, cache_age=30 * 60):
        seen_symbols["symbols"] = symbols
        seen_symbols["cache_age"] = cache_age
        return pd.DataFrame([{
            "symbol": "MOVE",
            "name": "Move Corp Cmn",
            "market_category": "R",
            "trigger_time": "6/16/2026 9:30:00 AM",
            "triggered_at": "2026-06-16T09:30:00-04:00",
            "short_sale_restricted": True,
            "ssr_risk_score": 82,
        }])

    cockpit_module.circuit_rows_for_symbols = fake_circuits
    try:
        with tempfile.TemporaryDirectory() as td:
            queue = build_action_queue(Path(td))
    finally:
        cockpit_module.build_data_health = old_health
        cockpit_module.build_positions = old_positions
        cockpit_module.build_paper_candidates = old_paper
        cockpit_module.build_swing_scout = old_swing
        cockpit_module.load_watchlist = old_watchlist
        cockpit_module.halt_rows_for_symbols = old_halts
        cockpit_module.threshold_rows_for_symbols = old_thresholds
        cockpit_module.circuit_rows_for_symbols = old_circuits

    rows = [row for row in queue["rows"] if row["category"] == "short_sale_circuit"]
    assert rows
    assert seen_symbols["symbols"] == ["MOVE"]
    assert seen_symbols["cache_age"] == 30 * 60
    assert rows[0]["label"] == "Short-sale circuit breaker"
    assert rows[0]["action"] == "review_short_sale_circuit"
    assert rows[0]["priority"] >= 82
    assert rows[0]["symbol"] == "MOVE"
    assert "Rule 201" in rows[0]["detail"]


def test_action_queue_surfaces_ready_watchlist_ideas():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_share_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_futures_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "validation_summary.json").write_text(json.dumps({
            "open_positions": 0,
            "assets": {
                "option": {"open_positions": 0},
                "share": {"open_positions": 0},
                "futures": {"open_positions": 0},
            },
        }), encoding="utf-8")
        (data_dir / "position_aging_summary.json").write_text(
            json.dumps({"open_count": 0}), encoding="utf-8",
        )
        pd.DataFrame([{
            "ticker": "AAPL",
            "side": "call",
            "strike": 200.0,
            "expiry": "2026-06-18",
            "mid": 3.2,
            "confidence": 80,
            "rank_score": 2.0,
            "trade_status": "Trade",
            "suggested_contracts": 1,
            "chain_source": "tradier",
            "quote_quality": "live_or_broker",
        }]).to_parquet(data_dir / "top_options_20260603_120000.parquet")
        (data_dir / "option_chain_shortlist.json").write_text(json.dumps({
            "generated_at": "2026-06-24T19:00:00+00:00",
            "rows": [{
                "symbol": "AAPL",
                "contract_query": "AAPL 2026-06-18 C 210",
                "side": "call",
                "strike": 210.0,
                "expiry": "2026-06-18",
                "dte": 10,
                "bid": 1.9,
                "ask": 2.1,
                "mid": 2.0,
                "premium_dollars": 200.0,
                "spread_pct": 0.10,
                "openInterest": 900,
                "volume": 120,
                "readiness_score": 88,
                "contract_quality_score": 90,
                "swing_fit_score": 91,
                "swing_fit_label": "reviewable_swing",
                "contract_grade": "B",
                "review_lane": "secondary_review",
                "chain_source": "cboe_options_chain",
            }],
        }), encoding="utf-8")

        add_watchlist_query("AAPL 20260618 C 200", data_dir)
        enriched = load_watchlist(data_dir, enrich=True)
        assert enriched["entries"][0]["option_alt_best"] == "AAPL C 210.0 2026-06-18"
        assert enriched["entries"][0]["option_alt_readiness"] == 88
        queue = build_action_queue(data_dir)
        ready = [
            row for row in queue["rows"]
            if row["category"] == "watchlist" and row["label"] == "Review swing-verdict candidate"
        ]
        assert ready
        assert ready[0]["symbol"] == "AAPL"
        assert ready[0]["action"] == "preview_paper_candidate"
        assert ready[0]["source"] == "watchlist_swing_verdict"
        assert ready[0]["swing_verdict_decision"] == "paper_review"
        assert ready[0]["swing_verdict_score"] >= 70
        assert ready[0]["option_alt_best"] == "AAPL C 210.0 2026-06-18"
        assert "best nearby contract" in ready[0]["detail"]


def test_action_queue_promotes_reviewable_swing_scout_rows():
    old_health = cockpit_module.build_data_health
    old_positions = cockpit_module.build_positions
    old_paper = cockpit_module.build_paper_candidates
    old_watchlist = cockpit_module.load_watchlist
    old_swing = cockpit_module.build_swing_scout
    swing_kwargs = {}

    cockpit_module.build_data_health = lambda *args, **kwargs: {"checks": [], "status": "ok"}
    cockpit_module.build_positions = lambda *args, **kwargs: {"rows": []}
    cockpit_module.build_paper_candidates = lambda *args, **kwargs: {"rows": []}
    cockpit_module.load_watchlist = lambda *args, **kwargs: {"entries": []}

    def fake_swing(*args, **kwargs):
        swing_kwargs.update(kwargs)
        return {
            "rows": [
                {
                    "asset": "option",
                    "ticker_or_symbol": "AAPL",
                    "setup": "AAPL long-dated option swing",
                    "review_action": "review_now",
                    "review_label": "Review now",
                    "conviction_score": 87,
                    "reasons": ["momentum confirmation"],
                },
                {
                    "asset": "share",
                    "ticker_or_symbol": "WAIT",
                    "setup": "WAIT weak row",
                    "review_action": "wait",
                    "conviction_score": 40,
                },
            ],
        }

    cockpit_module.build_swing_scout = fake_swing
    try:
        with tempfile.TemporaryDirectory() as td:
            queue = build_action_queue(Path(td))
    finally:
        cockpit_module.build_data_health = old_health
        cockpit_module.build_positions = old_positions
        cockpit_module.build_paper_candidates = old_paper
        cockpit_module.load_watchlist = old_watchlist
        cockpit_module.build_swing_scout = old_swing

    rows = [row for row in queue["rows"] if row["category"] == "swing_scout"]
    assert len(rows) == 1
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["action"] == "scan_swing_chain"
    assert rows[0]["priority"] >= 70
    assert "conviction 87" in rows[0]["detail"]
    assert swing_kwargs["include_nasdaq_movers"] is True


def test_action_queue_uses_best_setup_decision_row_not_raw_top():
    old_health = cockpit_module.build_data_health
    old_positions = cockpit_module.build_positions
    old_paper = cockpit_module.build_paper_candidates
    old_watchlist = cockpit_module.load_watchlist
    old_swing = cockpit_module.build_swing_scout
    old_best = cockpit_module.build_best_setups
    old_halts = cockpit_module.halt_rows_for_symbols
    old_thresholds = cockpit_module.threshold_rows_for_symbols
    old_circuits = cockpit_module.circuit_rows_for_symbols

    cockpit_module.build_data_health = lambda *args, **kwargs: {"checks": [], "status": "ok"}
    cockpit_module.build_positions = lambda *args, **kwargs: {"rows": []}
    cockpit_module.build_paper_candidates = lambda *args, **kwargs: {"rows": []}
    cockpit_module.load_watchlist = lambda *args, **kwargs: {"entries": []}
    cockpit_module.build_swing_scout = lambda *args, **kwargs: {"rows": []}
    cockpit_module.halt_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame()
    cockpit_module.threshold_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame()
    cockpit_module.circuit_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame()
    cockpit_module.build_best_setups = lambda *args, **kwargs: {
        "rows": [
            {
                "asset": "option",
                "ticker_or_symbol": "STALE",
                "setup": "STALE high raw score",
                "score": 99,
                "setup_gate_status": "avoid",
                "setup_gate_label": "Avoid for now",
                "setup_gate_reasons": ["snapshot is stale"],
                "setup_gate_next_step": "Skip this setup until the blocking issue clears.",
            },
            {
                "asset": "option",
                "ticker_or_symbol": "CLEAN",
                "setup": "CLEAN 3m+ swing call",
                "score": 72,
                "setup_gate_status": "ready",
                "setup_gate_label": "Ready to research",
                "setup_gate_reasons": ["readiness is ready"],
                "setup_gate_next_step": "Open research; if thesis and live quote agree, consider paper tracking.",
            },
        ],
        "decision_row": {
            "asset": "option",
            "ticker_or_symbol": "CLEAN",
            "setup": "CLEAN 3m+ swing call",
            "score": 72,
            "setup_gate_status": "ready",
            "setup_gate_label": "Ready to research",
            "setup_gate_reasons": ["readiness is ready"],
            "setup_gate_next_step": "Open research; if thesis and live quote agree, consider paper tracking.",
        },
    }
    try:
        with tempfile.TemporaryDirectory() as td:
            queue = build_action_queue(Path(td))
    finally:
        cockpit_module.build_data_health = old_health
        cockpit_module.build_positions = old_positions
        cockpit_module.build_paper_candidates = old_paper
        cockpit_module.load_watchlist = old_watchlist
        cockpit_module.build_swing_scout = old_swing
        cockpit_module.build_best_setups = old_best
        cockpit_module.halt_rows_for_symbols = old_halts
        cockpit_module.threshold_rows_for_symbols = old_thresholds
        cockpit_module.circuit_rows_for_symbols = old_circuits

    rows = [row for row in queue["rows"] if row["category"] == "best_setup"]
    assert len(rows) == 1
    assert rows[0]["symbol"] == "CLEAN"
    assert rows[0]["action"] == "scan_swing_chain"
    assert rows[0]["setup_gate_status"] == "ready"
    assert rows[0]["setup_gate_label"] == "Ready to research"
    assert "CLEAN 3m+ swing call" in rows[0]["detail"]
    assert not any(row["symbol"] == "STALE" and row["category"] == "best_setup" for row in queue["rows"])


def test_action_queue_marks_avoid_only_best_setup_as_held():
    old_health = cockpit_module.build_data_health
    old_positions = cockpit_module.build_positions
    old_paper = cockpit_module.build_paper_candidates
    old_watchlist = cockpit_module.load_watchlist
    old_swing = cockpit_module.build_swing_scout
    old_best = cockpit_module.build_best_setups
    old_halts = cockpit_module.halt_rows_for_symbols
    old_thresholds = cockpit_module.threshold_rows_for_symbols
    old_circuits = cockpit_module.circuit_rows_for_symbols

    cockpit_module.build_data_health = lambda *args, **kwargs: {"checks": [], "status": "ok"}
    cockpit_module.build_positions = lambda *args, **kwargs: {"rows": []}
    cockpit_module.build_paper_candidates = lambda *args, **kwargs: {"rows": []}
    cockpit_module.load_watchlist = lambda *args, **kwargs: {"entries": []}
    cockpit_module.build_swing_scout = lambda *args, **kwargs: {"rows": []}
    cockpit_module.halt_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame()
    cockpit_module.threshold_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame()
    cockpit_module.circuit_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame()
    cockpit_module.build_best_setups = lambda *args, **kwargs: {
        "rows": [{
            "asset": "option",
            "ticker_or_symbol": "RISKY",
            "setup": "RISKY short-dated option",
            "score": 80,
            "setup_gate_status": "avoid",
            "setup_gate_label": "Avoid for now",
            "setup_gate_reasons": ["below 90 dte"],
            "setup_gate_next_step": "Skip this setup until the blocking issue clears.",
        }],
        "decision_row": {
            "asset": "option",
            "ticker_or_symbol": "RISKY",
            "setup": "RISKY short-dated option",
            "score": 80,
            "setup_gate_status": "avoid",
            "setup_gate_label": "Avoid for now",
            "setup_gate_reasons": ["below 90 dte"],
            "setup_gate_next_step": "Skip this setup until the blocking issue clears.",
        },
    }
    try:
        with tempfile.TemporaryDirectory() as td:
            queue = build_action_queue(Path(td))
    finally:
        cockpit_module.build_data_health = old_health
        cockpit_module.build_positions = old_positions
        cockpit_module.build_paper_candidates = old_paper
        cockpit_module.load_watchlist = old_watchlist
        cockpit_module.build_swing_scout = old_swing
        cockpit_module.build_best_setups = old_best
        cockpit_module.halt_rows_for_symbols = old_halts
        cockpit_module.threshold_rows_for_symbols = old_thresholds
        cockpit_module.circuit_rows_for_symbols = old_circuits

    rows = [row for row in queue["rows"] if row["category"] == "best_setup"]
    assert len(rows) == 1
    assert rows[0]["label"] == "Best setup is held"
    assert rows[0]["action"] == "open_research"
    assert rows[0]["setup_gate_status"] == "avoid"
    assert "below 90 dte" in rows[0]["detail"]


def test_action_queue_validation_guard_reroutes_fresh_entry_actions():
    old_health = cockpit_module.build_data_health
    old_positions = cockpit_module.build_positions
    old_paper = cockpit_module.build_paper_candidates
    old_watchlist = cockpit_module.load_watchlist
    old_swing = cockpit_module.build_swing_scout
    old_halts = cockpit_module.halt_rows_for_symbols
    old_thresholds = cockpit_module.threshold_rows_for_symbols
    old_circuits = cockpit_module.circuit_rows_for_symbols

    cockpit_module.build_data_health = lambda *args, **kwargs: {
        "status": "bad",
        "validation_guardrail": {
            "level": "bad",
            "label": "Validation guardrail blocking entries",
            "detail": "Max drawdown is -72.4%. Win rate is 11.7%.",
            "closed_positions": 1000,
            "win_rate": 0.117,
            "max_drawdown": -0.724,
            "profit_factor": 2.35,
        },
        "checks": [],
    }
    cockpit_module.build_positions = lambda *args, **kwargs: {"rows": []}
    cockpit_module.build_paper_candidates = lambda *args, **kwargs: {
        "rows": [{
            "ticker_or_symbol": "AAPL",
            "asset": "option",
            "confidence": 88,
        }],
    }
    cockpit_module.load_watchlist = lambda *args, **kwargs: {"entries": []}
    cockpit_module.build_swing_scout = lambda *args, **kwargs: {
        "rows": [{
            "asset": "option",
            "ticker_or_symbol": "OBAI",
            "setup": "OBAI small-cap mover",
            "review_action": "review_now",
            "review_label": "Review now",
            "conviction_score": 95,
            "reasons": ["momentum confirmation"],
        }],
    }
    cockpit_module.halt_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame()
    cockpit_module.threshold_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame()
    cockpit_module.circuit_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame()
    try:
        with tempfile.TemporaryDirectory() as td:
            queue = build_action_queue(Path(td))
    finally:
        cockpit_module.build_data_health = old_health
        cockpit_module.build_positions = old_positions
        cockpit_module.build_paper_candidates = old_paper
        cockpit_module.load_watchlist = old_watchlist
        cockpit_module.build_swing_scout = old_swing
        cockpit_module.halt_rows_for_symbols = old_halts
        cockpit_module.threshold_rows_for_symbols = old_thresholds
        cockpit_module.circuit_rows_for_symbols = old_circuits

    guarded = [row for row in queue["rows"] if row.get("guarded_by_validation")]
    assert queue["validation_guardrail"]["level"] == "bad"
    assert {row["symbol"] for row in guarded} == {"AAPL", "OBAI"}
    assert all(row["action"] == "review_data_health" for row in guarded)
    assert all(row["route"] == "data_health" for row in guarded)
    assert all(row["label"] == "Validation-blocked candidate" for row in guarded)
    assert all(row["blocked_reason"].startswith("Max drawdown") for row in guarded)
    assert {row["original_action"] for row in guarded} == {"preview_paper_candidate", "scan_swing_chain"}
    assert not any(row["action"] in {"preview_paper_candidate", "scan_swing_chain"} for row in queue["rows"])


def test_today_review_combines_setups_saved_contracts_and_risk():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        old_gated = cockpit_module.build_climate_gated_setups
        old_saved = cockpit_module.build_saved_option_contracts
        old_risk = cockpit_module.build_risk_summary
        old_queue = cockpit_module.build_action_queue
        old_swing = cockpit_module.build_swing_scout
        swing_kwargs = {}

        def fake_gated(*args, **kwargs):
            return {
                "climate_label": "constructive_selective",
                "climate_score": 68,
                "rows": [{
                    "ticker_or_symbol": "AAPL",
                    "asset": "option",
                    "setup": "AAPL swing call",
                    "climate_gate_score": 86,
                    "readiness_score": 82,
                    "climate_gate_reasons": ["passes DTE gate", "spread acceptable"],
                }],
                "held": [],
            }

        def fake_saved(*args, **kwargs):
            return {
                "rows": [{
                    "symbol": "AAPL",
                    "query": "AAPL 2026-10-16 C 220",
                    "side": "call",
                    "side_code": "C",
                    "expiry": "2026-10-16",
                    "strike": 220,
                    "review_action": "refresh_quote",
                    "review_score": 74,
                    "review_reasons": ["quote not checked"],
                }],
            }

        def fake_risk(*args, **kwargs):
            return {
                "highest_exit_pressure": [{
                    "ticker_or_symbol": "TSLA",
                    "asset": "option",
                    "position_label": "TSLA open call",
                    "latest_exit_pressure": 85,
                    "pnl_pct": -0.25,
                }],
                "warnings": ["TSLA concentration is high."],
            }

        def fake_queue(*args, **kwargs):
            return {
                "rows": [{
                    "priority": 70,
                    "category": "data_health",
                    "label": "SEC ticker cache missing",
                    "detail": "Warm the free company cache.",
                    "action": "warm_sec_ticker_cache",
                }],
            }

        def fake_swing(*args, **kwargs):
            swing_kwargs.update(kwargs)
            return {
                "rows": [{
                    "asset": "share",
                    "ticker_or_symbol": "SMOL",
                    "setup": "SMOL small-cap squeeze watch",
                    "review_action": "review_now",
                    "review_label": "Review now",
                    "conviction_score": 88,
                    "swing_scout_score": 92,
                    "reasons": ["short/squeeze pressure", "retail/attention lift"],
                }],
            }

        cockpit_module.build_climate_gated_setups = fake_gated
        cockpit_module.build_saved_option_contracts = fake_saved
        cockpit_module.build_risk_summary = fake_risk
        cockpit_module.build_action_queue = fake_queue
        cockpit_module.build_swing_scout = fake_swing
        try:
            review = build_today_review(data_dir, limit=8)
        finally:
            cockpit_module.build_climate_gated_setups = old_gated
            cockpit_module.build_saved_option_contracts = old_saved
            cockpit_module.build_risk_summary = old_risk
            cockpit_module.build_action_queue = old_queue
            cockpit_module.build_swing_scout = old_swing

        categories = {row["category"] for row in review["rows"]}
        actions = {row["action"] for row in review["rows"]}
        assert review["climate_label"] == "constructive_selective"
        assert review["setup_count"] == 1
        assert review["saved_contract_count"] == 1
        assert review["risk_count"] == 2
        assert review["swing_scout_count"] == 1
        assert "setup" in categories
        assert "saved_contract" in categories
        assert "position_risk" in categories
        assert "swing_scout" in categories
        assert "scan_swing_chain" in actions
        assert "refresh_saved_quote" in actions
        assert "open_position_monitor" in actions
        assert any(row["route"] == "chains" for row in review["rows"])
        swing_row = next(row for row in review["rows"] if row["category"] == "swing_scout")
        assert swing_row["symbol"] == "SMOL"
        assert swing_row["action"] == "scan_swing_chain"
        assert swing_row["route"] == "chains"
        assert "conviction 88" in swing_row["detail"]
        assert swing_kwargs["include_nasdaq_movers"] is True


def test_today_review_validation_guard_reroutes_fresh_entry_actions():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        old_health = cockpit_module.build_data_health
        old_gated = cockpit_module.build_climate_gated_setups
        old_saved = cockpit_module.build_saved_option_contracts
        old_risk = cockpit_module.build_risk_summary
        old_queue = cockpit_module.build_action_queue
        old_swing = cockpit_module.build_swing_scout

        cockpit_module.build_data_health = lambda *args, **kwargs: {
            "status": "bad",
            "validation_guardrail": {
                "level": "bad",
                "label": "Validation guardrail blocking entries",
                "detail": "Max drawdown is -72.4%. Win rate is 11.7%.",
                "closed_positions": 1000,
                "win_rate": 0.117,
                "max_drawdown": -0.724,
                "profit_factor": 2.35,
            },
            "checks": [],
        }
        cockpit_module.build_climate_gated_setups = lambda *args, **kwargs: {
            "climate_label": "aggressive_swing",
            "climate_score": 100,
            "rows": [{
                "ticker_or_symbol": "AAPL",
                "asset": "option",
                "setup": "AAPL clean swing call",
                "climate_gate_score": 95,
                "readiness_score": 90,
                "climate_gate_reasons": ["passes DTE gate"],
            }],
            "held": [],
        }
        cockpit_module.build_saved_option_contracts = lambda *args, **kwargs: {
            "rows": [{
                "symbol": "MSFT",
                "query": "MSFT 2026-10-16 C 500",
                "side": "call",
                "side_code": "C",
                "expiry": "2026-10-16",
                "strike": 500,
                "review_action": "refresh_quote",
                "review_score": 80,
                "review_reasons": ["quote stale"],
            }],
        }
        cockpit_module.build_risk_summary = lambda *args, **kwargs: {"highest_exit_pressure": [], "warnings": []}
        cockpit_module.build_action_queue = lambda *args, **kwargs: {"rows": []}
        cockpit_module.build_swing_scout = lambda *args, **kwargs: {
            "rows": [{
                "asset": "option",
                "ticker_or_symbol": "NVDA",
                "setup": "NVDA swing scout call",
                "review_action": "review_now",
                "review_label": "Review now",
                "conviction_score": 95,
                "swing_scout_score": 100,
                "reasons": ["momentum confirmation"],
            }],
        }
        try:
            review = build_today_review(data_dir, limit=6)
        finally:
            cockpit_module.build_data_health = old_health
            cockpit_module.build_climate_gated_setups = old_gated
            cockpit_module.build_saved_option_contracts = old_saved
            cockpit_module.build_risk_summary = old_risk
            cockpit_module.build_action_queue = old_queue
            cockpit_module.build_swing_scout = old_swing

        guarded = [row for row in review["rows"] if row.get("guarded_by_validation")]
        assert review["validation_guardrail"]["level"] == "bad"
        assert review["review_now_count"] == 1
        assert len(guarded) == 2
        assert {row["symbol"] for row in guarded} == {"AAPL", "NVDA"}
        assert all(row["action"] == "review_data_health" for row in guarded)
        assert all(row["route"] == "data_health" for row in guarded)
        assert all(row["original_action"] == "scan_swing_chain" for row in guarded)
        assert all(row["label"] == "Validation-blocked candidate" for row in guarded)
        assert all(row["blocked_reason"].startswith("Max drawdown") for row in guarded)
        assert all("Original setup context" in row["detail"] for row in guarded)

        refresh = next(row for row in review["rows"] if row["category"] == "saved_contract")
        assert refresh["symbol"] == "MSFT"
        assert refresh["action"] == "refresh_saved_quote"
        assert refresh["route"] == "chains"


def test_command_center_summarizes_next_action_and_data_trust():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        old_health = cockpit_module.build_data_health
        old_today = cockpit_module.build_today_review
        old_risk = cockpit_module.build_risk_summary
        old_sources = cockpit_module.build_free_data_sources
        old_perf = cockpit_module.build_performance_summary
        old_swing = cockpit_module.build_swing_scout
        old_rh = cockpit_module.build_robinhood_agentic_queue_report
        old_session = cockpit_module._build_session_plan
        swing_kwargs = {}

        cockpit_module.build_data_health = lambda *args, **kwargs: {
            "status": "warn",
            "total_open": 4,
            "checks": [
                {"level": "ok", "label": "Dashboard freshness", "detail": "fresh"},
                {"level": "warn", "label": "Validation older than dashboard", "detail": "refresh"},
            ],
        }
        cockpit_module.build_today_review = lambda *args, **kwargs: {
            "count": 2,
            "review_now_count": 1,
            "climate_label": "constructive_selective",
            "climate_score": 68,
            "climate_posture": "Constructive, but stay selective.",
            "rows": [{
                "priority": 96,
                "label": "Review saved option contract",
                "detail": "AAPL 180d call has clean readiness.",
                "action": "scan_swing_chain",
                "route": "chains",
                "symbol": "AAPL",
                "query": "AAPL 2026-12-18 C 220",
                "source": "saved_option_contracts",
            }],
        }
        cockpit_module.build_risk_summary = lambda *args, **kwargs: {
            "risk_level": "elevated",
            "total_open": 4,
            "attention_count": 1,
            "high_exit_pressure_count": 0,
            "highest_exit_pressure": [{
                "asset": "option",
                "ticker_or_symbol": "AAPL",
                "position_label": "AAPL C 220",
                "latest_exit_pressure": 72,
                "pnl_pct": -0.12,
                "reprice_failed_count": 0,
            }],
        }
        cockpit_module.build_free_data_sources = lambda *args, **kwargs: {
            "source_count": 17,
            "no_key_count": 17,
            "primary_count": 10,
        }
        cockpit_module.build_performance_summary = lambda *args, **kwargs: {
            "total_latest_engine_sec": 92.4,
            "warnings": [],
        }
        cockpit_module.build_robinhood_agentic_queue_report = lambda *args, **kwargs: {
            "status": "ready",
            "account_budget": 500,
            "max_total_premium": 250,
            "min_dte": 90,
            "candidate_count": 1,
            "rejected_count": 3,
            "orders": [{"symbol": "AAPL", "contract": "AAPL 2026-12-18 C 220"}],
            "readiness": {"label": "ready", "ready_to_submit_count": 1, "premium_cap_remaining": 150},
            "diagnostics": {"label": "ready_guarded", "remediation": []},
            "chain_refresh": {"attempted": False},
            "sec_offering_risks": {},
            "top_rejection_reasons": [{"reason": "spread above filter", "count": 2}],
        }
        cockpit_module._build_session_plan = lambda: {
            "status": "active_window",
            "tone": "good",
            "label": "active window",
            "now_pt": "2026-06-17 09:30 AM PT",
            "window": "7:30 AM-1:00 PM PT",
            "cadence": "30 min scan loop",
            "min_option_dte": 90,
            "max_orders": 1,
            "next_step": "Run or keep the 30-minute loop active.",
            "recommended_command": "python run.py --aggressive --bankroll 500 --loop 30 --turbo --no-open",
            "notes": ["approval required"],
        }
        def fake_swing(*args, **kwargs):
            swing_kwargs.update(kwargs)
            return {
                "count": 1,
                "rows": [{
                    "asset": "option",
                    "ticker_or_symbol": "AAPL",
                    "setup": "AAPL 180d call swing",
                    "lane": "long_dated_option_swing",
                    "swing_scout_score": 88,
                    "review_action": "shortlist",
                    "review_label": "Shortlist",
                    "conviction_score": 78,
                    "readiness_label": "review",
                    "snapshot_freshness": "fresh",
                    "reasons": ["3m+ runway", "momentum confirmation"],
                    "warnings": ["verify delayed quote"],
                    "factor_breakdown": [
                        {"factor": "Momentum", "score": 18.5, "detail": "20d 12%, rank 2.0"},
                        {"factor": "Execution", "score": 15.0, "detail": "180d DTE, spread 8%"},
                    ],
                    "factor_summary": "Momentum + Execution",
                }],
            }

        cockpit_module.build_swing_scout = fake_swing
        try:
            center = build_command_center(data_dir)
        finally:
            cockpit_module.build_data_health = old_health
            cockpit_module.build_today_review = old_today
            cockpit_module.build_risk_summary = old_risk
            cockpit_module.build_free_data_sources = old_sources
            cockpit_module.build_performance_summary = old_perf
            cockpit_module.build_swing_scout = old_swing
            cockpit_module.build_robinhood_agentic_queue_report = old_rh
            cockpit_module._build_session_plan = old_session

        assert center["status"] == "review_first"
        assert center["climate_label"] == "constructive_selective"
        assert center["data_health_status"] == "warn"
        assert center["health_counts"] == {"ok": 1, "warn": 1, "bad": 0}
        assert center["next_action"]["action"] == "scan_swing_chain"
        assert center["next_action"]["route"] == "chains"
        assert center["top_queue"][0]["action"] == "scan_swing_chain"
        assert center["top_queue"][0]["route"] == "chains"
        assert center["no_key_count"] == 17
        assert len(center["cards"]) == 8
        assert center["session_plan"]["status"] == "active_window"
        assert center["session_plan"]["min_option_dte"] == 90
        assert "--loop 30" in center["session_plan"]["recommended_command"]
        assert center["position_triage"][0]["symbol"] == "AAPL"
        assert center["position_triage"][0]["action"] == "open_position_monitor"
        assert center["position_triage"][0]["tone"] == "warn"
        assert center["manual_review"]["label"] == "Ready: 1 candidate(s)"
        assert center["manual_review"]["route"] == "robinhood"
        assert center["manual_review"]["min_dte"] == 90
        assert "1 ready-to-review" in center["manual_review"]["checks"][0]
        assert center["swing_radar_count"] == 1
        assert center["swing_actions"][0]["action"] == "scan_swing_chain"
        assert center["swing_actions"][0]["route"] == "chains"
        assert center["swing_actions"][0]["score"] == 88
        assert center["swing_actions"][0]["priority"] == 78
        assert center["swing_actions"][0]["conviction_score"] == 78
        assert center["swing_actions"][0]["review_action"] == "shortlist"
        assert center["swing_actions"][0]["factor_summary"] == "Momentum + Execution"
        assert "Factors: Momentum + Execution" in center["swing_actions"][0]["detail"]
        assert "conviction 78/100" in center["swing_actions"][0]["detail"]
        assert "3m+ runway" in center["swing_actions"][0]["detail"]
        assert len(center["trust_ribbon"]) == 7
        ribbon = {row["label"]: row for row in center["trust_ribbon"]}
        assert ribbon["Data integrity"]["value"] == "warn"
        assert ribbon["Validation alignment"]["value"] == "warn"
        assert ribbon["Chain readiness"]["value"] == "missing"
        assert ribbon["Free sources"]["value"] == "17/17"
        assert swing_kwargs["include_nasdaq_movers"] is True


def test_command_center_session_gate_defers_new_entries_after_review_window():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        old_health = cockpit_module.build_data_health
        old_today = cockpit_module.build_today_review
        old_risk = cockpit_module.build_risk_summary
        old_sources = cockpit_module.build_free_data_sources
        old_perf = cockpit_module.build_performance_summary
        old_swing = cockpit_module.build_swing_scout
        old_rh = cockpit_module.build_robinhood_agentic_queue_report
        old_session = cockpit_module._build_session_plan

        cockpit_module.build_data_health = lambda *args, **kwargs: {
            "status": "warn",
            "checks": [{"level": "warn", "label": "Dashboard freshness", "detail": "stale"}],
        }
        cockpit_module.build_today_review = lambda *args, **kwargs: {
            "count": 1,
            "review_now_count": 1,
            "climate_label": "constructive_selective",
            "climate_score": 70,
            "rows": [{
                "priority": 96,
                "label": "Review climate-cleared setup",
                "detail": "AAPL cleared setup gates.",
                "action": "scan_swing_chain",
                "route": "chains",
                "symbol": "AAPL",
                "query": "AAPL",
                "source": "climate_gated_setups",
            }],
        }
        cockpit_module.build_risk_summary = lambda *args, **kwargs: {
            "risk_level": "medium",
            "total_open": 1,
            "attention_count": 1,
            "high_exit_pressure_count": 0,
            "highest_exit_pressure": [{
                "asset": "option",
                "ticker_or_symbol": "TLT",
                "position_label": "TLT C 80",
                "latest_exit_pressure": 65,
                "pnl_pct": 0.22,
                "reprice_failed_count": 3,
            }],
        }
        cockpit_module.build_free_data_sources = lambda *args, **kwargs: {
            "source_count": 17,
            "no_key_count": 17,
            "primary_count": 10,
        }
        cockpit_module.build_performance_summary = lambda *args, **kwargs: {
            "total_latest_engine_sec": 50.0,
            "warnings": [],
        }
        cockpit_module.build_swing_scout = lambda *args, **kwargs: {"count": 0, "rows": []}
        cockpit_module.build_robinhood_agentic_queue_report = lambda *args, **kwargs: {
            "status": "ready",
            "candidate_count": 1,
            "rejected_count": 0,
            "orders": [{"symbol": "AAPL"}],
            "readiness": {"label": "ready", "ready_to_submit_count": 1, "premium_cap_remaining": 150},
            "diagnostics": {},
            "chain_refresh": {"attempted": False},
        }
        cockpit_module._build_session_plan = lambda: {
            "status": "post_window",
            "tone": "warn",
            "label": "post window",
            "now_pt": "2026-06-17 02:30 PM PT",
            "window": "7:30 AM-1:00 PM PT",
            "cadence": "30 min scan loop",
            "min_option_dte": 90,
            "max_orders": 1,
            "next_step": "Stop adding new ideas; review open risk.",
            "recommended_command": "python run.py --aggressive --bankroll 500 --loop 30",
            "notes": [],
        }

        try:
            center = build_command_center(data_dir)
        finally:
            cockpit_module.build_data_health = old_health
            cockpit_module.build_today_review = old_today
            cockpit_module.build_risk_summary = old_risk
            cockpit_module.build_free_data_sources = old_sources
            cockpit_module.build_performance_summary = old_perf
            cockpit_module.build_swing_scout = old_swing
            cockpit_module.build_robinhood_agentic_queue_report = old_rh
            cockpit_module._build_session_plan = old_session

        assert center["next_action"]["session_gate_applied"] is True
        assert center["next_action"]["action"] == "open_position_monitor"
        assert center["next_action"]["route"] == "positions"
        assert center["next_action"]["symbol"] == "TLT"
        assert center["next_action"]["original_action"] == "scan_swing_chain"
        assert "Stop adding new ideas" in center["next_action"]["detail"]


def test_command_center_validation_guard_defers_new_entries_during_active_window():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        old_health = cockpit_module.build_data_health
        old_today = cockpit_module.build_today_review
        old_risk = cockpit_module.build_risk_summary
        old_sources = cockpit_module.build_free_data_sources
        old_perf = cockpit_module.build_performance_summary
        old_swing = cockpit_module.build_swing_scout
        old_rh = cockpit_module.build_robinhood_agentic_queue_report
        old_session = cockpit_module._build_session_plan

        cockpit_module.build_data_health = lambda *args, **kwargs: {
            "status": "bad",
            "total_open": 1,
            "checks": [{"level": "bad", "label": "Validation guardrail blocking entries", "detail": "Max drawdown is -35.0%."}],
            "validation_guardrail": {
                "level": "bad",
                "label": "Validation guardrail blocking entries",
                "detail": "Max drawdown is -35.0%.",
                "closed_positions": 120,
                "win_rate": 0.18,
                "max_drawdown": -0.35,
                "profit_factor": 0.8,
            },
        }
        cockpit_module.build_today_review = lambda *args, **kwargs: {
            "count": 1,
            "review_now_count": 1,
            "climate_label": "constructive_selective",
            "climate_score": 74,
            "rows": [{
                "priority": 97,
                "label": "Review climate-cleared setup",
                "detail": "AAPL cleared setup gates.",
                "action": "scan_swing_chain",
                "route": "chains",
                "symbol": "AAPL",
                "query": "AAPL",
                "source": "climate_gated_setups",
            }],
        }
        cockpit_module.build_risk_summary = lambda *args, **kwargs: {
            "risk_level": "low",
            "total_open": 1,
            "attention_count": 0,
            "high_exit_pressure_count": 0,
            "highest_exit_pressure": [{
                "asset": "option",
                "ticker_or_symbol": "MSFT",
                "position_label": "MSFT C 400",
                "latest_exit_pressure": 35,
                "pnl_pct": 0.12,
                "reprice_failed_count": 0,
            }],
        }
        cockpit_module.build_free_data_sources = lambda *args, **kwargs: {
            "source_count": 17,
            "no_key_count": 17,
            "primary_count": 10,
        }
        cockpit_module.build_performance_summary = lambda *args, **kwargs: {
            "total_latest_engine_sec": 40.0,
            "warnings": [],
        }
        cockpit_module.build_swing_scout = lambda *args, **kwargs: {"count": 0, "rows": []}
        cockpit_module.build_robinhood_agentic_queue_report = lambda *args, **kwargs: {
            "status": "ready",
            "candidate_count": 1,
            "rejected_count": 0,
            "orders": [{"symbol": "AAPL"}],
            "readiness": {"label": "ready", "ready_to_submit_count": 1},
            "diagnostics": {},
            "chain_refresh": {"attempted": False},
        }
        cockpit_module._build_session_plan = lambda: {
            "status": "active_window",
            "tone": "good",
            "label": "active window",
            "now_pt": "2026-06-17 09:30 AM PT",
            "window": "7:30 AM-1:00 PM PT",
            "cadence": "30 min scan loop",
            "min_option_dte": 90,
            "max_orders": 1,
            "next_step": "Run or keep the 30-minute loop active.",
            "recommended_command": "python run.py --aggressive --bankroll 500 --loop 30",
            "notes": [],
        }

        try:
            center = build_command_center(data_dir)
        finally:
            cockpit_module.build_data_health = old_health
            cockpit_module.build_today_review = old_today
            cockpit_module.build_risk_summary = old_risk
            cockpit_module.build_free_data_sources = old_sources
            cockpit_module.build_performance_summary = old_perf
            cockpit_module.build_swing_scout = old_swing
            cockpit_module.build_robinhood_agentic_queue_report = old_rh
            cockpit_module._build_session_plan = old_session

        assert center["status"] == "fix_first"
        assert center["validation_guardrail"]["level"] == "bad"
        assert center["next_action"]["validation_guard_applied"] is True
        assert center["next_action"]["session_gate_applied"] is False
        assert center["next_action"]["source"] == "validation_guard"
        assert center["next_action"]["action"] == "open_position_monitor"
        assert center["next_action"]["route"] == "positions"
        assert center["next_action"]["symbol"] == "MSFT"
        assert center["next_action"]["original_action"] == "scan_swing_chain"
        assert center["top_queue"][0]["action"] == "review_data_health"
        assert center["top_queue"][0]["route"] == "data_health"
        assert center["top_queue"][0]["original_action"] == "scan_swing_chain"
        assert center["top_queue"][0]["guarded_by_validation"] is True
        assert center["top_queue"][0]["label"] == "Validation-blocked candidate"
        assert center["top_queue"][0]["original_label"] == "Review climate-cleared setup"
        assert center["top_queue"][0]["blocked_reason"] == "Max drawdown is -35.0%."
        assert "Original setup context" in center["top_queue"][0]["detail"]
        assert center["manual_review"]["label"] == "Blocked by validation"
        assert center["manual_review"]["tone"] == "bad"
        assert center["manual_review"]["ready_to_submit_count"] == 0
        assert center["manual_review"]["guarded_candidate_count"] == 1
        assert center["manual_review"]["route"] == "data_health"
        assert "Validation guardrail blocked" in center["manual_review"]["checks"][0]


def test_manual_review_summary_surfaces_entry_gate_state():
    manual = cockpit_module._command_manual_review_summary({
        "status": "ready",
        "candidate_count": 1,
        "rejected_count": 3,
        "max_total_premium": 250,
        "min_dte": 90,
        "orders": [{"symbol": "AAPL"}],
        "readiness": {"ready_to_submit_count": 1, "premium_cap_remaining": 150},
        "entry_gate": {
            "status": "review_only",
            "label": "Approval-required review",
            "detail": "Fresh entries need manual approval after validation review.",
            "new_entries_allowed_after_live_checks": False,
            "approval_required": True,
        },
        "gated_ready_to_submit_count": 0,
        "review_only_entry_candidate_count": 1,
        "decision_log": {
            "recent_count": 2,
            "path": str(Path("data") / "robinhood_agentic_decisions.jsonl"),
        },
        "chain_refresh": {"attempted": True, "ok": True},
    })

    assert manual["label"] == "Review-only: 1 candidate(s)"
    assert manual["tone"] == "warn"
    assert manual["ready_to_submit_count"] == 0
    assert manual["review_only_entry_candidate_count"] == 1
    assert manual["entry_gate_label"] == "Approval-required review"
    assert manual["decision_log_recent_count"] == 2
    assert manual["route"] == "robinhood"
    assert manual["checks"][0] == "Entry gate: Approval-required review"
    assert any("2 recent local decision" in check for check in manual["checks"])

    blocked = cockpit_module._command_manual_review_summary({
        "status": "ready",
        "candidate_count": 1,
        "orders": [{"symbol": "AAPL"}],
        "entry_gate": {
            "status": "blocked",
            "label": "Fresh entries blocked",
            "detail": "Validation blockers need review first.",
        },
        "gated_ready_to_submit_count": 0,
        "review_only_entry_candidate_count": 1,
    })
    assert blocked["label"] == "Fresh entries blocked"
    assert blocked["tone"] == "bad"
    assert blocked["route"] == "data_health"


def test_validation_guardrail_uses_summary_closed_count_with_overall_metrics():
    guard = cockpit_module._validation_guardrail({
        "closed_positions": 1000,
        "overall": {
            "n": 1000,
            "win_rate": 0.117,
            "max_drawdown": -1.0,
            "profit_factor": 2.34,
        },
    })

    assert guard["closed_positions"] == 1000
    assert guard["win_rate"] == 0.117
    assert guard["max_drawdown"] == -1.0
    assert guard["level"] == "bad"
    assert "Max drawdown" in guard["detail"]
    assert "Only 0 closed" not in " ".join(guard["warnings"])


def test_swing_packet_builds_and_writes_daily_decision_packet():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        old_command = cockpit_module.build_command_center
        old_today = cockpit_module.build_today_review
        old_climate = cockpit_module.build_swing_climate
        old_gated = cockpit_module.build_climate_gated_setups
        old_paper = cockpit_module.build_paper_candidates
        old_sec = cockpit_module.build_watchlist_sec_filings
        old_provider = cockpit_module.build_provider_status

        cockpit_module.build_command_center = lambda *args, **kwargs: {
            "status": "ready_to_review",
            "status_detail": "Review the cleanest setup before opening anything new.",
            "data_health_status": "ok",
            "risk_level": "normal",
            "total_open": 3,
            "source_count": 18,
            "no_key_count": 18,
            "climate_label": "constructive_selective",
            "climate_score": 68,
            "next_action": {
                "label": "Review climate-cleared setup",
                "detail": "AAPL cleared the climate gate.",
                "action": "scan_swing_chain",
                "route": "chains",
                "symbol": "AAPL",
                "query": "AAPL",
                "source": "climate_gated_setups",
            },
            "cards": [],
        }
        cockpit_module.build_today_review = lambda *args, **kwargs: {
            "count": 1,
            "review_now_count": 1,
            "rows": [{
                "priority": 94,
                "category": "setup",
                "label": "Review climate-cleared setup",
                "detail": "AAPL passed.",
                "action": "scan_swing_chain",
                "route": "chains",
                "symbol": "AAPL",
                "query": "AAPL",
                "source": "climate_gated_setups",
                "asset": "option",
            }],
        }
        cockpit_module.build_swing_climate = lambda *args, **kwargs: {
            "climate_label": "constructive_selective",
            "climate_score": 68,
            "posture": "Selective risk-on.",
            "warnings": [],
            "trade_gates": [{"gate": "option DTE", "value": "90+"}],
            "asset_bias": [{"asset": "options", "bias": "selective"}],
        }
        cockpit_module.build_climate_gated_setups = lambda *args, **kwargs: {
            "selected_count": 1,
            "held_count": 0,
            "rows": [{
                "asset": "option",
                "ticker_or_symbol": "AAPL",
                "setup": "AAPL swing call",
                "readiness_score": 88,
                "climate_gate_score": 86,
                "climate_gate_status": "pass",
                "climate_gate_reasons": ["DTE fits", "spread acceptable"],
                "trade_status": "Trade",
                "confidence": 72,
                "rank_score": 2.1,
                "dte": 210,
                "spread_pct": 0.04,
            }],
            "held": [],
        }
        cockpit_module.build_paper_candidates = lambda *args, **kwargs: {
            "selected_count": 1,
            "excluded_count": 0,
            "top_rejection_reasons": [],
            "rows": [{
                "asset": "option",
                "ticker_or_symbol": "AAPL",
                "action": "BUY_TO_OPEN",
                "quantity": 1,
                "contract": "AAPL 2027-01-15 C 220",
                "option_side": "call",
                "strike": 220,
                "expiry": "2027-01-15",
                "entry_price": 5.0,
                "stop_price": 2.5,
                "target_price": 10.0,
                "confidence": 72,
                "rank_score": 2.1,
                "trade_status": "Trade",
                "reason_selected": "passed filters",
            }],
        }
        cockpit_module.build_watchlist_sec_filings = lambda *args, **kwargs: {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "symbols_checked": 1,
            "filing_count": 1,
            "fresh_count": 1,
            "high_impact_count": 1,
            "error_count": 0,
            "rows": [{
                "priority": 99,
                "ticker": "AAPL",
                "company_name": "Apple Inc.",
                "form": "S-3",
                "filing_date": datetime.now(timezone.utc).date().isoformat(),
                "days_old": 0,
                "freshness": "fresh",
                "signal": "dilution_or_offering_watch",
                "description": "Shelf registration statement",
                "url": "https://www.sec.gov/aapl",
            }],
            "notes": [],
        }
        cockpit_module.build_provider_status = lambda *args, **kwargs: {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "query": kwargs.get("query") or "AAPL",
            "symbol": "AAPL",
            "status": "ok",
            "ok_count": 4,
            "provider_count": 5,
            "data_trust": {
                "label": "ready",
                "score": 88,
                "history_ok_count": 2,
                "history_provider_count": 3,
                "history_source_summary": "yahoo_chart, nasdaq_historical",
                "history_quality_counts": {"free_or_delayed": 2},
                "option_chain_status": "skipped",
                "symbol_cache_ok_count": 2,
            },
            "rows": [
                {
                    "provider": "Yahoo chart",
                    "category": "history",
                    "status": "ok",
                    "latency_ms": 12.0,
                    "rows": 22,
                    "history_source": "yahoo_chart",
                    "history_quality": "free_or_delayed",
                    "last_close": 200.0,
                    "note": "Returned OHLCV rows.",
                },
                {
                    "provider": "Nasdaq historical",
                    "category": "history",
                    "status": "ok",
                    "latency_ms": 9.0,
                    "rows": 22,
                    "history_source": "nasdaq_historical",
                    "history_quality": "free_or_delayed",
                    "last_close": 200.0,
                    "note": "Returned OHLCV rows.",
                },
            ],
            "warnings": [],
        }
        (data_dir / "option_chain_shortlist.json").write_text(json.dumps({
            "generated_at": "2026-06-13T20:00:00+00:00",
            "rows": [{
                "symbol": "AAPL",
                "contract_query": "AAPL 2027-01-15 C 220",
                "side": "call",
                "strike": 220,
                "expiry": "2027-01-15",
                "dte": 216,
                "bid": 4.9,
                "ask": 5.1,
                "mid": 5.0,
                "premium_dollars": 500,
                "spread_pct": 0.04,
                "openInterest": 1200,
                "volume": 80,
                "breakeven_price": 225.0,
                "breakeven_move_pct": 0.125,
                "budget_usage_pct": 1.0,
                "contracts_for_budget": 1,
                "risk_dollars_reference": 250.0,
                "reward_dollars_reference": 500.0,
                "reward_risk_reference": 2.0,
                "budget_fit": "inside_budget",
                "contract_grade": "A",
                "review_lane": "primary_review",
                "readiness_score": 92,
                "contract_quality_score": 94,
                "swing_fit_score": 96,
                "swing_fit_label": "clean_swing",
                "review_thesis": "A-grade test contract.",
                "chain_source": "cboe",
                "quote_quality": "free_or_delayed",
            }],
        }), encoding="utf-8")
        earnings_date = (datetime.now(timezone.utc).date() + timedelta(days=3)).isoformat()
        pd.DataFrame([{
            "ticker": "AAPL",
            "contract": "AAPL 2027-01-15 C 220",
            "next_earnings_date": earnings_date,
            "days_to_earnings": 3,
            "earnings_score": -0.2,
            "whisper_score": 0.4,
            "whisper_gap_pct": 0.09,
            "rank_score": 2.1,
            "confidence": 72,
            "dte": 216,
        }]).to_parquet(data_dir / "top_options_20260613_200000.parquet")
        try:
            packet = build_swing_packet(data_dir, write=True)
        finally:
            cockpit_module.build_command_center = old_command
            cockpit_module.build_today_review = old_today
            cockpit_module.build_swing_climate = old_climate
            cockpit_module.build_climate_gated_setups = old_gated
            cockpit_module.build_paper_candidates = old_paper
            cockpit_module.build_watchlist_sec_filings = old_sec
            cockpit_module.build_provider_status = old_provider

        assert packet["does_not_place_orders"] is True
        assert packet["wrote_files"] is True
        assert packet["headline"].startswith("Review climate-cleared setup")
        assert packet["paper_candidates"]["selected_count"] == 1
        assert packet["chain_shortlist"]["count"] == 1
        assert packet["chain_shortlist"]["rows"][0]["contract"] == "AAPL 2027-01-15 C 220"
        assert packet["chain_shortlist"]["rows"][0]["openInterest"] == 1200
        assert packet["chain_shortlist"]["rows"][0]["breakeven_move_pct"] == 0.125
        assert packet["chain_shortlist"]["rows"][0]["contract_grade"] == "A"
        assert packet["chain_shortlist"]["quality_summary"]["status"] == "clean"
        assert packet["chain_shortlist"]["quality_summary"]["score"] >= 80
        assert packet["chain_shortlist"]["quality_summary"]["primary_review_count"] == 1
        assert packet["chain_shortlist"]["quality_summary"]["liquid_count"] == 1
        assert packet["sec_dilution_risk"]["status"] == "block_new_bullish_options"
        assert packet["sec_dilution_risk"]["count"] == 1
        assert packet["sec_dilution_risk"]["rows"][0]["form"] == "S-3"
        assert packet["data_trust_check"]["symbol"] == "AAPL"
        assert packet["data_trust_check"]["data_trust"]["label"] == "ready"
        assert packet["data_trust_check"]["data_trust"]["history_source_summary"] == "yahoo_chart, nasdaq_historical"
        assert packet["event_risk"]["status"] == "high_event_risk"
        assert packet["event_risk"]["high_count"] == 1
        assert packet["event_risk"]["rows"][0]["symbol"] == "AAPL"
        assert packet["event_risk"]["rows"][0]["action"] == "avoid_new_option_entry_until_after_earnings_review"
        assert packet["decision_gate"]["status"] == "wait"
        assert packet["decision_gate"]["blocker_count"] >= 2
        assert any("High earnings" in item for item in packet["decision_gate"]["blockers"])
        assert any("SEC offering" in item for item in packet["decision_gate"]["blockers"])
        assert any("Chain quality is clean" in item for item in packet["decision_gate"]["confirmations"])
        json_path = data_dir / "swing_packet.json"
        md_path = data_dir / "swing_packet.md"
        assert json_path.exists()
        assert md_path.exists()
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        assert payload["does_not_place_orders"] is True
        md = md_path.read_text(encoding="utf-8")
        assert "No broker execution is performed" in md
        assert "AAPL 2027-01-15 C 220" in md
        assert "Decision Gate" in md
        assert "High earnings or catalyst event risk is active" in md
        assert "Focus Data Trust" in md
        assert "yahoo_chart, nasdaq_historical" in md
        assert "Earnings / Catalyst Event Risk" in md
        assert "avoid_new_option_entry_until_after_earnings_review" in md
        assert "Quality: clean" in md
        assert "SEC Dilution / Offering Risk" in md
        assert "S-3" in md


def test_swing_packet_can_refresh_chain_shortlist_on_demand():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        old_command = cockpit_module.build_command_center
        old_today = cockpit_module.build_today_review
        old_climate = cockpit_module.build_swing_climate
        old_gated = cockpit_module.build_climate_gated_setups
        old_paper = cockpit_module.build_paper_candidates
        old_batch = cockpit_module.build_option_chain_batch
        old_writer = cockpit_module.write_option_chain_shortlist
        old_provider = cockpit_module.build_provider_status
        calls = {"batch": 0, "writer": 0}

        cockpit_module.build_command_center = lambda *args, **kwargs: {
            "status": "ready_to_review",
            "status_detail": "Review the chain-refreshed packet.",
            "data_health_status": "ok",
            "risk_level": "normal",
            "total_open": 0,
            "source_count": 18,
            "no_key_count": 18,
            "climate_label": "constructive_selective",
            "climate_score": 65,
            "next_action": {"label": "Review local research", "route": "chains", "query": "AAPL"},
            "cards": [],
        }
        cockpit_module.build_today_review = lambda *args, **kwargs: {"count": 0, "rows": []}
        cockpit_module.build_swing_climate = lambda *args, **kwargs: {
            "climate_label": "constructive_selective",
            "climate_score": 65,
            "posture": "Selective.",
        }
        cockpit_module.build_climate_gated_setups = lambda *args, **kwargs: {"rows": [], "held": []}
        cockpit_module.build_paper_candidates = lambda *args, **kwargs: {
            "selected_count": 0,
            "excluded_count": 0,
            "rows": [],
        }
        cockpit_module.build_provider_status = lambda *args, **kwargs: {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "query": kwargs.get("query") or "AAPL",
            "symbol": "AAPL",
            "status": "ok",
            "ok_count": 3,
            "provider_count": 5,
            "data_trust": {
                "label": "ready",
                "score": 80,
                "history_ok_count": 1,
                "history_provider_count": 3,
                "history_source_summary": "yahoo_chart",
                "history_quality_counts": {"free_or_delayed": 1},
                "option_chain_status": "skipped",
                "symbol_cache_ok_count": 2,
            },
            "rows": [],
            "warnings": [],
        }

        def fake_batch(*args, **kwargs):
            calls["batch"] += 1
            assert kwargs["preset"] == "swing"
            assert kwargs["min_dte"] == 90
            assert kwargs["max_dte"] == 180
            assert kwargs["max_spread_pct"] == 0.20
            return {
                "ok": True,
                "symbols_scanned": 1,
                "successful_scans": 1,
                "row_count": 1,
                "rows": [{
                    "symbol": "AAPL",
                    "contract_query": "AAPL 2027-01-15 C 220",
                    "side": "call",
                    "strike": 220,
                    "expiry": "2027-01-15",
                    "dte": 216,
                    "bid": 4.9,
                    "ask": 5.1,
                    "mid": 5.0,
                    "premium_dollars": 500,
                    "spread_pct": 0.04,
                    "openInterest": 1200,
                    "volume": 80,
                    "breakeven_price": 225.0,
                    "breakeven_move_pct": 0.125,
                    "budget_usage_pct": 1.0,
                    "contracts_for_budget": 1,
                    "risk_dollars_reference": 250.0,
                    "reward_dollars_reference": 500.0,
                    "reward_risk_reference": 2.0,
                    "budget_fit": "inside_budget",
                    "contract_grade": "A",
                    "review_lane": "primary_review",
                    "readiness_score": 92,
                    "contract_quality_score": 94,
                    "swing_fit_score": 96,
                    "swing_fit_label": "clean_swing",
                    "review_thesis": "A-grade test contract.",
                    "chain_source": "cboe",
                    "quote_quality": "free_or_delayed",
                }],
            }

        def fake_writer(report, write_dir):
            calls["writer"] += 1
            (write_dir / "option_chain_shortlist.json").write_text(json.dumps({
                "generated_at": "2026-06-13T20:00:00+00:00",
                "rows": report["rows"],
            }), encoding="utf-8")
            return {"ok": True, "count": len(report["rows"])}

        cockpit_module.build_option_chain_batch = fake_batch
        cockpit_module.write_option_chain_shortlist = fake_writer
        try:
            packet = build_swing_packet(
                data_dir,
                write=True,
                refresh_chains=True,
                chain_symbols_limit=3,
                chain_contracts_per_symbol=2,
            )
        finally:
            cockpit_module.build_command_center = old_command
            cockpit_module.build_today_review = old_today
            cockpit_module.build_swing_climate = old_climate
            cockpit_module.build_climate_gated_setups = old_gated
            cockpit_module.build_paper_candidates = old_paper
            cockpit_module.build_option_chain_batch = old_batch
            cockpit_module.write_option_chain_shortlist = old_writer
            cockpit_module.build_provider_status = old_provider

        assert calls == {"batch": 1, "writer": 1}
        assert packet["chain_refresh"]["attempted"] is True
        assert packet["chain_refresh"]["row_count"] == 1
        assert packet["chain_refresh"]["exported"] is True
        assert packet["chain_shortlist"]["status"] == "ready"
        assert packet["chain_shortlist"]["count"] == 1
        assert packet["chain_shortlist"]["rows"][0]["contract"] == "AAPL 2027-01-15 C 220"
        assert packet["chain_shortlist"]["quality_summary"]["status"] == "clean"
        assert packet["data_trust_check"]["data_trust"]["label"] == "ready"
        assert packet["decision_gate"]["status"] in {"ready_to_review", "selective_review"}
        assert (data_dir / "swing_packet.json").exists()
        assert (data_dir / "swing_packet.md").exists()


def test_enriched_watchlist_sorts_ready_ideas_first():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        add_watchlist_query("Apple", data_dir)
        add_watchlist_query("Nvidia 20260618 C 200", data_dir)
        pd.DataFrame([{
            "ticker": "NVDA",
            "side": "call",
            "strike": 200.0,
            "expiry": "2026-06-18",
            "mid": 4.2,
            "confidence": 82,
            "rank_score": 2.5,
            "trade_status": "Trade",
            "chain_source": "tradier",
            "quote_quality": "live_or_broker",
        }]).to_parquet(data_dir / "top_options_20260603_120000.parquet")
        (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_share_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_futures_positions.json").write_text("[]", encoding="utf-8")

        plain = load_watchlist(data_dir, enrich=False)
        assert [row["symbol"] for row in plain["entries"]] == ["AAPL", "NVDA"]

        enriched = load_watchlist(data_dir, enrich=True)
        assert enriched["entries"][0]["symbol"] == "NVDA"
        assert enriched["entries"][0]["paper_readiness_status"] == "ready"


def test_symbol_suggestions_include_local_contracts_positions_and_aliases():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        old_sec = cockpit_module.sec_company_search
        old_nasdaq = cockpit_module.nasdaq_symbol_search
        cockpit_module.sec_company_search = lambda query, limit=16, fetch_if_stale=True: []
        cockpit_module.nasdaq_symbol_search = lambda query, limit=16, fetch_if_stale=True: []
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
        (data_dir / "open_positions.json").write_text(
            json.dumps([{
                "ticker": "TSLA",
                "side": "call",
                "strike": 260.0,
                "expiry": "2026-12-18",
                "trade_status": "Open",
            }]),
            encoding="utf-8",
        )
        (data_dir / "open_futures_positions.json").write_text(
            json.dumps([{"symbol": "NG=F", "direction": "long", "contract": "/MNG"}]),
            encoding="utf-8",
        )
        (data_dir / "robinhood_broker_snapshot.json").write_text(json.dumps({
            "generated_at": "2026-06-24T19:00:00+00:00",
            "accounts": [{
                "account_mask": "****1497",
                "label": "Default individual margin",
                "option_positions": [{
                    "chain_symbol": "ROBN",
                    "option_type": "call",
                    "strike_price": "35.0000",
                    "expiration_date": "2026-12-18",
                    "quantity": "2.0000",
                }],
                "equity_positions": [{
                    "symbol": "HOOD",
                    "quantity": "5.0000",
                }],
            }],
        }), encoding="utf-8")
        (data_dir / "option_chain_shortlist.json").write_text(json.dumps({
            "generated_at": "2026-06-24T19:00:00+00:00",
            "rows": [{
                "symbol": "AAPL",
                "contract_query": "AAPL 2027-01-15 C 220",
                "side": "call",
                "strike": 220.0,
                "expiry": "2027-01-15",
                "mid": 5.0,
                "premium_dollars": 500.0,
                "spread_pct": 0.04,
                "readiness_score": 92,
                "readiness_label": "ready",
                "contract_quality_score": 94,
                "swing_fit_score": 96,
                "contract_grade": "A",
                "chain_source": "cboe_options_chain",
                "quote_quality": "free_or_delayed",
            }],
        }), encoding="utf-8")

        try:
            nvda = build_symbol_suggestions(data_dir, query="nvda")
            assert any(row["query"] == "NVDA 2026-06-18 C 200" for row in nvda["rows"])

            chain = build_symbol_suggestions(data_dir, query="2027")
            assert any(
                row["query"] == "AAPL 2027-01-15 C 220"
                and row["kind"] == "chain_option"
                and row["source"] == "saved option-chain shortlist"
                for row in chain["rows"]
            )
            assert any("saved chain shortlists" in note for note in chain["notes"])

            oil = build_symbol_suggestions(data_dir, query="oil")
            assert any(row["symbol"] == "CL=F" for row in oil["rows"])

            apple = build_symbol_suggestions(data_dir, query="apple")
            assert any(row["symbol"] == "AAPL" and row["kind"] == "alias" for row in apple["rows"])

            gas = build_symbol_suggestions(data_dir, query="NG")
            assert any(row["symbol"] == "NG=F" and row["kind"] == "open_futures" for row in gas["rows"])

            tsla = build_symbol_suggestions(data_dir, query="260")
            assert any(
                row["symbol"] == "TSLA"
                and row["kind"] == "open_option"
                and row["query"] == "TSLA 2026-12-18 C 260"
                for row in tsla["rows"]
            )

            robn = build_symbol_suggestions(data_dir, query="ROBN")
            assert any(row["symbol"] == "ROBN" and row["kind"] == "broker_option" for row in robn["rows"])
            assert any(row["query"] == "ROBN 2026-12-18 C 35" for row in robn["rows"])
            assert any("broker snapshots" in note for note in robn["notes"])

            hood = build_symbol_suggestions(data_dir, query="HOOD")
            assert any(row["symbol"] == "HOOD" and row["kind"] == "broker_equity" for row in hood["rows"])

            observed_fetch_modes = []

            def fake_sec_search(query, limit=16, fetch_if_stale=True):
                observed_fetch_modes.append(fetch_if_stale)
                return [{
                    "symbol": "SNOW",
                    "name": "Snowflake Inc.",
                    "score": 0.97,
                }]

            cockpit_module.sec_company_search = fake_sec_search
            snow = build_symbol_suggestions(data_dir, query="snowflake")
            assert any(row["symbol"] == "SNOW" and row["kind"] == "sec" for row in snow["rows"])
            assert "Nasdaq Trader" in " ".join(snow["notes"])
            assert observed_fetch_modes == [False]

            cockpit_module.sec_company_search = lambda query, limit=16, fetch_if_stale=True: []
            cockpit_module.nasdaq_symbol_search = lambda query, limit=16, fetch_if_stale=True: [{
                "symbol": "QQQ",
                "name": "Invesco QQQ Trust",
                "type": "ETF",
                "score": 0.94,
            }]
            qqq = build_symbol_suggestions(data_dir, query="invesco")
            assert any(row["symbol"] == "QQQ" and row["kind"] == "nasdaq" for row in qqq["rows"])
        finally:
            cockpit_module.sec_company_search = old_sec
            cockpit_module.nasdaq_symbol_search = old_nasdaq


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
                "chain_source": "tradier",
                "quote_quality": "live_or_broker",
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
        assert filtered["rows"][0]["chain_source"] == "tradier"
        assert filtered["rows"][0]["quote_quality"] == "live_or_broker"
        assert filtered["rows"][0]["snapshot_age_min"] >= 0
        assert filtered["rows"][0]["snapshot_freshness"] in {"fresh", "aging", "stale"}


def test_best_setups_builds_decision_shortlist_from_latest_snapshots():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        pd.DataFrame([
            {
                "ticker": "TSLA",
                "side": "put",
                "strike": 300,
                "expiry": "2026-06-18",
                "confidence": 80,
                "rank_score": 9.0,
                "trade_status": "Watch",
                "suggested_contracts": 0,
            },
            {
                "ticker": "AAPL",
                "side": "call",
                "strike": 220,
                "expiry": "2026-12-18",
                "dte": 180,
                "mid": 4.2,
                "confidence": 76,
                "rank_score": 1.5,
                "trade_status": "Trade",
                "suggested_contracts": 1,
                "spread_pct": 0.08,
                "net_edge_pct": 0.18,
                "chain_source": "tradier",
                "quote_quality": "live_or_broker",
            },
            {
                "ticker": "WEEKLY",
                "side": "call",
                "strike": 10,
                "expiry": "2026-07-01",
                "dte": 18,
                "mid": 1.0,
                "confidence": 99,
                "rank_score": 8.0,
                "trade_status": "Trade",
                "suggested_contracts": 1,
            },
        ]).to_parquet(data_dir / "top_options_20260603_120000.parquet")
        pd.DataFrame([
            {
                "ticker": "NVDA",
                "spot": 120,
                "confidence": 88,
                "rank_score": 2.0,
                "trade_status": "Trade",
                "suggested_dollars": 600,
                "ev_pct": 0.07,
            },
        ]).to_parquet(data_dir / "top_shares_20260603_120000.parquet")
        pd.DataFrame([
            {
                "symbol": "CL=F",
                "name": "Crude Oil WTI",
                "direction": "LONG",
                "contract": "/MCL",
                "using_micro": True,
                "futures_score": 1.3,
                "rank_score": 1.3,
                "confidence": 70,
                "trade_status": "Trade",
                "suggested_contracts": 2,
                "entry_price": 74.5,
                "stop_price": 72.0,
                "target_price": 79.0,
                "risk_dollars": 500,
                "reward_dollars": 900,
                "hv20": 0.21,
            },
        ]).to_parquet(data_dir / "top_futures_20260603_120000.parquet")
        pd.DataFrame([
            {
                "ticker": "LYFT",
                "value_score": 2.4,
                "value_bucket": "deep value",
                "pe": 2.0,
                "fcf_yield": 0.12,
            },
        ]).to_parquet(data_dir / "top_value_20260603_120000.parquet")

        report = build_best_setups(data_dir, per_asset=2, limit=4)
        assert report["count"] == 4
        assert report["by_asset"]["option"][0]["ticker_or_symbol"] == "AAPL"
        assert all(row["ticker_or_symbol"] != "WEEKLY" for row in report["rows"])
        assert report["by_asset"]["option"][0]["quality"] == "spread 8.0% | tradier"
        assert report["by_asset"]["option"][0]["readiness_label"] == "review"
        assert report["by_asset"]["option"][0]["readiness_score"] >= 65
        assert "missing stop" in report["by_asset"]["option"][0]["risk_flags"]
        assert report["by_asset"]["option"][0]["setup_gate_status"] == "quote_check"
        assert report["by_asset"]["option"][0]["setup_gate_label"] == "Needs live quote"
        assert any("missing stop" in reason for reason in report["by_asset"]["option"][0]["setup_gate_reasons"])
        assert report["asset_summaries"][0]["rows"] == 3
        assert report["asset_summaries"][0]["actionable_rows"] == 1
        assert {row["asset"] for row in report["rows"]} == {"option", "share", "futures", "value"}
        scores = [row["score"] for row in report["rows"]]
        assert scores == sorted(scores, reverse=True)


def test_best_setups_marks_clean_long_dated_option_ready():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        pd.DataFrame([{
            "ticker": "AAPL",
            "side": "call",
            "strike": 220,
            "expiry": "2026-12-18",
            "dte": 180,
            "mid": 4.2,
            "confidence": 82,
            "rank_score": 1.5,
            "trade_status": "Trade",
            "suggested_contracts": 1,
            "spread_pct": 0.08,
            "stop_price": 2.5,
            "target_price": 8.0,
            "chain_source": "tradier",
            "quote_quality": "live_or_broker",
        }]).to_parquet(data_dir / "top_options_20260603_120000.parquet")

        report = build_best_setups(data_dir, per_asset=1, limit=1)
        row = report["rows"][0]
        assert row["ticker_or_symbol"] == "AAPL"
        assert row["readiness_label"] == "ready"
        assert row["readiness_score"] >= 80
        assert row["risk_flags"] == []
        assert row["setup_gate_status"] == "ready"
        assert row["setup_gate_label"] == "Ready to research"
        assert "readiness is ready" in row["setup_gate_reasons"]


def test_best_setups_gate_marks_short_dated_option_avoid_when_reviewed():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        pd.DataFrame([{
            "ticker": "WEEKLY",
            "side": "call",
            "strike": 10,
            "expiry": "2026-07-01",
            "dte": 18,
            "mid": 1.0,
            "confidence": 90,
            "rank_score": 2.0,
            "trade_status": "Trade",
            "suggested_contracts": 0,
            "spread_pct": 0.08,
            "stop_price": 0.5,
            "target_price": 2.0,
            "chain_source": "cboe",
            "quote_quality": "free_or_delayed",
        }]).to_parquet(data_dir / "top_options_20260603_120000.parquet")

        report = build_best_setups(data_dir, per_asset=1, limit=1)

    row = report["by_asset"]["option"][0]
    assert row["ticker_or_symbol"] == "WEEKLY"
    assert row["readiness_label"] == "wait"
    assert row["setup_gate_status"] == "avoid"
    assert row["setup_gate_label"] == "Avoid for now"
    assert any("below 90 dte" in reason.lower() for reason in row["setup_gate_reasons"])


def test_best_setups_decision_row_prefers_reviewable_over_higher_scored_avoid():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        pd.DataFrame([
            {
                "ticker": "STALE",
                "side": "call",
                "strike": 10,
                "expiry": "2026-12-18",
                "dte": 180,
                "mid": 1.0,
                "confidence": 99,
                "rank_score": 5.0,
                "trade_status": "Trade",
                "suggested_contracts": 1,
                "spread_pct": 0.08,
                "stop_price": 0.5,
                "target_price": 2.0,
                "chain_source": "cboe",
                "quote_quality": "free_or_delayed",
                "snapshot_freshness": "stale",
            },
            {
                "ticker": "CLEAN",
                "side": "call",
                "strike": 20,
                "expiry": "2026-12-18",
                "dte": 180,
                "mid": 2.0,
                "confidence": 82,
                "rank_score": 1.0,
                "trade_status": "Trade",
                "suggested_contracts": 1,
                "spread_pct": 0.05,
                "stop_price": 1.0,
                "target_price": 4.0,
                "chain_source": "tradier",
                "quote_quality": "live_or_broker",
            },
        ]).to_parquet(data_dir / "top_options_20260603_120000.parquet")

        report = build_best_setups(data_dir, per_asset=2, limit=2)

    assert report["rows"][0]["ticker_or_symbol"] == "STALE"
    assert report["rows"][0]["setup_gate_status"] == "avoid"
    assert report["decision_row"]["ticker_or_symbol"] == "CLEAN"
    assert report["decision_row"]["setup_gate_status"] == "ready"


def test_best_setups_include_saved_chain_shortlist_contracts():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        generated_at = datetime.now(timezone.utc).isoformat()
        (data_dir / "option_chain_shortlist.json").write_text(json.dumps({
            "generated_at": generated_at,
            "rows": [{
                "generated_at": generated_at,
                "symbol": "AAPL",
                "contract_query": "AAPL 2027-01-15 C 220",
                "side": "call",
                "expiry": "2027-01-15",
                "strike": 220.0,
                "dte": 216,
                "mid": 1.20,
                "premium_dollars": 120.0,
                "stop_price_reference": 0.70,
                "target_price_reference": 2.30,
                "spread_pct": 0.04,
                "openInterest": 1200,
                "contract_grade": "A",
                "readiness_label": "ready",
                "readiness_score": 91,
                "contract_quality_score": 94,
                "swing_fit_score": 93,
                "swing_fit_label": "clean_swing",
                "swing_fit_reasons": ["long swing runway", "tight spread"],
                "swing_fit_warnings": ["verify delayed quote"],
                "breakeven_move_label": "moderate",
                "liquidity_label": "deep",
                "chain_source": "cboe",
                "quote_quality": "free_or_delayed",
            }],
        }), encoding="utf-8")

        report = build_best_setups(data_dir, per_asset=2, limit=2)

    assert report["count"] == 1
    row = report["rows"][0]
    assert row["ticker_or_symbol"] == "AAPL"
    assert row["source_file"] == "option_chain_shortlist.json"
    assert row["swing_fit_label"] == "clean_swing"
    assert row["swing_fit_score"] == 93
    assert "long swing runway" in row["swing_fit_reasons"]
    assert row["liquidity_label"] == "deep"
    assert row["quality"] == "spread 4.0% | cboe"
    assert row["setup_gate_status"] == "quote_check"
    assert row["setup_gate_label"] == "Needs live quote"
    assert any("verify live option quote" in reason for reason in row["setup_gate_reasons"])
    assert report["asset_summaries"][0]["chain_shortlist_rows"] == 1
    assert "option_chain_shortlist" in report["sources"]
    assert any("Saved 3m+ chain shortlist" in note for note in report["notes"])


def test_swing_scout_surfaces_small_caps_and_futures_but_filters_short_dte_options():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        pd.DataFrame([
            {
                "ticker": "RGTI",
                "side": "call",
                "strike": 25,
                "expiry": "2026-12-18",
                "dte": 188,
                "mid": 2.2,
                "confidence": 84,
                "rank_score": 2.2,
                "trade_status": "Trade",
                "suggested_contracts": 1,
                "spread_pct": 0.08,
                "stop_price": 1.2,
                "target_price": 4.8,
                "market_cap": 1_200_000_000,
                "short_int_score": 2.1,
                "short_pct_of_float": 0.22,
                "short_vol_ratio": 0.63,
                "social_score": 0.7,
                "gtrends_score": 0.6,
                "twitter_score": 0.5,
                "tech_score": 0.8,
                "sector_rs_score": 0.09,
                "ticker_ret_20d": 0.18,
                "chain_source": "cboe",
                "quote_quality": "free_or_delayed",
            },
            {
                "ticker": "WEEKLY",
                "side": "call",
                "strike": 10,
                "expiry": "2026-07-01",
                "dte": 14,
                "mid": 1.0,
                "confidence": 99,
                "rank_score": 9.0,
                "trade_status": "Trade",
                "suggested_contracts": 1,
                "market_cap": 500_000_000,
                "short_int_score": 5.0,
            },
        ]).to_parquet(data_dir / "top_options_20260603_120000.parquet")
        pd.DataFrame([{
            "ticker": "SMOL",
            "spot": 8.5,
            "confidence": 79,
            "rank_score": 1.6,
            "trade_status": "Trade",
            "suggested_dollars": 500,
            "stop_price": 7.2,
            "target_price": 12.0,
            "market_cap": 850_000_000,
            "short_int_score": 1.8,
            "short_pct_of_float": 0.18,
            "short_vol_ratio": 0.58,
            "social_score": 0.5,
            "gtrends_score": 0.4,
            "tech_score": 0.7,
            "sector_rs_score": 0.05,
            "ticker_ret_20d": 0.12,
            "ev_pct": 0.08,
        }]).to_parquet(data_dir / "top_shares_20260603_120000.parquet")
        pd.DataFrame([{
            "symbol": "CL=F",
            "name": "Crude Oil WTI",
            "direction": "LONG",
            "contract": "/MCL",
            "using_micro": True,
            "futures_score": 1.4,
            "rank_score": 1.4,
            "confidence": 72,
            "trade_status": "Trade",
            "suggested_contracts": 2,
            "entry_price": 74.5,
            "stop_price": 72.0,
            "target_price": 80.0,
            "risk_dollars": 500,
            "reward_dollars": 1100,
            "ret_20d": 0.16,
            "hv20": 0.25,
        }]).to_parquet(data_dir / "top_futures_20260603_120000.parquet")

        report = build_swing_scout(data_dir, limit=10)
        symbols = {row["ticker_or_symbol"] for row in report["rows"]}
        share_only = build_swing_scout(data_dir, limit=10, asset="share")
        futures_only = build_swing_scout(data_dir, limit=10, asset="futures")
        squeeze_only = build_swing_scout(data_dir, limit=10, lane="small_cap_squeeze_watch")
        queried = build_swing_scout(data_dir, limit=10, query="smol")
        hidden_wait = build_swing_scout(data_dir, limit=10, include_wait=False)
        high_score = build_swing_scout(data_dir, limit=10, min_score=90)

    assert "RGTI" in symbols
    assert "SMOL" in symbols
    assert "CL=F" in symbols
    assert "WEEKLY" not in symbols
    option = next(row for row in report["rows"] if row["ticker_or_symbol"] == "RGTI")
    future = next(row for row in report["rows"] if row["ticker_or_symbol"] == "CL=F")
    assert option["market_cap_bucket"] == "small"
    assert option["swing_scout_score"] >= 80
    assert option["conviction_score"] >= 70
    assert option["review_action"] in {"review_now", "shortlist"}
    assert option["review_label"] in {"Review now", "Shortlist"}
    assert option["warning_count"] == len(option["warnings"])
    assert option["dte"] >= 90
    assert "short/squeeze pressure" in option["reasons"]
    assert option["factor_summary"]
    option_factors = {item["factor"] for item in option["factor_breakdown"]}
    assert {"Squeeze", "Momentum", "Execution"} & option_factors
    assert future["lane"] == "futures_macro_swing"
    assert future["conviction_score"] >= 70
    assert future["review_action"] in {"review_now", "shortlist"}
    future_factors = {item["factor"] for item in future["factor_breakdown"]}
    assert "Momentum" in future_factors
    assert "Execution" in future_factors
    assert report["min_option_dte"] == 90
    assert report["reviewed_count"] == 4
    assert report["filters"]["include_wait"] is True
    assert report["filters"]["lane"] == "all"
    assert sum(report["review_action_counts"].values()) == report["count"]
    assert set(report["review_action_counts"]) <= {"review_now", "shortlist", "watch", "wait"}
    assert futures_only["filters"]["asset"] == "futures"
    assert squeeze_only["filters"]["lane"] == "small_cap_squeeze_watch"
    assert {row["asset"] for row in share_only["rows"]} == {"share"}
    assert {row["asset"] for row in futures_only["rows"]} == {"futures"}
    assert {row["lane"] for row in squeeze_only["rows"]} == {"small_cap_squeeze_watch"}
    assert [row["ticker_or_symbol"] for row in queried["rows"]] == ["SMOL"]
    assert all(row["readiness_label"] != "wait" for row in hidden_wait["rows"])
    assert all(row["swing_scout_score"] >= 90 for row in high_score["rows"])


def test_swing_scout_can_include_nasdaq_small_cap_movers():
    old_movers = cockpit_module.small_cap_movers
    old_dark_pool = cockpit_module.dark_pool_engine.run
    old_halts = cockpit_module.halt_rows_for_symbols
    old_thresholds = cockpit_module.threshold_rows_for_symbols
    old_circuits = cockpit_module.circuit_rows_for_symbols
    cockpit_module.small_cap_movers = lambda max_rows=24: pd.DataFrame([{
        "symbol": "MOVE",
        "name": "Move Corp",
        "last_price": 4.25,
        "pct_change": 8.4,
        "volume": 2_500_000,
        "market_cap": 220_000_000,
        "sector": "Technology",
        "industry": "Software",
        "nasdaq_mover_score": 91,
        "market_cap_bucket": "micro",
    }])
    cockpit_module.dark_pool_engine.run = lambda universe, lookback_days=3: pd.DataFrame([{
        "ticker": "MOVE",
        "short_vol_ratio": 0.64,
        "short_vol": 640_000,
        "total_vol": 1_000_000,
        "dark_pool_score": -0.56,
    }])
    cockpit_module.halt_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame()
    cockpit_module.threshold_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame()
    cockpit_module.circuit_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame()
    try:
        with tempfile.TemporaryDirectory() as td:
            report = build_swing_scout(
                Path(td),
                limit=5,
                include_nasdaq_movers=True,
                lane="nasdaq_small_cap_mover",
            )
    finally:
        cockpit_module.small_cap_movers = old_movers
        cockpit_module.dark_pool_engine.run = old_dark_pool
        cockpit_module.halt_rows_for_symbols = old_halts
        cockpit_module.threshold_rows_for_symbols = old_thresholds
        cockpit_module.circuit_rows_for_symbols = old_circuits

    assert report["count"] == 1
    row = report["rows"][0]
    assert row["ticker_or_symbol"] == "MOVE"
    assert row["asset"] == "share"
    assert row["lane"] == "nasdaq_small_cap_mover"
    assert row["trade_status"] == "Review"
    assert row["source_file"] == "nasdaq_screener"
    assert row["pct_change"] == 8.4
    assert row["short_vol_ratio"] == 0.64
    assert row["dark_pool_score"] == -0.56
    assert row["swing_scout_score"] == 98
    assert row["conviction_score"] >= 80
    assert row["review_action"] == "review_now"
    assert row["review_label"] == "Review now"
    assert row["warning_count"] == len(row["warnings"])
    assert row["factor_summary"]
    assert {item["factor"] for item in row["factor_breakdown"]} >= {"Momentum", "Short volume", "Market cap"}
    assert "FINRA short-volume 64%" in row["reasons"]
    assert "heavy short-volume pressure" in row["warnings"]
    assert "nasdaq_movers" in report["sources"]
    assert report["asset_counts"]["share"] == 1
    assert report["review_action_counts"]["review_now"] == 1
    assert report["filters"]["include_nasdaq_movers"] is True


def test_swing_scout_market_structure_risk_downgrades_nasdaq_movers():
    old_movers = cockpit_module.small_cap_movers
    old_dark_pool = cockpit_module.dark_pool_engine.run
    old_halts = cockpit_module.halt_rows_for_symbols
    old_thresholds = cockpit_module.threshold_rows_for_symbols
    old_circuits = cockpit_module.circuit_rows_for_symbols
    cockpit_module.small_cap_movers = lambda max_rows=24: pd.DataFrame([{
        "symbol": "RISK",
        "name": "Risk Move Corp",
        "last_price": 3.75,
        "pct_change": 18.2,
        "volume": 4_200_000,
        "market_cap": 180_000_000,
        "sector": "Technology",
        "industry": "Software",
        "nasdaq_mover_score": 94,
        "market_cap_bucket": "micro",
    }, {
        "symbol": "SAFE",
        "name": "Safe Move Corp",
        "last_price": 6.10,
        "pct_change": 9.5,
        "volume": 2_100_000,
        "market_cap": 650_000_000,
        "sector": "Industrials",
        "industry": "Machinery",
        "nasdaq_mover_score": 88,
        "market_cap_bucket": "small",
    }])
    cockpit_module.dark_pool_engine.run = lambda universe, lookback_days=3: pd.DataFrame()
    cockpit_module.halt_rows_for_symbols = lambda symbols, cache_age=60: pd.DataFrame([{
        "symbol": "RISK",
        "active_halt": True,
        "halt_risk_score": 98,
    }])
    cockpit_module.threshold_rows_for_symbols = lambda symbols, cache_age=21600: pd.DataFrame([{
        "symbol": "RISK",
        "is_threshold": True,
        "settlement_risk_score": 86,
    }])
    cockpit_module.circuit_rows_for_symbols = lambda symbols, cache_age=1800: pd.DataFrame([{
        "symbol": "RISK",
        "short_sale_restricted": True,
        "ssr_risk_score": 82,
    }])
    try:
        with tempfile.TemporaryDirectory() as td:
            report = build_swing_scout(
                Path(td),
                limit=5,
                include_nasdaq_movers=True,
                lane="nasdaq_small_cap_mover",
                include_wait=True,
                min_score=0,
            )
    finally:
        cockpit_module.small_cap_movers = old_movers
        cockpit_module.dark_pool_engine.run = old_dark_pool
        cockpit_module.halt_rows_for_symbols = old_halts
        cockpit_module.threshold_rows_for_symbols = old_thresholds
        cockpit_module.circuit_rows_for_symbols = old_circuits

    by_symbol = {row["ticker_or_symbol"]: row for row in report["rows"]}
    assert set(by_symbol) == {"RISK", "SAFE"}
    risky = by_symbol["RISK"]
    safe = by_symbol["SAFE"]
    assert risky["market_structure_risk_score"] == 98
    assert set(risky["market_structure_risk_flags"]) == {
        "active_halt", "regsho_threshold", "short_sale_restricted",
    }
    assert {"active trading halt", "Reg SHO threshold list", "short-sale circuit breaker active"} <= set(risky["warnings"])
    assert risky["active_halt"] is True
    assert risky["regsho_threshold"] is True
    assert risky["short_sale_restricted"] is True
    assert risky["trade_status"] == "Wait"
    assert risky["review_action"] == "wait"
    assert risky["conviction_score"] <= 40
    assert risky["swing_scout_score"] < safe["swing_scout_score"]
    assert "Market-structure risk" in {item["factor"] for item in risky["factor_breakdown"]}
    assert safe["market_structure_risk_flags"] == []
    assert safe["review_action"] in {"review_now", "shortlist"}


def test_climate_gated_setups_pass_clean_rows_and_hold_weak_contracts():
    old_history = cockpit_module.data_provider.get_history

    def fake_history(ticker: str, period: str = "6mo", interval: str = "1d", cache_age: int = 1800):
        del period, interval, cache_age
        idx = pd.date_range("2026-01-01", periods=80, freq="D", tz="UTC")
        slopes = {
            "SPY": 1.0,
            "RSP": 1.2,
            "IWM": 1.35,
            "QQQ": 1.4,
            "SMH": 1.8,
            "XLK": 1.5,
            "XLY": 1.4,
            "XLP": 0.2,
            "HYG": 0.6,
            "LQD": 0.1,
            "XLU": 0.1,
            "^VIX": -0.35,
            "TLT": -0.05,
            "GLD": 0.05,
            "UUP": -0.05,
        }
        slope = slopes.get(ticker, 0.45)
        close = [100 + i * slope for i in range(80)]
        return pd.DataFrame({"Close": close}, index=idx)

    try:
        cockpit_module.data_provider.get_history = fake_history
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            pd.DataFrame([
                {
                    "ticker": "AAPL",
                    "side": "call",
                    "strike": 220,
                    "expiry": "2026-12-18",
                    "dte": 180,
                    "mid": 4.2,
                    "confidence": 86,
                    "rank_score": 2.5,
                    "trade_status": "Trade",
                    "suggested_contracts": 1,
                    "spread_pct": 0.05,
                    "stop_price": 2.4,
                    "target_price": 8.0,
                    "chain_source": "tradier",
                    "quote_quality": "live_or_broker",
                },
                {
                    "ticker": "WIDE",
                    "side": "call",
                    "strike": 40,
                    "expiry": "2026-09-18",
                    "dte": 100,
                    "mid": 1.0,
                    "confidence": 80,
                    "rank_score": 2.0,
                    "trade_status": "Trade",
                    "suggested_contracts": 1,
                    "spread_pct": 0.30,
                    "stop_price": 0.5,
                    "target_price": 2.0,
                    "chain_source": "tradier",
                    "quote_quality": "live_or_broker",
                },
            ]).to_parquet(data_dir / "top_options_20260603_120000.parquet")
            pd.DataFrame([{
                "ticker": "NVDA",
                "spot": 120,
                "confidence": 88,
                "rank_score": 1.8,
                "fused_score": 1.7,
                "trade_status": "Trade",
                "suggested_dollars": 600,
                "stop_price": 110,
                "target_price": 145,
            }]).to_parquet(data_dir / "top_shares_20260603_120000.parquet")
            pd.DataFrame([{
                "symbol": "CL=F",
                "name": "Crude Oil WTI",
                "direction": "long",
                "contract": "/MCL",
                "using_micro": True,
                "futures_score": 1.3,
                "rank_score": 1.3,
                "confidence": 78,
                "trade_status": "Trade",
                "suggested_contracts": 1,
                "entry_price": 74.5,
                "stop_price": 72.0,
                "target_price": 79.0,
                "risk_dollars": 250,
                "reward_dollars": 450,
            }]).to_parquet(data_dir / "top_futures_20260603_120000.parquet")

            gated = build_climate_gated_setups(data_dir, per_asset=3, limit=5)
    finally:
        cockpit_module.data_provider.get_history = old_history

    passed = {row["ticker_or_symbol"]: row for row in gated["rows"]}
    held = {row["ticker_or_symbol"]: row for row in gated["held"]}
    assert gated["climate_label"] in {"aggressive_swing", "constructive_selective"}
    assert "AAPL" in passed
    assert passed["AAPL"]["climate_gate_status"] == "pass"
    assert "WIDE" in held
    assert held["WIDE"]["climate_gate_status"] == "hold"
    assert any("spread" in reason for reason in held["WIDE"]["climate_gate_reasons"])
    assert gated["asset_counts"]["option"]["pass"] >= 1
    assert gated["trade_gates"]


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


def test_exit_review_summary_reads_jsonl_and_filters_actions():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        rows = [
            {
                "timestamp": "2026-06-10T15:00:00+00:00",
                "asset": "option",
                "position_id": "opt-aapl",
                "ticker": "AAPL",
                "action": "tighten_stop",
                "exit_pressure": 72,
                "old_stop": 1.0,
                "new_stop": 1.4,
                "current_price": 2.1,
                "current_pnl_pct": 0.12,
                "reasons": ["confidence drop", "spread widening"],
                "used_learned_policy": False,
            },
            {
                "timestamp": "2026-06-10T15:02:00+00:00",
                "asset": "share",
                "position_id": "shr-nvda",
                "ticker": "NVDA",
                "action": "close_early",
                "exit_pressure": 86,
                "current_price": 118.0,
                "current_pnl_pct": -0.08,
                "reasons": ["negative news flip"],
                "used_learned_policy": True,
                "policy_version": "exit_policy_2",
            },
            {
                "timestamp": "2026-06-10T15:03:00+00:00",
                "asset": "futures",
                "position_id": "fut-cl",
                "symbol": "CL=F",
                "action": "hold",
                "exit_pressure": 25,
                "current_pnl_dollars": 75.0,
                "reasons": ["macro still supportive"],
            },
            {
                "timestamp": "2026-06-10T15:04:00+00:00",
                "asset": "option",
                "position_id": "opt-aapl",
                "ticker": "AAPL",
                "action": "watch",
                "exit_pressure": 45,
                "old_stop": 1.4,
                "new_stop": 1.4,
                "current_price": 2.3,
                "current_pnl_pct": 0.15,
                "reasons": ["latest review stabilized"],
            },
        ]
        text = "\n".join(json.dumps(row) for row in rows) + "\nnot-json\n"
        (data_dir / "exit_reviews.jsonl").write_text(text, encoding="utf-8")

        report = build_exit_review_summary(data_dir)
        assert report["total_before_limit"] == 4
        assert report["count"] == 4
        assert report["action_counts"] == {"watch": 1, "hold": 1, "close_early": 1, "tighten_stop": 1}
        assert report["asset_counts"] == {"option": 2, "futures": 1, "share": 1}
        assert report["high_pressure_count"] == 1
        assert report["learned_policy_count"] == 1
        assert report["current_decision_count"] == 3
        assert report["current_high_pressure_count"] == 1
        assert report["current_action_counts"] == {"watch": 1, "hold": 1, "close_early": 1}
        assert report["avg_exit_pressure"] == 57.0
        assert report["rows"][0]["ticker_or_symbol"] == "AAPL"
        assert report["rows"][0]["action"] == "watch"
        assert report["rows"][1]["ticker_or_symbol"] == "CL=F"
        assert report["rows"][2]["ticker_or_symbol"] == "NVDA"
        assert report["rows"][2]["policy_version"] == "exit_policy_2"
        assert report["rows"][2]["reasons_text"] == "negative news flip"
        assert report["current_decisions"][0]["ticker_or_symbol"] == "NVDA"
        assert report["current_decisions"][0]["latest_action"] == "close_early"
        assert report["current_decisions"][1]["ticker_or_symbol"] == "AAPL"
        assert report["current_decisions"][1]["latest_action"] == "watch"
        assert report["current_decisions"][1]["exit_pressure"] == 45
        assert report["by_symbol"][0]["ticker_or_symbol"] == "NVDA"
        assert report["by_symbol"][0]["max_exit_pressure"] == 86

        option_only = build_exit_review_summary(data_dir, asset="option", query="AAPL")
        assert option_only["count"] == 2
        assert option_only["current_decision_count"] == 1
        assert option_only["current_decisions"][0]["latest_action"] == "watch"


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


def test_market_pulse_uses_free_history_context_and_regime_labels():
    old_history = cockpit_module.data_provider.get_history
    old_options = cockpit_module.build_options_sentiment

    def fake_history(ticker: str, period: str = "6mo", interval: str = "1d", cache_age: int = 1800):
        del period, interval, cache_age
        idx = pd.date_range("2026-01-01", periods=80, freq="D", tz="UTC")
        if ticker in {"SPY", "QQQ", "IWM", "DIA"}:
            close = [100 + i for i in range(80)]
        elif ticker == "^VIX":
            close = [30 - i * 0.1 for i in range(80)]
        elif ticker == "TLT":
            close = [100 - i * 0.1 for i in range(80)]
        else:
            close = [50 + i * 0.05 for i in range(80)]
        return pd.DataFrame({"Close": close}, index=idx)

    def fake_options_sentiment(data_dir=None):
        del data_dir
        return {
            "status": "ok",
            "regime": "balanced",
            "coverage": "3/3",
            "total_pc": 0.86,
            "equity_pc": 0.57,
            "index_pc": 1.05,
            "rows": [{"key": "total", "status": "ok", "pc_ratio": 0.86, "signal": "balanced"}],
            "warnings": [],
        }

    try:
        cockpit_module.data_provider.get_history = fake_history
        cockpit_module.build_options_sentiment = fake_options_sentiment
        pulse = build_market_pulse(period="6mo")
    finally:
        cockpit_module.data_provider.get_history = old_history
        cockpit_module.build_options_sentiment = old_options

    assert pulse["coverage"] == "9/9"
    assert pulse["regime"] in {"risk_on", "constructive"}
    assert pulse["options_sentiment"]["regime"] == "balanced"
    assert pulse["options_sentiment"]["total_pc"] == 0.86
    assert pulse["risk_score"] > 0
    rows = {row["symbol"]: row for row in pulse["rows"]}
    assert rows["SPY"]["trend"] == "uptrend"
    assert rows["^VIX"]["trend"] in {"downtrend", "weak"}
    assert pulse["leaders"][0]["symbol"] in {"SPY", "QQQ", "IWM", "DIA"}


def test_options_sentiment_uses_cboe_put_call_snapshots():
    old_fetch = cockpit_module._fetch_cboe_put_call_snapshot
    old_daily = cockpit_module._fetch_cboe_daily_put_call_ratios
    today = str(pd.Timestamp.now(tz="UTC").date())
    snapshots = {
        "total": {
            "key": "total", "label": "Total options", "status": "ok",
            "signal": "balanced", "pc_ratio": 0.86, "latest_date": today,
        },
        "equity": {
            "key": "equity", "label": "Equity options", "status": "ok",
            "signal": "call_demand_high", "pc_ratio": 0.52, "latest_date": today,
        },
        "index": {
            "key": "index", "label": "Index options", "status": "ok",
            "signal": "defensive_hedging", "pc_ratio": 1.18, "latest_date": "2019-10-04",
        },
    }

    def fake_fetch(source, cache_age=21600):
        del cache_age
        return dict(snapshots[source["key"]])

    def fake_daily(cache_age=1800):
        del cache_age
        return {"index": 1.18}

    try:
        cockpit_module._fetch_cboe_put_call_snapshot = fake_fetch
        cockpit_module._fetch_cboe_daily_put_call_ratios = fake_daily
        sentiment = build_options_sentiment()
    finally:
        cockpit_module._fetch_cboe_put_call_snapshot = old_fetch
        cockpit_module._fetch_cboe_daily_put_call_ratios = old_daily

    assert sentiment["status"] == "ok"
    assert sentiment["coverage"] == "3/3"
    assert sentiment["total_pc"] == 0.86
    assert sentiment["equity_pc"] == 0.52
    assert sentiment["index_pc"] == 1.18
    assert sentiment["regime"] == "balanced"
    assert {row["key"] for row in sentiment["rows"]} == {"total", "equity", "index"}
    index_row = [row for row in sentiment["rows"] if row["key"] == "index"][0]
    assert index_row["source"] == "cboe_daily_market_statistics"


def test_cboe_daily_put_call_parser_handles_escaped_nextjs_payload():
    text = (
        r'self.__next_f.push([1,"{\"data\":{\"optionsData\":{\"ratios\":['
        r'{\"name\":\"TOTAL PUT/CALL RATIO\",\"value\":\"0.76\"},'
        r'{\"name\":\"INDEX PUT/CALL RATIO\",\"value\":\"1.06\"},'
        r'{\"name\":\"EQUITY PUT/CALL RATIO\",\"value\":\"0.54\"}'
        r']}}}"])'
    )
    parsed = cockpit_module._parse_cboe_daily_put_call_ratios(text)
    assert parsed == {"total": 0.76, "index": 1.06, "equity": 0.54}


def test_options_sentiment_marks_stale_when_daily_fallback_missing():
    old_fetch = cockpit_module._fetch_cboe_put_call_snapshot
    old_daily = cockpit_module._fetch_cboe_daily_put_call_ratios

    def fake_fetch(source, cache_age=21600):
        del cache_age
        return {
            "key": source["key"],
            "label": source["label"],
            "status": "ok",
            "signal": "balanced",
            "pc_ratio": 1.0,
            "latest_date": "2019-10-04",
        }

    def fake_daily(cache_age=1800):
        del cache_age
        return {}

    try:
        cockpit_module._fetch_cboe_put_call_snapshot = fake_fetch
        cockpit_module._fetch_cboe_daily_put_call_ratios = fake_daily
        sentiment = build_options_sentiment()
    finally:
        cockpit_module._fetch_cboe_put_call_snapshot = old_fetch
        cockpit_module._fetch_cboe_daily_put_call_ratios = old_daily

    assert sentiment["status"] == "missing"
    assert sentiment["coverage"] == "0/3"
    assert len(sentiment["warnings"]) == 3
    assert {row["status"] for row in sentiment["rows"]} == {"stale"}


def test_macro_stress_pulse_uses_keyless_fred_series():
    old_fred = cockpit_module.fred_csv_history

    def monthly_rows(latest: float, year_ago: float) -> list[dict[str, float | str]]:
        rows = []
        for idx in range(13):
            value = latest if idx == 0 else year_ago if idx == 12 else latest - idx * 0.1
            rows.append({"date": f"2026-{max(1, 12 - idx):02d}-01", "value": value})
        return rows

    def fake_fred(series_id: str, days: int = 90, cache_hours: int = 12):
        del days, cache_hours
        data = {
            "BAMLH0A0HYM2": [
                {"date": "2026-06-12", "value": 2.8},
                {"date": "2026-06-05", "value": 2.9},
                {"date": "2026-05-29", "value": 3.0},
                {"date": "2026-05-22", "value": 3.0},
                {"date": "2026-05-15", "value": 2.9},
            ],
            "T10Y3M": [
                {"date": "2026-06-12", "value": 0.9},
                {"date": "2026-06-05", "value": 0.8},
                {"date": "2026-05-29", "value": 0.7},
                {"date": "2026-05-22", "value": 0.6},
                {"date": "2026-05-15", "value": 0.5},
            ],
            "UNRATE": monthly_rows(4.0, 4.1),
            "ICSA": [
                {"date": "2026-06-06", "value": 220000},
                {"date": "2026-05-30", "value": 218000},
                {"date": "2026-05-23", "value": 216000},
                {"date": "2026-05-16", "value": 214000},
                {"date": "2026-05-09", "value": 215000},
            ],
            "CPIAUCSL": monthly_rows(320.0, 310.0),
            "INDPRO": monthly_rows(105.0, 103.0),
            "M2SL": monthly_rows(22000.0, 21000.0),
        }
        return data.get(series_id, [])

    try:
        cockpit_module.fred_csv_history = fake_fred
        pulse = build_macro_stress_pulse()
    finally:
        cockpit_module.fred_csv_history = old_fred

    assert pulse["source"] == "FRED public CSV"
    assert pulse["coverage"] == "7/7"
    assert pulse["regime"] == "macro_supportive"
    assert pulse["stress_score"] <= 15
    assert pulse["signal_counts"]["supportive"] >= 4
    assert pulse["signal_counts"]["warning"] >= 2
    cpi = [row for row in pulse["rows"] if row["series_id"] == "CPIAUCSL"][0]
    assert cpi["signal"] == "warning"
    assert cpi["yoy"] > 0.03


def test_breadth_pulse_uses_free_etf_pair_confirmation():
    old_history = cockpit_module.data_provider.get_history

    slopes = {
        "SPY": 1.0,
        "RSP": 1.25,
        "IWM": 1.45,
        "QQQ": 1.35,
        "XLY": 1.50,
        "XLP": 0.40,
        "HYG": 0.55,
        "LQD": 0.10,
        "SMH": 1.85,
        "XLU": 0.20,
    }

    def fake_history(ticker: str, period: str = "6mo", interval: str = "1d", cache_age: int = 1800):
        del period, interval, cache_age
        idx = pd.date_range("2026-01-01", periods=80, freq="D", tz="UTC")
        slope = slopes.get(ticker, 0.8)
        close = [100 + i * slope for i in range(80)]
        return pd.DataFrame({"Close": close}, index=idx)

    try:
        cockpit_module.data_provider.get_history = fake_history
        pulse = build_breadth_pulse(period="6mo")
    finally:
        cockpit_module.data_provider.get_history = old_history

    assert pulse["coverage"] == "7/7"
    assert pulse["regime"] in {"broad_risk_on", "selective_risk_on"}
    assert pulse["breadth_score"] > 0
    assert pulse["supportive_count"] >= 5
    rows = {row["label"]: row for row in pulse["rows"]}
    assert rows["Small-cap breadth"]["signal"] == "supportive"
    assert rows["Defensive pressure"]["signal"] == "supportive"
    assert rows["Credit risk appetite"]["pair"] == "HYG/LQD"


def test_swing_climate_combines_free_context_into_posture():
    old_history = cockpit_module.data_provider.get_history
    old_options = cockpit_module.build_options_sentiment
    old_macro = cockpit_module.build_macro_stress_pulse

    slopes = {
        "SPY": 1.0,
        "RSP": 1.25,
        "IWM": 1.45,
        "DIA": 0.95,
        "QQQ": 1.35,
        "XLY": 1.50,
        "XLP": 0.40,
        "HYG": 0.55,
        "LQD": 0.10,
        "SMH": 1.85,
        "XLU": 0.20,
        "XLK": 1.60,
        "XLE": -0.20,
        "^VIX": -0.30,
        "TLT": -0.05,
        "GLD": 0.05,
        "USO": 0.10,
        "UUP": -0.05,
    }

    def fake_history(ticker: str, period: str = "6mo", interval: str = "1d", cache_age: int = 1800):
        del period, interval, cache_age
        idx = pd.date_range("2026-01-01", periods=80, freq="D", tz="UTC")
        slope = slopes.get(ticker, 0.45)
        close = [100 + i * slope for i in range(80)]
        return pd.DataFrame({"Close": close}, index=idx)

    def fake_options_sentiment(data_dir=None):
        del data_dir
        return {
            "status": "ok",
            "regime": "call_demand_rising",
            "coverage": "3/3",
            "total_pc": 0.76,
            "equity_pc": 0.54,
            "index_pc": 1.06,
            "rows": [],
            "warnings": [],
        }

    def fake_macro_stress(data_dir=None):
        del data_dir
        return {
            "status": "ok",
            "regime": "macro_supportive",
            "stress_score": 10,
            "coverage": "7/7",
            "signal_counts": {"supportive": 6, "warning": 1},
            "warnings": [],
        }

    try:
        cockpit_module.data_provider.get_history = fake_history
        cockpit_module.build_options_sentiment = fake_options_sentiment
        cockpit_module.build_macro_stress_pulse = fake_macro_stress
        climate = build_swing_climate(period="6mo")
    finally:
        cockpit_module.data_provider.get_history = old_history
        cockpit_module.build_options_sentiment = old_options
        cockpit_module.build_macro_stress_pulse = old_macro

    assert climate["climate_score"] >= 60
    assert climate["climate_label"] in {"aggressive_swing", "constructive_selective"}
    assert climate["market_regime"] in {"risk_on", "constructive"}
    assert climate["breadth_regime"] in {"broad_risk_on", "selective_risk_on"}
    assert climate["options_sentiment_regime"] == "call_demand_rising"
    assert climate["macro_regime"] == "macro_supportive"
    assert climate["macro_stress_score"] == 10
    assert climate["options_sentiment"]["total_pc"] == 0.76
    assert climate["components"]["options_sentiment"] == 4.0
    assert climate["components"]["macro"] == 3.0
    assert climate["coverage"] == {
        "market": "9/9",
        "breadth": "7/7",
        "sector": "13/13",
        "options_sentiment": "3/3",
        "macro": "7/7",
    }
    assert climate["top_sector_symbol"] in {"SMH", "XLK"}
    assert climate["focus"]
    assert climate["playbook"]["option_min_dte"] >= 90
    assert climate["playbook"]["max_new_candidates"] >= 3
    assert any(row["gate"] == "Options DTE floor" for row in climate["trade_gates"])
    assert any(row["gate"] == "Options sentiment" for row in climate["trade_gates"])
    assert any(row["gate"] == "Macro stress" for row in climate["trade_gates"])
    assert any(row["asset"] == "options" for row in climate["asset_bias"])
    assert any("Cboe options sentiment" in item for item in climate["positives"])
    assert any("FRED macro stress pulse" in item for item in climate["positives"])
    assert any("Breadth pulse" in item for item in climate["positives"])


def test_sector_pulse_ranks_free_sector_etf_context():
    old_history = cockpit_module.data_provider.get_history

    def fake_history(ticker: str, period: str = "6mo", interval: str = "1d", cache_age: int = 1800):
        del period, interval, cache_age
        idx = pd.date_range("2026-01-01", periods=80, freq="D", tz="UTC")
        if ticker == "XLK":
            close = [100 + i * 1.5 for i in range(80)]
        elif ticker == "XLE":
            close = [100 - i * 0.8 for i in range(80)]
        else:
            close = [100 + i * 0.2 for i in range(80)]
        return pd.DataFrame({"Close": close}, index=idx)

    try:
        cockpit_module.data_provider.get_history = fake_history
        pulse = build_sector_pulse(period="6mo")
    finally:
        cockpit_module.data_provider.get_history = old_history

    assert pulse["coverage"] == "13/13"
    assert pulse["leaders"][0]["symbol"] == "XLK"
    assert pulse["laggards"][0]["symbol"] == "XLE"
    rows = {row["symbol"]: row for row in pulse["rows"]}
    assert rows["XLK"]["trend"] == "uptrend"
    assert rows["XLK"]["strength_score"] > rows["XLE"]["strength_score"]
    assert rows["XLK"]["last_date"] == "2026-03-21"


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
                "contract": "AAPL 2026-12-18 C 200",
                "side": "call",
                "strike": 200,
                "expiry": "2026-12-18",
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
                "contract": "MSFT 2026-12-18 C 500",
                "side": "call",
                "strike": 500,
                "expiry": "2026-12-18",
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

        filtered = build_paper_candidates(data_dir, max_new=5, query="AAPL 20261218 C 200")
        assert filtered["query"] == "AAPL 20261218 C 200"
        assert filtered["selected_count"] == 1
        assert filtered["rows"][0]["ticker_or_symbol"] == "AAPL"

        dry = build_paper_candidates(data_dir, dry_run=True)
        assert dry["excluded_count"] == 1
        assert any("suggested_contracts <= 0" in row["reason_excluded"] for row in dry["rows"])
        assert dry["rejection_reason_counts"]["suggested_contracts <= 0"] == 1
        assert dry["top_rejection_reasons"][0]["reason"] == "suggested_contracts <= 0"

        written = build_paper_candidates(data_dir, max_new=5, write=True)
        assert written["wrote_files"] is True
        assert (data_dir / "external_paper_orders.csv").exists()
        assert (data_dir / "external_paper_orders.json").exists()


def test_robinhood_agentic_queue_panel_builds_and_writes_long_dated_candidates():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        pd.DataFrame([
            {
                "ticker": "AAPL",
                "contract": "AAPL 2027-01-15 C 200",
                "side": "call",
                "strike": 200,
                "expiry": "2027-01-15",
                "mid": 0.75,
                "suggested_contracts": 1,
                "actual_dollars": 75,
                "stop_price": 0.35,
                "target_price": 1.6,
                "confidence": 72,
                "rank_score": 2.0,
                "fused_score": 1.5,
                "trade_status": "Trade",
                "spread_pct": 0.04,
            },
            {
                "ticker": "MSFT",
                "contract": "MSFT 2026-10-16 C 500",
                "side": "call",
                "strike": 500,
                "expiry": "2026-10-16",
                "mid": 0.65,
                "suggested_contracts": 1,
                "actual_dollars": 65,
                "stop_price": 0.3,
                "target_price": 1.4,
                "confidence": 70,
                "rank_score": 1.8,
                "trade_status": "Trade",
                "spread_pct": 0.04,
            },
        ]).to_parquet(data_dir / "top_options_20260613_120000.parquet")
        (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_share_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_futures_positions.json").write_text("[]", encoding="utf-8")

        preview = build_robinhood_agentic_queue_report(
            data_dir,
            account_budget=500,
            max_candidates=5,
            max_orders=2,
            min_dte=180,
        )
        assert preview["candidate_count"] == 1
        assert preview["orders"][0]["symbol"] == "AAPL"
        assert preview["orders"][0]["dte"] >= 180
        assert any("dte below 180" in row["reasons"] for row in preview["rejected"])
        assert preview["readiness"]["label"] == "ready"
        assert preview["readiness"]["premium_cap_remaining"] >= 0
        assert preview["rejection_reason_counts"]["dte below 180"] >= 1
        assert preview["diagnostics"]["reason_groups"]["below_min_dte"] >= 1

        written = build_robinhood_agentic_queue_report(data_dir, write=True)
        assert written["wrote_files"] is True
        assert (data_dir / "robinhood_agentic_queue.json").exists()
        assert (data_dir / "robinhood_agentic_prompt.md").exists()
        assert (data_dir / "robinhood_agentic_cycle.json").exists()
        assert (data_dir / "robinhood_agentic_cycle_prompt.md").exists()


def test_agentic_decision_journal_records_local_review_rows():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)

        empty = build_agentic_decision_journal(data_dir)
        assert empty["recent_count"] == 0
        assert empty["exists"] is False

        out = record_agentic_decision(
            {"decision": "skipped", "symbol": "aapl", "reason": "entry gate blocked"},
            data_dir,
        )

        assert out["ok"] is True
        assert out["recent_count"] == 1
        assert out["action_counts"]["skipped"] == 1
        row = out["rows"][0]
        assert row["decision"] == "skipped"
        assert row["symbol"] == "AAPL"
        assert row["reason"] == "entry gate blocked"
        assert row["source"] == "local_cockpit"
        assert (data_dir / "robinhood_agentic_decisions.jsonl").exists()


def test_agentic_autopilot_status_summarizes_gate_tickets_and_paper_book():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        contract = "AAPL 2027-01-15 C 200"
        now = datetime.now(timezone.utc).isoformat()
        ticket = {
            "symbol": "AAPL",
            "contract": contract,
            "option_side": "call",
            "strike": 200,
            "expiry": "2027-01-15",
            "generated_at": now,
            "quantity": 1,
            "limit_price": 1.25,
            "estimated_premium_dollars": 125.0,
            "entry_gate_status": "blocked",
            "confirmation_required": True,
            "confidence": 91,
            "rank_score": 2.4,
        }
        (data_dir / "robinhood_agentic_queue.json").write_text(json.dumps({
            "status": "ready",
            "generated_at": now,
            "orders": [ticket],
        }), encoding="utf-8")
        (data_dir / "robinhood_agentic_cycle.json").write_text(json.dumps({
            "generated_at": now,
            "auto_submit_allowed": False,
            "entry_gate": {
                "status": "blocked",
                "label": "Fresh entries blocked",
                "new_entries_allowed_after_live_checks": False,
                "blockers": ["validation max drawdown is -72.4%"],
                "warnings": ["execution mode defaults to approval_required"],
            },
            "review_only_entry_candidates": [ticket],
            "entry_candidates": [],
        }), encoding="utf-8")
        (data_dir / "robinhood_live_order_tickets.json").write_text(json.dumps({
            "confirmation_required": True,
            "generated_at": now,
            "tickets": [ticket],
        }), encoding="utf-8")
        (data_dir / "agentic_paper_positions.json").write_text(json.dumps([
            {
                "status": "open",
                "symbol": "AAPL",
                "contract": contract,
                "option_side": "call",
                "strike": 200,
                "expiry": "2027-01-15",
                "quantity": 1,
                "entry_price": 1.2,
                "paper_limit_price": 1.25,
                "stop_price_reference": 0.6,
                "target_price_reference": 2.4,
                "opened_at": now,
            }
        ]), encoding="utf-8")
        (data_dir / "open_positions.json").write_text(json.dumps([
            {
                "ticker": "AAPL",
                "side": "call",
                "strike": 200,
                "expiry": "2027-01-15",
                "direction": "long_call",
                "current_price": 1.5,
                "latest_exit_action": "hold",
                "latest_exit_pressure": 25,
                "last_reprice_source": "test_chain",
            }
        ]), encoding="utf-8")
        record_agentic_decision(
            {"decision": "reviewed", "symbol": "AAPL", "contract": contract, "reason": "paper only"},
            data_dir,
        )

        status = build_agentic_autopilot_status(data_dir)

        assert status["status"] == "blocked"
        assert status["label"] == "Entry gate blocked"
        assert status["queue_freshness"] == "fresh"
        assert status["cycle_freshness"] == "fresh"
        assert status["ticket_packet_freshness"] == "fresh"
        assert status["entry_gate_status"] == "blocked"
        assert status["fresh_entries_allowed"] is False
        assert status["ticket_preflight_block_count"] >= 1
        assert status["ticket_preflight_warn_count"] >= 1
        assert status["live_ticket_count"] == 1
        assert status["paper_open_count"] == 1
        assert status["paper_book_summary"]["priced_count"] == 1
        assert status["paper_book_summary"]["needs_quote_count"] == 0
        assert status["paper_book_summary"]["unrealized_pnl_dollars"] == 30.0
        assert status["decision_recent_count"] == 1
        assert status["decision_log_needed"] is False
        assert status["decision_debt_reasons"] == []
        assert "validation max drawdown is -72.4%" in status["blockers"]
        assert status["warnings"] == ["execution mode defaults to approval_required"]
        assert status["tickets"][0]["paper_tracked"] is True
        assert status["tickets"][0]["optedge_duplicate"] is True
        assert status["tickets"][0]["confirmation_required"] is True
        assert status["tickets"][0]["preflight_status"] == "blocked"
        assert status["tickets"][0]["preflight_blocks"] >= 1
        assert any(row["check"] == "Entry gate" and row["level"] == "block" for row in status["ticket_preflight"])
        assert any(row["check"] == "Paper duplicate" and row["level"] == "warn" for row in status["ticket_preflight"])
        assert status["tickets"][0]["freshness"] == "fresh"
        assert status["paper_positions"][0]["contract"] == contract
        assert status["paper_positions"][0]["current_price"] == 1.5
        assert round(status["paper_positions"][0]["pnl_pct"], 4) == 0.25
        assert status["paper_positions"][0]["pnl_dollars"] == 30.0
        assert status["paper_positions"][0]["review_action"] == "hold_review"
        assert status["paper_positions"][0]["mark_source"] == "test_chain"
        actions = [row["action"] for row in status["next_actions"]]
        assert "review_validation" in actions
        assert "review_ticket" in actions
        assert "review_paper_book" in actions


def test_agentic_autopilot_status_blocks_stale_packets_and_tickets():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        stale_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        ticket = {
            "symbol": "VICI",
            "contract": "VICI 2026-09-18 P 27.5",
            "option_side": "put",
            "strike": 27.5,
            "expiry": "2026-09-18",
            "generated_at": stale_time,
            "quantity": 1,
            "limit_price": 1.08,
            "confidence": 94,
            "rank_score": 1.9,
        }
        (data_dir / "robinhood_agentic_queue.json").write_text(json.dumps({
            "status": "ready",
            "generated_at": stale_time,
            "orders": [ticket],
        }), encoding="utf-8")
        (data_dir / "robinhood_agentic_cycle.json").write_text(json.dumps({
            "generated_at": stale_time,
            "auto_submit_allowed": True,
            "entry_gate": {
                "status": "open",
                "label": "Fresh entries open",
                "new_entries_allowed_after_live_checks": True,
                "blockers": [],
                "warnings": [],
            },
            "entry_candidates": [ticket],
        }), encoding="utf-8")
        (data_dir / "robinhood_live_order_tickets.json").write_text(json.dumps({
            "generated_at": stale_time,
            "tickets": [ticket],
        }), encoding="utf-8")
        (data_dir / "agentic_paper_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")

        status = build_agentic_autopilot_status(data_dir)

        assert status["status"] == "stale"
        assert status["label"] == "Refresh required"
        assert status["fresh_entries_allowed"] is False
        assert status["queue_freshness"] == "stale"
        assert status["cycle_freshness"] == "stale"
        assert status["ticket_packet_freshness"] == "stale"
        assert any("agentic queue is stale" in blocker for blocker in status["blockers"])
        assert any("agentic cycle is stale" in blocker for blocker in status["blockers"])
        assert any("live ticket packet is stale" in blocker for blocker in status["blockers"])
        assert status["tickets"][0]["status"] == "stale"
        assert status["tickets"][0]["freshness"] == "stale"
        assert status["tickets"][0]["preflight_status"] == "blocked"
        assert any(row["check"] == "Fresh packet" and row["level"] == "block" for row in status["ticket_preflight"])
        assert status["next_actions"][0]["action"] == "refresh_autopilot_packet"


def test_agentic_autopilot_preflight_passes_clean_confirmation_ticket():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        now = datetime.now(timezone.utc).isoformat()
        ticket = {
            "symbol": "AAPL",
            "contract": "AAPL 2027-01-15 C 200",
            "option_side": "call",
            "strike": 200,
            "expiry": "2027-01-15",
            "generated_at": now,
            "quantity": 1,
            "limit_price": 1.25,
            "estimated_premium_dollars": 125.0,
            "confirmation_required": True,
            "live_submit_allowed_by_this_script": False,
        }
        ticket["robinhood_mcp_review_plan"] = robinhood_mcp_option_review_plan(ticket)
        (data_dir / "robinhood_agentic_queue.json").write_text(json.dumps({
            "status": "ready",
            "generated_at": now,
            "orders": [ticket],
        }), encoding="utf-8")
        (data_dir / "robinhood_agentic_cycle.json").write_text(json.dumps({
            "generated_at": now,
            "auto_submit_allowed": False,
            "entry_gate": {
                "status": "open",
                "label": "Fresh entries open",
                "new_entries_allowed_after_live_checks": True,
                "blockers": [],
                "warnings": [],
            },
            "entry_candidates": [ticket],
        }), encoding="utf-8")
        (data_dir / "robinhood_live_order_tickets.json").write_text(json.dumps({
            "generated_at": now,
            "tickets": [ticket],
        }), encoding="utf-8")
        (data_dir / "robinhood_broker_snapshot.json").write_text(json.dumps({
            "generated_at": now,
            "accounts": [{
                "nickname": "Agentic",
                "agentic_allowed": True,
                "option_level": "option_level_2",
                "option_positions": [],
            }],
        }), encoding="utf-8")
        (data_dir / "agentic_paper_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")

        status = build_agentic_autopilot_status(data_dir)

        assert status["status"] == "ticket_ready"
        assert status["fresh_entries_allowed"] is True
        assert status["ticket_preflight_block_count"] == 0
        assert status["ticket_preflight_warn_count"] == 0
        assert status["decision_recent_count"] == 0
        assert status["decision_log_needed"] is True
        assert any("staged live ticket" in reason for reason in status["decision_debt_reasons"])
        assert status["tickets"][0]["preflight_status"] == "pass"
        assert status["tickets"][0]["preflight_blocks"] == 0
        assert status["tickets"][0]["preflight_warnings"] == 0
        assert status["tickets"][0]["mcp_review_status"] == "ready"
        assert status["tickets"][0]["mcp_review_tool"] == "review_option_order"
        assert status["tickets"][0]["mcp_place_tool"] == "place_option_order"
        assert status["tickets"][0]["mcp_lookup_symbol"] == "AAPL"
        checks = {(row["check"], row["level"]) for row in status["ticket_preflight"]}
        assert ("Fresh packet", "pass") in checks
        assert ("Entry gate", "pass") in checks
        assert ("Confirmation", "pass") in checks
        assert ("Execution mode", "pass") in checks
        assert ("MCP review plan", "pass") in checks
        actions = [row["action"] for row in status["next_actions"]]
        assert "log_decision" in actions


def test_agentic_autopilot_warns_when_live_ticket_lacks_mcp_review_plan():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        now = datetime.now(timezone.utc).isoformat()
        ticket = {
            "symbol": "AAPL",
            "contract": "AAPL 2027-01-15 C 200",
            "option_side": "call",
            "strike": 200,
            "expiry": "2027-01-15",
            "generated_at": now,
            "quantity": 1,
            "limit_price": 1.25,
            "estimated_premium_dollars": 125.0,
            "confirmation_required": True,
            "live_submit_allowed_by_this_script": False,
        }
        (data_dir / "robinhood_agentic_queue.json").write_text(json.dumps({
            "status": "ready",
            "generated_at": now,
            "orders": [ticket],
        }), encoding="utf-8")
        (data_dir / "robinhood_agentic_cycle.json").write_text(json.dumps({
            "generated_at": now,
            "auto_submit_allowed": False,
            "entry_gate": {
                "status": "open",
                "new_entries_allowed_after_live_checks": True,
                "blockers": [],
                "warnings": [],
            },
            "entry_candidates": [ticket],
        }), encoding="utf-8")
        (data_dir / "robinhood_live_order_tickets.json").write_text(json.dumps({
            "generated_at": now,
            "tickets": [ticket],
        }), encoding="utf-8")
        (data_dir / "robinhood_broker_snapshot.json").write_text(json.dumps({
            "generated_at": now,
            "accounts": [{
                "nickname": "Agentic",
                "agentic_allowed": True,
                "option_level": "option_level_2",
                "option_positions": [],
            }],
        }), encoding="utf-8")
        (data_dir / "agentic_paper_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")

        status = build_agentic_autopilot_status(data_dir)

        assert status["tickets"][0]["mcp_review_status"] == "missing"
        assert status["tickets"][0]["preflight_status"] == "warn"
        assert status["ticket_preflight_warn_count"] >= 1
        assert any(
            row["check"] == "MCP review plan" and row["level"] == "warn"
            for row in status["ticket_preflight"]
        )


def test_cockpit_can_normalize_raw_robinhood_snapshot_for_reconciliation():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        (data_dir / "robinhood_mcp_snapshot_raw.json").write_text(json.dumps({
            "accounts": [{
                "account_number": "FAKE123456",
                "nickname": "Agentic",
                "agentic_allowed": True,
                "option_level": "option_level_2",
            }],
            "option_positions": {
                "results": [{
                    "account_number": "FAKE123456",
                    "chain_symbol": "AAPL",
                    "option_type": "call",
                    "strike_price": "200",
                    "expiration_date": "2027-01-15",
                    "quantity": "1",
                    "average_price": "1.25",
                    "mark_price": "1.55",
                }],
            },
        }), encoding="utf-8")
        (data_dir / "open_positions.json").write_text(json.dumps([{
            "ticker": "AAPL",
            "side": "call",
            "strike": 200,
            "expiry": "2027-01-15",
            "quantity": 1,
        }]), encoding="utf-8")

        preview = normalize_robinhood_broker_snapshot_file(data_dir, dry_run=True)
        assert preview["ok"] is True
        assert preview["dry_run"] is True
        assert not (data_dir / "robinhood_broker_snapshot.json").exists()

        result = normalize_robinhood_broker_snapshot_file(data_dir)

        assert result["ok"] is True
        assert result["does_not_place_orders"] is True
        assert result["summary"]["option_positions"] == 1
        assert (data_dir / "robinhood_broker_snapshot.json").exists()
        assert result["broker_reconciliation"]["broker_option_count"] == 1
        assert result["broker_reconciliation"]["matched_count"] == 1


def test_broker_reconciliation_surfaces_broker_and_local_mismatches():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        now = datetime.now(timezone.utc).isoformat()
        expired_expiry = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
        (data_dir / "open_positions.json").write_text(json.dumps([
            {
                "ticker": "AAPL",
                "side": "call",
                "strike": 200.0,
                "expiry": "2027-01-15",
                "current_price": 1.5,
                "suggested_contracts": 1,
            },
            {
                "ticker": "MSFT",
                "side": "put",
                "strike": 300,
                "expiry": "2027-01-15",
                "current_price": 2.0,
                "suggested_contracts": 1,
            },
            {
                "ticker": "GOOG",
                "side": "call",
                "strike": 100,
                "expiry": expired_expiry,
                "current_price": 0.0,
                "suggested_contracts": 1,
            },
        ]), encoding="utf-8")
        (data_dir / "agentic_paper_positions.json").write_text(json.dumps([{
            "status": "open",
            "symbol": "TSLA",
            "option_side": "call",
            "strike": "250.00",
            "expiry": "2027-01-15",
            "quantity": 1,
            "entry_price": 1.0,
        }]), encoding="utf-8")
        (data_dir / "robinhood_broker_snapshot.json").write_text(json.dumps({
            "generated_at": now,
            "accounts": [
                {
                    "nickname": "Default",
                    "agentic_allowed": False,
                    "option_level": "option_level_2",
                    "option_positions": [
                        {
                            "chain_symbol": "AAPL",
                            "option_type": "call",
                            "strike_price": "200",
                            "expiration_date": "2027-01-15",
                            "quantity": "1.0000",
                            "average_price": "1.2500",
                        },
                        {
                            "chain_symbol": "ROBN",
                            "option_type": "call",
                            "strike_price": "20.00",
                            "expiration_date": "2026-12-18",
                            "quantity": "2.0000",
                            "average_price": "6.4500",
                        },
                    ],
                },
                {
                    "nickname": "Agentic",
                    "agentic_allowed": True,
                    "option_level": "",
                    "option_positions": [],
                },
            ],
        }), encoding="utf-8")

        report = build_broker_reconciliation(data_dir)

        assert report["status"] == "mismatch"
        assert report["broker_option_count"] == 2
        assert report["optedge_option_count"] == 3
        assert report["paper_option_count"] == 1
        assert report["matched_count"] == 1
        assert report["broker_only_count"] == 1
        assert report["local_only_count"] == 1
        assert report["local_expired_count"] == 1
        assert report["paper_only_count"] == 1
        assert report["agentic_option_ready"] is False
        assert any("options-approved account exists" in warning for warning in report["warnings"])
        statuses = {row["contract"]: row["status"] for row in report["rows"]}
        assert statuses["AAPL 2027-01-15 CALL 200"] == "matched"
        assert statuses["ROBN 2026-12-18 CALL 20"] == "broker_only"
        assert statuses["MSFT 2027-01-15 PUT 300"] == "local_only"
        assert statuses[f"GOOG {expired_expiry} CALL 100"] == "local_expired"
        assert statuses["TSLA 2027-01-15 CALL 250"] == "paper_only"


def test_agentic_autopilot_blocks_ticket_when_broker_reconciliation_mismatches():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        now = datetime.now(timezone.utc).isoformat()
        ticket = {
            "symbol": "AAPL",
            "contract": "AAPL 2027-01-15 C 200",
            "option_side": "call",
            "strike": 200,
            "expiry": "2027-01-15",
            "generated_at": now,
            "quantity": 1,
            "limit_price": 1.25,
            "estimated_premium_dollars": 125.0,
            "confirmation_required": True,
            "live_submit_allowed_by_this_script": False,
        }
        (data_dir / "robinhood_agentic_queue.json").write_text(json.dumps({
            "status": "ready",
            "generated_at": now,
            "orders": [ticket],
        }), encoding="utf-8")
        (data_dir / "robinhood_agentic_cycle.json").write_text(json.dumps({
            "generated_at": now,
            "auto_submit_allowed": False,
            "entry_gate": {
                "status": "open",
                "label": "Fresh entries open",
                "new_entries_allowed_after_live_checks": True,
                "blockers": [],
                "warnings": [],
            },
            "entry_candidates": [ticket],
        }), encoding="utf-8")
        (data_dir / "robinhood_live_order_tickets.json").write_text(json.dumps({
            "generated_at": now,
            "tickets": [ticket],
        }), encoding="utf-8")
        (data_dir / "agentic_paper_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "robinhood_broker_snapshot.json").write_text(json.dumps({
            "generated_at": now,
            "accounts": [{
                "nickname": "Default",
                "agentic_allowed": False,
                "option_level": "option_level_2",
                "option_positions": [{
                    "chain_symbol": "ROBN",
                    "option_type": "call",
                    "strike_price": "20",
                    "expiration_date": "2026-12-18",
                    "quantity": "2.0000",
                }],
            }],
        }), encoding="utf-8")

        status = build_agentic_autopilot_status(data_dir)

        assert status["status"] == "blocked"
        assert status["broker_reconciliation_status"] == "mismatch"
        assert status["broker_only_count"] == 1
        assert status["broker_reconciliation_rows"][0]["status"] == "broker_only"
        assert any("broker/local position mismatch" in blocker for blocker in status["blockers"])
        actions = [row["action"] for row in status["next_actions"]]
        assert "review_broker_sync" in actions
        assert status["tickets"][0]["preflight_status"] == "blocked"


def test_position_hygiene_builds_safe_cleanup_plan_without_mutating_positions():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        now = datetime.now(timezone.utc).isoformat()
        expired_expiry = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
        original_open = [
            {
                "ticker": "AAPL",
                "side": "call",
                "strike": 200,
                "expiry": expired_expiry,
                "entry_price": 1.0,
                "current_price": 0.0,
                "suggested_contracts": 1,
                "reprice_failed_count": 3,
            },
            {
                "ticker": "MSFT",
                "side": "put",
                "strike": 300,
                "expiry": "2027-01-15",
                "entry_price": 2.0,
                "current_price": 2.5,
                "suggested_contracts": 1,
                "reprice_failed_count": 25,
            },
        ]
        (data_dir / "open_positions.json").write_text(json.dumps(original_open), encoding="utf-8")
        (data_dir / "agentic_paper_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "robinhood_broker_snapshot.json").write_text(json.dumps({
            "generated_at": now,
            "accounts": [{
                "nickname": "Default",
                "agentic_allowed": False,
                "option_level": "option_level_2",
                "option_positions": [{
                    "chain_symbol": "ROBN",
                    "option_type": "call",
                    "strike_price": "35",
                    "expiration_date": "2026-12-18",
                    "quantity": "2.0000",
                }],
            }],
        }), encoding="utf-8")

        report = build_position_hygiene(data_dir)

        assert report["status"] == "needs_review"
        assert report["broker_only_count"] == 1
        assert report["local_expired_count"] == 1
        assert report["local_only_count"] == 1
        actions = {row["contract"]: row["action"] for row in report["rows"]}
        assert actions["ROBN 2026-12-18 CALL 35"] == "import_or_mark_unmanaged_broker_position"
        assert actions[f"AAPL {expired_expiry} CALL 200"] == "close_or_archive_expired_local_record"
        assert actions["MSFT 2027-01-15 PUT 300"] == "verify_local_position_against_broker"
        assert json.loads((data_dir / "open_positions.json").read_text(encoding="utf-8")) == original_open

        written = write_position_hygiene_plan(data_dir)
        assert written["wrote_file"] is True
        assert (data_dir / "position_hygiene_plan.json").exists()
        saved = json.loads((data_dir / "position_hygiene_plan.json").read_text(encoding="utf-8"))
        assert saved["action_count"] == report["action_count"]


def test_position_hygiene_apply_preview_does_not_mutate_positions():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        expired_expiry = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
        future_expiry = (datetime.now(timezone.utc) + timedelta(days=120)).date().isoformat()
        open_rows = [
            {
                "ticker": "AAPL",
                "side": "call",
                "strike": 200,
                "expiry": expired_expiry,
                "entry_time": (datetime.now(timezone.utc) - timedelta(days=20)).isoformat(),
                "entry_price": 1.0,
                "current_price": 0.25,
            },
            {
                "ticker": "MSFT",
                "side": "put",
                "strike": 300,
                "expiry": future_expiry,
                "entry_price": 2.0,
                "current_price": 2.5,
            },
        ]
        closed_rows = [{"ticker": "OLD", "exit_reason": "hard_target"}]
        (data_dir / "open_positions.json").write_text(json.dumps(open_rows), encoding="utf-8")
        (data_dir / "closed_positions.json").write_text(json.dumps(closed_rows), encoding="utf-8")

        report = apply_position_hygiene(data_dir, apply=False)

        assert report["status"] == "preview"
        assert report["expired_to_close_count"] == 1
        assert report["open_before"] == 2
        assert report["open_after"] == 2
        assert report["closed_before"] == 1
        assert report["closed_after"] == 1
        assert report["backup_paths"] == []
        assert report["rows"][0]["action"] == "preview_move_to_closed_positions"
        assert json.loads((data_dir / "open_positions.json").read_text(encoding="utf-8")) == open_rows
        assert json.loads((data_dir / "closed_positions.json").read_text(encoding="utf-8")) == closed_rows
        assert list(data_dir.glob("*.hygiene_backup_*.json")) == []


def test_position_hygiene_apply_backs_up_and_moves_only_expired_options():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        expired_expiry = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
        future_expiry = (datetime.now(timezone.utc) + timedelta(days=120)).date().isoformat()
        open_rows = [
            {
                "ticker": "AAPL",
                "side": "call",
                "strike": 200,
                "expiry": expired_expiry,
                "entry_time": (datetime.now(timezone.utc) - timedelta(days=20)).isoformat(),
                "entry_price": 1.0,
                "current_price": 0.25,
                "trade_status": "Watch",
            },
            {
                "ticker": "MSFT",
                "side": "put",
                "strike": 300,
                "expiry": future_expiry,
                "entry_price": 2.0,
                "current_price": 2.5,
                "trade_status": "Trade",
            },
        ]
        closed_rows = [{"ticker": "OLD", "exit_reason": "hard_target"}]
        (data_dir / "open_positions.json").write_text(json.dumps(open_rows), encoding="utf-8")
        (data_dir / "closed_positions.json").write_text(json.dumps(closed_rows), encoding="utf-8")

        report = apply_position_hygiene(data_dir, apply=True)

        assert report["status"] == "applied"
        assert report["wrote_files"] is True
        assert report["expired_to_close_count"] == 1
        assert report["open_before"] == 2
        assert report["open_after"] == 1
        assert report["closed_before"] == 1
        assert report["closed_after"] == 2
        assert len(report["backup_paths"]) == 2
        assert all(Path(path).exists() for path in report["backup_paths"])
        remaining_open = json.loads((data_dir / "open_positions.json").read_text(encoding="utf-8"))
        saved_closed = json.loads((data_dir / "closed_positions.json").read_text(encoding="utf-8"))
        assert [row["ticker"] for row in remaining_open] == ["MSFT"]
        closed = saved_closed[-1]
        assert closed["ticker"] == "AAPL"
        assert closed["exit_reason"] == "expired_hygiene"
        assert closed["exit_price"] == 0.0
        assert closed["pnl_pct"] == -1.0
        assert closed["trade_status"] == "Closed"
        assert closed["hygiene_source"] == "position_hygiene"
        assert "broker orders" in report["notes"][-1]


def test_agentic_autopilot_paper_book_marks_targets_and_missing_quotes():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        now = datetime.now(timezone.utc).isoformat()
        future_expiry = (datetime.now(timezone.utc) + timedelta(days=180)).date().isoformat()
        (data_dir / "robinhood_agentic_queue.json").write_text(json.dumps({
            "status": "empty",
            "generated_at": now,
            "orders": [],
        }), encoding="utf-8")
        (data_dir / "robinhood_agentic_cycle.json").write_text(json.dumps({
            "generated_at": now,
            "auto_submit_allowed": False,
            "entry_gate": {
                "status": "blocked",
                "label": "Fresh entries blocked",
                "new_entries_allowed_after_live_checks": False,
                "blockers": ["validation blocked"],
                "warnings": [],
            },
        }), encoding="utf-8")
        (data_dir / "robinhood_live_order_tickets.json").write_text(json.dumps({
            "generated_at": now,
            "tickets": [],
        }), encoding="utf-8")
        (data_dir / "agentic_paper_positions.json").write_text(json.dumps([
            {
                "status": "open",
                "symbol": "AAPL",
                "contract": f"AAPL {future_expiry} C 200",
                "option_side": "call",
                "strike": 200,
                "expiry": future_expiry,
                "quantity": 1,
                "entry_price": 1.0,
                "stop_price_reference": 0.5,
                "target_price_reference": 2.0,
                "opened_at": now,
            },
            {
                "status": "open",
                "symbol": "MSFT",
                "contract": f"MSFT {future_expiry} P 300",
                "option_side": "put",
                "strike": 300,
                "expiry": future_expiry,
                "quantity": 1,
                "entry_price": 1.5,
                "stop_price_reference": 0.75,
                "target_price_reference": 3.0,
                "opened_at": now,
            },
        ]), encoding="utf-8")
        (data_dir / "open_positions.json").write_text(json.dumps([
            {
                "ticker": "AAPL",
                "side": "call",
                "strike": 200,
                "expiry": future_expiry,
                "current_price": 2.2,
                "latest_exit_action": "hold",
                "latest_exit_pressure": 10,
                "last_reprice_source": "test_chain",
            }
        ]), encoding="utf-8")

        status = build_agentic_autopilot_status(data_dir)

        assert status["paper_open_count"] == 2
        assert status["paper_book_summary"]["priced_count"] == 1
        assert status["paper_book_summary"]["needs_quote_count"] == 1
        assert status["paper_book_summary"]["review_count"] == 2
        rows = {row["symbol"]: row for row in status["paper_positions"]}
        assert rows["AAPL"]["review_action"] == "target_review"
        assert round(rows["AAPL"]["pnl_dollars"], 2) == 120.0
        assert round(rows["AAPL"]["pnl_pct"], 4) == 1.2
        assert rows["MSFT"]["review_action"] == "refresh_quote"
        assert rows["MSFT"]["current_price"] is None


def test_agentic_autopilot_paper_book_does_not_fake_zero_pnl_without_quotes():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        now = datetime.now(timezone.utc).isoformat()
        future_expiry = (datetime.now(timezone.utc) + timedelta(days=180)).date().isoformat()
        (data_dir / "robinhood_agentic_queue.json").write_text(json.dumps({
            "status": "empty",
            "generated_at": now,
            "orders": [],
        }), encoding="utf-8")
        (data_dir / "robinhood_agentic_cycle.json").write_text(json.dumps({
            "generated_at": now,
            "auto_submit_allowed": False,
            "entry_gate": {
                "status": "blocked",
                "label": "Fresh entries blocked",
                "new_entries_allowed_after_live_checks": False,
                "blockers": [],
                "warnings": [],
            },
        }), encoding="utf-8")
        (data_dir / "robinhood_live_order_tickets.json").write_text(json.dumps({
            "generated_at": now,
            "tickets": [],
        }), encoding="utf-8")
        (data_dir / "agentic_paper_positions.json").write_text(json.dumps([{
            "status": "open",
            "symbol": "MSFT",
            "contract": f"MSFT {future_expiry} P 300",
            "option_side": "put",
            "strike": 300,
            "expiry": future_expiry,
            "quantity": 1,
            "entry_price": 1.5,
            "stop_price_reference": 0.75,
            "target_price_reference": 3.0,
            "opened_at": now,
        }]), encoding="utf-8")
        (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")

        status = build_agentic_autopilot_status(data_dir)

        assert status["paper_book_summary"]["priced_count"] == 0
        assert status["paper_book_summary"]["needs_quote_count"] == 1
        assert status["paper_book_summary"]["current_value_dollars"] is None
        assert status["paper_book_summary"]["unrealized_pnl_dollars"] is None
        assert status["paper_positions"][0]["review_action"] == "refresh_quote"


def test_option_chain_scan_fetches_and_filters_contracts():
    original = cockpit_module._fetch_option_chain

    def fake_fetch(ticker: str, cache_age: int = 600):
        assert ticker == "AAPL"
        return {
            "spot": 200.0,
            "source": "cboe",
            "quote_quality": "free_or_delayed",
            "data_delay": "delayed",
            "source_attempts": [
                {"provider": "cboe", "status": "ok", "rows": 3, "expirations": 2},
            ],
            "expirations": ["2027-01-15", "2026-06-18"],
            "chains": {
                "2027-01-15": pd.DataFrame([
                    {
                        "strike": 220.0,
                        "side": "call",
                        "bid": 4.90,
                        "ask": 5.10,
                        "lastPrice": 5.00,
                        "volume": 50,
                        "openInterest": 1000,
                        "impliedVolatility": 0.30,
                        "delta": 0.42,
                    },
                    {
                        "strike": 300.0,
                        "side": "call",
                        "bid": 1.00,
                        "ask": 2.00,
                        "lastPrice": 1.50,
                        "volume": 10,
                        "openInterest": 25,
                    },
                ]),
                "2026-06-18": pd.DataFrame([
                    {
                        "strike": 180.0,
                        "side": "put",
                        "bid": 2.00,
                        "ask": 2.10,
                        "lastPrice": 2.05,
                        "volume": 20,
                        "openInterest": 150,
                    },
                ]),
            },
        }

    try:
        cockpit_module._fetch_option_chain = fake_fetch
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            (data_dir / "open_positions.json").write_text(json.dumps([{
                "ticker": "AAPL",
                "side": "call",
                "strike": 220.0,
                "expiry": "2027-01-15",
                "entry_price": 4.0,
                "current_mid": 5.0,
                "latest_exit_pressure": 25,
            }]), encoding="utf-8")
            report = build_option_chain_scan(
                "AAPL",
                data_dir=data_dir,
                side="call",
                min_dte=180,
                max_dte=400,
                max_spread_pct=0.10,
                max_premium=600,
            )
    finally:
        cockpit_module._fetch_option_chain = original

    assert report["ok"] is True
    assert report["symbol"] == "AAPL"
    assert report["source"] == "cboe"
    assert report["quote_quality"] == "free_or_delayed"
    assert report["data_delay"] == "delayed"
    assert report["providers_checked"] == 1
    assert report["source_attempts"][0]["provider"] == "cboe"
    assert report["total_contracts"] == 3
    assert report["filtered_count"] == 1
    assert report["rejected_count"] == 2
    assert report["rejection_reason_counts"]["spread above filter"] == 1
    assert report["rejection_reason_counts"]["side is not call"] == 1
    assert report["top_rejection_reasons"][0]["count"] == 1
    assert len(report["rejection_examples"]) == 2
    reject_reasons = [reason for row in report["rejection_examples"] for reason in row["reasons"]]
    assert "spread above filter" in reject_reasons
    assert "side is not call" in reject_reasons
    wide_reject = [row for row in report["rejection_examples"] if row["reason"] == "spread above filter"][0]
    assert wide_reject["strike"] == 300.0
    assert wide_reject["side"] == "call"
    assert wide_reject["premium_dollars"] == 150.0
    row = report["rows"][0]
    assert row["side"] == "call"
    assert row["strike"] == 220.0
    assert row["chain_source"] == "cboe"
    assert row["quote_quality"] == "free_or_delayed"
    assert row["data_delay"] == "delayed"
    assert row["premium_dollars"] == 500.0
    assert row["breakeven_price"] == 225.0
    assert row["breakeven_direction"] == "up"
    assert row["breakeven_move_pct"] == 0.125
    assert row["budget_usage_pct"] == 0.8333
    assert row["contracts_for_budget"] == 1
    assert row["budget_fit"] == "inside_budget"
    assert row["stop_price_reference"] == 2.5
    assert row["target_price_reference"] == 10.0
    assert row["risk_dollars_reference"] == 250.0
    assert row["reward_dollars_reference"] == 500.0
    assert row["reward_risk_reference"] == 2.0
    assert row["contract_query"] == "AAPL 2027-01-15 C 220"
    assert row["spread_pct"] < 0.10
    assert row["dte_bucket"] in {"180-364d", "365d+"}
    assert row["swing_fit_label"] == "clean_swing"
    assert row["swing_fit_score"] >= 85
    assert row["breakeven_move_label"] == "moderate"
    assert row["liquidity_label"] == "deep"
    assert "long swing runway" in row["swing_fit_reasons"]
    assert "verify delayed quote" in row["swing_fit_warnings"]
    assert row["readiness_label"] in {"ready", "review"}
    assert row["readiness_score"] >= 65
    assert row["contract_grade"] == "A"
    assert row["review_lane"] == "primary_review"
    assert row["chain_factor_summary"]
    chain_factors = {item["factor"] for item in row["chain_factor_breakdown"]}
    assert {"Runway", "Liquidity", "Spread", "Budget", "Break-even", "Swing fit"} <= chain_factors
    assert "inside premium budget" in row["grade_reasons"]
    assert "A-grade" in row["review_thesis"]
    assert "12.5% break-even move" in row["review_thesis"]
    assert report["preset"] == "custom"
    assert report["scan_summary"]["best_call"].startswith("C 220")
    assert report["summary"] == report["scan_summary"]
    assert report["scan_summary"]["under_budget_count"] == 1
    assert report["scan_summary"]["review_count"] >= 1
    assert report["scan_summary"]["best_reviewable"].startswith("C 220")
    assert report["scan_summary"]["best_budget"].startswith("C 220")
    assert report["scan_summary"]["best_liquid"].startswith("C 220")
    assert report["scan_summary"]["best_long_dated"].startswith("C 220")
    assert report["scan_summary"]["best_swing_fit"].startswith("C 220")
    assert report["scan_summary"]["swing_fit_counts"]["clean_swing"] == 1
    assert report["scan_summary"]["clean_swing_count"] == 1
    assert report["scan_summary"]["grade_counts"]["A"] == 1
    assert report["scan_summary"]["primary_review_count"] == 1
    assert report["open_exposure"]["has_open"] is True
    assert report["open_exposure"]["open_count"] == 1
    assert report["open_exposure"]["asset_counts"] == {"option": 1}
    assert report["decision"]["status"] == "primary_review"
    assert report["decision"]["label"] == "Best contract"
    assert report["decision"]["primary"]["contract_query"] == "AAPL 2027-01-15 C 220"
    assert report["decision"]["primary"]["chain_factor_summary"] == row["chain_factor_summary"]
    assert report["decision"]["open_exposure"]["open_count"] == 1
    assert "Duplicate exposure check" in " ".join(report["decision"]["risk_notes"])
    assert report["decision"]["saveable_count"] == 1
    trade_plan = report["decision"]["trade_plan"]
    assert trade_plan["action"] == "review_contract"
    assert trade_plan["contract"] == "AAPL 2027-01-15 C 220"
    assert trade_plan["quantity"] == 1
    assert trade_plan["entry_price_reference"] == 5.0
    assert trade_plan["premium_dollars_reference"] == 500.0
    assert trade_plan["max_loss_dollars_reference"] == 500.0
    assert trade_plan["stop_price_reference"] == 2.5
    assert trade_plan["target_price_reference"] == 10.0
    assert trade_plan["stop_loss_dollars_reference"] == 250.0
    assert trade_plan["target_gain_dollars_reference"] == 500.0
    assert trade_plan["reward_risk_reference"] == 2.0
    assert trade_plan["breakeven_price"] == 225.0
    assert trade_plan["breakeven_move_pct"] == 0.125
    assert trade_plan["budget_fit"] == "inside_budget"
    assert "Refresh live bid/ask" in trade_plan["checklist"][0]
    assert "Quote may be free/delayed" in " ".join(report["decision"]["risk_notes"])
    assert report["expiry_summary"][0]["expiry"] == "2027-01-15"
    assert report["expiry_summary"][0]["reviewable_count"] == 1
    assert report["expiry_summary"][0]["under_budget_count"] == 1
    assert report["expiry_summary"][0]["primary_review_count"] == 1
    assert report["expiry_summary"][0]["clean_swing_count"] == 1
    assert report["expiry_summary"][0]["best_budget"].startswith("C 220")
    assert report["expiry_summary"][0]["best_budget_grade"] == "A"
    assert report["expiry_summary"][0]["best_budget_fit"] == "inside_budget"
    assert report["expiry_summary"][0]["best_budget_premium"] == 500.0
    assert report["expiry_summary"][0]["best_budget_spread_pct"] < 0.10


def test_option_chain_batch_scans_shortlist_and_ranks_contracts():
    original = cockpit_module.build_option_chain_scan

    def fake_scan(query: str, *args, **kwargs):
        symbol = str(query).upper()
        if symbol == "MSFT":
            rows = [{
                "symbol": "MSFT",
                "side": "call",
                "expiry": "2027-01-15",
                "dte": 220,
                "strike": 450.0,
                "mid": 4.0,
                "premium_dollars": 400.0,
                "spread_pct": 0.03,
                "openInterest": 1500,
                "volume": 100,
                "contract_quality_score": 94.0,
                "contract_grade": "A",
                "review_lane": "primary_review",
                "review_thesis": "A-grade test contract.",
                "grade_reasons": ["tight spread", "liquid"],
                "contract_query": "MSFT 2027-01-15 C 450",
            }]
        else:
            rows = [{
                "symbol": "AAPL",
                "side": "call",
                "expiry": "2027-01-15",
                "dte": 220,
                "strike": 220.0,
                "mid": 5.0,
                "premium_dollars": 500.0,
                "spread_pct": 0.05,
                "openInterest": 900,
                "volume": 50,
                "contract_quality_score": 80.0,
                "contract_grade": "B",
                "review_lane": "secondary_review",
                "review_thesis": "B-grade test contract.",
                "grade_reasons": ["acceptable spread"],
                "contract_query": "AAPL 2027-01-15 C 220",
            }]
        return {
            "ok": True,
            "symbol": symbol,
            "source": "cboe",
            "quote_quality": "free_or_delayed",
            "data_delay": "delayed",
            "open_exposure": {
                "has_open": symbol == "AAPL",
                "open_count": 2 if symbol == "AAPL" else 0,
                "asset_counts": {"option": 1, "share": 1} if symbol == "AAPL" else {},
                "attention_count": 1 if symbol == "AAPL" else 0,
                "summary": "2 open AAPL position(s)" if symbol == "AAPL" else "No open positions found",
            },
            "total_contracts": 20,
            "rejected_count": 4,
            "top_rejection_reasons": [{"reason": "spread above filter", "count": 4}],
            "filtered_count": len(rows),
            "scan_summary": {
                "grade_counts": {rows[0]["contract_grade"]: len(rows)},
                "best_reviewable": rows[0]["contract_query"],
            },
            "rows": rows,
        }

    try:
        cockpit_module.build_option_chain_scan = fake_scan
        with tempfile.TemporaryDirectory() as td:
            report = build_option_chain_batch(
                Path(td),
                query="AAPL,MSFT",
                preset="swing",
                symbols_limit=5,
                contracts_per_symbol=2,
            )
    finally:
        cockpit_module.build_option_chain_scan = original

    assert report["ok"] is True
    assert report["candidate_count"] == 2
    assert report["successful_scans"] == 2
    assert report["row_count"] == 2
    assert report["grade_counts"] == {"B": 1, "A": 1}
    assert report["source_counts"] == {"cboe": 2}
    assert report["rows"][0]["symbol"] == "MSFT"
    assert report["rows"][0]["contract_grade"] == "A"
    assert report["rows"][0]["candidate_source"] == "typed shortlist"
    aapl_row = [row for row in report["rows"] if row["symbol"] == "AAPL"][0]
    assert aapl_row["open_exposure_count"] == 2
    assert aapl_row["open_exposure_assets"] == "option:1, share:1"
    assert aapl_row["open_exposure_attention_count"] == 1
    assert "open exposure" in " ".join(aapl_row["risk_flags"])
    assert report["open_exposure_count"] == 2
    assert report["open_exposure_symbols"] == ["AAPL"]
    assert report["symbol_summaries"][0]["quote_quality"] == "free_or_delayed"
    assert report["symbol_summaries"][0]["rejected_count"] == 4
    assert report["symbol_summaries"][0]["top_rejects"] == "spread above filter (4)"
    assert report["symbol_summaries"][0]["open_exposure_count"] == 2


def test_option_chain_batch_uses_swing_scout_candidates_when_blank():
    old_scan = cockpit_module.build_option_chain_scan
    old_gated = cockpit_module.build_climate_gated_setups
    old_scout = cockpit_module.build_swing_scout
    old_best = cockpit_module.build_best_setups

    def fake_scan(query: str, *args, **kwargs):
        symbol = str(query).upper()
        return {
            "ok": True,
            "symbol": symbol,
            "source": "cboe",
            "quote_quality": "free_or_delayed",
            "data_delay": "delayed",
            "total_contracts": 12,
            "rejected_count": 0,
            "top_rejection_reasons": [],
            "filtered_count": 1,
            "scan_summary": {
                "grade_counts": {"B": 1},
                "best_reviewable": f"{symbol} 2027-01-15 C 10",
            },
            "rows": [{
                "symbol": symbol,
                "side": "call",
                "expiry": "2027-01-15",
                "dte": 216,
                "strike": 10.0,
                "mid": 1.0,
                "premium_dollars": 100.0,
                "spread_pct": 0.08,
                "openInterest": 200,
                "volume": 25,
                "contract_quality_score": 75.0,
                "contract_grade": "B",
                "review_lane": "secondary_review",
                "review_thesis": "B-grade scout contract.",
                "grade_reasons": ["3m+ swing"],
                "contract_query": f"{symbol} 2027-01-15 C 10",
            }],
        }

    def fake_scout(*args, **kwargs):
        return {
            "rows": [
                {
                    "asset": "share",
                    "ticker_or_symbol": "SMOL",
                    "swing_scout_score": 91,
                    "lane": "small_cap_squeeze_watch",
                    "reasons": ["small cap", "short/squeeze pressure"],
                },
                {
                    "asset": "option",
                    "ticker_or_symbol": "RGTI",
                    "swing_scout_score": 86,
                    "lane": "small_cap_options_momentum",
                    "reasons": ["retail/attention lift"],
                },
                {
                    "asset": "share",
                    "ticker_or_symbol": "RISK",
                    "swing_scout_score": 94,
                    "lane": "nasdaq_small_cap_mover",
                    "review_action": "wait",
                    "readiness_label": "wait",
                    "trade_status": "Wait",
                    "active_halt": True,
                    "market_structure_risk_score": 98,
                    "market_structure_risk_flags": ["active_halt"],
                    "warnings": ["active trading halt"],
                    "warning_count": 1,
                    "reasons": ["Nasdaq screener upside momentum"],
                },
                {
                    "asset": "futures",
                    "ticker_or_symbol": "CL=F",
                    "swing_scout_score": 90,
                    "lane": "futures_macro_swing",
                    "reasons": ["futures/macro momentum"],
                },
            ],
        }

    cockpit_module.build_option_chain_scan = fake_scan
    cockpit_module.build_climate_gated_setups = lambda *args, **kwargs: {"rows": []}
    cockpit_module.build_swing_scout = fake_scout
    cockpit_module.build_best_setups = lambda *args, **kwargs: {"rows": []}
    try:
        with tempfile.TemporaryDirectory() as td:
            report = build_option_chain_batch(
                Path(td),
                query="",
                preset="swing",
                symbols_limit=5,
                contracts_per_symbol=1,
            )
    finally:
        cockpit_module.build_option_chain_scan = old_scan
        cockpit_module.build_climate_gated_setups = old_gated
        cockpit_module.build_swing_scout = old_scout
        cockpit_module.build_best_setups = old_best

    assert report["candidate_count"] == 2
    assert report["candidate_skipped_count"] == 1
    assert {row["symbol"] for row in report["candidates"]} == {"SMOL", "RGTI"}
    smol = next(row for row in report["candidates"] if row["symbol"] == "SMOL")
    rgti = next(row for row in report["candidates"] if row["symbol"] == "RGTI")
    assert smol["chain_fit_label"] == "high-conviction swing candidate"
    assert smol["chain_candidate_label"] == "High priority: share swing -> 3m+ options overlay"
    assert smol["chain_priority"] >= 80
    assert rgti["chain_candidate_label"] == "High priority: existing option thesis -> refresh chain"
    assert report["excluded_candidates"][0]["symbol"] == "RISK"
    assert report["excluded_candidates"][0]["reason_excluded"] == "active trading halt"
    assert report["excluded_candidates"][0]["market_structure_risk_score"] == 98
    assert all(row["symbol"] != "CL=F" for row in report["candidates"])
    assert {row["candidate_source"] for row in report["rows"]} == {
        "swing scout share",
        "swing scout option",
    }
    assert "swing scout winners" in report["notes"][1]


def test_option_chain_shortlist_writer_creates_portable_artifacts():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        report = {
            "generated_at": "2026-06-13T20:00:00+00:00",
            "preset": "swing",
            "query": "AAPL,MSFT",
            "candidate_count": 2,
            "symbols_scanned": 2,
            "successful_scans": 2,
            "grade_counts": {"A": 1},
            "source_counts": {"cboe": 1},
            "rows": [{
                "symbol": "AAPL",
                "contract_query": "AAPL 2027-01-15 C 220",
                "side": "call",
                "expiry": "2027-01-15",
                "strike": 220.0,
                "dte": 216,
                "mid": 5.0,
                "premium_dollars": 500.0,
                "bid": 4.9,
                "ask": 5.1,
                "spread_pct": 0.04,
                "openInterest": 1200,
                "volume": 80,
                "breakeven_price": 225.0,
                "breakeven_move_pct": 0.125,
                "budget_usage_pct": 1.0,
                "stop_price_reference": 2.5,
                "target_price_reference": 10.0,
                "risk_dollars_reference": 250.0,
                "reward_dollars_reference": 500.0,
                "reward_risk_reference": 2.0,
                "budget_fit": "inside_budget",
                "contract_grade": "A",
                "review_lane": "primary_review",
                "readiness_label": "ready",
                "readiness_score": 91,
                "contract_quality_score": 94,
                "swing_fit_label": "clean_swing",
                "swing_fit_score": 96,
                "batch_quote_quality": "free_or_delayed",
                "batch_source": "cboe",
                "batch_data_delay": "delayed",
                "candidate_source": "typed shortlist",
                "candidate_reason": "AAPL",
                "open_exposure_count": 1,
                "open_exposure_assets": "option:1",
                "open_exposure_summary": "1 open AAPL position(s)",
                "open_exposure_attention_count": 1,
                "risk_flags": ["free/delayed"],
                "grade_reasons": ["tight spread", "3m+ swing"],
                "review_thesis": "A-grade test contract.",
            }],
        }

        result = write_option_chain_shortlist(report, data_dir)
        assert result["ok"] is True
        assert result["count"] == 1
        assert artifact_path("option-chain-shortlist", data_dir) == data_dir / "option_chain_shortlist.csv"
        assert artifact_path("option-chain-shortlist-json", data_dir) == data_dir / "option_chain_shortlist.json"

        csv_text = (data_dir / "option_chain_shortlist.csv").read_text()
        payload = json.loads((data_dir / "option_chain_shortlist.json").read_text())
        assert "AAPL 2027-01-15 C 220" in csv_text
        assert "tight spread; 3m+ swing" in csv_text
        assert payload["count"] == 1
        assert payload["rows"][0]["quote_quality"] == "free_or_delayed"
        assert payload["rows"][0]["chain_source"] == "cboe"
        assert payload["rows"][0]["breakeven_price"] == 225.0
        assert payload["rows"][0]["budget_fit"] == "inside_budget"
        assert payload["rows"][0]["reward_risk_reference"] == 2.0
        assert payload["rows"][0]["open_exposure_count"] == 1
        assert payload["rows"][0]["open_exposure_assets"] == "option:1"
        assert payload["quality_summary"]["status"] == "clean"
        assert payload["quality_summary"]["primary_review_count"] == 1
        assert payload["provider_summary"]["source_counts"] == {"cboe": 1}
        assert result["quality_summary"]["status"] == "clean"
        summary = cockpit_module._build_chain_shortlist_summary(data_dir)
        assert summary["quality_summary"]["status"] == "clean"
        assert summary["source_counts"] == {"cboe": 1}
        assert summary["successful_scans"] == 2


def test_option_chain_leaps_preset_overrides_manual_filters_and_summarizes():
    original = cockpit_module._fetch_option_chain

    def fake_fetch(ticker: str, cache_age: int = 600):
        assert ticker == "AAPL"
        return {
            "spot": 200.0,
            "source": "cboe",
            "quote_quality": "free_or_delayed",
            "expirations": ["2027-01-15", "2026-06-18"],
            "chains": {
                "2027-01-15": pd.DataFrame([
                    {
                        "strike": 220.0,
                        "side": "call",
                        "bid": 4.90,
                        "ask": 5.10,
                        "lastPrice": 5.00,
                        "volume": 50,
                        "openInterest": 1000,
                    },
                    {
                        "strike": 180.0,
                        "side": "put",
                        "bid": 3.00,
                        "ask": 3.50,
                        "lastPrice": 3.20,
                        "volume": 15,
                        "openInterest": 120,
                    },
                ]),
                "2026-06-18": pd.DataFrame([
                    {
                        "strike": 205.0,
                        "side": "call",
                        "bid": 1.00,
                        "ask": 1.05,
                        "lastPrice": 1.02,
                        "volume": 500,
                        "openInterest": 5000,
                    },
                ]),
            },
        }

    try:
        cockpit_module._fetch_option_chain = fake_fetch
        report = build_option_chain_scan(
            "AAPL",
            side="put",
            min_dte=0,
            max_dte=1,
            max_spread_pct=0.01,
            max_premium=1,
            min_open_interest=0,
            preset="leaps",
        )
    finally:
        cockpit_module._fetch_option_chain = original

    assert report["ok"] is True
    assert report["preset"] == "leaps"
    assert report["preset_label"] == "Long dated"
    assert report["filters"]["min_dte"] == 180
    assert report["filters"]["max_dte"] == 900
    assert report["filters"]["max_premium"] == 750.0
    assert report["filtered_count"] == 2
    assert {row["side"] for row in report["rows"]} == {"call", "put"}
    assert report["scan_summary"]["long_dated_count"] == 2
    assert report["scan_summary"]["best_call"].startswith("C 220")
    assert report["scan_summary"]["best_put"].startswith("P 180")
    assert report["scan_summary"]["review_count"] >= 1
    assert report["expiry_summary"][0]["contracts"] == 2
    assert report["expiry_summary"][0]["calls"] == 1
    assert report["expiry_summary"][0]["puts"] == 1


def test_cboe_option_activity_filters_3m_plus_public_contracts():
    old_run = cockpit_module.cboe_symbol_data_engine.run
    old_resolve = cockpit_module.resolve_symbol
    try:
        cockpit_module.resolve_symbol = lambda query: {"symbol": "AAPL", "name": "Apple Inc."}
        cockpit_module.cboe_symbol_data_engine.run = lambda symbols, min_volume=1: pd.DataFrame([
            {
                "ticker": "AAPL",
                "expiry": "2026-10-16",
                "strike": 200.0,
                "option_side": "call",
                "cboe_activity_volume": 1200,
                "cboe_activity_matched": 1100,
                "cboe_activity_routed": 100,
                "cboe_activity_bid": 4.9,
                "cboe_activity_ask": 5.1,
                "cboe_activity_last": 5.0,
                "cboe_activity_contract": "AAPL Oct 16 200.0 Call",
                "cboe_activity_venues": "Cboe Options,BZX Options",
                "cboe_activity_source": "cboe_symbol_data",
            },
            {
                "ticker": "AAPL",
                "expiry": "2026-07-17",
                "strike": 190.0,
                "option_side": "put",
                "cboe_activity_volume": 2000,
                "cboe_activity_bid": 2.0,
                "cboe_activity_ask": 2.4,
                "cboe_activity_contract": "AAPL Jul 17 190.0 Put",
                "cboe_activity_venues": "Cboe Options",
                "cboe_activity_source": "cboe_symbol_data",
            },
        ])

        report = build_cboe_option_activity(query="Apple", min_dte=90, max_dte=400, min_volume=1)
    finally:
        cockpit_module.cboe_symbol_data_engine.run = old_run
        cockpit_module.resolve_symbol = old_resolve

    assert report["status"] == "ready"
    assert report["symbol"] == "AAPL"
    assert report["count"] == 1
    row = report["rows"][0]
    assert row["contract"] == "AAPL Oct 16 200.0 Call"
    assert row["side"] == "call"
    assert row["premium_dollars"] == 500.0
    assert row["cboe_activity_spread_pct"] == 0.04
    assert row["cboe_activity_readiness"] == "active"
    assert report["summary"]["total_volume"] == 1200
    assert report["summary"]["side_counts"] == {"call": 1}
    assert "not consolidated OPRA" in " ".join(report["notes"])


def test_cboe_option_activity_marks_zero_bid_ask_as_context_only():
    old_run = cockpit_module.cboe_symbol_data_engine.run
    old_resolve = cockpit_module.resolve_symbol
    try:
        cockpit_module.resolve_symbol = lambda query: {"symbol": "AAPL", "name": "Apple Inc."}
        cockpit_module.cboe_symbol_data_engine.run = lambda symbols, min_volume=1: pd.DataFrame([{
            "ticker": "AAPL",
            "expiry": "2026-10-16",
            "strike": 350.0,
            "option_side": "call",
            "cboe_activity_volume": 1500,
            "cboe_activity_matched": 1500,
            "cboe_activity_routed": 0,
            "cboe_activity_bid": 0.0,
            "cboe_activity_ask": 0.0,
            "cboe_activity_last": 2.1,
            "cboe_activity_contract": "AAPL Oct 16 350.0 Call",
            "cboe_activity_venues": "BZX Options",
            "cboe_activity_source": "cboe_symbol_data",
        }])

        report = build_cboe_option_activity(query="Apple", min_dte=90, max_dte=400, min_volume=1)
    finally:
        cockpit_module.cboe_symbol_data_engine.run = old_run
        cockpit_module.resolve_symbol = old_resolve

    assert report["status"] == "ready"
    assert report["count"] == 1
    row = report["rows"][0]
    assert row["premium_dollars"] is None
    assert row["last_premium_dollars"] == 210.0
    assert row["cboe_activity_readiness"] == "review"
    assert row["cboe_activity_score"] < 78
    assert "missing public bid/ask" in row["cboe_activity_flags"]


def test_provider_status_checks_free_sources_without_running_scan():
    old_yahoo = cockpit_module.data_provider._yahoo_v8_history
    old_nasdaq = cockpit_module.data_provider._nasdaq_history
    old_stooq = cockpit_module.data_provider._stooq_history
    old_get_history = cockpit_module.data_provider.get_history
    old_chain = cockpit_module._fetch_option_chain
    old_companyfacts = cockpit_module.companyfacts_for_symbol
    old_halts = cockpit_module.halt_rows_for_symbols
    old_thresholds = cockpit_module.threshold_rows_for_symbols
    old_circuits = cockpit_module.circuit_rows_for_symbols
    old_ftd = cockpit_module.sec_ftd_engine.run
    idx = pd.to_datetime(["2026-06-10", "2026-06-11"], utc=True)
    hist = pd.DataFrame({
        "Open": [10.0, 11.0],
        "High": [11.0, 13.0],
        "Low": [9.0, 10.0],
        "Close": [10.5, 12.5],
        "Volume": [1000, 1500],
    }, index=idx)

    try:
        cockpit_module.data_provider._yahoo_v8_history = lambda *args, **kwargs: cockpit_module.data_provider._tag_history(
            hist.copy(), "yahoo_chart", "free_or_delayed",
        )
        cockpit_module.data_provider._nasdaq_history = lambda *args, **kwargs: cockpit_module.data_provider._tag_history(
            hist.copy(), "nasdaq_historical", "free_or_delayed",
        )
        cockpit_module.data_provider._stooq_history = lambda *args, **kwargs: pd.DataFrame()
        cockpit_module.data_provider.get_history = lambda *args, **kwargs: cockpit_module.data_provider._tag_history(
            hist.copy(), "yahoo_chart", "free_or_delayed",
        )
        cockpit_module._fetch_option_chain = lambda *args, **kwargs: {
            "spot": 200.0,
            "source": "cboe",
            "quote_quality": "free_or_delayed",
            "data_delay": "delayed",
            "source_attempts": [
                {
                    "provider": "cboe",
                    "status": "ok",
                    "rows": 2,
                    "expirations": 1,
                    "quote_quality": "free_or_delayed",
                },
                {
                    "provider": "nasdaq_stocks",
                    "status": "warn",
                    "rows": 0,
                    "expirations": 0,
                },
            ],
            "chains": {
                "2027-01-15": pd.DataFrame([
                    {"strike": 200, "side": "call", "bid": 4.9, "ask": 5.1},
                    {"strike": 180, "side": "put", "bid": 3.0, "ask": 3.2},
                ])
            },
        }
        cockpit_module.companyfacts_for_symbol = lambda *args, **kwargs: {
            "symbol": "AAPL",
            "cik": "320193",
            "company_name": "Apple Inc.",
            "source": "sec_companyfacts",
            "count": 2,
            "rows": [{"metric": "revenue"}, {"metric": "net_income"}],
            "metrics": {"revenue": 100.0, "net_income": 20.0},
            "watch_signals": ["net margin positive"],
        }
        cockpit_module.halt_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame()
        cockpit_module.threshold_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame()
        cockpit_module.circuit_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame()
        cockpit_module.sec_ftd_engine.run = lambda *args, **kwargs: pd.DataFrame()
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            (data_dir / "sec_company_tickers.json").write_text(
                json.dumps({"rows": [{"symbol": "AAPL", "name": "Apple Inc."}]}),
                encoding="utf-8",
            )
            (data_dir / "nasdaq_symbol_directory.json").write_text(
                json.dumps({"rows": [{"symbol": "AAPL", "name": "Apple Inc.", "type": "EQUITY"}]}),
                encoding="utf-8",
            )
            report = build_provider_status(data_dir, query="Apple")
            no_chain = build_provider_status(data_dir, query="Apple", include_chain=False)
    finally:
        cockpit_module.data_provider._yahoo_v8_history = old_yahoo
        cockpit_module.data_provider._nasdaq_history = old_nasdaq
        cockpit_module.data_provider._stooq_history = old_stooq
        cockpit_module.data_provider.get_history = old_get_history
        cockpit_module._fetch_option_chain = old_chain
        cockpit_module.companyfacts_for_symbol = old_companyfacts
        cockpit_module.halt_rows_for_symbols = old_halts
        cockpit_module.threshold_rows_for_symbols = old_thresholds
        cockpit_module.circuit_rows_for_symbols = old_circuits
        cockpit_module.sec_ftd_engine.run = old_ftd

    assert report["symbol"] == "AAPL"
    assert report["provider_count"] == 12
    assert report["ok_count"] == 11
    assert report["data_trust"]["label"] == "ready"
    assert report["data_trust"]["score"] >= 80
    assert report["data_trust"]["history_ok_count"] == 2
    assert report["data_trust"]["history_source_summary"] == "yahoo_chart, nasdaq_historical"
    assert report["data_trust"]["history_stack_status"] == "ok"
    assert report["data_trust"]["history_stack_source"] == "yahoo_chart"
    assert report["data_trust"]["history_stack_rows"] == 2
    assert report["data_trust"]["option_chain_status"] == "ok"
    assert report["data_trust"]["option_chain_source"] == "cboe"
    assert report["data_trust"]["option_chain_rows"] == 2
    assert report["data_trust"]["option_chain_quote_quality"] == "free_or_delayed"
    assert report["data_trust"]["option_chain_data_delay"] == "delayed"
    assert report["data_trust"]["option_chain_providers_checked"] == 2
    assert report["data_trust"]["option_chain_usable_provider_count"] == 1
    assert report["data_trust"]["option_chain_failed_provider_count"] == 1
    assert report["data_trust"]["option_chain_provider_summary"] == "cboe:ok/2; nasdaq_stocks:warn/0"
    assert "delayed" in report["data_trust"]["option_chain_warning"]
    assert report["data_trust"]["sec_companyfacts_status"] == "ok"
    assert report["data_trust"]["sec_companyfacts_rows"] == 2
    assert report["data_trust"]["market_structure_status"] == "clear"
    assert report["data_trust"]["market_structure_flags"] == []
    assert report["data_trust"]["market_structure_risk_score"] == 0
    providers = {row["provider"]: row for row in report["rows"]}
    assert providers["Layered history stack"]["rows"] == 2
    assert providers["Layered history stack"]["history_source"] == "yahoo_chart"
    assert providers["Yahoo chart"]["rows"] == 2
    assert providers["Yahoo chart"]["history_source"] == "yahoo_chart"
    assert providers["Nasdaq historical"]["last_close"] == 12.5
    assert providers["Nasdaq historical"]["history_source"] == "nasdaq_historical"
    assert providers["Stooq CSV"]["status"] == "warn"
    assert providers["Option chain stack"]["rows"] == 2
    assert providers["Option chain stack"]["usable_provider_count"] == 1
    assert providers["Option chain stack"]["failed_provider_count"] == 1
    assert providers["Option chain stack"]["provider_attempt_summary"] == "cboe:ok/2; nasdaq_stocks:warn/0"
    assert providers["SEC company facts"]["status"] == "ok"
    assert providers["SEC company facts"]["metric_count"] == 2
    assert providers["SEC company facts"]["watch_signals"] == "net margin positive"
    assert providers["SEC company ticker cache"]["status"] == "ok"
    assert providers["Nasdaq symbol directory cache"]["status"] == "ok"
    assert providers["Nasdaq Trader trade halt RSS"]["status"] == "ok"
    assert providers["Nasdaq Trader Reg SHO threshold list"]["status"] == "ok"
    assert providers["Nasdaq Trader short-sale circuit breaker"]["status"] == "ok"
    assert providers["SEC fails-to-deliver"]["status"] == "ok"
    assert "No recent SEC fails-to-deliver" in providers["SEC fails-to-deliver"]["note"]
    assert no_chain["provider_count"] == 11
    assert no_chain["data_trust"]["option_chain_status"] == "skipped"
    assert all(row["provider"] != "Option chain stack" for row in no_chain["rows"])


def test_provider_status_surfaces_market_structure_risk_for_symbol():
    old_yahoo = cockpit_module.data_provider._yahoo_v8_history
    old_nasdaq = cockpit_module.data_provider._nasdaq_history
    old_stooq = cockpit_module.data_provider._stooq_history
    old_get_history = cockpit_module.data_provider.get_history
    old_companyfacts = cockpit_module.companyfacts_for_symbol
    old_halts = cockpit_module.halt_rows_for_symbols
    old_thresholds = cockpit_module.threshold_rows_for_symbols
    old_circuits = cockpit_module.circuit_rows_for_symbols
    old_ftd = cockpit_module.sec_ftd_engine.run
    idx = pd.to_datetime(["2026-06-10", "2026-06-11"], utc=True)
    hist = pd.DataFrame({
        "Open": [10.0, 11.0],
        "High": [11.0, 13.0],
        "Low": [9.0, 10.0],
        "Close": [10.5, 12.5],
        "Volume": [1000, 1500],
    }, index=idx)

    try:
        tagged = cockpit_module.data_provider._tag_history(hist.copy(), "yahoo_chart", "free_or_delayed")
        cockpit_module.data_provider._yahoo_v8_history = lambda *args, **kwargs: tagged.copy()
        cockpit_module.data_provider._nasdaq_history = lambda *args, **kwargs: tagged.copy()
        cockpit_module.data_provider._stooq_history = lambda *args, **kwargs: pd.DataFrame()
        cockpit_module.data_provider.get_history = lambda *args, **kwargs: tagged.copy()
        cockpit_module.companyfacts_for_symbol = lambda *args, **kwargs: {
            "symbol": "RISK",
            "cik": "1",
            "company_name": "Risk Corp",
            "count": 1,
            "rows": [{"metric": "revenue"}],
        }
        cockpit_module.halt_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame([{
            "symbol": "RISK",
            "active_halt": True,
            "halt_risk_score": 98,
        }])
        cockpit_module.threshold_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame([{
            "symbol": "RISK",
            "is_threshold": True,
            "settlement_risk_score": 86,
        }])
        cockpit_module.circuit_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame([{
            "symbol": "RISK",
            "short_sale_restricted": True,
            "ssr_risk_score": 82,
        }])
        cockpit_module.sec_ftd_engine.run = lambda *args, **kwargs: pd.DataFrame([{
            "ticker": "RISK",
            "sec_ftd_score": 1.8,
            "sec_ftd_latest_date": "2026-06-12",
            "sec_ftd_fails": 750000,
            "sec_ftd_dollars": 1800000.0,
            "sec_ftd_active_days": 3,
            "sec_ftd_source": "sec_fails_to_deliver",
        }])
        with tempfile.TemporaryDirectory() as td:
            report = build_provider_status(Path(td), query="RISK", include_chain=False)
    finally:
        cockpit_module.data_provider._yahoo_v8_history = old_yahoo
        cockpit_module.data_provider._nasdaq_history = old_nasdaq
        cockpit_module.data_provider._stooq_history = old_stooq
        cockpit_module.data_provider.get_history = old_get_history
        cockpit_module.companyfacts_for_symbol = old_companyfacts
        cockpit_module.halt_rows_for_symbols = old_halts
        cockpit_module.threshold_rows_for_symbols = old_thresholds
        cockpit_module.circuit_rows_for_symbols = old_circuits
        cockpit_module.sec_ftd_engine.run = old_ftd

    assert report["status"] == "warn"
    assert report["data_trust"]["label"] == "blocked"
    assert report["data_trust"]["score"] <= 35
    assert report["data_trust"]["market_structure_status"] == "risk_review"
    assert set(report["data_trust"]["market_structure_flags"]) == {
        "active_halt", "regsho_threshold", "short_sale_restricted", "sec_ftd_pressure",
    }
    assert report["data_trust"]["market_structure_risk_score"] == 98
    assert report["data_trust"]["market_structure_warning_count"] == 4
    assert any("halt" in warning.lower() for warning in report["warnings"])
    providers = {row["provider"]: row for row in report["rows"]}
    assert providers["Nasdaq Trader trade halt RSS"]["status"] == "warn"
    assert providers["Nasdaq Trader trade halt RSS"]["risk_flag_name"] == "active_halt"
    assert providers["Nasdaq Trader Reg SHO threshold list"]["risk_flag_name"] == "regsho_threshold"
    assert providers["Nasdaq Trader short-sale circuit breaker"]["risk_flag_name"] == "short_sale_restricted"
    assert providers["SEC fails-to-deliver"]["status"] == "warn"
    assert providers["SEC fails-to-deliver"]["risk_flag_name"] == "sec_ftd_pressure"
    assert "review settlement pressure" in providers["SEC fails-to-deliver"]["note"]


def test_provider_status_uses_layered_history_stack_when_raw_probes_fail():
    old_yahoo = cockpit_module.data_provider._yahoo_v8_history
    old_nasdaq = cockpit_module.data_provider._nasdaq_history
    old_stooq = cockpit_module.data_provider._stooq_history
    old_get_history = cockpit_module.data_provider.get_history
    old_chain = cockpit_module._fetch_option_chain
    old_companyfacts = cockpit_module.companyfacts_for_symbol
    old_halts = cockpit_module.halt_rows_for_symbols
    old_thresholds = cockpit_module.threshold_rows_for_symbols
    old_circuits = cockpit_module.circuit_rows_for_symbols
    old_ftd = cockpit_module.sec_ftd_engine.run
    idx = pd.to_datetime(["2026-06-10", "2026-06-11"], utc=True)
    hist = pd.DataFrame({
        "Open": [10.0, 11.0],
        "High": [11.0, 13.0],
        "Low": [9.0, 10.0],
        "Close": [10.5, 12.5],
        "Volume": [1000, 1500],
    }, index=idx)

    try:
        cockpit_module.data_provider._yahoo_v8_history = lambda *args, **kwargs: pd.DataFrame()
        cockpit_module.data_provider._nasdaq_history = lambda *args, **kwargs: pd.DataFrame()
        cockpit_module.data_provider._stooq_history = lambda *args, **kwargs: pd.DataFrame()
        cockpit_module.data_provider.get_history = lambda *args, **kwargs: cockpit_module.data_provider._tag_history(
            hist.copy(), "stooq_csv", "delayed",
        )
        cockpit_module._fetch_option_chain = lambda *args, **kwargs: {}
        cockpit_module.companyfacts_for_symbol = lambda *args, **kwargs: {}
        cockpit_module.halt_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame()
        cockpit_module.threshold_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame()
        cockpit_module.circuit_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame()
        cockpit_module.sec_ftd_engine.run = lambda *args, **kwargs: pd.DataFrame()
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            report = build_provider_status(data_dir, query="AAPL", include_chain=False)
    finally:
        cockpit_module.data_provider._yahoo_v8_history = old_yahoo
        cockpit_module.data_provider._nasdaq_history = old_nasdaq
        cockpit_module.data_provider._stooq_history = old_stooq
        cockpit_module.data_provider.get_history = old_get_history
        cockpit_module._fetch_option_chain = old_chain
        cockpit_module.companyfacts_for_symbol = old_companyfacts
        cockpit_module.halt_rows_for_symbols = old_halts
        cockpit_module.threshold_rows_for_symbols = old_thresholds
        cockpit_module.circuit_rows_for_symbols = old_circuits
        cockpit_module.sec_ftd_engine.run = old_ftd

    providers = {row["provider"]: row for row in report["rows"]}
    assert providers["Layered history stack"]["status"] == "ok"
    assert providers["Layered history stack"]["history_source"] == "stooq_csv"
    assert providers["Yahoo chart"]["status"] == "warn"
    assert providers["Nasdaq historical"]["status"] == "warn"
    assert providers["Stooq CSV"]["status"] == "warn"
    assert providers["SEC fails-to-deliver"]["status"] == "ok"
    assert report["data_trust"]["history_ok_count"] == 0
    assert report["data_trust"]["history_stack_status"] == "ok"
    assert report["data_trust"]["history_stack_source"] == "stooq_csv"
    assert report["data_trust"]["history_stack_quality"] == "delayed"
    assert report["data_trust"]["label"] == "ready"


def test_free_data_sources_registry_lists_no_key_coverage():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        (data_dir / "sec_company_tickers.json").write_text(
            json.dumps({"rows": [{"symbol": "AAPL", "name": "Apple Inc."}]}),
            encoding="utf-8",
        )
        (data_dir / "nasdaq_symbol_directory.json").write_text(
            json.dumps({"rows": [{"symbol": "QQQ", "name": "Invesco QQQ Trust", "type": "ETF"}]}),
            encoding="utf-8",
        )
        report = build_free_data_sources(data_dir)

    names = {row["name"] for row in report["rows"]}
    assert report["source_count"] >= 10
    assert report["no_key_count"] == report["source_count"]
    assert report["primary_count"] >= 5
    assert "Layered history stack" in names
    assert "CBOE option chains" in names
    assert "CBOE put/call market statistics" in names
    assert "FINRA RegSHO short volume" in names
    assert "Yahoo chart" in names
    assert "Google News RSS" in names
    assert "Yahoo Finance RSS" in names
    assert "SEC EDGAR" in names
    assert "SEC fails-to-deliver" in names
    assert "Nasdaq Trader symbol directory" in names
    assert "Nasdaq Trader trade halt RSS" in names
    assert "Nasdaq Trader Reg SHO threshold list" in names
    assert "Nasdaq Trader short-sale circuit breakers" in names
    assert "Treasury yield XML" in names
    assert "news" in report["category_counts"]
    assert "options" in report["category_counts"]
    assert "options_sentiment" in report["category_counts"]
    assert report["sec_cache"]["row_count"] >= 1
    assert report["nasdaq_symbol_cache"]["row_count"] >= 1
    assert report["ram_cache"]["ram_cache_enabled"] in {True, False}


def test_saved_option_contracts_extracts_watchlist_option_requests():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        assert add_watchlist_query("AAPL 20991218 C 220", data_dir)["ok"] is True
        assert add_watchlist_query("MSFT 20200117 P 300", data_dir)["ok"] is True
        assert add_watchlist_query("NVDA", data_dir)["ok"] is True

        contracts = build_saved_option_contracts(data_dir, enrich=False)
        assert contracts["count"] == 2
        assert contracts["call_count"] == 1
        assert contracts["put_count"] == 1
        by_symbol = {row["symbol"]: row for row in contracts["rows"]}
        assert by_symbol["AAPL"]["side_code"] == "C"
        assert by_symbol["AAPL"]["dte"] >= 90
        assert by_symbol["AAPL"]["status"] == "saved_review"
        assert by_symbol["AAPL"]["review_action"] == "refresh_quote"
        assert by_symbol["AAPL"]["triage_bucket"] == "refresh_quote"
        assert by_symbol["AAPL"]["triage_label"] == "Refresh Quote"
        assert by_symbol["AAPL"]["triage_score"] > 0
        assert by_symbol["AAPL"]["review_score"] < 100
        assert "refresh quote first" in by_symbol["AAPL"]["review_reasons"]
        assert by_symbol["MSFT"]["status"] == "expired"
        assert by_symbol["MSFT"]["review_action"] == "refresh_quote"
        assert contracts["status_counts"]["expired"] == 1
        assert contracts["review_action_counts"]["refresh_quote"] == 2
        assert contracts["triage_counts"]["refresh_quote"] == 1
        assert contracts["triage_counts"]["expired"] == 1
        assert contracts["swing_count"] == 1


def test_watchlist_sec_filings_ranks_recent_official_filings():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        today = datetime.now(timezone.utc).date()
        watchlist = [
            {"id": "aapl", "symbol": "AAPL", "query": "Apple"},
            {"id": "nvda", "symbol": "NVDA", "query": "Nvidia"},
            {"id": "aapl_dupe", "symbol": "AAPL", "query": "Apple duplicate"},
        ]
        (data_dir / "cockpit_watchlist.json").write_text(
            json.dumps(watchlist),
            encoding="utf-8",
        )
        old_recent = cockpit_module.recent_filings_for_symbol

        def fake_recent(symbol, limit=8):
            if symbol == "AAPL":
                return {
                    "symbol": "AAPL",
                    "company_name": "Apple Inc.",
                    "rows": [
                        {
                            "ticker": "AAPL",
                            "company_name": "Apple Inc.",
                            "form": "S-3",
                            "filing_date": today.isoformat(),
                            "description": "Shelf registration statement",
                            "filing_signal": "dilution_or_offering_watch",
                            "url": "https://www.sec.gov/aapl",
                        }
                    ],
                }
            return {
                "symbol": "NVDA",
                "company_name": "NVIDIA Corp.",
                "rows": [
                    {
                        "ticker": "NVDA",
                        "company_name": "NVIDIA Corp.",
                        "form": "8-K",
                        "filing_date": (today - timedelta(days=4)).isoformat(),
                        "description": "Material event",
                        "filing_signal": "material_event_review",
                        "url": "https://www.sec.gov/nvda",
                    }
                ],
            }

        cockpit_module.recent_filings_for_symbol = fake_recent
        try:
            report = build_watchlist_sec_filings(data_dir, limit=10)
            cache_payload = json.loads(
                (data_dir / "watchlist_sec_filings.json").read_text(encoding="utf-8")
            )
        finally:
            cockpit_module.recent_filings_for_symbol = old_recent

    assert report["symbols_checked"] == 2
    assert report["filing_count"] == 2
    assert report["fresh_count"] == 2
    assert report["high_impact_count"] == 2
    assert report["rows"][0]["ticker"] == "AAPL"
    assert report["rows"][0]["form"] == "S-3"
    assert report["form_counts"]["S-3"] == 1
    assert report["signal_counts"]["material_event_review"] == 1
    assert cache_payload["filing_count"] == 2
    assert cache_payload["rows"][0]["ticker"] == "AAPL"


def test_saved_option_contracts_preserve_chain_scan_context():
    context = {
        "chain_source": "cboe",
        "quote_quality": "free_or_delayed",
        "data_delay": "delayed",
        "contract_grade": "A",
        "review_lane": "primary_review",
        "review_thesis": "A-grade 300 DTE call with tight spread.",
        "grade_reasons": ["tight spread", "liquid", "inside premium budget"],
        "readiness_label": "ready",
        "readiness_score": 92,
        "contract_quality_score": 88.5,
        "mid": 5.0,
        "bid": 4.9,
        "ask": 5.1,
        "spread_pct": 0.04,
        "premium_dollars": 500.0,
        "volume": 50,
        "openInterest": 1000,
    }
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        added = add_watchlist_query("AAPL 20991218 C 220", data_dir, context=context)
        assert added["ok"] is True
        loaded = load_watchlist(data_dir)["entries"][0]
        contracts = build_saved_option_contracts(data_dir, enrich=False)

    row = contracts["rows"][0]
    assert loaded["chain_context"]["contract_grade"] == "A"
    assert loaded["chain_context"]["saved_from"] == "option_chain_scan"
    assert row["saved_contract_grade"] == "A"
    assert row["saved_review_lane"] == "primary_review"
    assert row["saved_review_thesis"].startswith("A-grade")
    assert row["saved_mid"] == 5.0
    assert row["saved_spread_pct"] == 0.04
    assert row["saved_quote_quality"] == "free_or_delayed"
    assert contracts["saved_grade_counts"]["A"] == 1
    assert "saved grade A" in row["review_reasons"]
    assert row["triage_bucket"] == "refresh_quote"
    assert "A-grade chain save" in row["triage_reasons"]


def test_watchlist_bulk_add_preserves_each_chain_context():
    items = [
        {
            "query": "AAPL 20991218 C 220",
            "context": {
                "contract_grade": "A",
                "review_lane": "primary_review",
                "review_thesis": "A-grade AAPL contract.",
                "mid": 5.0,
                "spread_pct": 0.04,
            },
        },
        {
            "query": "MSFT 20991218 C 450",
            "context": {
                "contract_grade": "B",
                "review_lane": "secondary_review",
                "review_thesis": "B-grade MSFT contract.",
                "mid": 4.0,
                "spread_pct": 0.05,
            },
        },
    ]
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        saved = add_watchlist_queries(items, data_dir)
        contracts = build_saved_option_contracts(data_dir, enrich=False)

    by_symbol = {row["symbol"]: row for row in contracts["rows"]}
    assert saved["saved_count"] == 2
    assert saved["error_count"] == 0
    assert saved["count"] == 2
    assert by_symbol["AAPL"]["saved_contract_grade"] == "A"
    assert by_symbol["MSFT"]["saved_contract_grade"] == "B"
    assert contracts["saved_grade_counts"]["A"] == 1
    assert contracts["saved_grade_counts"]["B"] == 1
    assert contracts["triage_counts"]["refresh_quote"] == 2


def test_saved_option_contracts_can_refresh_exact_chain_quotes():
    original = cockpit_module._fetch_option_chain

    def fake_fetch(ticker: str, cache_age: int = 300):
        assert ticker == "AAPL"
        assert cache_age == 300
        return {
            "spot": 200.0,
            "source": "cboe",
            "quote_quality": "free_or_delayed",
            "chains": {
                "2099-12-18": pd.DataFrame([
                    {
                        "strike": 220.0,
                        "side": "call",
                        "bid": 4.90,
                        "ask": 5.10,
                        "lastPrice": 5.0,
                        "volume": 50,
                        "openInterest": 1000,
                        "impliedVolatility": 0.30,
                        "delta": 0.42,
                    }
                ])
            },
        }

    try:
        cockpit_module._fetch_option_chain = fake_fetch
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            assert add_watchlist_query("AAPL 20991218 C 220", data_dir)["ok"] is True
            contracts = build_saved_option_contracts(
                data_dir,
                enrich=False,
                refresh_quotes=True,
                quote_limit=5,
            )
    finally:
        cockpit_module._fetch_option_chain = original

    row = contracts["rows"][0]
    assert contracts["quote_checked_count"] == 1
    assert contracts["quote_status_counts"]["matched"] == 1
    assert row["quote_status"] == "matched"
    assert row["review_action"] == "review_now"
    assert row["triage_bucket"] == "ready_now"
    assert row["triage_label"] == "Ready Review"
    assert row["triage_score"] >= row["review_score"]
    assert row["review_score"] >= 80
    assert "quote matched" in row["review_reasons"]
    assert row["current_mid"] == 5.0
    assert row["current_premium_dollars"] == 500.0
    assert row["current_spread_pct"] < 0.10
    assert row["quote_readiness_label"] in {"ready", "review"}
    assert row["current_open_interest"] == 1000


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
            "chain_source": "tradier",
            "quote_quality": "live_or_broker",
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
        assert nvda["paper_readiness_status"] == "caution"
        assert nvda["paper_readiness_score"] < 75
        assert nvda["paper_readiness_bad_count"] == 0
        assert nvda["paper_readiness_warn_count"] >= 1
        assert nvda["swing_verdict_decision"] == "manage_existing"
        assert nvda["swing_verdict_score"] is not None
        assert nvda["swing_verdict_label"] == "Manage existing exposure"
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
    test_data_health_reports_fresh_sec_ticker_cache()
    test_data_health_audits_latest_opportunity_duplicates()
    test_warm_sec_ticker_cache_uses_data_dir_cache()
    test_action_queue_prioritizes_health_and_exit_risk_over_paper_candidates()
    test_action_queue_groups_stale_snapshots_into_refresh_action()
    test_action_queue_surfaces_cached_sec_filing_risk()
    test_action_queue_prompts_sec_monitor_refresh_when_cache_missing()
    test_action_queue_surfaces_trade_halt_risk_for_watchlist_symbol()
    test_action_queue_surfaces_regsho_threshold_risk_for_watchlist_symbol()
    test_action_queue_surfaces_short_sale_circuit_risk_for_watchlist_symbol()
    test_action_queue_surfaces_ready_watchlist_ideas()
    test_action_queue_promotes_reviewable_swing_scout_rows()
    test_action_queue_uses_best_setup_decision_row_not_raw_top()
    test_action_queue_marks_avoid_only_best_setup_as_held()
    test_action_queue_validation_guard_reroutes_fresh_entry_actions()
    test_today_review_combines_setups_saved_contracts_and_risk()
    test_today_review_validation_guard_reroutes_fresh_entry_actions()
    test_command_center_summarizes_next_action_and_data_trust()
    test_command_center_session_gate_defers_new_entries_after_review_window()
    test_command_center_validation_guard_defers_new_entries_during_active_window()
    test_manual_review_summary_surfaces_entry_gate_state()
    test_validation_guardrail_uses_summary_closed_count_with_overall_metrics()
    test_swing_packet_builds_and_writes_daily_decision_packet()
    test_swing_packet_can_refresh_chain_shortlist_on_demand()
    test_enriched_watchlist_sorts_ready_ideas_first()
    test_symbol_suggestions_include_local_contracts_positions_and_aliases()
    test_opportunity_explorer_reads_and_filters_latest_snapshots()
    test_best_setups_builds_decision_shortlist_from_latest_snapshots()
    test_best_setups_marks_clean_long_dated_option_ready()
    test_best_setups_gate_marks_short_dated_option_avoid_when_reviewed()
    test_best_setups_decision_row_prefers_reviewable_over_higher_scored_avoid()
    test_best_setups_include_saved_chain_shortlist_contracts()
    test_swing_scout_surfaces_small_caps_and_futures_but_filters_short_dte_options()
    test_swing_scout_can_include_nasdaq_small_cap_movers()
    test_swing_scout_market_structure_risk_downgrades_nasdaq_movers()
    test_climate_gated_setups_pass_clean_rows_and_hold_weak_contracts()
    test_position_monitor_reads_dedupes_and_filters_open_state()
    test_exit_review_summary_reads_jsonl_and_filters_actions()
    test_risk_summary_surfaces_concentration_and_exit_pressure()
    test_market_pulse_uses_free_history_context_and_regime_labels()
    test_options_sentiment_uses_cboe_put_call_snapshots()
    test_cboe_daily_put_call_parser_handles_escaped_nextjs_payload()
    test_options_sentiment_marks_stale_when_daily_fallback_missing()
    test_macro_stress_pulse_uses_keyless_fred_series()
    test_breadth_pulse_uses_free_etf_pair_confirmation()
    test_swing_climate_combines_free_context_into_posture()
    test_sector_pulse_ranks_free_sector_etf_context()
    test_performance_summary_reads_engine_perf_health_cache_and_finbert_state()
    test_paper_candidate_panel_builds_and_writes_filtered_exports()
    test_robinhood_agentic_queue_panel_builds_and_writes_long_dated_candidates()
    test_agentic_decision_journal_records_local_review_rows()
    test_agentic_autopilot_status_summarizes_gate_tickets_and_paper_book()
    test_agentic_autopilot_status_blocks_stale_packets_and_tickets()
    test_agentic_autopilot_preflight_passes_clean_confirmation_ticket()
    test_agentic_autopilot_warns_when_live_ticket_lacks_mcp_review_plan()
    test_cockpit_can_normalize_raw_robinhood_snapshot_for_reconciliation()
    test_broker_reconciliation_surfaces_broker_and_local_mismatches()
    test_agentic_autopilot_blocks_ticket_when_broker_reconciliation_mismatches()
    test_position_hygiene_builds_safe_cleanup_plan_without_mutating_positions()
    test_position_hygiene_apply_preview_does_not_mutate_positions()
    test_position_hygiene_apply_backs_up_and_moves_only_expired_options()
    test_agentic_autopilot_paper_book_marks_targets_and_missing_quotes()
    test_agentic_autopilot_paper_book_does_not_fake_zero_pnl_without_quotes()
    test_option_chain_scan_fetches_and_filters_contracts()
    test_option_chain_batch_scans_shortlist_and_ranks_contracts()
    test_option_chain_batch_uses_swing_scout_candidates_when_blank()
    test_option_chain_shortlist_writer_creates_portable_artifacts()
    test_option_chain_leaps_preset_overrides_manual_filters_and_summarizes()
    test_cboe_option_activity_filters_3m_plus_public_contracts()
    test_cboe_option_activity_marks_zero_bid_ask_as_context_only()
    test_provider_status_checks_free_sources_without_running_scan()
    test_provider_status_surfaces_market_structure_risk_for_symbol()
    test_provider_status_uses_layered_history_stack_when_raw_probes_fail()
    test_free_data_sources_registry_lists_no_key_coverage()
    test_saved_option_contracts_extracts_watchlist_option_requests()
    test_watchlist_sec_filings_ranks_recent_official_filings()
    test_saved_option_contracts_preserve_chain_scan_context()
    test_watchlist_bulk_add_preserves_each_chain_context()
    test_saved_option_contracts_can_refresh_exact_chain_quotes()
    test_research_watchlist_adds_dedupes_removes_and_builds_jobs()
    print("84/84 local cockpit tests passed")
