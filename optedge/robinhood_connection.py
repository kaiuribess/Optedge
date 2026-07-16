# Purpose: Bridge the synchronous local cockpit to one private Robinhood MCP loop.
"""Thread-safe synchronous lifecycle manager for :mod:`optedge.robinhood_mcp`.

The local cockpit uses ``http.server`` request threads, while the official MCP
client is asynchronous.  This manager owns exactly one private asyncio loop on
one daemon thread and exposes bounded, synchronous operations to those request
threads.  A connection attempt may wait for the browser OAuth callback on the
private loop without blocking the HTTP server.

There is deliberately no generic tool dispatcher and no placement method.
Every network action is a direct method call from the operator; this module has
no poller, scheduler, retry loop, file credential fallback, or account-data
cache.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import math
import re
import threading
from collections.abc import Callable, Coroutine, Mapping
from typing import Any
from urllib.parse import urlparse

from optedge.robinhood_mcp import (
    ROBINHOOD_MCP_ENDPOINT,
    CallbackUriError,
    CredentialStorageError,
    OAuthStateError,
    RobinhoodMcpClient,
    RobinhoodMcpError,
    ToolCatalogError,
    ToolPolicyError,
    ToolResultError,
    ToolSchemaError,
    sanitize_public_data,
    validate_callback_uri,
)

CONNECTION_MANAGER_SCHEMA = "optedge_robinhood_connection_manager_v1"
_SAFE_CONNECTION_STATES = frozenset(
    {
        "authorization_required",
        "connected",
        "connected_limited",
        "connecting",
        "disconnected",
        "disconnecting",
        "error",
        "shutdown",
        "starting",
    }
)
_SAFE_OAUTH_STATES = frozenset(
    {
        "authorization_required",
        "callback_received",
        "complete",
        "failed",
        "idle",
    }
)
_SAFE_ERROR_CODES = frozenset(
    {
        "authorization_url_invalid",
        "authorization_url_timeout",
        "authorization_url_unavailable",
        "callback_uri_invalid",
        "client_initialization_timeout",
        "client_reported_error",
        "client_unavailable",
        "connect_timeout",
        "connection_or_transport_error",
        "credential_storage_unavailable",
        "disconnect_timeout",
        "loop_start_timeout",
        "loop_thread_reentry_blocked",
        "loop_unavailable",
        "manager_shutdown",
        "mcp_operation_failed",
        "oauth_callback_timeout",
        "oauth_state_invalid",
        "operation_cancelled",
        "operation_failed",
        "read_timeout",
        "review_timeout",
        "shutdown_timeout",
        "status_timeout",
        "tool_arguments_invalid",
        "tool_catalog_invalid",
        "tool_policy_blocked",
        "tool_result_invalid",
    }
)
_TOOL_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,79}")


class RobinhoodConnectionError(RuntimeError):
    """A bounded manager failure containing only a safe categorical code."""

    def __init__(self, code: str) -> None:
        safe_code = str(code or "operation_failed").strip().lower()
        if not safe_code.replace("_", "").isalnum():
            safe_code = "operation_failed"
        self.code = safe_code
        super().__init__(safe_code)


def _bounded_timeout(value: Any, *, default: float, maximum: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    if not math.isfinite(result) or result <= 0:
        result = default
    return min(max(result, 0.01), maximum)


def _account_numbers_in(value: Any) -> set[str]:
    account_keys = {
        "account_number",
        "rhs_account_number",
        "brokerage_account_number",
    }
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key or "").strip().lower() in account_keys:
                account = str(child or "").strip()
                if account:
                    found.add(account)
            else:
                found.update(_account_numbers_in(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.update(_account_numbers_in(child))
    return found


def _sanitize_status(value: Any) -> dict[str, Any]:
    """Copy and redact even a faulty injected client's status payload."""
    if not isinstance(value, Mapping):
        return {}
    cleaned = sanitize_public_data(
        dict(value),
        account_numbers=_account_numbers_in(value),
    )
    return cleaned if isinstance(cleaned, dict) else {}


def _safe_error_code_value(value: Any) -> str | None:
    code = str(value or "").strip().lower()
    if not code:
        return None
    return code if code in _SAFE_ERROR_CODES else "client_reported_error"


def _safe_tool_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(
        {
            name
            for item in value
            for name in (str(item or "").strip(),)
            if _TOOL_NAME_PATTERN.fullmatch(name)
        }
    )[:100]


def _project_client_status(value: Any) -> dict[str, Any]:
    """Allow only the non-secret client fields the cockpit actually needs."""
    cleaned = _sanitize_status(value)
    state = str(cleaned.get("connection_state") or "").strip().lower()
    if state not in _SAFE_CONNECTION_STATES:
        state = "error"
    oauth_raw = cleaned.get("oauth") if isinstance(cleaned.get("oauth"), dict) else {}
    oauth_state = str(oauth_raw.get("status") or "").strip().lower()
    if oauth_state not in _SAFE_OAUTH_STATES:
        oauth_state = "failed" if oauth_state else "idle"
    credential_raw = (
        cleaned.get("credential_storage")
        if isinstance(cleaned.get("credential_storage"), dict)
        else {}
    )
    catalog_raw = (
        cleaned.get("tool_catalog") if isinstance(cleaned.get("tool_catalog"), dict) else {}
    )
    try:
        tool_count = max(0, min(int(catalog_raw.get("tool_count") or 0), 10_000))
    except (TypeError, ValueError):
        tool_count = 0
    try:
        known_account_count = max(
            0,
            min(int(cleaned.get("known_account_count") or 0), 10_000),
        )
    except (TypeError, ValueError):
        known_account_count = 0
    issues = catalog_raw.get("issues") if isinstance(catalog_raw.get("issues"), list) else []
    return {
        "schema": "optedge_robinhood_mcp_connection_v1",
        "endpoint": (
            ROBINHOOD_MCP_ENDPOINT if cleaned.get("endpoint") == ROBINHOOD_MCP_ENDPOINT else None
        ),
        "connection_state": state,
        "last_error_code": _safe_error_code_value(cleaned.get("last_error_code")),
        "oauth": {
            "status": oauth_state,
            "authorization_url_ready": oauth_raw.get("authorization_url_ready") is True,
            "contains_authorization_url": False,
            "contains_code_or_state": False,
        },
        "credential_storage": {
            "backend_ready": credential_raw.get("backend_ready") is True,
            "plaintext_fallback_allowed": False,
            "token_present": credential_raw.get("token_present") is True,
            "client_registration_present": (
                credential_raw.get("client_registration_present") is True
            ),
        },
        "tool_catalog": {
            "schema_valid": catalog_raw.get("schema_valid") is True,
            "ready_for_direct_review": (catalog_raw.get("ready_for_direct_review") is True),
            "placement_tools_detected": (catalog_raw.get("placement_tools_detected") is True),
            "placement_api_exposed": False,
            "tool_count": tool_count,
            "read_tools": _safe_tool_names(catalog_raw.get("read_tools")),
            "review_tools": _safe_tool_names(catalog_raw.get("review_tools")),
            "place_tools": _safe_tool_names(catalog_raw.get("place_tools")),
            "missing_required_read_tools": _safe_tool_names(
                catalog_raw.get("missing_required_read_tools")
            ),
            "missing_review_tools": _safe_tool_names(catalog_raw.get("missing_review_tools")),
            "missing_place_tools": _safe_tool_names(catalog_raw.get("missing_place_tools")),
            "unsupported_tools": _safe_tool_names(catalog_raw.get("unsupported_tools")),
            "issue_count": len(issues),
        },
        "known_account_count": known_account_count,
        "raw_account_numbers_exposed": False,
        "generic_tool_call_exposed": False,
        "placement_api_exposed": False,
        "automatic_retry_enabled": False,
        "background_polling_enabled": False,
    }


ClientFactory = Callable[[str], Any]


class RobinhoodConnectionManager:
    """Run one ``RobinhoodMcpClient`` on one private asyncio loop thread."""

    def __init__(
        self,
        callback_uri: str,
        *,
        client: Any | None = None,
        client_factory: ClientFactory | None = None,
        connect_timeout_seconds: float = 300.0,
        operation_timeout_seconds: float = 45.0,
        status_timeout_seconds: float = 1.0,
        shutdown_timeout_seconds: float = 5.0,
    ) -> None:
        if client is not None and client_factory is not None:
            raise ValueError("Pass either client or client_factory, not both.")
        self.callback_uri = validate_callback_uri(callback_uri)
        self.connect_timeout_seconds = _bounded_timeout(
            connect_timeout_seconds,
            default=300.0,
            maximum=600.0,
        )
        self.operation_timeout_seconds = _bounded_timeout(
            operation_timeout_seconds,
            default=45.0,
            maximum=120.0,
        )
        self.status_timeout_seconds = _bounded_timeout(
            status_timeout_seconds,
            default=1.0,
            maximum=5.0,
        )
        self.shutdown_timeout_seconds = _bounded_timeout(
            shutdown_timeout_seconds,
            default=5.0,
            maximum=30.0,
        )

        self._lock = threading.RLock()
        self._loop = asyncio.new_event_loop()
        self._loop_ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="OptedgeRobinhoodMCP",
            daemon=True,
        )
        self._client: Any | None = None
        self._client_status: dict[str, Any] = {}
        self._connection_state = "starting"
        self._last_error_code: str | None = None
        self._connect_future: concurrent.futures.Future[Any] | None = None
        self._inflight: set[concurrent.futures.Future[Any]] = set()
        self._shutdown = False
        self._thread.start()
        if not self._loop_ready.wait(timeout=self.shutdown_timeout_seconds):
            self._connection_state = "error"
            self._last_error_code = "loop_start_timeout"
            return

        factory = client_factory or (lambda uri: RobinhoodMcpClient(uri))
        try:
            if client is not None:
                self._client = client
            else:
                self._client = self._submit(
                    self._create_client(factory),
                    timeout=self.shutdown_timeout_seconds,
                    timeout_code="client_initialization_timeout",
                )
            self._connection_state = "disconnected"
        except RobinhoodConnectionError as exc:
            self._connection_state = "error"
            self._last_error_code = exc.code

    async def _create_client(self, factory: ClientFactory) -> Any:
        return factory(self.callback_uri)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        try:
            self._loop.run_forever()
        finally:
            pending = list(asyncio.all_tasks(self._loop))
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    @staticmethod
    def _safe_error_code(exc: BaseException, *, timeout_code: str) -> str:
        if isinstance(exc, (concurrent.futures.TimeoutError, TimeoutError)):
            return timeout_code
        if isinstance(exc, (concurrent.futures.CancelledError, asyncio.CancelledError)):
            return "operation_cancelled"
        if isinstance(exc, CallbackUriError):
            return "callback_uri_invalid"
        if isinstance(exc, CredentialStorageError):
            return "credential_storage_unavailable"
        if isinstance(exc, OAuthStateError):
            return "oauth_state_invalid"
        if isinstance(exc, ToolCatalogError):
            return "tool_catalog_invalid"
        if isinstance(exc, ToolPolicyError):
            return "tool_policy_blocked"
        if isinstance(exc, ToolSchemaError):
            return "tool_arguments_invalid"
        if isinstance(exc, ToolResultError):
            return "tool_result_invalid"
        if isinstance(exc, RobinhoodMcpError):
            return "mcp_operation_failed"
        return "operation_failed"

    def _submit(
        self,
        coroutine: Coroutine[Any, Any, Any],
        *,
        timeout: float,
        timeout_code: str,
    ) -> Any:
        with self._lock:
            unavailable = self._shutdown or not self._thread.is_alive()
        if unavailable:
            coroutine.close()
            raise RobinhoodConnectionError("manager_shutdown")
        if threading.current_thread() is self._thread:
            coroutine.close()
            raise RobinhoodConnectionError("loop_thread_reentry_blocked")
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        except Exception as exc:
            coroutine.close()
            raise RobinhoodConnectionError("loop_unavailable") from exc
        with self._lock:
            self._inflight.add(future)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise RobinhoodConnectionError(timeout_code) from exc
        except BaseException as exc:
            code = self._safe_error_code(exc, timeout_code=timeout_code)
            raise RobinhoodConnectionError(code) from exc
        finally:
            with self._lock:
                self._inflight.discard(future)

    async def _client_connection_status(self) -> Any:
        client = self._require_client()
        return await client.connection_status()

    def _require_client(self) -> Any:
        client = self._client
        if client is None:
            raise RobinhoodConnectionError("client_unavailable")
        return client

    def _cache_client_status(self, status: Any) -> dict[str, Any]:
        cleaned = _project_client_status(status)
        with self._lock:
            self._client_status = cleaned
            state = str(cleaned.get("connection_state") or "").strip().lower()
            preserve_manager_error = self._connection_state == "error" and state not in {
                "connected",
                "connected_limited",
                "error",
            }
            if (
                state
                and state not in {"connecting", "authorization_required"}
                and not preserve_manager_error
            ):
                self._connection_state = state
            safe_error = _safe_error_code_value(cleaned.get("last_error_code"))
            if safe_error:
                self._last_error_code = safe_error
        return cleaned

    def _compose_status(
        self,
        *,
        status_probe_error_code: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            client_status = dict(self._client_status)
            connect_pending = bool(
                self._connect_future is not None and not self._connect_future.done()
            )
            connection_state = self._connection_state
            last_error_code = self._last_error_code
            is_shutdown = self._shutdown
            thread_alive = self._thread.is_alive()
        oauth = client_status.get("oauth") if isinstance(client_status.get("oauth"), dict) else {}
        oauth_status = str(oauth.get("status") or "").strip().lower()
        if connect_pending and oauth_status in {"authorization_required", "callback_received"}:
            connection_state = "authorization_required"
        elif connect_pending and connection_state not in {"authorization_required"}:
            connection_state = "connecting"
        return {
            "schema": CONNECTION_MANAGER_SCHEMA,
            "connection_state": connection_state,
            "connect_pending": connect_pending,
            "authorization_url_ready": oauth.get("authorization_url_ready") is True,
            "last_error_code": last_error_code,
            "status_probe_error_code": status_probe_error_code,
            "loop_thread_alive": thread_alive,
            "loop_thread_count": 1 if thread_alive else 0,
            "shutdown": is_shutdown,
            "automatic_retry_enabled": False,
            "background_polling_enabled": False,
            "generic_tool_call_exposed": False,
            "placement_api_exposed": False,
            "account_data_persisted": False,
            "client": client_status,
        }

    def status(self) -> dict[str, Any]:
        """Return a bounded, recursively sanitized connection snapshot."""
        with self._lock:
            can_probe = not self._shutdown and self._client is not None and self._thread.is_alive()
        probe_error: str | None = None
        if can_probe:
            try:
                status = self._submit(
                    self._client_connection_status(),
                    timeout=self.status_timeout_seconds,
                    timeout_code="status_timeout",
                )
                self._cache_client_status(status)
            except RobinhoodConnectionError as exc:
                probe_error = exc.code
        return _sanitize_status(self._compose_status(status_probe_error_code=probe_error))

    async def _connect_once(self) -> Any:
        client = self._require_client()
        return await asyncio.wait_for(
            client.connect(),
            timeout=self.connect_timeout_seconds,
        )

    def _connect_done(self, future: concurrent.futures.Future[Any]) -> None:
        with self._lock:
            if self._connect_future is not future:
                self._inflight.discard(future)
                return
            self._connect_future = None
            self._inflight.discard(future)
            disconnecting = self._connection_state in {"disconnecting", "shutdown"}
        try:
            result = future.result()
        except BaseException as exc:
            if disconnecting and isinstance(
                exc,
                (concurrent.futures.CancelledError, asyncio.CancelledError),
            ):
                return
            code = self._safe_error_code(exc, timeout_code="connect_timeout")
            with self._lock:
                self._connection_state = "error"
                self._last_error_code = code
            return
        cleaned = self._cache_client_status(result)
        with self._lock:
            self._connection_state = (
                str(cleaned.get("connection_state") or "connected").strip().lower()
            )
            self._last_error_code = None

    def start_connect(self) -> dict[str, Any]:
        """Start one nonblocking connection attempt; never retry automatically."""
        with self._lock:
            if self._shutdown:
                result = self._compose_status()
                result.update({"connect_started": False, "idempotent": True})
                return result
            if self._client is None:
                self._connection_state = "error"
                self._last_error_code = "client_unavailable"
                result = self._compose_status()
                result.update({"connect_started": False, "idempotent": True})
                return result
            if self._connect_future is not None and not self._connect_future.done():
                result = self._compose_status()
                result.update({"connect_started": False, "idempotent": True})
                return result
            if self._connection_state in {"connected", "connected_limited"}:
                result = self._compose_status()
                result.update({"connect_started": False, "idempotent": True})
                return result
            self._connection_state = "connecting"
            self._last_error_code = None
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._connect_once(),
                    self._loop,
                )
            except Exception:
                self._connection_state = "error"
                self._last_error_code = "loop_unavailable"
                result = self._compose_status()
                result.update({"connect_started": False, "idempotent": False})
                return result
            self._connect_future = future
            self._inflight.add(future)
            future.add_done_callback(self._connect_done)
            result = self._compose_status()
            result.update({"connect_started": True, "idempotent": False})
            return result

    async def _trusted_authorization_url(self) -> str:
        return self._require_client().authorization_url_for_browser()

    def authorization_url_for_browser(self) -> str:
        """Return the pending OAuth URL only to trusted local route code."""
        try:
            value = self._submit(
                self._trusted_authorization_url(),
                timeout=self.status_timeout_seconds,
                timeout_code="authorization_url_timeout",
            )
        except RobinhoodConnectionError as exc:
            if exc.code == "operation_failed":
                raise RobinhoodConnectionError("authorization_url_unavailable") from exc
            raise
        parsed = urlparse(str(value or ""))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise RobinhoodConnectionError("authorization_url_invalid")
        return str(value)

    async def _submit_callback_once(self, callback_url: str) -> None:
        self._require_client().submit_oauth_callback(callback_url)

    def submit_oauth_callback(self, callback_url: str) -> dict[str, Any]:
        """Submit one exact callback to the pending attempt; never cache its URL."""
        try:
            self._submit(
                self._submit_callback_once(str(callback_url or "")),
                timeout=self.status_timeout_seconds,
                timeout_code="oauth_callback_timeout",
            )
        except RobinhoodConnectionError as exc:
            with self._lock:
                self._last_error_code = exc.code
            raise
        result = self.status()
        result["callback_accepted"] = True
        return result

    async def _disconnect_once(self) -> Any:
        return await self._require_client().disconnect()

    def disconnect(self) -> dict[str, Any]:
        """Explicitly clear OAuth state after cancelling a pending connection."""
        with self._lock:
            if self._shutdown:
                return self._compose_status()
            self._connection_state = "disconnecting"
            connect_future = self._connect_future
            self._connect_future = None
        if connect_future is not None and not connect_future.done():
            connect_future.cancel()
        try:
            result = self._submit(
                self._disconnect_once(),
                timeout=self.operation_timeout_seconds,
                timeout_code="disconnect_timeout",
            )
            self._cache_client_status(result)
        except RobinhoodConnectionError as exc:
            with self._lock:
                self._connection_state = "error"
                self._last_error_code = exc.code
            raise
        with self._lock:
            self._connection_state = "disconnected"
            self._last_error_code = None
            self._client_status = _project_client_status(result)
        output = self._compose_status()
        output["disconnected"] = True
        return output

    def _call_timeout(self, value: float | None) -> float:
        if value is None:
            return self.operation_timeout_seconds
        return min(
            _bounded_timeout(
                value,
                default=self.operation_timeout_seconds,
                maximum=120.0,
            ),
            self.operation_timeout_seconds,
        )

    async def _read_once(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self._require_client().call_read_tool(name, arguments)

    async def _read_schema_once(self, name: str) -> dict[str, Any]:
        return self._require_client().read_tool_input_schema(name)

    def read_tool_input_schema(self, name: str) -> dict[str, Any]:
        """Return one allowlisted live input schema for a bounded read workflow."""
        try:
            value = self._submit(
                self._read_schema_once(name),
                timeout=self.status_timeout_seconds,
                timeout_code="status_timeout",
            )
        except RobinhoodConnectionError as exc:
            with self._lock:
                self._last_error_code = exc.code
            raise
        if not isinstance(value, dict):
            raise RobinhoodConnectionError("tool_catalog_invalid")
        return value

    def call_read_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        """Run one explicit read call and return it without retaining account data."""
        try:
            return self._submit(
                self._read_once(name, arguments),
                timeout=self._call_timeout(timeout_seconds),
                timeout_code="read_timeout",
            )
        except RobinhoodConnectionError as exc:
            with self._lock:
                self._last_error_code = exc.code
            raise

    async def _review_once(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self._require_client().call_review_tool(name, arguments)

    def call_review_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        """Run one explicit preview call; this manager cannot place an order."""
        try:
            return self._submit(
                self._review_once(name, arguments),
                timeout=self._call_timeout(timeout_seconds),
                timeout_code="review_timeout",
            )
        except RobinhoodConnectionError as exc:
            with self._lock:
                self._last_error_code = exc.code
            raise

    def shutdown(self) -> dict[str, Any]:
        """Boundedly stop the sole loop thread without clearing persisted OAuth."""
        with self._lock:
            if self._shutdown:
                return self._compose_status()
            self._shutdown = True
            self._connection_state = "shutdown"
            futures = list(self._inflight)
            self._inflight.clear()
            self._connect_future = None
        for future in futures:
            future.cancel()
        if self._thread.is_alive() and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except RuntimeError:
                pass
            if threading.current_thread() is not self._thread:
                self._thread.join(timeout=self.shutdown_timeout_seconds)
        with self._lock:
            if self._thread.is_alive():
                self._last_error_code = "shutdown_timeout"
            else:
                self._last_error_code = None
        result = self._compose_status()
        result["shutdown_complete"] = not self._thread.is_alive()
        return result

    def __enter__(self) -> RobinhoodConnectionManager:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.shutdown()


__all__ = [
    "CONNECTION_MANAGER_SCHEMA",
    "RobinhoodConnectionError",
    "RobinhoodConnectionManager",
]
