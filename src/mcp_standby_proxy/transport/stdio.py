import logging
import os
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import anyio
from anyio import fail_after
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.shared.session import SessionMessage  # type: ignore[attr-defined]
from mcp.types import JSONRPCMessage, JSONRPCRequest, JSONRPCNotification

from mcp_standby_proxy.errors import TransportError

logger = logging.getLogger(__name__)

# Maximum time (seconds) allowed for a zero-buffer MemoryObjectStream send()
# to hand off a message to the SDK's TaskGroup reader. If the TaskGroup has
# crashed or stalled, send() would block indefinitely without this bound.
WRITE_HANDOFF_TIMEOUT_SECONDS = 10.0


class StdioTransport:
    """stdio backend transport using the MCP SDK's stdio_client.

    Spawns a child process and communicates via newline-delimited JSON-RPC
    over stdin/stdout. Shutdown (close stdin → wait 2s → SIGTERM → SIGKILL)
    is delegated entirely to the SDK.
    """

    def __init__(
        self,
        command: str,
        args: list[str],
        env: dict[str, str],
        cwd: Path,
    ) -> None:
        self._command = command
        self._args = args
        self._env = env
        self._cwd = cwd
        self._read_stream: MemoryObjectReceiveStream[SessionMessage | Exception] | None = None
        self._write_stream: MemoryObjectSendStream[SessionMessage] | None = None
        self._session_context: AbstractAsyncContextManager[Any] | None = None
        self._connected = False

    async def connect(self) -> None:
        """Spawn the child process via the MCP SDK's stdio_client context manager.

        Pre-merges the full proxy environment with config-specified overrides so that
        vars like LANG, XDG_*, NODE_OPTIONS, VIRTUAL_ENV are visible to the child.
        Config env keys override inherited values (last wins).
        """
        if self._connected:
            return

        merged_env = {**os.environ, **self._env}
        params = StdioServerParameters(
            command=self._command,
            args=self._args,
            env=merged_env,
            cwd=str(self._cwd),
        )
        # FR-21.9: child stderr is pass-through to the proxy's fd 2 at the OS level
        # (SDK default — no capture, no Python logging dispatch). Users debugging a
        # stdio backend must collect its stderr separately.
        ctx = stdio_client(params)
        try:
            self._read_stream, self._write_stream = await ctx.__aenter__()
        except OSError as exc:
            self._connected = False
            raise TransportError(f"Failed to spawn stdio backend: {exc}") from exc
        except Exception:
            self._connected = False
            raise
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
        """Exit the stdio_client context manager.

        Delegates the full shutdown sequence to the SDK:
        close stdin → wait 2s → SIGTERM (whole process group) → SIGKILL.
        Errors are logged and suppressed. Calling close() twice is a no-op.
        """
        if self._session_context is not None:
            try:
                await self._session_context.__aexit__(None, None, None)
            except Exception as exc:
                logger.warning("Error during transport close: %s", exc)
            self._session_context = None
        self._connected = False

    def is_connected(self) -> bool:
        """Check if the transport connection is alive."""
        return self._connected
