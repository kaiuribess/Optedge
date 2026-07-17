# Purpose: Test cockpit workflows trust and manual broker gates.
import hashlib
import json
import os
import sys
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.local_cockpit as cockpit_module  # noqa: E402
from risk.account_drawdown import append_snapshot_observation  # noqa: E402
from scripts.export_robinhood_agentic_queue import robinhood_mcp_option_review_plan  # noqa: E402
from scripts.local_cockpit import (  # noqa: E402
    add_watchlist_queries,
    add_watchlist_query,
    apply_position_hygiene,
    artifact_path,
    build_action_queue,
    build_agentic_autopilot_status,
    build_agentic_decision_journal,
    build_best_setups,
    build_breadth_pulse,
    build_broker_reconciliation,
    build_cboe_option_activity,
    build_climate_gated_setups,
    build_command_center,
    build_dashboard_handoff,
    build_data_health,
    build_exit_review_summary,
    build_free_data_sources,
    build_lookup_history,
    build_macro_stress_pulse,
    build_market_pulse,
    build_opportunities,
    build_option_chain_batch,
    build_option_chain_scan,
    build_options_sentiment,
    build_paper_candidates,
    build_performance_summary,
    build_position_hygiene,
    build_positions,
    build_provider_status,
    build_risk_summary,
    build_robinhood_agentic_queue_report,
    build_saved_option_contracts,
    build_sector_pulse,
    build_summary,
    build_swing_climate,
    build_swing_packet,
    build_swing_scout,
    build_symbol_suggestions,
    build_today_review,
    build_trade_desk,
    build_trade_plan_report,
    build_watchlist_sec_filings,
    load_watchlist,
    normalize_robinhood_broker_snapshot_file,
    record_agentic_decision,
    remove_watchlist_entry,
    render_cockpit_html,
    run_watchlist_scans,
    warm_sec_ticker_cache,
    write_option_chain_shortlist,
    write_position_hygiene_plan,
)
from scripts.normalize_robinhood_broker_snapshot import (  # noqa: E402
    EQUITY_LEDGER_DIRNAME,
    account_equity_ledger_backup_path,
    account_equity_ledger_path,
    default_account_equity_ledger_dir,
    normalize_broker_snapshot,
)

TEST_ACCOUNT_KEY = "acct_0123456789abcdef"
PASSING_ACCOUNT_KEY = "acct_1111111111111111"
OTHER_ACCOUNT_KEY = "acct_2222222222222222"


def test_cockpit_summary_counts_open_positions():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        (data_dir / "open_positions.json").write_text(json.dumps([{"ticker": "AAPL"}]))
        (data_dir / "open_share_positions.json").write_text(json.dumps([{"ticker": "NVDA"}]))
        (data_dir / "open_futures_positions.json").write_text(
            json.dumps(
                [
                    {"symbol": "CL=F"},
                    {"symbol": "NG=F"},
                ]
            )
        )
        summary = build_summary(data_dir)
        assert summary["open_counts"] == {"options": 1, "shares": 1, "futures": 2}
        assert summary["total_open"] == 4
        assert summary["active_open_counts"] == {"options": 1, "shares": 1, "futures": 2}
        assert summary["active_total_open"] == 4


def test_cockpit_summary_separates_expired_records_from_active_exposure():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        expired = (datetime.now(UTC) - timedelta(days=2)).date().isoformat()
        expires_today = datetime.now(UTC).date().isoformat()
        active = (datetime.now(UTC) + timedelta(days=120)).date().isoformat()
        (data_dir / "open_positions.json").write_text(
            json.dumps(
                [
                    {"ticker": "AAPL", "expiry": expired},
                    {"ticker": "SPY", "expiry": expires_today},
                    {"ticker": "MSFT", "expiry": active},
                ]
            ),
            encoding="utf-8",
        )
        (data_dir / "open_share_positions.json").write_text(
            json.dumps([{"ticker": "NVDA"}]),
            encoding="utf-8",
        )
        (data_dir / "open_futures_positions.json").write_text(
            json.dumps([{"symbol": "CL=F"}]),
            encoding="utf-8",
        )

        summary = build_summary(data_dir)

        assert summary["open_counts"] == {"options": 3, "shares": 1, "futures": 1}
        assert summary["total_open"] == 5
        assert summary["active_open_counts"] == {"options": 2, "shares": 1, "futures": 1}
        assert summary["active_total_open"] == 4
        assert summary["expired_open_counts"] == {"options": 1, "shares": 0, "futures": 0}


def test_cockpit_artifact_path_finds_latest_dashboard():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        old = data_dir / "dashboard_20260101_000000.html"
        new = data_dir / "dashboard_20260102_000000.html"
        old.write_text("old")
        new.write_text("new")
        assert artifact_path("latest-dashboard", data_dir) == new


def test_lookup_history_reads_saved_reports():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        report_dir = data_dir / "lookup_reports"
        report_dir.mkdir(parents=True)
        (report_dir / "lookup_AAPL_20260627_120000_000000.html").write_text(
            "<html>AAPL</html>",
            encoding="utf-8",
        )
        (data_dir / "lookup_history.jsonl").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "query": "AAPL 20270115 C 220",
                    "lookup_symbol": "AAPL",
                    "total_hits": 3,
                    "research_label": "Paper candidate review",
                    "research_action": "paper_candidate_review",
                    "research_route": "paper",
                    "risk_level": "medium",
                    "can_export_paper_candidate": True,
                    "chain_symbol": "AAPL",
                    "chain_side": "call",
                    "chain_min_dte": 180,
                    "chain_max_dte": 900,
                    "swing_label": "Selective swing review",
                    "swing_score": 72,
                    "contract_pick": "Alternative looks cleaner",
                    "contract_winner": "alternative",
                    "archive_html_path": "lookup_reports/lookup_AAPL_20260627_120000_000000.html",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        history = build_lookup_history(data_dir)

        assert history["count"] == 1
        row = history["rows"][0]
        assert row["query"] == "AAPL 20270115 C 220"
        assert row["lookup_symbol"] == "AAPL"
        assert row["report"].startswith("/lookup-report?file=lookup_reports")
        assert row["contract_winner"] == "alternative"
        assert row["can_export_paper_candidate"] is True
        assert row["chain_symbol"] == "AAPL"
        assert row["chain_side"] == "call"
        assert row["chain_min_dte"] == 180
        assert row["follow_status"] == "no_baseline"
        assert row["follow_direction"] == "bullish"
        assert row["review_age_label"] == "fresh"
        assert history["summary"]["total_saved"] == 1
        assert history["summary"]["priced_count"] == 0
        assert history["summary"]["paper_eligible_count"] == 1
        assert history["summary"]["chain_ready_count"] == 1
        assert history["summary"]["no_baseline_count"] == 1


def test_lookup_history_surfaces_stale_refresh_leaderboard():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        stale_time = datetime.now(UTC) - timedelta(days=30)
        (data_dir / "lookup_history.jsonl").write_text(
            json.dumps(
                {
                    "generated_at": stale_time.isoformat(),
                    "query": "MSFT 20270115 C 400",
                    "lookup_symbol": "MSFT",
                    "research_label": "Paper candidate review",
                    "research_action": "paper_candidate_review",
                    "research_route": "paper",
                    "risk_level": "medium",
                    "can_export_paper_candidate": True,
                    "chain_symbol": "MSFT",
                    "chain_side": "call",
                    "chain_min_dte": 90,
                    "chain_max_dte": 900,
                    "swing_score": 78,
                }
            )
            + "\n",
            encoding="utf-8",
        )

        history = build_lookup_history(data_dir)

    row = history["rows"][0]
    assert row["review_age_label"] == "stale"
    assert row["review_stale"] is True
    summary = history["summary"]
    assert summary["stale_review_count"] == 1
    assert len(summary["leaderboard_needs_refresh"]) == 1
    refresh = summary["leaderboard_needs_refresh"][0]
    assert refresh["symbol"] == "MSFT"
    assert refresh["can_export_paper_candidate"] is True
    assert refresh["chain_side"] == "call"


def test_lookup_history_computes_followup_return_from_free_history():
    old_history = cockpit_module.data_provider.get_history
    try:

        def fake_history(symbol, period="6mo", interval="1d", cache_age=1800):
            assert symbol == "AAPL"
            assert period == "6mo"
            assert interval == "1d"
            assert cache_age == 1800
            idx = pd.date_range("2026-06-20", periods=2, freq="D", tz="UTC")
            df = pd.DataFrame({"Close": [100.0, 110.0]}, index=idx)
            df.attrs["history_source"] = "unit_history"
            return df

        cockpit_module.data_provider.get_history = fake_history
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            (data_dir / "lookup_history.jsonl").write_text(
                json.dumps(
                    {
                        "generated_at": datetime.now(UTC).isoformat(),
                        "query": "AAPL",
                        "lookup_symbol": "AAPL",
                        "lookup_price": 100.0,
                        "lookup_price_date": "2026-06-20",
                        "lookup_price_source": "unit_history",
                        "research_label": "Paper candidate review",
                        "swing_label": "Selective swing review",
                        "archive_html_path": "lookup_AAPL.html",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            history = build_lookup_history(data_dir)
    finally:
        cockpit_module.data_provider.get_history = old_history

    row = history["rows"][0]
    assert row["follow_status"] == "strong_green"
    assert row["follow_return_pct"] == 0.1
    assert row["follow_underlying_return_pct"] == 0.1
    assert row["follow_direction"] == "raw"
    assert row["follow_price"] == 110.0
    assert row["follow_source"] == "unit_history"
    summary = history["summary"]
    assert summary["priced_count"] == 1
    assert summary["green_count"] == 1
    assert summary["green_rate"] == 1.0
    assert summary["avg_follow_return_pct"] == 0.1
    assert summary["best"]["symbol"] == "AAPL"
    assert summary["worst"]["symbol"] == "AAPL"
    assert summary["by_direction"][0]["group"] == "raw"
    assert summary["by_direction"][0]["avg_thesis_return"] == 0.1
    assert summary["by_action"][0]["group"] == "Paper candidate review"
    assert summary["by_action"][0]["green_rate"] == 1.0
    assert summary["leaderboard_best"][0]["symbol"] == "AAPL"
    assert summary["leaderboard_best"][0]["thesis_return"] == 0.1
    assert summary["leaderboard_best"][0]["can_export_paper_candidate"] is False
    assert summary["leaderboard_best"][0]["review_age"] == "fresh"
    assert summary["leaderboard_worst"][0]["symbol"] == "AAPL"


def test_lookup_history_scores_puts_by_bearish_thesis():
    old_history = cockpit_module.data_provider.get_history
    try:

        def fake_history(symbol, period="6mo", interval="1d", cache_age=1800):
            assert symbol == "AAPL"
            idx = pd.date_range("2026-06-20", periods=2, freq="D", tz="UTC")
            return pd.DataFrame({"Close": [100.0, 90.0]}, index=idx)

        cockpit_module.data_provider.get_history = fake_history
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            (data_dir / "lookup_history.jsonl").write_text(
                json.dumps(
                    {
                        "generated_at": datetime.now(UTC).isoformat(),
                        "query": "AAPL 20270115 P 220",
                        "lookup_symbol": "AAPL",
                        "lookup_price": 100.0,
                        "chain_side": "put",
                        "research_label": "Paper candidate review",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            history = build_lookup_history(data_dir)
    finally:
        cockpit_module.data_provider.get_history = old_history

    row = history["rows"][0]
    assert row["follow_direction"] == "bearish"
    assert row["follow_underlying_return_pct"] == -0.1
    assert row["follow_return_pct"] == 0.1
    assert row["follow_status"] == "strong_green"
    assert history["summary"]["green_count"] == 1
    assert history["summary"]["by_direction"][0]["group"] == "bearish"
    assert history["summary"]["by_direction"][0]["best_symbol"] == "AAPL"


def _share_plan_payload():
    return {
        "symbol": "AAPL",
        "asset": "share",
        "direction": "long",
        "account_equity": 10_000,
        "risk_pct": 1,
        "allocation_pct": 10,
        "slippage_pct": 0.5,
        "entry_price": 100,
        "stop_price": 95,
        "target_price": 112,
        "candidate_fingerprint": "1" * 24,
        "candidate_source_file": "top_shares_20260713_120000.parquet",
        "candidate_source_generated_at": None,
    }


def _manual_gate_account(
    *,
    label="Agentic",
    equity=10_000,
    buying_power=1_000,
    active=True,
    agentic_allowed=True,
    options_ready=True,
    account_key=None,
):
    account_key = account_key or f"acct_{hashlib.sha256(str(label).encode()).hexdigest()[:16]}"
    return {
        "account": label,
        "account_mask": "...0001",
        "account_key": account_key,
        "state": "active" if active else "deactivated",
        "active": active,
        "agentic_allowed": agentic_allowed,
        "option_level": "option_level_2" if options_ready else "",
        "options_ready": options_ready,
        "buying_power": buying_power,
        "account_equity": equity,
        "funded": buying_power is not None and buying_power > 0,
        "status": "ready"
        if active and agentic_allowed and options_ready and buying_power
        else "not_ready",
    }


def _manual_gate_broker(account_rows, *, option_ready=True):
    return {
        "snapshot_exists": True,
        "snapshot_age_minutes": 1.0,
        "snapshot_schema": "optedge_robinhood_broker_snapshot_v1",
        "raw_bundle_schema": "optedge_robinhood_mcp_read_bundle_v2",
        "execution_capture_ready": True,
        "status": "synced",
        "warnings": [],
        "account_readiness_rows": account_rows,
        "agentic_readiness_status": "ready" if option_ready else "missing_ready_account",
        "agentic_readiness_detail": "A single active account must meet every review constraint.",
    }


def _manual_gate_share_plan(
    *,
    assumed_equity=9_000,
    risk_fraction=0.01,
    allocation_fraction=0.10,
    planned_stop_loss=50,
    notional=900,
):
    stop_distance = planned_stop_loss / 9
    return {
        "direction": "long",
        "order": {
            "symbol": "AAPL",
            "direction": "long",
            "intent": "open_long",
            "side": "buy",
            "quantity": 9,
            "limit_price": 100,
            "stop_price": round(100 - stop_distance, 6),
            "target_price": round(100 + 2 * stop_distance, 6),
            "estimated_notional_dollars": notional,
        },
        "risk": {
            "planned_stop_loss_dollars": planned_stop_loss,
            "full_share_notional_at_risk_dollars": notional,
        },
        "account_assumptions": {
            "account_equity_dollars": assumed_equity,
            "risk_fraction": risk_fraction,
            "allocation_fraction": allocation_fraction,
        },
    }


def _manual_gate_option_plan(*, assumed_equity=10_000, debit=100):
    return {
        "direction": "long",
        "order": {
            "symbol": "AAPL",
            "option_type": "call",
            "underlying_type": "equity",
            "strike": 200,
            "expiry": "2027-01-15",
            "quantity": 1,
            "limit_price": 1.0,
            "estimated_debit_dollars": debit,
        },
        "risk": {"full_option_debit_at_risk_dollars": debit},
        "account_assumptions": {
            "account_equity_dollars": assumed_equity,
            "risk_fraction": 0.01,
            "allocation_fraction": 0.10,
        },
    }


def _manual_gate_option_candidate(**updates):
    expiry = "2027-01-15"
    candidate = {
        "asset": "option",
        "symbol": "AAPL",
        "action": "BUY_TO_OPEN",
        "order_type": "limit",
        "time_in_force": "day",
        "option_side": "call",
        "strike": 200,
        "expiry": expiry,
        "dte": (datetime.fromisoformat(expiry).date() - datetime.now(UTC).date()).days,
        "quantity": 1,
        "max_limit_price": 1.0,
        "source_quote_at": datetime.now(UTC).isoformat(),
        "source_quote_time_basis": "provider_quote_timestamp",
        "quote_quality": "live_or_broker",
        "data_delay": "real_time",
        "source_bid": 0.99,
        "source_ask": 1.01,
        "max_allowed_spread_pct": 0.12,
        "underlying_type": "equity",
    }
    candidate.update(updates)
    return candidate


def _portfolio_review_constraints(
    *,
    asset="share",
    assumed_equity=10_000,
    live_equity=10_000,
    allocation_fraction=0.10,
    current_capital=0,
    proposed_capital=1_000,
):
    now = datetime.now(UTC)
    basis = min(assumed_equity, live_equity)
    cap = round(basis * allocation_fraction, 2)
    post_trade = round(current_capital + proposed_capital, 2)
    return {
        "schema": "optedge_portfolio_review_constraints_v1",
        "source": "optedge_robinhood_broker_snapshot_v1",
        "raw_bundle_schema": "optedge_robinhood_mcp_read_bundle_v2",
        "broker_snapshot_generated_at": now.isoformat(),
        "broker_snapshot_digest_sha256": "b" * 64,
        "same_account_only": True,
        "local_research_counted_as_live": False,
        "nonterminal_order_policy": "block",
        "cap_method": "min_assumed_and_live_same_account_equity_times_allocation_fraction",
        "proposed_capital_basis": (
            "full_option_debit_at_risk_dollars"
            if asset == "option"
            else "full_share_notional_at_risk_dollars"
        ),
        "eligible_account_count": 1,
        "eligible_accounts": [
            {
                "schema": "optedge_post_trade_portfolio_gate_v1",
                "status": "allowed",
                "allowed": True,
                "account_key": TEST_ACCOUNT_KEY,
                "account_mask": "...0001",
                "asof": now.date().isoformat(),
                "exposure_schema": "optedge_broker_portfolio_exposure_v1",
                "position_count": 0,
                "same_account_nonterminal_order_count": 0,
                "equity_basis_method": "min_assumed_and_live_same_account_equity",
                "assumed_equity_dollars": assumed_equity,
                "live_equity_dollars": live_equity,
                "equity_basis_dollars": basis,
                "allocation_fraction": allocation_fraction,
                "allocation_cap_dollars": cap,
                "current_capital_at_risk_dollars": current_capital,
                "proposed_capital_at_risk_dollars": proposed_capital,
                "post_trade_capital_at_risk_dollars": post_trade,
                "headroom_before_trade_dollars": round(cap - current_capital, 2),
                "headroom_after_trade_dollars": round(cap - post_trade, 2),
                "utilization_before": round(current_capital / cap, 6),
                "utilization_after": round(post_trade / cap, 6),
                "blockers": [],
            }
        ],
    }


def _drawdown_review_constraints(
    *,
    equity=10_000,
    risk_fraction=0.01,
    account_key=TEST_ACCOUNT_KEY,
):
    now = datetime.now(UTC)
    return {
        "schema": "optedge_account_drawdown_review_constraints_v1",
        "policy_version": "robinhood_account_drawdown_v2",
        "status": "allowed",
        "allowed": True,
        "missing_or_unsafe_state_policy": "block_new_entries",
        "broker_snapshot_digest_sha256": "b" * 64,
        "source_snapshot_digest_sha256": "c" * 64,
        "base_risk_fraction": 0.01,
        "requested_risk_fraction": risk_fraction,
        "eligible_account_count": 1,
        "eligible_accounts": [
            {
                "schema": "optedge_robinhood_account_drawdown_interlock_v1",
                "policy_version": "robinhood_account_drawdown_v2",
                "status": "ready",
                "review_ready": True,
                "allowed": True,
                "account_key": account_key,
                "account_mask": "...0001",
                "asof": now.isoformat(),
                "observation_count": 2,
                "baseline_started_at": (now - timedelta(hours=24)).isoformat(),
                "baseline_span_hours": 24.0,
                "baseline_ny_calendar_date_count": 2,
                "current_equity_dollars": equity,
                "high_water_equity_dollars": equity,
                "high_water_drawdown_fraction": 0.0,
                "ny_session_date": now.date().isoformat(),
                "ny_session_reference_equity_dollars": equity,
                "ny_session_loss_fraction": 0.0,
                "risk_multiplier": 1.0,
                "max_allowed_risk_fraction": 0.01,
                "source_snapshot_digest_sha256": "c" * 64,
                "ledger_digest_sha256": "d" * 64,
                "blockers": [],
                "policy": {
                    "max_observation_age_minutes": 90.0,
                    "minimum_baseline_observations": 2,
                    "minimum_baseline_ny_calendar_dates": 2,
                    "minimum_baseline_span_hours": 18.0,
                    "half_risk_at_drawdown_fraction": 0.05,
                    "quarter_risk_at_drawdown_fraction": 0.08,
                    "block_at_drawdown_fraction": 0.10,
                    "block_at_ny_session_loss_fraction": -0.03,
                    "block_at_unexplained_adjacent_jump_fraction": 0.25,
                    "missing_or_unsafe_state_policy": "block_new_entries",
                    "risk_multiplier_may_increase_risk": False,
                },
                "does_not_place_orders": True,
            }
        ],
    }


def _share_candidate_review_constraints(plan):
    now = datetime.now(UTC)
    order = plan["order"]
    return {
        "schema": "optedge_share_candidate_review_attestation_v1",
        "status": "allowed",
        "allowed": True,
        "asset": "share",
        "direction": "long",
        "symbol": order["symbol"],
        "source_pattern": "top_shares_*.parquet",
        "source_file": "top_shares_20260713_120000.parquet",
        "source_artifact_at": now.isoformat(),
        "source_artifact_age_minutes": 0.0,
        "max_source_age_minutes": 45.0,
        "source_artifact_digest_sha256": "d" * 64,
        "candidate_row_digest_sha256": "f" * 64,
        "candidate_fingerprint": "1" * 24,
        "candidate_source_generated_at": None,
        "candidate_source_price_session": now.date().isoformat(),
        "candidate_source_price_basis": "history_last_bar_close",
        "candidate_source_quote_at": None,
        "candidate_quote_available": False,
        "candidate_source_quote_time_basis": None,
        "candidate_source_bid": None,
        "candidate_source_ask": None,
        "candidate_source_spread_fraction": None,
        "candidate_quote_quality": None,
        "trade_status": "Trade",
        "setup_gate_status": "ready",
        "research_guard_status": "pass",
        "entry_price": order["limit_price"],
        "stop_price": order["stop_price"],
        "target_price": order["target_price"],
        "max_units": order["quantity"],
        "max_notional_dollars": order["estimated_notional_dollars"],
        "planned_quantity": order["quantity"],
        "planned_notional_dollars": order["estimated_notional_dollars"],
        "top_rank_limit": 3,
        "require_exact_geometry": True,
        "require_loaded_candidate_fingerprint": True,
        "blockers": [],
    }


def _write_test_equity_ledgers(
    data_dir,
    snapshot,
    *,
    initial_equity_multiplier=1.0,
):
    """Seed a durable multi-date baseline and its rollback-detection sidecar."""
    generated = datetime.fromisoformat(str(snapshot["generated_at"]).replace("Z", "+00:00"))
    initial = json.loads(json.dumps(snapshot))
    initial["generated_at"] = (generated - timedelta(days=2)).isoformat()
    for account in initial.get("accounts") or []:
        portfolio = account.get("portfolio") if isinstance(account, dict) else None
        if isinstance(portfolio, dict) and isinstance(portfolio.get("total_value"), (int, float)):
            portfolio["total_value"] = round(
                float(portfolio["total_value"]) * initial_equity_multiplier,
                2,
            )
    for account in snapshot.get("accounts") or []:
        if not isinstance(account, dict) or not account.get("account_key"):
            continue
        try:
            ledger, _ = append_snapshot_observation(
                None,
                initial,
                account["account_key"],
            )
            prior_close = json.loads(json.dumps(snapshot))
            prior_close["generated_at"] = (generated - timedelta(days=1)).isoformat()
            ledger, _ = append_snapshot_observation(
                ledger,
                prior_close,
                account["account_key"],
            )
            ledger, _ = append_snapshot_observation(
                ledger,
                snapshot,
                account["account_key"],
            )
        except ValueError:
            continue
        path = account_equity_ledger_path(
            default_account_equity_ledger_dir(data_dir),
            account["account_key"],
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ledger), encoding="utf-8")
        account_equity_ledger_backup_path(path).write_text(json.dumps(ledger), encoding="utf-8")


def _run_manual_gate(
    asset,
    plan,
    broker,
    *,
    candidate=None,
    snapshot_updates=None,
    entry_gate_allowed=True,
    seed_equity_ledger=True,
    initial_equity_multiplier=1.0,
    local_option_positions=None,
    local_share_positions=None,
    seed_share_candidate=True,
    share_candidate_updates=None,
    candidate_request_updates=None,
    share_candidate_age_minutes=0,
    cycle_candidates=None,
    queue_candidates=None,
    cycle_updates=None,
    queue_updates=None,
):
    old_health = cockpit_module.build_data_health
    old_broker = cockpit_module.build_broker_reconciliation
    old_edge_lab = cockpit_module.build_edge_lab_report
    try:
        cockpit_module.build_data_health = lambda data_dir: {
            "status": "ok",
            "validation_guardrail": {"level": "ok", "detail": "Validated."},
        }
        cockpit_module.build_broker_reconciliation = lambda data_dir, snapshot_override=None: broker
        profile = str(plan.get("execution_profile") or "swing_execution")
        cockpit_module.build_edge_lab_report = lambda data_dir: {
            "schema": "optedge_edge_lab_v1",
            "status": "validated",
            "live_capital_eligible": True,
            "headline_horizon_sessions": 10,
            "asset_rows": [
                {
                    "asset": asset,
                    "status": "validated",
                    "live_capital_eligible": True,
                    "evidence_lane": "current_method_executable",
                    "primary_blocker": None,
                }
            ],
            "leaps_swing": {
                "schema": "optedge_leaps_swing_evidence_v1",
                "profile": "leaps_swing",
                "evidence_lane": "option_leaps_swing",
                "status": "validated" if profile == "leaps_swing" else "paper_only",
                "live_capital_eligible": profile == "leaps_swing",
                "primary_blocker": None if profile == "leaps_swing" else "Not selected",
                "required_horizons_sessions": [5, 10, 20],
            },
        }
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            now = datetime.now(UTC).isoformat()
            snapshot = {
                "schema": "optedge_robinhood_broker_snapshot_v1",
                "generated_at": now,
                "normalized_at": now,
                "normalization_blockers": [],
                "accounts": [
                    {
                        "account_key": row.get("account_key"),
                        "portfolio": {"total_value": row.get("account_equity")},
                    }
                    for row in (broker.get("account_readiness_rows") or [])
                    if isinstance(row, dict) and row.get("account_key")
                ],
                "equity_positions": [],
                "option_positions": [],
                "equity_orders": [],
                "option_orders": [],
            }
            snapshot.update(snapshot_updates or {})
            (data_dir / "robinhood_broker_snapshot.json").write_text(
                json.dumps(snapshot),
                encoding="utf-8",
            )
            if seed_equity_ledger:
                _write_test_equity_ledgers(
                    data_dir,
                    snapshot,
                    initial_equity_multiplier=initial_equity_multiplier,
                )
            (data_dir / "open_positions.json").write_text(
                json.dumps(local_option_positions or []), encoding="utf-8"
            )
            (data_dir / "open_share_positions.json").write_text(
                json.dumps(local_share_positions or []), encoding="utf-8"
            )
            if asset == "share" and seed_share_candidate:
                order = plan.get("order") if isinstance(plan.get("order"), dict) else {}
                candidate_row = {
                    "ticker": order.get("symbol") or "AAPL",
                    "entry_price": order.get("limit_price"),
                    "stop_price": order.get("stop_price"),
                    "target_price": order.get("target_price"),
                    "source_price_session": datetime.now(UTC).date().isoformat(),
                    "source_price_basis": "history_last_bar_close",
                    "confidence": 80,
                    "rank_score": 1.0,
                    "suggested_dollars": max(
                        100_000.0,
                        float(order.get("estimated_notional_dollars") or 0.0),
                    ),
                    "stop_pct": -0.04,
                    "target_pct": 0.08,
                    "trade_status": "Trade",
                    "is_actionable": True,
                    "research_guard_status": "pass",
                }
                candidate_row.update(share_candidate_updates or {})
                share_path = data_dir / "top_shares_20260713_120000.parquet"
                pd.DataFrame([candidate_row]).to_parquet(share_path)
                if share_candidate_age_minutes:
                    stale_time = datetime.now(UTC).timestamp() - share_candidate_age_minutes * 60
                    os.utime(share_path, (stale_time, stale_time))
                loaded = cockpit_module._read_parquet(share_path)
                record = cockpit_module._best_setup_record(loaded.iloc[0], "share", share_path.name)
                planner_candidate = record.get("planner_candidate") or {}
                plan["candidate_request"] = {
                    "candidate_fingerprint": planner_candidate.get("candidate_fingerprint"),
                    "source_file": share_path.name,
                    "source_generated_at": planner_candidate.get("source_generated_at"),
                }
            if asset == "share" and candidate_request_updates:
                plan.setdefault("candidate_request", {}).update(candidate_request_updates)
            if asset == "option":
                candidate_row = (
                    candidate if candidate is not None else _manual_gate_option_candidate()
                )
                cycle_rows = (
                    cycle_candidates
                    if cycle_candidates is not None
                    else [json.loads(json.dumps(candidate_row))]
                )
                queue_rows = (
                    queue_candidates
                    if queue_candidates is not None
                    else [json.loads(json.dumps(candidate_row))]
                )
                queue_payload = {
                    "schema": "optedge_robinhood_agentic_options_queue_v1",
                    "generated_at": now,
                    "does_not_place_orders": True,
                    "execution_enabled": False,
                    "max_orders_to_submit": 0,
                    "orders": queue_rows,
                }
                queue_payload.update(queue_updates or {})
                (data_dir / "robinhood_agentic_queue.json").write_text(
                    json.dumps(queue_payload),
                    encoding="utf-8",
                )
                cycle_payload = {
                    "schema": "optedge_robinhood_agentic_cycle_v1",
                    "generated_at": now,
                    "does_not_place_orders": True,
                    "auto_submit_allowed": False,
                    "entry_gate": {
                        "new_entries_allowed_after_live_checks": entry_gate_allowed,
                    },
                    "manual_review_candidates": cycle_rows,
                }
                cycle_payload.update(cycle_updates or {})
                (data_dir / "robinhood_agentic_cycle.json").write_text(
                    json.dumps(cycle_payload), encoding="utf-8"
                )
            return cockpit_module._manual_review_gate(asset, data_dir, plan)
    finally:
        cockpit_module.build_data_health = old_health
        cockpit_module.build_broker_reconciliation = old_broker
        cockpit_module.build_edge_lab_report = old_edge_lab


def test_manual_review_gate_requires_the_matching_asset_edge_lane():
    broker = _manual_gate_broker(
        [
            _manual_gate_account(equity=10_000, buying_power=2_000),
        ]
    )
    old_health = cockpit_module.build_data_health
    old_broker = cockpit_module.build_broker_reconciliation
    old_edge_lab = cockpit_module.build_edge_lab_report
    try:
        cockpit_module.build_data_health = lambda data_dir: {
            "status": "ok",
            "validation_guardrail": {"level": "ok", "detail": "Validated."},
        }
        cockpit_module.build_broker_reconciliation = lambda data_dir, snapshot_override=None: broker
        cockpit_module.build_edge_lab_report = lambda data_dir: {
            "status": "validated",
            "live_capital_eligible": True,
            "headline_horizon_sessions": 10,
            "asset_rows": [
                {
                    "asset": "share",
                    "status": "validated",
                    "live_capital_eligible": True,
                    "primary_blocker": None,
                },
                {
                    "asset": "option",
                    "status": "insufficient",
                    "live_capital_eligible": False,
                    "primary_blocker": "Broker-observed option outcome coverage",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            now = datetime.now(UTC).isoformat()
            (data_dir / "robinhood_broker_snapshot.json").write_text(
                json.dumps(
                    {
                        "generated_at": now,
                        "accounts": [],
                        "equity_positions": [],
                        "option_positions": [],
                        "equity_orders": [],
                        "option_orders": [],
                    }
                ),
                encoding="utf-8",
            )
            (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")
            (data_dir / "open_share_positions.json").write_text("[]", encoding="utf-8")
            (data_dir / "robinhood_agentic_queue.json").write_text(
                json.dumps({"generated_at": now}),
                encoding="utf-8",
            )
            (data_dir / "robinhood_agentic_cycle.json").write_text(
                json.dumps(
                    {
                        "generated_at": now,
                        "entry_gate": {"new_entries_allowed_after_live_checks": True},
                        "manual_review_candidates": [_manual_gate_option_candidate()],
                    }
                ),
                encoding="utf-8",
            )
            gate = cockpit_module._manual_review_gate(
                "option",
                data_dir,
                _manual_gate_option_plan(),
            )
    finally:
        cockpit_module.build_data_health = old_health
        cockpit_module.build_broker_reconciliation = old_broker
        cockpit_module.build_edge_lab_report = old_edge_lab

    assert gate["review_allowed"] is False
    assert gate["edge_live_capital_eligible"] is False
    assert gate["review_constraints"]["evidence"]["asset"] == "option"
    assert any("Option Edge Lab lane is not validated" in value for value in gate["blockers"])


def test_manual_review_gate_allows_conservative_user_equity_when_live_math_passes():
    broker = _manual_gate_broker(
        [
            _manual_gate_account(equity=10_000, buying_power=1_000),
        ]
    )

    gate = _run_manual_gate("share", _manual_gate_share_plan(assumed_equity=9_000), broker)

    assert gate["review_allowed"] is True
    assert gate["blockers"] == []
    assert gate["review_constraints"]["account"]["eligible_same_account_match_count"] == 1
    derivation = gate["review_constraints"]["account"]["account_key_derivation"]
    assert derivation["schema"] == "optedge_robinhood_account_key_derivation_v1"
    assert derivation["namespace"] == "optedge-robinhood-account-v1|"
    assert derivation["require_exact_eligible_key_match"] is True
    assert derivation["persist_raw_account_number"] is False
    assert (
        gate["review_constraints"]["drawdown"]["eligible_accounts"][0]["account_mask"] == "...0001"
    )


def test_share_review_binds_realistic_candidate_without_pretending_bar_data_is_quote():
    gate = _run_manual_gate(
        "share",
        _manual_gate_share_plan(),
        _manual_gate_broker([_manual_gate_account(equity=10_000, buying_power=2_000)]),
    )

    candidate = gate["review_constraints"]["candidate"]
    assert gate["review_allowed"] is True
    assert candidate["allowed"] is True
    assert candidate["candidate_quote_available"] is False
    assert candidate["candidate_source_price_basis"] == "history_last_bar_close"
    assert len(candidate["candidate_fingerprint"]) == 24
    assert len(candidate["source_artifact_digest_sha256"]) == 64


def test_share_review_blocks_missing_stale_or_mismatched_exact_candidate():
    broker = _manual_gate_broker(
        [
            _manual_gate_account(equity=10_000, buying_power=2_000),
        ]
    )
    cases = [
        ({"seed_share_candidate": False}, "top_shares artifact is missing"),
        ({"share_candidate_age_minutes": 46}, "older than the 45-minute review limit"),
        (
            {"candidate_request_updates": {"candidate_fingerprint": "f" * 24}},
            "symbol and fingerprint are not among",
        ),
        (
            {"share_candidate_updates": {"source_price_basis": "live_quote"}},
            "history_last_bar_close",
        ),
        (
            {"share_candidate_updates": {"source_price_session": "2020-01-01"}},
            "no more than four calendar days old",
        ),
    ]
    for kwargs, expected in cases:
        gate = _run_manual_gate(
            "share",
            _manual_gate_share_plan(),
            broker,
            **kwargs,
        )
        assert gate["review_allowed"] is False
        assert any(expected in blocker for blocker in gate["blockers"])


def test_manual_review_gate_blocks_without_chained_account_equity_history():
    broker = _manual_gate_broker(
        [
            _manual_gate_account(equity=10_000, buying_power=1_000),
        ]
    )

    gate = _run_manual_gate(
        "share",
        _manual_gate_share_plan(assumed_equity=9_000),
        broker,
        seed_equity_ledger=False,
    )

    assert gate["review_allowed"] is False
    assert gate["review_constraints"]["drawdown"]["eligible_account_count"] == 0
    assert any("account-equity history" in blocker for blocker in gate["blockers"])


def test_manual_review_gate_applies_drawdown_reduced_risk_ceiling():
    broker = _manual_gate_broker(
        [
            _manual_gate_account(equity=10_000, buying_power=1_000),
        ]
    )
    reduced_plan = _manual_gate_share_plan(
        assumed_equity=10_000,
        risk_fraction=0.005,
        planned_stop_loss=50,
    )

    reduced = _run_manual_gate(
        "share",
        reduced_plan,
        broker,
        initial_equity_multiplier=1 / 0.95,
    )
    too_large = _run_manual_gate(
        "share",
        _manual_gate_share_plan(
            assumed_equity=10_000,
            risk_fraction=0.01,
            planned_stop_loss=50,
        ),
        broker,
        initial_equity_multiplier=1 / 0.95,
    )

    assert reduced["review_allowed"] is True
    attestation = reduced["review_constraints"]["drawdown"]["eligible_accounts"][0]
    assert attestation["status"] == "reduced"
    assert attestation["risk_multiplier"] == 0.5
    assert attestation["max_allowed_risk_fraction"] == 0.005
    assert too_large["review_allowed"] is False
    assert any("drawdown multiplier" in blocker for blocker in too_large["blockers"])


def test_manual_review_gate_blocks_at_ten_percent_account_drawdown():
    gate = _run_manual_gate(
        "share",
        _manual_gate_share_plan(
            assumed_equity=10_000,
            risk_fraction=0.0025,
            planned_stop_loss=25,
        ),
        _manual_gate_broker(
            [
                _manual_gate_account(equity=10_000, buying_power=1_000),
            ]
        ),
        initial_equity_multiplier=1 / 0.89,
    )

    assert gate["review_allowed"] is False
    assert any("at least 10%" in blocker for blocker in gate["blockers"])


def test_manual_review_gate_attests_existing_plus_proposed_same_account_exposure():
    account = _manual_gate_account(equity=10_000, buying_power=2_000)
    broker = _manual_gate_broker([account])
    existing_position = {
        "account_key": account["account_key"],
        "symbol": "MSFT",
        "quantity": 5,
        "signed_quantity": 5,
        "position_type": "long",
        "market_value": 500,
        "current_price": 100,
    }
    plan = _manual_gate_share_plan(allocation_fraction=0.20, notional=900)

    gate = _run_manual_gate(
        "share",
        plan,
        broker,
        snapshot_updates={"equity_positions": [existing_position]},
    )

    assert gate["review_allowed"] is True
    portfolio = gate["review_constraints"]["portfolio"]
    assert portfolio["eligible_account_count"] == 1
    attestation = portfolio["eligible_accounts"][0]
    assert attestation["current_capital_at_risk_dollars"] == 500
    assert attestation["proposed_capital_at_risk_dollars"] == 900
    assert attestation["post_trade_capital_at_risk_dollars"] == 1_400
    assert attestation["allocation_cap_dollars"] == 1_800


def test_manual_review_gate_blocks_same_account_total_open_cap_breach():
    account = _manual_gate_account(equity=10_000, buying_power=2_000)
    gate = _run_manual_gate(
        "share",
        _manual_gate_share_plan(allocation_fraction=0.10, notional=900),
        _manual_gate_broker([account]),
        snapshot_updates={
            "equity_positions": [
                {
                    "account_key": account["account_key"],
                    "symbol": "MSFT",
                    "quantity": 5,
                    "signed_quantity": 5,
                    "position_type": "long",
                    "market_value": 500,
                    "current_price": 100,
                }
            ]
        },
    )

    assert gate["review_allowed"] is False
    assert any("total-open allocation cap" in blocker for blocker in gate["blockers"])
    assert gate["review_constraints"]["portfolio"]["eligible_account_count"] == 0


def test_manual_review_gate_ignores_other_account_exposure_without_pooling():
    account = _manual_gate_account(equity=10_000, buying_power=2_000)
    gate = _run_manual_gate(
        "share",
        _manual_gate_share_plan(allocation_fraction=0.10, notional=900),
        _manual_gate_broker([account]),
        snapshot_updates={
            "equity_positions": [
                {
                    "account_key": OTHER_ACCOUNT_KEY,
                    "symbol": "MSFT",
                    "quantity": 100,
                    "signed_quantity": -100,
                    "position_type": "short",
                    "market_value": -50_000,
                }
            ]
        },
    )

    assert gate["review_allowed"] is True
    attestation = gate["review_constraints"]["portfolio"]["eligible_accounts"][0]
    assert attestation["current_capital_at_risk_dollars"] == 0


def test_manual_review_gate_duplicate_checks_use_only_fully_passing_account():
    passing = _manual_gate_account(
        label="Passing",
        equity=10_000,
        buying_power=2_000,
        account_key=PASSING_ACCOUNT_KEY,
    )
    failing_other = _manual_gate_account(
        label="Other",
        equity=4_000,
        buying_power=2_000,
        account_key=OTHER_ACCOUNT_KEY,
    )
    broker = _manual_gate_broker([passing, failing_other])
    cases = [
        (
            "share",
            _manual_gate_share_plan(),
            None,
            {
                "equity_positions": [
                    {
                        "account_key": OTHER_ACCOUNT_KEY,
                        "symbol": "AAPL",
                        "quantity": 1,
                        "signed_quantity": 1,
                        "position_type": "long",
                        "market_value": 100,
                        "current_price": 100,
                    }
                ]
            },
        ),
        (
            "share",
            _manual_gate_share_plan(),
            None,
            {
                "equity_orders": [
                    {
                        "account_key": OTHER_ACCOUNT_KEY,
                        "symbol": "AAPL",
                        "quantity": 1,
                        "state": "queued",
                    }
                ]
            },
        ),
        (
            "option",
            _manual_gate_option_plan(),
            _manual_gate_option_candidate(),
            {
                "option_positions": [
                    {
                        "account_key": OTHER_ACCOUNT_KEY,
                        "symbol": "AAPL",
                        "option_type": "call",
                        "strike_price": 200,
                        "expiration_date": "2027-01-15",
                        "quantity": 1,
                        "signed_quantity": 1,
                        "position_type": "long",
                        "trade_value_multiplier": 100,
                        "mark_price": 1.0,
                        "state": "open",
                    }
                ]
            },
        ),
        (
            "option",
            _manual_gate_option_plan(),
            _manual_gate_option_candidate(),
            {
                "option_orders": [
                    {
                        "account_key": OTHER_ACCOUNT_KEY,
                        "symbol": "AAPL",
                        "option_type": "call",
                        "strike_price": 200,
                        "expiration_date": "2027-01-15",
                        "quantity": 1,
                        "state": "queued",
                        "side": "buy",
                        "position_effect": "open",
                    }
                ]
            },
        ),
    ]

    for asset, plan, candidate, snapshot_updates in cases:
        gate = _run_manual_gate(
            asset,
            plan,
            broker,
            candidate=candidate,
            snapshot_updates=snapshot_updates,
        )

        assert gate["review_allowed"] is True, (asset, snapshot_updates, gate["blockers"])
        assert gate["review_constraints"]["account"]["eligible_same_account_match_count"] == 1
        assert (
            gate["review_constraints"]["portfolio"]["eligible_accounts"][0]["account_key"]
            == PASSING_ACCOUNT_KEY
        )


def test_manual_review_gate_blocks_same_underlying_cross_asset_overlap():
    account = _manual_gate_account(equity=10_000, buying_power=2_000)
    broker = _manual_gate_broker([account])
    option_gate = _run_manual_gate(
        "option",
        _manual_gate_option_plan(),
        broker,
        candidate=_manual_gate_option_candidate(),
        snapshot_updates={
            "equity_positions": [
                {
                    "account_key": account["account_key"],
                    "symbol": "AAPL",
                    "quantity": 2,
                    "signed_quantity": 2,
                    "position_type": "long",
                    "market_value": 200,
                    "current_price": 100,
                }
            ]
        },
    )
    share_gate = _run_manual_gate(
        "share",
        _manual_gate_share_plan(
            assumed_equity=10_000,
            allocation_fraction=0.20,
        ),
        broker,
        snapshot_updates={
            "option_positions": [
                {
                    "account_key": account["account_key"],
                    "symbol": "AAPL",
                    "option_type": "call",
                    "strike_price": 200,
                    "expiration_date": "2027-01-15",
                    "quantity": 1,
                    "signed_quantity": 1,
                    "position_type": "long",
                    "trade_value_multiplier": 100,
                    "mark_price": 1.0,
                    "state": "open",
                }
            ]
        },
    )

    assert option_gate["review_allowed"] is False
    assert share_gate["review_allowed"] is False
    assert any("cross-asset concentration" in value for value in option_gate["blockers"])
    assert any("cross-asset concentration" in value for value in share_gate["blockers"])


def test_nonzero_filled_position_rows_remain_active_exposure():
    account = _manual_gate_account(equity=10_000, buying_power=2_000)
    broker = _manual_gate_broker([account])
    share_gate = _run_manual_gate(
        "share",
        _manual_gate_share_plan(),
        broker,
        snapshot_updates={
            "equity_positions": [
                {
                    "account_key": account["account_key"],
                    "symbol": "AAPL",
                    "quantity": 1,
                    "signed_quantity": 1,
                    "position_type": "long",
                    "market_value": 100,
                    "state": "filled",
                }
            ]
        },
    )
    option_gate = _run_manual_gate(
        "option",
        _manual_gate_option_plan(),
        broker,
        candidate=_manual_gate_option_candidate(),
        snapshot_updates={
            "option_positions": [
                {
                    "account_key": account["account_key"],
                    "symbol": "AAPL",
                    "option_type": "call",
                    "strike_price": 200,
                    "expiration_date": "2027-01-15",
                    "quantity": 1,
                    "signed_quantity": 1,
                    "position_type": "long",
                    "trade_value_multiplier": 100,
                    "mark_price": 1.0,
                    "state": "filled",
                }
            ]
        },
    )

    assert share_gate["review_allowed"] is False
    assert any("Existing long or short AAPL" in value for value in share_gate["blockers"])
    assert option_gate["review_allowed"] is False
    assert any(
        "already holds this exact option contract" in value for value in option_gate["blockers"]
    )


def test_manual_review_gate_blocks_broker_linked_local_option_suggested_contracts():
    account = _manual_gate_account(equity=10_000, buying_power=2_000)
    gate = _run_manual_gate(
        "share",
        _manual_gate_share_plan(
            assumed_equity=10_000,
            allocation_fraction=0.20,
        ),
        _manual_gate_broker([account]),
        local_option_positions=[
            {
                "account_key": account["account_key"],
                "tracking_scope": "broker_linked",
                "ticker": "AAPL",
                "option_side": "call",
                "strike": 200,
                "expiry": "2027-01-15",
                "trade_status": "open",
                "suggested_contracts": 1,
            }
        ],
    )

    assert gate["review_allowed"] is False
    assert any("cross-asset concentration" in value for value in gate["blockers"])


def test_manual_review_gate_blocks_broker_linked_local_share_suggested_dollars():
    account = _manual_gate_account(equity=10_000, buying_power=2_000)
    gate = _run_manual_gate(
        "option",
        _manual_gate_option_plan(),
        _manual_gate_broker([account]),
        candidate=_manual_gate_option_candidate(),
        local_share_positions=[
            {
                "account_key": account["account_key"],
                "tracking_scope": "broker_linked",
                "ticker": "AAPL",
                "trade_status": "open",
                "suggested_dollars": 500,
            }
        ],
    )

    assert gate["review_allowed"] is False
    assert any("cross-asset concentration" in value for value in gate["blockers"])


def test_manual_review_gate_ignores_terminal_or_expired_local_option_rows():
    account = _manual_gate_account(equity=10_000, buying_power=2_000)
    gate = _run_manual_gate(
        "share",
        _manual_gate_share_plan(
            assumed_equity=10_000,
            allocation_fraction=0.20,
        ),
        _manual_gate_broker([account]),
        local_option_positions=[
            {
                "account_key": account["account_key"],
                "tracking_scope": "broker_linked",
                "ticker": "AAPL",
                "expiry": "2027-01-15",
                "trade_status": "closed",
                "suggested_contracts": 1,
            },
            {
                "account_key": account["account_key"],
                "tracking_scope": "broker_linked",
                "ticker": "AAPL",
                "expiry": "2020-01-17",
                "trade_status": "open",
                "suggested_contracts": 1,
            },
        ],
    )

    assert gate["review_allowed"] is True
    assert not any("cross-asset concentration" in value for value in gate["blockers"])


def test_manual_review_gate_keeps_unscoped_nonzero_and_working_rows_fail_closed():
    account = _manual_gate_account(
        equity=10_000,
        buying_power=2_000,
        account_key=PASSING_ACCOUNT_KEY,
    )
    cases = [
        (
            {
                "equity_positions": [
                    {
                        "symbol": "MSFT",
                        "quantity": 1,
                        "signed_quantity": 1,
                        "position_type": "long",
                        "market_value": 100,
                        "current_price": 100,
                    }
                ]
            },
            "not account-scoped",
        ),
        (
            {
                "equity_orders": [
                    {
                        "symbol": "MSFT",
                        "quantity": 1,
                        "state": "queued",
                    }
                ]
            },
            "not account-scoped",
        ),
    ]

    for snapshot_updates, expected in cases:
        gate = _run_manual_gate(
            "share",
            _manual_gate_share_plan(),
            _manual_gate_broker([account]),
            snapshot_updates=snapshot_updates,
        )

        assert gate["review_allowed"] is False
        assert any(expected in blocker for blocker in gate["blockers"])


def test_manual_review_gate_blocks_incomplete_v2_capture_even_for_shares():
    broker = _manual_gate_broker(
        [
            _manual_gate_account(equity=10_000, buying_power=1_000),
        ]
    )
    broker.update(
        {
            "normalization_ready": False,
            "normalization_blocker_count": 1,
            "normalization_blockers": [
                "get_equity_orders capture is incomplete because data.next is non-null."
            ],
        }
    )

    gate = _run_manual_gate("share", _manual_gate_share_plan(assumed_equity=9_000), broker)

    assert gate["review_allowed"] is False
    assert any("read capture is incomplete" in blocker for blocker in gate["blockers"])


def test_manual_review_gate_blocks_materially_overstated_user_equity():
    broker = _manual_gate_broker(
        [
            _manual_gate_account(equity=10_000, buying_power=2_000),
        ]
    )

    gate = _run_manual_gate("share", _manual_gate_share_plan(assumed_equity=12_000), broker)

    assert gate["review_allowed"] is False
    assert any("materially above" in blocker for blocker in gate["blockers"])
    assert gate["review_constraints"]["account"]["eligible_same_account_match_count"] == 0


def test_manual_review_gate_does_not_mix_capacity_across_accounts():
    broker = _manual_gate_broker(
        [
            _manual_gate_account(label="Risk capacity", equity=10_000, buying_power=500),
            _manual_gate_account(label="Cash capacity", equity=4_000, buying_power=1_000),
        ]
    )
    plan = _manual_gate_share_plan(
        assumed_equity=4_000,
        risk_fraction=0.02,
        allocation_fraction=0.20,
        planned_stop_loss=100,
        notional=900,
    )

    gate = _run_manual_gate("share", plan, broker)

    assert gate["review_allowed"] is False
    assert any("No single eligible Robinhood account" in blocker for blocker in gate["blockers"])
    assert any("risk fraction" in blocker for blocker in gate["blockers"])
    assert any("allocation fraction" in blocker for blocker in gate["blockers"])
    assert any("verified buying power" in blocker for blocker in gate["blockers"])
    assert gate["review_constraints"]["account"]["eligible_same_account_match_count"] == 0


def test_manual_review_gate_preserves_inactive_and_missing_equity_fail_closed():
    cases = [
        (
            [_manual_gate_account(active=False, equity=10_000, buying_power=1_000)],
            "No active, funded, agentic-accessible equity account",
        ),
        (
            [_manual_gate_account(equity=None, buying_power=1_000)],
            "portfolio total value is missing",
        ),
    ]

    for account_rows, expected in cases:
        gate = _run_manual_gate(
            "share",
            _manual_gate_share_plan(assumed_equity=9_000),
            _manual_gate_broker(account_rows),
        )
        assert gate["review_allowed"] is False
        assert any(expected in blocker for blocker in gate["blockers"])


def test_manual_review_gate_rejects_truthy_strings_for_broker_permissions():
    account = _manual_gate_account(equity=10_000, buying_power=2_000)
    account["agentic_allowed"] = "false"
    share_gate = _run_manual_gate(
        "share",
        _manual_gate_share_plan(),
        _manual_gate_broker([account]),
    )
    assert share_gate["review_allowed"] is False
    assert any(
        "No active, funded, agentic-accessible equity account" in blocker
        for blocker in share_gate["blockers"]
    )

    option_gate = _run_manual_gate(
        "option",
        _manual_gate_option_plan(),
        _manual_gate_broker([_manual_gate_account()]),
        candidate=_manual_gate_option_candidate(),
        entry_gate_allowed="false",
    )
    assert option_gate["review_allowed"] is False
    assert any("option-entry gate" in blocker for blocker in option_gate["blockers"])


def test_manual_review_gate_blocks_unresolved_nonterminal_broker_orders():
    account = _manual_gate_account(equity=10_000, buying_power=2_000)
    cases = [
        (
            "share",
            _manual_gate_share_plan(),
            None,
            {"equity_orders": [{"state": "mystery_pending", "order_id": "eq-unknown"}]},
            "nonterminal equity order whose symbol cannot be verified",
        ),
        (
            "option",
            _manual_gate_option_plan(),
            _manual_gate_option_candidate(),
            {"option_orders": [{"state": "mystery_pending", "order_id": "opt-unknown"}]},
            "nonterminal option order whose exact contract cannot be verified",
        ),
    ]

    for asset, plan, candidate, snapshot_updates, expected in cases:
        gate = _run_manual_gate(
            asset,
            plan,
            _manual_gate_broker([account]),
            candidate=candidate,
            snapshot_updates=snapshot_updates,
        )
        assert gate["review_allowed"] is False
        assert any(expected in blocker for blocker in gate["blockers"])


def test_manual_option_review_blocks_normalized_multi_leg_order_with_planned_second_leg():
    raw = {
        "accounts": {"accounts": [{"account_number": "FAKE123456"}]},
        "option_orders": {
            "results": [
                {
                    "account_number": "FAKE123456",
                    "id": "queued-spread",
                    "chain_symbol": "AAPL",
                    "state": "queued",
                    "quantity": "1",
                    "legs": [
                        {
                            "side": "sell",
                            "position_effect": "open",
                            "option_type": "call",
                            "expiration_date": "2027-01-15",
                            "strike_price": "210",
                            "option_id": "first-short-leg",
                        },
                        {
                            "side": "buy",
                            "position_effect": "open",
                            "option_type": "call",
                            "expiration_date": "2027-01-15",
                            "strike_price": "200",
                            "option_id": "planned-long-call-second-leg",
                        },
                    ],
                }
            ]
        },
    }
    snapshot = normalize_broker_snapshot(
        raw,
        generated_at=datetime.now(UTC).isoformat(),
    )

    gate = _run_manual_gate(
        "option",
        _manual_gate_option_plan(),
        _manual_gate_broker(
            [
                _manual_gate_account(
                    equity=10_000,
                    buying_power=2_000,
                    account_key=snapshot["option_orders"][0]["account_key"],
                )
            ]
        ),
        candidate=_manual_gate_option_candidate(),
        snapshot_updates={"option_orders": snapshot["option_orders"]},
    )

    assert snapshot["option_orders"][0]["contract_identity_status"] == "unresolved_multi_leg"
    assert gate["review_allowed"] is False
    assert any("nonterminal multi-leg option order" in blocker for blocker in gate["blockers"])


def test_manual_review_gate_blocks_legacy_capture_for_shares_and_options():
    broker = _manual_gate_broker(
        [
            _manual_gate_account(equity=10_000, buying_power=2_000),
        ]
    )
    broker.update(
        {
            "snapshot_schema": "optedge_robinhood_broker_snapshot_v1",
            "raw_bundle_schema": "legacy_flexible_bundle",
            "execution_capture_ready": False,
            "agentic_readiness_status": "capture_untrusted",
            "agentic_readiness_detail": "A complete account-scoped V2 capture is required.",
        }
    )

    share_gate = _run_manual_gate("share", _manual_gate_share_plan(), broker)
    option_gate = _run_manual_gate(
        "option",
        _manual_gate_option_plan(),
        broker,
        candidate=_manual_gate_option_candidate(),
    )

    for gate in (share_gate, option_gate):
        assert gate["review_allowed"] is False
        assert any(
            "normalized from a complete optedge_robinhood_mcp_read_bundle_v2 capture" in blocker
            for blocker in gate["blockers"]
        )


def test_manual_share_review_blocks_negative_short_position_before_open_long_buy():
    broker = _manual_gate_broker(
        [
            _manual_gate_account(equity=10_000, buying_power=2_000),
        ]
    )

    gate = _run_manual_gate(
        "share",
        _manual_gate_share_plan(),
        broker,
        snapshot_updates={
            "equity_positions": [
                {
                    "symbol": "AAPL",
                    "quantity": -5,
                    "signed_quantity": -5,
                    "position_type": "short",
                }
            ]
        },
    )

    assert gate["review_allowed"] is False
    assert any("may not silently cover or reduce" in blocker for blocker in gate["blockers"])


def test_manual_option_review_blocks_same_direction_broker_exposure_across_contracts():
    broker = _manual_gate_broker(
        [
            _manual_gate_account(equity=10_000, buying_power=2_000),
        ]
    )
    cases = [
        (
            {
                "option_positions": [
                    {
                        "symbol": "AAPL",
                        "option_type": "call",
                        "position_type": "long",
                        "strike_price": 210,
                        "expiration_date": "2027-06-18",
                        "quantity": 1,
                        "state": "open",
                    }
                ]
            },
            "same-symbol, same-direction option exposure",
        ),
        (
            {
                "option_orders": [
                    {
                        "symbol": "AAPL",
                        "option_type": "call",
                        "strike_price": 210,
                        "expiration_date": "2027-06-18",
                        "quantity": 1,
                        "state": "queued",
                        "side": "buy",
                        "position_effect": "open",
                    }
                ]
            },
            "working same-symbol, same-direction option order",
        ),
    ]

    for snapshot_updates, expected in cases:
        gate = _run_manual_gate(
            "option",
            _manual_gate_option_plan(),
            broker,
            candidate=_manual_gate_option_candidate(),
            snapshot_updates=snapshot_updates,
        )
        assert gate["review_allowed"] is False
        assert any(expected in blocker for blocker in gate["blockers"])


def test_manual_option_review_detects_signed_quantity_without_quantity_alias():
    account = _manual_gate_account(equity=10_000, buying_power=2_000)
    gate = _run_manual_gate(
        "option",
        _manual_gate_option_plan(),
        _manual_gate_broker([account]),
        candidate=_manual_gate_option_candidate(),
        snapshot_updates={
            "option_positions": [
                {
                    "account_key": account["account_key"],
                    "symbol": "AAPL",
                    "option_type": "call",
                    "position_type": "long",
                    "strike_price": 200,
                    "expiration_date": "2027-01-15",
                    "signed_quantity": 1,
                    "trade_value_multiplier": 100,
                    "mark_price": 1.0,
                    "state": "open",
                }
            ]
        },
    )

    assert gate["review_allowed"] is False
    assert any("already holds this exact option contract" in value for value in gate["blockers"])


def test_manual_option_review_requires_fresh_two_sided_quote_within_spread_cap():
    broker = _manual_gate_broker(
        [
            _manual_gate_account(equity=10_000, buying_power=2_000),
        ]
    )
    cases = [
        (
            _manual_gate_option_candidate(source_quote_at=""),
            "source quote timestamp is missing or invalid",
        ),
        (
            _manual_gate_option_candidate(source_quote_time_basis="artifact_generated_at"),
            "timestamp basis is missing or non-explicit",
        ),
        (
            _manual_gate_option_candidate(source_bid=None),
            "source bid/ask is missing or invalid",
        ),
        (
            _manual_gate_option_candidate(source_bid=0.80, source_ask=1.20),
            "source spread exceeds",
        ),
    ]

    for candidate, expected in cases:
        gate = _run_manual_gate(
            "option",
            _manual_gate_option_plan(),
            broker,
            candidate=candidate,
        )
        assert gate["review_allowed"] is False
        assert any(expected in blocker for blocker in gate["blockers"])

    valid_gate = _run_manual_gate(
        "option",
        _manual_gate_option_plan(),
        broker,
        candidate=_manual_gate_option_candidate(),
    )
    assert valid_gate["review_allowed"] is True
    assert valid_gate["review_constraints"]["quote"]["candidate_source_bid"] == 0.99
    assert valid_gate["review_constraints"]["quote"]["candidate_source_ask"] == 1.01

    research_gate = _run_manual_gate(
        "option",
        _manual_gate_option_plan(),
        broker,
        candidate=_manual_gate_option_candidate(
            source_quote_time_basis="provider_response_received_at",
            quote_quality="free_or_delayed",
            data_delay="delayed",
        ),
    )
    assert research_gate["review_allowed"] is True
    assert research_gate["review_constraints"]["quote"]["candidate_quote_is_research_only"] is True
    assert research_gate["review_constraints"]["quote"]["max_live_quote_age_seconds"] == 120
    assert research_gate["review_constraints"]["quote"]["limit_price_may_increase"] is False
    assert research_gate["review_constraints"]["quote"]["max_spread_fraction"] == 0.12
    assert any("fresh Robinhood quote" in warning for warning in research_gate["warnings"])


def test_manual_option_review_freezes_exact_cycle_and_queue_candidate_attestation():
    gate = _run_manual_gate(
        "option",
        _manual_gate_option_plan(),
        _manual_gate_broker(
            [
                _manual_gate_account(equity=10_000, buying_power=2_000),
            ]
        ),
        candidate=_manual_gate_option_candidate(),
    )

    candidate = gate["review_constraints"]["candidate"]
    assert gate["review_allowed"] is True
    assert candidate["schema"] == "optedge_option_candidate_review_attestation_v1"
    assert candidate["status"] == "allowed"
    assert candidate["allowed"] is True
    assert candidate["blockers"] == []
    assert candidate["source_cycle_schema"] == "optedge_robinhood_agentic_cycle_v1"
    assert candidate["source_queue_schema"] == "optedge_robinhood_agentic_options_queue_v1"
    assert candidate["exact_candidate_count_cycle"] == 1
    assert candidate["exact_candidate_count_queue"] == 1
    assert candidate["candidate_rows_match"] is True
    assert len(candidate["cycle_digest_sha256"]) == 64
    assert len(candidate["queue_digest_sha256"]) == 64
    assert len(candidate["candidate_row_digest_sha256"]) == 64
    assert candidate["candidate_fingerprint"] == candidate["candidate_row_digest_sha256"][:24]
    assert candidate["asset"] == "option"
    assert candidate["action"] == "BUY_TO_OPEN"
    assert candidate["order_type"] == "limit"
    assert candidate["time_in_force"] == "day"
    assert candidate["underlying_type"] == "equity"
    assert candidate["symbol"] == "AAPL"
    assert candidate["option_type"] == "call"
    assert candidate["strike"] == 200
    assert candidate["expiry"] == "2027-01-15"
    assert candidate["dte"] >= 90
    assert candidate["entry_gate_new_entries_allowed_after_live_checks"] is True
    assert candidate["cycle_auto_submit_allowed"] is False
    assert candidate["cycle_does_not_place_orders"] is True
    assert candidate["queue_does_not_place_orders"] is True
    assert candidate["queue_execution_enabled"] is False
    assert candidate["queue_max_orders_to_submit"] == 0
    assert candidate["candidate_quantity_cap"] == 1
    assert candidate["candidate_limit_cap"] == 1.0
    assert candidate["planned_quantity"] == 1
    assert candidate["planned_limit"] == 1.0
    assert candidate["candidate_source_bid"] == 0.99
    assert candidate["candidate_source_ask"] == 1.01
    assert candidate["candidate_quote_is_research_only"] is False
    assert set(candidate) == {
        "schema",
        "status",
        "allowed",
        "blockers",
        "asset",
        "action",
        "order_type",
        "time_in_force",
        "underlying_type",
        "symbol",
        "option_type",
        "strike",
        "expiry",
        "dte",
        "candidate_fingerprint",
        "candidate_row_digest_sha256",
        "source_cycle_schema",
        "source_queue_schema",
        "cycle_generated_at",
        "queue_generated_at",
        "max_source_age_minutes",
        "cycle_digest_sha256",
        "queue_digest_sha256",
        "exact_candidate_count_cycle",
        "exact_candidate_count_queue",
        "candidate_rows_match",
        "entry_gate_new_entries_allowed_after_live_checks",
        "cycle_auto_submit_allowed",
        "cycle_does_not_place_orders",
        "queue_does_not_place_orders",
        "queue_execution_enabled",
        "queue_max_orders_to_submit",
        "candidate_quantity_cap",
        "candidate_limit_cap",
        "planned_quantity",
        "planned_limit",
        "max_spread_fraction",
        "candidate_source_quote_at",
        "candidate_source_quote_time_basis",
        "candidate_source_bid",
        "candidate_source_ask",
        "candidate_source_spread_fraction",
        "candidate_quote_quality",
        "candidate_data_delay",
        "candidate_quote_is_research_only",
    }


def test_leaps_option_review_uses_only_the_profile_specific_lane():
    expiry = "2028-01-21"
    plan = _manual_gate_option_plan()
    plan["execution_profile"] = "leaps_swing"
    plan["order"]["expiry"] = expiry
    candidate = _manual_gate_option_candidate(
        expiry=expiry,
        dte=(datetime.fromisoformat(expiry).date() - datetime.now(UTC).date()).days,
        execution_profile="leaps_swing",
        strategy_evidence_lane="option_leaps_swing",
        profile_policy_version=cockpit_module.LEAPS_SWING_PROFILE.policy_version,
        leaps_swing_status="execution_ready",
        leaps_execution_ready=True,
        leaps_hard_blockers=[],
        leaps_data_blockers=[],
        max_allowed_spread_pct=0.10,
    )
    broker = _manual_gate_broker(
        [
            _manual_gate_account(equity=10_000, buying_power=2_000),
        ]
    )

    gate = _run_manual_gate("option", plan, broker, candidate=candidate)

    assert gate["review_allowed"] is True
    evidence = gate["review_constraints"]["evidence"]
    attestation = gate["review_constraints"]["candidate"]
    assert evidence["execution_profile"] == "leaps_swing"
    assert evidence["evidence_lane"] == "option_leaps_swing"
    assert evidence["required_horizons_sessions"] == [5, 10, 20]
    assert evidence["require_broker_market_observed"] is True
    assert attestation["execution_profile"] == "leaps_swing"
    assert attestation["leaps_swing_status"] == "execution_ready"
    assert attestation["leaps_data_blockers"] == []
    assert attestation["max_spread_fraction"] == 0.10

    research_only = dict(candidate)
    research_only.update(
        {
            "leaps_swing_status": "research_only",
            "leaps_execution_ready": False,
            "leaps_data_blockers": ["quote is delayed"],
        }
    )
    blocked = _run_manual_gate("option", plan, broker, candidate=research_only)
    assert blocked["review_allowed"] is False
    assert any("research-only or blocked" in value for value in blocked["blockers"])


def test_manual_option_review_blocks_missing_duplicate_or_mismatched_queue_candidate():
    broker = _manual_gate_broker(
        [
            _manual_gate_account(equity=10_000, buying_power=2_000),
        ]
    )
    candidate = _manual_gate_option_candidate()
    cases = [
        (
            [],
            0,
            False,
            "must occur exactly once in queue.orders",
        ),
        (
            [candidate, json.loads(json.dumps(candidate))],
            2,
            False,
            "must occur exactly once in queue.orders",
        ),
        (
            [{**candidate, "research_instruction": "mismatched row"}],
            1,
            False,
            "candidate rows in the cycle and queue do not match",
        ),
    ]

    for queue_candidates, expected_count, expected_rows_match, expected_blocker in cases:
        gate = _run_manual_gate(
            "option",
            _manual_gate_option_plan(),
            broker,
            candidate=candidate,
            queue_candidates=queue_candidates,
        )
        attestation = gate["review_constraints"]["candidate"]
        assert gate["review_allowed"] is False
        assert attestation["allowed"] is False
        assert attestation["exact_candidate_count_cycle"] == 1
        assert attestation["exact_candidate_count_queue"] == expected_count
        assert attestation["candidate_rows_match"] is expected_rows_match
        assert any(expected_blocker in blocker for blocker in gate["blockers"])


def test_manual_option_review_requires_inert_cycle_and_queue_controls():
    broker = _manual_gate_broker(
        [
            _manual_gate_account(equity=10_000, buying_power=2_000),
        ]
    )
    cases = [
        ({"cycle_updates": {"auto_submit_allowed": True}}, "auto_submit_allowed=false"),
        ({"cycle_updates": {"does_not_place_orders": False}}, "cycle must explicitly declare"),
        ({"queue_updates": {"does_not_place_orders": False}}, "queue must explicitly declare"),
        ({"queue_updates": {"execution_enabled": True}}, "execution_enabled=false"),
        ({"queue_updates": {"max_orders_to_submit": 1}}, "max_orders_to_submit=0"),
    ]

    for updates, expected in cases:
        gate = _run_manual_gate(
            "option",
            _manual_gate_option_plan(),
            broker,
            candidate=_manual_gate_option_candidate(),
            **updates,
        )
        candidate = gate["review_constraints"]["candidate"]
        assert gate["review_allowed"] is False
        assert candidate["allowed"] is False
        assert any(expected in blocker for blocker in gate["blockers"])


def test_trade_plan_report_calculates_but_blocks_without_local_evidence():
    with tempfile.TemporaryDirectory() as td:
        report = build_trade_plan_report(_share_plan_payload(), Path(td))

    assert report["calculation_ok"] is True
    assert report["ok"] is False
    assert report["trade_plan"]["order"]["quantity"] == 10
    assert report["review_packet"]["status"] == "blocked"
    assert "DO NOT CALL" in report["review_prompt"]


def test_trade_plan_report_rejects_explicit_invalid_unit_caps():
    for value in (0, -1, 1.5, "not-a-number", True):
        payload = {**_share_plan_payload(), "max_units": value}
        with tempfile.TemporaryDirectory() as td:
            report = build_trade_plan_report(payload, Path(td))

        codes = {row.get("code") for row in report["trade_plan"]["validation"]["errors"]}
        assert report["calculation_ok"] is False, value
        assert report["review_packet"]["status"] == "blocked", value
        assert "invalid_max_units" in codes, value


def test_best_setup_preserves_exact_option_identity_for_planner():
    row = pd.Series(
        {
            "ticker": "VICI",
            "side": "put",
            "strike": 27.5,
            "expiry": "2027-09-17",
            "underlying_type": "equity",
            "mid": 1.0,
            "bid": 0.95,
            "ask": 1.05,
            "spread_pct": 0.10,
            "stop_price": 0.5,
            "target_price": 2.0,
            "suggested_contracts": 1,
            "dte": 400,
            "confidence": 94,
            "trade_status": "Trade",
            "quote_quality": "delayed",
            "source_quote_at": "2026-07-13T15:00:00+00:00",
            "source_quote_time_basis": "provider_timestamp",
            "generated_at": "2026-07-13T15:00:01+00:00",
        }
    )

    record = cockpit_module._best_setup_record(row, "option", "options.parquet")
    candidate = record["planner_candidate"]

    assert candidate["plan_ready"] is True
    assert candidate["asset"] == "option"
    assert candidate["symbol"] == "VICI"
    assert candidate["option_type"] == "put"
    assert candidate["strike"] == 27.5
    assert candidate["expiry"] == "2027-09-17"
    assert candidate["entry_price"] == 1.0
    assert candidate["stop_price"] == 0.5
    assert candidate["target_price"] == 2.0
    assert len(candidate["candidate_fingerprint"]) == 24


def test_command_next_action_preserves_exact_planner_candidate():
    candidate = {
        "asset": "option",
        "symbol": "VICI",
        "option_type": "put",
        "strike": 27.5,
        "expiry": "2027-09-17",
        "underlying_type": "equity",
        "entry_price": 1.0,
        "stop_price": 0.5,
        "target_price": 2.0,
        "plan_ready": True,
    }
    result = cockpit_module._command_next_action(
        {
            "priority": 90,
            "label": "Review exact put",
            "detail": "Exact contract is ready for research.",
            "action": "scan_swing_chain",
            "route": "chains",
            "symbol": "VICI",
            "planner_candidate": candidate,
        },
        {"status": "active_window"},
        [],
        "Review",
        validation_guard={"level": "ok"},
    )

    assert result["entry_blocked"] is False
    assert result["planner_candidate"] == candidate
    assert result["planner_candidate"]["option_type"] == "put"


def test_trade_plan_report_builds_manual_review_without_execution_or_automation():
    old_gate = cockpit_module._manual_review_gate
    try:
        cockpit_module._manual_review_gate = lambda asset, data_dir, plan: {
            "status": "ready",
            "review_allowed": True,
            "asset": asset,
            "blockers": [],
            "warnings": [],
            "review_constraints": {
                "evidence": {
                    "schema": "optedge_edge_lab_review_attestation_v1",
                    "source_schema": "optedge_edge_lab_v1",
                    "report_digest_sha256": "e" * 64,
                    "asset": "share",
                    "edge_lab_status": "validated",
                    "asset_lane_status": "validated",
                    "asset_lane_live_capital_eligible": True,
                    "evidence_lane": "current_method_executable",
                    "headline_horizon_sessions": 10,
                    "require_current_method_executable": True,
                },
                "account": {
                    "assumed_equity_dollars": 10_000,
                    "risk_fraction": 0.01,
                    "allocation_fraction": 0.10,
                    "max_equity_overstatement_fraction": 0.05,
                    "eligible_same_account_match_count": 1,
                    "require_active": True,
                    "require_agentic_allowed": True,
                    "require_options_approval": False,
                    "use_conservative_buying_power": True,
                    "account_key_derivation": {
                        "schema": "optedge_robinhood_account_key_derivation_v1",
                        "algorithm": "sha256",
                        "namespace": "optedge-robinhood-account-v1|",
                        "input_field": "get_accounts.account_number",
                        "input_normalization": "strip_surrounding_whitespace",
                        "output_prefix": "acct_",
                        "lowercase_hex_characters": 16,
                        "require_exact_eligible_key_match": True,
                        "persist_raw_account_number": False,
                    },
                },
                "portfolio": _portfolio_review_constraints(),
                "drawdown": _drawdown_review_constraints(),
                "candidate": _share_candidate_review_constraints(plan),
                "quote": {
                    "quote_tool": "get_equity_quotes",
                    "max_live_quote_age_seconds": 120,
                    "max_spread_fraction": 0.01,
                    "require_positive_bid_ask": True,
                    "require_live_tick_validation": True,
                    "limit_price_may_increase": False,
                },
            },
            "does_not_place_orders": True,
        }
        with tempfile.TemporaryDirectory() as td:
            report = build_trade_plan_report(_share_plan_payload(), Path(td))
    finally:
        cockpit_module._manual_review_gate = old_gate

    assert report["ok"] is True
    assert report["calculation_ok"] is True
    assert report["does_not_place_orders"] is True
    assert report["automation_enabled"] is False
    assert report["trade_plan"]["order"]["quantity"] == 10
    assert report["review_packet"]["status"] == "manual_review_required"
    assert report["review_packet"]["review_plan"]["review_tool"] == "review_equity_order"
    assert "No scheduled task" in report["review_prompt"]
    assert "The packet ends after the broker preview" in report["review_prompt"]
    assert "place_equity_order" not in report["review_prompt"]
    assert "place_arguments_after_confirmation" not in json.dumps(
        report["review_packet"],
        sort_keys=True,
    )


def test_trade_plan_report_blocks_short_option_review():
    with tempfile.TemporaryDirectory() as td:
        report = build_trade_plan_report(
            {
                "symbol": "AAPL",
                "asset": "option",
                "direction": "short",
                "option_type": "call",
                "underlying_type": "equity",
                "expiry": "2027-01-15",
                "strike": 200,
                "contract_multiplier": 100,
                "account_equity": 10_000,
                "risk_pct": 1,
                "allocation_pct": 10,
                "entry_price": 2,
                "stop_price": 1,
                "target_price": 4,
            },
            Path(td),
        )

    assert report["ok"] is False
    assert report["trade_plan"]["is_actionable"] is False
    assert report["review_packet"]["status"] == "blocked"
    assert "DO NOT CALL" in report["review_prompt"]


def test_trade_plan_report_blocks_index_option_types_and_roots():
    base = {
        "asset": "option",
        "direction": "long",
        "option_type": "call",
        "expiry": "2027-01-15",
        "strike": 200,
        "contract_multiplier": 100,
        "account_equity": 10_000,
        "risk_pct": 1,
        "allocation_pct": 10,
        "entry_price": 1,
        "stop_price": 0.5,
        "target_price": 2,
    }
    cases = [
        ({"symbol": "AAPL", "underlying_type": "index"}, "unsupported_review_underlying_type"),
        ({"symbol": "SPX", "underlying_type": "equity"}, "unsupported_index_option_root"),
        ({"symbol": "^SPX", "underlying_type": "equity"}, "unsupported_index_option_root"),
    ]

    for updates, expected_code in cases:
        with tempfile.TemporaryDirectory() as td:
            report = build_trade_plan_report({**base, **updates}, Path(td))
        error_codes = {row.get("code") for row in report["trade_plan"]["validation"]["errors"]}
        assert report["ok"] is False
        assert report["review_packet"]["status"] == "blocked"
        assert expected_code in error_codes


def _comparison_option_record(
    symbol="AAPL",
    *,
    raw_score=1.0,
    artifact_age=10.0,
    quote_age=5.0,
    quote_basis="provider_quote_timestamp",
    quote_quality="live_broker",
):
    now = datetime.now(UTC)
    quote_at = (now - timedelta(minutes=quote_age)).isoformat() if quote_age is not None else None
    strike = 200.0 if symbol == "AAPL" else 100.0
    fingerprint = hashlib.sha256(symbol.encode("utf-8")).hexdigest()[:24]
    planner = {
        "asset": "option",
        "symbol": symbol,
        "direction": "long",
        "option_type": "call",
        "strike": strike,
        "expiry": "2027-12-17",
        "underlying_type": "equity",
        "contract_multiplier": 100,
        "contract": f"{symbol} 2027-12-17 C {strike:g}",
        "identity_label": f"{symbol} 2027-12-17 call {strike:g}",
        "entry_price": 1.0,
        "stop_price": 0.5,
        "target_price": 2.0,
        "max_units": 1,
        "source_file": f"top_options_{symbol}.parquet",
        "source_artifact_age_minutes": artifact_age,
        "source_artifact_time_basis": "file_mtime_age",
        "source_quote_at": quote_at,
        "source_quote_time_basis": quote_basis,
        "quote_quality": quote_quality,
        "data_delay": "real_time",
        "bid": 0.98,
        "ask": 1.02,
        "mid": 1.0,
        "spread_pct": 0.04,
        "candidate_fingerprint": fingerprint,
        "plan_ready": True,
        "blockers": [],
    }
    return {
        "asset": "option",
        "ticker_or_symbol": symbol,
        "setup": planner["identity_label"],
        "action": "call",
        "score": raw_score,
        "entry_price": 1.0,
        "stop_price": 0.5,
        "target_price": 2.0,
        "suggested_contracts": 1,
        "spread_pct": 0.04,
        "source_file": planner["source_file"],
        "snapshot_age_min": artifact_age,
        "snapshot_freshness": "fresh" if artifact_age <= 90 else "stale",
        "setup_gate_status": "ready",
        "setup_gate_reasons": [],
        "planner_candidate": planner,
    }


def _comparison_context(*, edge_eligible=True, broker=None, validation_level="ok"):
    command = {
        "climate_label": "constructive",
        "climate_score": 68,
        "validation_guardrail": {
            "level": validation_level,
            "detail": "Independent evidence is current."
            if validation_level == "ok"
            else "Refresh validation evidence.",
        },
    }
    edge = {
        "status": "validated" if edge_eligible else "paper_only",
        "primary_blocker": None if edge_eligible else "Option evidence is still paper-only",
        "source_attestation": {"met": True},
        "asset_rows": [
            {
                "asset": "option",
                "status": "validated" if edge_eligible else "insufficient",
                "live_capital_eligible": edge_eligible,
                "evidence_lane": "current_method_executable"
                if edge_eligible
                else "current_method_shadow",
                "primary_blocker": None if edge_eligible else "Option evidence is still paper-only",
            }
        ],
    }
    return (
        command,
        edge,
        broker
        or {
            "manual_review_candidates": [],
            "broker_reconciliation_rows": [],
            "paper_positions": [],
        },
    )


def test_candidate_comparison_ranks_freshness_before_incomparable_raw_scores():
    stale_high_score = _comparison_option_record(
        "STALE",
        raw_score=999.0,
        artifact_age=900.0,
    )
    fresh_low_score = _comparison_option_record(
        "FRESH",
        raw_score=0.01,
        artifact_age=8.0,
    )
    command, edge, broker = _comparison_context()

    board = cockpit_module.build_candidate_comparison(
        {"by_asset": {"option": [stale_high_score, fresh_low_score]}},
        command=command,
        edge=edge,
        broker=broker,
    )

    assert board["raw_cross_asset_scores_used"] is False
    assert board["rows"][0]["symbol"] == "FRESH"
    assert board["rows"][0]["planner_load_allowed"] is True
    assert board["winner_id"] == board["rows"][0]["candidate_id"]
    assert board["rows"][-1]["kind"] == "baseline"


def test_candidate_comparison_keeps_missing_quote_visible_but_locks_planner():
    missing_quote = _comparison_option_record(
        "NOQUOTE",
        quote_age=None,
        quote_basis=None,
        quote_quality=None,
    )
    command, edge, broker = _comparison_context()

    board = cockpit_module.build_candidate_comparison(
        {"by_asset": {"option": [missing_quote]}},
        command=command,
        edge=edge,
        broker=broker,
    )

    candidate, baseline = board["rows"]
    assert candidate["symbol"] == "NOQUOTE"
    assert candidate["quote_age_minutes"] is None
    assert candidate["planner_load_allowed"] is False
    assert candidate["planner_candidate"] is None
    assert "quote" in candidate["primary_blocker"].lower()
    assert baseline["candidate_id"] == "no_trade"
    assert baseline["recommended"] is True
    assert board["winner_id"] == "no_trade"


def test_candidate_comparison_blocks_exact_portfolio_overlap():
    record = _comparison_option_record("AAPL")
    broker = {
        "manual_review_candidates": [],
        "broker_reconciliation_rows": [
            {
                "symbol": "AAPL",
                "option_side": "call",
                "strike": 200.0,
                "expiry": "2027-12-17",
                "position_type": "long",
            }
        ],
        "paper_positions": [],
    }
    command, edge, broker = _comparison_context(broker=broker)

    board = cockpit_module.build_candidate_comparison(
        {"by_asset": {"option": [record]}},
        command=command,
        edge=edge,
        broker=broker,
    )

    candidate = board["rows"][0]
    assert candidate["overlap"] == "exact contract overlap"
    assert candidate["planner_load_allowed"] is False
    assert "exact contract" in candidate["primary_blocker"].lower()
    assert board["winner_id"] == "no_trade"


def test_candidate_comparison_shows_only_top_three_plus_no_trade():
    records = [
        _comparison_option_record(symbol, artifact_age=10.0 + index)
        for index, symbol in enumerate(("AAA", "BBB", "CCC", "DDD"))
    ]
    command, edge, broker = _comparison_context()

    board = cockpit_module.build_candidate_comparison(
        {"by_asset": {"option": records}},
        command=command,
        edge=edge,
        broker=broker,
    )

    assert board["candidate_count"] == 4
    assert board["displayed_candidate_count"] == 3
    assert len(board["rows"]) == 4
    assert [row["kind"] for row in board["rows"]].count("baseline") == 1


def test_dashboard_handoff_preserves_exact_dashboard_snapshot_and_option_lineage(tmp_path):
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    dashboard = tmp_path / f"dashboard_{stamp}.html"
    dashboard.write_text("<html><body>live scan</body></html>", encoding="utf-8")
    pd.DataFrame(
        [
            {
                "ticker": "AAA",
                "side": "call",
                "strike": 100.0,
                "expiry": "2027-01-15",
                "dte": 181,
                "bid": 4.9,
                "ask": 5.1,
                "mid": 5.0,
                "spread_pct": 0.04,
                "confidence": 80,
                "rank_score": 3.0,
                "trade_status": "Trade",
                "suggested_contracts": 1,
                "stop_price": 2.5,
                "target_price": 10.0,
                "underlying_type": "equity",
                "quote_quality": "live_or_broker",
            },
            {
                "ticker": "BBB",
                "side": "put",
                "strike": 50.0,
                "expiry": "2027-01-15",
                "dte": 181,
                "bid": 2.9,
                "ask": 3.1,
                "mid": 3.0,
                "spread_pct": 0.066,
                "confidence": 75,
                "rank_score": 2.0,
                "trade_status": "Trade",
                "suggested_contracts": 1,
                "stop_price": 1.5,
                "target_price": 6.0,
                "underlying_type": "equity",
                "quote_quality": "live_or_broker",
            },
        ]
    ).to_parquet(tmp_path / f"top_options_{stamp}.parquet")
    pd.DataFrame([{"ticker": "OLD", "spot": 10.0, "rank_score": 99.0}]).to_parquet(
        tmp_path / "top_shares_20000101_000000.parquet"
    )
    generated_at = datetime.now(UTC).isoformat()
    finalist = {
        "symbol": "AAA",
        "ticker_or_symbol": "AAA",
        "option_side": "call",
        "strike": 100.0,
        "expiry": "2027-01-15",
        "dte": 181,
        "chain_source": "broker",
        "quote_quality": "live_or_broker",
    }
    (tmp_path / "robinhood_agentic_queue.json").write_text(
        json.dumps({"generated_at": generated_at, "orders": [finalist]}), encoding="utf-8"
    )
    (tmp_path / "robinhood_agentic_cycle.json").write_text(
        json.dumps({"generated_at": generated_at, "manual_review_candidates": [finalist]}),
        encoding="utf-8",
    )

    report = build_dashboard_handoff(tmp_path)

    assert report["status"] == "synchronized"
    assert report["snapshot_tag"] == stamp
    assert report["count"] == 2
    assert [row["ticker_or_symbol"] for row in report["by_asset"]["option"]] == [
        "AAA",
        "BBB",
    ]
    assert report["by_asset"]["share"] == []
    share_alignment = next(row for row in report["source_alignment"] if row["lane"] == "share")
    assert share_alignment["status"] == "empty_for_snapshot"
    assert share_alignment["ignored_mixed_snapshot_file"] == "top_shares_20000101_000000.parquet"
    assert report["quality"]["mixed_snapshot_rows_used"] == 0
    assert report["option_lineage"]["matches_dashboard_contract"] is True
    assert report["option_lineage"]["dashboard_lane"] == "option_call"
    assert report["option_lineage"]["dashboard_rank"] == 1
    assert report["option_lineage"]["read_check_available"] is True


def test_candidate_comparison_keeps_demo_dashboard_row_visible_but_blocks_planner():
    record = _comparison_option_record("DEMO")
    record["dashboard_source_mode"] = "demo_hybrid"
    record["dashboard_rank"] = 1
    record["dashboard_lane"] = "option_call"
    command, edge, broker = _comparison_context()

    board = cockpit_module.build_candidate_comparison(
        {"by_asset": {"option": [record]}},
        command=command,
        edge=edge,
        broker=broker,
    )

    candidate = board["rows"][0]
    assert candidate["dashboard_rank"] == 1
    assert candidate["dashboard_lane"] == "option_call"
    assert candidate["planner_load_allowed"] is False
    assert any("demo/hybrid" in blocker for blocker in candidate["blockers"])
    assert board["winner_id"] == "no_trade"


def test_market_refresh_scan_args_rebuild_inert_robinhood_lineage():
    args = cockpit_module._market_refresh_scan_args("quick", bankroll=750, aggressive=True)

    assert args[:4] == ["--minimal", "--aggressive", "--bankroll", "750.0"]
    assert "--robinhood-agentic-queue" in args
    assert args[args.index("--robinhood-budget") + 1] == "750.0"
    assert args[args.index("--robinhood-min-dte") + 1] == str(
        cockpit_module.MIN_SWING_OPTION_DTE
    )
    assert args[args.index("--robinhood-max-candidates") + 1] == "10"
    assert args[args.index("--robinhood-max-orders") + 1] == "1"


def test_trade_desk_contract_is_manual_and_versioned():
    old_command = cockpit_module.build_command_center
    old_autopilot = cockpit_module.build_agentic_autopilot_status
    old_edge = cockpit_module.build_edge_lab_report
    old_best = cockpit_module.build_best_setups
    calls = {"command": 0, "broker": 0, "edge": 0, "best": 0}
    try:

        def command_builder(data_dir, include_live_discovery=False, refresh_trade_queue=False):
            calls["command"] += 1
            return {
                "status": "review",
                "status_detail": "Review the snapshot.",
                "validation_guardrail": {"level": "ok", "detail": "Current."},
            }

        def broker_builder(data_dir):
            calls["broker"] += 1
            return {"status": "idle", "auto_submit_allowed": False}

        def edge_builder(data_dir):
            calls["edge"] += 1
            return {"status": "paper_only", "asset_rows": []}

        def best_builder(data_dir, per_asset=3, limit=12):
            calls["best"] += 1
            return {"by_asset": {}}

        cockpit_module.build_command_center = command_builder
        cockpit_module.build_agentic_autopilot_status = broker_builder
        cockpit_module.build_edge_lab_report = edge_builder
        cockpit_module.build_best_setups = best_builder
        with tempfile.TemporaryDirectory() as td:
            desk = build_trade_desk(Path(td))
    finally:
        cockpit_module.build_command_center = old_command
        cockpit_module.build_agentic_autopilot_status = old_autopilot
        cockpit_module.build_edge_lab_report = old_edge
        cockpit_module.build_best_setups = old_best

    assert desk["schema"] == "optedge_trade_desk_v2"
    assert desk["strategy_version"]
    assert desk["execution_mode"] == "manual_review_only"
    assert desk["automation_enabled"] is False
    assert desk["snapshot_id"] == "local-empty"
    assert desk["model_trust"]["ordinary_scan_training"] == "disabled"
    assert desk["account_drawdown"]["status"] == "blocked"
    assert desk["account_drawdown"]["base_risk_fraction"] == 0.01
    assert desk["candidate_comparison"]["rows"][-1]["candidate_id"] == "no_trade"
    assert desk["candidate_comparison"]["broker_action_enabled"] is False
    assert desk["scan_handoff"]["schema"] == cockpit_module.DASHBOARD_HANDOFF_SCHEMA
    assert calls == {"command": 1, "broker": 1, "edge": 1, "best": 1}


def test_first_load_climate_is_local_and_fail_closed(monkeypatch, tmp_path):
    def fail_live_climate(*args, **kwargs):
        raise AssertionError("first load must not fetch live climate")

    monkeypatch.setattr(cockpit_module, "build_swing_climate", fail_live_climate)
    monkeypatch.setattr(
        cockpit_module,
        "build_best_setups",
        lambda *args, **kwargs: {
            "rows": [
                {
                    "asset": "option",
                    "ticker_or_symbol": "HYG",
                    "setup": "HYG P 75 2026-12-18",
                    "readiness_score": 100,
                    "readiness_label": "ready",
                    "trade_status": "ready",
                    "dte": 365,
                    "spread_pct": 0.05,
                    "suggested_contracts": 1,
                    "swing_fit_label": "clean_swing",
                    "score": 99,
                }
            ],
            "asset_summaries": [],
            "sources": {},
        },
    )

    climate = cockpit_module._local_first_load_climate(tmp_path)
    report = build_climate_gated_setups(tmp_path, climate=climate)

    assert climate["context_source"] == "local_first_load_defensive_fallback"
    assert climate["live_fetch_performed"] is False
    assert report["climate_label"] == "context_unavailable"
    assert report["rows"][0]["ticker_or_symbol"] == "HYG"


def test_cockpit_html_contains_lookup_controls():
    html = render_cockpit_html()
    assert "__OPTEDGE_CSRF_TOKEN__" not in html
    assert cockpit_module.COCKPIT_CSRF_TOKEN in html
    assert "X-Optedge-CSRF" in html
    assert "window.fetch =" in html
    assert "\ufffd" not in html
    assert "Optedge Local Cockpit" in html
    assert "Research options" in html
    assert "Research positions" in html
    assert "open-options-meta" in html
    assert "active_open_counts" in html
    assert "need cleanup" in html
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
    assert "brief-save-preferred-btn" in html
    assert "Save preferred contract" in html
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
    assert "loadView('desk')" in html
    assert "Optedge scan handoff" in html
    assert "trade-desk-handoff" in html
    assert "tradeDeskScanHandoff" in html
    assert "Execution-gated candidate comparison" in html
    assert "trade-desk-candidates" in html
    assert "tradeDeskCandidates" in html
    assert "desk-comparison-plan" in html
    assert "Exact identity" in html
    assert "Artifact age" in html
    assert "Quote age" in html
    assert "Slippage R/R" in html
    assert (
        ".grid, .desk-flow, .candidate-grid, .firewall-grid, .planner-grid, .candidate-metrics, .edge-metrics, .handoff-flow, .handoff-row { grid-template-columns:1fr; }"
        in html
    )
    assert "Capital and model firewalls" in html
    assert "trade-desk-firewalls" in html
    assert "tradeDeskFirewalls" in html
    assert 'id="plan-risk-pct" type="number" min="0.1" max="1"' in html
    assert 'id="plan-allocation-pct" type="number" min="1" max="25"' in html
    assert "use the freshness-gated comparison above" in html
    assert "desk-candidate-plan" not in html
    assert "prefillTradePlannerFromCandidate" not in html
    assert "loaders.slice(0, 1)" in html
    assert "window.setTimeout" in html
    assert "loadPositions().catch" not in html
    assert "loadPaperCandidates(false).catch" not in html
    assert "loadRobinhoodQueue(false).catch" not in html
    assert "loadExplorer().catch" not in html
    assert "loadWatchlist().catch" not in html
    assert "Trade Desk" in html
    assert 'data-view="desk"' in html
    assert "/api/trade-desk" in html
    assert "tradeDeskFlow" in html
    assert "Risk-first trade planner" in html
    assert "plan-calculate" in html
    assert "plan-copy-review" in html
    assert "plan-download" in html
    assert "/api/trade-plan" in html
    assert "calculateTradePlan" in html
    assert "manual Robinhood review" in html
    assert "no automation" in html
    assert "Equity and ETF options only" in html
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
    assert "Research lifecycle risk" in html
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
    assert "queue-alt-lookup-btn" in html
    assert "action_query" in html
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
    assert "Option data coverage" in html
    assert "/artifact/option-history-coverage" in html
    assert "/artifact/option-history-requests" in html
    assert "/artifact/option-history-prompt" in html
    assert "CBOE public activity" in html
    assert "/api/cboe-option-activity" in html
    assert "cboeActivityResultsHtml" in html
    assert "cboe-activity-query" in html
    assert "Decision gate" in html
    assert "Focus data trust" in html
    assert "Data coverage" in html
    assert "Coverage score" in html
    assert "Research loaded for" in html
    assert "Research lookup failed for" in html
    assert "overflow-wrap:anywhere" in html
    assert "Event risk" in html
    assert "Earnings / catalyst event risk" in html
    assert "Chain quality" in html
    assert "SEC offering risk" in html
    assert "SEC dilution / offering risk" in html
    assert "Agentic options queue" in html
    assert 'id="rh-min-dte" type="number" min="0" max="1200" step="1" value="90"' in html
    assert "/api/robinhood-queue" in html
    assert "/api/build-robinhood-queue" in html
    assert 'id="rh-check-finalist"' in html
    assert "Check top 10 on Robinhood" in html
    assert "/api/robinhood-check-top-options" in html
    assert "/api/robinhood-review-option" in html
    assert "/api/robinhood-place-option" in html
    assert "Place this exact order once" in html
    assert "confirmation_text:'PLACE'" in html
    assert "Load checked quote into planner" in html
    assert "The quote expires after 120 seconds" in html
    assert "loadRobinhoodQueue" in html
    assert "Manual review status" in html
    assert "autopilot-summary" in html
    assert "autopilot-actions" in html
    assert "autopilot-notes" in html
    assert "autopilot-preflight" in html
    assert "Agentic account readiness" in html
    assert "autopilot-account-readiness" in html
    assert "Robinhood MCP capability map" in html
    assert "autopilot-mcp-capabilities" in html
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
    assert "rh-profile" in html
    assert "Premium left" in html
    assert "Top rejects" in html
    assert "Option chain scan" in html
    assert "3m+ swing preset" in html
    assert "LEAPS swing preset" in html
    assert "Broad 6m+ research" in html
    assert "True LEAPS swing (365d+)" in html
    assert "leaps_swing_status" in html
    assert "LEAPS blockers" in html
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
    assert "Contract pick" in html
    assert "Pick winner" in html
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
    assert "Contract pick" in html
    assert "watch-alt-lookup-btn" in html
    assert "Lookup alt" in html
    assert "Symbol lookup" in html
    assert "/api/lookup" in html
    assert "fetch('/api/lookup', {" in html
    assert "method: 'POST'" in html
    assert "/api/lookup?symbol=" not in html
    assert "Recent lookup history" in html
    assert "lookup-history-refresh" in html
    assert "lookup-history-filter" in html
    assert "lookup-history-direction" in html
    assert "lookup-history-status" in html
    assert "lookup-history-sort" in html
    assert "lookup-history-age" in html
    assert "Best thesis return" in html
    assert "Worst thesis return" in html
    assert "Stale review" in html
    assert "lookup-history-paper-only" in html
    assert "lookup-history-chain-only" in html
    assert "lookup-history-summary" in html
    assert "lookup-history-breakdown" in html
    assert "lookup-history-leaderboard" in html
    assert "lookupHistoryFilteredRows" in html
    assert "lookupHistorySortedRows" in html
    assert "renderLookupHistoryRows" in html
    assert "lookupHistorySummary" in html
    assert "lookupHistoryBreakdown" in html
    assert "lookupHistoryLeaderboard" in html
    assert "lookupHistoryLeaderboardTable" in html
    assert "lookupHistoryActionButtons" in html
    assert "By thesis direction" in html
    assert "By research action" in html
    assert "Best follow-through" in html
    assert "Worst follow-through" in html
    assert "Paper-ready shortlist" in html
    assert "Chain-ready shortlist" in html
    assert "Needs refresh" in html
    assert "leaderboard_needs_refresh" in html
    assert "includeRefreshScan=false" in html
    assert "Fresh scan" in html
    assert "btn.textContent = 'Starting...'" in html
    assert "lookupHistoryTable" in html
    assert "Thesis return" in html
    assert "Review age" in html
    assert "lookup-history-watch-btn" in html
    assert "lookup-history-workspace-btn" in html
    assert "lookup-history-scan-btn" in html
    assert "lookup-history-paper-btn" in html
    assert "lookup-history-chain-btn" in html
    assert "/api/lookup-history" in html
    assert "/lookup-report" in html
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


def test_mutation_requests_require_json_same_origin_and_per_process_token():
    valid = {
        "Content-Type": "application/json; charset=utf-8",
        "X-Optedge-CSRF": cockpit_module.COCKPIT_CSRF_TOKEN,
        "Host": "127.0.0.1:8765",
        "Origin": "http://127.0.0.1:8765",
    }
    assert cockpit_module._post_request_rejection(valid) is None
    assert "Content-Type" in cockpit_module._post_request_rejection(
        {**valid, "Content-Type": "text/plain"}
    )
    assert "token" in cockpit_module._post_request_rejection({**valid, "X-Optedge-CSRF": "wrong"})
    assert "Cross-origin" in cockpit_module._post_request_rejection(
        {
            **valid,
            "Origin": "https://malicious.example",
        }
    )
    hostile_rebound = {
        **valid,
        "Host": "attacker.example:8765",
        "Origin": "http://attacker.example:8765",
    }
    assert "Host" in cockpit_module._post_request_rejection(hostile_rebound)
    assert cockpit_module._loopback_request_host("127.0.0.1:8765") is True
    assert cockpit_module._loopback_request_host("localhost:8765") is True
    assert cockpit_module._loopback_request_host("attacker.example:8765") is False


def test_lookup_http_routes_require_csrf_json_post_and_get_routes_are_inert():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        calls: list[tuple[str, str]] = []
        original_lookup = cockpit_module.lookup_symbol
        original_save = cockpit_module.save_lookup

        def fake_lookup(symbol, target_dir, **kwargs):
            assert Path(target_dir) == data_dir
            calls.append(("lookup", str(symbol)))
            return {
                "query": str(symbol).upper(),
                "lookup_symbol": str(symbol).upper(),
                "total_hits": 0,
                "brief": {},
            }

        def fake_save(report, target_dir):
            assert Path(target_dir) == data_dir
            calls.append(("save", str(report.get("query"))))
            archive_dir = data_dir / "lookup_reports"
            return {
                "html": data_dir / "lookup_AAPL.html",
                "json": data_dir / "lookup_AAPL.json",
                "archive_html": archive_dir / "lookup_AAPL_20260712_120000.html",
                "archive_json": archive_dir / "lookup_AAPL_20260712_120000.json",
                "history": data_dir / "lookup_history.jsonl",
            }

        cockpit_module.lookup_symbol = fake_lookup
        cockpit_module.save_lookup = fake_save
        handler = type(
            "LookupSecurityTestHandler",
            (cockpit_module.CockpitHandler,),
            {"data_dir": data_dir},
        )
        server = cockpit_module.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = int(server.server_address[1])
        origin = f"http://127.0.0.1:{port}"

        def request(method, path, *, body=None, headers=None):
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request(method, path, body=body, headers=headers or {})
                response = connection.getresponse()
                payload = response.read()
                return response.status, payload
            finally:
                connection.close()

        try:
            for path in ("/api/lookup?symbol=AAPL", "/lookup?symbol=AAPL"):
                status, _ = request("GET", path)
                assert status == 404
            assert calls == []
            assert list(data_dir.rglob("*")) == []

            status, _ = request(
                "POST",
                "/api/lookup",
                body=json.dumps({"symbol": "AAPL"}),
                headers={"Content-Type": "application/json", "Origin": origin},
            )
            assert status == 403
            assert calls == []

            status, _ = request(
                "POST",
                "/api/lookup",
                body=json.dumps({"symbol": "AAPL"}),
                headers={
                    "Content-Type": "text/plain",
                    "X-Optedge-CSRF": cockpit_module.COCKPIT_CSRF_TOKEN,
                    "Origin": origin,
                },
            )
            assert status == 403
            assert calls == []

            status, payload = request(
                "POST",
                "/api/lookup",
                body=json.dumps({"symbol": "AAPL"}),
                headers={
                    "Content-Type": "application/json",
                    "X-Optedge-CSRF": cockpit_module.COCKPIT_CSRF_TOKEN,
                    "Origin": origin,
                },
            )
            assert status == 200
            result = json.loads(payload.decode("utf-8"))
            assert result["lookup_symbol"] == "AAPL"
            assert result["saved_lookup"]["report_url"].startswith("/lookup-report?")
            assert calls == [("lookup", "AAPL"), ("save", "AAPL")]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            cockpit_module.lookup_symbol = original_lookup
            cockpit_module.save_lookup = original_save


def test_robinhood_connection_routes_are_explicit_loopback_only_and_secret_safe():
    original_sync = cockpit_module.sync_robinhood_broker_snapshot
    original_finalist_check = cockpit_module.check_best_option_finalist
    sync_calls = []
    finalist_calls = []

    def fake_sync(manager, *, data_dir):
        sync_calls.append((manager, Path(data_dir)))
        return {
            "schema": "optedge_robinhood_direct_snapshot_sync_v1",
            "ok": True,
            "snapshot_ready": True,
            "account_count": 1,
            "counts": {"equity_positions": 0, "option_positions": 0},
            "raw_bundle_written": False,
            "does_not_place_orders": True,
        }

    cockpit_module.sync_robinhood_broker_snapshot = fake_sync

    def fake_finalist_check(manager, *, data_dir, write):
        finalist_calls.append((manager, Path(data_dir), write))
        return {
            "schema": "optedge_robinhood_option_finalist_check_v1",
            "status": "passed",
            "market_check_passed": True,
            "ready_for_manual_review": False,
            "candidate": {"label": "HYG 2026-12-18 P 75"},
            "quote": {"bid_price": 0.48, "ask_price": 0.50},
            "does_not_place_orders": True,
            "does_not_preview_orders": True,
        }

    cockpit_module.check_best_option_finalist = fake_finalist_check

    class FakeConnectionManager:
        def __init__(self, callback_uri):
            self.callback_uri = callback_uri
            self.connect_calls = 0
            self.disconnect_calls = 0
            self.callbacks = []

        @staticmethod
        def _status(state="authorization_required"):
            return {
                "schema": "optedge_robinhood_connection_manager_v1",
                "connection_state": state,
                "connect_pending": state == "authorization_required",
                "authorization_url_ready": state == "authorization_required",
                "last_error_code": None,
                "placement_api_exposed": False,
                "automatic_retry_enabled": False,
                "background_polling_enabled": False,
                "client": {
                    "oauth": {
                        "status": state,
                        "authorization_url_ready": state == "authorization_required",
                        "contains_authorization_url": False,
                        "contains_code_or_state": False,
                    },
                    "credential_storage": {
                        "backend_ready": True,
                        "token_present": False,
                    },
                    "tool_catalog": {
                        "ready_for_direct_review": False,
                        "read_tools": [],
                    },
                },
            }

        def status(self):
            return self._status()

        def start_connect(self):
            self.connect_calls += 1
            return self._status()

        def authorization_url_for_browser(self):
            return "https://robinhood.example/authorize?state=state-secret"

        def submit_oauth_callback(self, callback_url):
            self.callbacks.append(callback_url)
            return self._status("connecting")

        def disconnect(self):
            self.disconnect_calls += 1
            return self._status("disconnected")

    with tempfile.TemporaryDirectory() as td:
        handler = type(
            "RobinhoodConnectionRouteTestHandler",
            (cockpit_module.CockpitHandler,),
            {"data_dir": Path(td), "robinhood_connection_manager": None},
        )
        server = cockpit_module.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = int(server.server_address[1])
        origin = f"http://127.0.0.1:{port}"
        manager = FakeConnectionManager(f"{origin}/oauth/robinhood/callback")
        handler.robinhood_connection_manager = manager
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(method, path, *, body=None, headers=None):
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request(method, path, body=body, headers=headers or {})
                response = connection.getresponse()
                payload = response.read()
                return response.status, dict(response.getheaders()), payload
            finally:
                connection.close()

        post_headers = {
            "Content-Type": "application/json",
            "X-Optedge-CSRF": cockpit_module.COCKPIT_CSRF_TOKEN,
            "Origin": origin,
        }
        try:
            status, _, payload = request("GET", "/api/robinhood-connection")
            assert status == 200
            public = json.loads(payload.decode("utf-8"))
            assert public["authorization_url_ready"] is True
            assert "state-secret" not in payload.decode("utf-8")
            assert "https://robinhood.example" not in payload.decode("utf-8")

            status, headers, payload = request("GET", "/auth/robinhood/authorize")
            assert status == 302
            assert headers["Location"].startswith("https://robinhood.example/")
            assert headers["Referrer-Policy"] == "no-referrer"
            assert payload == b""

            status, headers, payload = request(
                "GET",
                "/oauth/robinhood/callback?code=code-secret&state=state-secret",
            )
            assert status == 200
            assert headers["Referrer-Policy"] == "no-referrer"
            rendered = payload.decode("utf-8")
            assert "code-secret" not in rendered
            assert "state-secret" not in rendered
            assert manager.callbacks == [
                f"{origin}/oauth/robinhood/callback?code=code-secret&state=state-secret"
            ]

            status, _, _ = request(
                "POST",
                "/api/robinhood-connect",
                body="{}",
                headers={"Content-Type": "application/json", "Origin": origin},
            )
            assert status == 403
            assert manager.connect_calls == 0

            status, _, _ = request(
                "POST", "/api/robinhood-connect", body="{}", headers=post_headers
            )
            assert status == 200
            assert manager.connect_calls == 1

            status, _, payload = request(
                "POST",
                "/api/robinhood-sync-snapshot",
                body="{}",
                headers=post_headers,
            )
            assert status == 200
            assert json.loads(payload.decode("utf-8"))["raw_bundle_written"] is False
            assert sync_calls == [(manager, Path(td))]

            status, _, payload = request(
                "POST",
                "/api/robinhood-check-finalist",
                body="{}",
                headers=post_headers,
            )
            assert status == 200
            finalist = json.loads(payload.decode("utf-8"))
            assert finalist["market_check_passed"] is True
            assert finalist["ready_for_manual_review"] is False
            assert finalist["does_not_place_orders"] is True
            assert finalist_calls == [(manager, Path(td), True)]

            status, _, _ = request(
                "POST", "/api/robinhood-disconnect", body="{}", headers=post_headers
            )
            assert status == 200
            assert manager.disconnect_calls == 1
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            cockpit_module.sync_robinhood_broker_snapshot = original_sync
            cockpit_module.check_best_option_finalist = original_finalist_check


def test_robinhood_top_ten_review_and_place_routes_stay_separate():
    original_batch = cockpit_module.check_top_option_finalists
    batch_calls = []

    def fake_batch(manager, *, data_dir, limit, write):
        batch_calls.append((manager, Path(data_dir), limit, write))
        return {
            "schema": "optedge_robinhood_option_finalist_batch_v1",
            "candidate_count": 1,
            "market_passed_count": 1,
            "review_ready_count": 1,
            "reports": [{"candidate_index": 0, "ready_for_manual_review": True}],
            "does_not_place_orders": True,
        }

    class FakeExecutionService:
        def __init__(self):
            self.review_calls = []
            self.place_calls = []

        def account_choices(self):
            return {
                "accounts": [
                    {
                        "account_key": "acct_test",
                        "label": "Agentic ••••1234",
                        "eligible_for_live_options": True,
                    }
                ]
            }

        def review(self, *, candidate_index, account_key):
            self.review_calls.append((candidate_index, account_key))
            return {
                "status": "preview_ready",
                "confirmation_token": "opaque-token",
                "confirmation_required": True,
            }

        def place(self, *, confirmation_token, confirmation_text):
            self.place_calls.append((confirmation_token, confirmation_text))
            return {"status": "order_sent", "automatic_retry_enabled": False}

    cockpit_module.check_top_option_finalists = fake_batch
    service = FakeExecutionService()
    manager = object()
    with tempfile.TemporaryDirectory() as td:
        handler = type(
            "RobinhoodExecutionRouteTestHandler",
            (cockpit_module.CockpitHandler,),
            {
                "data_dir": Path(td),
                "robinhood_connection_manager": manager,
                "robinhood_option_execution_service": service,
            },
        )
        server = cockpit_module.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = int(server.server_address[1])
        origin = f"http://127.0.0.1:{port}"
        headers = {
            "Content-Type": "application/json",
            "X-Optedge-CSRF": cockpit_module.COCKPIT_CSRF_TOKEN,
            "Origin": origin,
        }

        def post(path, body):
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request("POST", path, body=json.dumps(body), headers=headers)
                response = connection.getresponse()
                return response.status, json.loads(response.read().decode("utf-8"))
            finally:
                connection.close()

        try:
            status, batch = post("/api/robinhood-check-top-options", {"limit": 10})
            assert status == 200
            assert batch["candidate_count"] == 1
            assert batch["accounts"][0]["account_key"] == "acct_test"
            assert service.review_calls == []
            assert service.place_calls == []

            status, preview = post(
                "/api/robinhood-review-option",
                {"candidate_index": 0, "account_key": "acct_test"},
            )
            assert status == 200
            assert preview["status"] == "preview_ready"
            assert service.review_calls == [(0, "acct_test")]
            assert service.place_calls == []

            status, placed = post(
                "/api/robinhood-place-option",
                {"confirmation_token": "opaque-token", "confirmation_text": "PLACE"},
            )
            assert status == 200
            assert placed["status"] == "order_sent"
            assert service.place_calls == [("opaque-token", "PLACE")]
            assert batch_calls == [(manager, Path(td), 10, True)]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            cockpit_module.check_top_option_finalists = original_batch


def test_read_only_watchlist_enrichment_does_not_queue_broker_research():
    original_lookup = cockpit_module.lookup_symbol
    calls: list[dict] = []

    def fake_lookup(query, data_dir, **kwargs):
        calls.append(dict(kwargs))
        return {"total_hits": 0, "brief": {}}

    try:
        cockpit_module.lookup_symbol = fake_lookup
        with tempfile.TemporaryDirectory() as td:
            result = cockpit_module._enrich_watchlist_entry(
                {"symbol": "AAPL", "query": "AAPL"},
                Path(td),
            )
    finally:
        cockpit_module.lookup_symbol = original_lookup

    assert result["local_hits"] == 0
    assert calls == [{"include_sec": False, "queue_broker_request": False}]


def test_cockpit_refuses_non_loopback_bindings():
    try:
        cockpit_module.run_server("0.0.0.0", 8765, Path("unused"), open_browser=False)
        raise AssertionError("expected non-loopback binding to be rejected")
    except ValueError as exc:
        assert "local-only" in str(exc)


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
            json.dumps([{"position_id": "fut-1", "symbol": "CL=F"}]),
            encoding="utf-8",
        )
        (data_dir / "validation_summary.json").write_text(
            json.dumps(
                {
                    "open_positions": 0,
                    "assets": {
                        "option": {"open_positions": 0},
                        "share": {"open_positions": 0},
                        "futures": {"open_positions": 0},
                    },
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "position_aging_summary.json").write_text(
            json.dumps({"open_count": 1}),
            encoding="utf-8",
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
        assert health["active_open_counts"] == {"options": 0, "shares": 0, "futures": 1}
        assert health["active_total_open"] == 1
        assert "AAPL 2026-06-18 CALL 200" in health["expired_local_option_examples"]
        assert health["free_data_caches"]["sec_company_tickers"]["status"] == "missing"
        assert health["free_data_caches"]["nasdaq_symbol_directory"]["status"] == "missing"


def test_data_health_reports_fresh_sec_ticker_cache():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_share_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_futures_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "validation_summary.json").write_text(
            json.dumps(
                {
                    "open_positions": 0,
                    "assets": {
                        "option": {"open_positions": 0},
                        "share": {"open_positions": 0},
                        "futures": {"open_positions": 0},
                    },
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "position_aging_summary.json").write_text(
            json.dumps({"open_count": 0}),
            encoding="utf-8",
        )
        (data_dir / "sec_company_tickers.json").write_text(
            json.dumps(
                {
                    "rows": [
                        {"symbol": "SNOW", "name": "Snowflake Inc.", "cik": 1640147},
                        {"symbol": "AAPL", "name": "Apple Inc.", "cik": 320193},
                    ],
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "nasdaq_symbol_directory.json").write_text(
            json.dumps(
                {
                    "rows": [
                        {"symbol": "SNOW", "name": "Snowflake Inc.", "type": "EQUITY"},
                        {
                            "symbol": "QQQ",
                            "name": "Invesco QQQ Trust",
                            "type": "ETF",
                            "is_etf": True,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

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
        (data_dir / "validation_summary.json").write_text(
            json.dumps(
                {
                    "open_positions": 0,
                    "assets": {
                        "option": {"open_positions": 0},
                        "share": {"open_positions": 0},
                        "futures": {"open_positions": 0},
                    },
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "position_aging_summary.json").write_text(
            json.dumps({"open_count": 0}),
            encoding="utf-8",
        )
        pd.DataFrame(
            [
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
            ]
        ).to_parquet(data_dir / "top_options_20260603_120000.parquet")
        pd.DataFrame(
            [
                {
                    "ticker": "NVDA",
                    "spot": 100.0,
                    "suggested_dollars": 500,
                    "trade_status": "Trade",
                }
            ]
        ).to_parquet(data_dir / "top_shares_20260603_120000.parquet")
        pd.DataFrame(
            [
                {
                    "symbol": "ES=F",
                    "contract": "/MES",
                    "direction": "long",
                    "entry_price": 5000,
                    "suggested_contracts": 1,
                    "trade_status": "Trade",
                }
            ]
        ).to_parquet(data_dir / "top_futures_20260603_120000.parquet")

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
            Path(cache_path).write_text(
                json.dumps(
                    {
                        "rows": [
                            {"symbol": "SNOW", "name": "Snowflake Inc.", "cik": 1640147},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            return [{"symbol": "SNOW", "name": "Snowflake Inc.", "cik": 1640147}]

        def fake_nasdaq_loader(cache_path, timeout=8.0, fetch_if_stale=True, **kwargs):
            Path(cache_path).write_text(
                json.dumps(
                    {
                        "rows": [
                            {
                                "symbol": "QQQ",
                                "name": "Invesco QQQ Trust",
                                "type": "ETF",
                                "is_etf": True,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
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
        (data_dir / "validation_summary.json").write_text(
            json.dumps(
                {
                    "open_positions": 0,
                    "assets": {
                        "option": {"open_positions": 0},
                        "share": {"open_positions": 0},
                        "futures": {"open_positions": 0},
                    },
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "position_aging_summary.json").write_text(
            json.dumps({"open_count": 1}),
            encoding="utf-8",
        )
        (data_dir / "equity_curve.png").write_bytes(b"bad png")
        (data_dir / "open_positions.json").write_text(
            json.dumps(
                [
                    {
                        "ticker": "AAPL",
                        "side": "call",
                        "strike": 200,
                        "expiry": "2099-06-18",
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
                ]
            ),
            encoding="utf-8",
        )
        (data_dir / "open_share_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_futures_positions.json").write_text("[]", encoding="utf-8")
        pd.DataFrame(
            [
                {
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
                }
            ]
        ).to_parquet(data_dir / "top_options_20260603_120000.parquet")
        pd.DataFrame().to_parquet(data_dir / "top_shares_20260603_120000.parquet")
        pd.DataFrame().to_parquet(data_dir / "top_futures_20260603_120000.parquet")

        queue = build_action_queue(data_dir)
        assert queue["rows"][0]["category"] == "data_health"
        assert queue["rows"][0]["priority"] == 100
        assert any(
            row["label"] == "SEC ticker cache missing" and row["action"] == "warm_symbol_caches"
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
        assert any(
            row["category"] == "open_position" and row["symbol"] == "AAPL" for row in queue["rows"]
        )
        aapl_rows = [
            row
            for row in queue["rows"]
            if row["category"] == "open_position" and row["symbol"] == "AAPL"
        ]
        assert len(aapl_rows) == 1
        assert aapl_rows[0]["grouped_count"] == 1
        assert any(
            row["category"] == "paper_candidate" and row["symbol"] == "NVDA"
            for row in queue["rows"]
        )


def test_action_queue_groups_stale_snapshots_into_refresh_action():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_share_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_futures_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "validation_summary.json").write_text(
            json.dumps(
                {
                    "open_positions": 0,
                    "assets": {
                        "option": {"open_positions": 0},
                        "share": {"open_positions": 0},
                        "futures": {"open_positions": 0},
                    },
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "position_aging_summary.json").write_text(
            json.dumps({"open_count": 0}),
            encoding="utf-8",
        )
        (data_dir / "dashboard_20260603_120000.html").write_text("<html></html>", encoding="utf-8")
        pd.DataFrame(
            [
                {
                    "ticker": "AAPL",
                    "side": "call",
                    "strike": 200,
                    "expiry": "2026-12-18",
                    "mid": 2.5,
                    "suggested_contracts": 1,
                    "trade_status": "Trade",
                }
            ]
        ).to_parquet(data_dir / "top_options_20260603_120000.parquet")
        pd.DataFrame(
            [
                {
                    "ticker": "NVDA",
                    "spot": 100,
                    "suggested_dollars": 500,
                    "trade_status": "Trade",
                }
            ]
        ).to_parquet(data_dir / "top_shares_20260603_120000.parquet")
        pd.DataFrame(
            [
                {
                    "symbol": "ES=F",
                    "direction": "long",
                    "entry_price": 5000,
                    "suggested_contracts": 1,
                    "trade_status": "Trade",
                }
            ]
        ).to_parquet(data_dir / "top_futures_20260603_120000.parquet")
        pd.DataFrame(
            [
                {
                    "ticker": "HMY",
                    "value_score": 1.5,
                    "trade_status": "Trade",
                }
            ]
        ).to_parquet(data_dir / "top_value_20260603_120000.parquet")

        old_ts = (datetime.now(UTC) - timedelta(days=2)).timestamp()
        for path in data_dir.glob("top_*_20260603_120000.parquet"):
            os.utime(path, (old_ts, old_ts))

        queue = build_action_queue(data_dir)
        refresh = [row for row in queue["rows"] if row["action"] == "run_refresh_scan"]
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
            (data_dir / "watchlist_sec_filings.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-06-16T12:00:00+00:00",
                        "rows": [
                            {
                                "priority": 94,
                                "ticker": "AAPL",
                                "form": "S-3",
                                "filing_date": "2026-06-16",
                                "days_old": 0,
                                "freshness": "fresh",
                                "signal": "dilution_or_offering_watch",
                                "description": "Shelf registration statement",
                                "url": "https://www.sec.gov/aapl",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

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
        row
        for row in queue["rows"]
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
        return pd.DataFrame(
            [
                {
                    "symbol": "MOVE",
                    "name": "Move Corp Cmn",
                    "market": "NASDAQ",
                    "reason_code": "T1",
                    "halted_at": "2026-06-16T14:19:52-04:00",
                    "resumption_trade_time": None,
                    "active_halt": True,
                    "halt_risk_score": 98,
                }
            ]
        )

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
        return pd.DataFrame(
            [
                {
                    "symbol": "MOVE",
                    "name": "Move Corp Cmn",
                    "market_category": "S",
                    "reg_sho_threshold_flag": "Y",
                    "rule_3210": "N",
                    "is_threshold": True,
                    "settlement_risk_score": 86,
                }
            ]
        )

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
        return pd.DataFrame(
            [
                {
                    "symbol": "MOVE",
                    "name": "Move Corp Cmn",
                    "market_category": "R",
                    "trigger_time": "6/16/2026 9:30:00 AM",
                    "triggered_at": "2026-06-16T09:30:00-04:00",
                    "short_sale_restricted": True,
                    "ssr_risk_score": 82,
                }
            ]
        )

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
        (data_dir / "validation_summary.json").write_text(
            json.dumps(
                {
                    "open_positions": 0,
                    "assets": {
                        "option": {"open_positions": 0},
                        "share": {"open_positions": 0},
                        "futures": {"open_positions": 0},
                    },
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "position_aging_summary.json").write_text(
            json.dumps({"open_count": 0}),
            encoding="utf-8",
        )
        pd.DataFrame(
            [
                {
                    "ticker": "AAPL",
                    "side": "call",
                    "strike": 200.0,
                    "expiry": "2026-06-18",
                    "mid": 3.2,
                    "confidence": 80,
                    "rank_score": 2.0,
                    "trade_status": "Trade",
                    "suggested_contracts": 1,
                    "premium_dollars": 320.0,
                    "spread_pct": 0.24,
                    "chain_source": "tradier",
                    "quote_quality": "live_or_broker",
                }
            ]
        ).to_parquet(data_dir / "top_options_20260603_120000.parquet")
        (data_dir / "option_chain_shortlist.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-06-24T19:00:00+00:00",
                    "rows": [
                        {
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
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        add_watchlist_query("AAPL 20260618 C 200", data_dir)
        enriched = load_watchlist(data_dir, enrich=True)
        assert enriched["entries"][0]["option_alt_best"] == "AAPL C 210.0 2026-06-18"
        assert enriched["entries"][0]["option_alt_readiness"] == 88
        assert enriched["entries"][0]["contract_pick_winner"] == "alternative"
        queue = build_action_queue(data_dir)
        ready = [
            row
            for row in queue["rows"]
            if row["category"] == "watchlist" and row["label"] == "Review swing-verdict candidate"
        ]
        assert ready
        assert ready[0]["symbol"] == "AAPL"
        assert ready[0]["action"] == "preview_paper_candidate"
        assert ready[0]["source"] == "watchlist_swing_verdict"
        assert ready[0]["swing_verdict_decision"] == "paper_review"
        assert ready[0]["swing_verdict_score"] >= 70
        assert ready[0]["option_alt_best"] == "AAPL C 210.0 2026-06-18"
        assert ready[0]["contract_pick_winner"] == "alternative"
        assert ready[0]["preferred_contract"] == "AAPL C 210.0 2026-06-18"
        assert ready[0]["action_query"] == "AAPL C 210.0 2026-06-18"
        assert "best nearby contract" in ready[0]["detail"]
        assert "contract pick" in ready[0]["detail"]


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
    assert not any(
        row["symbol"] == "STALE" and row["category"] == "best_setup" for row in queue["rows"]
    )


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
        "rows": [
            {
                "asset": "option",
                "ticker_or_symbol": "RISKY",
                "setup": "RISKY short-dated option",
                "score": 80,
                "setup_gate_status": "avoid",
                "setup_gate_label": "Avoid for now",
                "setup_gate_reasons": ["below 90 dte"],
                "setup_gate_next_step": "Skip this setup until the blocking issue clears.",
            }
        ],
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
        "rows": [
            {
                "ticker_or_symbol": "AAPL",
                "asset": "option",
                "confidence": 88,
            }
        ],
    }
    cockpit_module.load_watchlist = lambda *args, **kwargs: {"entries": []}
    cockpit_module.build_swing_scout = lambda *args, **kwargs: {
        "rows": [
            {
                "asset": "option",
                "ticker_or_symbol": "OBAI",
                "setup": "OBAI small-cap mover",
                "review_action": "review_now",
                "review_label": "Review now",
                "conviction_score": 95,
                "reasons": ["momentum confirmation"],
            }
        ],
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
    assert {row["original_action"] for row in guarded} == {
        "preview_paper_candidate",
        "scan_swing_chain",
    }
    assert not any(
        row["action"] in {"preview_paper_candidate", "scan_swing_chain"} for row in queue["rows"]
    )


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
                "rows": [
                    {
                        "ticker_or_symbol": "AAPL",
                        "asset": "option",
                        "setup": "AAPL swing call",
                        "climate_gate_score": 86,
                        "readiness_score": 82,
                        "climate_gate_reasons": ["passes DTE gate", "spread acceptable"],
                    }
                ],
                "held": [],
            }

        def fake_saved(*args, **kwargs):
            return {
                "rows": [
                    {
                        "symbol": "AAPL",
                        "query": "AAPL 2026-10-16 C 220",
                        "side": "call",
                        "side_code": "C",
                        "expiry": "2026-10-16",
                        "strike": 220,
                        "review_action": "refresh_quote",
                        "review_score": 74,
                        "review_reasons": ["quote not checked"],
                    }
                ],
            }

        def fake_risk(*args, **kwargs):
            return {
                "highest_exit_pressure": [
                    {
                        "ticker_or_symbol": "TSLA",
                        "asset": "option",
                        "position_label": "TSLA open call",
                        "latest_exit_pressure": 85,
                        "pnl_pct": -0.25,
                    }
                ],
                "warnings": ["TSLA concentration is high."],
            }

        def fake_queue(*args, **kwargs):
            return {
                "rows": [
                    {
                        "priority": 70,
                        "category": "data_health",
                        "label": "SEC ticker cache missing",
                        "detail": "Warm the free company cache.",
                        "action": "warm_sec_ticker_cache",
                    }
                ],
            }

        def fake_swing(*args, **kwargs):
            swing_kwargs.update(kwargs)
            return {
                "rows": [
                    {
                        "asset": "share",
                        "ticker_or_symbol": "SMOL",
                        "setup": "SMOL small-cap squeeze watch",
                        "review_action": "review_now",
                        "review_label": "Review now",
                        "conviction_score": 88,
                        "swing_scout_score": 92,
                        "reasons": ["short/squeeze pressure", "retail/attention lift"],
                    }
                ],
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
            "rows": [
                {
                    "ticker_or_symbol": "AAPL",
                    "asset": "option",
                    "setup": "AAPL clean swing call",
                    "climate_gate_score": 95,
                    "readiness_score": 90,
                    "climate_gate_reasons": ["passes DTE gate"],
                }
            ],
            "held": [],
        }
        cockpit_module.build_saved_option_contracts = lambda *args, **kwargs: {
            "rows": [
                {
                    "symbol": "MSFT",
                    "query": "MSFT 2026-10-16 C 500",
                    "side": "call",
                    "side_code": "C",
                    "expiry": "2026-10-16",
                    "strike": 500,
                    "review_action": "refresh_quote",
                    "review_score": 80,
                    "review_reasons": ["quote stale"],
                }
            ],
        }
        cockpit_module.build_risk_summary = lambda *args, **kwargs: {
            "highest_exit_pressure": [],
            "warnings": [],
        }
        cockpit_module.build_action_queue = lambda *args, **kwargs: {"rows": []}
        cockpit_module.build_swing_scout = lambda *args, **kwargs: {
            "rows": [
                {
                    "asset": "option",
                    "ticker_or_symbol": "NVDA",
                    "setup": "NVDA swing scout call",
                    "review_action": "review_now",
                    "review_label": "Review now",
                    "conviction_score": 95,
                    "swing_scout_score": 100,
                    "reasons": ["momentum confirmation"],
                }
            ],
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
            "rows": [
                {
                    "priority": 96,
                    "label": "Review saved option contract",
                    "detail": "AAPL 180d call has clean readiness.",
                    "action": "scan_swing_chain",
                    "route": "chains",
                    "symbol": "AAPL",
                    "query": "AAPL 2026-12-18 C 220",
                    "source": "saved_option_contracts",
                }
            ],
        }
        cockpit_module.build_risk_summary = lambda *args, **kwargs: {
            "risk_level": "elevated",
            "total_open": 4,
            "attention_count": 1,
            "high_exit_pressure_count": 0,
            "highest_exit_pressure": [
                {
                    "asset": "option",
                    "ticker_or_symbol": "AAPL",
                    "position_label": "AAPL C 220",
                    "latest_exit_pressure": 72,
                    "pnl_pct": -0.12,
                    "reprice_failed_count": 0,
                }
            ],
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
            "readiness": {
                "label": "ready",
                "ready_to_submit_count": 1,
                "premium_cap_remaining": 150,
            },
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
                "rows": [
                    {
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
                    }
                ],
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
            "rows": [
                {
                    "priority": 96,
                    "label": "Review climate-cleared setup",
                    "detail": "AAPL cleared setup gates.",
                    "action": "scan_swing_chain",
                    "route": "chains",
                    "symbol": "AAPL",
                    "query": "AAPL",
                    "source": "climate_gated_setups",
                }
            ],
        }
        cockpit_module.build_risk_summary = lambda *args, **kwargs: {
            "risk_level": "medium",
            "total_open": 1,
            "attention_count": 1,
            "high_exit_pressure_count": 0,
            "highest_exit_pressure": [
                {
                    "asset": "option",
                    "ticker_or_symbol": "TLT",
                    "position_label": "TLT C 80",
                    "latest_exit_pressure": 65,
                    "pnl_pct": 0.22,
                    "reprice_failed_count": 3,
                }
            ],
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
            "readiness": {
                "label": "ready",
                "ready_to_submit_count": 1,
                "premium_cap_remaining": 150,
            },
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
            "checks": [
                {
                    "level": "bad",
                    "label": "Validation guardrail blocking entries",
                    "detail": "Max drawdown is -35.0%.",
                }
            ],
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
            "rows": [
                {
                    "priority": 97,
                    "label": "Review climate-cleared setup",
                    "detail": "AAPL cleared setup gates.",
                    "action": "scan_swing_chain",
                    "route": "chains",
                    "symbol": "AAPL",
                    "query": "AAPL",
                    "source": "climate_gated_setups",
                }
            ],
        }
        cockpit_module.build_risk_summary = lambda *args, **kwargs: {
            "risk_level": "low",
            "total_open": 1,
            "attention_count": 0,
            "high_exit_pressure_count": 0,
            "highest_exit_pressure": [
                {
                    "asset": "option",
                    "ticker_or_symbol": "MSFT",
                    "position_label": "MSFT C 400",
                    "latest_exit_pressure": 35,
                    "pnl_pct": 0.12,
                    "reprice_failed_count": 0,
                }
            ],
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
    manual = cockpit_module._command_manual_review_summary(
        {
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
        }
    )

    assert manual["label"] == "Review-only: 1 candidate(s)"
    assert manual["tone"] == "warn"
    assert manual["ready_to_submit_count"] == 0
    assert manual["review_only_entry_candidate_count"] == 1
    assert manual["entry_gate_label"] == "Approval-required review"
    assert manual["decision_log_recent_count"] == 2
    assert manual["route"] == "robinhood"
    assert manual["checks"][0] == "Entry gate: Approval-required review"
    assert any("2 recent local decision" in check for check in manual["checks"])

    blocked = cockpit_module._command_manual_review_summary(
        {
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
        }
    )
    assert blocked["label"] == "Fresh entries blocked"
    assert blocked["tone"] == "bad"
    assert blocked["route"] == "data_health"


def test_validation_guardrail_uses_summary_closed_count_with_overall_metrics():
    guard = cockpit_module._validation_guardrail(
        {
            "closed_positions": 1000,
            "overall": {
                "n": 1000,
                "win_rate": 0.117,
                "max_drawdown": -1.0,
                "profit_factor": 2.34,
            },
        }
    )

    assert guard["closed_positions"] == 1000
    assert guard["raw_closed_positions"] == 1000
    assert guard["excluded_closed_positions"] == 0
    assert guard["win_rate"] == 0.117
    assert guard["max_drawdown"] == -1.0
    assert guard["level"] == "bad"
    assert "Max drawdown" in guard["detail"]
    assert "Only 0 closed" not in " ".join(guard["warnings"])


def test_validation_guardrail_prefers_independent_swing_metrics():
    guard = cockpit_module._validation_guardrail(
        {
            "closed_positions": 1000,
            "overall": {
                "n": 1000,
                "win_rate": 0.80,
                "max_drawdown": -0.05,
                "profit_factor": 2.0,
            },
            "swing_eligible_closed_positions": 259,
            "swing_excluded_closed_positions": 741,
            "swing_eligible_after_slippage": {
                "n": 259,
                "win_rate": 0.41,
                "max_drawdown": -0.75,
                "profit_factor": 2.15,
            },
        }
    )
    assert guard["closed_positions"] == 259
    assert guard["raw_closed_positions"] == 1000
    assert guard["excluded_closed_positions"] == 741
    assert guard["validation_basis"] == "executable_swing_after_slippage"
    assert guard["win_rate"] == 0.41
    assert guard["max_drawdown"] == -0.75
    assert guard["level"] == "bad"


def test_validation_guardrail_surfaces_fixed_horizon_shadow_evidence():
    review = cockpit_module._validation_guardrail(
        {
            "closed_positions": 500,
            "overall": {"n": 500, "win_rate": 0.55, "max_drawdown": -0.05, "profit_factor": 1.2},
            "fixed_horizon": {
                "headline_shadow": {"n": 20, "unique_entry_days": 4},
            },
        }
    )
    assert review["fixed_shadow_n"] == 20
    assert review["fixed_shadow_days"] == 4
    assert review["level"] == "warn"
    assert "shadow evidence" in review["detail"].lower()

    blocked = cockpit_module._validation_guardrail(
        {
            "closed_positions": 500,
            "overall": {"n": 500, "win_rate": 0.55, "max_drawdown": -0.05, "profit_factor": 1.2},
            "fixed_horizon": {
                "headline_shadow": {
                    "n": 120,
                    "unique_entry_days": 12,
                    "avg_return": -0.01,
                    "avg_excess_vs_spy": -0.02,
                },
            },
        }
    )
    assert blocked["level"] == "bad"
    assert "Fixed-horizon shadow return" in blocked["detail"]


def test_option_setup_readiness_penalizes_negative_buyer_edge():
    base = pd.Series(
        {
            "trade_status": "Trade",
            "snapshot_freshness": "fresh",
            "confidence": 80,
            "stop_price": 1.0,
            "target_price": 4.0,
            "dte": 120,
            "spread_pct": 0.05,
            "suggested_contracts": 1,
            "quote_quality": "live_or_broker",
        }
    )
    positive = cockpit_module._setup_readiness(
        pd.Series({**base.to_dict(), "buyer_edge_pct": 0.10}),
        "option",
    )
    negative = cockpit_module._setup_readiness(
        pd.Series({**base.to_dict(), "buyer_edge_pct": -0.10}),
        "option",
    )
    assert negative["readiness_score"] == positive["readiness_score"] - 35
    assert "negative buyer edge after spread" in negative["risk_flags"]


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
            "rows": [
                {
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
                }
            ],
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
            "rows": [
                {
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
                    "underlying_type": "equity",
                }
            ],
            "held": [],
        }
        cockpit_module.build_paper_candidates = lambda *args, **kwargs: {
            "selected_count": 1,
            "excluded_count": 0,
            "top_rejection_reasons": [],
            "rows": [
                {
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
                }
            ],
        }
        cockpit_module.build_watchlist_sec_filings = lambda *args, **kwargs: {
            "generated_at": datetime.now(UTC).isoformat(),
            "symbols_checked": 1,
            "filing_count": 1,
            "fresh_count": 1,
            "high_impact_count": 1,
            "error_count": 0,
            "rows": [
                {
                    "priority": 99,
                    "ticker": "AAPL",
                    "company_name": "Apple Inc.",
                    "form": "S-3",
                    "filing_date": datetime.now(UTC).date().isoformat(),
                    "days_old": 0,
                    "freshness": "fresh",
                    "signal": "dilution_or_offering_watch",
                    "description": "Shelf registration statement",
                    "url": "https://www.sec.gov/aapl",
                }
            ],
            "notes": [],
        }
        cockpit_module.build_provider_status = lambda *args, **kwargs: {
            "generated_at": datetime.now(UTC).isoformat(),
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
        (data_dir / "option_chain_shortlist.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-06-13T20:00:00+00:00",
                    "rows": [
                        {
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
                            "underlying_type": "equity",
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
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        earnings_date = (datetime.now(UTC).date() + timedelta(days=3)).isoformat()
        pd.DataFrame(
            [
                {
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
                }
            ]
        ).to_parquet(data_dir / "top_options_20260613_200000.parquet")
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
        assert (
            packet["data_trust_check"]["data_trust"]["history_source_summary"]
            == "yahoo_chart, nasdaq_historical"
        )
        assert packet["event_risk"]["status"] == "high_event_risk"
        assert packet["event_risk"]["high_count"] == 1
        assert packet["event_risk"]["rows"][0]["symbol"] == "AAPL"
        assert (
            packet["event_risk"]["rows"][0]["action"]
            == "avoid_new_option_entry_until_after_earnings_review"
        )
        assert packet["decision_gate"]["status"] == "wait"
        assert packet["decision_gate"]["blocker_count"] >= 2
        assert any("High earnings" in item for item in packet["decision_gate"]["blockers"])
        assert any("SEC offering" in item for item in packet["decision_gate"]["blockers"])
        assert any(
            "Chain quality is clean" in item for item in packet["decision_gate"]["confirmations"]
        )
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
            "generated_at": datetime.now(UTC).isoformat(),
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
                "rows": [
                    {
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
                    }
                ],
            }

        def fake_writer(report, write_dir):
            calls["writer"] += 1
            (write_dir / "option_chain_shortlist.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-06-13T20:00:00+00:00",
                        "rows": report["rows"],
                    }
                ),
                encoding="utf-8",
            )
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
        pd.DataFrame(
            [
                {
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
                }
            ]
        ).to_parquet(data_dir / "top_options_20260603_120000.parquet")
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
        pd.DataFrame(
            [
                {
                    "ticker": "NVDA",
                    "side": "call",
                    "strike": 200.0,
                    "expiry": "2026-06-18",
                    "confidence": 82,
                    "rank_score": 2.5,
                    "trade_status": "Trade",
                }
            ]
        ).to_parquet(data_dir / "top_options_20260603_120000.parquet")
        pd.DataFrame(
            [
                {
                    "ticker": "AAPL",
                    "confidence": 70,
                    "rank_score": 1.0,
                    "trade_status": "Trade",
                    "suggested_dollars": 500,
                }
            ]
        ).to_parquet(data_dir / "top_shares_20260603_120000.parquet")
        pd.DataFrame(
            [
                {
                    "symbol": "CL=F",
                    "name": "Crude Oil WTI",
                    "direction": "long",
                    "contract": "/MCL",
                    "futures_score": 1.4,
                    "trade_status": "Trade",
                }
            ]
        ).to_parquet(data_dir / "top_futures_20260603_120000.parquet")
        (data_dir / "open_positions.json").write_text(
            json.dumps(
                [
                    {
                        "ticker": "TSLA",
                        "side": "call",
                        "strike": 260.0,
                        "expiry": "2026-12-18",
                        "trade_status": "Open",
                    }
                ]
            ),
            encoding="utf-8",
        )
        (data_dir / "open_futures_positions.json").write_text(
            json.dumps([{"symbol": "NG=F", "direction": "long", "contract": "/MNG"}]),
            encoding="utf-8",
        )
        (data_dir / "robinhood_broker_snapshot.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-06-24T19:00:00+00:00",
                    "accounts": [
                        {
                            "account_mask": "****1497",
                            "label": "Default individual margin",
                            "option_positions": [
                                {
                                    "chain_symbol": "ROBN",
                                    "option_type": "call",
                                    "strike_price": "35.0000",
                                    "expiration_date": "2026-12-18",
                                    "quantity": "2.0000",
                                }
                            ],
                            "equity_positions": [
                                {
                                    "symbol": "HOOD",
                                    "quantity": "5.0000",
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "option_chain_shortlist.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-06-24T19:00:00+00:00",
                    "rows": [
                        {
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
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

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
            assert any(
                row["symbol"] == "NG=F" and row["kind"] == "open_futures" for row in gas["rows"]
            )

            tsla = build_symbol_suggestions(data_dir, query="260")
            assert any(
                row["symbol"] == "TSLA"
                and row["kind"] == "open_option"
                and row["query"] == "TSLA 2026-12-18 C 260"
                for row in tsla["rows"]
            )

            robn = build_symbol_suggestions(data_dir, query="ROBN")
            assert any(
                row["symbol"] == "ROBN" and row["kind"] == "broker_option" for row in robn["rows"]
            )
            assert any(row["query"] == "ROBN 2026-12-18 C 35" for row in robn["rows"])
            assert any("broker snapshots" in note for note in robn["notes"])

            hood = build_symbol_suggestions(data_dir, query="HOOD")
            assert any(
                row["symbol"] == "HOOD" and row["kind"] == "broker_equity" for row in hood["rows"]
            )

            observed_fetch_modes = []

            def fake_sec_search(query, limit=16, fetch_if_stale=True):
                observed_fetch_modes.append(fetch_if_stale)
                return [
                    {
                        "symbol": "SNOW",
                        "name": "Snowflake Inc.",
                        "score": 0.97,
                    }
                ]

            cockpit_module.sec_company_search = fake_sec_search
            snow = build_symbol_suggestions(data_dir, query="snowflake")
            assert any(row["symbol"] == "SNOW" and row["kind"] == "sec" for row in snow["rows"])
            assert "Nasdaq Trader" in " ".join(snow["notes"])
            assert observed_fetch_modes == [False]

            cockpit_module.sec_company_search = lambda query, limit=16, fetch_if_stale=True: []
            cockpit_module.nasdaq_symbol_search = lambda query, limit=16, fetch_if_stale=True: [
                {
                    "symbol": "QQQ",
                    "name": "Invesco QQQ Trust",
                    "type": "ETF",
                    "score": 0.94,
                }
            ]
            qqq = build_symbol_suggestions(data_dir, query="invesco")
            assert any(row["symbol"] == "QQQ" and row["kind"] == "nasdaq" for row in qqq["rows"])
        finally:
            cockpit_module.sec_company_search = old_sec
            cockpit_module.nasdaq_symbol_search = old_nasdaq


def test_opportunity_explorer_reads_and_filters_latest_snapshots():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        pd.DataFrame(
            [
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
            ]
        ).to_parquet(data_dir / "top_options_20260603_120000.parquet")
        pd.DataFrame(
            [
                {
                    "ticker": "NVDA",
                    "confidence": 90,
                    "rank_score": 2.0,
                    "trade_status": "Trade",
                    "suggested_dollars": 500,
                },
            ]
        ).to_parquet(data_dir / "top_shares_20260603_120000.parquet")

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
        pd.DataFrame(
            [
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
            ]
        ).to_parquet(data_dir / "top_options_20260603_120000.parquet")
        pd.DataFrame(
            [
                {
                    "ticker": "NVDA",
                    "spot": 120,
                    "confidence": 88,
                    "rank_score": 2.0,
                    "trade_status": "Trade",
                    "suggested_dollars": 600,
                    "ev_pct": 0.07,
                },
            ]
        ).to_parquet(data_dir / "top_shares_20260603_120000.parquet")
        pd.DataFrame(
            [
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
            ]
        ).to_parquet(data_dir / "top_futures_20260603_120000.parquet")
        pd.DataFrame(
            [
                {
                    "ticker": "LYFT",
                    "value_score": 2.4,
                    "value_bucket": "deep value",
                    "pe": 2.0,
                    "fcf_yield": 0.12,
                },
            ]
        ).to_parquet(data_dir / "top_value_20260603_120000.parquet")

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
        assert any(
            "missing stop" in reason
            for reason in report["by_asset"]["option"][0]["setup_gate_reasons"]
        )
        assert report["asset_summaries"][0]["rows"] == 3
        assert report["asset_summaries"][0]["actionable_rows"] == 1
        assert {row["asset"] for row in report["rows"]} == {"option", "share", "futures", "value"}
        scores = [row["score"] for row in report["rows"]]
        assert scores == sorted(scores, reverse=True)


def test_best_setups_marks_clean_long_dated_option_ready():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        pd.DataFrame(
            [
                {
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
                }
            ]
        ).to_parquet(data_dir / "top_options_20260603_120000.parquet")

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
        pd.DataFrame(
            [
                {
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
                }
            ]
        ).to_parquet(data_dir / "top_options_20260603_120000.parquet")

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
        pd.DataFrame(
            [
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
            ]
        ).to_parquet(data_dir / "top_options_20260603_120000.parquet")

        report = build_best_setups(data_dir, per_asset=2, limit=2)

    assert report["rows"][0]["ticker_or_symbol"] == "STALE"
    assert report["rows"][0]["setup_gate_status"] == "avoid"
    assert report["decision_row"]["ticker_or_symbol"] == "CLEAN"
    assert report["decision_row"]["setup_gate_status"] == "ready"


def test_best_setups_include_saved_chain_shortlist_contracts():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        generated_at = datetime.now(UTC).isoformat()
        (data_dir / "option_chain_shortlist.json").write_text(
            json.dumps(
                {
                    "generated_at": generated_at,
                    "rows": [
                        {
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
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

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


def test_best_setups_marks_stale_chain_artifact_avoid_even_when_row_scores_high():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        generated_at = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        (data_dir / "option_chain_shortlist.json").write_text(
            json.dumps(
                {
                    "generated_at": generated_at,
                    "rows": [
                        {
                            "generated_at": generated_at,
                            "symbol": "STALE",
                            "contract_query": "STALE 2027-12-17 C 100",
                            "side": "call",
                            "underlying_type": "equity",
                            "expiry": "2027-12-17",
                            "strike": 100.0,
                            "dte": 400,
                            "mid": 1.0,
                            "bid": 0.98,
                            "ask": 1.02,
                            "premium_dollars": 100.0,
                            "stop_price_reference": 0.5,
                            "target_price_reference": 2.0,
                            "spread_pct": 0.04,
                            "contract_grade": "A",
                            "readiness_label": "ready",
                            "readiness_score": 99,
                            "contract_quality_score": 99,
                            "swing_fit_score": 99,
                            "quote_quality": "live_broker",
                            "source_quote_at": datetime.now(UTC).isoformat(),
                            "source_quote_time_basis": "provider_quote_timestamp",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        report = build_best_setups(data_dir, per_asset=1, limit=1)

    row = report["by_asset"]["option"][0]
    assert row["snapshot_age_min"] > cockpit_module.STALE_SNAPSHOT_MINUTES
    assert row["snapshot_freshness"] == "stale"
    assert row["setup_gate_status"] == "avoid"
    assert any("stale" in reason.lower() for reason in row["setup_gate_reasons"])


def test_swing_scout_surfaces_small_caps_and_futures_but_filters_short_dte_options():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        pd.DataFrame(
            [
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
            ]
        ).to_parquet(data_dir / "top_options_20260603_120000.parquet")
        pd.DataFrame(
            [
                {
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
                }
            ]
        ).to_parquet(data_dir / "top_shares_20260603_120000.parquet")
        pd.DataFrame(
            [
                {
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
                }
            ]
        ).to_parquet(data_dir / "top_futures_20260603_120000.parquet")

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
    cockpit_module.small_cap_movers = lambda max_rows=24: pd.DataFrame(
        [
            {
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
            }
        ]
    )
    cockpit_module.dark_pool_engine.run = lambda universe, lookback_days=3: pd.DataFrame(
        [
            {
                "ticker": "MOVE",
                "short_vol_ratio": 0.64,
                "short_vol": 640_000,
                "total_vol": 1_000_000,
                "dark_pool_score": -0.56,
            }
        ]
    )
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
    assert {item["factor"] for item in row["factor_breakdown"]} >= {
        "Momentum",
        "Short volume",
        "Market cap",
    }
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
    cockpit_module.small_cap_movers = lambda max_rows=24: pd.DataFrame(
        [
            {
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
            },
            {
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
            },
        ]
    )
    cockpit_module.dark_pool_engine.run = lambda universe, lookback_days=3: pd.DataFrame()
    cockpit_module.halt_rows_for_symbols = lambda symbols, cache_age=60: pd.DataFrame(
        [
            {
                "symbol": "RISK",
                "active_halt": True,
                "halt_risk_score": 98,
            }
        ]
    )
    cockpit_module.threshold_rows_for_symbols = lambda symbols, cache_age=21600: pd.DataFrame(
        [
            {
                "symbol": "RISK",
                "is_threshold": True,
                "settlement_risk_score": 86,
            }
        ]
    )
    cockpit_module.circuit_rows_for_symbols = lambda symbols, cache_age=1800: pd.DataFrame(
        [
            {
                "symbol": "RISK",
                "short_sale_restricted": True,
                "ssr_risk_score": 82,
            }
        ]
    )
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
        "active_halt",
        "regsho_threshold",
        "short_sale_restricted",
    }
    assert {
        "active trading halt",
        "Reg SHO threshold list",
        "short-sale circuit breaker active",
    } <= set(risky["warnings"])
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
            pd.DataFrame(
                [
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
                ]
            ).to_parquet(data_dir / "top_options_20260603_120000.parquet")
            pd.DataFrame(
                [
                    {
                        "ticker": "NVDA",
                        "spot": 120,
                        "confidence": 88,
                        "rank_score": 1.8,
                        "fused_score": 1.7,
                        "trade_status": "Trade",
                        "suggested_dollars": 600,
                        "stop_price": 110,
                        "target_price": 145,
                    }
                ]
            ).to_parquet(data_dir / "top_shares_20260603_120000.parquet")
            pd.DataFrame(
                [
                    {
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
                    }
                ]
            ).to_parquet(data_dir / "top_futures_20260603_120000.parquet")

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
                "expiry": "2099-06-18",
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
                "expiry": "2099-06-18",
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
        (data_dir / "open_futures_positions.json").write_text(
            json.dumps(
                [
                    {
                        "symbol": "ETH=F",
                        "direction": "short",
                        "contract": "/MET",
                        "entry_time": "2026-06-01T02:00:00+00:00",
                        "entry_price": 1800,
                        "current_price": 1750,
                        "pnl_pct": 0.03,
                        "trade_status": "Trade",
                        "latest_exit_pressure": 20,
                    }
                ]
            ),
            encoding="utf-8",
        )

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
        assert report["action_counts"] == {
            "watch": 1,
            "hold": 1,
            "close_early": 1,
            "tighten_stop": 1,
        }
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
        (data_dir / "open_positions.json").write_text(
            json.dumps(
                [
                    {
                        "ticker": "AAPL",
                        "side": "call",
                        "strike": 280,
                        "expiry": "2099-06-18",
                        "entry_price": 2.0,
                        "current_mid": 1.0,
                        "latest_exit_pressure": 85,
                        "trade_status": "Trade",
                    },
                    {
                        "ticker": "AAPL",
                        "side": "call",
                        "strike": 300,
                        "expiry": "2099-06-18",
                        "entry_price": 1.5,
                        "current_mid": 1.8,
                        "latest_exit_pressure": 20,
                        "trade_status": "Trade",
                    },
                ]
            ),
            encoding="utf-8",
        )
        (data_dir / "open_share_positions.json").write_text(
            json.dumps(
                [
                    {
                        "ticker": "NVDA",
                        "entry_price": 100.0,
                        "current_price": 90.0,
                        "latest_exit_pressure": 65,
                        "reprice_failed_count": 2,
                        "trade_status": "Watch",
                    }
                ]
            ),
            encoding="utf-8",
        )
        (data_dir / "open_futures_positions.json").write_text(
            json.dumps(
                [
                    {
                        "symbol": "CL=F",
                        "direction": "long",
                        "contract": "/MCL",
                        "entry_price": 70.0,
                        "current_price": 73.5,
                        "latest_exit_pressure": 10,
                        "trade_status": "Trade",
                    }
                ]
            ),
            encoding="utf-8",
        )

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


def test_risk_summary_excludes_expired_options_from_active_exposure():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        (data_dir / "open_positions.json").write_text(
            json.dumps(
                [
                    {
                        "ticker": "OLD",
                        "side": "call",
                        "strike": 10,
                        "expiry": "2020-01-17",
                        "entry_price": 1.0,
                        "current_mid": 0.0,
                        "latest_exit_pressure": 100,
                    },
                    {
                        "ticker": "LIVE",
                        "side": "call",
                        "strike": 10,
                        "expiry": "2099-01-17",
                        "entry_price": 1.0,
                        "current_mid": 1.2,
                        "latest_exit_pressure": 10,
                    },
                ]
            ),
            encoding="utf-8",
        )

        risk = build_risk_summary(data_dir)

    assert risk["total_lifecycle_rows"] == 2
    assert risk["total_open"] == 1
    assert risk["research_lifecycle_open"] == 1
    assert risk["broker_linked_lifecycle_open"] == 0
    assert risk["expired_lifecycle_count"] == 1
    assert risk["hygiene_status"] == "needs_cleanup"
    assert risk["concentration"][0]["symbol"] == "LIVE"
    assert risk["attention_count"] == 0


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
            "key": "total",
            "label": "Total options",
            "status": "ok",
            "signal": "balanced",
            "pc_ratio": 0.86,
            "latest_date": today,
        },
        "equity": {
            "key": "equity",
            "label": "Equity options",
            "status": "ok",
            "signal": "call_demand_high",
            "pc_ratio": 0.52,
            "latest_date": today,
        },
        "index": {
            "key": "index",
            "label": "Index options",
            "status": "ok",
            "signal": "defensive_hedging",
            "pc_ratio": 1.18,
            "latest_date": "2019-10-04",
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
        r"{\"name\":\"TOTAL PUT/CALL RATIO\",\"value\":\"0.76\"},"
        r"{\"name\":\"INDEX PUT/CALL RATIO\",\"value\":\"1.06\"},"
        r"{\"name\":\"EQUITY PUT/CALL RATIO\",\"value\":\"0.54\"}"
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
            pd.DataFrame(
                [
                    {
                        "ts": "2026-06-03T20:00:00+00:00",
                        "engine": "insider",
                        "elapsed_sec": 121.0,
                        "rows": 10,
                        "ok": True,
                        "error": "",
                    },
                    {
                        "ts": "2026-06-03T20:00:01+00:00",
                        "engine": "mispricing",
                        "elapsed_sec": 44.0,
                        "rows": 500,
                        "ok": True,
                        "error": "",
                    },
                ]
            ).to_parquet(perf.PERF_LOG)
            engine_health.record(
                {
                    "insider": {"ok": True, "rows": 10, "elapsed": 121.0},
                    "mispricing": {"ok": True, "rows": 500, "elapsed": 44.0},
                }
            )
            cache_stats.record_hit("history:AAPL")
            cache_stats.record_miss("history:MSFT")
            data_provider.configure_ram_cache(enabled=True, max_items=100)
            data_provider.cache_put("test:cockpit-performance", {"ok": True})
            pd.DataFrame(
                [
                    {
                        "ticker": "AAPL",
                        "finbert_device": "cuda",
                    }
                ]
            ).to_parquet(data_dir / "top_options_20260603_120000.parquet")

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
        pd.DataFrame(
            [
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
                    "underlying_type": "equity",
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
                    "underlying_type": "equity",
                },
            ]
        ).to_parquet(data_dir / "top_options_20260603_120000.parquet")
        pd.DataFrame(
            [
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
            ]
        ).to_parquet(data_dir / "top_shares_20260603_120000.parquet")
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
        source_quote_at = datetime.now(UTC).isoformat()
        pd.DataFrame(
            [
                {
                    "ticker": "AAPL",
                    "contract": "AAPL 2027-01-15 C 200",
                    "underlying_type": "equity",
                    "side": "call",
                    "strike": 200,
                    "expiry": "2027-01-15",
                    "mid": 0.75,
                    "bid": 0.735,
                    "ask": 0.765,
                    "source_quote_at": source_quote_at,
                    "data_delay": "real_time",
                    "source_quote_time_basis": "broker_quote_timestamp",
                    "quote_quality": "live_or_broker",
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
                    "underlying_type": "equity",
                    "side": "call",
                    "strike": 500,
                    "expiry": "2026-10-16",
                    "mid": 0.65,
                    "bid": 0.637,
                    "ask": 0.663,
                    "source_quote_at": source_quote_at,
                    "data_delay": "real_time",
                    "source_quote_time_basis": "broker_quote_timestamp",
                    "quote_quality": "live_or_broker",
                    "suggested_contracts": 1,
                    "actual_dollars": 65,
                    "stop_price": 0.3,
                    "target_price": 1.4,
                    "confidence": 70,
                    "rank_score": 1.8,
                    "trade_status": "Trade",
                    "spread_pct": 0.04,
                },
            ]
        ).to_parquet(data_dir / "top_options_20260613_120000.parquet")
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
        now = datetime.now(UTC).isoformat()
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
        (data_dir / "robinhood_agentic_queue.json").write_text(
            json.dumps(
                {
                    "status": "ready",
                    "generated_at": now,
                    "orders": [ticket],
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "robinhood_agentic_cycle.json").write_text(
            json.dumps(
                {
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
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "robinhood_live_order_tickets.json").write_text(
            json.dumps(
                {
                    "confirmation_required": True,
                    "generated_at": now,
                    "tickets": [ticket],
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "agentic_paper_positions.json").write_text(
            json.dumps(
                [
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
                ]
            ),
            encoding="utf-8",
        )
        (data_dir / "open_positions.json").write_text(
            json.dumps(
                [
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
                ]
            ),
            encoding="utf-8",
        )
        record_agentic_decision(
            {
                "decision": "reviewed",
                "symbol": "AAPL",
                "contract": contract,
                "reason": "paper only",
            },
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
        assert any(
            row["check"] == "Entry gate" and row["level"] == "block"
            for row in status["ticket_preflight"]
        )
        assert any(
            row["check"] == "Paper duplicate" and row["level"] == "warn"
            for row in status["ticket_preflight"]
        )
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
        stale_time = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
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
        (data_dir / "robinhood_agentic_queue.json").write_text(
            json.dumps(
                {
                    "status": "ready",
                    "generated_at": stale_time,
                    "orders": [ticket],
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "robinhood_agentic_cycle.json").write_text(
            json.dumps(
                {
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
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "robinhood_live_order_tickets.json").write_text(
            json.dumps(
                {
                    "generated_at": stale_time,
                    "tickets": [ticket],
                }
            ),
            encoding="utf-8",
        )
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
        assert any(
            row["check"] == "Fresh packet" and row["level"] == "block"
            for row in status["ticket_preflight"]
        )
        assert status["next_actions"][0]["action"] == "refresh_autopilot_packet"


def test_agentic_autopilot_blocks_legacy_ticket_but_preserves_defensive_preflight():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        now = datetime.now(UTC).isoformat()
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
        (data_dir / "robinhood_agentic_queue.json").write_text(
            json.dumps(
                {
                    "status": "ready",
                    "generated_at": now,
                    "orders": [ticket],
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "robinhood_agentic_cycle.json").write_text(
            json.dumps(
                {
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
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "robinhood_live_order_tickets.json").write_text(
            json.dumps(
                {
                    "generated_at": now,
                    "tickets": [ticket],
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "robinhood_broker_snapshot.json").write_text(
            json.dumps(
                {
                    "generated_at": now,
                    "accounts": [
                        {
                            "nickname": "Agentic",
                            "state": "active",
                            "agentic_allowed": True,
                            "option_level": "option_level_2",
                            "buying_power": 500,
                            "unleveraged_buying_power": 500,
                            "portfolio": {
                                "total_value": 10_000,
                                "buying_power": 500,
                                "unleveraged_buying_power": 500,
                            },
                            "option_positions": [],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "agentic_paper_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")

        status = build_agentic_autopilot_status(data_dir)

        assert status["status"] == "blocked"
        assert status["label"] == "Legacy ticket blocked"
        assert status["fresh_entries_allowed"] is False
        assert any(
            "legacy live-ticket artifacts are deprecated" in value for value in status["blockers"]
        )
        assert status["ticket_preflight_block_count"] >= 1
        assert status["ticket_preflight_warn_count"] == 0
        assert status["decision_recent_count"] == 0
        assert status["decision_log_needed"] is True
        assert any("staged live ticket" in reason for reason in status["decision_debt_reasons"])
        assert status["tickets"][0]["status"] == "blocked"
        assert status["tickets"][0]["preflight_status"] == "blocked"
        assert status["tickets"][0]["preflight_blocks"] >= 1
        assert status["tickets"][0]["preflight_warnings"] == 0
        assert status["tickets"][0]["mcp_review_status"] == "review"
        assert status["tickets"][0]["mcp_review_tool"] is None
        assert status["tickets"][0]["mcp_place_tool"] is None
        assert status["tickets"][0]["mcp_lookup_symbol"] is None
        checks = {(row["check"], row["level"]) for row in status["ticket_preflight"]}
        assert ("Fresh packet", "pass") in checks
        assert ("Entry gate", "block") in checks
        assert ("Confirmation", "pass") in checks
        assert ("Execution mode", "pass") in checks
        assert ("MCP review plan", "block") in checks
        actions = [row["action"] for row in status["next_actions"]]
        assert "log_decision" in actions
        assert "review_ticket" in actions


def test_agentic_autopilot_blocks_stale_unfunded_and_split_broker_state():
    cases = [
        (
            "stale",
            (datetime.now(UTC) - timedelta(hours=3)).isoformat(),
            [
                {
                    "nickname": "Agentic",
                    "state": "active",
                    "agentic_allowed": True,
                    "option_level": "option_level_2",
                    "buying_power": 500,
                    "unleveraged_buying_power": 500,
                    "portfolio": {
                        "total_value": 10_000,
                        "buying_power": 500,
                        "unleveraged_buying_power": 500,
                    },
                    "option_positions": [],
                }
            ],
            "broker snapshot is stale",
            "stale",
        ),
        (
            "unfunded",
            datetime.now(UTC).isoformat(),
            [
                {
                    "nickname": "Agentic",
                    "state": "active",
                    "agentic_allowed": True,
                    "option_level": "option_level_2",
                    "buying_power": 0,
                    "unleveraged_buying_power": 0,
                    "portfolio": {
                        "total_value": 10_000,
                        "buying_power": 0,
                        "unleveraged_buying_power": 0,
                    },
                    "option_positions": [],
                }
            ],
            "not funded and agentic-options ready",
            "blocked",
        ),
        (
            "split",
            datetime.now(UTC).isoformat(),
            [
                {
                    "nickname": "Options",
                    "state": "active",
                    "agentic_allowed": False,
                    "option_level": "option_level_2",
                    "buying_power": 500,
                    "unleveraged_buying_power": 500,
                    "portfolio": {
                        "total_value": 10_000,
                        "buying_power": 500,
                        "unleveraged_buying_power": 500,
                    },
                    "option_positions": [],
                },
                {
                    "nickname": "Agentic",
                    "state": "active",
                    "agentic_allowed": True,
                    "option_level": "",
                    "buying_power": 500,
                    "unleveraged_buying_power": 500,
                    "portfolio": {
                        "total_value": 10_000,
                        "buying_power": 500,
                        "unleveraged_buying_power": 500,
                    },
                    "option_positions": [],
                },
            ],
            "not funded and agentic-options ready",
            "blocked",
        ),
    ]
    for label, snapshot_time, accounts, blocker_text, expected_status in cases:
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            now = datetime.now(UTC).isoformat()
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
            (data_dir / "robinhood_agentic_queue.json").write_text(
                json.dumps(
                    {
                        "status": "ready",
                        "generated_at": now,
                        "orders": [ticket],
                    }
                ),
                encoding="utf-8",
            )
            (data_dir / "robinhood_agentic_cycle.json").write_text(
                json.dumps(
                    {
                        "generated_at": now,
                        "auto_submit_allowed": False,
                        "entry_gate": {
                            "status": "open",
                            "new_entries_allowed_after_live_checks": True,
                            "blockers": [],
                            "warnings": [],
                        },
                        "entry_candidates": [ticket],
                    }
                ),
                encoding="utf-8",
            )
            (data_dir / "robinhood_live_order_tickets.json").write_text(
                json.dumps(
                    {
                        "generated_at": now,
                        "tickets": [ticket],
                    }
                ),
                encoding="utf-8",
            )
            (data_dir / "robinhood_broker_snapshot.json").write_text(
                json.dumps(
                    {
                        "generated_at": snapshot_time,
                        "accounts": accounts,
                        "option_positions": [],
                    }
                ),
                encoding="utf-8",
            )
            (data_dir / "agentic_paper_positions.json").write_text("[]", encoding="utf-8")
            (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")

            status = build_agentic_autopilot_status(data_dir)

            assert status["status"] == expected_status, label
            assert status["fresh_entries_allowed"] is False, label
            assert status["ticket_preflight_block_count"] >= 1, label
            assert status["tickets"][0]["preflight_status"] == "blocked", label
            assert any(blocker_text in blocker for blocker in status["blockers"]), label


def test_agentic_autopilot_blocks_when_live_ticket_lacks_mcp_review_plan():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        now = datetime.now(UTC).isoformat()
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
        (data_dir / "robinhood_agentic_queue.json").write_text(
            json.dumps(
                {
                    "status": "ready",
                    "generated_at": now,
                    "orders": [ticket],
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "robinhood_agentic_cycle.json").write_text(
            json.dumps(
                {
                    "generated_at": now,
                    "auto_submit_allowed": False,
                    "entry_gate": {
                        "status": "open",
                        "new_entries_allowed_after_live_checks": True,
                        "blockers": [],
                        "warnings": [],
                    },
                    "entry_candidates": [ticket],
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "robinhood_live_order_tickets.json").write_text(
            json.dumps(
                {
                    "generated_at": now,
                    "tickets": [ticket],
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "robinhood_broker_snapshot.json").write_text(
            json.dumps(
                {
                    "generated_at": now,
                    "accounts": [
                        {
                            "nickname": "Agentic",
                            "state": "active",
                            "agentic_allowed": True,
                            "option_level": "option_level_2",
                            "buying_power": 500,
                            "unleveraged_buying_power": 500,
                            "portfolio": {
                                "total_value": 10_000,
                                "buying_power": 500,
                                "unleveraged_buying_power": 500,
                            },
                            "option_positions": [],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "agentic_paper_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")

        status = build_agentic_autopilot_status(data_dir)

        assert status["tickets"][0]["mcp_review_status"] == "missing"
        assert status["tickets"][0]["preflight_status"] == "blocked"
        assert status["ticket_preflight_block_count"] >= 1
        assert any(
            row["check"] == "MCP review plan" and row["level"] == "block"
            for row in status["ticket_preflight"]
        )


def test_cockpit_can_normalize_raw_robinhood_snapshot_for_reconciliation():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        (data_dir / "robinhood_mcp_snapshot_raw.json").write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "accounts": [
                        {
                            "account_number": "FAKE123456",
                            "nickname": "Agentic",
                            "state": "active",
                            "agentic_allowed": True,
                            "option_level": "option_level_2",
                            "buying_power": 500,
                            "unleveraged_buying_power": 500,
                            "portfolio": {
                                "total_value": 10_000,
                                "buying_power": 500,
                                "unleveraged_buying_power": 500,
                            },
                        }
                    ],
                    "option_positions": {
                        "results": [
                            {
                                "account_number": "FAKE123456",
                                "chain_symbol": "AAPL",
                                "option_type": "call",
                                "strike_price": "200",
                                "expiration_date": "2027-01-15",
                                "quantity": "1",
                                "average_price": "1.25",
                                "mark_price": "1.55",
                            }
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "open_positions.json").write_text(
            json.dumps(
                [
                    {
                        "ticker": "AAPL",
                        "side": "call",
                        "strike": 200,
                        "expiry": "2027-01-15",
                        "quantity": 1,
                        "tracking_scope": "broker_linked",
                    }
                ]
            ),
            encoding="utf-8",
        )

        preview = normalize_robinhood_broker_snapshot_file(data_dir, dry_run=True)
        assert preview["ok"] is True
        assert preview["dry_run"] is True
        assert not (data_dir / "robinhood_broker_snapshot.json").exists()
        assert not (data_dir / EQUITY_LEDGER_DIRNAME).exists()

        result = normalize_robinhood_broker_snapshot_file(data_dir)

        assert result["ok"] is True
        assert result["does_not_place_orders"] is True
        assert result["summary"]["option_positions"] == 1
        assert (data_dir / "robinhood_broker_snapshot.json").exists()
        assert result["equity_ledger_update"]["observations_appended"] == 1
        assert len(list((data_dir / EQUITY_LEDGER_DIRNAME).glob("*.json"))) == 1
        assert result["broker_reconciliation"]["broker_option_count"] == 1
        assert result["broker_reconciliation"]["matched_count"] == 1


def test_broker_reconciliation_surfaces_broker_and_local_mismatches():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        now = datetime.now(UTC).isoformat()
        # Use a known regular exchange session so this test never changes
        # behavior based on the weekday on which the suite is executed.
        expired_expiry = "2026-07-10"
        (data_dir / "open_positions.json").write_text(
            json.dumps(
                [
                    {
                        "ticker": "AAPL",
                        "side": "call",
                        "strike": 200.0,
                        "expiry": "2027-01-15",
                        "current_price": 1.5,
                        "suggested_contracts": 1,
                        "tracking_scope": "broker_linked",
                    },
                    {
                        "ticker": "MSFT",
                        "side": "put",
                        "strike": 300,
                        "expiry": "2027-01-15",
                        "current_price": 2.0,
                        "suggested_contracts": 1,
                        "tracking_scope": "broker_linked",
                    },
                    {
                        "ticker": "GOOG",
                        "side": "call",
                        "strike": 100,
                        "expiry": expired_expiry,
                        "current_price": 0.0,
                        "suggested_contracts": 1,
                        "tracking_scope": "broker_linked",
                    },
                ]
            ),
            encoding="utf-8",
        )
        (data_dir / "agentic_paper_positions.json").write_text(
            json.dumps(
                [
                    {
                        "status": "open",
                        "symbol": "TSLA",
                        "option_side": "call",
                        "strike": "250.00",
                        "expiry": "2027-01-15",
                        "quantity": 1,
                        "entry_price": 1.0,
                    }
                ]
            ),
            encoding="utf-8",
        )
        (data_dir / "robinhood_broker_snapshot.json").write_text(
            json.dumps(
                {
                    "generated_at": now,
                    "accounts": [
                        {
                            "nickname": "Default",
                            "state": "active",
                            "agentic_allowed": False,
                            "option_level": "option_level_2",
                            "buying_power": 500,
                            "unleveraged_buying_power": 500,
                            "portfolio": {
                                "total_value": 10_000,
                                "buying_power": 500,
                                "unleveraged_buying_power": 500,
                            },
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
                            "state": "active",
                            "agentic_allowed": True,
                            "option_level": "",
                            "buying_power": 500,
                            "unleveraged_buying_power": 500,
                            "portfolio": {
                                "total_value": 10_000,
                                "buying_power": 500,
                                "unleveraged_buying_power": 500,
                            },
                            "option_positions": [],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        report = build_broker_reconciliation(data_dir)

        assert report["status"] == "mismatch"
        assert report["broker_option_count"] == 2
        assert report["optedge_option_count"] == 3
        assert report["paper_option_count"] == 1
        assert report["matched_count"] == 1
        assert report["broker_only_count"] == 1
        assert report["local_only_count"] == 1
        assert report["research_lifecycle_option_count"] == 0
        assert report["local_expired_count"] == 1
        assert report["paper_only_count"] == 1
        assert report["agentic_option_ready"] is False
        capabilities = report["robinhood_mcp_capabilities"]
        assert any(
            row["capability"] == "Option chains / contracts / quotes" for row in capabilities
        )
        option_write = next(
            row
            for row in capabilities
            if row["capability"] == "Single-leg option review / placement"
        )
        assert option_write["tool_support"] == "write supported"
        assert option_write["local_policy"] == "explicit approval required"
        assert option_write["account_status"] == "Trusted V2 broker capture required"
        assert any("options-approved account exists" in warning for warning in report["warnings"])
        statuses = {row["contract"]: row["status"] for row in report["rows"]}
        assert statuses["AAPL 2027-01-15 CALL 200"] == "matched"
        assert statuses["ROBN 2026-12-18 CALL 20"] == "broker_only"
        assert statuses["MSFT 2027-01-15 PUT 300"] == "local_only"
        assert statuses[f"GOOG {expired_expiry} CALL 100"] == "local_expired"
        assert statuses["TSLA 2027-01-15 CALL 250"] == "paper_only"


def test_broker_reconciliation_uses_caller_frozen_snapshot_instead_of_rereading():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        now = datetime.now(UTC).isoformat()

        def snapshot(account_key, equity):
            return {
                "schema": cockpit_module.SNAPSHOT_SCHEMA,
                "raw_bundle_schema": cockpit_module.RAW_BUNDLE_SCHEMA,
                "generated_at": now,
                "normalization_blockers": [],
                "accounts": [
                    {
                        "account_key": account_key,
                        "nickname": account_key,
                        "state": "active",
                        "agentic_allowed": True,
                        "option_level": "option_level_2",
                        "portfolio": {
                            "total_value": equity,
                            "buying_power": 1_000,
                            "unleveraged_buying_power": 1_000,
                        },
                    }
                ],
                "option_positions": [],
                "equity_positions": [],
                "option_orders": [],
                "equity_orders": [],
            }

        frozen = snapshot("acct_frozen", 10_000)
        replaced = snapshot("acct_replaced", 100_000)
        (data_dir / "robinhood_broker_snapshot.json").write_text(
            json.dumps(replaced),
            encoding="utf-8",
        )

        report = build_broker_reconciliation(
            data_dir,
            snapshot_override=frozen,
        )

        assert report["account_readiness_rows"][0]["account_key"] == "acct_frozen"
        assert report["account_readiness_rows"][0]["account_equity"] == 10_000
        assert (
            report["snapshot_digest_sha256"]
            == hashlib.sha256(
                json.dumps(
                    frozen,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
        )


def test_broker_reconciliation_keeps_research_lifecycle_out_of_live_mismatch():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        now = datetime.now(UTC).isoformat()
        (data_dir / "open_positions.json").write_text(
            json.dumps(
                [
                    {
                        "ticker": "AAPL",
                        "side": "call",
                        "strike": 200,
                        "expiry": "2099-01-16",
                        "suggested_contracts": 1,
                        "trade_status": "Watch",
                    }
                ]
            ),
            encoding="utf-8",
        )
        (data_dir / "agentic_paper_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "robinhood_broker_snapshot.json").write_text(
            json.dumps(
                {
                    "generated_at": now,
                    "accounts": [],
                    "option_positions": [],
                    "option_orders": [],
                }
            ),
            encoding="utf-8",
        )

        report = build_broker_reconciliation(data_dir)

    assert report["status"] == "empty"
    assert report["broker_option_count"] == 0
    assert report["optedge_option_count"] == 0
    assert report["local_only_count"] == 0
    assert report["research_lifecycle_option_count"] == 1
    assert report["research_lifecycle_active_count"] == 1
    assert any("tracked separately" in value for value in report["warnings"])


def test_agentic_autopilot_blocks_ticket_when_broker_reconciliation_mismatches():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        now = datetime.now(UTC).isoformat()
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
        (data_dir / "robinhood_agentic_queue.json").write_text(
            json.dumps(
                {
                    "status": "ready",
                    "generated_at": now,
                    "orders": [ticket],
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "robinhood_agentic_cycle.json").write_text(
            json.dumps(
                {
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
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "robinhood_live_order_tickets.json").write_text(
            json.dumps(
                {
                    "generated_at": now,
                    "tickets": [ticket],
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "agentic_paper_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "open_positions.json").write_text("[]", encoding="utf-8")
        (data_dir / "robinhood_broker_snapshot.json").write_text(
            json.dumps(
                {
                    "generated_at": now,
                    "accounts": [
                        {
                            "nickname": "Default",
                            "agentic_allowed": False,
                            "option_level": "option_level_2",
                            "option_positions": [
                                {
                                    "chain_symbol": "ROBN",
                                    "option_type": "call",
                                    "strike_price": "20",
                                    "expiration_date": "2026-12-18",
                                    "quantity": "2.0000",
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

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
        now = datetime.now(UTC).isoformat()
        expired_expiry = (datetime.now(UTC) - timedelta(days=2)).date().isoformat()
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
        (data_dir / "robinhood_broker_snapshot.json").write_text(
            json.dumps(
                {
                    "generated_at": now,
                    "accounts": [
                        {
                            "nickname": "Default",
                            "agentic_allowed": False,
                            "option_level": "option_level_2",
                            "option_positions": [
                                {
                                    "chain_symbol": "ROBN",
                                    "option_type": "call",
                                    "strike_price": "35",
                                    "expiration_date": "2026-12-18",
                                    "quantity": "2.0000",
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        report = build_position_hygiene(data_dir)

        assert report["status"] == "needs_review"
        assert report["broker_only_count"] == 1
        assert report["local_expired_count"] == 1
        assert report["local_only_count"] == 0
        assert report["research_lifecycle_option_count"] == 2
        actions = {row["contract"]: row["action"] for row in report["rows"]}
        assert actions["ROBN 2026-12-18 CALL 35"] == "import_or_mark_unmanaged_broker_position"
        assert actions[f"AAPL {expired_expiry} CALL 200"] == "close_or_archive_expired_local_record"
        assert actions["MSFT 2027-01-15 PUT 300"] == "refresh_quote_or_exit_review"
        assert (
            json.loads((data_dir / "open_positions.json").read_text(encoding="utf-8"))
            == original_open
        )

        written = write_position_hygiene_plan(data_dir)
        assert written["wrote_file"] is True
        assert (data_dir / "position_hygiene_plan.json").exists()
        saved = json.loads((data_dir / "position_hygiene_plan.json").read_text(encoding="utf-8"))
        assert saved["action_count"] == report["action_count"]


def test_position_hygiene_apply_preview_does_not_mutate_positions():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        expired_expiry = (datetime.now(UTC) - timedelta(days=2)).date().isoformat()
        future_expiry = (datetime.now(UTC) + timedelta(days=120)).date().isoformat()
        open_rows = [
            {
                "ticker": "AAPL",
                "side": "call",
                "strike": 200,
                "expiry": expired_expiry,
                "entry_time": (datetime.now(UTC) - timedelta(days=20)).isoformat(),
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

        report = apply_position_hygiene(
            data_dir,
            apply=False,
            fetch_expiry_history=False,
        )

        assert report["status"] == "preview"
        assert report["expired_to_close_count"] == 1
        assert report["open_before"] == 2
        assert report["open_after"] == 2
        assert report["closed_before"] == 1
        assert report["closed_after"] == 1
        assert report["backup_paths"] == []
        assert report["rows"][0]["action"] == "preview_move_to_closed_positions"
        assert (
            json.loads((data_dir / "open_positions.json").read_text(encoding="utf-8")) == open_rows
        )
        assert (
            json.loads((data_dir / "closed_positions.json").read_text(encoding="utf-8"))
            == closed_rows
        )
        assert list(data_dir.glob("*.hygiene_backup_*.json")) == []


def test_position_hygiene_apply_backs_up_and_moves_only_expired_options():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        # A fixed regular exchange session avoids weekday-dependent provenance.
        expired_expiry = "2026-07-10"
        future_expiry = (datetime.now(UTC) + timedelta(days=120)).date().isoformat()
        open_rows = [
            {
                "ticker": "AAPL",
                "side": "call",
                "strike": 200,
                "expiry": expired_expiry,
                "entry_time": (datetime.now(UTC) - timedelta(days=20)).isoformat(),
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

        def fake_history(_ticker, period="1y", interval="1d", cache_age=3600):
            frame = pd.DataFrame(
                {"Close": [205.0]},
                index=pd.to_datetime([expired_expiry], utc=True),
            )
            frame.attrs["history_source"] = "test_history"
            frame.attrs["history_quality"] = "observed_test"
            return frame

        report = apply_position_hygiene(
            data_dir,
            apply=True,
            history_fetcher=fake_history,
        )

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
        assert closed["exit_reason"] == "expired"
        assert closed["exit_price"] == 5.0
        assert closed["pnl_pct"] == 4.0
        assert closed["trade_status"] == "Closed"
        assert closed["hygiene_source"] == "position_hygiene"
        assert closed["expiry_close_price_source"] == "intrinsic_proxy_from_underlying_expiry_close"
        assert report["unresolved_expiry_count"] == 0
        assert "broker orders" in report["notes"][-1]


def test_position_hygiene_rolls_back_if_second_lifecycle_write_fails():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        expired_expiry = (datetime.now(UTC) - timedelta(days=2)).date().isoformat()
        open_rows = [
            {
                "ticker": "AAPL",
                "side": "call",
                "strike": 200,
                "expiry": expired_expiry,
                "entry_price": 1.0,
            }
        ]
        closed_rows = [{"ticker": "OLD", "exit_reason": "hard_target"}]
        open_path = data_dir / "open_positions.json"
        closed_path = data_dir / "closed_positions.json"
        open_path.write_text(json.dumps(open_rows), encoding="utf-8")
        closed_path.write_text(json.dumps(closed_rows), encoding="utf-8")
        original_open = open_path.read_bytes()
        original_closed = closed_path.read_bytes()
        original_writer = cockpit_module._atomic_json_list_write

        def fail_open_write(path, rows):
            if path == open_path:
                raise OSError("simulated second write failure")
            return original_writer(path, rows)

        cockpit_module._atomic_json_list_write = fail_open_write
        try:
            report = apply_position_hygiene(
                data_dir,
                apply=True,
                fetch_expiry_history=False,
            )
        finally:
            cockpit_module._atomic_json_list_write = original_writer

        assert report["status"] == "failed"
        assert report["wrote_files"] is False
        assert "rolled back" in report["error"]
        assert open_path.read_bytes() == original_open
        assert closed_path.read_bytes() == original_closed


def test_position_hygiene_blocks_malformed_existing_history():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        open_path = data_dir / "open_positions.json"
        closed_path = data_dir / "closed_positions.json"
        open_path.write_text("[]", encoding="utf-8")
        malformed = b'{"not": "a closed-position list"}'
        closed_path.write_bytes(malformed)

        report = apply_position_hygiene(data_dir, apply=True)

        assert report["status"] == "blocked"
        assert report["wrote_files"] is False
        assert "not a valid JSON list" in report["error"]
        assert closed_path.read_bytes() == malformed
        assert json.loads(open_path.read_text(encoding="utf-8")) == []


def test_agentic_autopilot_paper_book_marks_targets_and_missing_quotes():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        now = datetime.now(UTC).isoformat()
        future_expiry = (datetime.now(UTC) + timedelta(days=180)).date().isoformat()
        (data_dir / "robinhood_agentic_queue.json").write_text(
            json.dumps(
                {
                    "status": "empty",
                    "generated_at": now,
                    "orders": [],
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "robinhood_agentic_cycle.json").write_text(
            json.dumps(
                {
                    "generated_at": now,
                    "auto_submit_allowed": False,
                    "entry_gate": {
                        "status": "blocked",
                        "label": "Fresh entries blocked",
                        "new_entries_allowed_after_live_checks": False,
                        "blockers": ["validation blocked"],
                        "warnings": [],
                    },
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "robinhood_live_order_tickets.json").write_text(
            json.dumps(
                {
                    "generated_at": now,
                    "tickets": [],
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "agentic_paper_positions.json").write_text(
            json.dumps(
                [
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
                ]
            ),
            encoding="utf-8",
        )
        (data_dir / "open_positions.json").write_text(
            json.dumps(
                [
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
                ]
            ),
            encoding="utf-8",
        )

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
        now = datetime.now(UTC).isoformat()
        future_expiry = (datetime.now(UTC) + timedelta(days=180)).date().isoformat()
        (data_dir / "robinhood_agentic_queue.json").write_text(
            json.dumps(
                {
                    "status": "empty",
                    "generated_at": now,
                    "orders": [],
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "robinhood_agentic_cycle.json").write_text(
            json.dumps(
                {
                    "generated_at": now,
                    "auto_submit_allowed": False,
                    "entry_gate": {
                        "status": "blocked",
                        "label": "Fresh entries blocked",
                        "new_entries_allowed_after_live_checks": False,
                        "blockers": [],
                        "warnings": [],
                    },
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "robinhood_live_order_tickets.json").write_text(
            json.dumps(
                {
                    "generated_at": now,
                    "tickets": [],
                }
            ),
            encoding="utf-8",
        )
        (data_dir / "agentic_paper_positions.json").write_text(
            json.dumps(
                [
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
                    }
                ]
            ),
            encoding="utf-8",
        )
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
            "source_quote_at": "2026-06-13T19:58:00+00:00",
            "source_quote_time_basis": "provider_quote_timestamp",
            "source_attempts": [
                {"provider": "cboe", "status": "ok", "rows": 3, "expirations": 2},
            ],
            "expirations": ["2027-01-15", "2026-06-18"],
            "chains": {
                "2027-01-15": pd.DataFrame(
                    [
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
                            "source_quote_at": "2026-06-13T19:59:00+00:00",
                            "source_quote_time_basis": "provider_row.quote_timestamp",
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
                    ]
                ),
                "2026-06-18": pd.DataFrame(
                    [
                        {
                            "strike": 180.0,
                            "side": "put",
                            "bid": 2.00,
                            "ask": 2.10,
                            "lastPrice": 2.05,
                            "volume": 20,
                            "openInterest": 150,
                        },
                    ]
                ),
            },
        }

    try:
        cockpit_module._fetch_option_chain = fake_fetch
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            (data_dir / "open_positions.json").write_text(
                json.dumps(
                    [
                        {
                            "ticker": "AAPL",
                            "side": "call",
                            "strike": 220.0,
                            "expiry": "2027-01-15",
                            "entry_price": 4.0,
                            "current_mid": 5.0,
                            "latest_exit_pressure": 25,
                        }
                    ]
                ),
                encoding="utf-8",
            )
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
    assert report["source_quote_at"] == "2026-06-13T19:58:00+00:00"
    assert report["source_quote_time_basis"] == "provider_quote_timestamp"
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
    wide_reject = [
        row for row in report["rejection_examples"] if row["reason"] == "spread above filter"
    ][0]
    assert wide_reject["strike"] == 300.0
    assert wide_reject["side"] == "call"
    assert wide_reject["premium_dollars"] == 150.0
    row = report["rows"][0]
    assert row["side"] == "call"
    assert row["strike"] == 220.0
    assert row["chain_source"] == "cboe"
    assert row["quote_quality"] == "free_or_delayed"
    assert row["data_delay"] == "delayed"
    assert row["source_quote_at"] == "2026-06-13T19:59:00+00:00"
    assert row["source_quote_time_basis"] == "provider_row.quote_timestamp"
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
            rows = [
                {
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
                }
            ]
        else:
            rows = [
                {
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
                }
            ]
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
                "summary": "2 open AAPL position(s)"
                if symbol == "AAPL"
                else "No open positions found",
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
            "rows": [
                {
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
                }
            ],
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
            "rows": [
                {
                    "generated_at": "2026-06-13T20:00:00+00:00",
                    "source_quote_at": "2026-06-13T19:59:00+00:00",
                    "source_quote_time_basis": "provider_quote_timestamp",
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
                    "delta": 0.67,
                    "confidence": 73,
                    "after_cost_edge_pct": 0.04,
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
                }
            ],
        }

        result = write_option_chain_shortlist(report, data_dir)
        assert result["ok"] is True
        assert result["count"] == 1
        assert (
            artifact_path("option-chain-shortlist", data_dir)
            == data_dir / "option_chain_shortlist.csv"
        )
        assert (
            artifact_path("option-chain-shortlist-json", data_dir)
            == data_dir / "option_chain_shortlist.json"
        )

        csv_text = (data_dir / "option_chain_shortlist.csv").read_text()
        payload = json.loads((data_dir / "option_chain_shortlist.json").read_text())
        assert "AAPL 2027-01-15 C 220" in csv_text
        assert "tight spread; 3m+ swing" in csv_text
        assert payload["count"] == 1
        assert payload["rows"][0]["quote_quality"] == "free_or_delayed"
        assert payload["rows"][0]["generated_at"] == "2026-06-13T20:00:00+00:00"
        assert payload["rows"][0]["source_quote_at"] == "2026-06-13T19:59:00+00:00"
        assert payload["rows"][0]["source_quote_time_basis"] == "provider_quote_timestamp"
        assert payload["rows"][0]["chain_source"] == "cboe"
        assert payload["rows"][0]["confidence"] == 73
        assert payload["rows"][0]["after_cost_edge_pct"] == 0.04
        assert payload["rows"][0]["breakeven_price"] == 225.0
        assert payload["rows"][0]["budget_fit"] == "inside_budget"
        assert payload["rows"][0]["reward_risk_reference"] == 2.0
        assert payload["rows"][0]["open_exposure_count"] == 1
        assert payload["rows"][0]["open_exposure_assets"] == "option:1"
        assert payload["quality_summary"]["status"] == "clean"
        assert payload["quality_summary"]["primary_review_count"] == 1
        assert payload["provider_summary"]["source_counts"] == {"cboe": 1}
        assert result["quality_summary"]["status"] == "clean"
        loaded = cockpit_module._load_option_chain_shortlist(data_dir)
        assert loaded.iloc[0]["confidence"] == 73
        assert loaded.iloc[0]["readiness_score"] == 91
        assert loaded.iloc[0]["after_cost_edge_pct"] == 0.04
        summary = cockpit_module._build_chain_shortlist_summary(data_dir)
        assert summary["quality_summary"]["status"] == "clean"
        assert summary["source_counts"] == {"cboe": 1}
        assert summary["successful_scans"] == 2


def test_option_chain_leaps_preset_overrides_manual_filters_and_summarizes():
    original = cockpit_module._fetch_option_chain
    quote_at = cockpit_module._now_iso()

    def fake_fetch(ticker: str, cache_age: int = 600):
        assert ticker == "AAPL"
        return {
            "spot": 200.0,
            "source": "cboe",
            "quote_quality": "free_or_delayed",
            "source_quote_at": quote_at,
            "source_quote_time_basis": "provider_quote_timestamp",
            "expirations": ["2028-01-21", "2026-08-21"],
            "chains": {
                "2028-01-21": pd.DataFrame(
                    [
                        {
                            "strike": 220.0,
                            "side": "call",
                            "bid": 4.90,
                            "ask": 5.10,
                            "lastPrice": 5.00,
                            "volume": 50,
                            "openInterest": 1000,
                            "delta": 0.65,
                            "confidence": 72,
                            "after_cost_edge_pct": 0.08,
                        },
                        {
                            "strike": 180.0,
                            "side": "put",
                            "bid": 3.10,
                            "ask": 3.30,
                            "lastPrice": 3.20,
                            "volume": 15,
                            "openInterest": 500,
                            "delta": -0.65,
                            "confidence": 70,
                            "after_cost_edge_pct": 0.05,
                        },
                    ]
                ),
                "2026-08-21": pd.DataFrame(
                    [
                        {
                            "strike": 205.0,
                            "side": "call",
                            "bid": 1.00,
                            "ask": 1.05,
                            "lastPrice": 1.02,
                            "volume": 500,
                            "openInterest": 5000,
                        },
                    ]
                ),
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
    assert report["preset_label"] == "LEAPS swing"
    assert report["execution_profile"] == "leaps_swing"
    assert report["strategy_evidence_lane"] == "option_leaps_swing"
    assert report["filters"]["min_dte"] == 365
    assert report["filters"]["max_dte"] == 900
    assert report["filters"]["max_spread_pct"] == 0.10
    assert report["filters"]["max_premium"] == 0.0
    assert report["filters"]["min_open_interest"] == 250
    assert report["filtered_count"] == 2
    assert {row["side"] for row in report["rows"]} == {"call", "put"}
    assert report["scan_summary"]["long_dated_count"] == 2
    assert report["scan_summary"]["best_call"].startswith("C 220")
    assert report["scan_summary"]["best_put"].startswith("P 180")
    assert report["scan_summary"]["review_count"] == 0
    assert report["scan_summary"]["wait_count"] == 2
    assert report["scan_summary"]["profile_status_counts"] == {"research_only": 2}
    assert report["scan_summary"]["leaps_execution_ready_count"] == 0
    assert report["decision"]["status"] == "research_only"
    assert report["decision"]["saveable_count"] == 0
    assert report["decision"]["trade_plan"]["action"] == "watch_only"
    assert report["expiry_summary"][0]["contracts"] == 2
    assert report["expiry_summary"][0]["calls"] == 1
    assert report["expiry_summary"][0]["puts"] == 1
    call = next(row for row in report["rows"] if row["side"] == "call")
    assert call["execution_profile"] == "leaps_swing"
    assert call["strategy_evidence_lane"] == "option_leaps_swing"
    assert call["leaps_swing_status"] == "research_only"
    assert call["leaps_execution_ready"] is False
    assert call["leaps_hard_blockers"] == []
    assert any("delayed" in reason for reason in call["leaps_data_blockers"])
    assert call["review_sessions"] == [3, 5, 10]
    assert call["default_hold_sessions"] == 10
    assert call["max_hold_sessions"] == 20
    assert call["contract_dte_is_not_hold_time"] is True
    assert call["stop_price_reference"] == 3.75
    assert call["target_price_reference"] == 6.75
    assert call["stop_loss_fraction"] == 0.25
    assert call["target_gain_fraction"] == 0.35
    assert call["breakeven_review_trigger_fraction"] == 0.20
    assert call["manual_management_only"] is True


def test_option_chain_long_dated_preset_retains_broad_comparison_window():
    preset, config = cockpit_module._chain_preset_config("long")

    assert preset == "long_dated"
    assert config["label"] == "Broad long-dated research"
    assert config["min_dte"] == 180
    assert config["max_dte"] == 900
    assert config["max_spread_pct"] == 0.25
    assert config["min_open_interest"] == 10
    assert config["execution_profile"] == "swing_execution"


def test_cboe_option_activity_filters_3m_plus_public_contracts():
    old_run = cockpit_module.cboe_symbol_data_engine.run
    old_resolve = cockpit_module.resolve_symbol
    try:
        cockpit_module.resolve_symbol = lambda query: {"symbol": "AAPL", "name": "Apple Inc."}
        cockpit_module.cboe_symbol_data_engine.run = lambda symbols, min_volume=1: pd.DataFrame(
            [
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
            ]
        )

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
        cockpit_module.cboe_symbol_data_engine.run = lambda symbols, min_volume=1: pd.DataFrame(
            [
                {
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
                }
            ]
        )

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
    hist = pd.DataFrame(
        {
            "Open": [10.0, 11.0],
            "High": [11.0, 13.0],
            "Low": [9.0, 10.0],
            "Close": [10.5, 12.5],
            "Volume": [1000, 1500],
        },
        index=idx,
    )

    try:
        cockpit_module.data_provider._yahoo_v8_history = lambda *args, **kwargs: (
            cockpit_module.data_provider._tag_history(
                hist.copy(),
                "yahoo_chart",
                "free_or_delayed",
            )
        )
        cockpit_module.data_provider._nasdaq_history = lambda *args, **kwargs: (
            cockpit_module.data_provider._tag_history(
                hist.copy(),
                "nasdaq_historical",
                "free_or_delayed",
            )
        )
        cockpit_module.data_provider._stooq_history = lambda *args, **kwargs: pd.DataFrame()
        cockpit_module.data_provider.get_history = lambda *args, **kwargs: (
            cockpit_module.data_provider._tag_history(
                hist.copy(),
                "yahoo_chart",
                "free_or_delayed",
            )
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
                "2027-01-15": pd.DataFrame(
                    [
                        {"strike": 200, "side": "call", "bid": 4.9, "ask": 5.1},
                        {"strike": 180, "side": "put", "bid": 3.0, "ask": 3.2},
                    ]
                )
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
    assert (
        report["data_trust"]["option_chain_provider_summary"] == "cboe:ok/2; nasdaq_stocks:warn/0"
    )
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
    assert (
        providers["Option chain stack"]["provider_attempt_summary"]
        == "cboe:ok/2; nasdaq_stocks:warn/0"
    )
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
    hist = pd.DataFrame(
        {
            "Open": [10.0, 11.0],
            "High": [11.0, 13.0],
            "Low": [9.0, 10.0],
            "Close": [10.5, 12.5],
            "Volume": [1000, 1500],
        },
        index=idx,
    )

    try:
        tagged = cockpit_module.data_provider._tag_history(
            hist.copy(), "yahoo_chart", "free_or_delayed"
        )
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
        cockpit_module.halt_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame(
            [
                {
                    "symbol": "RISK",
                    "active_halt": True,
                    "halt_risk_score": 98,
                }
            ]
        )
        cockpit_module.threshold_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame(
            [
                {
                    "symbol": "RISK",
                    "is_threshold": True,
                    "settlement_risk_score": 86,
                }
            ]
        )
        cockpit_module.circuit_rows_for_symbols = lambda *args, **kwargs: pd.DataFrame(
            [
                {
                    "symbol": "RISK",
                    "short_sale_restricted": True,
                    "ssr_risk_score": 82,
                }
            ]
        )
        cockpit_module.sec_ftd_engine.run = lambda *args, **kwargs: pd.DataFrame(
            [
                {
                    "ticker": "RISK",
                    "sec_ftd_score": 1.8,
                    "sec_ftd_latest_date": "2026-06-12",
                    "sec_ftd_fails": 750000,
                    "sec_ftd_dollars": 1800000.0,
                    "sec_ftd_active_days": 3,
                    "sec_ftd_source": "sec_fails_to_deliver",
                }
            ]
        )
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
        "active_halt",
        "regsho_threshold",
        "short_sale_restricted",
        "sec_ftd_pressure",
    }
    assert report["data_trust"]["market_structure_risk_score"] == 98
    assert report["data_trust"]["market_structure_warning_count"] == 4
    assert any("halt" in warning.lower() for warning in report["warnings"])
    providers = {row["provider"]: row for row in report["rows"]}
    assert providers["Nasdaq Trader trade halt RSS"]["status"] == "warn"
    assert providers["Nasdaq Trader trade halt RSS"]["risk_flag_name"] == "active_halt"
    assert providers["Nasdaq Trader Reg SHO threshold list"]["risk_flag_name"] == "regsho_threshold"
    assert (
        providers["Nasdaq Trader short-sale circuit breaker"]["risk_flag_name"]
        == "short_sale_restricted"
    )
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
    hist = pd.DataFrame(
        {
            "Open": [10.0, 11.0],
            "High": [11.0, 13.0],
            "Low": [9.0, 10.0],
            "Close": [10.5, 12.5],
            "Volume": [1000, 1500],
        },
        index=idx,
    )

    try:
        cockpit_module.data_provider._yahoo_v8_history = lambda *args, **kwargs: pd.DataFrame()
        cockpit_module.data_provider._nasdaq_history = lambda *args, **kwargs: pd.DataFrame()
        cockpit_module.data_provider._stooq_history = lambda *args, **kwargs: pd.DataFrame()
        cockpit_module.data_provider.get_history = lambda *args, **kwargs: (
            cockpit_module.data_provider._tag_history(
                hist.copy(),
                "stooq_csv",
                "delayed",
            )
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
        today = datetime.now(UTC).date()
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
                "2099-12-18": pd.DataFrame(
                    [
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
                    ]
                )
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

        pd.DataFrame(
            [
                {
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
                }
            ]
        ).to_parquet(data_dir / "top_options_20260603_120000.parquet")
        (data_dir / "open_positions.json").write_text(
            json.dumps(
                [
                    {
                        "ticker": "NVDA",
                        "side": "call",
                        "strike": 200,
                        "expiry": "2026-06-18",
                        "entry_price": 3.0,
                        "current_mid": 4.5,
                        "unrealized_pct": 0.5,
                    }
                ]
            ),
            encoding="utf-8",
        )
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

        jobs = run_watchlist_scans(
            data_dir, mode="quick", bankroll=25000, aggressive=True, launch=False
        )
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
    test_cockpit_summary_separates_expired_records_from_active_exposure()
    test_cockpit_artifact_path_finds_latest_dashboard()
    test_lookup_history_reads_saved_reports()
    test_lookup_history_surfaces_stale_refresh_leaderboard()
    test_lookup_history_computes_followup_return_from_free_history()
    test_lookup_history_scores_puts_by_bearish_thesis()
    test_manual_review_gate_allows_conservative_user_equity_when_live_math_passes()
    test_manual_review_gate_blocks_incomplete_v2_capture_even_for_shares()
    test_manual_review_gate_blocks_materially_overstated_user_equity()
    test_manual_review_gate_does_not_mix_capacity_across_accounts()
    test_manual_review_gate_preserves_inactive_and_missing_equity_fail_closed()
    test_manual_review_gate_blocks_unresolved_nonterminal_broker_orders()
    test_manual_option_review_blocks_normalized_multi_leg_order_with_planned_second_leg()
    test_manual_review_gate_blocks_legacy_capture_for_shares_and_options()
    test_manual_share_review_blocks_negative_short_position_before_open_long_buy()
    test_manual_option_review_blocks_same_direction_broker_exposure_across_contracts()
    test_manual_option_review_requires_fresh_two_sided_quote_within_spread_cap()
    test_trade_plan_report_calculates_but_blocks_without_local_evidence()
    test_trade_plan_report_builds_manual_review_without_execution_or_automation()
    test_trade_plan_report_blocks_short_option_review()
    test_trade_plan_report_blocks_index_option_types_and_roots()
    test_trade_desk_contract_is_manual_and_versioned()
    test_cockpit_html_contains_lookup_controls()
    test_mutation_requests_require_json_same_origin_and_per_process_token()
    test_lookup_http_routes_require_csrf_json_post_and_get_routes_are_inert()
    test_read_only_watchlist_enrichment_does_not_queue_broker_research()
    test_cockpit_refuses_non_loopback_bindings()
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
    test_validation_guardrail_prefers_independent_swing_metrics()
    test_validation_guardrail_surfaces_fixed_horizon_shadow_evidence()
    test_option_setup_readiness_penalizes_negative_buyer_edge()
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
    test_agentic_autopilot_blocks_legacy_ticket_but_preserves_defensive_preflight()
    test_agentic_autopilot_blocks_stale_unfunded_and_split_broker_state()
    test_agentic_autopilot_blocks_when_live_ticket_lacks_mcp_review_plan()
    test_cockpit_can_normalize_raw_robinhood_snapshot_for_reconciliation()
    test_broker_reconciliation_surfaces_broker_and_local_mismatches()
    test_agentic_autopilot_blocks_ticket_when_broker_reconciliation_mismatches()
    test_position_hygiene_builds_safe_cleanup_plan_without_mutating_positions()
    test_position_hygiene_apply_preview_does_not_mutate_positions()
    test_position_hygiene_apply_backs_up_and_moves_only_expired_options()
    test_position_hygiene_rolls_back_if_second_lifecycle_write_fails()
    test_position_hygiene_blocks_malformed_existing_history()
    test_agentic_autopilot_paper_book_marks_targets_and_missing_quotes()
    test_agentic_autopilot_paper_book_does_not_fake_zero_pnl_without_quotes()
    test_option_chain_scan_fetches_and_filters_contracts()
    test_option_chain_batch_scans_shortlist_and_ranks_contracts()
    test_option_chain_batch_uses_swing_scout_candidates_when_blank()
    test_option_chain_shortlist_writer_creates_portable_artifacts()
    test_option_chain_leaps_preset_overrides_manual_filters_and_summarizes()
    test_option_chain_long_dated_preset_retains_broad_comparison_window()
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
    print("116/116 local cockpit tests passed")
