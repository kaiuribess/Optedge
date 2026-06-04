import json
import sys
import tempfile
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
        assert brief["open_positions"]["count"] == 1
        assert brief["open_positions"]["avg_unrealized_pct"] == 0.5
        assert brief["validation"]["win_rate"] == 0.6
        assert brief["research_action"]["action"] == "paper_candidate_review"
        assert brief["research_action"]["can_export_paper_candidate"] is True
        assert "macro" in {x["factor"] for x in brief["top_positive_factors"]}
        assert "insider" in {x["factor"] for x in brief["top_negative_factors"]}
        assert "sample warning" in brief["risk_warnings"]
        assert "Research action" in render_html(report)
        assert "Research Brief" in render_html(report)


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


if __name__ == "__main__":
    test_resolver_prefers_local_aliases_for_common_company_names_and_futures()
    test_lookup_reads_open_option_positions()
    test_lookup_reads_open_futures_positions()
    test_lookup_saves_json_and_html()
    test_lookup_matches_requested_option_contract()
    test_lookup_resolves_company_name_option_request_to_ticker()
    test_option_request_falls_back_to_closest_strike()
    test_lookup_builds_research_brief_from_local_factors_and_open_state()
    test_lookup_includes_recent_sec_filings_when_available()
    test_lookup_action_prioritizes_open_exit_pressure()
    print("10/10 lookup tests passed")
