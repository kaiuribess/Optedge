import sys
import tempfile
from datetime import datetime as real_datetime
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.research_jobs as jobs_module
from scripts.research_jobs import (
    create_job, create_refresh_job, job_dashboard_path, job_log_path, job_lookup_path,
    list_jobs, read_job, read_job_log, run_job, run_refresh_job, write_job,
)


def test_create_job_resolves_ticker_without_launching():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        job = create_job("nvda", data_dir, launch=False)
        assert job["ok"] is True
        assert job["symbol"] == "NVDA"
        stored = read_job(job["job_id"], data_dir)
        assert stored is not None
        assert stored["status"] == "queued"


def test_list_jobs_returns_recent_jobs():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        create_job("AAPL", data_dir, launch=False)
        create_job("MSFT", data_dir, launch=False)
        jobs = list_jobs(data_dir)
        assert len(jobs) == 2
        assert {j["symbol"] for j in jobs} == {"AAPL", "MSFT"}


def test_create_job_does_not_overwrite_same_second_symbol_jobs():
    class FrozenDateTime:
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 6, 27, 12, 0, 0, tzinfo=tz or timezone.utc)

    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        old_datetime = jobs_module.datetime
        jobs_module.datetime = FrozenDateTime
        try:
            first = create_job("AAPL", data_dir, launch=False)
            second = create_job("Apple 20261218 C 200", data_dir, launch=False)
        finally:
            jobs_module.datetime = old_datetime

        assert first["job_id"] == "20260627_120000_AAPL"
        assert second["job_id"] == "20260627_120000_AAPL_01"
        assert len(list_jobs(data_dir)) == 2
        assert read_job(first["job_id"], data_dir)["query"] == "AAPL"
        assert read_job(second["job_id"], data_dir)["request_label"] == "AAPL 2026-12-18 C 200"


def test_create_refresh_job_without_launching():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        job = create_refresh_job(
            data_dir,
            launch=False,
            extra_scan_args=["--minimal"],
            scan_mode="quick",
        )
        assert job["ok"] is True
        assert job["job_type"] == "market_refresh"
        assert job["symbol"] == "ALL"
        assert job["scan_args"] == ["--minimal"]
        stored = read_job(job["job_id"], data_dir)
        assert stored is not None
        assert stored["status"] == "queued"


def test_create_refresh_job_does_not_overwrite_same_second_jobs():
    class FrozenDateTime:
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 6, 27, 12, 0, 0, tzinfo=tz or timezone.utc)

    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        old_datetime = jobs_module.datetime
        jobs_module.datetime = FrozenDateTime
        try:
            first = create_refresh_job(data_dir, launch=False)
            second = create_refresh_job(data_dir, launch=False)
        finally:
            jobs_module.datetime = old_datetime

        assert first["job_id"] == "20260627_120000_MARKET_REFRESH"
        assert second["job_id"] == "20260627_120000_MARKET_REFRESH_01"
        assert len(list_jobs(data_dir)) == 2


def test_create_job_returns_error_for_empty_query():
    with tempfile.TemporaryDirectory() as td:
        job = create_job("", Path(td), launch=False)
        assert job["ok"] is False
        assert "error" in job


def test_create_job_preserves_option_request_and_reads_log_tail():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        job = create_job("AAPL 20260618 C 200", data_dir, launch=False)
        assert job["request"]["side"] == "call"
        assert job["request"]["expiry"] == "2026-06-18"
        log_path = job_log_path(job["job_id"], data_dir)
        log_path.write_text("\n".join(f"line {i}" for i in range(100)), encoding="utf-8")
        tail = read_job_log(job["job_id"], data_dir, max_lines=3)
        assert tail["lines"] == ["line 97", "line 98", "line 99"]


def test_run_job_writes_lookup_summary_for_requested_option():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        pd.DataFrame([{
            "ticker": "AAPL",
            "side": "call",
            "strike": 200.0,
            "expiry": "2026-06-18",
            "mid": 3.2,
            "confidence": 80,
            "rank_score": 2.0,
            "trade_status": "Trade",
            "chain_source": "tradier",
            "quote_quality": "live_or_broker",
        }]).to_parquet(data_dir / "top_options_20260603_120000.parquet")
        job = create_job("AAPL 20260618 C 200", data_dir, launch=False)

        old_run = jobs_module.subprocess.run
        jobs_module.subprocess.run = lambda *args, **kwargs: SimpleNamespace(returncode=0)
        try:
            assert run_job(job["job_id"], job["symbol"], data_dir) == 0
        finally:
            jobs_module.subprocess.run = old_run

        stored = read_job(job["job_id"], data_dir)
        assert stored["status"] == "completed"
        assert stored["request_label"] == "AAPL 2026-06-18 C 200"
        assert stored["requested_match_status"] == "exact"
        assert stored["requested_match_label"] == "Exact contract found"
        assert stored["requested_match_count"] == 1
        assert stored["requested_match_quality"] == "exact"
        assert stored["requested_match_mid"] == 3.2
        assert stored["requested_match_quote_quality"] == "live_or_broker"
        assert Path(stored["lookup_html_path"]).exists()
        assert Path(stored["lookup_json_path"]).exists()
        assert job_lookup_path(job["job_id"], data_dir) == Path(stored["lookup_html_path"]).resolve()
        log = read_job_log(job["job_id"], data_dir, max_lines=10)
        assert any("Requested contract" in line for line in log["lines"])


def test_run_job_marks_missing_requested_option_contract():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        pd.DataFrame([{
            "ticker": "MSFT",
            "side": "call",
            "strike": 450.0,
            "expiry": "2026-06-18",
            "mid": 2.1,
            "confidence": 75,
            "rank_score": 1.0,
            "trade_status": "Trade",
        }]).to_parquet(data_dir / "top_options_20260603_120000.parquet")
        job = create_job("AAPL 20260618 C 200", data_dir, launch=False)

        old_run = jobs_module.subprocess.run
        jobs_module.subprocess.run = lambda *args, **kwargs: SimpleNamespace(returncode=0)
        try:
            assert run_job(job["job_id"], job["symbol"], data_dir) == 0
        finally:
            jobs_module.subprocess.run = old_run

        stored = read_job(job["job_id"], data_dir)
        assert stored["requested_match_status"] == "missing"
        assert stored["requested_match_label"] == "Requested contract not found"
        assert stored["requested_match_count"] == 0
        assert stored["requested_match_quality"] is None


def test_run_refresh_job_uses_full_market_command_without_ticker():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        job = create_refresh_job(data_dir, launch=False, extra_scan_args=["--minimal"])

        old_run = jobs_module.subprocess.run
        jobs_module.subprocess.run = lambda *args, **kwargs: SimpleNamespace(returncode=0)
        try:
            assert run_refresh_job(job["job_id"], data_dir, ["--minimal"]) == 0
        finally:
            jobs_module.subprocess.run = old_run

        stored = read_job(job["job_id"], data_dir)
        command = stored["command"]
        assert stored["status"] == "completed"
        assert "--no-open" in command
        assert "--out-dir" in command
        assert "--minimal" in command
        assert "--universe" not in command


def test_job_dashboard_path_allows_only_data_dashboard_file():
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td)
        dashboard = data_dir / "dashboard_20260101_000000.html"
        dashboard.write_text("ok", encoding="utf-8")
        job = create_job("NVDA", data_dir, launch=False)
        job["dashboard_path"] = str(dashboard)
        write_job(job, data_dir)
        assert job_dashboard_path(job["job_id"], data_dir) == dashboard.resolve()
        job["dashboard_path"] = str(data_dir / "not_dashboard.html")
        write_job(job, data_dir)
        assert job_dashboard_path(job["job_id"], data_dir) is None


if __name__ == "__main__":
    test_create_job_resolves_ticker_without_launching()
    test_list_jobs_returns_recent_jobs()
    test_create_job_does_not_overwrite_same_second_symbol_jobs()
    test_create_refresh_job_without_launching()
    test_create_refresh_job_does_not_overwrite_same_second_jobs()
    test_create_job_returns_error_for_empty_query()
    test_create_job_preserves_option_request_and_reads_log_tail()
    test_run_job_writes_lookup_summary_for_requested_option()
    test_run_job_marks_missing_requested_option_contract()
    test_run_refresh_job_uses_full_market_command_without_ticker()
    test_job_dashboard_path_allows_only_data_dashboard_file()
    print("11/11 research job tests passed")
