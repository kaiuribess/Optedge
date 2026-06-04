import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.lookup_symbol import lookup_symbol, match_option_request, render_html, save_lookup


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


if __name__ == "__main__":
    test_lookup_reads_open_option_positions()
    test_lookup_reads_open_futures_positions()
    test_lookup_saves_json_and_html()
    test_lookup_matches_requested_option_contract()
    test_option_request_falls_back_to_closest_strike()
    print("5/5 lookup tests passed")
