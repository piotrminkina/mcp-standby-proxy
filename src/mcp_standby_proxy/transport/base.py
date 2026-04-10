from typing import Any, Protocol


class BackendTransport(Protocol):
    """Protocol for communicating with a backend MCP server.

    Implementations handle framing, connection management, and transport details.
    """

    async def connect(self) -> None:
        """Establish connection to backend.

        For stdio: spawn child process via asyncio.create_subprocess_exec.
        For SSE: GET the SSE endpoint, receive 'endpoint' event, note POST URL.
        For Streamable HTTP: no persistent connection (stateless POST per request).
        """
        ...

    async def request(self, method: str, params: Any = None, id: Any = None) -> dict:  # type: ignore[return]
        """Send JSON-RPC request and return the response as a raw dict.

        The transport handles framing (newline-delimited JSON, HTTP POST, etc.)
        and correlates request ID to response.
        """
        ...

    async def notify(self, method: str, params: Any = None) -> None:
        """Send JSON-RPC notification (no response expected)."""
        ...

    async def close(self) -> None:
        """Gracefully close connection.

        For stdio: close stdin, wait, SIGTERM, SIGKILL.
        For SSE: close the SSE connection.
        For Streamable HTTP: send DELETE if session exists.
        """
        ...

    def is_connected(self) -> bool:
        """Check if transport connection is alive."""
        ...
