# Purpose: Verify versioned outbound identity and SEC contact safeguards.
from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import setup_check
from optedge import __version__
from optedge.http_identity import (
    SecContactRequiredError,
    configured_contact,
    outbound_headers,
    outbound_user_agent,
    sec_headers,
    sec_user_agent,
)

ROOT = Path(__file__).resolve().parent.parent
IDENTITY_CALL_SITES = (
    "config.py",
    "setup_check.py",
    "scripts/symbol_resolver.py",
    "scripts/sec_filings.py",
    "engines/buybacks.py",
    "engines/cboe_symbol_data.py",
    "engines/fda_calendar.py",
    "engines/form_144.py",
    "engines/google_trends.py",
    "engines/insider.py",
    "engines/r_options.py",
    "engines/sec_ftd.py",
    "engines/thirteen_f.py",
)


def test_general_identity_is_versioned_and_safe_without_contact() -> None:
    user_agent = outbound_user_agent({})

    assert user_agent == f"Optedge/{__version__}"
    assert "example" not in user_agent.lower()
    assert ".local" not in user_agent.lower()


def test_outbound_call_sites_do_not_restore_stale_or_fake_identities() -> None:
    forbidden = ("optedge-research/", "contact@example", "@optedge.local", "local@example")

    for relative_path in IDENTITY_CALL_SITES:
        source = (ROOT / relative_path).read_text(encoding="utf-8").lower()
        assert not any(marker in source for marker in forbidden), relative_path


def test_contact_is_used_only_for_sec_identity() -> None:
    env = {"OPTEDGE_CONTACT": "operator@real-domain.dev"}

    assert configured_contact(env) == "operator@real-domain.dev"
    assert outbound_user_agent(env) == f"Optedge/{__version__}"
    assert sec_user_agent(env) == f"Optedge/{__version__} (operator@real-domain.dev)"
    assert outbound_headers(environ=env)["User-Agent"] == outbound_user_agent(env)
    assert sec_headers(environ=env)["User-Agent"] == sec_user_agent(env)


@pytest.mark.parametrize(
    "placeholder",
    [
        "contact@example.com",
        "research@optedge.local",
        "operator@localhost",
        "person@domain.invalid",
        "person@domain.test",
    ],
)
def test_sec_identity_rejects_placeholder_contacts(placeholder: str) -> None:
    with pytest.raises(SecContactRequiredError, match="requires a real operator email"):
        sec_user_agent({"OPTEDGE_CONTACT": placeholder})


def test_legacy_sec_user_agent_can_supply_a_real_contact_without_overriding_product() -> None:
    env = {"SEC_USER_AGENT": "Research Desk operator@real-domain.dev"}

    assert sec_user_agent(env) == f"Optedge/{__version__} (operator@real-domain.dev)"


def test_sec_headers_include_fair_access_fields() -> None:
    headers = sec_headers(
        accept="application/json",
        host="data.sec.gov",
        environ={"OPTEDGE_CONTACT": "operator@real-domain.dev"},
    )

    assert headers == {
        "User-Agent": f"Optedge/{__version__} (operator@real-domain.dev)",
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
        "Host": "data.sec.gov",
    }


def test_setup_check_stops_before_sec_network_without_contact() -> None:
    output = io.StringIO()
    with patch.dict(os.environ, {}, clear=True), patch("requests.get") as get:
        with contextlib.redirect_stdout(output):
            ok, status = setup_check.check_sec()

    assert ok is False
    assert status == "contact_required"
    assert get.call_count == 0
    assert "requires a real operator email" in output.getvalue()
