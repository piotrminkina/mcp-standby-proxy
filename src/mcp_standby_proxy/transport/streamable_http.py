import logging
from contextlib import AbstractAsyncContextManager
from typing import Any

import anyio
from anyio import fail_after
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.session import SessionMessage  # type: ignore[attr-defined]
from mcp.types import JSONRPCMessage, JSONRPCRequest, JSONRPCNotification

from mcp_standby_proxy.errors import TransportError

logger = logging.getLogger(__name__)

# Maximum time (seconds) allowed for a zero-buffer MemoryObjectStream send()
# to hand off a message to the SDK's TaskGroup reader.  If the TaskGroup has
# crashed or stalled, send() would block indefinitely without this bound.
WRITE_HANDOFF_TIMEOUT_SECONDS = 10.0


class StreamableHttpTransport:
    """Streamable HTTP backend transport using the MCP SDK's streamable_http_client."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._read_stream: MemoryObjectReceiveStream[SessionMessage | Exception] | None = None
        self._write_stream: MemoryObjectSendStream[SessionMessage] | None = None
        self._session_context: AbstractAsyncContextManager[Any] | None = None
        self._connected = False

    async def connect(self) -> None:
        """Enter the mcp SDK's streamable_http_client context manager.

        Stores the read/write streams for subsequent request/notify calls.
        Session ID management is delegated to the SDK: it tracks the
        Mcp-Session-Id header internally and sends DELETE on __aexit__.

        `_session_context` is assigned only after a successful `__aenter__` so
        that `close()` called after a failed connect does not attempt `__aexit__`
        on an unentered context manager.
        """
        if self._connected:
            return
        ctx = streamable_http_client(self._url)
        try:
            read_stream, write_stream, _get_session_id = await ctx.__aenter__()
        except Exception:
            self._connected = False
            raise
        self._read_stream = read_stream
        self._write_stream = write_stream
        self._session_context = ctx
        self._connected = True

    async def request(self, method: str, params: Any = None, id: Any = None) -> dict[str, Any]:
        """Send a JSON-RPC request and return the response dict with matching id."""
        if self._write_stream is None or self._read_stream is None:
            raise TransportError("Transport is not connected")

        msg = JSONRPCMessage(
            JSONRPCRequest(
                jsonrpc="2.0",
                method=method,
                params=params,
                id=id,
            )
        )
        try:
            with fail_after(WRITE_HANDOFF_TIMEOUT_SECONDS):
                await self._write_stream.send(SessionMessage(message=msg))
        except TimeoutError as exc:
            self._connected = False
            raise TransportError(
                "Write stream handoff timed out after 10s - backend TaskGroup may be unresponsive"
            ) from exc
        except (anyio.ClosedResourceError, anyio.EndOfStream) as exc:
            self._connected = False
            raise TransportError(f"Write stream closed: {exc}") from exc

        # Read until we get a response matching our id
        try:
            async for item in self._read_stream:
                if isinstance(item, Exception):
                    self._connected = False
                    raise TransportError(f"Stream error: {item}") from item
                inner = item.message.root
                if hasattr(inner, "id") and inner.id == id:
                    return inner.model_dump()
        except (anyio.ClosedResourceError, anyio.EndOfStream) as exc:
            self._connected = False
            raise TransportError(f"Read stream closed: {exc}") from exc

        # Stream ended without matching response
        self._connected = False
        raise TransportError(f"Stream ended without a response for id={id}")

    async def notify(self, method: str, params: Any = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if self._write_stream is None:
            raise TransportError("Transport is not connected")

        msg = JSONRPCMessage(
            JSONRPCNotification(
                jsonrpc="2.0",
                method=method,
                params=params,
            )
        )
        try:
            with fail_after(WRITE_HANDOFF_TIMEOUT_SECONDS):
                await self._write_stream.send(SessionMessage(message=msg))
        except TimeoutError as exc:
            self._connected = False
            raise TransportError(
                "Write stream handoff timed out after 10s - backend TaskGroup may be unresponsive"
            ) from exc
        except (anyio.ClosedResourceError, anyio.EndOfStream) as exc:
            self._connected = False
            raise TransportError(f"Write stream closed: {exc}") from exc

    async def close(self) -> None:
        """Exit the streamable_http_client context manager.

        The SDK automatically sends a DELETE request to terminate the session
        if a session ID exists.
        """
        if self._session_context is not None:
            try:
                await self._session_context.__aexit__(None, None, None)
            except Exception as exc:
                logger.warning("Error during transport close: %s", exc)
            self._session_context = None
        self._connected = False

    def is_connected(self) -> bool:
        """Check if transport connection is alive."""
        return self._connected
