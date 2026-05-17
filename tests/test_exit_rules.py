import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest import exit_rules


def test_exit_pressure_blocks_on_research_guard():
    review = exit_rules.compute_exit_pressure(
        {"ticker": "XYZ", "research_guard_status": "blocked_spread", "stop_price": 8, "entry_price": 10},
        asset="share",
    )
    assert review["action"] == "close_early"
    assert "research guard blocked" in review["reasons"]


def test_exit_pressure_tightens_on_confidence_collapse():
    review = exit_rules.compute_exit_pressure(
        {"ticker": "XYZ", "confidence": 80, "stop_price": 8, "entry_price": 10, "current_price": 12, "unrealized_pct": 0.2},
        {"confidence": 50},
        asset="share",
    )
    assert review["action"] in {"tighten_stop", "close_early"}


def test_dynamic_exit_logging_writes_jsonl():
    with tempfile.TemporaryDirectory() as td:
        old_data = exit_rules.DATA_DIR
        old_file = exit_rules.EXIT_REVIEWS_FILE
        exit_rules.DATA_DIR = Path(td)
        exit_rules.EXIT_REVIEWS_FILE = Path(td) / "exit_reviews.jsonl"
        try:
            exit_rules.log_exit_review({"asset": "share", "action": "hold"})
            rows = [json.loads(x) for x in exit_rules.EXIT_REVIEWS_FILE.read_text().splitlines()]
            assert rows[0]["asset"] == "share"
        finally:
            exit_rules.DATA_DIR = old_data
            exit_rules.EXIT_REVIEWS_FILE = old_file


if __name__ == "__main__":
    test_exit_pressure_blocks_on_research_guard()
    test_exit_pressure_tightens_on_confidence_collapse()
    test_dynamic_exit_logging_writes_jsonl()
    print("3/3 exit rules tests passed")
