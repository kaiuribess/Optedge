import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.export_robinhood_agentic_queue import (
    build_queue_from_candidates, render_agent_prompt, write_outputs,
)


def _candidate(**overrides):
    row = {
        "generated_at": "2026-06-11T10:00:00+00:00",
        "asset": "option",
        "ticker_or_symbol": "AAPL",
        "action": "BUY_TO_OPEN",
        "direction": "long_call",
        "quantity": 1,
        "contract": "AAPL 2026-06-18 CALL 200",
        "option_side": "call",
        "strike": 200,
        "expiry": "2026-06-18",
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


def test_queue_rejects_contracts_above_500_budget_caps():
    queue = _queue([
        _candidate(entry_price=2.50, confidence=90, rank_score=9.0),
        _candidate(ticker_or_symbol="MSFT", contract="MSFT 2026-06-18 CALL 500"),
    ])
    symbols = {row["symbol"] for row in queue["orders"]}
    assert symbols == {"MSFT"}
    rejected = [row for row in queue["rejected"] if row["ticker"] == "AAPL"][0]
    assert "premium cap leaves no buyable contracts" in rejected["reasons"]


def test_queue_caps_order_count_and_total_premium():
    queue = _queue(
        [
            _candidate(ticker_or_symbol="AAPL", contract="AAPL 2026-06-18 CALL 200", rank_score=5.0),
            _candidate(ticker_or_symbol="MSFT", contract="MSFT 2026-06-18 CALL 500", rank_score=4.0),
            _candidate(ticker_or_symbol="NVDA", contract="NVDA 2026-06-18 CALL 200", rank_score=3.0),
        ],
        max_orders=2,
        max_total_premium=150,
        max_premium_per_order=100,
    )
    assert len(queue["orders"]) == 2
    assert queue["estimated_total_premium"] == 150.0
    assert any("premium cap leaves no buyable contracts" in row["reasons"] for row in queue["rejected"])


def test_queue_prompt_requires_codex_double_check_and_limit_orders():
    queue = _queue([_candidate()])
    prompt = render_agent_prompt(queue)
    assert "Double-check current Robinhood quotes" in prompt
    assert "BUY_TO_OPEN limit DAY orders only" in prompt
    assert "Do not exceed any max_limit_price" in prompt
    assert "current news" in prompt


def test_queue_write_outputs_json_and_prompt():
    queue = _queue([_candidate()])
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        queue_path, prompt_path = write_outputs(queue, data_dir)
        saved = json.loads(queue_path.read_text(encoding="utf-8"))
        assert saved["schema"] == "optedge_robinhood_agentic_options_queue_v1"
        assert saved["orders"][0]["symbol"] == "AAPL"
        assert "Robinhood Agentic Options Queue" in prompt_path.read_text(encoding="utf-8")


if __name__ == "__main__":
    test_queue_is_options_only()
    test_queue_rejects_contracts_above_500_budget_caps()
    test_queue_caps_order_count_and_total_premium()
    test_queue_prompt_requires_codex_double_check_and_limit_orders()
    test_queue_write_outputs_json_and_prompt()
    print("5/5 robinhood agentic queue tests passed")
