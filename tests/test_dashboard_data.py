import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard import build as dashboard_build


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
            assert "All open positions (2)" in html
            assert "AAPL C 280 06-18" in html
            assert "AAPL C 285 06-18" in html
        finally:
            dashboard_build.ROOT = old_root


if __name__ == "__main__":
    test_dashboard_helpers_dedupe_and_label_positions()
    test_dashboard_analytics_uses_pnl_wins_and_unique_open_labels()
    print("2/2 dashboard data tests passed")
