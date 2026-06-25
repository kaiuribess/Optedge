import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.lookup_symbol as lookup_module
from scripts.lookup_symbol import lookup_symbol, match_option_request, render_html, save_lookup
from scripts.symbol_resolver import resolve_symbol

lookup_module.recent_filings_for_symbol = lambda symbol, limit=8: {
    "symbol": symbol,
    "source": "sec_edgar_submissions",
    "count": 0,
    "rows": [],
}
lookup_module.companyfacts_for_symbol = lambda symbol, limit=12: {
    "symbol": symbol,
    "source": "sec_companyfacts",
    "count": 0,
    "rows": [],
    "metrics": {},
    "watch_signals": [],
}


def test_resolver_prefers_local_aliases_for_common_company_names_and_futures():
    apple = resolve_symbol("Apple")
    assert apple["symbol"] == "AAPL"
    assert apple["source"] == "alias"

    tesla = resolve_symbol("Tesla")
    assert tesla["symbol"] == "TSLA"
    assert tesla["source"] == "alias"

    spx = resolve_symbol("S&P 500 futures")
    assert spx["symbol"] == "ES=F"
    assert spx["source"] == "alias"


def test_lookup_reads_open_option_positions():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        (data_dir / "open_positions.json").write_text(json.dumps([{
            "ticker": "NVDA",
            "side": "call",
            "strike": 200,
            "expiry": "2026-06-18",
            "entry_price": 2.0,
        }]), encoding="utf-8")
        report = lookup_symbol("nvda", data_dir)
        assert report["total_hits"] == 1
        assert report["sections"]["open_options"][0]["ticker"] == "NVDA"


def test_lookup_reads_open_futures_positions():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        (data_dir / "open_futures_positions.json").write_text(json.dumps([{
            "symbol": "CL=F",
            "direction": "long",
            "entry_price": 70,
        }]), encoding="utf-8")
        report = lookup_symbol("CL=F", data_dir)
        assert report["total_hits"] == 1
        assert report["sections"]["open_futures"][0]["symbol"] == "CL=F"


def test_lookup_saves_json_and_html():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        report = lookup_symbol("MISS", data_dir)
        paths = save_lookup(report, data_dir)
        assert paths["json"].exists()
        assert paths["html"].exists()
        assert "Optedge Lookup" in render_html(report)
        assert report["brief"]["research_action"]["action"] == "run_focused_scan"
        assert report["brief"]["research_action"]["can_export_paper_candidate"] is False


def test_lookup_matches_requested_option_contract():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        pd.DataFrame([
            {
                "ticker": "AAPL",
                "side": "call",
                "strike": 195.0,
                "expiry": "2026-06-18",
                "mid": 5.1,
                "confidence": 50,
                "rank_score": 1.0,
            },
            {
                "ticker": "AAPL",
                "side": "call",
                "strike": 200.0,
                "expiry": "2026-06-18",
                "mid": 3.2,
                "confidence": 80,
                "rank_score": 2.0,
                "trade_status": "Trade",
                "chain_source": "tradier",
                "quote_quality": "live_or_broker",
            },
            {
                "ticker": "AAPL",
                "side": "put",
                "strike": 200.0,
                "expiry": "2026-06-18",
                "mid": 2.0,
                "confidence": 99,
                "rank_score": 9.0,
            },
        ]).to_parquet(data_dir / "top_options_20260603_120000.parquet")

        report = lookup_symbol("AAPL 20260618 C 200", data_dir)
        matches = report["sections"]["requested_option_matches"]
        assert matches[0]["ticker"] == "AAPL"
        assert matches[0]["side"] == "call"
        assert matches[0]["strike"] == 200.0
        assert matches[0]["match_quality"] == "exact"
        assert report["lookup_symbol"] == "AAPL"
        assert report["brief"]["requested_option"]["label"] == "AAPL 2026-06-18 C 200"
        assert report["brief"]["requested_option"]["match_quality"] == "exact"
        assert report["brief"]["requested_option"]["matched_contract"] == "AAPL C 200.0 2026-06-18"
        assert report["brief"]["paper_readiness"]["status"] == "ready"
        assert report["brief"]["paper_readiness"]["score"] >= 75
        html = render_html(report)
        assert "Requested option" in html
        assert "Requested match" in html
        assert "Paper readiness" in html
        assert "Readiness checklist" in html


def test_lookup_resolves_company_name_option_request_to_ticker():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        pd.DataFrame([
            {
                "ticker": "AAPL",
                "side": "call",
                "strike": 200.0,
                "expiry": "2026-06-18",
                "mid": 3.2,
                "confidence": 80,
                "rank_score": 2.0,
            },
        ]).to_parquet(data_dir / "top_options_20260603_120000.parquet")

        report = lookup_symbol("Apple 20260618 C 200", data_dir)
        assert report["lookup_symbol"] == "AAPL"
        assert report["resolution"]["source"] == "alias"
        assert report["resolution"]["request"]["ticker"] == "AAPL"
        assert report["sections"]["requested_option_matches"][0]["match_quality"] == "exact"
        assert report["brief"]["resolution_source"] == "alias"
        assert "Resolved via" in render_html(report)


def test_lookup_matches_requested_option_from_chain_shortlist_without_top_board():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        (data_dir / "option_chain_shortlist.json").write_text(json.dumps({
            "generated_at": "2026-06-24T19:00:00+00:00",
            "rows": [{
                "symbol": "AAPL",
                "contract_query": "AAPL 2027-01-15 C 220",
                "side": "call",
                "strike": 220.0,
                "expiry": "2027-01-15",
                "dte": 205,
                "bid": 4.9,
                "ask": 5.1,
                "mid": 5.0,
                "premium_dollars": 500.0,
                "spread_pct": 0.04,
                "openInterest": 1200,
                "volume": 80,
                "stop_price_reference": 2.5,
                "target_price_reference": 10.0,
                "readiness_score": 92,
                "readiness_label": "ready",
                "contract_quality_score": 94,
                "swing_fit_score": 96,
                "swing_fit_label": "clean_swing",
                "contract_grade": "A",
                "review_lane": "primary_review",
                "chain_source": "cboe_options_chain",
                "quote_quality": "free_or_delayed",
                "review_thesis": "Good depth for a six-month swing candidate.",
            }],
        }), encoding="utf-8")

        report = lookup_symbol("AAPL 20270115 C 220", data_dir)

        chain_rows = report["sections"]["chain_shortlist"]
        assert chain_rows[0]["ticker"] == "AAPL"
        assert chain_rows[0]["contract_grade"] == "A"
        matches = report["sections"]["requested_option_matches"]
        assert matches[0]["ticker"] == "AAPL"
        assert matches[0]["strike"] == 220.0
        assert matches[0]["match_quality"] == "exact"
        assert matches[0]["match_source"] == "option_chain_shortlist"
        assert matches[0]["readiness_score"] == 92
        assert report["brief"]["requested_option"]["match_quality"] == "exact"
        assert report["brief"]["best_idea"]["asset"] == "option"
        assert report["brief"]["best_idea"]["contract_grade"] == "A"
        assert "option_chain_shortlist.json" in report["sources"]["requested_option_matches"]
        html = render_html(report)
        assert "Chain Shortlist" in html
        assert "option_chain_shortlist" in html


def test_option_request_falls_back_to_closest_strike():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        pd.DataFrame([
            {"ticker": "MSFT", "side": "call", "strike": 410.0, "expiry": "2026-06-18"},
            {"ticker": "MSFT", "side": "call", "strike": 430.0, "expiry": "2026-06-18"},
        ]).to_parquet(data_dir / "top_options_20260603_120000.parquet")

        matches = match_option_request({
            "asset": "option",
            "ticker": "MSFT",
            "side": "call",
            "strike": 420.0,
            "expiry": "2026-06-18",
        }, data_dir)
        assert matches[0]["strike"] == 410.0
        assert matches[0]["strike_diff"] == 10.0


def test_lookup_brief_warns_when_requested_option_is_closest_only():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        pd.DataFrame([
            {"ticker": "MSFT", "side": "call", "strike": 410.0, "expiry": "2026-06-18"},
            {"ticker": "MSFT", "side": "call", "strike": 430.0, "expiry": "2026-06-18"},
        ]).to_parquet(data_dir / "top_options_20260603_120000.parquet")

        report = lookup_symbol("MSFT 20260618 C 420", data_dir)
        requested = report["brief"]["requested_option"]
        assert requested["label"] == "MSFT 2026-06-18 C 420"
        assert requested["match_quality"] == "closest"
        assert requested["strike_diff"] == 10.0
        assert report["brief"]["paper_readiness"]["status"] in {"caution", "blocked"}
        assert any(
            row["label"] == "Requested option match"
            and row["level"] == "warn"
            for row in report["brief"]["paper_readiness"]["checks"]
        )
        assert any("matched as closest" in warning for warning in report["brief"]["risk_warnings"])
        assert "Requested match" in render_html(report)


def test_lookup_builds_research_brief_from_local_factors_and_open_state():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        pd.DataFrame([{
            "ticker": "NVDA",
            "side": "call",
            "strike": 200.0,
            "expiry": "2026-06-18",
            "mid": 4.2,
            "confidence": 82,
            "rank_score": 2.5,
            "trade_status": "Trade",
            "stop_price": 2.1,
            "target_price": 8.4,
            "spread_pct": 0.12,
            "net_edge_pct": 0.35,
            "suggested_contracts": 1,
            "chain_source": "tradier",
            "quote_quality": "live_or_broker",
            "z_macro": 1.5,
            "z_insider": -0.8,
            "top_headline": "NVDA test headline",
        }]).to_parquet(data_dir / "top_options_20260603_120000.parquet")
        (data_dir / "open_positions.json").write_text(json.dumps([{
            "ticker": "NVDA",
            "side": "call",
            "strike": 200,
            "expiry": "2026-06-18",
            "entry_price": 3.0,
            "current_mid": 4.5,
            "unrealized_pct": 0.5,
            "latest_exit_pressure": 22,
        }]), encoding="utf-8")
        (data_dir / "validation_summary.json").write_text(json.dumps({
            "validation_scope": "current_model",
            "closed_positions": 10,
            "open_positions": 1,
            "overall": {"win_rate": 0.6, "avg_return": 0.12},
            "warnings": ["sample warning"],
        }), encoding="utf-8")

        report = lookup_symbol("NVDA", data_dir)
        brief = report["brief"]
        assert brief["symbol"] == "NVDA"
        assert brief["best_idea"]["label"] == "NVDA C 200.0 2026-06-18"
        assert brief["best_idea"]["quote_source_label"] == "Live Tradier"
        assert brief["best_idea"]["quote_source"]["is_live_or_broker"] is True
        assert brief["best_idea"]["spread_pct"] == 0.12
        assert brief["best_idea"]["net_edge_pct"] == 0.35
        assert brief["open_positions"]["count"] == 1
        assert brief["open_positions"]["avg_unrealized_pct"] == 0.5
        assert brief["validation"]["win_rate"] == 0.6
        assert brief["research_action"]["action"] == "paper_candidate_review"
        assert brief["research_action"]["can_export_paper_candidate"] is True
        assert "macro" in {x["factor"] for x in brief["top_positive_factors"]}
        assert "insider" in {x["factor"] for x in brief["top_negative_factors"]}
        assert "sample warning" in brief["risk_warnings"]
        html = render_html(report)
        assert "Research action" in html
        assert "Research Brief" in html
        assert "Quote source" in html
        assert "Live Tradier" in html


def test_lookup_flags_stale_snapshot_age():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        path = data_dir / "top_options_20260603_120000.parquet"
        pd.DataFrame([{
            "ticker": "NVDA",
            "side": "call",
            "strike": 200.0,
            "expiry": "2026-06-18",
            "mid": 4.2,
            "confidence": 82,
            "rank_score": 2.5,
            "trade_status": "Trade",
        }]).to_parquet(path)
        old_ts = time.time() - (8 * 60 * 60)
        os.utime(path, (old_ts, old_ts))

        report = lookup_symbol("NVDA", data_dir)
        brief = report["brief"]
        assert brief["best_idea"]["snapshot_freshness"] == "stale"
        assert brief["best_idea"]["snapshot_age_min"] >= 360
        assert any("stale" in str(w).lower() for w in brief["risk_warnings"])
        assert any(
            "stale snapshot" in str(reason).lower()
            for reason in brief["research_action"]["reasons"]
        )
        html = render_html(report)
        assert "Snapshot age" in html
        assert "stale" in html


def test_lookup_includes_recent_sec_filings_when_available():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        old = lookup_module.recent_filings_for_symbol
        try:
            lookup_module.recent_filings_for_symbol = lambda symbol, limit=8: {
                "symbol": symbol,
                "source": "sec_edgar_submissions",
                "count": 2,
                "rows": [
                    {
                        "ticker": symbol,
                        "company_name": "NVIDIA CORP",
                        "form": "8-K",
                        "filing_date": "2026-06-01",
                        "report_date": "2026-06-01",
                        "filing_signal": "material_event_review",
                        "description": "Current report",
                        "url": "https://www.sec.gov/example",
                    },
                    {
                        "ticker": symbol,
                        "company_name": "NVIDIA CORP",
                        "form": "10-Q",
                        "filing_date": "2026-05-20",
                        "report_date": "2026-04-30",
                        "filing_signal": "fundamental_update_review",
                        "description": "Quarterly report",
                        "url": "https://www.sec.gov/example2",
                    },
                ],
            }
            report = lookup_symbol("NVDA", data_dir)
        finally:
            lookup_module.recent_filings_for_symbol = old

        filings = report["sections"]["recent_sec_filings"]
        assert len(filings) == 2
        assert filings[0]["form"] == "8-K"
        assert report["sources"]["recent_sec_filings"] == "SEC EDGAR submissions API"
        assert report["brief"]["recent_sec_filings"]["count"] == 2
        assert "material_event_review" in report["brief"]["recent_sec_filings"]["watch_signals"]
        assert report["brief"]["research_action"]["action"] == "run_focused_scan"
        assert "Recent SEC filings" in render_html(report)


def test_lookup_includes_sec_companyfacts_when_available():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        old = lookup_module.companyfacts_for_symbol
        try:
            lookup_module.companyfacts_for_symbol = lambda symbol, limit=12: {
                "symbol": symbol,
                "source": "sec_companyfacts",
                "count": 3,
                "rows": [
                    {
                        "ticker": symbol,
                        "company_name": "NVIDIA CORP",
                        "metric": "cash",
                        "label": "Cash and equivalents",
                        "value": 10_000_000_000,
                        "unit": "USD",
                        "period_end": "2026-04-30",
                        "filed": "2026-05-20",
                        "form": "10-Q",
                    },
                    {
                        "ticker": symbol,
                        "company_name": "NVIDIA CORP",
                        "metric": "debt",
                        "label": "Debt",
                        "value": 5_000_000_000,
                        "unit": "USD",
                        "period_end": "2026-04-30",
                        "filed": "2026-05-20",
                        "form": "10-Q",
                    },
                    {
                        "ticker": symbol,
                        "company_name": "NVIDIA CORP",
                        "metric": "net_income",
                        "label": "Net income",
                        "value": -100_000_000,
                        "unit": "USD",
                        "period_end": "2026-04-30",
                        "filed": "2026-05-20",
                        "form": "10-Q",
                    },
                ],
                "metrics": {
                    "cash": 10_000_000_000,
                    "debt": 5_000_000_000,
                    "assets": 50_000_000_000,
                    "cash_to_debt": 2.0,
                    "debt_to_assets": 0.10,
                    "net_margin": -0.05,
                },
                "watch_signals": ["unprofitable_watch"],
            }
            report = lookup_symbol("NVDA", data_dir)
        finally:
            lookup_module.companyfacts_for_symbol = old

        facts = report["sections"]["sec_companyfacts"]
        assert len(facts) == 3
        assert report["sources"]["sec_companyfacts"] == "SEC EDGAR companyfacts API"
        assert report["brief"]["sec_fundamentals"]["cash_to_debt"] == 2.0
        assert "unprofitable_watch" in report["brief"]["sec_fundamentals"]["watch_signals"]
        assert "SEC companyfacts: unprofitable_watch" in report["brief"]["risk_warnings"]
        html = render_html(report)
        assert "SEC cash/debt" in html
        assert "2.00x" in html


def test_lookup_action_prioritizes_open_exit_pressure():
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
        (data_dir / "open_positions.json").write_text(json.dumps([{
            "ticker": "NVDA",
            "side": "call",
            "strike": 200,
            "expiry": "2026-06-18",
            "entry_price": 3.0,
            "current_mid": 2.2,
            "unrealized_pct": -0.27,
            "latest_exit_pressure": 84,
        }]), encoding="utf-8")

        action = lookup_symbol("NVDA", data_dir)["brief"]["research_action"]
        assert action["action"] == "review_exit_now"
        assert action["risk_level"] == "high"
        assert action["can_export_paper_candidate"] is False


def test_lookup_includes_broker_snapshot_and_blocks_duplicate_entry():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        pd.DataFrame([{
            "ticker": "ROBN",
            "side": "call",
            "strike": 35.0,
            "expiry": "2026-12-18",
            "mid": 11.8,
            "confidence": 82,
            "rank_score": 2.5,
            "trade_status": "Trade",
            "chain_source": "tradier",
            "quote_quality": "live_or_broker",
            "suggested_contracts": 1,
            "stop_price": 7.0,
            "target_price": 18.0,
        }]).to_parquet(data_dir / "top_options_20260624_120000.parquet")
        (data_dir / "robinhood_broker_snapshot.json").write_text(json.dumps({
            "generated_at": "2026-06-24T19:00:00+00:00",
            "accounts": [{
                "account_mask": "****1497",
                "label": "Default individual margin",
                "agentic_allowed": False,
                "option_level": "option_level_2",
                "option_positions": [{
                    "chain_symbol": "ROBN",
                    "symbol": "ROBN",
                    "option_type": "call",
                    "strike_price": "35.0000",
                    "expiration_date": "2026-12-18",
                    "quantity": "2.0000",
                    "average_price": 6.45,
                    "current_price": 11.8,
                    "bid_price": 10.7,
                    "ask_price": 12.9,
                    "quote_updated_at": "2026-06-24T19:00:00Z",
                }],
            }],
        }), encoding="utf-8")

        report = lookup_symbol("ROBN", data_dir)
        broker_rows = report["sections"]["broker_positions"]
        assert broker_rows[0]["symbol"] == "ROBN"
        assert broker_rows[0]["contract"] == "ROBN 2026-12-18 C 35.0000"
        assert broker_rows[0]["unrealized_pct"] > 0.80
        brief = report["brief"]
        assert brief["broker_positions"]["count"] == 1
        assert brief["broker_positions"]["option_count"] == 1
        assert brief["research_action"]["action"] == "review_broker_position"
        assert brief["research_action"]["can_export_paper_candidate"] is False
        assert any("Broker snapshot has 1 position" in warning for warning in brief["risk_warnings"])
        html = render_html(report)
        assert "Broker positions" in html
        assert "Broker snapshot" in html


if __name__ == "__main__":
    test_resolver_prefers_local_aliases_for_common_company_names_and_futures()
    test_lookup_reads_open_option_positions()
    test_lookup_reads_open_futures_positions()
    test_lookup_saves_json_and_html()
    test_lookup_matches_requested_option_contract()
    test_lookup_resolves_company_name_option_request_to_ticker()
    test_lookup_matches_requested_option_from_chain_shortlist_without_top_board()
    test_option_request_falls_back_to_closest_strike()
    test_lookup_brief_warns_when_requested_option_is_closest_only()
    test_lookup_builds_research_brief_from_local_factors_and_open_state()
    test_lookup_flags_stale_snapshot_age()
    test_lookup_includes_recent_sec_filings_when_available()
    test_lookup_includes_sec_companyfacts_when_available()
    test_lookup_action_prioritizes_open_exit_pressure()
    test_lookup_includes_broker_snapshot_and_blocks_duplicate_entry()
    print("15/15 lookup tests passed")
