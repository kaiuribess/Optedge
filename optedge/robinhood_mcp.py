# Purpose: Provide the official, approval-gated Robinhood MCP client foundation.
"""Official Robinhood Trading MCP client primitives.

This module exposes only a fixed option-placement call for a higher-level,
single-use confirmation workflow.  It has no generic tool-call or generic
placement method; the dashboard must preview, revalidate, and confirm one exact
order before this narrow boundary can be reached.

OAuth grants and dynamic client registration data are stored only through the
operating-system keyring.  There is no file, environment-variable, or plaintext
fallback.  Network calls are one-shot user actions: this module contains no
polling loop, scheduler, retry loop, or background order activity.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import secrets
import sys
import threading
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any
from urllib.parse import parse_qs, urlparse, urlunparse

import httpx
import jsonschema
import keyring
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyUrl

ROBINHOOD_MCP_ENDPOINT = "https://agent.robinhood.com/mcp/trading"
DEFAULT_CALLBACK_PATH = "/oauth/robinhood/callback"
DEFAULT_KEYRING_SERVICE = "Optedge Robinhood MCP"
TOKEN_KEYRING_USERNAME = "oauth-token-v1"
CLIENT_INFO_KEYRING_USERNAME = "oauth-client-registration-v1"
MAX_TOOL_PAGES = 100

# Windows Credential Manager caps one generic credential blob at 2,560 bytes.
# python-keyring passes strings through the Unicode WinCred API, so keep each
# vault value well below that ceiling and use a committed manifest for larger
# OAuth envelopes.  Every chunk remains an OS-vault credential; there is no
# filesystem fallback.
_KEYRING_CHUNK_SCHEMA = "optedge_keyring_chunks_v1"
_KEYRING_CHUNK_CHAR_LIMIT = 900
_KEYRING_MAX_CHUNKS = 64
_KEYRING_GENERATION_RE = re.compile(r"^[0-9a-f]{16}$")
_KEYRING_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

_WINDOWS_OS_KEYRING_BACKENDS = frozenset(
    {
        ("keyring.backends.Windows", "WinVaultKeyring"),
    }
)
_MACOS_OS_KEYRING_BACKENDS = frozenset(
    {
        ("keyring.backends.macOS", "Keyring"),
    }
)
_LINUX_OS_KEYRING_BACKENDS = frozenset(
    {
        ("keyring.backends.SecretService", "Keyring"),
        ("keyring.backends.libsecret", "Keyring"),
        ("keyring.backends.kwallet", "DBusKeyring"),
        ("keyring.backends.kwallet", "DBusKeyringKWallet4"),
    }
)

READ_TOOL_ALLOWLIST = frozenset(
    {
        "get_accounts",
        "get_portfolio",
        "get_equity_positions",
        "get_option_positions",
        "get_equity_orders",
        "get_option_orders",
        "get_equity_quotes",
        "get_equity_fundamentals",
        "get_equity_historicals",
        "get_equity_tradability",
        "get_earnings_calendar",
        "get_earnings_results",
        "get_indexes",
        "get_index_quotes",
        "get_option_chains",
        "get_option_instruments",
        "get_option_quotes",
        "get_option_historicals",
        "get_realized_pnl",
        "get_pnl_trade_history",
        "get_scans",
        "run_scan",
        "search",
    }
)

REQUIRED_PREFLIGHT_READ_TOOLS = frozenset(
    {
        "get_accounts",
        "get_portfolio",
        "get_equity_positions",
        "get_option_positions",
        "get_equity_orders",
        "get_option_orders",
        "get_equity_quotes",
        "get_equity_tradability",
        "get_option_chains",
        "get_option_instruments",
        "get_option_quotes",
    }
)

REVIEW_TOOL_ALLOWLIST = frozenset(
    {
        "review_equity_order",
        "review_option_order",
    }
)

PLACE_TOOL_ALLOWLIST = frozenset(
    {
        "place_equity_order",
        "place_option_order",
    }
)

_ACCOUNT_NUMBER_KEYS = frozenset(
    {
        "account_number",
        "rhs_account_number",
        "brokerage_account_number",
    }
)
_EXACT_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "authorization",
        "authorization_code",
        "authorization_url",
        "callback_code",
        "client_secret",
        "code_verifier",
        "cookie",
        "cookies",
        "mfa_code",
        "oauth_state",
        "password",
        "pkce_verifier",
        "state",
        "token",
    }
)


class RobinhoodMcpError(RuntimeError):
    """Base exception whose messages are safe to show in the local cockpit."""


class CallbackUriError(RobinhoodMcpError):
    """Raised when the OAuth callback is not an exact loopback URI."""


class CredentialStorageError(RobinhoodMcpError):
    """Raised when secure OS credential storage is unavailable or malformed."""


class OAuthStateError(RobinhoodMcpError):
    """Raised when an OAuth redirect or callback fails the one-time state gate."""


class ToolCatalogError(RobinhoodMcpError):
    """Raised for duplicate, malformed, incomplete, or cyclic MCP tool pages."""


class ToolPolicyError(RobinhoodMcpError):
    """Raised when a caller asks for a tool outside its narrow method policy."""


class ToolSchemaError(RobinhoodMcpError):
    """Raised when arguments do not satisfy the live MCP input schema."""


class ToolResultError(RobinhoodMcpError):
    """Raised when a broker result is an error or is not decoded JSON."""


def validate_callback_uri(uri: str) -> str:
    """Return a canonical callback URI or reject anything non-loopback.

    OAuth is the sole reason the local cockpit needs a callback GET.  Requiring
    an exact HTTP loopback URI prevents remote callbacks, embedded credentials,
    fragments, and pre-seeded query parameters.
    """
    value = str(uri or "").strip()
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise CallbackUriError("OAuth callback URI has an invalid port.") from exc
    if parsed.scheme != "http":
        raise CallbackUriError("OAuth callback URI must use local HTTP.")
    if str(parsed.hostname or "").lower() not in {"127.0.0.1", "localhost"}:
        raise CallbackUriError("OAuth callback URI must use an exact loopback host.")
    if port is None or not (1 <= port <= 65535):
        raise CallbackUriError("OAuth callback URI must include a valid local port.")
    if parsed.username or parsed.password:
        raise CallbackUriError("OAuth callback URI cannot contain user information.")
    if parsed.path != DEFAULT_CALLBACK_PATH:
        raise CallbackUriError(f"OAuth callback URI path must be {DEFAULT_CALLBACK_PATH}.")
    if parsed.params or parsed.query or parsed.fragment:
        raise CallbackUriError("OAuth callback URI cannot contain parameters or fragments.")
    return urlunparse(("http", parsed.netloc.lower(), parsed.path, "", "", ""))


def _backend_priority(backend: Any) -> float:
    try:
        value = backend.priority
        return float(value)
    except Exception as exc:
        raise CredentialStorageError("Secure OS credential storage is unavailable.") from exc


def _supported_os_keyring_backends() -> frozenset[tuple[str, str]]:
    """Return the exact system-vault backends supported on this OS.

    Positive keyring priority only means that a backend is usable.  It does not
    mean credentials are stored in an operating-system vault: third-party
    plaintext and encrypted-file providers can also report positive priority.
    Keep this list exact and platform-specific so an alternate or chained
    backend cannot silently weaken the no-file-storage guarantee.
    """
    if sys.platform == "win32":
        return _WINDOWS_OS_KEYRING_BACKENDS
    if sys.platform == "darwin":
        return _MACOS_OS_KEYRING_BACKENDS
    if sys.platform.startswith("linux"):
        return _LINUX_OS_KEYRING_BACKENDS
    return frozenset()


class KeyringTokenStorage(TokenStorage):
    """MCP OAuth storage backed only by the active operating-system keyring."""

    def __init__(
        self,
        service_name: str = DEFAULT_KEYRING_SERVICE,
        *,
        keyring_module: Any | None = None,
    ) -> None:
        self.service_name = str(service_name or "").strip()
        if not self.service_name:
            raise CredentialStorageError("Credential service name is required.")
        self._keyring = keyring_module or keyring
        self._assert_secure_backend()

    def _assert_secure_backend(self) -> None:
        try:
            backend = self._keyring.get_keyring()
        except Exception as exc:
            raise CredentialStorageError("Secure OS credential storage is unavailable.") from exc
        backend_identity = (type(backend).__module__, type(backend).__name__)
        if (
            _backend_priority(backend) <= 0
            or backend_identity not in _supported_os_keyring_backends()
        ):
            raise CredentialStorageError(
                "A real operating-system credential backend is required; plaintext fallback is disabled."
            )

    def _read_entry(self, username: str) -> str | None:
        self._assert_secure_backend()
        try:
            value = self._keyring.get_password(self.service_name, username)
        except Exception as exc:
            raise CredentialStorageError("Could not read secure OAuth state.") from exc
        return value if isinstance(value, str) and value else None

    def _write_entry(self, username: str, value: str) -> None:
        self._assert_secure_backend()
        if not isinstance(value, str) or not value:
            raise CredentialStorageError("Could not store empty secure OAuth state.")
        try:
            self._keyring.set_password(self.service_name, username, value)
            observed = self._keyring.get_password(self.service_name, username)
        except Exception as exc:
            raise CredentialStorageError("Could not store secure OAuth state.") from exc
        if not isinstance(observed, str) or not secrets.compare_digest(observed, value):
            raise CredentialStorageError(
                "The operating-system credential backend did not verify the OAuth write."
            )

    def _delete_entry(self, username: str) -> None:
        self._assert_secure_backend()
        if self._read_entry(username) is None:
            return
        try:
            self._keyring.delete_password(self.service_name, username)
        except Exception as exc:
            raise CredentialStorageError("Could not clear secure OAuth state.") from exc
        if self._read_entry(username) is not None:
            raise CredentialStorageError(
                "The operating-system credential backend did not verify the OAuth deletion."
            )

    @staticmethod
    def _chunk_username(username: str, generation: str, index: int) -> str:
        return f"{username}:chunk:{generation}:{index:03d}"

    @staticmethod
    def _parse_chunk_manifest(raw: str | None) -> dict[str, Any] | None:
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except Exception:
            return None
        if not isinstance(payload, dict) or payload.get("schema") != _KEYRING_CHUNK_SCHEMA:
            return None
        generation = payload.get("generation")
        count = payload.get("count")
        length = payload.get("length")
        digest = payload.get("sha256")
        if (
            not isinstance(generation, str)
            or _KEYRING_GENERATION_RE.fullmatch(generation) is None
            or isinstance(count, bool)
            or not isinstance(count, int)
            or not (1 <= count <= _KEYRING_MAX_CHUNKS)
            or isinstance(length, bool)
            or not isinstance(length, int)
            or length <= _KEYRING_CHUNK_CHAR_LIMIT
            or length > _KEYRING_CHUNK_CHAR_LIMIT * _KEYRING_MAX_CHUNKS
            or not isinstance(digest, str)
            or _KEYRING_DIGEST_RE.fullmatch(digest) is None
        ):
            raise CredentialStorageError(
                "Stored OAuth chunk manifest is malformed; reconnect is required."
            )
        return {
            "generation": generation,
            "count": count,
            "length": length,
            "sha256": digest,
        }

    def _chunk_entry_names(
        self,
        username: str,
        manifest: Mapping[str, Any],
    ) -> list[str]:
        return [
            self._chunk_username(username, str(manifest["generation"]), index)
            for index in range(int(manifest["count"]))
        ]

    def _get_secret(self, username: str) -> str | None:
        raw = self._read_entry(username)
        manifest = self._parse_chunk_manifest(raw)
        if manifest is None:
            return raw
        parts: list[str] = []
        for chunk_username in self._chunk_entry_names(username, manifest):
            chunk = self._read_entry(chunk_username)
            if chunk is None:
                raise CredentialStorageError(
                    "Stored OAuth credential chunks are incomplete; reconnect is required."
                )
            parts.append(chunk)
        value = "".join(parts)
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        if len(value) != int(manifest["length"]) or not secrets.compare_digest(
            digest, str(manifest["sha256"])
        ):
            raise CredentialStorageError(
                "Stored OAuth credential chunks failed integrity verification; reconnect is required."
            )
        return value

    def _cleanup_chunk_entries(
        self,
        username: str,
        manifest: Mapping[str, Any] | None,
    ) -> None:
        if manifest is None:
            return
        first_error: CredentialStorageError | None = None
        for chunk_username in self._chunk_entry_names(username, manifest):
            try:
                self._delete_entry(chunk_username)
            except CredentialStorageError as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error

    def _set_secret(self, username: str, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise CredentialStorageError("Could not store empty secure OAuth state.")
        old_raw = self._read_entry(username)
        try:
            old_manifest = self._parse_chunk_manifest(old_raw)
        except CredentialStorageError:
            # A replacement can recover an unreadable primary entry. Unknown
            # orphaned chunks cannot be referenced without a valid manifest.
            old_manifest = None

        if len(value) <= _KEYRING_CHUNK_CHAR_LIMIT:
            self._write_entry(username, value)
            self._cleanup_chunk_entries(username, old_manifest)
            return

        chunks = [
            value[offset : offset + _KEYRING_CHUNK_CHAR_LIMIT]
            for offset in range(0, len(value), _KEYRING_CHUNK_CHAR_LIMIT)
        ]
        if len(chunks) > _KEYRING_MAX_CHUNKS:
            raise CredentialStorageError(
                "OAuth state exceeds the bounded operating-system credential capacity."
            )
        generation = secrets.token_hex(8)
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        manifest = {
            "schema": _KEYRING_CHUNK_SCHEMA,
            "generation": generation,
            "count": len(chunks),
            "length": len(value),
            "sha256": digest,
        }
        manifest_raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        written_entries: list[str] = []
        try:
            for index, chunk in enumerate(chunks):
                chunk_username = self._chunk_username(username, generation, index)
                self._write_entry(chunk_username, chunk)
                written_entries.append(chunk_username)
            # Commit only after every chunk is present and verified. Readers see
            # either the previous credential or the complete new generation.
            self._write_entry(username, manifest_raw)
            observed = self._get_secret(username)
            if observed is None or not secrets.compare_digest(observed, value):
                raise CredentialStorageError(
                    "The operating-system credential backend did not verify the chunked OAuth write."
                )
        except Exception as exc:
            try:
                current = self._read_entry(username)
                if current is not None and secrets.compare_digest(current, manifest_raw):
                    if old_raw is None:
                        self._delete_entry(username)
                    else:
                        self._write_entry(username, old_raw)
            except CredentialStorageError:
                pass
            for chunk_username in reversed(written_entries):
                try:
                    self._delete_entry(chunk_username)
                except CredentialStorageError:
                    pass
            if isinstance(exc, CredentialStorageError):
                raise
            raise CredentialStorageError("Could not store secure OAuth state.") from exc
        self._cleanup_chunk_entries(username, old_manifest)

    def _delete_secret(self, username: str) -> None:
        raw = self._read_entry(username)
        if raw is None:
            return
        try:
            manifest = self._parse_chunk_manifest(raw)
        except CredentialStorageError:
            manifest = None
        first_error: CredentialStorageError | None = None
        try:
            self._cleanup_chunk_entries(username, manifest)
        except CredentialStorageError as exc:
            first_error = exc
        try:
            self._delete_entry(username)
        except CredentialStorageError as exc:
            first_error = first_error or exc
        if first_error is not None:
            raise first_error

    @staticmethod
    def _serialize(schema: str, value: Any) -> str:
        return json.dumps(
            {
                "schema": schema,
                "value": value.model_dump(mode="json", exclude_none=True),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _deserialize(raw: str, schema: str, model: Any) -> Any:
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("schema") != schema:
                raise ValueError("wrong envelope")
            return model.model_validate(payload.get("value"))
        except Exception as exc:
            raise CredentialStorageError(
                "Stored OAuth state is malformed; reconnect is required."
            ) from exc

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._get_secret(TOKEN_KEYRING_USERNAME)
        if raw is None:
            return None
        return self._deserialize(raw, "optedge_robinhood_oauth_token_v1", OAuthToken)

    async def set_tokens(self, tokens: OAuthToken) -> None:
        if not isinstance(tokens, OAuthToken):
            raise CredentialStorageError("OAuth token has an unsupported shape.")
        self._set_secret(
            TOKEN_KEYRING_USERNAME,
            self._serialize("optedge_robinhood_oauth_token_v1", tokens),
        )

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._get_secret(CLIENT_INFO_KEYRING_USERNAME)
        if raw is None:
            return None
        return self._deserialize(
            raw,
            "optedge_robinhood_oauth_client_v1",
            OAuthClientInformationFull,
        )

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        if not isinstance(client_info, OAuthClientInformationFull):
            raise CredentialStorageError("OAuth client registration has an unsupported shape.")
        self._set_secret(
            CLIENT_INFO_KEYRING_USERNAME,
            self._serialize("optedge_robinhood_oauth_client_v1", client_info),
        )

    async def clear(self) -> None:
        """Delete both OAuth envelopes without creating any fallback state."""
        self._delete_secret(TOKEN_KEYRING_USERNAME)
        self._delete_secret(CLIENT_INFO_KEYRING_USERNAME)

    def public_status(self) -> dict[str, Any]:
        """Return presence-only credential state; never return secret material."""
        return {
            "backend_ready": True,
            "plaintext_fallback_allowed": False,
            "token_present": self._get_secret(TOKEN_KEYRING_USERNAME) is not None,
            "client_registration_present": (
                self._get_secret(CLIENT_INFO_KEYRING_USERNAME) is not None
            ),
        }


class OAuthCallbackCoordinator:
    """Bridge the SDK's async OAuth callbacks to a loopback HTTP handler.

    The authorization URL is available only through the explicit trusted
    accessor.  ``public_status`` reveals only booleans and never exposes the
    URL, state, code, or PKCE material.
    """

    def __init__(self, callback_uri: str) -> None:
        self.callback_uri = validate_callback_uri(callback_uri)
        self._lock = threading.RLock()
        self._authorization_url: str | None = None
        self._expected_state: str | None = None
        self._callback_future: asyncio.Future[tuple[str, str | None]] | None = None
        self._callback_loop: asyncio.AbstractEventLoop | None = None
        self._status = "idle"

    async def redirect_handler(self, authorization_url: str) -> None:
        parsed = urlparse(str(authorization_url or ""))
        states = parse_qs(parsed.query, keep_blank_values=True).get("state", [])
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise OAuthStateError("OAuth authorization redirect is not a valid HTTPS URL.")
        if len(states) != 1 or not states[0]:
            raise OAuthStateError("OAuth authorization redirect is missing one exact state value.")
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        with self._lock:
            if self._status in {"authorization_required", "callback_received"}:
                raise OAuthStateError("An OAuth authorization attempt is already pending.")
            self._authorization_url = str(authorization_url)
            self._expected_state = states[0]
            self._callback_future = future
            self._callback_loop = loop
            self._status = "authorization_required"

    async def callback_handler(self) -> tuple[str, str | None]:
        with self._lock:
            future = self._callback_future
        if future is None:
            raise OAuthStateError("No OAuth authorization callback is pending.")
        try:
            result = await future
        finally:
            with self._lock:
                # A reset may already have replaced/cleared this attempt.  Do
                # not let a cancelled waiter overwrite the newer idle state.
                if self._callback_future is future:
                    self._authorization_url = None
                    self._expected_state = None
                    self._callback_future = None
                    self._callback_loop = None
                    if self._status == "callback_received":
                        self._status = "complete"
                    elif self._status != "failed":
                        self._status = "failed"
        return result

    def authorization_url_for_browser(self) -> str:
        """Return the pending URL to trusted loopback UI code, never to status JSON."""
        with self._lock:
            if self._status != "authorization_required" or not self._authorization_url:
                raise OAuthStateError("No OAuth authorization URL is ready.")
            return self._authorization_url

    def submit_callback(self, callback_url: str) -> None:
        """Validate an exact callback URL and deliver its code once."""
        parsed = urlparse(str(callback_url or ""))
        callback_base = urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path, "", "", ""))
        if callback_base != self.callback_uri or parsed.params or parsed.fragment:
            raise OAuthStateError("OAuth callback does not match the registered loopback URI.")
        query = parse_qs(parsed.query, keep_blank_values=True)
        if "error" in query:
            errors = query.get("error", [])
            states = query.get("state", [])
            with self._lock:
                expected_state = self._expected_state
                pending = (
                    self._status == "authorization_required"
                    and self._callback_future is not None
                    and self._callback_loop is not None
                )
            if (
                len(errors) != 1
                or not errors[0]
                or len(states) != 1
                or not states[0]
                or not expected_state
                or not pending
                or not secrets.compare_digest(states[0], expected_state)
            ):
                raise OAuthStateError("OAuth error callback state is invalid or already consumed.")
            self._fail_pending_callback("OAuth authorization was denied or failed.")
            raise OAuthStateError("OAuth authorization was denied or failed.")
        codes = query.get("code", [])
        states = query.get("state", [])
        if len(codes) != 1 or not codes[0] or len(states) != 1 or not states[0]:
            raise OAuthStateError("OAuth callback must contain one code and one state.")
        with self._lock:
            state = self._expected_state
            future = self._callback_future
            loop = self._callback_loop
            status = self._status
            if (
                status != "authorization_required"
                or not state
                or future is None
                or loop is None
                or not secrets.compare_digest(states[0], state)
            ):
                raise OAuthStateError("OAuth callback state is invalid or already consumed.")
            self._status = "callback_received"

        def deliver() -> None:
            if not future.done():
                future.set_result((codes[0], states[0]))

        loop.call_soon_threadsafe(deliver)

    def _fail_pending_callback(self, message: str) -> None:
        with self._lock:
            future = self._callback_future
            loop = self._callback_loop
            self._status = "failed"
        if future is not None and loop is not None:
            loop.call_soon_threadsafe(
                lambda: future.done() or future.set_exception(OAuthStateError(message))
            )

    def reset(self) -> None:
        """Cancel a pending one-time authorization without touching broker state."""
        with self._lock:
            future = self._callback_future
            loop = self._callback_loop
            self._authorization_url = None
            self._expected_state = None
            self._callback_future = None
            self._callback_loop = None
            self._status = "idle"
        if future is not None and loop is not None:
            loop.call_soon_threadsafe(lambda: future.done() or future.cancel())

    def public_status(self) -> dict[str, Any]:
        with self._lock:
            status = self._status
            url_ready = bool(self._authorization_url)
        return {
            "status": status,
            "callback_uri": self.callback_uri,
            "authorization_url_ready": url_ready,
            "contains_authorization_url": False,
            "contains_code_or_state": False,
        }


def create_robinhood_oauth_provider(
    callback_uri: str,
    storage: TokenStorage,
    coordinator: OAuthCallbackCoordinator,
) -> OAuthClientProvider:
    """Build the official OAuth provider with the fixed Robinhood endpoint."""
    canonical_callback = validate_callback_uri(callback_uri)
    if coordinator.callback_uri != canonical_callback:
        raise CallbackUriError("OAuth coordinator callback does not match client metadata.")
    metadata = OAuthClientMetadata(
        client_name="Optedge Local Swing Trading Workstation",
        redirect_uris=[AnyUrl(canonical_callback)],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )
    return OAuthClientProvider(
        server_url=ROBINHOOD_MCP_ENDPOINT,
        client_metadata=metadata,
        storage=storage,
        redirect_handler=coordinator.redirect_handler,
        callback_handler=coordinator.callback_handler,
        timeout=300.0,
    )


def _tool_value(tool: Any, *names: str) -> Any:
    if isinstance(tool, Mapping):
        for name in names:
            if name in tool:
                return tool.get(name)
        return None
    for name in names:
        if hasattr(tool, name):
            return getattr(tool, name)
    return None


def _normalize_tool(tool: Any) -> dict[str, Any]:
    name = str(_tool_value(tool, "name") or "").strip()
    schema = _tool_value(tool, "inputSchema", "input_schema")
    if not name:
        raise ToolCatalogError("MCP tool catalog contains a blank tool name.")
    if not isinstance(schema, dict):
        raise ToolCatalogError(f"MCP tool {name} has no object input schema.")
    return {"name": name, "input_schema": schema}


def validate_tool_catalog(tools: Iterable[Any]) -> dict[str, Any]:
    """Validate names and JSON schemas, then return a sanitized capability map."""
    catalog: dict[str, dict[str, Any]] = {}
    issues: list[str] = []
    for raw_tool in tools:
        try:
            tool = _normalize_tool(raw_tool)
        except ToolCatalogError as exc:
            issues.append(str(exc))
            continue
        name = tool["name"]
        if name in catalog:
            issues.append(f"MCP tool catalog contains duplicate tool {name}.")
            continue
        schema = tool["input_schema"]
        try:
            jsonschema.validators.validator_for(schema).check_schema(schema)
        except jsonschema.exceptions.SchemaError:
            issues.append(f"MCP tool {name} has an invalid JSON input schema.")
            continue
        if schema.get("type") not in (None, "object"):
            issues.append(f"MCP tool {name} input schema is not an object.")
            continue
        if "properties" in schema and not isinstance(schema.get("properties"), dict):
            issues.append(f"MCP tool {name} properties are malformed.")
            continue
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(x, str) for x in required):
            issues.append(f"MCP tool {name} required fields are malformed.")
            continue
        catalog[name] = tool

    names = set(catalog)
    missing_reads = sorted(REQUIRED_PREFLIGHT_READ_TOOLS - names)
    missing_reviews = sorted(REVIEW_TOOL_ALLOWLIST - names)
    missing_places = sorted(PLACE_TOOL_ALLOWLIST - names)
    unsupported = sorted(names - READ_TOOL_ALLOWLIST - REVIEW_TOOL_ALLOWLIST - PLACE_TOOL_ALLOWLIST)
    schema_valid = not issues
    return {
        "schema_valid": schema_valid,
        "ready_for_direct_review": schema_valid and not missing_reads and not missing_reviews,
        "placement_tools_detected": not missing_places,
        "placement_api_exposed": False,
        "confirmed_option_placement_supported": schema_valid and not missing_places,
        "tool_count": len(catalog),
        "read_tools": sorted(names & READ_TOOL_ALLOWLIST),
        "review_tools": sorted(names & REVIEW_TOOL_ALLOWLIST),
        "place_tools": sorted(names & PLACE_TOOL_ALLOWLIST),
        "missing_required_read_tools": missing_reads,
        "missing_review_tools": missing_reviews,
        "missing_place_tools": missing_places,
        "unsupported_tools": unsupported,
        "issues": issues,
        "_catalog": catalog,
    }


def _result_value(result: Any, *names: str) -> Any:
    if isinstance(result, Mapping):
        for name in names:
            if name in result:
                return result.get(name)
        return None
    for name in names:
        if hasattr(result, name):
            return getattr(result, name)
    return None


def decode_tool_result(result: Any) -> Any:
    """Decode one successful MCP result into JSON without echoing error prose."""
    if isinstance(result, Mapping) and not any(
        key in result
        for key in ("isError", "is_error", "structuredContent", "structured_content", "content")
    ):
        return dict(result)
    if _result_value(result, "isError", "is_error") is True:
        raise ToolResultError("Robinhood MCP returned a tool error.")
    structured = _result_value(result, "structuredContent", "structured_content")
    if structured is not None:
        if isinstance(structured, (dict, list)):
            return structured
        raise ToolResultError("Robinhood MCP structured content is not decoded JSON.")
    content = _result_value(result, "content")
    if not isinstance(content, list) or not content:
        raise ToolResultError("Robinhood MCP result contains no decoded JSON.")
    decoded: list[Any] = []
    for block in content:
        block_type = str(_tool_value(block, "type") or "").strip().lower()
        text = _tool_value(block, "text")
        if block_type and block_type != "text":
            continue
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            decoded.append(json.loads(text))
        except json.JSONDecodeError as exc:
            raise ToolResultError("Robinhood MCP text content is not valid JSON.") from exc
    if not decoded:
        raise ToolResultError("Robinhood MCP result contains no decoded JSON.")
    if len(decoded) == 1:
        return decoded[0]
    return decoded


def _sensitive_key(key: Any) -> bool:
    value = str(key or "").strip().lower()
    if value in _ACCOUNT_NUMBER_KEYS or value in _EXACT_SENSITIVE_KEYS:
        return True
    if value.endswith(("_access_token", "_refresh_token", "_client_secret", "_password")):
        return True
    return False


def sanitize_public_data(value: Any, *, account_numbers: Iterable[str] = ()) -> Any:
    """Recursively redact broker identities and authentication material."""
    accounts = tuple(
        sorted(
            {str(item).strip() for item in account_numbers if str(item).strip()},
            key=len,
            reverse=True,
        )
    )

    def clean(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {
                str(key): "[redacted]" if _sensitive_key(key) else clean(child)
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [clean(child) for child in item]
        if isinstance(item, tuple):
            return [clean(child) for child in item]
        if isinstance(item, str):
            result = item
            for account in accounts:
                result = re.sub(
                    rf"(?<![A-Za-z0-9]){re.escape(account)}(?![A-Za-z0-9])",
                    "[redacted-account]",
                    result,
                )
            return result
        return item

    return clean(value)


def _account_numbers_in(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).strip().lower() in _ACCOUNT_NUMBER_KEYS:
                account = str(child or "").strip()
                if account:
                    found.add(account)
            else:
                found.update(_account_numbers_in(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.update(_account_numbers_in(child))
    return found


SessionFactory = Callable[[], AbstractAsyncContextManager[Any]]


class RobinhoodMcpClient:
    """One-shot official Robinhood MCP read, preview, and confirmed-option client.

    ``call_read_tool`` cannot call preview or placement tools.
    ``call_review_tool`` can call only the two broker preview tools.
    ``call_confirmed_option_order`` is fixed to one option placement tool and
    is intended only for the single-use capability service.
    """

    def __init__(
        self,
        callback_uri: str,
        *,
        token_storage: KeyringTokenStorage | None = None,
        oauth_coordinator: OAuthCallbackCoordinator | None = None,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self.callback_uri = validate_callback_uri(callback_uri)
        self.token_storage = token_storage or KeyringTokenStorage()
        self.oauth_coordinator = oauth_coordinator or OAuthCallbackCoordinator(self.callback_uri)
        if self.oauth_coordinator.callback_uri != self.callback_uri:
            raise CallbackUriError("OAuth callback components do not match.")
        self._session_factory = session_factory or self._open_oauth_session
        self._operation_lock = asyncio.Lock()
        self._state = "disconnected"
        self._last_error_code: str | None = None
        self._catalog: dict[str, dict[str, Any]] = {}
        self._catalog_status: dict[str, Any] = validate_tool_catalog([])
        self._known_account_numbers: set[str] = set()

    @asynccontextmanager
    async def _open_oauth_session(self):
        provider = create_robinhood_oauth_provider(
            self.callback_uri,
            self.token_storage,
            self.oauth_coordinator,
        )
        timeout = httpx.Timeout(30.0, connect=15.0)
        async with httpx.AsyncClient(
            auth=provider,
            follow_redirects=True,
            timeout=timeout,
        ) as http_client:
            async with streamable_http_client(
                ROBINHOOD_MCP_ENDPOINT,
                http_client=http_client,
                terminate_on_close=True,
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    yield session

    async def _load_catalog(self, session: Any) -> dict[str, Any]:
        tools: list[Any] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(MAX_TOOL_PAGES):
            page = await session.list_tools(cursor=cursor)
            page_tools = _result_value(page, "tools")
            if not isinstance(page_tools, list):
                raise ToolCatalogError("MCP tool page does not contain a tool list.")
            tools.extend(page_tools)
            next_cursor = _result_value(page, "nextCursor", "next_cursor")
            if next_cursor in (None, ""):
                break
            next_cursor = str(next_cursor)
            if next_cursor in seen_cursors:
                raise ToolCatalogError("MCP tool pagination contains a cursor cycle.")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            raise ToolCatalogError("MCP tool catalog exceeded the bounded page limit.")
        validation = validate_tool_catalog(tools)
        if not validation.get("schema_valid"):
            raise ToolCatalogError("Robinhood MCP tool catalog failed schema validation.")
        return validation

    def _install_catalog(self, validation: dict[str, Any]) -> None:
        self._catalog = dict(validation.get("_catalog") or {})
        self._catalog_status = {
            key: value for key, value in validation.items() if key != "_catalog"
        }
        self._state = (
            "connected"
            if self._catalog_status.get("ready_for_direct_review") is True
            else "connected_limited"
        )
        self._last_error_code = None

    async def connect(self) -> dict[str, Any]:
        """Make one explicit connection attempt and validate the live tools."""
        async with self._operation_lock:
            self._state = "connecting"
            self._last_error_code = None
            try:
                async with self._session_factory() as session:
                    validation = await self._load_catalog(session)
                self._install_catalog(validation)
            except Exception as exc:
                oauth_status = self.oauth_coordinator.public_status().get("status")
                self._state = (
                    "authorization_required"
                    if oauth_status in {"authorization_required", "callback_received"}
                    else "error"
                )
                self._last_error_code = self._safe_error_code(exc)
                if isinstance(exc, RobinhoodMcpError):
                    raise
                raise RobinhoodMcpError("Robinhood MCP connection failed.") from exc
            return await self.connection_status()

    async def list_tools(self) -> dict[str, Any]:
        """Refresh the catalog once and return only its sanitized capability map."""
        async with self._operation_lock:
            try:
                async with self._session_factory() as session:
                    validation = await self._load_catalog(session)
                self._install_catalog(validation)
            except Exception as exc:
                self._last_error_code = self._safe_error_code(exc)
                if isinstance(exc, RobinhoodMcpError):
                    raise
                raise RobinhoodMcpError("Robinhood MCP tool discovery failed.") from exc
            return dict(self._catalog_status)

    def read_tool_input_schema(self, name: str) -> dict[str, Any]:
        """Return a defensive copy of one connected allowlisted read schema.

        The schema contains no account or OAuth data.  It lets a narrow
        high-level workflow adapt to official cursor and identifier field
        shapes without guessing arguments or exposing a generic tool surface.
        """
        tool_name = str(name or "").strip()
        if tool_name not in READ_TOOL_ALLOWLIST:
            raise ToolPolicyError("Only allowlisted read-tool schemas are available.")
        if self._state not in {"connected", "connected_limited"}:
            raise ToolPolicyError("Connect and validate Robinhood MCP tools first.")
        tool = self._catalog.get(tool_name)
        if not isinstance(tool, dict) or not isinstance(tool.get("input_schema"), dict):
            raise ToolCatalogError("The requested read-tool schema is unavailable.")
        return copy.deepcopy(tool["input_schema"])

    async def disconnect(self) -> dict[str, Any]:
        """Clear OAuth grants and all ephemeral broker identity/catalog state."""
        async with self._operation_lock:
            await self.token_storage.clear()
            self.oauth_coordinator.reset()
            self._catalog = {}
            self._catalog_status = {
                key: value for key, value in validate_tool_catalog([]).items() if key != "_catalog"
            }
            self._known_account_numbers.clear()
            self._last_error_code = None
            self._state = "disconnected"
            return await self.connection_status()

    def authorization_url_for_browser(self) -> str:
        return self.oauth_coordinator.authorization_url_for_browser()

    def submit_oauth_callback(self, callback_url: str) -> None:
        self.oauth_coordinator.submit_callback(callback_url)

    async def connection_status(self) -> dict[str, Any]:
        """Return a sanitized status that cannot contain raw broker identity."""
        try:
            credential_status = self.token_storage.public_status()
        except CredentialStorageError:
            credential_status = {
                "backend_ready": False,
                "plaintext_fallback_allowed": False,
                "token_present": False,
                "client_registration_present": False,
            }
        status = {
            "schema": "optedge_robinhood_mcp_connection_v1",
            "endpoint": ROBINHOOD_MCP_ENDPOINT,
            "connection_state": self._state,
            "last_error_code": self._last_error_code,
            "oauth": self.oauth_coordinator.public_status(),
            "credential_storage": credential_status,
            "tool_catalog": dict(self._catalog_status),
            "known_account_count": len(self._known_account_numbers),
            "raw_account_numbers_exposed": False,
            "generic_tool_call_exposed": False,
            "placement_api_exposed": False,
            "confirmed_option_placement_api_exposed": True,
            "automatic_retry_enabled": False,
            "background_polling_enabled": False,
        }
        return sanitize_public_data(
            status,
            account_numbers=self._known_account_numbers,
        )

    @staticmethod
    def _safe_error_code(exc: Exception) -> str:
        if isinstance(exc, CredentialStorageError):
            return "credential_storage_unavailable"
        if isinstance(exc, OAuthStateError):
            return "oauth_state_invalid"
        if isinstance(exc, ToolCatalogError):
            return "tool_catalog_invalid"
        if isinstance(exc, ToolSchemaError):
            return "tool_arguments_invalid"
        if isinstance(exc, ToolResultError):
            return "tool_result_invalid"
        if isinstance(exc, ToolPolicyError):
            return "tool_policy_blocked"
        return "connection_or_transport_error"

    @staticmethod
    def _validate_arguments(schema: dict[str, Any], arguments: Any) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ToolSchemaError("MCP tool arguments must be an object.")
        try:
            validator_cls = jsonschema.validators.validator_for(schema)
            validator_cls.check_schema(schema)
            validator_cls(schema).validate(arguments)
        except jsonschema.exceptions.ValidationError as exc:
            field = ".".join(str(part) for part in exc.absolute_path)
            detail = f" at {field}" if field else ""
            raise ToolSchemaError(f"MCP tool arguments failed validation{detail}.") from exc
        except jsonschema.exceptions.SchemaError as exc:
            raise ToolSchemaError("MCP tool schema changed or is invalid.") from exc
        return dict(arguments)

    async def _call_allowed_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        allowed: frozenset[str],
        confirmed_placement: bool = False,
    ) -> Any:
        tool_name = str(name or "").strip()
        if tool_name in PLACE_TOOL_ALLOWLIST and not confirmed_placement:
            raise ToolPolicyError(
                "Placement is unavailable without a higher-level opaque confirmation capability."
            )
        if confirmed_placement and tool_name != "place_option_order":
            raise ToolPolicyError("Only the fixed confirmed option-placement tool is available.")
        if tool_name not in allowed:
            raise ToolPolicyError("MCP tool is not allowed through this method.")
        async with self._operation_lock:
            if self._state not in {"connected", "connected_limited"}:
                raise ToolPolicyError("Connect and validate Robinhood MCP tools first.")
            try:
                async with self._session_factory() as session:
                    live_validation = await self._load_catalog(session)
                    live_catalog = dict(live_validation.get("_catalog") or {})
                    live_tool = live_catalog.get(tool_name)
                    if live_tool is None:
                        raise ToolCatalogError(f"Required MCP tool {tool_name} is unavailable.")
                    cached_tool = self._catalog.get(tool_name)
                    if cached_tool is not None:
                        cached_digest = hashlib.sha256(
                            json.dumps(
                                cached_tool.get("input_schema"),
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).digest()
                        live_digest = hashlib.sha256(
                            json.dumps(
                                live_tool.get("input_schema"),
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).digest()
                        if not secrets.compare_digest(cached_digest, live_digest):
                            self._install_catalog(live_validation)
                            raise ToolSchemaError(
                                "MCP tool schema changed after discovery; review the new schema first."
                            )
                    safe_arguments = self._validate_arguments(
                        live_tool["input_schema"],
                        arguments,
                    )
                    result = await session.call_tool(tool_name, arguments=safe_arguments)
                self._install_catalog(live_validation)
                decoded = decode_tool_result(result)
                if tool_name == "get_accounts":
                    self._known_account_numbers.update(_account_numbers_in(decoded))
                return decoded
            except Exception as exc:
                self._last_error_code = self._safe_error_code(exc)
                if isinstance(exc, RobinhoodMcpError):
                    raise
                raise RobinhoodMcpError("Robinhood MCP tool call failed.") from exc

    async def call_read_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call one allowlisted read tool exactly once after live schema validation."""
        return await self._call_allowed_tool(
            name,
            arguments,
            allowed=READ_TOOL_ALLOWLIST,
        )

    async def call_review_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call one allowlisted broker preview tool; this cannot place an order."""
        return await self._call_allowed_tool(
            name,
            arguments,
            allowed=REVIEW_TOOL_ALLOWLIST,
        )

    async def call_confirmed_option_order(self, arguments: dict[str, Any]) -> Any:
        """Place one exact option order after the caller consumes a confirmation token."""
        return await self._call_allowed_tool(
            "place_option_order",
            arguments,
            allowed=frozenset({"place_option_order"}),
            confirmed_placement=True,
        )


__all__ = [
    "CLIENT_INFO_KEYRING_USERNAME",
    "DEFAULT_CALLBACK_PATH",
    "DEFAULT_KEYRING_SERVICE",
    "PLACE_TOOL_ALLOWLIST",
    "READ_TOOL_ALLOWLIST",
    "REQUIRED_PREFLIGHT_READ_TOOLS",
    "REVIEW_TOOL_ALLOWLIST",
    "ROBINHOOD_MCP_ENDPOINT",
    "TOKEN_KEYRING_USERNAME",
    "CallbackUriError",
    "CredentialStorageError",
    "KeyringTokenStorage",
    "OAuthCallbackCoordinator",
    "OAuthStateError",
    "RobinhoodMcpClient",
    "RobinhoodMcpError",
    "ToolCatalogError",
    "ToolPolicyError",
    "ToolResultError",
    "ToolSchemaError",
    "create_robinhood_oauth_provider",
    "decode_tool_result",
    "sanitize_public_data",
    "validate_callback_uri",
    "validate_tool_catalog",
]
