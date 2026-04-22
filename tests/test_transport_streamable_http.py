import logging
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest
from anyio.streams.memory import MemoryObjectSendStream
from mcp.shared.session import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCResponse

from mcp_standby_proxy.errors import TransportError
from mcp_standby_proxy.transport.streamable_http import StreamableHttpTransport


def _make_streams():
    """Create a pair of in-memory streams for testing."""
    send, recv = anyio.create_memory_object_stream[SessionMessage | Exception](16)
    return send, recv


def _response_message(id: Any, result: Any) -> SessionMessage:
    msg = JSONRPCMessage(
        JSONRPCResponse(jsonrpc="2.0", id=id, result=result)
    )
    return SessionMessage(message=msg)


def _make_mock_streamable_http_context(client_recv, client_send):
    """Return a mock streamable_http_client context that yields (client_recv, client_send, get_session_id).

    client_recv: what the transport reads from (we push responses here)
    client_send: what the transport writes to (we read requests from here)
    """
    @asynccontextmanager
    async def _ctx(url, **kwargs):
        yield client_recv, client_send, lambda: None  # 3rd elem: get_session_id stub

    return _ctx


async def test_connect_sets_connected() -> None:
    transport = StreamableHttpTransport("http://localhost:8080/mcp")
    assert not transport.is_connected()

    client_send, client_recv = _make_streams()
    with patch(
        "mcp_standby_proxy.transport.streamable_http.streamable_http_client",
        _make_mock_streamable_http_context(client_recv, client_send),
    ):
        await transport.connect()

    assert transport.is_connected()
    await transport.close()


async def test_request_sends_and_reads_response() -> None:
    client_write, _ = _make_streams()  # transport writes here
    server_send, server_recv = _make_streams()  # transport reads from here

    transport = StreamableHttpTransport("http://localhost:8080/mcp")

    @asynccontextmanager
    async def _ctx(url, **kwargs):
        yield server_recv, client_write, lambda: None

    with patch("mcp_standby_proxy.transport.streamable_http.streamable_http_client", _ctx):
        await transport.connect()

    # Push a response into the read stream
    await server_send.send(_response_message(id="p-1", result={"tools": []}))

    result = await transport.request("tools/list", id="p-1")
    assert result["id"] == "p-1"
    assert result["result"] == {"tools": []}

    await transport.close()


async def test_request_closed_stream_raises_transport_error() -> None:
    _, server_recv = _make_streams()
    closed_write: MemoryObjectSendStream = MagicMock()
    closed_write.send = AsyncMock(side_effect=anyio.ClosedResourceError())

    transport = StreamableHttpTransport("http://localhost:8080/mcp")

    @asynccontextmanager
    async def _ctx(url, **kwargs):
        yield server_recv, closed_write, lambda: None

    with patch("mcp_standby_proxy.transport.streamable_http.streamable_http_client", _ctx):
        await transport.connect()

    with pytest.raises(TransportError):
        await transport.request("tools/list", id="p-1")

    assert not transport.is_connected()
    await transport.close()


async def test_close_sets_disconnected() -> None:
    client_write, client_recv = _make_streams()

    transport = StreamableHttpTransport("http://localhost:8080/mcp")

    @asynccontextmanager
    async def _ctx(url, **kwargs):
        yield client_recv, client_write, lambda: None

    with patch("mcp_standby_proxy.transport.streamable_http.streamable_http_client", _ctx):
        await transport.connect()

    assert transport.is_connected()
    await transport.close()
    assert not transport.is_connected()


async def test_notify_sends_message_without_response() -> None:
    client_write, client_read = _make_streams()
    _, server_recv = _make_streams()

    transport = StreamableHttpTransport("http://localhost:8080/mcp")

    @asynccontextmanager
    async def _ctx(url, **kwargs):
        yield server_recv, client_write, lambda: None

    with patch("mcp_standby_proxy.transport.streamable_http.streamable_http_client", _ctx):
        await transport.connect()

    await transport.notify("notifications/initialized")
    # Notification was sent — we can read it from client_read
    sent = await client_read.receive()
    assert sent.message.root.method == "notifications/initialized"

    await transport.close()


async def test_response_matching_ignores_wrong_id() -> None:
    """If multiple messages arrive, only the one with matching id is returned."""
    client_write, _ = _make_streams()
    server_send, server_recv = _make_streams()

    transport = StreamableHttpTransport("http://localhost:8080/mcp")

    @asynccontextmanager
    async def _ctx(url, **kwargs):
        yield server_recv, client_write, lambda: None

    with patch("mcp_standby_proxy.transport.streamable_http.streamable_http_client", _ctx):
        await transport.connect()

    # Push wrong id first, then correct id
    await server_send.send(_response_message(id="wrong", result={"x": 1}))
    await server_send.send(_response_message(id="p-42", result={"answer": 42}))

    result = await transport.request("some/method", id="p-42")
    assert result["id"] == "p-42"
    assert result["result"]["answer"] == 42

    await transport.close()


async def test_request_not_connected_raises_transport_error() -> None:
    transport = StreamableHttpTransport("http://localhost:8080/mcp")

    with pytest.raises(TransportError, match="not connected"):
        await transport.request("tools/list", id="p-1")


async def test_notify_not_connected_raises_transport_error() -> None:
    transport = StreamableHttpTransport("http://localhost:8080/mcp")

    with pytest.raises(TransportError, match="not connected"):
        await transport.notify("notifications/initialized")


async def test_close_logs_warning_when_aexit_raises(caplog: pytest.LogCaptureFixture) -> None:
    """close() catches and logs exceptions from __aexit__ instead of propagating."""
    client_write, client_recv = _make_streams()

    transport = StreamableHttpTransport("http://localhost:8080/mcp")

    @asynccontextmanager
    async def _ctx(url, **kwargs):
        yield client_recv, client_write, lambda: None

    with patch("mcp_standby_proxy.transport.streamable_http.streamable_http_client", _ctx):
        await transport.connect()

    # Replace the stored context with one whose __aexit__ raises
    failing_ctx = MagicMock()
    failing_ctx.__aexit__ = AsyncMock(side_effect=RuntimeError("server gone"))
    transport._session_context = failing_ctx

    with caplog.at_level(logging.WARNING, logger="mcp_standby_proxy.transport.streamable_http"):
        await transport.close()  # must not raise

    assert not transport.is_connected()
    assert any("server gone" in r.message for r in caplog.records)


async def test_connect_when_aenter_raises_stays_disconnected() -> None:
    """If __aenter__ raises, _connected stays False and the object remains safe to use."""
    transport = StreamableHttpTransport("http://localhost:8080/mcp")

    @asynccontextmanager
    async def _failing_ctx(url, **kwargs):
        raise ConnectionError("refused")
        yield  # pragma: no cover

    with patch(
        "mcp_standby_proxy.transport.streamable_http.streamable_http_client",
        _failing_ctx,
    ):
        with pytest.raises(ConnectionError):
            await transport.connect()

    assert not transport.is_connected()
    # close() on an unconnected transport must be safe
    await transport.close()


# ---------------------------------------------------------------------------
# Tests — phantom __aexit__ regression
# ---------------------------------------------------------------------------

async def test_close_after_failed_connect_is_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """close() after a failed connect must not raise and must emit no WARNING logs.

    Regression for the phantom __aexit__ bug: _session_context was previously
    stored before __aenter__ succeeded.  After the fix, _session_context is None
    when __aenter__ raises, so close() has nothing to __aexit__ on.
    """
    @asynccontextmanager
    async def _failing_ctx(url, **kwargs):
        raise ConnectionError("HTTP server refused connection")
        yield  # pragma: no cover

    transport = StreamableHttpTransport("http://localhost:8080/mcp")

    with patch(
        "mcp_standby_proxy.transport.streamable_http.streamable_http_client",
        _failing_ctx,
    ):
        with pytest.raises(ConnectionError):
            await transport.connect()

    caplog.clear()
    with caplog.at_level(
        logging.WARNING, logger="mcp_standby_proxy.transport.streamable_http"
    ):
        await transport.close()

    http_warnings = [
        r for r in caplog.records
        if r.name == "mcp_standby_proxy.transport.streamable_http"
        and r.levelno >= logging.WARNING
    ]
    assert http_warnings == [], f"Unexpected warning(s): {http_warnings}"


# ---------------------------------------------------------------------------
# Tests — write timeout (Item 3)
# ---------------------------------------------------------------------------

async def test_request_send_times_out_after_10s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the write stream send() hangs, request() raises TransportError with timeout message.

    Technique A: monkeypatch anyio.fail_after in the streamable_http module to
    raise TimeoutError immediately — no real sleeping.
    """
    import mcp_standby_proxy.transport.streamable_http as http_module

    _, server_recv = _make_streams()
    client_write, _ = _make_streams()

    transport = StreamableHttpTransport("http://localhost:8080/mcp")

    @asynccontextmanager
    async def _ctx(url, **kwargs):
        yield server_recv, client_write, lambda: None

    with patch(
        "mcp_standby_proxy.transport.streamable_http.streamable_http_client", _ctx
    ):
        await transport.connect()

    class _ImmediateTimeoutScope:
        def __enter__(self):
            raise TimeoutError("simulated write timeout")
        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        http_module, "fail_after", lambda s: _ImmediateTimeoutScope()
    )

    with pytest.raises(TransportError, match="timed out after 10s"):
        await transport.request("tools/list", id="p-1")

    assert not transport.is_connected()
