# Purpose: Build honest versioned identities for Optedge HTTP requests.
"""Build honest, versioned identities for Optedge HTTP requests.

Public data providers use the ``User-Agent`` header to identify clients and,
for the SEC, to contact an automated tool's operator.  Optedge never invents
that contact information.  General requests use a versioned product token;
SEC requests fail closed until the operator supplies a real email address.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping

from optedge import __version__

PRODUCT = "Optedge"
CONTACT_ENV = "OPTEDGE_CONTACT"
LEGACY_SEC_USER_AGENT_ENV = "SEC_USER_AGENT"

_EMAIL_RE = re.compile(
    r"(?<![A-Z0-9._%+-])"
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}"
    r"(?![A-Z0-9._%+-])",
    re.IGNORECASE,
)
_NON_CONTACT_DOMAINS = {
    "example.com",
    "example.net",
    "example.org",
    "localhost",
}
_NON_CONTACT_SUFFIXES = (".example", ".invalid", ".local", ".localhost", ".test")


class SecContactRequiredError(RuntimeError):
    """Raised before an SEC request when no honest operator contact is configured."""


def _environment(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def _extract_real_email(value: object) -> str | None:
    """Return a plausible operator email, excluding documented placeholder domains."""
    match = _EMAIL_RE.search(str(value or "").strip())
    if not match:
        return None
    email = match.group(0)
    domain = email.rsplit("@", 1)[-1].lower().rstrip(".")
    if domain in _NON_CONTACT_DOMAINS or domain.endswith(_NON_CONTACT_SUFFIXES):
        return None
    return email


def configured_contact(environ: Mapping[str, str] | None = None) -> str | None:
    """Read a real contact email without ever substituting sample data.

    ``OPTEDGE_CONTACT`` is the canonical setting.  A real email embedded in
    the older ``SEC_USER_AGENT`` setting remains supported for compatibility,
    but the final header is still normalized to the current Optedge version.
    """
    env = _environment(environ)
    preferred = str(env.get(CONTACT_ENV) or "").strip()
    if preferred:
        return _extract_real_email(preferred)
    legacy = str(env.get(LEGACY_SEC_USER_AGENT_ENV) or "").strip()
    if legacy:
        return _extract_real_email(legacy)
    return None


def outbound_user_agent(environ: Mapping[str, str] | None = None) -> str:
    """Return a versioned product identity without leaking the SEC contact."""
    del environ
    return f"{PRODUCT}/{__version__}"


def sec_user_agent(environ: Mapping[str, str] | None = None) -> str:
    """Return a declared SEC identity or fail before making the request."""
    contact = configured_contact(environ)
    if not contact:
        raise SecContactRequiredError(
            "SEC automated access requires a real operator email. Set "
            f"{CONTACT_ENV} (preferred) or provide a real email in "
            f"{LEGACY_SEC_USER_AGENT_ENV}; placeholder/example/.local addresses are rejected."
        )
    return f"{PRODUCT}/{__version__} ({contact})"


def outbound_headers(
    *,
    accept: str | None = None,
    accept_encoding: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a fresh header mapping for a non-SEC provider."""
    headers = {"User-Agent": outbound_user_agent(environ)}
    if accept:
        headers["Accept"] = accept
    if accept_encoding:
        headers["Accept-Encoding"] = accept_encoding
    return headers


def sec_headers(
    *,
    accept: str | None = None,
    accept_encoding: str = "gzip, deflate",
    host: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build SEC fair-access headers after validating operator contact."""
    headers = {"User-Agent": sec_user_agent(environ)}
    if accept:
        headers["Accept"] = accept
    if accept_encoding:
        headers["Accept-Encoding"] = accept_encoding
    if host:
        headers["Host"] = host
    return headers
