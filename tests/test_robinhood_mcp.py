# Purpose: Test the official Robinhood MCP connector safety foundation.
"""Deterministic tests for OAuth, keyring, tool policy, and result decoding."""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from optedge.robinhood_mcp import (
    PLACE_TOOL_ALLOWLIST,
    READ_TOOL_ALLOWLIST,
    REQUIRED_PREFLIGHT_READ_TOOLS,
    REVIEW_TOOL_ALLOWLIST,
    ROBINHOOD_MCP_ENDPOINT,
    CallbackUriError,
    CredentialStorageError,
    KeyringTokenStorage,
    OAuthCallbackCoordinator,
    OAuthStateError,
    RobinhoodMcpClient,
    ToolCatalogError,
    ToolPolicyError,
    ToolResultError,
    ToolSchemaError,
    create_robinhood_oauth_provider,
    decode_tool_result,
    sanitize_public_data,
    validate_callback_uri,
    validate_tool_catalog,
)

CALLBACK_URI = "http://127.0.0.1:8765/oauth/robinhood/callback"


def _supported_fake_backend_identity() -> tuple[str, str]:
    if sys.platform == "win32":
        return ("keyring.backends.Windows", "WinVaultKeyring")
    if sys.platform == "darwin":
        return ("keyring.backends.macOS", "Keyring")
    return ("keyring.backends.SecretService", "Keyring")


class _FakeKeyring:
    def __init__(
        self,
        *,
        priority: float = 1,
        backend_identity: tuple[str, str] | None = None,
        max_password_chars: int | None = None,
    ) -> None:
        module_name, class_name = backend_identity or _supported_fake_backend_identity()
        backend_type = type(
            class_name,
            (),
            {"__module__": module_name, "priority": priority},
        )
        self.backend = backend_type()
        self.values: dict[tuple[str, str], str] = {}
        self.max_password_chars = max_password_chars

    def get_keyring(self):
        return self.backend

    def get_password(self, service: str, username: str):
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str):
        if self.max_password_chars is not None and len(password) > self.max_password_chars:
            raise OSError(1783, "The stub received bad data")
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str):
        del self.values[(service, username)]


def _tool(name: str, schema: dict | None = None) -> dict:
    return {
        "name": name,
        "inputSchema": schema or {"type": "object", "additionalProperties": True},
    }


def _complete_tools() -> list[dict]:
    names = (
        set(REQUIRED_PREFLIGHT_READ_TOOLS) | set(REVIEW_TOOL_ALLOWLIST) | set(PLACE_TOOL_ALLOWLIST)
    )
    return [_tool(name) for name in sorted(names)]


class _FakeSession:
    def __init__(self, tools: list[dict] | None = None) -> None:
        self.tools = list(tools or _complete_tools())
        self.calls: list[tuple[str, dict]] = []
        self.list_calls: list[str | None] = []
        self.results: dict[str, object] = {
            "get_accounts": {
                "structuredContent": {
                    "data": {"accounts": [{"account_number": "123456789", "agentic_allowed": True}]}
                },
                "isError": False,
            },
            "review_option_order": {
                "content": [{"type": "text", "text": json.dumps({"preview_id": "preview-1"})}],
                "isError": False,
            },
        }

    async def list_tools(self, cursor=None):
        self.list_calls.append(cursor)
        return {"tools": self.tools, "nextCursor": None}

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, dict(arguments or {})))
        return self.results.get(name, {"structuredContent": {"ok": True}, "isError": False})


def _session_factory(session: _FakeSession):
    @asynccontextmanager
    async def factory():
        yield session

    return factory


def test_endpoint_is_fixed_and_callback_uri_is_exact_loopback():
    assert ROBINHOOD_MCP_ENDPOINT == "https://agent.robinhood.com/mcp/trading"
    assert validate_callback_uri(CALLBACK_URI) == CALLBACK_URI
    assert (
        validate_callback_uri("http://localhost:9000/oauth/robinhood/callback")
        == "http://localhost:9000/oauth/robinhood/callback"
    )

    invalid = [
        "https://127.0.0.1:8765/oauth/robinhood/callback",
        "http://0.0.0.0:8765/oauth/robinhood/callback",
        "http://attacker.example:8765/oauth/robinhood/callback",
        "http://127.0.0.1/oauth/robinhood/callback",
        "http://127.0.0.1:8765/wrong",
        f"{CALLBACK_URI}?code=preseeded",
    ]
    for value in invalid:
        with pytest.raises(CallbackUriError):
            validate_callback_uri(value)


def test_keyring_storage_roundtrips_oauth_models_without_public_secrets():
    async def run():
        fake = _FakeKeyring()
        storage = KeyringTokenStorage(keyring_module=fake)
        tokens = OAuthToken(
            access_token="access-secret",
            refresh_token="refresh-secret",
            expires_in=3600,
        )
        client_info = OAuthClientInformationFull(
            client_id="client-id",
            client_secret="client-secret",
            redirect_uris=[AnyUrl(CALLBACK_URI)],
        )
        await storage.set_tokens(tokens)
        await storage.set_client_info(client_info)
        assert await storage.get_tokens() == tokens
        assert await storage.get_client_info() == client_info
        status = storage.public_status()
        assert status == {
            "backend_ready": True,
            "plaintext_fallback_allowed": False,
            "token_present": True,
            "client_registration_present": True,
        }
        rendered = json.dumps(status)
        assert "access-secret" not in rendered
        assert "refresh-secret" not in rendered
        assert "client-secret" not in rendered
        await storage.clear()
        assert await storage.get_tokens() is None
        assert await storage.get_client_info() is None

    asyncio.run(run())


def test_keyring_storage_chunks_large_tokens_inside_the_os_vault_limit():
    async def run():
        fake = _FakeKeyring(max_password_chars=900)
        storage = KeyringTokenStorage(keyring_module=fake)
        tokens = OAuthToken(
            access_token="a" * 2400,
            refresh_token="r" * 2600,
            expires_in=3600,
        )

        await storage.set_tokens(tokens)

        token_entries = {
            username: value
            for (service, username), value in fake.values.items()
            if service == storage.service_name and username.startswith("oauth-token-v1")
        }
        assert len(token_entries) > 2
        assert all(len(value) <= 900 for value in token_entries.values())
        assert any(":chunk:" in username for username in token_entries)
        assert await storage.get_tokens() == tokens
        assert "a" * 100 not in token_entries["oauth-token-v1"]
        assert "r" * 100 not in token_entries["oauth-token-v1"]

        first_generation = set(token_entries)
        replacement = OAuthToken(
            access_token="b" * 2200,
            refresh_token="s" * 2500,
            expires_in=7200,
        )
        await storage.set_tokens(replacement)
        assert await storage.get_tokens() == replacement
        assert all(
            username == "oauth-token-v1" or username not in fake.values
            for username in first_generation
        )

        await storage.clear()
        assert not any(service == storage.service_name for service, _username in fake.values)

    asyncio.run(run())


def test_keyring_storage_rejects_tampered_or_missing_chunks():
    async def run():
        fake = _FakeKeyring(max_password_chars=900)
        storage = KeyringTokenStorage(keyring_module=fake)
        tokens = OAuthToken(
            access_token="a" * 1800,
            refresh_token="r" * 1800,
            expires_in=3600,
        )
        await storage.set_tokens(tokens)
        chunk_key = next(
            key for key in fake.values if key[0] == storage.service_name and ":chunk:" in key[1]
        )
        original = fake.values[chunk_key]
        fake.values[chunk_key] = ("x" if original[0] != "x" else "y") + original[1:]
        with pytest.raises(CredentialStorageError, match="integrity verification"):
            await storage.get_tokens()

        del fake.values[chunk_key]
        with pytest.raises(CredentialStorageError, match="incomplete"):
            await storage.get_tokens()
        await storage.clear()

    asyncio.run(run())


def test_keyring_storage_fails_closed_for_null_or_corrupt_backends():
    with pytest.raises(CredentialStorageError):
        KeyringTokenStorage(keyring_module=_FakeKeyring(priority=0))

    async def run():
        fake = _FakeKeyring()
        storage = KeyringTokenStorage(keyring_module=fake)
        fake.values[(storage.service_name, "oauth-token-v1")] = "not-json"
        with pytest.raises(CredentialStorageError):
            await storage.get_tokens()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("platform_name", "backend_identity"),
    [
        ("win32", ("keyring.backends.Windows", "WinVaultKeyring")),
        ("darwin", ("keyring.backends.macOS", "Keyring")),
        ("linux", ("keyring.backends.SecretService", "Keyring")),
        ("linux", ("keyring.backends.libsecret", "Keyring")),
        ("linux", ("keyring.backends.kwallet", "DBusKeyring")),
        ("linux", ("keyring.backends.kwallet", "DBusKeyringKWallet4")),
    ],
)
def test_keyring_storage_accepts_recognized_system_vault_for_current_platform(
    monkeypatch,
    platform_name: str,
    backend_identity: tuple[str, str],
):
    monkeypatch.setattr("optedge.robinhood_mcp.sys.platform", platform_name)
    KeyringTokenStorage(keyring_module=_FakeKeyring(backend_identity=backend_identity))


def test_keyring_storage_rejects_system_vault_for_a_different_platform(monkeypatch):
    monkeypatch.setattr("optedge.robinhood_mcp.sys.platform", "win32")
    with pytest.raises(CredentialStorageError, match="operating-system credential backend"):
        KeyringTokenStorage(
            keyring_module=_FakeKeyring(
                backend_identity=("keyring.backends.SecretService", "Keyring")
            )
        )


@pytest.mark.parametrize(
    "backend_identity",
    [
        ("keyrings.alt.file", "PlaintextKeyring"),
        ("keyrings.alt.file", "EncryptedKeyring"),
        ("keyring.backends.chainer", "ChainerBackend"),
        ("example.keyring", "UnknownPositivePriorityBackend"),
    ],
)
def test_keyring_storage_rejects_positive_priority_non_os_backends(
    backend_identity: tuple[str, str],
):
    with pytest.raises(CredentialStorageError, match="operating-system credential backend"):
        KeyringTokenStorage(
            keyring_module=_FakeKeyring(
                priority=99,
                backend_identity=backend_identity,
            )
        )


def test_oauth_coordinator_validates_one_time_state_without_publicly_exposing_it():
    async def run():
        coordinator = OAuthCallbackCoordinator(CALLBACK_URI)
        auth_url = "https://robinhood.example/authorize?state=state-secret&code_challenge=pkce"
        await coordinator.redirect_handler(auth_url)
        assert coordinator.authorization_url_for_browser() == auth_url
        status = coordinator.public_status()
        assert status["status"] == "authorization_required"
        assert status["authorization_url_ready"] is True
        rendered = json.dumps(status)
        assert "state-secret" not in rendered
        assert auth_url not in rendered

        waiter = asyncio.create_task(coordinator.callback_handler())
        coordinator.submit_callback(f"{CALLBACK_URI}?code=code-secret&state=state-secret")
        assert await waiter == ("code-secret", "state-secret")
        assert coordinator.public_status()["status"] == "complete"
        with pytest.raises(OAuthStateError):
            coordinator.submit_callback(f"{CALLBACK_URI}?code=second-code&state=state-secret")

    asyncio.run(run())


def test_oauth_coordinator_rejects_wrong_state_and_callback_route():
    async def run():
        coordinator = OAuthCallbackCoordinator(CALLBACK_URI)
        await coordinator.redirect_handler("https://robinhood.example/auth?state=expected")
        with pytest.raises(OAuthStateError):
            coordinator.submit_callback(f"{CALLBACK_URI}?code=x&state=wrong")
        with pytest.raises(OAuthStateError):
            coordinator.submit_callback("http://127.0.0.1:8765/wrong?code=x&state=expected")
        coordinator.reset()

    asyncio.run(run())


def test_oauth_error_callback_requires_state_and_preserves_failed_status():
    async def run():
        coordinator = OAuthCallbackCoordinator(CALLBACK_URI)
        await coordinator.redirect_handler("https://robinhood.example/auth?state=expected")
        waiter = asyncio.create_task(coordinator.callback_handler())

        with pytest.raises(OAuthStateError):
            coordinator.submit_callback(f"{CALLBACK_URI}?error=access_denied&state=wrong")
        assert coordinator.public_status()["status"] == "authorization_required"
        assert waiter.done() is False

        with pytest.raises(OAuthStateError):
            coordinator.submit_callback(f"{CALLBACK_URI}?error=access_denied&state=expected")
        with pytest.raises(OAuthStateError):
            await waiter
        assert coordinator.public_status()["status"] == "failed"
        assert coordinator.public_status()["authorization_url_ready"] is False

    asyncio.run(run())


def test_oauth_reset_cannot_be_overwritten_by_cancelled_callback_waiter():
    async def run():
        coordinator = OAuthCallbackCoordinator(CALLBACK_URI)
        await coordinator.redirect_handler("https://robinhood.example/auth?state=expected")
        waiter = asyncio.create_task(coordinator.callback_handler())
        await asyncio.sleep(0)
        coordinator.reset()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert coordinator.public_status()["status"] == "idle"
        assert coordinator.public_status()["authorization_url_ready"] is False

    asyncio.run(run())


def test_oauth_provider_uses_fixed_endpoint_and_validated_callback():
    fake = _FakeKeyring()
    storage = KeyringTokenStorage(keyring_module=fake)
    coordinator = OAuthCallbackCoordinator(CALLBACK_URI)
    provider = create_robinhood_oauth_provider(CALLBACK_URI, storage, coordinator)
    assert provider.context.server_url == ROBINHOOD_MCP_ENDPOINT
    assert [str(uri) for uri in provider.context.client_metadata.redirect_uris] == [CALLBACK_URI]
    assert provider.context.storage is storage


def test_catalog_validation_separates_read_review_place_and_unknown_tools():
    tools = _complete_tools() + [_tool("cancel_option_order")]
    status = validate_tool_catalog(tools)
    assert status["schema_valid"] is True
    assert status["ready_for_direct_review"] is True
    assert status["placement_tools_detected"] is True
    assert status["placement_api_exposed"] is False
    assert status["confirmed_option_placement_supported"] is True
    assert set(REQUIRED_PREFLIGHT_READ_TOOLS) <= set(status["read_tools"])
    assert set(status["review_tools"]) == set(REVIEW_TOOL_ALLOWLIST)
    assert set(status["place_tools"]) == set(PLACE_TOOL_ALLOWLIST)
    assert status["unsupported_tools"] == ["cancel_option_order"]

    duplicate = validate_tool_catalog([_tool("get_accounts"), _tool("get_accounts")])
    assert duplicate["schema_valid"] is False
    malformed = validate_tool_catalog(
        [
            _tool("get_accounts", {"type": "array"}),
        ]
    )
    assert malformed["schema_valid"] is False


def test_tool_result_decoder_accepts_structured_or_json_text_and_rejects_errors():
    assert decode_tool_result({"structuredContent": {"data": {"ok": True}}}) == {
        "data": {"ok": True}
    }
    assert decode_tool_result(
        {
            "content": [{"type": "text", "text": '{"data":{"value":1}}'}],
            "isError": False,
        }
    ) == {"data": {"value": 1}}
    assert decode_tool_result({"already": "decoded"}) == {"already": "decoded"}
    with pytest.raises(ToolResultError):
        decode_tool_result({"isError": True, "content": [{"type": "text", "text": "secret"}]})
    with pytest.raises(ToolResultError):
        decode_tool_result({"content": [{"type": "text", "text": "not-json"}]})


def test_public_sanitizer_removes_account_ids_and_authentication_material():
    account = "123456789"
    clean = sanitize_public_data(
        {
            "account_number": account,
            "message": f"Account {account} is ready",
            "access_token": "secret-token",
            "nested": [{"client_secret": "secret-client"}],
            "account_key": "acct_0123456789abcdef",
        },
        account_numbers=[account],
    )
    rendered = json.dumps(clean)
    assert clean["account_number"] == "[redacted]"
    assert account not in clean["message"]
    assert "secret-token" not in rendered
    assert "secret-client" not in rendered
    assert clean["account_key"] == "acct_0123456789abcdef"


def test_client_calls_only_fixed_confirmed_option_placement_and_no_generic_place():
    async def run():
        fake_keyring = _FakeKeyring()
        storage = KeyringTokenStorage(keyring_module=fake_keyring)
        session = _FakeSession()
        client = RobinhoodMcpClient(
            CALLBACK_URI,
            token_storage=storage,
            session_factory=_session_factory(session),
        )
        status = await client.connect()
        assert status["connection_state"] == "connected"
        assert status["placement_api_exposed"] is False
        assert status["confirmed_option_placement_api_exposed"] is True
        assert not hasattr(client, "call_place_tool")

        schema = client.read_tool_input_schema("get_accounts")
        schema["properties"] = {"tampered": {"type": "string"}}
        assert "tampered" not in client.read_tool_input_schema("get_accounts").get("properties", {})
        with pytest.raises(ToolPolicyError):
            client.read_tool_input_schema("review_option_order")
        with pytest.raises(ToolPolicyError):
            client.read_tool_input_schema("place_option_order")

        accounts = await client.call_read_tool("get_accounts", {})
        assert accounts["data"]["accounts"][0]["account_number"] == "123456789"
        preview = await client.call_review_tool("review_option_order", {})
        assert preview == {"preview_id": "preview-1"}
        placed = await client.call_confirmed_option_order({"ref_id": "one-order"})
        assert placed == {"ok": True}
        assert [name for name, _ in session.calls] == [
            "get_accounts",
            "review_option_order",
            "place_option_order",
        ]

        with pytest.raises(ToolPolicyError):
            await client.call_read_tool("review_option_order", {})
        with pytest.raises(ToolPolicyError):
            await client.call_read_tool("place_option_order", {})
        with pytest.raises(ToolPolicyError):
            await client.call_review_tool("place_option_order", {})
        with pytest.raises(ToolPolicyError):
            await client._call_allowed_tool(
                "place_option_order",
                {},
                allowed=PLACE_TOOL_ALLOWLIST,
            )
        with pytest.raises(ToolPolicyError):
            await client.call_read_tool("cancel_option_order", {})
        assert len(session.calls) == 3

        public = await client.connection_status()
        rendered = json.dumps(public)
        assert public["known_account_count"] == 1
        assert public["raw_account_numbers_exposed"] is False
        assert "123456789" not in rendered

    asyncio.run(run())


def test_client_revalidates_live_schema_and_blocks_drift_before_tool_call():
    async def run():
        session = _FakeSession()
        client = RobinhoodMcpClient(
            CALLBACK_URI,
            token_storage=KeyringTokenStorage(keyring_module=_FakeKeyring()),
            session_factory=_session_factory(session),
        )
        await client.connect()
        session.tools = [
            _tool(
                name,
                {
                    "type": "object",
                    "required": ["new_required"] if name == "get_accounts" else [],
                    "properties": {"new_required": {"type": "string"}},
                    "additionalProperties": False,
                },
            )
            for name in sorted(
                set(REQUIRED_PREFLIGHT_READ_TOOLS)
                | set(REVIEW_TOOL_ALLOWLIST)
                | set(PLACE_TOOL_ALLOWLIST)
            )
        ]
        with pytest.raises(ToolSchemaError):
            await client.call_read_tool("get_accounts", {})
        assert session.calls == []

    asyncio.run(run())


def test_client_validates_arguments_against_live_schema():
    async def run():
        tools = _complete_tools()
        for row in tools:
            if row["name"] == "review_option_order":
                row["inputSchema"] = {
                    "type": "object",
                    "required": ["quantity"],
                    "properties": {"quantity": {"type": "string", "pattern": "^[1-9][0-9]*$"}},
                    "additionalProperties": False,
                }
        session = _FakeSession(tools)
        client = RobinhoodMcpClient(
            CALLBACK_URI,
            token_storage=KeyringTokenStorage(keyring_module=_FakeKeyring()),
            session_factory=_session_factory(session),
        )
        await client.connect()
        with pytest.raises(ToolSchemaError):
            await client.call_review_tool("review_option_order", {"quantity": 1})
        assert session.calls == []
        await client.call_review_tool("review_option_order", {"quantity": "1"})
        assert session.calls == [("review_option_order", {"quantity": "1"})]

    asyncio.run(run())


def test_client_blocks_cyclic_tool_pagination_without_retrying():
    class CyclicSession(_FakeSession):
        async def list_tools(self, cursor=None):
            self.list_calls.append(cursor)
            return {"tools": [], "nextCursor": "same"}

    async def run():
        session = CyclicSession()
        client = RobinhoodMcpClient(
            CALLBACK_URI,
            token_storage=KeyringTokenStorage(keyring_module=_FakeKeyring()),
            session_factory=_session_factory(session),
        )
        with pytest.raises(ToolCatalogError):
            await client.connect()
        assert session.list_calls == [None, "same"]
        assert session.calls == []

    asyncio.run(run())


def test_public_read_allowlist_does_not_contain_order_writes():
    assert not (READ_TOOL_ALLOWLIST & REVIEW_TOOL_ALLOWLIST)
    assert not (READ_TOOL_ALLOWLIST & PLACE_TOOL_ALLOWLIST)
    assert not (REVIEW_TOOL_ALLOWLIST & PLACE_TOOL_ALLOWLIST)
