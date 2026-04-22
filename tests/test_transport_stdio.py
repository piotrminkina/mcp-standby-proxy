"""Unit tests for StdioTransport.

All tests monkeypatch `mcp_standby_proxy.transport.stdio.stdio_client` with a
fake async context manager that exposes two anyio memory streams, so no real
subprocess is spawned.
"""
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import pytest
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from mcp.shared.session import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCResponse

from mcp_standby_proxy.errors import TransportError
from mcp_standby_proxy.transport.stdio import StdioTransport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_streams() -> tuple[
    MemoryObjectReceiveStream[SessionMessage | Exception],
    MemoryObjectSendStream[SessionMessage],
    MemoryObjectSendStream[SessionMessage | Exception],
    MemoryObjectReceiveStream[SessionMessage],
]:
    """Create a matched pair of anyio memory object streams for testing.

    Returns (read_stream, write_stream, feed_stream, drain_stream) where:
    - read_stream / write_stream are what StdioTransport sees (matching the
      shape stdio_client yields).
    - feed_stream is used by tests to push fake responses into read_stream.
    - drain_stream is used by tests to receive what the transport sent.
    """
    # Streams from transport's perspective:
    #   read_stream:  transport reads responses from SDK
    #   write_stream: transport writes requests to SDK
    feed_send, read_recv = anyio.create_memory_object_stream[SessionMessage | Exception](16)
    write_send, drain_recv = anyio.create_memory_object_stream[SessionMessage](16)
    return read_recv, write_send, feed_send, drain_recv


def _make_response_message(id: Any, result: dict | None = None) -> SessionMessage:
    """Build a SessionMessage wrapping a JSONRPCResponse."""
    resp = JSONRPCResponse(
        jsonrpc="2.0",
        id=id,
        result=result or {},
    )
    return SessionMessage(message=JSONRPCMessage(resp))


def _make_transport(
    config_dir: Path | None = None,
    env: dict[str, str] | None = None,
) -> StdioTransport:
    return StdioTransport(
        command="echo",
        args=["hello"],
        env=env or {},
        cwd=config_dir or Path("/tmp"),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_enters_sdk_context_with_correct_params(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """stdio_client is called with StdioServerParameters containing the right fields."""
    read_recv, write_send, _feed, _drain = _make_streams()
    captured_params: list[Any] = []

    @asynccontextmanager
    async def fake_stdio_client(params):
        captured_params.append(params)
        yield read_recv, write_send

    monkeypatch.setattr("mcp_standby_proxy.transport.stdio.stdio_client", fake_stdio_client)

    transport = StdioTransport(
        command="node",
        args=["server.js", "--port", "3000"],
        env={"FOO": "bar"},
        cwd=tmp_path,
    )
    await transport.connect()

    assert len(captured_params) == 1
    p = captured_params[0]
    assert p.command == "node"
    assert p.args == ["server.js", "--port", "3000"]
    assert p.cwd == str(tmp_path)
    # Env must contain both PATH (inherited) and the config-specified FOO
    assert "PATH" in p.env
    assert p.env["FOO"] == "bar"


@pytest.mark.asyncio
async def test_connect_env_config_overrides_inherited(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Config env values override os.environ values (config wins)."""
    read_recv, write_send, _feed, _drain = _make_streams()
    captured_params: list[Any] = []

    @asynccontextmanager
    async def fake_stdio_client(params):
        captured_params.append(params)
        yield read_recv, write_send

    monkeypatch.setattr("mcp_standby_proxy.transport.stdio.stdio_client", fake_stdio_client)
    # Override os.environ for the duration of this test
    monkeypatch.setattr(
        "mcp_standby_proxy.transport.stdio.os.environ",
        {"FOO": "from-os", "PATH": "/usr/bin"},
    )

    transport = StdioTransport(
        command="node",
        args=[],
        env={"FOO": "from-config"},
        cwd=tmp_path,
    )
    await transport.connect()

    assert captured_params[0].env["FOO"] == "from-config"
    # Inherited key that is NOT overridden is preserved
    assert captured_params[0].env["PATH"] == "/usr/bin"


@pytest.mark.asyncio
async def test_connect_spawn_failure_wrapped_as_transport_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """OSError from stdio_client.__aenter__ is wrapped in TransportError."""

    @asynccontextmanager
    async def fake_stdio_client(params):
        raise OSError(2, "No such file or directory")
        yield  # make it a generator

    monkeypatch.setattr("mcp_standby_proxy.transport.stdio.stdio_client", fake_stdio_client)

    transport = _make_transport(config_dir=tmp_path)
    with pytest.raises(TransportError, match="Failed to spawn stdio backend"):
        await transport.connect()

    assert transport.is_connected() is False


@pytest.mark.asyncio
async def test_close_after_failed_connect_is_silent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """close() after a failed connect must not raise and must emit no WARNING logs.

    Regression for the bug where _session_context was stored before __aenter__
    succeeded, causing close() to call __aexit__ on an unentered context manager
    (RuntimeError: generator didn't yield) — logged as a spurious WARNING.
    """
    import logging

    @asynccontextmanager
    async def fake_stdio_client(params):
        raise OSError(2, "No such file or directory")
        yield  # make it a generator

    monkeypatch.setattr("mcp_standby_proxy.transport.stdio.stdio_client", fake_stdio_client)

    transport = _make_transport(config_dir=tmp_path)
    with pytest.raises(TransportError):
        await transport.connect()

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="mcp_standby_proxy.transport.stdio"):
        await transport.close()

    stdio_warnings = [
        r for r in caplog.records
        if r.name == "mcp_standby_proxy.transport.stdio" and r.levelno >= logging.WARNING
    ]
    assert stdio_warnings == [], f"Unexpected warning(s): {stdio_warnings}"


@pytest.mark.asyncio
async def test_request_sends_frame_and_matches_response_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """request() sends a JSONRPCRequest frame and returns the dict whose id matches."""
    from mcp.types import JSONRPCRequest

    read_recv, write_send, feed_send, drain_recv = _make_streams()

    @asynccontextmanager
    async def fake_stdio_client(params):
        yield read_recv, write_send

    monkeypatch.setattr("mcp_standby_proxy.transport.stdio.stdio_client", fake_stdio_client)

    transport = _make_transport(config_dir=tmp_path)
    await transport.connect()

    # Pre-load the read stream so request() can return immediately after sending.
    # The write stream has a buffer of 16 so send() does not block — the frame
    # lands in drain_recv before request() begins iterating read_recv.
    await feed_send.send(_make_response_message(id=42, result={"tools": []}))
    await feed_send.aclose()

    result = await transport.request("tools/list", id=42)

    # Verify the frame that was actually sent on the write stream
    sent: SessionMessage = drain_recv.receive_nowait()
    inner = sent.message.root
    assert isinstance(inner, JSONRPCRequest)
    assert inner.id == 42
    assert inner.method == "tools/list"

    assert result["id"] == 42


@pytest.mark.asyncio
async def test_request_skips_non_matching_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """request() ignores responses with non-matching ids and returns the first match."""
    read_recv, write_send, feed_send, drain_recv = _make_streams()

    @asynccontextmanager
    async def fake_stdio_client(params):
        yield read_recv, write_send

    monkeypatch.setattr("mcp_standby_proxy.transport.stdio.stdio_client", fake_stdio_client)

    transport = _make_transport(config_dir=tmp_path)
    await transport.connect()

    # First push a response with a different id, then the matching one
    await feed_send.send(_make_response_message(id=99, result={"ignore": True}))
    await feed_send.send(_make_response_message(id=42, result={"tools": ["real"]}))
    await feed_send.aclose()

    result = await transport.request("tools/list", id=42)
    assert result["id"] == 42
    assert result["result"] == {"tools": ["real"]}


@pytest.mark.asyncio
async def test_request_raises_transport_error_on_closed_write_stream(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ClosedResourceError on write stream raises TransportError; is_connected() → False."""
    read_recv, write_send, feed_send, drain_recv = _make_streams()

    @asynccontextmanager
    async def fake_stdio_client(params):
        yield read_recv, write_send

    monkeypatch.setattr("mcp_standby_proxy.transport.stdio.stdio_client", fake_stdio_client)

    transport = _make_transport(config_dir=tmp_path)
    await transport.connect()

    # Close the write side so sending raises ClosedResourceError
    await write_send.aclose()

    with pytest.raises(TransportError, match="Write stream closed"):
        await transport.request("tools/list", id=1)

    assert transport.is_connected() is False


@pytest.mark.asyncio
async def test_request_raises_transport_error_when_stream_ends_without_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Stream ends without matching response raises TransportError; is_connected() → False."""
    read_recv, write_send, feed_send, drain_recv = _make_streams()

    @asynccontextmanager
    async def fake_stdio_client(params):
        yield read_recv, write_send

    monkeypatch.setattr("mcp_standby_proxy.transport.stdio.stdio_client", fake_stdio_client)

    transport = _make_transport(config_dir=tmp_path)
    await transport.connect()

    # Close the feed side (no messages) so iteration raises EndOfStream or
    # the loop exits, triggering the "stream ended" fallback TransportError
    await feed_send.aclose()

    with pytest.raises(TransportError):
        await transport.request("tools/list", id=1)

    assert transport.is_connected() is False


@pytest.mark.asyncio
async def test_notify_sends_notification_frame(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """notify() sends a JSONRPCNotification (no id field) on the write stream."""
    read_recv, write_send, feed_send, drain_recv = _make_streams()

    @asynccontextmanager
    async def fake_stdio_client(params):
        yield read_recv, write_send

    monkeypatch.setattr("mcp_standby_proxy.transport.stdio.stdio_client", fake_stdio_client)

    transport = _make_transport(config_dir=tmp_path)
    await transport.connect()

    await transport.notify("notifications/progress", params={"progress": 50})

    # Drain what was sent
    sent: SessionMessage = drain_recv.receive_nowait()
    inner = sent.message.root
    assert inner.method == "notifications/progress"
    # A notification has no 'id' in the JSON-RPC sense — verify model type
    from mcp.types import JSONRPCNotification
    assert isinstance(inner, JSONRPCNotification)


@pytest.mark.asyncio
async def test_close_exits_sdk_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """close() calls __aexit__; is_connected() is False; calling close() twice is a no-op."""
    read_recv, write_send, feed_send, drain_recv = _make_streams()
    exit_call_count = 0

    class _FakeContext:
        async def __aenter__(self):
            return read_recv, write_send

        async def __aexit__(self, *args):
            nonlocal exit_call_count
            exit_call_count += 1

    def fake_stdio_client(params):
        return _FakeContext()

    monkeypatch.setattr("mcp_standby_proxy.transport.stdio.stdio_client", fake_stdio_client)

    transport = _make_transport(config_dir=tmp_path)
    await transport.connect()
    assert transport.is_connected() is True

    await transport.close()
    assert transport.is_connected() is False
    assert exit_call_count == 1

    # Second close must be a no-op (no exception, __aexit__ not called again)
    await transport.close()
    assert exit_call_count == 1


@pytest.mark.asyncio
async def test_close_swallows_exit_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """If __aexit__ raises, close() logs a warning and returns cleanly."""
    read_recv, write_send, _feed, _drain = _make_streams()

    class _FaultyContext:
        async def __aenter__(self):
            return read_recv, write_send

        async def __aexit__(self, *args):
            raise RuntimeError("SDK blew up during cleanup")

    def fake_stdio_client(params):
        return _FaultyContext()

    monkeypatch.setattr("mcp_standby_proxy.transport.stdio.stdio_client", fake_stdio_client)

    transport = _make_transport(config_dir=tmp_path)
    await transport.connect()

    # Must not raise
    await transport.close()
    assert transport.is_connected() is False


def test_is_connected_false_before_connect(tmp_path: Path) -> None:
    """A freshly constructed StdioTransport is not connected."""
    transport = _make_transport(config_dir=tmp_path)
    assert transport.is_connected() is False
