import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.auto_agentic_paper import (
    LIVE_ORDER_TICKETS_JSON,
    PAPER_ORDERS_JSONL,
    PAPER_POSITIONS_JSON,
    process_agentic_paper,
)
from scripts.export_robinhood_agentic_queue import CYCLE_JSON, KILL_SWITCH, QUEUE_JSON


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
        "max_limit_price": 0.81,
        "estimated_premium_dollars": 75.0,
        "stop_price_reference": 0.35,
        "target_price_reference": 1.6,
        "confidence": 72,
        "rank_score": 2.1,
        "fused_score": 1.8,
        "swing_fit_score": 91,
        "swing_fit_label": "clean_swing",
    }


def _write_queue_cycle(data_dir: Path, *, gate_open: bool = False, orders: list[dict] | None = None):
    orders = orders or [_order()]
    _write_json(data_dir / QUEUE_JSON, {
        "schema": "optedge_robinhood_agentic_options_queue_v1",
        "status": "ready",
        "max_orders_to_submit": 2,
        "orders": orders,
    })
    entry_gate = {
        "status": "open" if gate_open else "blocked",
        "new_entries_allowed_after_live_checks": gate_open,
        "blockers": [] if gate_open else ["validation max drawdown is too high"],
    }
    _write_json(data_dir / CYCLE_JSON, {
        "schema": "optedge_robinhood_agentic_cycle_v1",
        "hard_pause": False,
        "entry_gate": entry_gate,
        "entry_candidates": orders if gate_open else [],
        "review_only_entry_candidates": [] if gate_open else orders,
    })


def test_default_blocked_gate_opens_no_paper_positions():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        _write_queue_cycle(data_dir, gate_open=False)
        result = process_agentic_paper(data_dir=data_dir)
        assert result["opened_paper_count"] == 0
        assert result["candidate_source"] == "blocked_by_entry_gate"
        assert not (data_dir / PAPER_POSITIONS_JSON).exists()


def test_allow_blocked_paper_opens_review_only_candidate_and_live_ticket():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        _write_queue_cycle(data_dir, gate_open=False)
        result = process_agentic_paper(data_dir=data_dir, allow_blocked_paper=True)
        assert result["opened_paper_count"] == 1
        assert result["candidate_source"] == "review_only_entry_candidates"
        positions = json.loads((data_dir / PAPER_POSITIONS_JSON).read_text(encoding="utf-8"))
        assert positions[0]["symbol"] == "AAPL"
        assert positions[0]["paper_override_validation_gate"] is True
        tickets = json.loads((data_dir / LIVE_ORDER_TICKETS_JSON).read_text(encoding="utf-8"))
        assert tickets["tickets"][0]["confirmation_required"] is True
        assert tickets["tickets"][0]["live_submit_allowed_by_this_script"] is False
        assert tickets["broker_mcp_review_supported"] is True
        plan = tickets["tickets"][0]["robinhood_mcp_review_plan"]
        assert plan["review_tool"] == "review_option_order"
        assert plan["place_tool_after_explicit_confirmation"] == "place_option_order"
        assert plan["requires_explicit_user_confirmation_before_place"] is True
        assert plan["contract_lookup"]["chain_symbol"] == "AAPL"
        assert plan["contract_lookup"]["expiration_date"] == "2027-01-15"
        assert plan["review_arguments_template"]["price"] == "0.81"
        assert plan["review_arguments_template"]["legs"][0]["side"] == "buy"


def test_duplicate_contract_is_skipped():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        order = _order()
        _write_queue_cycle(data_dir, gate_open=True, orders=[order])
        _write_json(data_dir / PAPER_POSITIONS_JSON, [{
            "status": "open",
            "symbol": "AAPL",
            "option_side": "call",
            "expiry": "2027-01-15",
            "strike": 200.0,
            "direction": "long_call",
        }])
        result = process_agentic_paper(data_dir=data_dir)
        assert result["opened_paper_count"] == 0
        assert result["skipped_count"] == 1
        assert "already open" in " ".join(result["skipped"][0]["reasons"])


def test_kill_switch_blocks_even_paper_override():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        _write_queue_cycle(data_dir, gate_open=False)
        (data_dir / KILL_SWITCH).write_text("stop", encoding="utf-8")
        result = process_agentic_paper(data_dir=data_dir, allow_blocked_paper=True)
        assert result["opened_paper_count"] == 0
        assert "kill-switch file is present" in result["blockers"]
        assert (data_dir / PAPER_ORDERS_JSONL).exists()


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
    test_allow_blocked_paper_opens_review_only_candidate_and_live_ticket()
    test_duplicate_contract_is_skipped()
    test_kill_switch_blocks_even_paper_override()
    test_respects_max_orders()
    print("5/5 auto agentic paper tests passed")
