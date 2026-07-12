# Purpose: Test optional engine failure handling.
from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from optedge import orchestrator


def test_optional_engine_dependency_absence_is_logged(caplog) -> None:
    with patch.object(
        orchestrator.importlib,
        "import_module",
        side_effect=ImportError("optional dependency unavailable"),
    ):
        with caplog.at_level(logging.WARNING, logger="optedge"):
            assert orchestrator._load_optional_engine("sample") is None

    assert "optional engine sample is unavailable" in caplog.text


def test_unexpected_optional_engine_import_failure_is_not_swallowed() -> None:
    with patch.object(
        orchestrator.importlib,
        "import_module",
        side_effect=RuntimeError("broken module initialization"),
    ):
        with pytest.raises(RuntimeError, match="broken module initialization"):
            orchestrator._load_optional_engine("sample")
