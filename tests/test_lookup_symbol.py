import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.lookup_symbol import lookup_symbol, render_html, save_lookup


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
        report = lookup_symbol("MISSING", data_dir)
        paths = save_lookup(report, data_dir)
        assert paths["json"].exists()
        assert paths["html"].exists()
        assert "Optedge Lookup" in render_html(report)


if __name__ == "__main__":
    test_lookup_reads_open_option_positions()
    test_lookup_reads_open_futures_positions()
    test_lookup_saves_json_and_html()
    print("3/3 lookup tests passed")
