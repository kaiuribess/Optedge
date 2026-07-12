# Purpose: Test keyless FRED setup and secret protection.
from __future__ import annotations

import contextlib
import io
import os
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
