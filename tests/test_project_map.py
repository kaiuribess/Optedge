# Purpose: Keep repository documentation complete and compatibility diagnostics fail-closed.
"""Repository-map, packaging, and diagnostic-surface regression checks."""

from __future__ import annotations

import re
import subprocess
import tomllib
from collections import Counter
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
MAP_PATH = ROOT / "docs" / "PROJECT_MAP.md"
ROW_PATTERN = re.compile(r"^\| `([^`]+)` \| (.+) \|$", re.MULTILINE)


def _tracked_paths() -> set[str]:
    """Return Git-tracked paths or skip when tests run from an archive/wheel."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        pytest.skip("Git is unavailable; project-map coverage requires a checkout")
    if result.returncode != 0 or not result.stdout:
        pytest.skip("project-map coverage requires a Git checkout")
    return {raw.decode("utf-8").replace("\\", "/") for raw in result.stdout.split(b"\0") if raw}


def _map_rows() -> list[tuple[str, str]]:
    text = MAP_PATH.read_text(encoding="utf-8")
    return [
        (path.replace("\\", "/"), purpose.strip()) for path, purpose in ROW_PATTERN.findall(text)
    ]


def test_project_map_covers_every_tracked_path_once() -> None:
    rows = _map_rows()
    paths = [path for path, _ in rows]
    counts = Counter(paths)
    duplicates = sorted(path for path, count in counts.items() if count != 1)
    assert not duplicates, f"project-map paths must appear exactly once: {duplicates}"

    expected = _tracked_paths() | {
        "docs/PROJECT_MAP.md",
        "tests/test_project_map.py",
    }
    missing = sorted(expected.difference(paths))
    assert not missing, f"tracked paths missing from docs/PROJECT_MAP.md: {missing}"


def test_project_map_rows_are_real_and_descriptive() -> None:
    rows = _map_rows()
    missing_on_disk = sorted(path for path, _ in rows if not (ROOT / path).is_file())
    assert not missing_on_disk, f"project-map rows point to missing files: {missing_on_disk}"

    weak = sorted(
        path
        for path, purpose in rows
        if len(purpose) < 18 or purpose.lower() in {"repository file.", "project file."}
    )
    assert not weak, f"project-map descriptions are too weak: {weak}"


def test_legacy_analysis_surfaces_cannot_promote_models() -> None:
    from backtest import alpha_decay
    from engines import backtest_engine, forward_test

    for module in (alpha_decay, backtest_engine, forward_test):
        assert module.EVIDENCE_STATUS.startswith("diagnostic_only_")
        assert module.ELIGIBLE_FOR_MODEL_PROMOTION is False
        assert module.ELIGIBLE_FOR_LIVE_REVIEW is False

    refits = forward_test.refit_all_buckets({})
    assert refits
    assert all(row["mode"] == "diagnostic_only_no_refit" for row in refits.values())
    assert all(row["eligible_for_model_promotion"] is False for row in refits.values())

    missing_bucket = backtest_engine.walk_forward_one_bucket("__missing_diagnostic_bucket__")
    assert missing_bucket["evidence_status"].startswith("diagnostic_only_")
    assert missing_bucket["eligible_for_model_promotion"] is False

    empty = alpha_decay.compute_alpha_decay(pd.DataFrame())
    assert list(empty["evidence_status"].unique()) == []
    assert "eligible_for_model_promotion" in empty.columns


def test_optional_capabilities_are_declared_professionally() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = config["project"]["dependencies"]
    extras = config["project"]["optional-dependencies"]
    assert any(item.startswith("psutil>=") for item in dependencies)
    assert any(item.startswith("torch>=") for item in extras["sentiment"])
    assert any(item.startswith("transformers>=") for item in extras["sentiment"])

    from telemetry import health

    rss_mb = health._mem_rss_mb()
    assert rss_mb is None or rss_mb > 0


def test_current_diagnostic_has_no_obsolete_setup_guidance() -> None:
    source = (ROOT / "engines" / "diagnose.py").read_text(encoding="utf-8").lower()
    for forbidden in ("v16", "--diagnose", "extract optedge", "archive"):
        assert forbidden not in source
