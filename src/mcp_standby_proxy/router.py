import asyncio
import logging
import time
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Callable

from mcp_standby_proxy.cache import CacheData, CacheManager
from mcp_standby_proxy.config import ProxyConfig
from mcp_standby_proxy.errors import FailureReason, LifecycleError, TransportError
from mcp_standby_proxy.jsonrpc import (
    INTERNAL_ERROR,
    METHOD_NOT_FOUND,
    IdMapper,
    JsonRpcWriter,
    make_error,
    make_response,
)
from mcp_standby_proxy.lifecycle import LifecycleManager
from mcp_standby_proxy.state import BackendState, StateMachine
from mcp_standby_proxy.transport.base import BackendTransport

logger = logging.getLogger(__name__)

# Cooldown in seconds before retrying a failed backend — split by failure reason (FR-22.5)
FAILURE_COOLDOWN_START = 10.0       # preserves prior behavior for start-time failures
FAILURE_COOLDOWN_MIDSESSION = 5.0   # shorter window for mid-session transport deaths

# Methods that are served from cache or locally without backend
_CACHED_METHODS = frozenset(["tools/list", "resources/list", "prompts/list"])

# MCP protocol version
MCP_PROTOCOL_VERSION = "2024-11-05"

# Default capabilities when no cache exists. Declares tool support so
# MCP clients send tools/list, triggering cold bootstrap (FR-1.3).
_DEFAULT_CAPABILITIES: Mapping[str, Any] = MappingProxyType({"tools": {}})


def _resolve_capabilities(
    backend_capabilities: dict[str, Any], cache_data: dict[str, Any]
) -> dict[str, Any]:
    """Derive capabilities to store in cache.

    If backend returned non-empty capabilities, use them as-is.
    Otherwise derive from method keys present in cache_data.
    Falls back to _DEFAULT_CAPABILITIES if derivation yields nothing.
    """
    if backend_capabilities:
        return backend_capabilities
    derived: dict[str, dict[str, Any]] = {}
    if "tools/list" in cache_data:
        derived["tools"] = {}
    if "resources/list" in cache_data:
        derived["resources"] = {}
    if "prompts/list" in cache_data:
        derived["prompts"] = {}
    return derived or dict(_DEFAULT_CAPABILITIES)


class MessageRouter:
    """Routes incoming JSON-RPC messages from clients to backends.

    Handles cache serving, backend lifecycle (lazy start), request queuing
    during startup, and ID remapping.
    """

    def __init__(
        self,
        config: ProxyConfig,
        state_machine: StateMachine,
        lifecycle_manager: LifecycleManager,
        cache_manager: CacheManager,
        transport_factory: Callable[[], BackendTransport],
        writer: JsonRpcWriter,
    ) -> None:
        self._config = config
        self._sm = state_machine
        self._lifecycle = lifecycle_manager
        self._cache = cache_manager
        self._transport_factory = transport_factory
        self._writer = writer
        self._transport: BackendTransport | None = None
        self._id_mapper = IdMapper()
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._failure_time: tuple[float, FailureReason] | None = None
        self._initialized = False

    async def handle_message(self, message: dict[str, Any]) -> None:
        """Route a single incoming JSON-RPC message."""
        method = message.get("method", "")
        msg_id = message.get("id")
        is_request = msg_id is not None
        is_notification = not is_request

        logger.debug("[%s] Received: %s (id=%s)", self._config.server.name, method, msg_id)

        if method == "initialize" and is_request:
            await self._handle_initialize(message)

        elif method == "notifications/initialized" and is_notification:
            self._initialized = True
            logger.debug("[%s] Client initialized", self._config.server.name)

        elif method == "ping" and is_request:
            await self._writer.write_message(make_response(id=msg_id, result={}))

        elif method in _CACHED_METHODS and is_request:
            await self._handle_cacheable(message)

        elif is_request:
            await self._handle_forwarded_request(message)

        else:
            # Notification
            state = self._sm.state
            if state == BackendState.ACTIVE and self._transport is not None:
                try:
                    await self._transport.notify(method, message.get("params"))
                except TransportError as exc:
                    # Mid-session transport death on a notification path (FR-22.4).
                    # Transition to FAILED under lock; no retry (notifications are fire-and-forget).
                    await self._detect_transport_death(method, exc)
                except Exception as exc:
                    logger.warning(
                        "[%s] Failed to forward notification: %s",
                        self._config.server.name,
                        exc,
                    )
            else:
                logger.debug(
                    "[%s] Dropping notification %s (backend not active)",
                    self._config.server.name,
                    method,
                )

    async def _handle_initialize(self, message: dict[str, Any]) -> None:
        """Respond to MCP initialize with proxy server info and capabilities."""
        msg_id = message.get("id")
        cache_data = self._cache.load()
        capabilities = (cache_data.get("capabilities") or dict(_DEFAULT_CAPABILITIES)) if cache_data else dict(_DEFAULT_CAPABILITIES)

        response = make_response(
            id=msg_id,
            result={
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": capabilities,
                "serverInfo": {
                    "name": self._config.server.name,
                    "version": self._config.server.version,
                },
                **(
                    {"instructions": self._config.server.instructions}
                    if self._config.server.instructions
                    else {}
                ),
            },
        )
        await self._writer.write_message(response)

    async def _handle_cacheable(self, message: dict[str, Any]) -> None:
        """Serve tools/list, resources/list, or prompts/list.

        Cache hit: respond immediately.
        Cache miss: start backend, fetch from backend, cache, respond.
        """
        method = message["method"]
        msg_id = message.get("id")
        cache_data = self._cache.load()

        if cache_data is not None and method in cache_data:
            logger.debug("[%s] Serving %s from cache", self._config.server.name, method)
            await self._writer.write_message(make_response(id=msg_id, result=cache_data[method]))
            return

        # Cache miss — need to start backend and fetch
        logger.info("[%s] Cache miss for %s, starting backend", self._config.server.name, method)
        try:
            await self.ensure_active()
        except LifecycleError as exc:
            await self._writer.write_message(
                make_error(id=msg_id, code=INTERNAL_ERROR, message=str(exc))
            )
            return

        assert self._transport is not None
        try:
            internal_id = self._id_mapper.next_internal_id()
            result = await self._transport.request(method, id=internal_id)
            actual_result = result.get("result", {})
            await self._writer.write_message(make_response(id=msg_id, result=actual_result))

            # Reload cache to pick up capabilities derived during activation
            cache_data = self._cache.load()

            # Save to cache asynchronously — update() first, then fix capabilities
            new_cache = CacheData(cache_version=1)
            if cache_data:
                new_cache.update(cache_data)
            new_cache["capabilities"] = new_cache.get("capabilities") or dict(_DEFAULT_CAPABILITIES)
            new_cache[method] = actual_result
            asyncio.create_task(self._cache.save(new_cache))

        except TransportError as exc:
            # Mid-session transport death — attempt single inline recovery (FR-22.2)
            await self._detect_transport_death(method, exc)
            timeout = min(self._config.lifecycle.start.timeout, 60.0)

            async def _cacheable_retry() -> dict[str, Any]:
                logger.info(
                    "[%s] restarting backend after mid-session transport death",
                    self._config.server.name,
                )
                await self.ensure_active()
                assert self._transport is not None
                retry_internal_id = self._id_mapper.next_internal_id()
                return await self._transport.request(method, id=retry_internal_id)

            try:
                retry_result = await asyncio.wait_for(_cacheable_retry(), timeout=timeout)
                actual_result = retry_result.get("result", {})
                await self._writer.write_message(make_response(id=msg_id, result=actual_result))
                logger.info(
                    "[%s] transport recovered; %s succeeded",
                    self._config.server.name,
                    method,
                )

                cache_data = self._cache.load()
                new_cache = CacheData(cache_version=1)
                if cache_data:
                    new_cache.update(cache_data)
                new_cache["capabilities"] = new_cache.get("capabilities") or dict(_DEFAULT_CAPABILITIES)
                new_cache[method] = actual_result
                asyncio.create_task(self._cache.save(new_cache))

            except LifecycleError as retry_exc:
                self._failure_time = (time.monotonic(), FailureReason.MIDSESSION)
                error_msg = f"transport died during {method}; restart failed: {retry_exc}"
                logger.warning(
                    "[%s] transport recovery failed: %s", self._config.server.name, retry_exc
                )
                await self._writer.write_message(
                    make_error(id=msg_id, code=INTERNAL_ERROR, message=error_msg)
                )
            except TransportError as retry_exc:
                self._failure_time = (time.monotonic(), FailureReason.MIDSESSION)
                error_msg = f"transport died during {method}; retry after restart also failed: {retry_exc}"
                logger.warning(
                    "[%s] transport recovery failed: %s", self._config.server.name, retry_exc
                )
                await self._writer.write_message(
                    make_error(id=msg_id, code=INTERNAL_ERROR, message=error_msg)
                )
            except asyncio.TimeoutError:
                self._failure_time = (time.monotonic(), FailureReason.MIDSESSION)
                error_msg = f"transport died during {method}; restart failed: timed out after {timeout:.1f}s"
                logger.warning(
                    "[%s] transport recovery failed: timed out after %.1fs",
                    self._config.server.name,
                    timeout,
                )
                await self._writer.write_message(
                    make_error(id=msg_id, code=INTERNAL_ERROR, message=error_msg)
                )
            except Exception as retry_exc:
                self._failure_time = (time.monotonic(), FailureReason.MIDSESSION)
                logger.warning(
                    "[%s] transport recovery failed: %s", self._config.server.name, retry_exc
                )
                await self._writer.write_message(
                    make_error(id=msg_id, code=INTERNAL_ERROR, message=str(retry_exc))
                )

        except Exception as exc:
            logger.error("[%s] Failed to fetch %s: %s", self._config.server.name, method, exc)
            await self._writer.write_message(
                make_error(id=msg_id, code=INTERNAL_ERROR, message=str(exc))
            )

    async def _handle_forwarded_request(self, message: dict[str, Any]) -> None:
        """Forward a request to the backend. Start backend if needed."""
        method = message.get("method", "")
        msg_id = message.get("id")

        state = self._sm.state
        if state == BackendState.COLD and not self._could_be_backend_method(method):
            await self._writer.write_message(
                make_error(
                    id=msg_id,
                    code=METHOD_NOT_FOUND,
                    message=f"Method not found: {method}",
                )
            )
            return

        try:
            await self.ensure_active()
        except LifecycleError as exc:
            await self._writer.write_message(
                make_error(id=msg_id, code=INTERNAL_ERROR, message=str(exc))
            )
            return

        assert self._transport is not None
        proxy_id = self._id_mapper.wrap(msg_id)
        try:
            result = await self._transport.request(method, message.get("params"), id=proxy_id)
            original_id = self._id_mapper.unwrap(proxy_id)
            # Forward whatever the backend returned, with original client ID
            if "result" in result:
                await self._writer.write_message(make_response(id=original_id, result=result["result"]))
            elif "error" in result:
                err = result["error"]
                await self._writer.write_message(
                    make_error(
                        id=original_id,
                        code=err.get("code", INTERNAL_ERROR),
                        message=err.get("message", "Backend error"),
                        data=err.get("data"),
                    )
                )

        except TransportError as exc:
            # Mid-session transport death — recover original_id then attempt single inline recovery (FR-22.2).
            # unwrap() is destructive; call it exactly once here, before the recovery path.
            try:
                original_id = self._id_mapper.unwrap(proxy_id)
            except KeyError:
                original_id = msg_id

            await self._detect_transport_death(method, exc)
            timeout = min(self._config.lifecycle.start.timeout, 60.0)

            async def _forwarded_retry() -> dict[str, Any]:
                logger.info(
                    "[%s] restarting backend after mid-session transport death",
                    self._config.server.name,
                )
                await self.ensure_active()
                assert self._transport is not None
                retry_proxy_id = self._id_mapper.wrap(msg_id)
                result = await self._transport.request(method, message.get("params"), id=retry_proxy_id)
                self._id_mapper.unwrap(retry_proxy_id)
                return result

            try:
                retry_result = await asyncio.wait_for(_forwarded_retry(), timeout=timeout)
                logger.info(
                    "[%s] transport recovered; %s succeeded",
                    self._config.server.name,
                    method,
                )
                if "result" in retry_result:
                    await self._writer.write_message(
                        make_response(id=original_id, result=retry_result["result"])
                    )
                elif "error" in retry_result:
                    err = retry_result["error"]
                    await self._writer.write_message(
                        make_error(
                            id=original_id,
                            code=err.get("code", INTERNAL_ERROR),
                            message=err.get("message", "Backend error"),
                            data=err.get("data"),
                        )
                    )

            except LifecycleError as retry_exc:
                self._failure_time = (time.monotonic(), FailureReason.MIDSESSION)
                error_msg = f"transport died during {method}; restart failed: {retry_exc}"
                logger.warning(
                    "[%s] transport recovery failed: %s", self._config.server.name, retry_exc
                )
                await self._writer.write_message(
                    make_error(id=original_id, code=INTERNAL_ERROR, message=error_msg)
                )
            except TransportError as retry_exc:
                self._failure_time = (time.monotonic(), FailureReason.MIDSESSION)
                error_msg = f"transport died during {method}; retry after restart also failed: {retry_exc}"
                logger.warning(
                    "[%s] transport recovery failed: %s", self._config.server.name, retry_exc
                )
                await self._writer.write_message(
                    make_error(id=original_id, code=INTERNAL_ERROR, message=error_msg)
                )
            except asyncio.TimeoutError:
                self._failure_time = (time.monotonic(), FailureReason.MIDSESSION)
                error_msg = f"transport died during {method}; restart failed: timed out after {timeout:.1f}s"
                logger.warning(
                    "[%s] transport recovery failed: timed out after %.1fs",
                    self._config.server.name,
                    timeout,
                )
                await self._writer.write_message(
                    make_error(id=original_id, code=INTERNAL_ERROR, message=error_msg)
                )
            except Exception as retry_exc:
                self._failure_time = (time.monotonic(), FailureReason.MIDSESSION)
                logger.warning(
                    "[%s] transport recovery failed: %s", self._config.server.name, retry_exc
                )
                await self._writer.write_message(
                    make_error(id=original_id, code=INTERNAL_ERROR, message=str(retry_exc))
                )

        except Exception as exc:
            try:
                original_id = self._id_mapper.unwrap(proxy_id)
            except KeyError:
                original_id = msg_id
            logger.error(
                "[%s] Failed to forward %s: %s", self._config.server.name, method, exc
            )
            await self._writer.write_message(
                make_error(id=original_id, code=INTERNAL_ERROR, message=str(exc))
            )

    async def _detect_transport_death(self, method: str, exc: Exception) -> None:
        """Transition ACTIVE → FAILED on mid-session transport death (FR-22.1).

        Must be called immediately after catching TransportError from transport.request()
        or transport.notify(). _failure_time is intentionally NOT set here — the cooldown
        write point is the retry-failure branch, so the retry's ensure_active() does not
        trip the cooldown gate before _do_start() runs.
        """
        async with self._sm.lock:
            if self._sm.state == BackendState.ACTIVE:
                await self._sm.transition(BackendState.FAILED)
            # Close transport unconditionally — regardless of who made the ACTIVE→FAILED
            # transition, we need to clear the stale reference. All of this must be
            # under one lock acquisition to close the TOCTOU window.
            if self._transport is not None:
                try:
                    await self._transport.close()
                except Exception as close_exc:
                    logger.debug(
                        "[%s] Secondary exception while closing dead transport: %s",
                        self._config.server.name,
                        close_exc,
                    )
                self._transport = None
        logger.warning(
            "[%s] transport died during %s: %s",
            self._config.server.name,
            method,
            exc,
        )

    _BACKEND_METHODS = frozenset({
        "tools/call", "resources/read", "resources/subscribe",
        "resources/unsubscribe", "prompts/get", "sampling/createMessage",
        "roots/list", "completion/complete", "logging/setLevel",
    })

    def _could_be_backend_method(self, method: str) -> bool:
        """Return True for MCP methods that must be forwarded to the backend."""
        return method in self._BACKEND_METHODS

    async def ensure_active(self) -> None:
        """Ensure backend is in Active state. Start if needed.

        If already Starting/Healthy — wait. If Failed — check cooldown then restart.
        Raises LifecycleError if start fails.
        """
        while True:
            state = self._sm.state

            if state == BackendState.ACTIVE:
                return

            if state in (BackendState.STARTING, BackendState.HEALTHY):
                # Wait for Active or Failed
                result = await self._sm.wait_for(
                    BackendState.ACTIVE, BackendState.FAILED
                )
                if result == BackendState.FAILED:
                    raise LifecycleError("Backend failed to start")
                return

            if state == BackendState.STOPPING:
                # Wait for Cold, then loop to restart
                await self._sm.wait_for(BackendState.COLD)
                continue

            if state in (BackendState.FAILED, BackendState.COLD):
                async with self._sm.lock:
                    # Re-read state under lock — another coroutine may have changed it
                    locked_state = self._sm.state

                    if locked_state == BackendState.ACTIVE:
                        return

                    if locked_state in (BackendState.STARTING, BackendState.HEALTHY):
                        # Another coroutine is starting — loop back to wait outside lock
                        continue

                    if locked_state == BackendState.FAILED:
                        # Check cooldown before reset — reason determines the window (FR-22.5)
                        if self._failure_time is not None:
                            elapsed = time.monotonic() - self._failure_time[0]
                            reason = self._failure_time[1]
                            cooldown = (
                                FAILURE_COOLDOWN_MIDSESSION
                                if reason == FailureReason.MIDSESSION
                                else FAILURE_COOLDOWN_START
                            )
                            if elapsed < cooldown:
                                raise LifecycleError(
                                    f"Backend failed recently ({elapsed:.1f}s ago, "
                                    f"cooldown={cooldown}s)"
                                )
                        # FAILED → COLD → STARTING all under the same lock
                        await self._sm.transition(BackendState.COLD)

                    # Now must be COLD (either was already, or just transitioned)
                    if self._sm.state == BackendState.COLD:
                        await self._do_start()
                        return

                # Lock released — re-check state
                continue

    async def _do_start(self) -> None:
        """Start lifecycle and establish transport connection.

        Must be called with state_machine.lock held.
        Transitions: COLD -> STARTING -> HEALTHY -> ACTIVE.
        """
        try:
            await self._lifecycle.start()
        except Exception:
            self._failure_time = (time.monotonic(), FailureReason.START)
            raise

        # Connect transport
        transport = self._transport_factory()
        try:
            await transport.connect()
        except Exception as exc:
            self._failure_time = (time.monotonic(), FailureReason.START)
            await self._sm.transition(BackendState.FAILED)
            raise LifecycleError(f"Failed to connect transport: {exc}") from exc

        self._transport = transport

        # Perform MCP initialize handshake
        internal_id = self._id_mapper.next_internal_id()
        backend_capabilities: dict[str, Any] = {}
        try:
            init_result = await transport.request(
                "initialize",
                params={
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": "mcp-standby-proxy",
                        "version": "0.1.0",
                    },
                },
                id=internal_id,
            )
            backend_capabilities = init_result.get("result", {}).get("capabilities", {})
            await transport.notify("notifications/initialized")
        except Exception as exc:
            self._failure_time = (time.monotonic(), FailureReason.START)
            await transport.close()
            self._transport = None
            await self._sm.transition(BackendState.FAILED)
            raise LifecycleError(f"Failed MCP handshake: {exc}") from exc

        # Cold bootstrap: fetch capabilities if no cache
        cache_data = self._cache.load()
        if cache_data is None:
            await self._bootstrap_cache(transport, backend_capabilities)
        elif not cache_data.get("capabilities"):
            # Update capabilities in existing cache without re-fetching method lists
            cache_data["capabilities"] = _resolve_capabilities(backend_capabilities, cache_data)
            asyncio.create_task(self._cache.save(cache_data))

        await self._sm.transition(BackendState.ACTIVE)
        logger.info("[%s] Backend is active", self._config.server.name)

    async def _bootstrap_cache(
        self, transport: BackendTransport, capabilities: dict[str, Any] | None = None
    ) -> None:
        """Fetch tools/list (and optionally resources/list, prompts/list) from backend."""
        cache = CacheData(cache_version=1, capabilities=capabilities or {})

        for method in ["tools/list", "resources/list", "prompts/list"]:
            try:
                internal_id = self._id_mapper.next_internal_id()
                result = await transport.request(method, id=internal_id)
                if "result" in result:
                    cache[method] = result["result"]
                    logger.debug(
                        "[%s] Bootstrapped cache: %s", self._config.server.name, method
                    )
            except Exception as exc:
                logger.debug(
                    "[%s] Could not fetch %s during bootstrap: %s",
                    self._config.server.name,
                    method,
                    exc,
                )

        if not cache.get("capabilities"):
            cache["capabilities"] = _resolve_capabilities(capabilities or {}, cache)

        asyncio.create_task(self._cache.save(cache))
        logger.info("[%s] Cache bootstrapped", self._config.server.name)

    async def drain_queue(self) -> None:
        """Forward all queued requests to the backend and write responses."""
        while not self._queue.empty():
            message = await self._queue.get()
            try:
                await self.handle_message(message)
            except Exception as exc:
                msg_id = message.get("id")
                if msg_id is not None:
                    await self._writer.write_message(
                        make_error(id=msg_id, code=INTERNAL_ERROR, message=str(exc))
                    )
            finally:
                self._queue.task_done()

    async def close(self) -> None:
        """Close the transport if connected."""
        if self._transport is not None:
            try:
                await self._transport.close()
            except Exception:
                pass
            self._transport = None
