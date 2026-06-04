"""Background focused-scan jobs for the local cockpit."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.lookup_symbol import DATA_DIR
from scripts.symbol_resolver import resolve_symbol

JOBS_DIRNAME = "cockpit_jobs"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def jobs_dir(data_dir: Path = DATA_DIR) -> Path:
    path = data_dir / JOBS_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def job_path(job_id: str, data_dir: Path = DATA_DIR) -> Path:
    safe = "".join(ch for ch in str(job_id) if ch.isalnum() or ch in {"_", "-"})[:80]
    return jobs_dir(data_dir) / f"{safe}.json"


def job_log_path(job_id: str, data_dir: Path = DATA_DIR) -> Path:
    safe = "".join(ch for ch in str(job_id) if ch.isalnum() or ch in {"_", "-"})[:80]
    return jobs_dir(data_dir) / f"{safe}.log"


def read_job(job_id: str, data_dir: Path = DATA_DIR) -> dict[str, Any] | None:
    path = job_path(job_id, data_dir)
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def read_job_log(job_id: str, data_dir: Path = DATA_DIR, max_lines: int = 80) -> dict[str, Any]:
    path = job_log_path(job_id, data_dir)
    if not path.exists() or not path.is_file():
        return {"job_id": job_id, "log_path": str(path), "lines": []}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        return {"job_id": job_id, "log_path": str(path), "lines": [], "error": str(exc)}
    return {"job_id": job_id, "log_path": str(path), "lines": lines[-max_lines:]}


def write_job(job: dict[str, Any], data_dir: Path = DATA_DIR) -> None:
    path = job_path(str(job["job_id"]), data_dir)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def list_jobs(data_dir: Path = DATA_DIR, limit: int = 20) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(jobs_dir(data_dir).glob("*.json"), key=lambda p: p.stat().st_mtime,
                       reverse=True):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
        if len(rows) >= limit:
            break
    return rows


def _latest_dashboard_after(started_at: datetime, data_dir: Path) -> str | None:
    files = [p for p in data_dir.glob("dashboard_*.html") if p.is_file()]
    files = [p for p in files if datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc) >= started_at]
    if not files:
        return None
    return str(max(files, key=lambda p: p.stat().st_mtime))


def create_job(query: str, data_dir: Path = DATA_DIR, *, launch: bool = True,
               extra_scan_args: list[str] | None = None) -> dict[str, Any]:
    resolution = resolve_symbol(query)
    if not resolution.get("symbol"):
        return {
            "ok": False,
            "error": resolution.get("error") or "could not resolve symbol",
            "resolution": resolution,
        }
    symbol = str(resolution["symbol"]).upper()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    job_id = f"{stamp}_{symbol}"
    job = {
        "ok": True,
        "job_id": job_id,
        "query": query,
        "symbol": symbol,
        "name": resolution.get("name"),
        "resolution": resolution,
        "request": resolution.get("request"),
        "status": "queued",
        "created_at": _now(),
        "updated_at": _now(),
        "log_path": str(job_log_path(job_id, data_dir)),
        "dashboard_path": None,
        "exit_code": None,
    }
    write_job(job, data_dir)
    if launch:
        launcher = [
            sys.executable,
            str(ROOT / "scripts" / "research_jobs.py"),
            "run-job",
            job_id,
            symbol,
            "--data-dir",
            str(data_dir),
        ]
        for arg in extra_scan_args or []:
            launcher.extend(["--scan-arg", arg])
        kwargs: dict[str, Any] = {
            "cwd": str(ROOT),
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(launcher, **kwargs)
    return job


def run_job(job_id: str, symbol: str, data_dir: Path = DATA_DIR,
            extra_scan_args: list[str] | None = None) -> int:
    job = read_job(job_id, data_dir) or {
        "ok": True,
        "job_id": job_id,
        "query": symbol,
        "symbol": symbol,
        "status": "queued",
        "created_at": _now(),
    }
    started = datetime.now(timezone.utc)
    log_path = job_log_path(job_id, data_dir)
    command = [
        sys.executable,
        str(ROOT / "run.py"),
        "--universe",
        symbol,
        "--no-open",
        "--out-dir",
        str(data_dir),
    ]
    command.extend(extra_scan_args or [])
    job.update({
        "status": "running",
        "started_at": started.isoformat(),
        "updated_at": _now(),
        "command": command,
        "log_path": str(log_path),
    })
    write_job(job, data_dir)
    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        log_file.write(f"Optedge focused scan for {symbol}\n")
        if job.get("request"):
            log_file.write("Requested contract: " + json.dumps(job["request"], default=str) + "\n")
        log_file.write("Command: " + " ".join(command) + "\n\n")
        log_file.flush()
        proc = subprocess.run(command, cwd=str(ROOT), stdout=log_file,
                              stderr=subprocess.STDOUT, text=True)
    dashboard = _latest_dashboard_after(started, data_dir)
    job.update({
        "status": "completed" if proc.returncode == 0 else "failed",
        "exit_code": proc.returncode,
        "finished_at": _now(),
        "updated_at": _now(),
        "dashboard_path": dashboard,
    })
    write_job(job, data_dir)
    return int(proc.returncode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage Optedge local research jobs.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run-job")
    run.add_argument("job_id")
    run.add_argument("symbol")
    run.add_argument("--data-dir", default=str(DATA_DIR))
    run.add_argument("--scan-arg", action="append", default=[])
    create = sub.add_parser("create")
    create.add_argument("query")
    create.add_argument("--data-dir", default=str(DATA_DIR))
    create.add_argument("--no-launch", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd == "run-job":
        return run_job(args.job_id, args.symbol, Path(args.data_dir), args.scan_arg)
    if args.cmd == "create":
        print(json.dumps(create_job(args.query, Path(args.data_dir), launch=not args.no_launch),
                         indent=2, default=str))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
