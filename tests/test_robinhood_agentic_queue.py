import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.export_robinhood_agentic_queue as rh_module
from scripts.export_robinhood_agentic_queue import (
    CYCLE_JSON,
    CYCLE_PROMPT_MD,
    DECISION_LOG_JSONL,
    append_agent_decision,
    build_agentic_cycle_packet,
    build_queue_from_candidates,
    build_robinhood_queue,
    decision_log_summary,
    normalize_agent_decision,
    render_agent_prompt,
    render_cycle_prompt,
    write_outputs,
)


def _candidate(**overrides):
    row = {
        "generated_at": "2026-06-11T10:00:00+00:00",
        "asset": "option",
        "ticker_or_symbol": "AAPL",
        "action": "BUY_TO_OPEN",
        "direction": "long_call",
        "quantity": 1,
        "contract": "AAPL 2027-01-15 CALL 200",
        "option_side": "call",
        "strike": 200,
        "expiry": "2027-01-15",
        "entry_price": 0.75,
        "stop_price": 0.35,
        "target_price": 1.6,
        "confidence": 72,
        "rank_score": 2.1,
        "fused_score": 1.8,
        "trade_status": "Trade",
        "risk_dollars": 40,
        "reward_dollars": 85,
        "suggested_contracts": 1,
        "reason_selected": "passed external option filters",
        "reason_excluded": "",
    }
    row.update(overrides)
    return row


def _queue(rows, **kwargs):
    return build_queue_from_candidates(
        pd.DataFrame(rows),
        generated_at="2026-06-11T10:00:00+00:00",
        **kwargs,
    )


def test_queue_is_options_only():
    queue = _queue([
        _candidate(),
        _candidate(asset="share", ticker_or_symbol="NVDA", action="BUY", entry_price=100),
    ])
    assert len(queue["orders"]) == 1
    assert queue["orders"][0]["asset"] == "option"
    assert "not an option candidate" in queue["rejected"][0]["reasons"]


def test_queue_accepts_ticker_fallback_and_writes_aliases():
    row = _candidate()
    row.pop("ticker_or_symbol")
    row["ticker"] = "AAPL"
    queue = _queue([row])
    order = queue["orders"][0]
    assert order["symbol"] == "AAPL"
    assert order["ticker_or_symbol"] == "AAPL"


def test_ready_queue_with_guarded_rejects_is_labeled_ready_guarded():
    queue = _queue([
        _candidate(ticker_or_symbol="AAPL", rank_score=5.0),
        _candidate(
            ticker_or_symbol="TSLA",
            contract="TSLA 2027-01-15 CALL 500",
            reason_excluded="research guard blocked",
            rank_score=4.0,
        ),
    ])
    assert len(queue["orders"]) == 1
    assert queue["diagnostics"]["label"] == "ready_guarded"
    assert queue["diagnostics"]["reason_groups"]["research_guard_blocked"] == 1


def test_queue_rejects_contracts_above_500_budget_caps():
    queue = _queue([
        _candidate(entry_price=2.50, confidence=90, rank_score=9.0),
        _candidate(ticker_or_symbol="MSFT", contract="MSFT 2027-01-15 CALL 500"),
    ])
    symbols = {row["symbol"] for row in queue["orders"]}
    assert symbols == {"MSFT"}
    rejected = [row for row in queue["rejected"] if row["ticker"] == "AAPL"][0]
    assert "premium cap leaves no buyable contracts" in rejected["reasons"]
    assert queue["diagnostics"]["reason_groups"]["premium_cap"] == 1
    assert any("premium cap" in note for note in queue["diagnostics"]["notes"])
    assert queue["diagnostics"]["near_misses"][0]["ticker"] == "AAPL"
    assert "Review only" in queue["diagnostics"]["near_misses"][0]["review_note"]
    ladder = queue["diagnostics"]["budget_ladder"]
    assert ladder["review_only"] is True
    assert ladder["current_max_premium_per_order"] == 150.0
    assert ladder["next_unlock_cap"] == 250.0
    assert any(row["unlock_count"] >= 1 for row in ladder["caps"])


def test_queue_blocks_bullish_calls_with_active_sec_offering_risk():
    risks = {
        "AAPL": [{
            "ticker": "AAPL",
            "form": "S-3",
            "filing_date": "2026-06-10",
            "days_old": 1,
            "signal": "dilution_or_offering_watch",
        }]
    }
    queue = _queue([
        _candidate(ticker_or_symbol="AAPL", option_side="call", direction="long_call"),
        _candidate(ticker_or_symbol="AAPL", option_side="put", direction="long_put", contract="AAPL 2027-01-15 PUT 200"),
    ], sec_offering_risks=risks)
    assert len(queue["orders"]) == 1
    assert queue["orders"][0]["option_side"] == "put"
    assert queue["sec_offering_risks"]["AAPL"][0]["form"] == "S-3"
    assert queue["diagnostics"]["reason_groups"]["sec_offering_risk"] == 1
    assert any("SEC offering" in reason for reason in queue["rejection_reason_counts"])

    prompt = render_agent_prompt(queue)
    assert "SEC Offering / Dilution Risk" in prompt
    assert "Bullish call candidates" in prompt


def test_queue_carries_public_cboe_activity_context():
    activity = pd.DataFrame([
        {
            "ticker": "AAPL",
            "expiry": "2027-01-15",
            "strike": 200.0,
            "option_side": "call",
            "cboe_activity_volume": 321,
            "cboe_activity_matched": 300,
            "cboe_activity_routed": 21,
            "cboe_activity_bid": 0.70,
            "cboe_activity_ask": 0.75,
            "cboe_activity_last": 0.72,
            "cboe_activity_contract": "AAPL Jan 15 200.0 Call",
            "cboe_activity_venues": "Cboe Options",
            "cboe_activity_source": "cboe_symbol_data",
        }
    ])
    queue = _queue([_candidate()], cboe_activity=activity)
    order = queue["orders"][0]
    assert order["cboe_activity_volume"] == 321
    assert "Public Cboe symbol activity matched" in order["cboe_activity_note"]
    assert queue["cboe_activity"]["exact_candidate_matches"] == 1
    prompt = render_agent_prompt(queue)
    assert "Public Cboe Activity Check" in prompt
    assert "volume 321" in prompt


def test_queue_caps_candidate_count_separately_from_order_count():
    queue = _queue(
        [
            _candidate(ticker_or_symbol="AAPL", contract="AAPL 2027-01-15 CALL 200", rank_score=5.0),
            _candidate(ticker_or_symbol="MSFT", contract="MSFT 2027-01-15 CALL 500", rank_score=4.0),
            _candidate(ticker_or_symbol="NVDA", contract="NVDA 2027-01-15 CALL 200", rank_score=3.0),
        ],
        max_orders=2,
        max_total_premium=500,
        max_premium_per_order=100,
        max_candidates=2,
    )
    assert len(queue["orders"]) == 2
    assert queue["max_orders_to_submit"] == 2
    assert queue["estimated_total_candidate_premium"] == 150.0
    assert any("max candidate count reached" in row["reasons"] for row in queue["rejected"])


def test_queue_rejects_short_dated_options_by_default():
    queue = _queue([_candidate(expiry="2026-06-18", contract="AAPL 2026-06-18 CALL 200")])
    assert queue["orders"] == []
    assert "dte below 90" in queue["rejected"][0]["reasons"]
    assert queue["rejection_reason_counts"]["dte below 90"] == 1
    assert queue["readiness"]["label"] == "empty"


def test_queue_enforces_total_premium_cap_and_summarizes_rejections():
    queue = _queue(
        [
            _candidate(ticker_or_symbol="AAPL", contract="AAPL 2027-01-15 CALL 200", rank_score=5.0),
            _candidate(ticker_or_symbol="MSFT", contract="MSFT 2027-01-15 CALL 500", rank_score=4.0),
        ],
        max_total_premium=100,
        max_premium_per_order=100,
    )
    assert len(queue["orders"]) == 1
    assert queue["orders"][0]["symbol"] == "AAPL"
    assert queue["rejection_reason_counts"]["max total premium reached"] == 1
    assert queue["top_rejection_reasons"][0]["reason"] == "max total premium reached"
    assert queue["readiness"]["premium_cap_remaining"] == 25.0


def test_queue_can_give_agent_more_candidates_than_order_cap():
    rows = [
        _candidate(ticker_or_symbol=f"T{i}", contract=f"T{i} 2027-01-15 CALL 20", rank_score=10 - i)
        for i in range(6)
    ]
    queue = _queue(rows, max_candidates=5, max_orders=2, max_total_premium=500)
    assert len(queue["orders"]) == 5
    assert queue["max_orders_to_submit"] == 2
    assert any("max candidate count reached" in row["reasons"] for row in queue["rejected"])


def test_queue_prompt_requires_codex_double_check_and_limit_orders():
    queue = _queue([_candidate()])
    prompt = render_agent_prompt(queue)
    assert "Double-check current Robinhood quotes" in prompt
    assert "BUY_TO_OPEN limit DAY orders only" in prompt
    assert "Do not exceed any max_limit_price" in prompt
    assert "Long-dated options only" in prompt
    assert "current news" in prompt


def test_queue_includes_recurring_cycle_and_management_rules():
    queue = _queue([_candidate()])
    assert queue["agent_cycle"]["recommended_interval_minutes"] == 30
    assert queue["agent_cycle"]["default_execution_mode"] == "approval_required"
    assert queue["agent_cycle"]["auto_submit_default"] is False
    assert any("SELL_TO_CLOSE" in check for check in queue["required_management_checks"])

    prompt = render_agent_prompt(queue)
    assert "Recurring Cycle Checklist" in prompt
    assert "Position Management Checks" in prompt
    assert "Do not auto-submit orders" in prompt
    assert "SELL_TO_CLOSE limit DAY orders only" in prompt


def test_empty_queue_diagnostics_explain_stale_and_short_dte_rows():
    queue = _queue([
        _candidate(
            expiry="2026-07-17",
            contract="AAPL 2026-07-17 CALL 200",
            reason_excluded="stale row; dte below 180",
        )
    ])
    diagnostics = queue["diagnostics"]
    assert diagnostics["label"] == "refresh_chain_scan"
    assert diagnostics["reason_groups"]["stale"] >= 1
    assert diagnostics["reason_groups"]["below_min_dte"] >= 1
    assert any("Refresh" in step for step in diagnostics["remediation"])

    prompt = render_agent_prompt(queue)
    assert "Queue Diagnostics" in prompt
    assert "refresh_chain_scan" in prompt
    assert "Next Fixes" in prompt
    assert "Review-Only Budget Ladder" in prompt


def test_queue_write_outputs_json_and_prompt():
    queue = _queue([_candidate()])
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        queue_path, prompt_path = write_outputs(queue, data_dir)
        saved = json.loads(queue_path.read_text(encoding="utf-8"))
        assert saved["schema"] == "optedge_robinhood_agentic_options_queue_v1"
        assert saved["orders"][0]["symbol"] == "AAPL"
        assert "Robinhood Agentic Options Queue" in prompt_path.read_text(encoding="utf-8")
        assert (data_dir / CYCLE_JSON).exists()
        assert (data_dir / CYCLE_PROMPT_MD).exists()


def test_cycle_packet_summarizes_open_positions_exits_and_validation():
    queue = _queue([_candidate()])
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        (data_dir / "open_positions.json").write_text(json.dumps([
            {
                "ticker": "AAPL",
                "side": "call",
                "strike": 200,
                "expiry": "2027-01-15",
                "entry_price": 0.75,
                "current_price": 1.1,
                "stop_price": 0.35,
                "target_price": 1.6,
                "latest_exit_pressure": 65,
                "latest_exit_action": "tighten_stop",
                "reprice_failed_count": 0,
            }
        ]), encoding="utf-8")
        (data_dir / "validation_summary.json").write_text(json.dumps({
            "generated_at": "2026-06-11T10:00:00+00:00",
            "closed_positions": 25,
            "open_positions": 1,
            "overall": {"win_rate": 0.40, "avg_return": 0.02, "profit_factor": 1.1, "max_drawdown": -0.15},
            "equity_curve": {
                "mode": "normalized_signal_allocation",
                "default_allocation_pct": 0.01,
                "description": "Drawdown uses normalized signal allocation.",
            },
            "warnings": ["Sample size is still small."],
        }), encoding="utf-8")
        (data_dir / "exit_reviews.jsonl").write_text(
            json.dumps({
                "timestamp": "2026-06-11T10:00:00+00:00",
                "asset": "option",
                "position_id": "AAPL|call|200|2027-01-15",
                "ticker": "AAPL",
                "action": "close_early",
                "exit_pressure": 82,
                "current_price": 1.1,
                "current_pnl_pct": 0.46,
            }) + "\n",
            encoding="utf-8",
        )

        packet = build_agentic_cycle_packet(queue, data_dir)
        assert packet["schema"] == "optedge_robinhood_agentic_cycle_v1"
        assert packet["auto_submit_allowed"] is False
        assert packet["queue_summary"]["diagnostics"]["label"] == "ready"
        assert packet["open_option_positions"]["count"] == 1
        assert packet["recent_option_exit_reviews"]["actionable_count"] == 1
        assert packet["validation"]["closed_positions"] == 25
        assert packet["validation"]["max_drawdown_mode"] == "normalized_signal_allocation"
        assert packet["validation"]["default_signal_allocation_pct"] == 0.01
        assert packet["entry_gate"]["status"] == "review_only"
        assert packet["entry_gate"]["new_entries_allowed_after_live_checks"] is False
        assert packet["entry_candidates"] == []
        assert packet["review_only_entry_candidates"][0]["symbol"] == "AAPL"
        assert packet["queue_summary"]["gated_ready_to_submit_count"] == 0
        assert "Sample size is still small." in packet["auto_submit_blockers"]

        prompt = render_cycle_prompt(packet)
        assert "Optedge Robinhood Agentic Cycle Packet" in prompt
        assert "Entry Gate" in prompt
        assert "Review-Only Entry Candidates" in prompt
        assert "No fresh entry candidate is submit-eligible" in prompt
        assert "Actionable Exit Reviews" in prompt
        assert "SELL_TO_CLOSE" in prompt
        assert "Max drawdown mode: normalized_signal_allocation" in prompt
        assert "Default signal allocation: 0.01" in prompt


def test_cycle_packet_blocks_entries_on_bad_validation_but_keeps_review_context():
    queue = _queue([_candidate()])
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        (data_dir / "validation_summary.json").write_text(json.dumps({
            "closed_positions": 120,
            "overall": {
                "win_rate": 0.18,
                "avg_return": -0.04,
                "profit_factor": 0.72,
                "max_drawdown": -0.35,
                "max_drawdown_mode": "normalized_signal_allocation",
            },
            "equity_curve": {
                "mode": "normalized_signal_allocation",
                "default_allocation_pct": 0.01,
                "description": "Drawdown uses normalized signal allocation.",
            },
            "warnings": ["Max drawdown is worse than -20%: -35.0%."],
        }), encoding="utf-8")

        packet = build_agentic_cycle_packet(queue, data_dir)
        assert packet["entry_gate"]["status"] == "blocked"
        assert packet["entry_gate"]["new_entries_allowed_after_live_checks"] is False
        assert packet["entry_candidates"] == []
        assert packet["review_only_entry_candidates"][0]["symbol"] == "AAPL"
        assert packet["queue_summary"]["gated_ready_to_submit_count"] == 0
        assert packet["queue_summary"]["review_only_entry_candidate_count"] == 1
        assert any("drawdown" in reason.lower() for reason in packet["entry_gate"]["blockers"])
        assert any("normalized_signal_allocation" in reason for reason in packet["entry_gate"]["blockers"])
        assert any("win rate" in reason.lower() for reason in packet["entry_gate"]["blockers"])

        prompt = render_cycle_prompt(packet)
        assert "Fresh entries blocked" in prompt
        assert "These are context only. Do not submit" in prompt
        assert "Max drawdown mode: normalized_signal_allocation" in prompt
        assert "Equity curve note: Drawdown uses normalized signal allocation." in prompt


def test_agent_decision_log_normalizes_appends_and_feeds_cycle_prompt():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        row = normalize_agent_decision({
            "action": "SKIPPED",
            "ticker_or_symbol": "aapl",
            "contract": "AAPL 2027-01-15 CALL 200",
            "option_side": "CALL",
            "strike": 200,
            "expiry": "2027-01-15",
            "quantity": 1,
            "max_limit_price": 0.8,
            "reason": "entry gate blocked",
            "entry_gate_status": "blocked",
        }, generated_at="2026-06-11T10:05:00+00:00")
        assert row["decision"] == "skipped"
        assert row["symbol"] == "AAPL"
        assert row["option_side"] == "call"
        assert row["limit_price"] == 0.8

        path = append_agent_decision(row, data_dir=Path(td))
        assert path.name == DECISION_LOG_JSONL
        summary = decision_log_summary(Path(td))
        assert summary["exists"] is True
        assert summary["recent_count"] == 1
        assert summary["action_counts"] == {"skipped": 1}
        assert summary["latest"][0]["reason"] == "entry gate blocked"

        packet = build_agentic_cycle_packet(_queue([_candidate()]), Path(td))
        assert packet["decision_log"]["recent_count"] == 1
        assert packet["files"]["decision_log"].endswith(DECISION_LOG_JSONL)
        prompt = render_cycle_prompt(packet)
        assert "Local Decision Journal" in prompt
        assert "entry gate blocked" in prompt


def test_cycle_packet_hard_pauses_on_kill_switch():
    queue = _queue([_candidate()])
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        (data_dir / "agentic_trading_disabled.flag").write_text("stop", encoding="utf-8")
        packet = build_agentic_cycle_packet(queue, data_dir)
        assert packet["hard_pause"] is True
        assert "kill-switch file is present" in packet["hard_pause_reasons"]


def test_build_robinhood_queue_can_refresh_chain_before_loading_candidates():
    old_refresh = rh_module.refresh_option_chain_shortlist
    old_build = rh_module.build_external_orders
    calls = {"refresh": 0, "build": 0}

    def fake_refresh(**kwargs):
        calls["refresh"] += 1
        assert kwargs["preset"] == "swing"
        assert kwargs["min_dte"] == 90
        assert kwargs["max_premium_per_order"] == 150.0
        assert kwargs["write"] is True
        return {
            "attempted": True,
            "ok": True,
            "applied_to_queue": True,
            "write": True,
            "preset": kwargs["preset"],
            "symbols_scanned": 1,
            "successful_scans": 1,
            "row_count": 1,
            "export": {"ok": True},
        }

    def fake_build_external_orders(**kwargs):
        calls["build"] += 1
        assert kwargs["dry_run"] is True
        assert kwargs["min_option_dte"] == 90
        assert kwargs["include_chain_shortlist"] is True
        return pd.DataFrame([_candidate(expiry="2026-12-18", contract="AAPL 2026-12-18 CALL 200")])

    try:
        rh_module.refresh_option_chain_shortlist = fake_refresh
        rh_module.build_external_orders = fake_build_external_orders
        with tempfile.TemporaryDirectory() as td:
            queue = rh_module.build_robinhood_queue(
                data_dir=Path(td),
                account_budget=500,
                min_dte=90,
                refresh_chain=True,
                chain_preset="swing",
                chain_refresh_write=True,
            )
        assert calls == {"refresh": 1, "build": 1}
        assert queue["chain_refresh"]["ok"] is True
        assert queue["orders"][0]["symbol"] == "AAPL"
    finally:
        rh_module.refresh_option_chain_shortlist = old_refresh
        rh_module.build_external_orders = old_build


def test_failed_chain_refresh_does_not_reuse_stale_shortlist():
    old_refresh = rh_module.refresh_option_chain_shortlist
    old_build = rh_module.build_external_orders

    def fake_refresh(**kwargs):
        return {
            "attempted": True,
            "ok": False,
            "applied_to_queue": False,
            "write": True,
            "preset": kwargs["preset"],
            "error": "no chain shortlist rows to export",
        }

    def fake_build_external_orders(**kwargs):
        assert kwargs["include_chain_shortlist"] is False
        return pd.DataFrame([])

    try:
        rh_module.refresh_option_chain_shortlist = fake_refresh
        rh_module.build_external_orders = fake_build_external_orders
        with tempfile.TemporaryDirectory() as td:
            queue = rh_module.build_robinhood_queue(
                data_dir=Path(td),
                account_budget=500,
                min_dte=90,
                refresh_chain=True,
                chain_preset="swing",
            )
        assert queue["orders"] == []
        assert queue["chain_refresh"]["applied_to_queue"] is False
        assert queue["diagnostics"]["source_row_count"] == 0
    finally:
        rh_module.refresh_option_chain_shortlist = old_refresh
        rh_module.build_external_orders = old_build


def test_build_robinhood_queue_loads_watchlist_sec_filing_risk():
    old_build = rh_module.build_external_orders

    def fake_build_external_orders(**kwargs):
        return pd.DataFrame([_candidate(ticker_or_symbol="AAPL")])

    try:
        rh_module.build_external_orders = fake_build_external_orders
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            (data_dir / "watchlist_sec_filings.json").write_text(json.dumps({
                "rows": [{
                    "ticker": "AAPL",
                    "form": "424B5",
                    "filing_date": "2026-06-10",
                    "days_old": 1,
                    "signal": "dilution_or_offering_watch",
                    "description": "prospectus supplement",
                }]
            }), encoding="utf-8")
            queue = build_robinhood_queue(data_dir=data_dir, min_dte=90)
        assert queue["orders"] == []
        assert queue["sec_offering_risks"]["AAPL"][0]["form"] == "424B5"
        assert queue["diagnostics"]["reason_groups"]["sec_offering_risk"] == 1
    finally:
        rh_module.build_external_orders = old_build


if __name__ == "__main__":
    test_queue_is_options_only()
    test_queue_accepts_ticker_fallback_and_writes_aliases()
    test_ready_queue_with_guarded_rejects_is_labeled_ready_guarded()
    test_queue_rejects_contracts_above_500_budget_caps()
    test_queue_blocks_bullish_calls_with_active_sec_offering_risk()
    test_queue_carries_public_cboe_activity_context()
    test_queue_caps_candidate_count_separately_from_order_count()
    test_queue_rejects_short_dated_options_by_default()
    test_queue_enforces_total_premium_cap_and_summarizes_rejections()
    test_queue_can_give_agent_more_candidates_than_order_cap()
    test_queue_prompt_requires_codex_double_check_and_limit_orders()
    test_queue_includes_recurring_cycle_and_management_rules()
    test_empty_queue_diagnostics_explain_stale_and_short_dte_rows()
    test_queue_write_outputs_json_and_prompt()
    test_cycle_packet_summarizes_open_positions_exits_and_validation()
    test_cycle_packet_blocks_entries_on_bad_validation_but_keeps_review_context()
    test_agent_decision_log_normalizes_appends_and_feeds_cycle_prompt()
    test_cycle_packet_hard_pauses_on_kill_switch()
    test_build_robinhood_queue_can_refresh_chain_before_loading_candidates()
    test_failed_chain_refresh_does_not_reuse_stale_shortlist()
    test_build_robinhood_queue_loads_watchlist_sec_filing_risk()
    print("21/21 robinhood agentic queue tests passed")
