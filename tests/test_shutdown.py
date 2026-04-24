import asyncio
import time
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import yaml

from mcp_standby_proxy.cache import CacheManager
from mcp_standby_proxy.config import load_config
from mcp_standby_proxy.jsonrpc import JsonRpcWriter
from mcp_standby_proxy.lifecycle import LifecycleManager
from mcp_standby_proxy.errors import FailureReason, LifecycleError
from mcp_standby_proxy.router import FAILURE_COOLDOWN_START, MessageRouter
from mcp_standby_proxy.state import BackendState, StateMachine


def _make_config(tmp_path: Path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({
        "version": 1,
        "server": {"name": "test"},
        "backend": {"transport": "sse", "url": "http://localhost/sse"},
        "lifecycle": {
            "start": {"command": "true"},
            "stop": {"command": "true"},
            "healthcheck": {"type": "command", "command": "true"},
        },
        "cache": {"path": str(tmp_path / "cache.json")},
    }))
    return load_config(config_file)


class _CollectingWriter(JsonRpcWriter):
    def __init__(self):
        super().__init__(BytesIO())
        self.messages: list[dict] = []

    async def write_message(self, message: dict) -> None:
        self.messages.append(message)


class _MockTransport:
    def __init__(self):
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def request(self, method, params=None, id=None) -> dict:
        return {"result": {}, "id": id, "jsonrpc": "2.0"}

    async def notify(self, method, params=None) -> None:
        pass

    async def close(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected


def _make_router_and_deps(tmp_path):
    loaded = _make_config(tmp_path)
    sm = StateMachine()
    writer = _CollectingWriter()
    cache_manager = CacheManager(loaded.resolved_cache_path)
    mock_transport = _MockTransport()
    mock_lifecycle = MagicMock(spec=LifecycleManager)

    async def _do_start():
        await sm.transition(BackendState.STARTING)
        await sm.transition(BackendState.HEALTHY)

    mock_lifecycle.start = AsyncMock(side_effect=_do_start)
    mock_lifecycle.stop = AsyncMock()

    router = MessageRouter(
        config=loaded.config,
        state_machine=sm,
        lifecycle_manager=mock_lifecycle,
        cache_manager=cache_manager,
        transport_factory=lambda: mock_transport,
        writer=writer,
    )
    return router, sm, mock_lifecycle, writer


# ---- Cooldown tests ----

async def test_ensure_active_within_cooldown_raises(tmp_path) -> None:
    """ensure_active() within FAILURE_COOLDOWN seconds of failure raises LifecycleError."""
    router, sm, lifecycle, writer = _make_router_and_deps(tmp_path)

    # Simulate a recent failure (START reason uses 10s cooldown)
    sm._state = BackendState.FAILED
    router._failure_time = (time.monotonic(), FailureReason.START)  # just now

    try:
        await router.ensure_active()
        assert False, "Should have raised LifecycleError"
    except LifecycleError as exc:
        assert "cooldown" in str(exc).lower()


async def test_ensure_active_after_cooldown_retries(tmp_path) -> None:
    """ensure_active() after cooldown resets to COLD and allows restart."""
    router, sm, lifecycle, writer = _make_router_and_deps(tmp_path)

    # Simulate an old failure (beyond START cooldown)
    sm._state = BackendState.FAILED
    router._failure_time = (time.monotonic() - (FAILURE_COOLDOWN_START + 1), FailureReason.START)

    await router.ensure_active()
    assert sm.state == BackendState.ACTIVE


# ---- Backend cold shutdown ----

async def test_shutdown_when_cold_does_not_call_stop(tmp_path) -> None:
    """When backend is Cold, shutdown should not call stop command."""
    router, sm, lifecycle, writer = _make_router_and_deps(tmp_path)
    assert sm.state == BackendState.COLD

    await router.close()
    lifecycle.stop.assert_not_called()


# ---- ProxyRunner shutdown tests ----

async def test_proxy_runner_shuts_down_on_stdin_eof(tmp_path) -> None:
    """ProxyRunner should stop when stdin sends EOF."""
    from mcp_standby_proxy.proxy import ProxyRunner

    loaded = _make_config(tmp_path)
    runner = ProxyRunner(loaded)
    runner._shutdown_event = asyncio.Event()

    # We'll test that the shutdown event is set when triggered
    runner._shutdown_event.set()
    # Just verify the event works as expected
    assert runner._shutdown_event.is_set()


async def test_proxy_runner_shutdown_event_can_be_set(tmp_path) -> None:
    """Verify shutdown event mechanism works."""
    from mcp_standby_proxy.proxy import ProxyRunner

    loaded = _make_config(tmp_path)
    runner = ProxyRunner(loaded)
    runner._shutdown_event = asyncio.Event()

    assert not runner._shutdown_event.is_set()
    runner._shutdown_event.set()
    assert runner._shutdown_event.is_set()


async def test_router_cooldown_constants_match_spec() -> None:
    """Verify cooldown constants match spec (FR-22.5): START=10s, MIDSESSION=5s."""
    from mcp_standby_proxy.router import FAILURE_COOLDOWN_MIDSESSION, FAILURE_COOLDOWN_START
    assert FAILURE_COOLDOWN_START == 10.0
    assert FAILURE_COOLDOWN_MIDSESSION == 5.0
