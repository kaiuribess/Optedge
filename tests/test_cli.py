# Purpose: Test command routing without unintended server startup.
from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import pytest

from optedge.cli import OfflineDemoNetworkError, offline_demo_network

ROOT = Path(__file__).resolve().parents[1]


def _run_help(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "run.py", *args],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=20,
    )


def test_top_level_help_advertises_cockpit() -> None:
    result = _run_help("--help")

    assert result.returncode == 0
    assert "--cockpit" in result.stdout
    assert "Optedge starting" not in result.stdout
    assert "auto-retrain" not in result.stdout


def test_cockpit_help_routes_to_cockpit_parser_without_starting_server() -> None:
    result = _run_help("--cockpit", "--help")

    assert result.returncode == 0
    assert "Run the free local Optedge interactive cockpit" in result.stdout
    assert "Press Ctrl+C to stop" not in result.stdout


def test_offline_demo_network_guard_blocks_and_restores_sockets() -> None:
    original_getaddrinfo = socket.getaddrinfo

    with offline_demo_network():
        with pytest.raises(OfflineDemoNetworkError, match="disabled in --demo mode"):
            socket.getaddrinfo("example.com", 443)

    assert socket.getaddrinfo is original_getaddrinfo
