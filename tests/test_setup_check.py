from __future__ import annotations

import contextlib
import io
import os
from unittest.mock import patch

import setup_check


def test_fred_setup_never_echoes_supplied_secret() -> None:
    secret = "fred-test-secret-value"
    output = io.StringIO()
    previous = os.environ.pop("FRED_API_KEY", None)
    try:
        with patch.object(setup_check.getpass, "getpass", return_value=secret):
            with contextlib.redirect_stdout(output):
                result = setup_check.maybe_setup_fred()

        assert result == secret
        assert os.environ["FRED_API_KEY"] == secret
        assert secret not in output.getvalue()
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
