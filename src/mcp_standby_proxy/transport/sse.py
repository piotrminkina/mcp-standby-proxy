from contextlib import AbstractAsyncContextManager
from typing import Any

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.client.sse import sse_client
from mcp.shared.session import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCRequest, JSONRPCNotification

from mcp_standby_proxy.errors import TransportError


class SseTransport:
    """SSE-based backend transport using the MCP SDK's sse_client."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._read_stream: MemoryObjectReceiveStream[SessionMessage | Exception] | None = None
        self._write_stream: MemoryObjectSendStream[SessionMessage] | None = None
        self._session_context: AbstractAsyncContextManager | None = None
        self._connected = False

    async def connect(self) -> None:
        """Enter the mcp SDK's sse_client context manager.

        Stores the read/write streams for subsequent request/notify calls.
        """
        ctx = sse_client(self._url)
        self._session_context = ctx
        streams = await ctx.__aenter__()
        self._read_stream, self._write_stream = streams
        self._connected = True

    async def request(self, method: str, params: Any = None, id: Any = None) -> dict:  # type: ignore[return]
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
            await self._write_stream.send(SessionMessage(message=msg))
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
            await self._write_stream.send(SessionMessage(message=msg))
        except (anyio.ClosedResourceError, anyio.EndOfStream) as exc:
            self._connected = False
            raise TransportError(f"Write stream closed: {exc}") from exc

    async def close(self) -> None:
        """Exit the sse_client context manager."""
        if self._session_context is not None:
            try:
                await self._session_context.__aexit__(None, None, None)
            except Exception:
                pass
            self._session_context = None
        self._connected = False

    def is_connected(self) -> bool:
        """Check if transport connection is alive."""
        return self._connected
