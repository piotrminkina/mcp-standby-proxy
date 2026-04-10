import asyncio
import logging
import time
from typing import Callable

from mcp_standby_proxy.cache import CacheData, CacheManager
from mcp_standby_proxy.config import ProxyConfig
from mcp_standby_proxy.errors import LifecycleError, TransportError
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

# Cooldown in seconds before retrying a failed backend
FAILURE_COOLDOWN = 10.0

# Methods that are served from cache or locally without backend
_CACHED_METHODS = frozenset(["tools/list", "resources/list", "prompts/list"])

# MCP protocol version
MCP_PROTOCOL_VERSION = "2024-11-05"


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
        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        self._failure_time: float | None = None
        self._initialized = False

    async def handle_message(self, message: dict) -> None:
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

    async def _handle_initialize(self, message: dict) -> None:
        """Respond to MCP initialize with proxy server info and capabilities."""
        msg_id = message.get("id")
        cache_data = self._cache.load()
        capabilities = cache_data.get("capabilities", {}) if cache_data else {}

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

    async def _handle_cacheable(self, message: dict) -> None:
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

            # Save to cache asynchronously
            new_cache = CacheData(
                cache_version=1,
                capabilities=cache_data.get("capabilities", {}) if cache_data else {},
            )
            if cache_data:
                new_cache.update(cache_data)
            new_cache[method] = actual_result
            asyncio.create_task(self._cache.save(new_cache))

        except (TransportError, Exception) as exc:
            logger.error("[%s] Failed to fetch %s: %s", self._config.server.name, method, exc)
            await self._writer.write_message(
                make_error(id=msg_id, code=INTERNAL_ERROR, message=str(exc))
            )

    async def _handle_forwarded_request(self, message: dict) -> None:
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
        except (TransportError, Exception) as exc:
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
                        # Check cooldown before reset
                        if self._failure_time is not None:
                            elapsed = time.monotonic() - self._failure_time
                            if elapsed < FAILURE_COOLDOWN:
                                raise LifecycleError(
                                    f"Backend failed recently ({elapsed:.1f}s ago, "
                                    f"cooldown={FAILURE_COOLDOWN}s)"
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
            self._failure_time = time.monotonic()
            raise

        # Connect transport
        transport = self._transport_factory()
        try:
            await transport.connect()
        except Exception as exc:
            self._failure_time = time.monotonic()
            await self._sm.transition(BackendState.FAILED)
            raise LifecycleError(f"Failed to connect transport: {exc}") from exc

        self._transport = transport

        # Perform MCP initialize handshake
        internal_id = self._id_mapper.next_internal_id()
        backend_capabilities: dict = {}
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
            self._failure_time = time.monotonic()
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
            cache_data["capabilities"] = backend_capabilities
            asyncio.create_task(self._cache.save(cache_data))

        await self._sm.transition(BackendState.ACTIVE)
        logger.info("[%s] Backend is active", self._config.server.name)

    async def _bootstrap_cache(
        self, transport: BackendTransport, capabilities: dict | None = None
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
