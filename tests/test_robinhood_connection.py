# Purpose: Test the synchronous Robinhood MCP connection manager.
"""Thread, timeout, OAuth, sanitization, and one-shot operation tests."""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from optedge.robinhood_connection import (
    CONNECTION_MANAGER_SCHEMA,
    RobinhoodConnectionError,
    RobinhoodConnectionManager,
)
from optedge.robinhood_mcp import OAuthStateError

CALLBACK_URI = "http://127.0.0.1:8765/oauth/robinhood/callback"
RAW_ACCOUNT = "RH-123456789"
AUTH_URL = "https://robinhood.example/authorize?state=oauth-state-secret"


class _FakeClient:
    def __init__(self) -> None:
        self.connection_state = "disconnected"
        self.oauth_status = "idle"
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.callback_calls = 0
        self.read_calls: list[tuple[str, dict]] = []
        self.review_calls: list[tuple[str, dict]] = []
        self.place_calls: list[dict] = []
        self.loop_thread_ids: set[int] = set()
        self.connect_started = threading.Event()
        self.connect_finished = threading.Event()
        self.connect_cancelled = threading.Event()
        self.read_cancelled = threading.Event()
        self._connect_gate: asyncio.Event | None = None
        self.hang_read = False
        self.fail_read = False
        self.hang_status = False
        self.reported_error_code: str | None = None

    def _record_loop(self) -> None:
        self.loop_thread_ids.add(threading.get_ident())

    async def connect(self):
        self._record_loop()
        self.connect_calls += 1
        self.connection_state = "connecting"
        self.oauth_status = "authorization_required"
        self._connect_gate = asyncio.Event()
        self.connect_started.set()
        try:
            await self._connect_gate.wait()
        except asyncio.CancelledError:
            self.connect_cancelled.set()
            self.connection_state = "disconnected"
            self.oauth_status = "idle"
            raise
        self.connection_state = "connected"
        self.oauth_status = "complete"
        self.connect_finished.set()
        return await self.connection_status()

    async def connection_status(self):
        self._record_loop()
        if self.hang_status:
            await asyncio.Event().wait()
        return {
            "schema": "fake_connection_v1",
            "connection_state": self.connection_state,
            "oauth": {
                "status": self.oauth_status,
                "authorization_url_ready": self.oauth_status == "authorization_required",
                "state": "must-not-escape",
            },
            "account_number": RAW_ACCOUNT,
            "message": f"Account {RAW_ACCOUNT} is ready",
            "access_token": "access-secret",
            "last_error_code": self.reported_error_code,
            "confirmed_option_placement_api_exposed": True,
        }

    def authorization_url_for_browser(self):
        self._record_loop()
        if self.oauth_status != "authorization_required":
            raise OAuthStateError("secret OAuth detail")
        return AUTH_URL

    def submit_oauth_callback(self, callback_url: str):
        self._record_loop()
        self.callback_calls += 1
        if "code=" not in callback_url or "state=" not in callback_url:
            raise OAuthStateError("callback included secret details")
        self.oauth_status = "callback_received"
        if self._connect_gate is None:
            raise OAuthStateError("no pending OAuth attempt")
        self._connect_gate.set()

    async def disconnect(self):
        self._record_loop()
        self.disconnect_calls += 1
        self.connection_state = "disconnected"
        self.oauth_status = "idle"
        return await self.connection_status()

    async def call_read_tool(self, name: str, arguments: dict):
        self._record_loop()
        self.read_calls.append((name, dict(arguments)))
        if self.fail_read:
            raise RuntimeError(f"secret failure access-secret {RAW_ACCOUNT}")
        if self.hang_read:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.read_cancelled.set()
                raise
        return {
            "data": {"accounts": [{"account_number": RAW_ACCOUNT}]},
            "request": name,
        }

    def read_tool_input_schema(self, name: str):
        self._record_loop()
        if name != "get_accounts":
            raise RuntimeError("unknown schema")
        return {
            "type": "object",
            "properties": {"cursor": {"type": "string"}},
            "additionalProperties": False,
        }

    async def call_review_tool(self, name: str, arguments: dict):
        self._record_loop()
        self.review_calls.append((name, dict(arguments)))
        return {"preview_id": "preview-1", "disclosure": "review only"}

    async def call_confirmed_option_order(self, arguments: dict):
        self._record_loop()
        self.place_calls.append(dict(arguments))
        return {"order_id": "order-1"}


def _manager(client: _FakeClient, **kwargs) -> RobinhoodConnectionManager:
    return RobinhoodConnectionManager(
        CALLBACK_URI,
        client=client,
        connect_timeout_seconds=kwargs.pop("connect_timeout_seconds", 1.0),
        operation_timeout_seconds=kwargs.pop("operation_timeout_seconds", 1.0),
        status_timeout_seconds=kwargs.pop("status_timeout_seconds", 0.2),
        shutdown_timeout_seconds=kwargs.pop("shutdown_timeout_seconds", 1.0),
        **kwargs,
    )


def test_manager_uses_exactly_one_private_loop_thread_and_factory_runs_on_it():
    created: dict[str, object] = {}

    def factory(callback_uri: str):
        created["thread_id"] = threading.get_ident()
        created["callback_uri"] = callback_uri
        created["client"] = _FakeClient()
        return created["client"]

    manager = RobinhoodConnectionManager(
        CALLBACK_URI,
        client_factory=factory,
        status_timeout_seconds=0.2,
        shutdown_timeout_seconds=1.0,
    )
    try:
        status = manager.status()
        assert status["schema"] == CONNECTION_MANAGER_SCHEMA
        assert status["loop_thread_alive"] is True
        assert status["loop_thread_count"] == 1
        assert created["thread_id"] != threading.get_ident()
        assert created["callback_uri"] == CALLBACK_URI
        client = created["client"]
        assert isinstance(client, _FakeClient)
        assert client.loop_thread_ids == {created["thread_id"]}
    finally:
        stopped = manager.shutdown()
    assert stopped["shutdown_complete"] is True
    assert stopped["loop_thread_count"] == 0


def test_status_is_recursively_sanitized_and_contains_no_authorization_url():
    client = _FakeClient()
    client.reported_error_code = "secret_error_access_secret"
    manager = _manager(client)
    try:
        status = manager.status()
        rendered = json.dumps(status)
        assert status["account_data_persisted"] is False
        assert status["automatic_retry_enabled"] is False
        assert status["background_polling_enabled"] is False
        assert status["placement_api_exposed"] is False
        assert status["generic_tool_call_exposed"] is False
        assert status["last_error_code"] == "client_reported_error"
        assert status["client"]["last_error_code"] == "client_reported_error"
        assert "message" not in status["client"]
        assert RAW_ACCOUNT not in rendered
        assert "access-secret" not in rendered
        assert "secret_error_access_secret" not in rendered
        assert "must-not-escape" not in rendered
        assert AUTH_URL not in rendered
    finally:
        manager.shutdown()


def test_manager_returns_read_schema_on_private_loop_without_status_exposure():
    client = _FakeClient()
    manager = _manager(client)
    try:
        schema = manager.read_tool_input_schema("get_accounts")
        assert schema["properties"] == {"cursor": {"type": "string"}}
        schema["properties"]["secret"] = {"type": "string"}
        assert "secret" not in manager.read_tool_input_schema("get_accounts")["properties"]
        assert client.loop_thread_ids == {manager._thread.ident}
        assert "input_schema" not in json.dumps(manager.status())
    finally:
        manager.shutdown()


def test_start_connect_is_nonblocking_idempotent_and_oauth_waits_off_http_thread():
    client = _FakeClient()
    manager = _manager(client)
    try:
        started_at = time.perf_counter()
        first = manager.start_connect()
        elapsed = time.perf_counter() - started_at
        assert elapsed < 0.2
        assert first["connect_started"] is True
        assert first["connect_pending"] is True
        assert client.connect_started.wait(timeout=1)

        second = manager.start_connect()
        assert second["connect_started"] is False
        assert second["idempotent"] is True
        assert client.connect_calls == 1

        pending = manager.status()
        assert pending["connection_state"] == "authorization_required"
        assert pending["authorization_url_ready"] is True
        assert AUTH_URL not in json.dumps(pending)
        assert manager.authorization_url_for_browser() == AUTH_URL

        callback_url = f"{CALLBACK_URI}?code=oauth-code-secret&state=oauth-state-secret"
        accepted = manager.submit_oauth_callback(callback_url)
        assert accepted["callback_accepted"] is True
        assert client.connect_finished.wait(timeout=1)
        connected = manager.status()
        assert connected["connection_state"] == "connected"
        assert connected["connect_pending"] is False
        assert "oauth-code-secret" not in json.dumps(connected)
        assert client.callback_calls == 1
        assert client.connect_calls == 1
    finally:
        manager.shutdown()


def test_disconnect_cancels_pending_connect_then_clears_connection_explicitly():
    client = _FakeClient()
    manager = _manager(client)
    try:
        manager.start_connect()
        assert client.connect_started.wait(timeout=1)
        result = manager.disconnect()
        assert result["disconnected"] is True
        assert result["connection_state"] == "disconnected"
        assert client.connect_cancelled.wait(timeout=1)
        assert client.connect_calls == 1
        assert client.disconnect_calls == 1
    finally:
        manager.shutdown()


def test_read_review_and_fixed_option_place_are_one_shot_without_generic_call():
    client = _FakeClient()
    client.connection_state = "connected"
    manager = _manager(client)
    try:
        read = manager.call_read_tool("get_accounts", {})
        review = manager.call_review_tool(
            "review_option_order",
            {"quantity": "1"},
        )
        placed = manager.place_confirmed_option_order({"ref_id": "one-order"})
        assert read["data"]["accounts"][0]["account_number"] == RAW_ACCOUNT
        assert review["preview_id"] == "preview-1"
        assert placed["order_id"] == "order-1"
        assert client.read_calls == [("get_accounts", {})]
        assert client.review_calls == [("review_option_order", {"quantity": "1"})]
        assert client.place_calls == [{"ref_id": "one-order"}]
        assert not hasattr(manager, "call_place_tool")
        assert not hasattr(manager, "call_tool")

        rendered_status = json.dumps(manager.status())
        assert RAW_ACCOUNT not in rendered_status
        assert "preview-1" not in rendered_status
    finally:
        manager.shutdown()


def test_operation_failure_exposes_only_safe_code_and_never_retries():
    client = _FakeClient()
    client.connection_state = "connected"
    client.fail_read = True
    manager = _manager(client)
    try:
        with pytest.raises(RobinhoodConnectionError) as caught:
            manager.call_read_tool("get_accounts", {})
        assert caught.value.code == "operation_failed"
        assert str(caught.value) == "operation_failed"
        assert client.read_calls == [("get_accounts", {})]
        status = manager.status()
        assert status["last_error_code"] == "operation_failed"
        rendered = json.dumps(status)
        assert RAW_ACCOUNT not in rendered
        assert "access-secret" not in rendered
    finally:
        manager.shutdown()


def test_one_shot_read_timeout_cancels_without_retry():
    client = _FakeClient()
    client.connection_state = "connected"
    client.hang_read = True
    manager = _manager(client, operation_timeout_seconds=0.05)
    try:
        with pytest.raises(RobinhoodConnectionError) as caught:
            manager.call_read_tool("get_accounts", {})
        assert caught.value.code == "read_timeout"
        assert client.read_cancelled.wait(timeout=1)
        assert client.read_calls == [("get_accounts", {})]
        assert manager.status()["last_error_code"] == "read_timeout"
    finally:
        manager.shutdown()


def test_connect_timeout_is_bounded_and_does_not_retry():
    client = _FakeClient()
    manager = _manager(client, connect_timeout_seconds=0.05)
    try:
        first = manager.start_connect()
        assert first["connect_started"] is True
        assert client.connect_started.wait(timeout=1)
        assert client.connect_cancelled.wait(timeout=1)
        deadline = time.monotonic() + 1
        status = manager.status()
        while status["connect_pending"] and time.monotonic() < deadline:
            time.sleep(0.01)
            status = manager.status()
        assert status["connect_pending"] is False
        assert status["connection_state"] == "error"
        assert status["last_error_code"] == "connect_timeout"
        assert client.connect_calls == 1
    finally:
        manager.shutdown()


def test_status_probe_timeout_returns_cached_safe_status():
    client = _FakeClient()
    manager = _manager(client, status_timeout_seconds=0.05)
    try:
        manager.status()
        client.hang_status = True
        status = manager.status()
        assert status["status_probe_error_code"] == "status_timeout"
        assert status["connection_state"] == "disconnected"
    finally:
        manager.shutdown()


def test_authorization_accessor_failure_is_safe_and_callback_secret_is_not_cached():
    client = _FakeClient()
    manager = _manager(client)
    try:
        with pytest.raises(RobinhoodConnectionError) as caught:
            manager.authorization_url_for_browser()
        assert caught.value.code == "oauth_state_invalid"
        with pytest.raises(RobinhoodConnectionError) as callback_error:
            manager.submit_oauth_callback(f"{CALLBACK_URI}?bad=secret")
        assert callback_error.value.code == "oauth_state_invalid"
        rendered = json.dumps(manager.status())
        assert "bad=secret" not in rendered
        assert "secret OAuth detail" not in rendered
    finally:
        manager.shutdown()


def test_shutdown_is_bounded_idempotent_and_blocks_new_operations():
    client = _FakeClient()
    manager = _manager(client)
    manager.start_connect()
    assert client.connect_started.wait(timeout=1)
    first = manager.shutdown()
    second = manager.shutdown()
    assert first["shutdown_complete"] is True
    assert second["shutdown"] is True
    assert second["loop_thread_alive"] is False
    assert client.connect_cancelled.wait(timeout=1)
    with pytest.raises(RobinhoodConnectionError) as caught:
        manager.call_read_tool("get_accounts", {})
    assert caught.value.code == "manager_shutdown"


def test_client_factory_failure_keeps_only_safe_status_code():
    def failing_factory(callback_uri: str):
        raise RuntimeError(f"factory leaked {callback_uri} access-secret")

    manager = RobinhoodConnectionManager(
        CALLBACK_URI,
        client_factory=failing_factory,
        shutdown_timeout_seconds=1.0,
    )
    try:
        status = manager.status()
        assert status["connection_state"] == "error"
        assert status["last_error_code"] == "operation_failed"
        assert "access-secret" not in json.dumps(status)
    finally:
        manager.shutdown()
