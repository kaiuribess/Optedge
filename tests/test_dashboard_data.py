# Purpose: Test deduplicated dashboard positions and performance.
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard import build as dashboard_build


def _sample_option_row(**extra):
    row = {
        "ticker": "AAPL",
        "side": "call",
        "strike": 280.0,
        "spot": 300.0,
        "top_headline": "",
        "days_to_earnings": None,
        "pred_option_return_pct": 0.1,
        "ev_pct": 0.2,
        "kelly_pct": 0.05,
        "actual_dollars": 500,
        "suggested_contracts": 1,
        "stop_price": 0.5,
        "target_price": 2.0,
        "contract": "AAPL 2026-06-18 C 280",
        "trade_status": "Trade",
        "confidence": 80,
        "dte": 30,
        "mid": 1.0,
        "iv_market": 0.4,
        "fair_vol": 0.3,
        "vol_premium": 0.1,
        "delta": 0.4,
        "open_interest": 500,
        "spread_pct": 0.05,
        "reasoning": "test",
        "risks": "test",
    }
    row.update(extra)
    return dashboard_build.pd.Series(row)


def test_dashboard_helpers_dedupe_and_label_positions():
    rows = [
        {
            "asset": "option",
            "ticker": "AAPL",
            "side": "call",
            "strike": 280,
            "expiry": "2026-06-18",
            "entry_time": "2026-06-01T00:00:00+00:00",
            "entry_price": 2.0,
        },
        {
            "asset": "option",
            "ticker": "AAPL",
            "side": "call",
            "strike": 280,
            "expiry": "2026-06-18",
            "entry_time": "2026-06-01T00:00:00+00:00",
            "entry_price": 2.0,
        },
    ]
    assert len(dashboard_build._dedupe_position_rows(rows)) == 1
    assert dashboard_build._open_position_label(rows[0]) == "AAPL C 280 06-18"
    assert dashboard_build._is_win_pnl(0.01) is True
    assert dashboard_build._is_win_pnl(-0.01) is False


def test_option_card_and_table_show_quote_quality():
    live_row = _sample_option_row(
        chain_source="tradier", quote_quality="live_or_broker",
        buyer_edge_pct=0.10, pricing_direction="underpriced_after_spread",
        trade_gate_reason="passed",
    )
    fallback_row = _sample_option_row(ticker="MSFT", chain_source="yfinance", quote_quality="free_or_delayed")
    yahoo_row = _sample_option_row(ticker="NVDA", chain_source="yahoo_options", quote_quality="free_or_delayed")

    card_html = dashboard_build._option_card(live_row)
    table_html = dashboard_build._options_table(dashboard_build.pd.DataFrame([
        live_row,
        fallback_row,
        yahoo_row,
    ]))

    assert "Live Tradier" in card_html
    assert "buyer edge +10.0%" in card_html
    assert "underpriced after spread" in card_html
    assert "Source" in table_html
    assert "Live Tradier" in table_html
    assert "Yahoo fallback" in table_html
    assert "Yahoo options" in table_html


def test_option_card_hides_position_size_for_non_actionable_pricing():
    row = _sample_option_row(
        trade_status="Watch", buyer_edge_pct=-0.20,
        pricing_direction="overpriced_after_spread",
        trade_gate_reason="negative_buyer_edge_after_spread",
    )
    card_html = dashboard_build._option_card(row)
    assert "Not executable: negative buyer edge after spread." in card_html
    assert "<strong>1</strong> contract" not in card_html


def test_dashboard_analytics_uses_pnl_wins_and_unique_open_labels():
    old_root = dashboard_build.ROOT
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        data = root / "data"
        data.mkdir()
        dashboard_build.ROOT = root
        try:
            closed = [
                {
                    "asset": "option",
                    "ticker": "AAPL",
                    "side": "call",
                    "strike": 280,
                    "expiry": "2026-06-18",
                    "entry_time": "2026-06-01T00:00:00+00:00",
                    "exit_time": "2026-06-02T00:00:00+00:00",
                    "entry_price": 2.0,
                    "exit_reason": "hard_target",
                    "pnl_pct": 1.0,
                    "confidence": 70,
                },
                {
                    "asset": "option",
                    "ticker": "MSFT",
                    "side": "put",
                    "strike": 400,
                    "expiry": "2026-06-18",
                    "entry_time": "2026-06-01T01:00:00+00:00",
                    "exit_time": "2026-06-02T01:00:00+00:00",
                    "entry_price": 2.0,
                    "exit_reason": "dynamic_exit",
                    "pnl_pct": 0.2,
                    "confidence": 70,
                },
                {
                    "asset": "option",
                    "ticker": "TSLA",
                    "side": "call",
                    "strike": 500,
                    "expiry": "2026-06-18",
                    "entry_time": "2026-06-01T02:00:00+00:00",
                    "exit_time": "2026-06-02T02:00:00+00:00",
                    "entry_price": 2.0,
                    "exit_reason": "hard_stop",
                    "pnl_pct": -0.5,
                    "confidence": 70,
                },
            ]
            open_rows = [
                {
                    "asset": "option",
                    "ticker": "AAPL",
                    "side": "call",
                    "strike": 280,
                    "expiry": "2026-06-18",
                    "entry_time": "2026-06-03T00:00:00+00:00",
                    "entry_price": 2.0,
                    "current_mid": 3.0,
                    "unrealized_pct": 0.5,
                    "stop_price": 1.0,
                    "target_price": 4.0,
                },
                {
                    "asset": "option",
                    "ticker": "AAPL",
                    "side": "call",
                    "strike": 285,
                    "expiry": "2026-06-18",
                    "entry_time": "2026-06-03T01:00:00+00:00",
                    "entry_price": 1.5,
                    "current_mid": 1.0,
                    "unrealized_pct": -0.3333,
                    "stop_price": 0.75,
                    "target_price": 3.0,
                },
            ]
            (data / "closed_positions.json").write_text(json.dumps(closed), encoding="utf-8")
            (data / "open_positions.json").write_text(json.dumps(open_rows), encoding="utf-8")

            html = dashboard_build._build_analytics_html()
            assert "Win rate (3 closed)" in html
            assert "66.7%" in html
            assert "Gross cumulative P&amp;L" not in html
            assert "Median closed P&amp;L" in html
            assert "+20%" in html
            assert "All open positions (2)" in html
            assert "AAPL C 280 06-18" in html
            assert "AAPL C 285 06-18" in html
        finally:
            dashboard_build.ROOT = old_root


def test_dashboard_performance_prefers_validation_over_forward_telemetry():
    validation = {
        "open_positions": 4,
        "closed_positions": 2,
        "overall": {
            "win_rate": 0.5,
            "avg_return": 0.1,
            "median_return": 0.05,
            "profit_factor": 1.2,
            "max_drawdown": -0.03,
        },
        "assets": {
            "option": {"open_positions": 3, "closed_positions": 2, "win_rate": 0.5, "avg_return": 0.1},
            "share": {"open_positions": 1, "closed_positions": 0},
            "futures": {"open_positions": 0, "closed_positions": 0},
        },
        "fixed_horizon": {
            "headline_horizon_sessions": 10,
            "headline": {
                "n": 0, "unique_entry_days": 0,
            },
            "headline_shadow": {
                "n": 8, "unique_entry_days": 3, "win_rate": 0.625,
                "avg_return": 0.04, "avg_excess_vs_spy": 0.02,
            },
        },
    }
    forward = {
        "signals": object(),
        "overall": {"n_signals": 999, "win_rate": 0.99, "avg_pnl_pct": 9.99},
    }

    html = dashboard_build._performance_panel(forward, validation)
    assert "lifecycle validation" in html
    assert "<span class=\"val\">4</span>" in html
    assert "<span class=\"val\">2</span>" in html
    assert "999" not in html
    assert "Independent 10-session evidence" in html
    assert "n=8 / 3 days" in html


def test_dashboard_engine_panels_are_merged_into_one_section():
    html = dashboard_build._build_v20_panels_html(
        portfolio_greeks={},
        hedge_suggestion=None,
        breaker_state=None,
        engine_timings={
            "news": {"elapsed": 2.0, "rows": 100, "ok": True},
            "insider": {"elapsed": 5.0, "rows": 80, "ok": True},
        },
        engine_health={
            "engines": [
                {"engine": "news", "health_score": 90, "hit_rate": 0.9, "ok_rate": 1.0, "avg_elapsed": 2.0},
                {"engine": "insider", "health_score": 65, "hit_rate": 0.6, "ok_rate": 0.9, "avg_elapsed": 5.0},
            ]
        },
        v20_factors={},
        empty_engines=[],
    )

    assert html.count('id="sect-telemetry"') == 1
    assert "Engine health" not in html
    assert "Engine telemetry" not in html
    assert "Engine runtime" in html
    assert "This run" in html
    assert "Rolling health" in html


def test_dashboard_section_labels_are_not_mislabeled_as_analyst_only():
    analytics_html = dashboard_build._build_analytics_html()
    assert "Live Signal Analytics" in analytics_html
    assert "Analyst Live Analytics" not in analytics_html

    performance_html = dashboard_build._performance_panel(None, {
        "open_positions": 0,
        "closed_positions": 0,
        "overall": {},
        "assets": {},
    })
    assert "Signal Performance Tracking" in performance_html
    assert "Analyst Performance Tracking" not in performance_html

    analyst_html = dashboard_build._analyst_panel(dashboard_build.pd.DataFrame([
        {
            "ticker": "AAPL",
            "analyst_total": 8,
            "analyst_score": 1.2,
            "analyst_momentum": 1,
            "analyst_strong_buy": 2,
            "analyst_buy": 4,
            "analyst_hold": 2,
            "analyst_sell": 0,
            "analyst_strong_sell": 0,
        }
    ]))
    assert "Analyst Recommendations" in analyst_html
    assert "Analyst Analyst Recommendations" not in analyst_html


def test_dashboard_includes_export_and_workflow_controls():
    old_root = dashboard_build.ROOT
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data").mkdir()
        dashboard_build.ROOT = root
        try:
            empty = dashboard_build.pd.DataFrame()
            html = dashboard_build.render(
                calls=empty,
                puts=empty,
                shares=empty,
                ranked_options=empty,
                ranked_shares=empty,
                macro={},
                asof=__import__("datetime").datetime(2026, 6, 1),
                value_plays=empty,
                futures_plays=empty,
            ).read_text(encoding="utf-8")
        finally:
            dashboard_build.ROOT = old_root

    assert 'id="download-csv"' in html
    assert 'id="download-json"' in html
    assert 'id="copy-visible"' in html
    assert 'id="print-dashboard"' in html
    assert 'id="top-only"' in html
    assert "optedge-visible-" in html
    assert "Long-only buy list" not in html
    assert "9 signals" not in html
    assert "Multi-asset swing research" in html
    assert "Discovery profile:" in html
    assert "Swing-execution profile:" in html


if __name__ == "__main__":
    test_dashboard_helpers_dedupe_and_label_positions()
    test_option_card_and_table_show_quote_quality()
    test_option_card_hides_position_size_for_non_actionable_pricing()
    test_dashboard_analytics_uses_pnl_wins_and_unique_open_labels()
    test_dashboard_performance_prefers_validation_over_forward_telemetry()
    test_dashboard_engine_panels_are_merged_into_one_section()
    test_dashboard_section_labels_are_not_mislabeled_as_analyst_only()
    test_dashboard_includes_export_and_workflow_controls()
    print("8/8 dashboard data tests passed")
