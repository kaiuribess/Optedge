# Purpose: Test bounded non-live agentic paper orders.
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.auto_agentic_paper import (  # noqa: E402
    LIVE_ORDER_TICKETS_JSON,
    PAPER_ORDERS_JSONL,
    PAPER_POSITIONS_JSON,
    process_agentic_paper,
)
from scripts.export_robinhood_agentic_queue import CYCLE_JSON, KILL_SWITCH, QUEUE_JSON  # noqa: E402


def _write_json(path: Path, payload: dict | list):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _order(symbol: str = "AAPL", strike: float = 200.0) -> dict:
    return {
        "asset": "option",
        "symbol": symbol,
        "ticker_or_symbol": symbol,
        "action": "BUY_TO_OPEN",
        "order_type": "limit",
        "time_in_force": "day",
        "quantity": 1,
        "contract": f"{symbol} 2027-01-15 C {strike:g}",
        "option_side": "call",
        "strike": strike,
        "expiry": "2027-01-15",
        "direction": "long_call",
        "reference_entry_price": 0.75,
        "source_bid": 0.73,
        "source_ask": 0.77,
        "max_limit_price": 0.81,
        "estimated_premium_dollars": 75.0,
        "stop_price_reference": 0.35,
        "target_price_reference": 1.6,
        "confidence": 72,
        "rank_score": 2.1,
        "fused_score": 1.8,
        "swing_fit_score": 91,
        "swing_fit_label": "clean_swing",
        "execution_profile": "leaps_swing",
        "strategy_evidence_lane": "option_leaps_swing",
        "profile_policy_version": "2026.07-leaps-swing-v1",
        "planned_hold_sessions": 10,
        "max_hold_sessions": 20,
        "leaps_execution_ready": True,
        "after_cost_edge_pct": 0.06,
    }


def _write_queue_cycle(
    data_dir: Path, *, gate_open: bool = False, orders: list[dict] | None = None
):
    orders = orders or [_order()]
    generated_at = datetime.now(UTC).isoformat()
    _write_json(
        data_dir / QUEUE_JSON,
        {
            "schema": "optedge_robinhood_agentic_options_queue_v1",
            "status": "ready",
            "generated_at": generated_at,
            "max_orders_to_submit": 0,
            "max_manual_reviews": 2,
            "execution_enabled": False,
            "manual_trade_desk_required": True,
            "orders": orders,
        },
    )
    entry_gate = {
        "status": "open" if gate_open else "blocked",
        "new_entries_allowed_after_live_checks": gate_open,
        "blockers": [] if gate_open else ["validation max drawdown is too high"],
    }
    _write_json(
        data_dir / CYCLE_JSON,
        {
            "schema": "optedge_robinhood_agentic_cycle_v1",
            "generated_at": generated_at,
            "hard_pause": False,
            "entry_gate": entry_gate,
            "entry_candidates": [],
            "manual_review_candidates": orders if gate_open else [],
            "review_only_entry_candidates": [] if gate_open else orders,
        },
    )


def test_default_blocked_gate_opens_no_paper_positions():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        _write_queue_cycle(data_dir, gate_open=False)
        result = process_agentic_paper(data_dir=data_dir)
        assert result["opened_paper_count"] == 0
        assert result["candidate_source"] == "blocked_by_entry_gate"
        assert not (data_dir / PAPER_POSITIONS_JSON).exists()


def test_allow_blocked_paper_opens_review_only_candidate_without_live_ticket():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        _write_queue_cycle(data_dir, gate_open=False)
        result = process_agentic_paper(data_dir=data_dir, allow_blocked_paper=True)
        assert result["opened_paper_count"] == 1
        assert result["candidate_source"] == "review_only_entry_candidates"
        positions = json.loads((data_dir / PAPER_POSITIONS_JSON).read_text(encoding="utf-8"))
        assert positions[0]["symbol"] == "AAPL"
        assert positions[0]["paper_override_validation_gate"] is True
        assert positions[0]["source_reference_price"] == 0.75
        assert positions[0]["entry_price"] == 0.81
        assert positions[0]["estimated_premium_dollars"] == 81.0
        assert positions[0]["paper_fill_assumption"] == "filled_at_full_buy_limit"
        assert positions[0]["paper_fill_slippage_per_contract_dollars"] == 6.0
        assert positions[0]["execution_profile"] == "leaps_swing"
        assert positions[0]["strategy_evidence_lane"] == "option_leaps_swing"
        assert positions[0]["planned_hold_sessions"] == 10
        assert positions[0]["max_hold_sessions"] == 20
        assert positions[0]["leaps_execution_ready"] is True
        assert positions[0]["after_cost_edge_pct"] == 0.06
        assert result["ticket_count"] == 0
        assert any("paper-only" in reason for reason in result["live_ticket_blockers"])
        assert not (data_dir / LIVE_ORDER_TICKETS_JSON).exists()


def test_duplicate_contract_is_skipped():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        order = _order()
        _write_queue_cycle(data_dir, gate_open=True, orders=[order])
        _write_json(
            data_dir / PAPER_POSITIONS_JSON,
            [
                {
                    "status": "open",
                    "symbol": "AAPL",
                    "option_side": "call",
                    "expiry": "2027-01-15",
                    "strike": 200.0,
                    "direction": "long_call",
                }
            ],
        )
        result = process_agentic_paper(data_dir=data_dir)
        assert result["opened_paper_count"] == 0
        assert result["candidate_source"] == "manual_review_candidates"
        assert result["skipped_count"] == 1
        assert "already open" in " ".join(result["skipped"][0]["reasons"])


def test_paper_order_stays_unfilled_when_source_ask_exceeds_limit():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        order = _order()
        order["source_ask"] = 0.90
        _write_queue_cycle(data_dir, gate_open=True, orders=[order])

        result = process_agentic_paper(data_dir=data_dir)

        assert result["opened_paper_count"] == 0
        assert result["skipped_count"] == 1
        assert "no fill is observed" in " ".join(result["skipped"][0]["reasons"])
        assert not (data_dir / PAPER_POSITIONS_JSON).exists()


def test_open_gate_is_still_research_only_and_removes_legacy_live_ticket():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        _write_queue_cycle(data_dir, gate_open=True)
        _write_json(data_dir / LIVE_ORDER_TICKETS_JSON, {"tickets": [_order()]})

        result = process_agentic_paper(data_dir=data_dir)

        assert result["opened_paper_count"] == 1
        assert result["candidate_source"] == "manual_review_candidates"
        assert result["ticket_count"] == 0
        assert any("research/paper-only" in reason for reason in result["live_ticket_blockers"])
        assert not (data_dir / LIVE_ORDER_TICKETS_JSON).exists()


def test_kill_switch_blocks_even_paper_override():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        _write_queue_cycle(data_dir, gate_open=False)
        _write_json(data_dir / LIVE_ORDER_TICKETS_JSON, {"tickets": [_order()]})
        (data_dir / KILL_SWITCH).write_text("stop", encoding="utf-8")
        result = process_agentic_paper(data_dir=data_dir, allow_blocked_paper=True)
        assert result["opened_paper_count"] == 0
        assert result["ticket_count"] == 0
        assert "kill-switch file is present" in result["blockers"]
        assert not (data_dir / LIVE_ORDER_TICKETS_JSON).exists()
        assert (data_dir / PAPER_ORDERS_JSONL).exists()


def test_hard_pause_suppresses_and_removes_live_tickets():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        _write_queue_cycle(data_dir, gate_open=True)
        cycle = json.loads((data_dir / CYCLE_JSON).read_text(encoding="utf-8"))
        cycle["hard_pause"] = True
        cycle["hard_pause_reasons"] = ["broker access uncertain"]
        _write_json(data_dir / CYCLE_JSON, cycle)
        _write_json(data_dir / LIVE_ORDER_TICKETS_JSON, {"tickets": [_order()]})

        result = process_agentic_paper(data_dir=data_dir)

        assert result["opened_paper_count"] == 0
        assert result["ticket_count"] == 0
        assert "broker access uncertain" in result["blockers"]
        assert not (data_dir / LIVE_ORDER_TICKETS_JSON).exists()


def test_missing_queue_and_cycle_timestamps_fail_closed_for_manual_review():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        _write_queue_cycle(data_dir, gate_open=True)
        queue = json.loads((data_dir / QUEUE_JSON).read_text(encoding="utf-8"))
        cycle = json.loads((data_dir / CYCLE_JSON).read_text(encoding="utf-8"))
        queue.pop("generated_at")
        cycle.pop("generated_at")
        _write_json(data_dir / QUEUE_JSON, queue)
        _write_json(data_dir / CYCLE_JSON, cycle)

        result = process_agentic_paper(data_dir=data_dir)

        assert result["ticket_count"] == 0
        assert any("timestamps are required" in reason for reason in result["live_ticket_blockers"])
        assert not (data_dir / LIVE_ORDER_TICKETS_JSON).exists()


def test_stale_queue_and_cycle_timestamps_fail_closed_for_manual_review():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        _write_queue_cycle(data_dir, gate_open=True)
        queue = json.loads((data_dir / QUEUE_JSON).read_text(encoding="utf-8"))
        cycle = json.loads((data_dir / CYCLE_JSON).read_text(encoding="utf-8"))
        stale_at = "2000-01-01T00:00:00+00:00"
        queue["generated_at"] = stale_at
        cycle["generated_at"] = stale_at
        _write_json(data_dir / QUEUE_JSON, queue)
        _write_json(data_dir / CYCLE_JSON, cycle)

        result = process_agentic_paper(data_dir=data_dir)

        assert result["ticket_count"] == 0
        assert any("source is older" in reason for reason in result["live_ticket_blockers"])
        assert not (data_dir / LIVE_ORDER_TICKETS_JSON).exists()


def test_respects_max_orders():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        orders = [_order("AAPL"), _order("MSFT", strike=300)]
        _write_queue_cycle(data_dir, gate_open=True, orders=orders)
        result = process_agentic_paper(data_dir=data_dir, max_orders=1)
        assert result["opened_paper_count"] == 1
        positions = json.loads((data_dir / PAPER_POSITIONS_JSON).read_text(encoding="utf-8"))
        assert len(positions) == 1


if __name__ == "__main__":
    test_default_blocked_gate_opens_no_paper_positions()
    test_allow_blocked_paper_opens_review_only_candidate_without_live_ticket()
    test_duplicate_contract_is_skipped()
    test_paper_order_stays_unfilled_when_source_ask_exceeds_limit()
    test_open_gate_is_still_research_only_and_removes_legacy_live_ticket()
    test_kill_switch_blocks_even_paper_override()
    test_hard_pause_suppresses_and_removes_live_tickets()
    test_missing_queue_and_cycle_timestamps_fail_closed_for_manual_review()
    test_stale_queue_and_cycle_timestamps_fail_closed_for_manual_review()
    test_respects_max_orders()
    print("10/10 auto agentic paper tests passed")
