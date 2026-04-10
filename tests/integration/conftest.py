from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from mcp_standby_proxy.cache import CacheData, CacheManager
from mcp_standby_proxy.config import load_config, ProxyConfig
from mcp_standby_proxy.jsonrpc import JsonRpcWriter
from mcp_standby_proxy.lifecycle import LifecycleManager
from mcp_standby_proxy.router import MessageRouter
from mcp_standby_proxy.state import BackendState, StateMachine


class CollectingWriter(JsonRpcWriter):
    """Writer that collects all written messages for test assertions."""

    def __init__(self):
        super().__init__(BytesIO())
        self.messages: list[dict] = []

    async def write_message(self, message: dict) -> None:
        self.messages.append(message)


class MockTransport:
    """In-memory transport that simulates a real MCP backend."""

    def __init__(self, tool_responses: dict | None = None) -> None:
        self._connected = False
        self.requests: list[dict] = []
        self.notifications: list[str] = []
        self._tool_responses = tool_responses or {}

    async def connect(self) -> None:
        self._connected = True

    async def request(self, method: str, params=None, id=None) -> dict:
        self.requests.append({"method": method, "params": params, "id": id})

        if method == "initialize":
            return {
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mock-backend", "version": "0.1.0"},
                },
                "id": id,
                "jsonrpc": "2.0",
            }
        elif method == "tools/list":
            return {
                "result": {"tools": [{"name": "mock-tool", "description": "A mock tool"}]},
                "id": id,
                "jsonrpc": "2.0",
            }
        elif method == "resources/list":
            return {"result": {"resources": []}, "id": id, "jsonrpc": "2.0"}
        elif method == "prompts/list":
            return {"result": {"prompts": []}, "id": id, "jsonrpc": "2.0"}
        elif method == "tools/call":
            tool_name = (params or {}).get("name", "unknown")
            resp = self._tool_responses.get(tool_name, {"content": [{"type": "text", "text": "ok"}]})
            return {"result": resp, "id": id, "jsonrpc": "2.0"}
        else:
            return {"result": {}, "id": id, "jsonrpc": "2.0"}

    async def notify(self, method: str, params=None) -> None:
        self.notifications.append(method)

    async def close(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected


def make_config(tmp_path: Path) -> ProxyConfig:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({
        "version": 1,
        "server": {"name": "integration-test", "version": "1.0.0"},
        "backend": {"transport": "sse", "url": "http://localhost/sse"},
        "lifecycle": {
            "start": {"command": "true"},
            "stop": {"command": "true"},
            "healthcheck": {"type": "command", "command": "true"},
        },
        "cache": {"path": str(tmp_path / "cache.json")},
    }))
    return load_config(config_file)


def make_router(
    tmp_path: Path,
    transport: MockTransport | None = None,
    cache_data: CacheData | None = None,
    start_fails: bool = False,
) -> tuple[MessageRouter, CollectingWriter, MockTransport, StateMachine]:
    config = make_config(tmp_path)
    sm = StateMachine()
    writer = CollectingWriter()
    mock_transport = transport or MockTransport()
    cache_manager = CacheManager(Path(config.cache.path))

    mock_lifecycle = MagicMock(spec=LifecycleManager)

    if start_fails:
        from mcp_standby_proxy.errors import StartError

        async def _failing_start():
            sm._state = BackendState.FAILED
            raise StartError(exit_code=1, stderr="start failed")

        mock_lifecycle.start = AsyncMock(side_effect=_failing_start)
    else:
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
    return router, writer, mock_transport, sm


@pytest.fixture
def sample_config(tmp_path) -> ProxyConfig:
    return make_config(tmp_path)


@pytest.fixture
def mock_transport() -> MockTransport:
    return MockTransport()
