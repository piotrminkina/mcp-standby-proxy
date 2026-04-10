from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock


from mcp_standby_proxy.cache import CacheData, CacheManager
from mcp_standby_proxy.config import (
    BackendConfig,
    BackendTransport as BackendTransportEnum,
    CacheConfig,
    HealthcheckConfig,
    HealthcheckType,
    LifecycleCommandConfig,
    LifecycleConfig,
    ProxyConfig,
    ServerConfig,
)
from mcp_standby_proxy.errors import StartError
from mcp_standby_proxy.jsonrpc import (
    METHOD_NOT_FOUND,
    JsonRpcWriter,
)
from mcp_standby_proxy.lifecycle import LifecycleManager
from mcp_standby_proxy.router import MessageRouter
from mcp_standby_proxy.state import BackendState, StateMachine


def _make_config(tmp_path: Path) -> ProxyConfig:
    return ProxyConfig(
        version=1,
        server=ServerConfig(name="test-server", version="1.0.0"),
        backend=BackendConfig(
            transport=BackendTransportEnum.SSE,
            url="http://localhost/sse",
        ),
        lifecycle=LifecycleConfig(
            start=LifecycleCommandConfig(command="true", timeout=5),
            stop=LifecycleCommandConfig(command="true", timeout=5),
            healthcheck=HealthcheckConfig(
                type=HealthcheckType.COMMAND,
                command="true",
                interval=1,
                max_attempts=1,
                timeout=1,
            ),
        ),
        cache=CacheConfig(path=str(tmp_path / "cache.json")),
    )


def _make_writer() -> tuple[JsonRpcWriter, list[dict]]:
    """Return (writer, messages_list). All written messages are collected."""
    messages: list[dict] = []

    buf = BytesIO()

    class _CollectingWriter(JsonRpcWriter):
        async def write_message(self, message: dict) -> None:
            messages.append(message)

    return _CollectingWriter(buf), messages


class _MockTransport:
    """Simple in-memory transport for testing."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self._responses = responses or {}
        self._connected = False
        self.requests_received: list[dict] = []
        self.notifications_sent: list[str] = []

    async def connect(self) -> None:
        self._connected = True

    async def request(self, method: str, params: Any = None, id: Any = None) -> dict:
        self.requests_received.append({"method": method, "params": params, "id": id})
        if method in self._responses:
            return {"result": self._responses[method], "id": id, "jsonrpc": "2.0"}
        return {"result": {}, "id": id, "jsonrpc": "2.0"}

    async def notify(self, method: str, params: Any = None) -> None:
        self.notifications_sent.append(method)

    async def close(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected


async def _make_router(
    tmp_path: Path,
    transport: _MockTransport | None = None,
    cache_data: CacheData | None = None,
    start_side_effect=None,
) -> tuple[MessageRouter, list[dict], _MockTransport, StateMachine]:
    config = _make_config(tmp_path)
    sm = StateMachine()
    writer, messages = _make_writer()
    mock_transport = transport or _MockTransport(
        responses={
            "tools/list": {"tools": [{"name": "test-tool"}]},
            "resources/list": {"resources": []},
            "prompts/list": {"prompts": []},
        }
    )

    # Pre-seed cache if provided
    cache_manager = CacheManager(Path(config.cache.path))
    if cache_data is not None:
        await cache_manager.save(cache_data)

    mock_lifecycle = MagicMock(spec=LifecycleManager)
    if start_side_effect is not None:
        mock_lifecycle.start = AsyncMock(side_effect=start_side_effect)
    else:
        # Successful start: transitions state to HEALTHY
        async def _do_start():
            await sm.transition(BackendState.STARTING)
            await sm.transition(BackendState.HEALTHY)
        mock_lifecycle.start = AsyncMock(side_effect=_do_start)
    mock_lifecycle.stop = AsyncMock()

    router = MessageRouter(
        config=config,
        state_machine=sm,
        lifecycle_manager=mock_lifecycle,
        cache_manager=cache_manager,
        transport_factory=lambda: mock_transport,
        writer=writer,
    )
    return router, messages, mock_transport, sm


# ---- initialize ----

async def test_initialize_returns_server_info_from_cache(tmp_path) -> None:
    cache_data = CacheData(
        cache_version=1,
        capabilities={"tools": {"listChanged": True}},
    )
    router, messages, _, _ = await _make_router(tmp_path, cache_data=cache_data)
    await router.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})

    assert len(messages) == 1
    result = messages[0]["result"]
    assert result["serverInfo"]["name"] == "test-server"
    assert result["capabilities"] == {"tools": {"listChanged": True}}


async def test_initialize_without_cache_returns_minimal_capabilities(tmp_path) -> None:
    router, messages, _, _ = await _make_router(tmp_path)
    await router.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})

    result = messages[0]["result"]
    assert result["capabilities"] == {}


# ---- ping ----

async def test_ping_returns_empty_result(tmp_path) -> None:
    router, messages, _, _ = await _make_router(tmp_path)
    await router.handle_message({"jsonrpc": "2.0", "id": 5, "method": "ping"})

    assert messages[0] == {"jsonrpc": "2.0", "id": 5, "result": {}}


# ---- tools/list ----

async def test_tools_list_with_cache_returns_cached(tmp_path) -> None:
    cache_data = CacheData(
        cache_version=1,
        capabilities={},
        **{"tools/list": {"tools": [{"name": "cached-tool"}]}},
    )
    router, messages, transport, sm = await _make_router(tmp_path, cache_data=cache_data)
    await router.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    assert messages[0]["result"] == {"tools": [{"name": "cached-tool"}]}
    # Backend should NOT have been started
    assert sm.state == BackendState.COLD


async def test_tools_list_without_cache_starts_backend(tmp_path) -> None:
    router, messages, transport, sm = await _make_router(tmp_path)
    await router.handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})

    # Backend should have been started
    assert sm.state == BackendState.ACTIVE
    # Response should contain tools
    assert "result" in messages[0]


# ---- tools/call ----

async def test_tools_call_starts_backend_and_forwards(tmp_path) -> None:
    router, messages, transport, sm = await _make_router(tmp_path)
    await router.handle_message({
        "jsonrpc": "2.0",
        "id": 10,
        "method": "tools/call",
        "params": {"name": "test-tool", "arguments": {}},
    })

    assert sm.state == BackendState.ACTIVE
    assert any(r["method"] == "tools/call" for r in transport.requests_received)
    assert len(messages) >= 1
    assert "result" in messages[-1] or "error" in messages[-1]


async def test_tools_call_when_already_active_does_not_restart(tmp_path) -> None:
    router, messages, transport, sm = await _make_router(tmp_path)
    # First call starts the backend
    await router.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "t", "arguments": {}}})
    assert sm.state == BackendState.ACTIVE
    initial_start_count = router._lifecycle.start.call_count

    # Second call should NOT restart
    await router.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                  "params": {"name": "t", "arguments": {}}})
    assert router._lifecycle.start.call_count == initial_start_count


# ---- ID remapping ----

async def test_id_remapping_preserves_client_id(tmp_path) -> None:
    router, messages, transport, sm = await _make_router(tmp_path)
    await router.handle_message({
        "jsonrpc": "2.0",
        "id": "client-123",
        "method": "tools/call",
        "params": {"name": "t", "arguments": {}},
    })

    # The response back to client should use the original ID
    final_msg = messages[-1]
    assert final_msg.get("id") == "client-123"


# ---- Backend failure ----

async def test_tools_call_when_start_fails_returns_error(tmp_path) -> None:
    config = _make_config(tmp_path)
    sm = StateMachine()
    writer, messages = _make_writer()
    mock_transport = _MockTransport()
    cache_manager = CacheManager(Path(config.cache.path))
    mock_lifecycle = MagicMock(spec=LifecycleManager)

    async def _failing_start():
        # LifecycleManager.start() transitions state internally;
        # we simulate it setting FAILED and raising
        sm._state = BackendState.FAILED
        raise StartError(exit_code=1, stderr="command failed")

    mock_lifecycle.start = AsyncMock(side_effect=_failing_start)
    mock_lifecycle.stop = AsyncMock()

    router = MessageRouter(
        config=config,
        state_machine=sm,
        lifecycle_manager=mock_lifecycle,
        cache_manager=cache_manager,
        transport_factory=lambda: mock_transport,
        writer=writer,
    )

    # Feed tools/call — ensure_active should catch the failure and write error response
    await router.handle_message({
        "jsonrpc": "2.0",
        "id": 99,
        "method": "tools/call",
        "params": {"name": "t", "arguments": {}},
    })

    assert any("error" in m for m in messages)


# ---- Unknown method ----

async def test_unknown_method_when_cold_returns_method_not_found(tmp_path) -> None:
    router, messages, _, sm = await _make_router(tmp_path)
    assert sm.state == BackendState.COLD

    await router.handle_message({
        "jsonrpc": "2.0",
        "id": 7,
        "method": "some/unknown/method",
    })

    assert messages[0]["error"]["code"] == METHOD_NOT_FOUND


async def test_unknown_method_when_active_forwarded(tmp_path) -> None:
    router, messages, transport, sm = await _make_router(tmp_path)
    # First activate backend
    await router.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "t", "arguments": {}}})
    assert sm.state == BackendState.ACTIVE

    messages.clear()
    transport.requests_received.clear()

    await router.handle_message({
        "jsonrpc": "2.0",
        "id": 99,
        "method": "custom/method",
        "params": {"x": 1},
    })

    assert any(r["method"] == "custom/method" for r in transport.requests_received)


# ---- Notifications ----

async def test_notification_when_active_forwarded(tmp_path) -> None:
    router, messages, transport, sm = await _make_router(tmp_path)
    await router.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "t", "arguments": {}}})
    assert sm.state == BackendState.ACTIVE

    await router.handle_message({
        "jsonrpc": "2.0",
        "method": "notifications/something",
    })

    assert "notifications/something" in transport.notifications_sent


async def test_notification_when_cold_dropped(tmp_path) -> None:
    router, messages, transport, sm = await _make_router(tmp_path)
    assert sm.state == BackendState.COLD

    await router.handle_message({
        "jsonrpc": "2.0",
        "method": "notifications/something",
    })

    assert "notifications/something" not in transport.notifications_sent
    assert len(messages) == 0  # No response for notifications
