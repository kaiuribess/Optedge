import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.research_jobs import create_job, job_log_path, list_jobs, read_job, read_job_log


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


if __name__ == "__main__":
    test_create_job_resolves_ticker_without_launching()
    test_list_jobs_returns_recent_jobs()
    test_create_job_returns_error_for_empty_query()
    test_create_job_preserves_option_request_and_reads_log_tail()
    print("4/4 research job tests passed")
