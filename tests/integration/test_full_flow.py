"""Integration tests that wire all real components together.

Uses a MockTransport instead of a real SSE server, but all other components
(StateMachine, CacheManager, LifecycleManager mock, MessageRouter) are real.
"""
import asyncio
from pathlib import Path


from mcp_standby_proxy.cache import CacheData, CacheManager
from mcp_standby_proxy.state import BackendState

from tests.integration.conftest import make_router


# ---- US-001: Cache hit flow ----

async def test_cache_hit_tools_list_no_backend_start(tmp_path) -> None:
    """US-001: tools/list served from cache without starting backend."""
    cache_file = tmp_path / "cache.json"
    cache_data = CacheData(
        cache_version=1,
        capabilities={"tools": {}},
        **{"tools/list": {"tools": [{"name": "cached-tool"}]}},
    )
    cache_manager = CacheManager(cache_file)
    await cache_manager.save(cache_data)

    router, writer, transport, sm = make_router(tmp_path, cache_data=cache_data)

    # Initialize
    await router.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    await router.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})

    # tools/list from cache
    await router.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    assert sm.state == BackendState.COLD  # backend not started
    tools_response = next(m for m in writer.messages if m.get("id") == 2)
    assert tools_response["result"]["tools"][0]["name"] == "cached-tool"


# ---- US-002: Full cold-start flow ----

async def test_full_cold_start_flow(tmp_path) -> None:
    """US-002: Client requests trigger cold start, backend activates, request forwarded."""
    router, writer, transport, sm = make_router(tmp_path)

    await router.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    await router.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})

    await router.handle_message({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "mock-tool", "arguments": {"x": 1}},
    })

    assert sm.state == BackendState.ACTIVE
    tools_call_response = next(m for m in writer.messages if m.get("id") == 2)
    assert "result" in tools_call_response
    assert any(r["method"] == "tools/call" for r in transport.requests)


# ---- US-003: Cold cache bootstrap ----

async def test_cold_cache_bootstrap(tmp_path) -> None:
    """US-003: No cache file; tools/list triggers backend start, tools fetched and cached."""
    router, writer, transport, sm = make_router(tmp_path)
    cache_file = Path(router._config.cache.path)
    assert not cache_file.exists()

    await router.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert sm.state == BackendState.ACTIVE
    # Cache file should be written
    await asyncio.sleep(0.05)  # allow async cache save task to complete
    assert cache_file.exists()

    cached = CacheManager(cache_file).load()
    assert cached is not None


# ---- US-004: Concurrent tools/call during startup ----

async def test_concurrent_tools_call_during_startup(tmp_path) -> None:
    """US-004: Multiple tools/call during startup — backend starts once, all get responses."""
    router, writer, transport, sm = make_router(tmp_path)

    # Fire 3 concurrent tools/call
    tasks = [
        asyncio.create_task(router.handle_message({
            "jsonrpc": "2.0",
            "id": i,
            "method": "tools/call",
            "params": {"name": "mock-tool", "arguments": {}},
        }))
        for i in range(1, 4)
    ]
    await asyncio.gather(*tasks)

    # Backend started exactly once
    assert router._lifecycle.start.call_count == 1
    assert sm.state == BackendState.ACTIVE

    # All 3 requests got responses
    response_ids = {m["id"] for m in writer.messages if "id" in m and ("result" in m or "error" in m)}
    assert {1, 2, 3}.issubset(response_ids)


# ---- US-005: Backend failure ----

async def test_backend_failure_returns_error_response(tmp_path) -> None:
    """US-005: Start command fails → tools/call receives JSON-RPC error."""
    router, writer, transport, sm = make_router(tmp_path, start_fails=True)

    await router.handle_message({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "t", "arguments": {}},
    })

    assert any("error" in m and m.get("id") == 1 for m in writer.messages)


# ---- US-006: Graceful shutdown ----

async def test_graceful_shutdown_after_active(tmp_path) -> None:
    """US-006: After backend is active, router.close() closes transport."""
    router, writer, transport, sm = make_router(tmp_path)

    # Activate backend
    await router.handle_message({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "mock-tool", "arguments": {}},
    })
    assert sm.state == BackendState.ACTIVE
    assert transport.is_connected()

    await router.close()
    assert not transport.is_connected()


# ---- Ping and initialize ----

async def test_initialize_and_ping(tmp_path) -> None:
    """initialize returns server info; ping returns empty result."""
    router, writer, transport, sm = make_router(tmp_path)

    await router.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    await router.handle_message({"jsonrpc": "2.0", "id": 2, "method": "ping"})

    init_resp = next(m for m in writer.messages if m.get("id") == 1)
    assert init_resp["result"]["serverInfo"]["name"] == "integration-test"

    ping_resp = next(m for m in writer.messages if m.get("id") == 2)
    assert ping_resp == {"jsonrpc": "2.0", "id": 2, "result": {}}


# ---- ID mapping preserved ----

async def test_id_mapping_preserved_through_roundtrip(tmp_path) -> None:
    """Client ID is preserved through the proxy round-trip."""
    router, writer, transport, sm = make_router(tmp_path)

    await router.handle_message({
        "jsonrpc": "2.0",
        "id": "req-abc-123",
        "method": "tools/call",
        "params": {"name": "mock-tool", "arguments": {}},
    })

    resp = next(m for m in writer.messages if m.get("id") == "req-abc-123")
    assert "result" in resp or "error" in resp
