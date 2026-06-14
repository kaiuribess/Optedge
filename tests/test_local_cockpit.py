import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.local_cockpit as cockpit_module
from scripts.local_cockpit import (
    add_watchlist_queries, add_watchlist_query, artifact_path, build_opportunities, build_paper_candidates,
    build_action_queue, build_data_health, build_option_chain_scan, build_performance_summary,
    build_option_chain_batch,
    build_best_setups, build_breadth_pulse, build_climate_gated_setups, build_market_pulse,
    build_free_data_sources, build_positions, build_provider_status, build_risk_summary, build_robinhood_agentic_queue_report,
    build_saved_option_contracts, build_sector_pulse, build_summary, build_swing_climate, build_symbol_suggestions,
    build_today_review,
    load_watchlist, remove_watchlist_entry, render_cockpit_html, run_watchlist_scans,
    warm_sec_ticker_cache,
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
    assert "Opportunity quality" in html
    assert "opportunityQualityTable" in html
    assert "Action queue" in html
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
    assert "Climate-gated setups" in html
    assert "/api/climate-gated-setups" in html
    assert "climateGatedSetupsHtml" in html
    assert "loadClimateGatedSetups" in html
    assert "Scan 3m+ chain" in html
    assert "setup-chain-btn" in html
    assert "canScanOptionChainSymbol" in html
    assert "Save contract" in html
    assert "contract-watchlist-btn" in html
    assert "optionContractQuery" in html
    assert "wireOptionChainActions" in html
    assert "Market pulse" in html
    assert "/api/market-pulse" in html
    assert "marketPulseHtml" in html
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
    assert "/api/best-setups" in html
    assert "bestSetupsHtml" in html
    assert "loadBestSetups" in html
    assert "readiness_label" in html
    assert "risk_flags" in html
    assert "Opportunity explorer" in html
    assert "/api/opportunities" in html
    assert "External paper candidates" in html
    assert "/api/paper-candidates" in html
    assert "/api/export-paper" in html
    assert "Write export files" in html
    assert "Agentic options queue" in html
    assert "/api/robinhood-queue" in html
    assert "/api/build-robinhood-queue" in html
    assert "loadRobinhoodQueue" in html
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
    assert "Save best A/B contracts" in html
    assert "wireChainBatchActions" in html
    assert "Expiration quality" in html
    assert "Grade / lane" in html
    assert "Primary review" in html
    assert "Best budget" in html
    assert "Provider status" in html
    assert "/api/provider-status" in html
    assert "loadProviderStatus" in html
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
    assert "/api/positions" in html
    assert "briefHtml" in html
    assert "Research brief" in html
    assert "Research action" in html
    assert "Requested match" in html
    assert "Paper readiness" in html
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
    assert "/job-lookup" in html
    assert "/api/warm-sec-cache" in html
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
        assert labels["SEC ticker cache missing"]["level"] == "warn"
        assert health["free_data_caches"]["sec_company_tickers"]["status"] == "missing"


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

        health = build_data_health(data_dir)
        labels = {row["label"]: row for row in health["checks"]}
        assert labels["SEC ticker cache"]["level"] == "ok"
        assert health["free_data_caches"]["sec_company_tickers"]["status"] == "fresh"
        assert health["free_data_caches"]["sec_company_tickers"]["row_count"] == 2


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

        def fake_loader(cache_path, timeout=8.0, fetch_if_stale=True, **kwargs):
            Path(cache_path).write_text(json.dumps({
                "rows": [
                    {"symbol": "SNOW", "name": "Snowflake Inc.", "cik": 1640147},
                ],
            }), encoding="utf-8")
            return [{"symbol": "SNOW", "name": "Snowflake Inc.", "cik": 1640147}]

        cockpit_module.load_sec_company_tickers = fake_loader
        try:
            result = warm_sec_ticker_cache(data_dir)
        finally:
            cockpit_module.load_sec_company_tickers = old_loader

        assert result["ok"] is True
        assert result["row_count"] == 1
        assert result["cache"]["status"] == "fresh"
        assert (data_dir / "sec_company_tickers.json").exists()


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
            "contract": "NVDA 2026-09-18 C 200",
            "side": "call",
            "strike": 200,
            "expiry": "2026-09-18",
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
            and row["action"] == "warm_sec_ticker_cache"
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

        add_watchlist_query("AAPL 20260618 C 200", data_dir)
        queue = build_action_queue(data_dir)
        ready = [
            row for row in queue["rows"]
            if row["category"] == "watchlist" and row["label"] == "Review ready watchlist idea"
        ]
        assert ready
        assert ready[0]["symbol"] == "AAPL"
        assert ready[0]["action"] == "preview_paper_candidate"


def test_today_review_combines_setups_saved_contracts_and_risk():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        old_gated = cockpit_module.build_climate_gated_setups
        old_saved = cockpit_module.build_saved_option_contracts
        old_risk = cockpit_module.build_risk_summary
        old_queue = cockpit_module.build_action_queue

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

        cockpit_module.build_climate_gated_setups = fake_gated
        cockpit_module.build_saved_option_contracts = fake_saved
        cockpit_module.build_risk_summary = fake_risk
        cockpit_module.build_action_queue = fake_queue
        try:
            review = build_today_review(data_dir, limit=8)
        finally:
            cockpit_module.build_climate_gated_setups = old_gated
            cockpit_module.build_saved_option_contracts = old_saved
            cockpit_module.build_risk_summary = old_risk
            cockpit_module.build_action_queue = old_queue

        categories = {row["category"] for row in review["rows"]}
        actions = {row["action"] for row in review["rows"]}
        assert review["climate_label"] == "constructive_selective"
        assert review["setup_count"] == 1
        assert review["saved_contract_count"] == 1
        assert review["risk_count"] == 2
        assert "setup" in categories
        assert "saved_contract" in categories
        assert "position_risk" in categories
        assert "scan_swing_chain" in actions
        assert "refresh_saved_quote" in actions
        assert "open_position_monitor" in actions
        assert any(row["route"] == "chains" for row in review["rows"])


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
        cockpit_module.sec_company_search = lambda query, limit=16, fetch_if_stale=True: []
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

        try:
            nvda = build_symbol_suggestions(data_dir, query="nvda")
            assert any(row["query"] == "NVDA 2026-06-18 C 200" for row in nvda["rows"])

            oil = build_symbol_suggestions(data_dir, query="oil")
            assert any(row["symbol"] == "CL=F" for row in oil["rows"])

            apple = build_symbol_suggestions(data_dir, query="apple")
            assert any(row["symbol"] == "AAPL" and row["kind"] == "alias" for row in apple["rows"])

            gas = build_symbol_suggestions(data_dir, query="NG")
            assert any(row["symbol"] == "NG=F" and row["kind"] == "open_futures" for row in gas["rows"])

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
            assert "free SEC ticker map" in " ".join(snow["notes"])
            assert observed_fetch_modes == [False]
        finally:
            cockpit_module.sec_company_search = old_sec


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

    try:
        cockpit_module.data_provider.get_history = fake_history
        pulse = build_market_pulse(period="6mo")
    finally:
        cockpit_module.data_provider.get_history = old_history

    assert pulse["coverage"] == "9/9"
    assert pulse["regime"] in {"risk_on", "constructive"}
    assert pulse["risk_score"] > 0
    rows = {row["symbol"]: row for row in pulse["rows"]}
    assert rows["SPY"]["trend"] == "uptrend"
    assert rows["^VIX"]["trend"] in {"downtrend", "weak"}
    assert pulse["leaders"][0]["symbol"] in {"SPY", "QQQ", "IWM", "DIA"}


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

    try:
        cockpit_module.data_provider.get_history = fake_history
        climate = build_swing_climate(period="6mo")
    finally:
        cockpit_module.data_provider.get_history = old_history

    assert climate["climate_score"] >= 60
    assert climate["climate_label"] in {"aggressive_swing", "constructive_selective"}
    assert climate["market_regime"] in {"risk_on", "constructive"}
    assert climate["breadth_regime"] in {"broad_risk_on", "selective_risk_on"}
    assert climate["coverage"] == {"market": "9/9", "breadth": "7/7", "sector": "13/13"}
    assert climate["top_sector_symbol"] in {"SMH", "XLK"}
    assert climate["focus"]
    assert climate["playbook"]["option_min_dte"] >= 90
    assert climate["playbook"]["max_new_candidates"] >= 3
    assert any(row["gate"] == "Options DTE floor" for row in climate["trade_gates"])
    assert any(row["asset"] == "options" for row in climate["asset_bias"])
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
                "contract": "AAPL 2026-09-18 C 200",
                "side": "call",
                "strike": 200,
                "expiry": "2026-09-18",
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
                "contract": "MSFT 2026-09-18 C 500",
                "side": "call",
                "strike": 500,
                "expiry": "2026-09-18",
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

        filtered = build_paper_candidates(data_dir, max_new=5, query="AAPL 20260918 C 200")
        assert filtered["query"] == "AAPL 20260918 C 200"
        assert filtered["selected_count"] == 1
        assert filtered["rows"][0]["ticker_or_symbol"] == "AAPL"

        dry = build_paper_candidates(data_dir, dry_run=True)
        assert dry["excluded_count"] == 1
        assert any("suggested_contracts <= 0" in row["reason_excluded"] for row in dry["rows"])

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

        written = build_robinhood_agentic_queue_report(data_dir, write=True)
        assert written["wrote_files"] is True
        assert (data_dir / "robinhood_agentic_queue.json").exists()
        assert (data_dir / "robinhood_agentic_prompt.md").exists()


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
        report = build_option_chain_scan(
            "AAPL",
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
    row = report["rows"][0]
    assert row["side"] == "call"
    assert row["strike"] == 220.0
    assert row["chain_source"] == "cboe"
    assert row["quote_quality"] == "free_or_delayed"
    assert row["data_delay"] == "delayed"
    assert row["premium_dollars"] == 500.0
    assert row["contract_query"] == "AAPL 2027-01-15 C 220"
    assert row["spread_pct"] < 0.10
    assert row["dte_bucket"] in {"180-364d", "365d+"}
    assert row["readiness_label"] in {"ready", "review"}
    assert row["readiness_score"] >= 65
    assert row["contract_grade"] == "A"
    assert row["review_lane"] == "primary_review"
    assert "inside premium budget" in row["grade_reasons"]
    assert "A-grade" in row["review_thesis"]
    assert report["preset"] == "custom"
    assert report["scan_summary"]["best_call"].startswith("C 220")
    assert report["scan_summary"]["under_budget_count"] == 1
    assert report["scan_summary"]["review_count"] >= 1
    assert report["scan_summary"]["best_reviewable"].startswith("C 220")
    assert report["scan_summary"]["best_budget"].startswith("C 220")
    assert report["scan_summary"]["best_liquid"].startswith("C 220")
    assert report["scan_summary"]["best_long_dated"].startswith("C 220")
    assert report["scan_summary"]["grade_counts"]["A"] == 1
    assert report["scan_summary"]["primary_review_count"] == 1
    assert report["expiry_summary"][0]["expiry"] == "2027-01-15"
    assert report["expiry_summary"][0]["reviewable_count"] == 1


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
            "total_contracts": 20,
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
    assert report["symbol_summaries"][0]["quote_quality"] == "free_or_delayed"


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


def test_provider_status_checks_free_sources_without_running_scan():
    old_yahoo = cockpit_module.data_provider._yahoo_v8_history
    old_nasdaq = cockpit_module.data_provider._nasdaq_history
    old_stooq = cockpit_module.data_provider._stooq_history
    old_chain = cockpit_module._fetch_option_chain
    idx = pd.to_datetime(["2026-06-10", "2026-06-11"], utc=True)
    hist = pd.DataFrame({
        "Open": [10.0, 11.0],
        "High": [11.0, 13.0],
        "Low": [9.0, 10.0],
        "Close": [10.5, 12.5],
        "Volume": [1000, 1500],
    }, index=idx)

    try:
        cockpit_module.data_provider._yahoo_v8_history = lambda *args, **kwargs: hist
        cockpit_module.data_provider._nasdaq_history = lambda *args, **kwargs: hist
        cockpit_module.data_provider._stooq_history = lambda *args, **kwargs: pd.DataFrame()
        cockpit_module._fetch_option_chain = lambda *args, **kwargs: {
            "spot": 200.0,
            "source": "cboe",
            "chains": {
                "2027-01-15": pd.DataFrame([
                    {"strike": 200, "side": "call", "bid": 4.9, "ask": 5.1},
                    {"strike": 180, "side": "put", "bid": 3.0, "ask": 3.2},
                ])
            },
        }
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            (data_dir / "sec_company_tickers.json").write_text(
                json.dumps({"rows": [{"symbol": "AAPL", "name": "Apple Inc."}]}),
                encoding="utf-8",
            )
            report = build_provider_status(data_dir, query="Apple")
            no_chain = build_provider_status(data_dir, query="Apple", include_chain=False)
    finally:
        cockpit_module.data_provider._yahoo_v8_history = old_yahoo
        cockpit_module.data_provider._nasdaq_history = old_nasdaq
        cockpit_module.data_provider._stooq_history = old_stooq
        cockpit_module._fetch_option_chain = old_chain

    assert report["symbol"] == "AAPL"
    assert report["provider_count"] == 5
    assert report["ok_count"] == 4
    providers = {row["provider"]: row for row in report["rows"]}
    assert providers["Yahoo chart"]["rows"] == 2
    assert providers["Nasdaq historical"]["last_close"] == 12.5
    assert providers["Stooq CSV"]["status"] == "warn"
    assert providers["Option chain stack"]["rows"] == 2
    assert providers["SEC company ticker cache"]["status"] == "ok"
    assert no_chain["provider_count"] == 4
    assert all(row["provider"] != "Option chain stack" for row in no_chain["rows"])


def test_free_data_sources_registry_lists_no_key_coverage():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        (data_dir / "sec_company_tickers.json").write_text(
            json.dumps({"rows": [{"symbol": "AAPL", "name": "Apple Inc."}]}),
            encoding="utf-8",
        )
        report = build_free_data_sources(data_dir)

    names = {row["name"] for row in report["rows"]}
    assert report["source_count"] >= 10
    assert report["no_key_count"] == report["source_count"]
    assert report["primary_count"] >= 5
    assert "CBOE option chains" in names
    assert "Yahoo chart" in names
    assert "Google News RSS" in names
    assert "Yahoo Finance RSS" in names
    assert "SEC EDGAR" in names
    assert "news" in report["category_counts"]
    assert "options" in report["category_counts"]
    assert report["sec_cache"]["row_count"] >= 1
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
        assert nvda["paper_readiness_status"] == "ready"
        assert nvda["paper_readiness_score"] >= 75
        assert nvda["paper_readiness_bad_count"] == 0
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
    test_action_queue_surfaces_ready_watchlist_ideas()
    test_today_review_combines_setups_saved_contracts_and_risk()
    test_enriched_watchlist_sorts_ready_ideas_first()
    test_symbol_suggestions_include_local_contracts_positions_and_aliases()
    test_opportunity_explorer_reads_and_filters_latest_snapshots()
    test_best_setups_builds_decision_shortlist_from_latest_snapshots()
    test_best_setups_marks_clean_long_dated_option_ready()
    test_climate_gated_setups_pass_clean_rows_and_hold_weak_contracts()
    test_position_monitor_reads_dedupes_and_filters_open_state()
    test_risk_summary_surfaces_concentration_and_exit_pressure()
    test_market_pulse_uses_free_history_context_and_regime_labels()
    test_breadth_pulse_uses_free_etf_pair_confirmation()
    test_swing_climate_combines_free_context_into_posture()
    test_sector_pulse_ranks_free_sector_etf_context()
    test_performance_summary_reads_engine_perf_health_cache_and_finbert_state()
    test_paper_candidate_panel_builds_and_writes_filtered_exports()
    test_robinhood_agentic_queue_panel_builds_and_writes_long_dated_candidates()
    test_option_chain_scan_fetches_and_filters_contracts()
    test_option_chain_batch_scans_shortlist_and_ranks_contracts()
    test_option_chain_leaps_preset_overrides_manual_filters_and_summarizes()
    test_provider_status_checks_free_sources_without_running_scan()
    test_free_data_sources_registry_lists_no_key_coverage()
    test_saved_option_contracts_extracts_watchlist_option_requests()
    test_watchlist_bulk_add_preserves_each_chain_context()
    test_saved_option_contracts_can_refresh_exact_chain_quotes()
    test_research_watchlist_adds_dedupes_removes_and_builds_jobs()
    print("34/34 local cockpit tests passed")
