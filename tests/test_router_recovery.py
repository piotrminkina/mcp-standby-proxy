"""Unit tests for FR-22: mid-session backend recovery.

Tests 1-15 from tech-spec §5.5 Test Plan.
"""

import asyncio
import logging
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
from mcp_standby_proxy.errors import FailureReason, LifecycleError, TransportError
from mcp_standby_proxy.jsonrpc import (
    INTERNAL_ERROR,
    JsonRpcWriter,
)
from mcp_standby_proxy.lifecycle import LifecycleManager
from mcp_standby_proxy.router import (
    FAILURE_COOLDOWN_MIDSESSION,
    FAILURE_COOLDOWN_START,
    MessageRouter,
)
from mcp_standby_proxy.state import BackendState, StateMachine


# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


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


class _CollectingWriter(JsonRpcWriter):
    def __init__(self) -> None:
        super().__init__(BytesIO())
        self.messages: list[dict[str, Any]] = []

    async def write_message(self, message: dict[str, Any]) -> None:
        self.messages.append(message)


class _BaseTransport:
    """Minimal transport that handles handshake methods correctly and allows
    subclasses to override behaviour for tools/call (the primary test path)."""

    def __init__(self) -> None:
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def request(self, method: str, params: Any = None, id: Any = None) -> dict[str, Any]:
        if method == "initialize":
            return {"result": {"capabilities": {}}, "id": id, "jsonrpc": "2.0"}
        if method in ("tools/list", "resources/list", "prompts/list"):
            return {"result": {method.split("/")[0]: []}, "id": id, "jsonrpc": "2.0"}
        return {"result": {}, "id": id, "jsonrpc": "2.0"}

    async def notify(self, method: str, params: Any = None) -> None:
        pass

    async def close(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected


def _make_writer() -> _CollectingWriter:
    return _CollectingWriter()


async def _make_router(
    tmp_path: Path,
    transport_factory: Any | None = None,
    cache_data: CacheData | None = None,
    start_side_effect: Any = None,
) -> tuple[MessageRouter, _CollectingWriter, StateMachine]:
    config = _make_config(tmp_path)
    sm = StateMachine()
    writer = _make_writer()

    cache_manager = CacheManager(Path(config.cache.path))
    if cache_data is not None:
        await cache_manager.save(cache_data)

    mock_lifecycle = MagicMock(spec=LifecycleManager)
    if start_side_effect is not None:
        mock_lifecycle.start = AsyncMock(side_effect=start_side_effect)
    else:
        async def _do_start() -> None:
            await sm.transition(BackendState.STARTING)
            await sm.transition(BackendState.HEALTHY)

        mock_lifecycle.start = AsyncMock(side_effect=_do_start)
    mock_lifecycle.stop = AsyncMock()

    if transport_factory is None:
        default_transport = _BaseTransport()
        transport_factory = lambda: default_transport  # noqa: E731

    router = MessageRouter(
        config=config,
        state_machine=sm,
        lifecycle_manager=mock_lifecycle,
        cache_manager=cache_manager,
        transport_factory=transport_factory,
        writer=writer,
    )
    return router, writer, sm


async def _force_active(router: MessageRouter, sm: StateMachine) -> None:
    """Force the router into ACTIVE state with the transport attached.

    Bypasses `handle_message` for the initial activation so that the test
    transport's first `tools/call` is NOT consumed during setup. This ensures
    each test controls exactly how many times its transport is called.

    We call `_do_start()` directly under the lock, which transitions
    COLD → STARTING → HEALTHY → ACTIVE via the lifecycle mock and transport factory.
    """
    async with sm.lock:
        await router._do_start()
    assert sm.state == BackendState.ACTIVE
    router._writer.messages.clear()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Test 1: mid-session TransportError transitions to FAILED, clears transport,
#          does NOT write _failure_time at detection time
# ---------------------------------------------------------------------------


async def test_midsession_transport_error_transitions_to_failed(tmp_path: Path) -> None:
    """FR-22.1: TransportError from transport.request() must → ACTIVE→FAILED,
    clear _transport, and NOT set _failure_time (so retry is not gated)."""

    class _DieOnceTransport(_BaseTransport):
        """Raises TransportError on the FIRST tools/call, succeeds thereafter."""

        def __init__(self) -> None:
            super().__init__()
            self._tools_call_count = 0

        async def request(self, method: str, params: Any = None, id: Any = None) -> dict[str, Any]:
            if method == "tools/call":
                self._tools_call_count += 1
                if self._tools_call_count == 1:
                    raise TransportError("Write stream closed")
                return {"result": {"content": []}, "id": id, "jsonrpc": "2.0"}
            return await super().request(method, params, id)

    transport = _DieOnceTransport()
    router, writer, sm = await _make_router(tmp_path, transport_factory=lambda: transport)
    await _force_active(router, sm)

    # Intercept _detect_transport_death to capture _failure_time AT DETECTION TIME
    captured_failure_time: list[Any] = []
    original_detect = router._detect_transport_death

    async def _patched_detect(
        method: str, exc: Exception, stale_transport: Any
    ) -> None:
        await original_detect(method, exc, stale_transport)
        captured_failure_time.append(router._failure_time)

    router._detect_transport_death = _patched_detect  # type: ignore[method-assign]

    # Make the retry's lifecycle.start fail so the test terminates cleanly.
    # Mimic real LifecycleManager.start(): transition to STARTING then FAILED, then raise.
    async def _failing_start() -> None:
        await sm.transition(BackendState.STARTING)
        await sm.transition(BackendState.FAILED)
        raise LifecycleError("restart intentionally failed for test")

    router._lifecycle.start = AsyncMock(side_effect=_failing_start)  # type: ignore[method-assign]

    await router.handle_message({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "t", "arguments": {}},
    })

    # At the moment of detection (just after ACTIVE→FAILED, before retry), _failure_time must be None
    assert len(captured_failure_time) >= 1
    assert captured_failure_time[0] is None, (
        "_failure_time must NOT be set during detection — only in retry-failure branch. "
        "If set at detection, the retry's ensure_active() would trip the cooldown gate."
    )

    # After detection, transport must be cleared
    assert router._transport is None

    # After retry fails, _failure_time must be set and tagged MIDSESSION
    assert router._failure_time is not None
    assert router._failure_time[1] == FailureReason.MIDSESSION


# ---------------------------------------------------------------------------
# Test 2: retry succeeds — client receives success response with original id
# ---------------------------------------------------------------------------


async def test_retry_succeeds_after_restart(tmp_path: Path) -> None:
    """FR-22.2: When retry's transport.request() succeeds, client sees success
    response with the original client id — no error written."""

    class _DieOnceTransport(_BaseTransport):
        def __init__(self) -> None:
            super().__init__()
            self._tools_call_count = 0

        async def request(self, method: str, params: Any = None, id: Any = None) -> dict[str, Any]:
            if method == "tools/call":
                self._tools_call_count += 1
                if self._tools_call_count == 1:
                    raise TransportError("Write stream closed")
                return {"result": {"content": [{"type": "text", "text": "recovered"}]}, "id": id, "jsonrpc": "2.0"}
            return await super().request(method, params, id)

    transport = _DieOnceTransport()
    router, writer, sm = await _make_router(tmp_path, transport_factory=lambda: transport)
    await _force_active(router, sm)

    await router.handle_message({
        "jsonrpc": "2.0",
        "id": "client-42",
        "method": "tools/call",
        "params": {"name": "t", "arguments": {}},
    })

    # Client must see success with original id
    assert len(writer.messages) == 1
    msg = writer.messages[0]
    assert msg.get("id") == "client-42"
    assert "result" in msg, f"Expected result, got: {msg}"
    assert "error" not in msg


# ---------------------------------------------------------------------------
# Test 3: retry failure from LifecycleError propagates correctly
# ---------------------------------------------------------------------------


async def test_retry_failure_propagates_lifecycle_error(tmp_path: Path) -> None:
    """FR-22.2: LifecycleError from retry's ensure_active → specific error message."""

    class _AlwaysDiesTransport(_BaseTransport):
        """Dies on tools/call unconditionally."""

        async def request(self, method: str, params: Any = None, id: Any = None) -> dict[str, Any]:
            if method == "tools/call":
                raise TransportError("Write stream closed")
            return await super().request(method, params, id)

    transport = _AlwaysDiesTransport()
    router, writer, sm = await _make_router(tmp_path, transport_factory=lambda: transport)
    await _force_active(router, sm)

    # Make restart fail with LifecycleError.
    # Mimic real LifecycleManager.start(): transition to STARTING then FAILED, then raise.
    async def _failing_start() -> None:
        await sm.transition(BackendState.STARTING)
        await sm.transition(BackendState.FAILED)
        raise LifecycleError("docker failed to start")

    router._lifecycle.start = AsyncMock(side_effect=_failing_start)  # type: ignore[method-assign]

    await router.handle_message({
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "t", "arguments": {}},
    })

    assert len(writer.messages) == 1
    err = writer.messages[0]
    assert "error" in err
    assert err["error"]["code"] == INTERNAL_ERROR
    msg = err["error"]["message"]
    assert "transport died during tools/call" in msg
    assert "restart failed" in msg
    assert "docker failed to start" in msg


# ---------------------------------------------------------------------------
# Test 4: retry failure from second TransportError
# ---------------------------------------------------------------------------


async def test_retry_failure_propagates_second_transport_error(tmp_path: Path) -> None:
    """FR-22.2: Second TransportError from retry's transport.request() → specific message."""

    call_count = 0

    class _AlwaysDiesTransport(_BaseTransport):
        async def request(self, method: str, params: Any = None, id: Any = None) -> dict[str, Any]:
            nonlocal call_count
            if method == "tools/call":
                call_count += 1
                raise TransportError(f"Connection reset (call {call_count})")
            return await super().request(method, params, id)

    transport = _AlwaysDiesTransport()
    router, writer, sm = await _make_router(tmp_path, transport_factory=lambda: transport)
    await _force_active(router, sm)

    await router.handle_message({
        "jsonrpc": "2.0",
        "id": 8,
        "method": "tools/call",
        "params": {"name": "t", "arguments": {}},
    })

    assert len(writer.messages) == 1
    err = writer.messages[0]
    assert "error" in err
    msg = err["error"]["message"]
    assert "transport died during tools/call" in msg
    assert "retry after restart also failed" in msg


# ---------------------------------------------------------------------------
# Test 5: timeout during recovery
# ---------------------------------------------------------------------------


async def test_retry_failure_propagates_timeout(tmp_path: Path) -> None:
    """FR-22.3: asyncio.wait_for fires → message contains 'timed out after'."""

    first_tools_call = True

    class _HangingTransport(_BaseTransport):
        async def request(self, method: str, params: Any = None, id: Any = None) -> dict[str, Any]:
            nonlocal first_tools_call
            if method == "tools/call":
                if first_tools_call:
                    first_tools_call = False
                    raise TransportError("Write stream closed")
                # Hang "forever" on retry
                await asyncio.sleep(9999)
                return {"result": {}, "id": id, "jsonrpc": "2.0"}
            return await super().request(method, params, id)

    transport = _HangingTransport()
    router, writer, sm = await _make_router(tmp_path, transport_factory=lambda: transport)
    await _force_active(router, sm)

    # Use very short timeout: min(1, 60) = 1s, but we patch wait_for to use 0.05s
    original_wait_for = asyncio.wait_for

    async def _fast_wait_for(coro: Any, timeout: float) -> Any:
        return await original_wait_for(coro, timeout=0.05)

    with patch("mcp_standby_proxy.router.asyncio.wait_for", side_effect=_fast_wait_for):
        await router.handle_message({
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "t", "arguments": {}},
        })

    assert len(writer.messages) == 1
    err = writer.messages[0]
    assert "error" in err
    msg = err["error"]["message"]
    assert "timed out after" in msg


# ---------------------------------------------------------------------------
# Test 6: no double retry — _do_start called at most once per request
# ---------------------------------------------------------------------------


async def test_does_not_retry_twice_on_same_request(tmp_path: Path) -> None:
    """FR-22.2: A single request handler issues at most one retry."""

    class _AlwaysDiesTransport(_BaseTransport):
        async def request(self, method: str, params: Any = None, id: Any = None) -> dict[str, Any]:
            if method == "tools/call":
                raise TransportError("Write stream closed")
            return await super().request(method, params, id)

    transport = _AlwaysDiesTransport()
    start_call_count = 0

    async def _counting_start() -> None:
        nonlocal start_call_count
        start_call_count += 1
        await sm.transition(BackendState.STARTING)
        await sm.transition(BackendState.HEALTHY)

    router, writer, sm = await _make_router(tmp_path, transport_factory=lambda: transport)
    router._lifecycle.start = AsyncMock(side_effect=_counting_start)  # type: ignore[method-assign]
    await _force_active(router, sm)

    initial_count = start_call_count

    await router.handle_message({
        "jsonrpc": "2.0",
        "id": 10,
        "method": "tools/call",
        "params": {"name": "t", "arguments": {}},
    })

    # At most one additional _do_start call (the retry)
    assert start_call_count - initial_count <= 1
    # Client must see exactly one error response
    assert len(writer.messages) == 1
    assert "error" in writer.messages[0]


# ---------------------------------------------------------------------------
# Test 7: notification triggers ACTIVE→FAILED but no retry, no client error
# ---------------------------------------------------------------------------


async def test_notification_transitions_but_does_not_retry(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """FR-22.4: notify() TransportError → state FAILED, WARNING log, no client response."""

    first_notify = True

    class _NotifyDyingTransport(_BaseTransport):
        async def notify(self, method: str, params: Any = None) -> None:
            nonlocal first_notify
            if first_notify and method not in ("notifications/initialized",):
                first_notify = False
                raise TransportError("Connection reset")

    transport = _NotifyDyingTransport()
    router, writer, sm = await _make_router(tmp_path, transport_factory=lambda: transport)
    await _force_active(router, sm)

    with caplog.at_level(logging.WARNING, logger="mcp_standby_proxy.router"):
        await router.handle_message({
            "jsonrpc": "2.0",
            "method": "notifications/something",
        })

    # No client-facing response (notifications have no id)
    assert len(writer.messages) == 0

    # State transitioned to FAILED
    assert sm.state == BackendState.FAILED

    # WARNING log emitted with canonical format
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "transport died during notifications/something" in r.message
        for r in warning_records
    ), (
        f"Expected 'transport died during notifications/something' in warnings: "
        f"{[r.message for r in warning_records]}"
    )


# ---------------------------------------------------------------------------
# Test 8: cooldown gate does NOT block the first retry
# ---------------------------------------------------------------------------


async def test_cooldown_gate_first_retry_not_blocked(tmp_path: Path) -> None:
    """FR-22.5: _failure_time is None at the moment ensure_active() is called
    for the first retry — no LifecycleError('Backend failed recently') raised."""

    class _DieOnceTransport(_BaseTransport):
        def __init__(self) -> None:
            super().__init__()
            self._tools_call_count = 0

        async def request(self, method: str, params: Any = None, id: Any = None) -> dict[str, Any]:
            if method == "tools/call":
                self._tools_call_count += 1
                if self._tools_call_count == 1:
                    raise TransportError("Write stream closed")
                return {"result": {"content": []}, "id": id, "jsonrpc": "2.0"}
            return await super().request(method, params, id)

    transport = _DieOnceTransport()
    router, writer, sm = await _make_router(tmp_path, transport_factory=lambda: transport)
    await _force_active(router, sm)

    # Capture _failure_time at the moment ensure_active() is entered for the retry
    failure_time_at_retry: list[Any] = []
    original_ensure = router.ensure_active
    call_count = 0

    async def _patched_ensure() -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            # Second invocation = retry's ensure_active — capture before it runs
            failure_time_at_retry.append(router._failure_time)
        await original_ensure()

    router.ensure_active = _patched_ensure  # type: ignore[method-assign]

    await router.handle_message({
        "jsonrpc": "2.0",
        "id": 11,
        "method": "tools/call",
        "params": {"name": "t", "arguments": {}},
    })

    # Must have captured the retry invocation
    assert len(failure_time_at_retry) >= 1
    assert failure_time_at_retry[0] is None, (
        "_failure_time must be None at the start of retry's ensure_active() — "
        "if it's not, the retry trips the cooldown gate and never attempts _do_start()"
    )

    # Recovery succeeded — client sees success
    assert len(writer.messages) == 1
    assert "result" in writer.messages[0]


# ---------------------------------------------------------------------------
# Test 9: cooldown gates subsequent client requests after failed retry
# ---------------------------------------------------------------------------


async def test_cooldown_gates_subsequent_client_request(tmp_path: Path) -> None:
    """FR-22.5: After retry fails (sets _failure_time MIDSESSION), subsequent
    client request within 5s receives LifecycleError quoting the cooldown."""

    class _AlwaysDiesTransport(_BaseTransport):
        async def request(self, method: str, params: Any = None, id: Any = None) -> dict[str, Any]:
            if method == "tools/call":
                raise TransportError("Write stream closed")
            return await super().request(method, params, id)

    transport = _AlwaysDiesTransport()
    router, writer, sm = await _make_router(tmp_path, transport_factory=lambda: transport)
    await _force_active(router, sm)

    # First request — triggers transport death + failed retry
    await router.handle_message({
        "jsonrpc": "2.0",
        "id": 12,
        "method": "tools/call",
        "params": {"name": "t", "arguments": {}},
    })
    assert router._failure_time is not None
    assert router._failure_time[1] == FailureReason.MIDSESSION

    writer.messages.clear()

    # Second request — arrives immediately within MIDSESSION cooldown window
    await router.handle_message({
        "jsonrpc": "2.0",
        "id": 13,
        "method": "tools/call",
        "params": {"name": "t", "arguments": {}},
    })

    assert len(writer.messages) == 1
    err = writer.messages[0]
    assert "error" in err
    msg = err["error"]["message"]
    assert f"cooldown={FAILURE_COOLDOWN_MIDSESSION}s" in msg


# ---------------------------------------------------------------------------
# Test 10: start-time failure uses 10s cooldown
# ---------------------------------------------------------------------------


async def test_start_failure_keeps_10s_cooldown(tmp_path: Path) -> None:
    """FR-22.5: _do_start() fails from cold start (not retry) → _failure_time tagged START → 10s cooldown."""
    router, writer, sm = await _make_router(tmp_path)

    async def _failing_start() -> None:
        # Mimic real LifecycleManager.start(): transition to STARTING then FAILED, then raise.
        # No manual sm._state cheating — transitions go through the state machine.
        await sm.transition(BackendState.STARTING)
        await sm.transition(BackendState.FAILED)
        raise LifecycleError("docker timeout")

    router._lifecycle.start = AsyncMock(side_effect=_failing_start)  # type: ignore[method-assign]

    await router.handle_message({
        "jsonrpc": "2.0",
        "id": 14,
        "method": "tools/call",
        "params": {"name": "t", "arguments": {}},
    })

    assert router._failure_time is not None
    assert router._failure_time[1] == FailureReason.START

    writer.messages.clear()

    # Second request — should be gated by the 10s START cooldown
    await router.handle_message({
        "jsonrpc": "2.0",
        "id": 15,
        "method": "tools/call",
        "params": {"name": "t", "arguments": {}},
    })

    assert len(writer.messages) == 1
    err = writer.messages[0]
    assert "error" in err
    msg = err["error"]["message"]
    assert f"cooldown={FAILURE_COOLDOWN_START}s" in msg


# ---------------------------------------------------------------------------
# Test 11: tag override — retry's _do_start failure → MIDSESSION tag
# ---------------------------------------------------------------------------


async def test_tag_override_on_retry_do_start_failure(tmp_path: Path) -> None:
    """FR-22.5: if retry's ensure_active() calls _do_start() and it fails,
    the retry-failure branch OVERWRITES the START tag to MIDSESSION."""

    class _AlwaysDiesTransport(_BaseTransport):
        async def request(self, method: str, params: Any = None, id: Any = None) -> dict[str, Any]:
            if method == "tools/call":
                raise TransportError("Write stream closed")
            return await super().request(method, params, id)

    transport = _AlwaysDiesTransport()
    initial_start_done = False

    async def _start_that_fails_on_retry() -> None:
        nonlocal initial_start_done
        if not initial_start_done:
            initial_start_done = True
            await sm.transition(BackendState.STARTING)
            await sm.transition(BackendState.HEALTHY)
        else:
            # Retry's _do_start — mimic real LifecycleManager: transition to STARTING then FAILED.
            # No manual sm._state cheating — transitions go through the state machine.
            await sm.transition(BackendState.STARTING)
            await sm.transition(BackendState.FAILED)
            raise LifecycleError("restart failed")

    router, writer, sm = await _make_router(tmp_path, transport_factory=lambda: transport)
    router._lifecycle.start = AsyncMock(side_effect=_start_that_fails_on_retry)  # type: ignore[method-assign]
    await _force_active(router, sm)

    await router.handle_message({
        "jsonrpc": "2.0",
        "id": 16,
        "method": "tools/call",
        "params": {"name": "t", "arguments": {}},
    })

    # Final tag must be MIDSESSION (overwriting any START tag from _do_start)
    assert router._failure_time is not None
    assert router._failure_time[1] == FailureReason.MIDSESSION, (
        f"Expected MIDSESSION tag, got {router._failure_time[1]}. "
        "The retry-failure branch must overwrite START tag written by _do_start()."
    )


# ---------------------------------------------------------------------------
# Test 12: concurrent failures — exactly one _do_start per incident
# ---------------------------------------------------------------------------


async def test_concurrent_failures_serialize_one_restart(tmp_path: Path) -> None:
    """FR-22.6: N=5 concurrent handlers all await request() on the same dying transport
    simultaneously. After BLOCKING-1 fix (stale_transport identity check), exactly one
    handler must trigger lifecycle.start; the rest must be no-ops.

    Uses asyncio.Event gates so all N tasks truly suspend inside request() at the
    same time before the transport dies — this reproduces the real concurrent-death race
    that the original test missed.

    The transport_factory creates a NEW transport object each time _do_start() is called,
    so the stale_transport identity check (`self._transport is not stale_transport`) can
    distinguish the stale (pre-failure) transport from the fresh recovery transport.
    """
    N = 5
    # Event that all N tasks wait for before request() proceeds to die
    all_suspended = asyncio.Event()
    release_gate = asyncio.Event()
    suspended_count = 0
    # After the gate is opened and the initial transport dies, subsequent calls succeed.
    # This flag lives on the INITIAL transport instance, not shared across factory calls.

    class _DyingTransport(_BaseTransport):
        """Transport that participates in the gate scenario.

        killed: if True, the NEXT tools/call suspends at the gate then dies.
        """
        def __init__(self, *, kill_on_call: bool = False) -> None:
            super().__init__()
            self._kill_on_call = kill_on_call

        async def request(self, method: str, params: Any = None, id: Any = None) -> dict[str, Any]:
            nonlocal suspended_count
            if method == "tools/call" and self._kill_on_call:
                suspended_count += 1
                if suspended_count == N:
                    all_suspended.set()
                # All N tasks wait here simultaneously → true concurrent death
                await release_gate.wait()
                raise TransportError("Write stream closed")
            return await super().request(method, params, id)

    # The initial transport (installed by _force_active) kills on tools/call.
    # Factory creates a FRESH (non-killing) transport for recovery → distinct object.
    initial_transport = _DyingTransport(kill_on_call=True)
    transport_call_count = 0

    def _factory() -> _DyingTransport:
        nonlocal transport_call_count
        transport_call_count += 1
        if transport_call_count == 1:
            # First call: return the initial (dying) transport
            return initial_transport
        # Subsequent calls (recovery): return a fresh transport that succeeds
        return _DyingTransport(kill_on_call=False)

    # Count lifecycle.start invocations beyond the initial activation
    start_count = 0
    initial_done = False

    async def _counting_start() -> None:
        nonlocal start_count, initial_done
        if not initial_done:
            initial_done = True
        else:
            start_count += 1
        await sm.transition(BackendState.STARTING)
        await sm.transition(BackendState.HEALTHY)

    router, writer, sm = await _make_router(tmp_path, transport_factory=_factory)
    router._lifecycle.start = AsyncMock(side_effect=_counting_start)  # type: ignore[method-assign]
    await _force_active(router, sm)

    start_count = 0  # Reset — only count retry starts

    # Fire N concurrent tasks — all will suspend inside request() waiting for the gate
    tasks = [
        asyncio.create_task(
            router.handle_message({
                "jsonrpc": "2.0",
                "id": 100 + i,
                "method": "tools/call",
                "params": {"name": "t", "arguments": {}},
            })
        )
        for i in range(N)
    ]

    # Wait until all N tasks are suspended inside transport.request()
    await asyncio.wait_for(all_suspended.wait(), timeout=5.0)

    # Release all N tasks simultaneously — they all raise TransportError at once
    release_gate.set()

    await asyncio.gather(*tasks)

    # Critical invariant: EXACTLY one lifecycle.start per incident.
    # Before BLOCKING-1 fix this was N starts; after the fix it must be 1.
    assert start_count == 1, (
        f"Expected exactly 1 lifecycle.start call for N={N} concurrent failures, "
        f"got {start_count}. BLOCKING-1 fix (stale_transport identity check) may be missing."
    )

    # All N handlers must have produced a response (success or error)
    assert len(writer.messages) == N


# ---------------------------------------------------------------------------
# Test 12b: timeout during lifecycle.start → FAILED state → cooldown blocks next request
# ---------------------------------------------------------------------------


async def test_start_timeout_leaves_failed_state_and_cooldown_applies(tmp_path: Path) -> None:
    """BLOCKING-2: asyncio.wait_for cancels _do_start() mid-execution.
    After the timeout, state must be FAILED (not COLD) and _failure_time must be set,
    so a subsequent request within the cooldown window receives LifecycleError immediately
    instead of re-entering _do_start() and hanging again.

    This exercises the BaseException handler in _do_start that catches CancelledError.
    """
    # Use a transport that always dies so the initial request triggers the recovery retry
    class _AlwaysDiesTransport(_BaseTransport):
        async def request(self, method: str, params: Any = None, id: Any = None) -> dict[str, Any]:
            if method == "tools/call":
                raise TransportError("Write stream closed")
            return await super().request(method, params, id)

    transport = _AlwaysDiesTransport()
    router, writer, sm = await _make_router(tmp_path, transport_factory=lambda: transport)
    await _force_active(router, sm)

    # Make restart hang "forever" so wait_for fires and cancels _do_start
    async def _hanging_start() -> None:
        await asyncio.sleep(9999)

    router._lifecycle.start = AsyncMock(side_effect=_hanging_start)  # type: ignore[method-assign]

    # Use a very short timeout to force fast failure
    original_wait_for = asyncio.wait_for

    async def _fast_wait_for(coro: Any, timeout: float) -> Any:
        return await original_wait_for(coro, timeout=0.05)

    with patch("mcp_standby_proxy.router.asyncio.wait_for", side_effect=_fast_wait_for):
        await router.handle_message({
            "jsonrpc": "2.0",
            "id": 50,
            "method": "tools/call",
            "params": {"name": "t", "arguments": {}},
        })

    # State must be FAILED (not COLD) — BLOCKING-2 fix
    assert sm.state == BackendState.FAILED, (
        f"Expected FAILED state after timeout, got {sm.state}. "
        "CancelledError must be caught in _do_start to guarantee FAILED state."
    )

    # _failure_time must be set (MIDSESSION — overwritten by retry-failure branch)
    assert router._failure_time is not None, (
        "_failure_time must be set after timeout — cooldown gate must be armed."
    )

    writer.messages.clear()

    # Second request must be gated by cooldown and must NOT re-enter _do_start
    t0 = asyncio.get_event_loop().time()
    await router.handle_message({
        "jsonrpc": "2.0",
        "id": 51,
        "method": "tools/call",
        "params": {"name": "t", "arguments": {}},
    })
    elapsed = asyncio.get_event_loop().time() - t0

    # Must return in < 500ms (not hang for another 9999s)
    assert elapsed < 0.5, (
        f"Second request took {elapsed:.2f}s — expected < 0.5s (cooldown gate). "
        "Backend must not re-enter _do_start after a failed timeout."
    )

    assert len(writer.messages) == 1
    err = writer.messages[0]
    assert "error" in err
    msg = err["error"]["message"]
    assert "cooldown=" in msg, (
        f"Expected 'cooldown=' in error message (cooldown gate). Got: {msg}"
    )


# ---------------------------------------------------------------------------
# Test 13: forwarded request retry uses IdMapper.wrap(msg_id)
# ---------------------------------------------------------------------------


async def test_id_mapper_forwarded_request_uses_wrap(tmp_path: Path) -> None:
    """FR-22.2: retry in _handle_forwarded_request allocates new id via wrap(msg_id).
    After success, the response carries the original client id."""

    class _DieOnceTransport(_BaseTransport):
        def __init__(self) -> None:
            super().__init__()
            self._tools_call_count = 0

        async def request(self, method: str, params: Any = None, id: Any = None) -> dict[str, Any]:
            if method == "tools/call":
                self._tools_call_count += 1
                if self._tools_call_count == 1:
                    raise TransportError("Write stream closed")
                return {"result": {"content": [{"type": "text", "text": "recovered"}]}, "id": id, "jsonrpc": "2.0"}
            return await super().request(method, params, id)

    transport = _DieOnceTransport()
    router, writer, sm = await _make_router(tmp_path, transport_factory=lambda: transport)
    await _force_active(router, sm)

    client_msg_id = "original-client-id-xyz"

    await router.handle_message({
        "jsonrpc": "2.0",
        "id": client_msg_id,
        "method": "tools/call",
        "params": {"name": "t", "arguments": {}},
    })

    # Response must carry original client id
    assert len(writer.messages) == 1
    msg = writer.messages[0]
    assert msg.get("id") == client_msg_id, (
        f"Expected original client id '{client_msg_id}', got '{msg.get('id')}'"
    )
    assert "result" in msg


# ---------------------------------------------------------------------------
# Test 14: cacheable retry uses next_internal_id (no client_id)
# ---------------------------------------------------------------------------


async def test_id_mapper_cacheable_uses_next_internal_id(tmp_path: Path) -> None:
    """FR-22.2: retry in _handle_cacheable allocates id via next_internal_id()
    (no client_id in scope). The internal id is unique per the mapper counter.

    Strategy: use a 'kill_next_tools_list' flag that is set to True only AFTER
    setup completes, so the bootstrap's tools/list call is not affected.
    """

    kill_next_tools_list = False
    ids_recorded: list[tuple[str, Any]] = []

    class _SequencedTransport(_BaseTransport):
        async def request(self, method: str, params: Any = None, id: Any = None) -> dict[str, Any]:
            nonlocal kill_next_tools_list
            if method == "tools/list":
                ids_recorded.append(("tools/list", id))
                if kill_next_tools_list:
                    kill_next_tools_list = False
                    raise TransportError("Write stream closed")
                return {"result": {"tools": [{"name": "t"}]}, "id": id, "jsonrpc": "2.0"}
            return await super().request(method, params, id)

    transport = _SequencedTransport()
    router, writer, sm = await _make_router(tmp_path, transport_factory=lambda: transport)

    # Activate (bootstrap will call tools/list with kill_next_tools_list=False — succeeds)
    await _force_active(router, sm)
    ids_recorded.clear()

    # Force cache miss by clearing cache file
    router._cache._path.unlink(missing_ok=True)

    # Now arm the kill for the NEXT tools/list call (the client-originated one)
    kill_next_tools_list = True

    # Send tools/list — cache miss → reaches transport → dies → retries
    await router.handle_message({
        "jsonrpc": "2.0",
        "id": "tools-list-client",
        "method": "tools/list",
    })

    # Expect at least 2 tools/list calls:
    # 1. The initial cache-miss call (dies — id removed from mapping immediately after unwrap)
    # 2. The bootstrap inside the retry's _do_start (also tools/list)
    # 3. The explicit retry call in _cacheable_retry (the one we care about)
    # Note: bootstrap calls are also recorded via ids_recorded.
    tools_list_ids = [id_ for (method, id_) in ids_recorded if method == "tools/list"]
    assert len(tools_list_ids) >= 2, (
        f"Expected at least 2 tools/list calls (original + retry path). "
        f"All recorded requests: {ids_recorded}"
    )

    # All internal ids must be unique (no id reuse)
    assert len(tools_list_ids) == len(set(tools_list_ids)), (
        f"All ids must be unique — retry must not reuse the pre-failure id: {tools_list_ids}"
    )

    # All ids must be proxy-internal (start with "p-")
    for id_ in tools_list_ids:
        assert isinstance(id_, str) and id_.startswith("p-"), (
            f"Expected internal proxy id (starts with 'p-'), got: {id_!r}. "
            "cacheable path must use next_internal_id(), not wrap(client_id)."
        )

    # Client sees success with its original id
    success_msgs = [m for m in writer.messages if "result" in m]
    assert len(success_msgs) >= 1, f"Expected success response. Got: {writer.messages}"
    assert success_msgs[0].get("id") == "tools-list-client"


# ---------------------------------------------------------------------------
# Test 15: canonical log line formats
# ---------------------------------------------------------------------------


async def test_log_line_formats(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """FR-22.7: all four canonical log lines emitted at the correct levels."""

    # --- Part A: detection, retry-start, recovery-success ---

    class _DieOnceTransport(_BaseTransport):
        def __init__(self) -> None:
            super().__init__()
            self._tools_call_count = 0

        async def request(self, method: str, params: Any = None, id: Any = None) -> dict[str, Any]:
            if method == "tools/call":
                self._tools_call_count += 1
                if self._tools_call_count == 1:
                    raise TransportError("Write stream closed")
                return {"result": {"content": []}, "id": id, "jsonrpc": "2.0"}
            return await super().request(method, params, id)

    transport_a = _DieOnceTransport()
    router_a, writer_a, sm_a = await _make_router(tmp_path / "a", transport_factory=lambda: transport_a)
    await _force_active(router_a, sm_a)

    with caplog.at_level(logging.DEBUG, logger="mcp_standby_proxy.router"):
        await router_a.handle_message({
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {"name": "t", "arguments": {}},
        })

    records = caplog.records

    server_name = "test-server"  # matches _make_config()

    # Detection WARNING — exact match against the format string produced by router.py
    assert any(
        r.levelno == logging.WARNING
        and r.getMessage() == f"[{server_name}] transport died during tools/call: Write stream closed"
        for r in records
    ), f"Missing detection WARNING. Records: {[(r.levelno, r.getMessage()) for r in records]}"

    # Retry start INFO — exact match
    assert any(
        r.levelno == logging.INFO
        and r.getMessage() == f"[{server_name}] restarting backend after mid-session transport death"
        for r in records
    ), f"Missing retry-start INFO. Records: {[(r.levelno, r.getMessage()) for r in records]}"

    # Recovery success INFO — exact match
    assert any(
        r.levelno == logging.INFO
        and r.getMessage() == f"[{server_name}] transport recovered; tools/call succeeded"
        for r in records
    ), f"Missing recovery-success INFO. Records: {[(r.levelno, r.getMessage()) for r in records]}"

    # --- Part B: retry failure WARNING ---
    caplog.clear()

    class _AlwaysDiesTransport(_BaseTransport):
        async def request(self, method: str, params: Any = None, id: Any = None) -> dict[str, Any]:
            if method == "tools/call":
                raise TransportError("Write stream closed")
            return await super().request(method, params, id)

    transport_b = _AlwaysDiesTransport()
    router_b, writer_b, sm_b = await _make_router(tmp_path / "b", transport_factory=lambda: transport_b)
    await _force_active(router_b, sm_b)

    with caplog.at_level(logging.DEBUG, logger="mcp_standby_proxy.router"):
        await router_b.handle_message({
            "jsonrpc": "2.0",
            "id": 21,
            "method": "tools/call",
            "params": {"name": "t", "arguments": {}},
        })

    # Retry failure WARNING — exact prefix match (exception text varies by failure path)
    assert any(
        r.levelno == logging.WARNING
        and r.getMessage().startswith(f"[{server_name}] transport recovery failed: ")
        for r in caplog.records
    ), f"Missing retry-failure WARNING. Records: {[(r.levelno, r.getMessage()) for r in caplog.records]}"
