import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.local_cockpit import artifact_path, build_summary, render_cockpit_html


def test_cockpit_summary_counts_open_positions():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        (data_dir / "open_positions.json").write_text(json.dumps([{"ticker": "AAPL"}]))
        (data_dir / "open_share_positions.json").write_text(json.dumps([{"ticker": "NVDA"}]))
        (data_dir / "open_futures_positions.json").write_text(json.dumps([
            {"symbol": "CL=F"},
            {"symbol": "NG=F"},
        ]))
        summary = build_summary(data_dir)
        assert summary["open_counts"] == {"options": 1, "shares": 1, "futures": 2}
        assert summary["total_open"] == 4


def test_cockpit_artifact_path_finds_latest_dashboard():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        old = data_dir / "dashboard_20260101_000000.html"
        new = data_dir / "dashboard_20260102_000000.html"
        old.write_text("old")
        new.write_text("new")
        assert artifact_path("latest-dashboard", data_dir) == new


def test_cockpit_html_contains_lookup_controls():
    html = render_cockpit_html()
    assert "Optedge Local Cockpit" in html
    assert "Symbol lookup" in html
    assert "/api/lookup" in html
    assert "Run focused scan" in html
    assert "/api/run-symbol" in html
    assert "/api/job-log" in html


if __name__ == "__main__":
    test_cockpit_summary_counts_open_positions()
    test_cockpit_artifact_path_finds_latest_dashboard()
    test_cockpit_html_contains_lookup_controls()
    print("3/3 local cockpit tests passed")
