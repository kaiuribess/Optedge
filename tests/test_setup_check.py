# Purpose: Test keyless FRED setup and secret protection.
from __future__ import annotations

import contextlib
import io
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import setup_check


def test_missing_fred_key_uses_keyless_fallback_without_prompting() -> None:
    output = io.StringIO()
    previous = os.environ.pop("FRED_API_KEY", None)
    try:
        with contextlib.redirect_stdout(output):
            result = setup_check.maybe_setup_fred()

        assert result == ""
        assert "FRED_API_KEY" not in os.environ
        assert "Keyless FRED CSV fallback is available" in output.getvalue()
        assert "Never paste" in output.getvalue()
    finally:
        if previous is None:
            os.environ.pop("FRED_API_KEY", None)
        else:
            os.environ["FRED_API_KEY"] = previous


def test_existing_fred_secret_is_not_partially_printed() -> None:
    secret = "abcdef-super-secret"
    output = io.StringIO()

    with patch.dict(os.environ, {"FRED_API_KEY": secret}):
        with contextlib.redirect_stdout(output):
            result = setup_check.maybe_setup_fred()

    assert result == secret
    assert secret not in output.getvalue()
    assert "abcdef" not in output.getvalue()
    assert "value hidden" in output.getvalue()


def test_offline_setup_checks_packages_without_touching_providers_or_status() -> None:
    output = io.StringIO()
    with tempfile.TemporaryDirectory() as td:
        status_path = Path(td) / ".optedge_status.json"
        with (
            patch.object(setup_check, "STATUS_FILE", status_path),
            patch.object(setup_check, "check_python", return_value=True),
            patch.object(setup_check, "check_packages", return_value=True),
            patch.object(setup_check, "check_yfinance", side_effect=AssertionError("network")),
            patch.object(setup_check, "check_reddit", side_effect=AssertionError("network")),
            patch.object(setup_check, "check_sec", side_effect=AssertionError("network")),
            patch.object(setup_check, "check_macro", side_effect=AssertionError("network")),
            contextlib.redirect_stdout(output),
        ):
            result = setup_check.main(["--offline"])

        assert result == 0
        assert not status_path.exists()
        assert "no network requests were made" in output.getvalue()
