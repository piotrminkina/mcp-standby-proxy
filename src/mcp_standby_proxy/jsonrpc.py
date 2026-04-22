import asyncio
import json
from typing import Any, BinaryIO

# JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603
SERVER_NOT_INITIALIZED = -32002


def make_response(id: Any, result: Any) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 response."""
    return {"jsonrpc": "2.0", "id": id, "result": result}


def make_error(id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 error response."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": id, "error": error}


def make_notification(method: str, params: Any = None) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 notification (no id)."""
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    return msg


class JsonRpcReader:
    """Reads newline-delimited JSON-RPC messages from an asyncio StreamReader."""

    def __init__(self, reader: asyncio.StreamReader) -> None:
        self._reader = reader

    async def read_message(self) -> dict[str, Any] | None:
        """Read one JSON-RPC message. Returns None on EOF.

        Invalid JSON lines are skipped with a log warning.
        """
        while True:
            try:
                line = await self._reader.readline()
            except (asyncio.IncompleteReadError, ConnectionResetError):
                return None

            if not line:
                return None

            line = line.rstrip(b"\n\r")
            if not line:
                continue

            try:
                parsed: dict[str, Any] = json.loads(line)
                return parsed
            except json.JSONDecodeError:
                # Skip invalid lines; caller should not rely on ordering
                continue


class JsonRpcWriter:
    """Writes newline-delimited JSON-RPC messages to stdout."""

    def __init__(self, writer: asyncio.StreamWriter | BinaryIO) -> None:
        self._writer = writer

    async def write_message(self, message: dict[str, Any]) -> None:
        """Serialize message as JSON + newline, write to stream, flush."""
        line = json.dumps(message, separators=(",", ":")) + "\n"
        encoded = line.encode("utf-8")

        if isinstance(self._writer, asyncio.StreamWriter):
            self._writer.write(encoded)
            await self._writer.drain()
        else:
            # BinaryIO (e.g. sys.stdout.buffer) — synchronous write
            self._writer.write(encoded)
            self._writer.flush()


class IdMapper:
    """Maps client JSON-RPC IDs to internal proxy IDs and back.

    Prevents ID collisions between multiple concurrent client requests and
    ensures ID uniqueness when forwarding to the backend.
    """

    def __init__(self, prefix: str = "p") -> None:
        self._prefix = prefix
        self._counter = 0
        self._proxy_to_client: dict[str, Any] = {}

    def wrap(self, client_id: Any) -> str:
        """Generate internal ID and store mapping. Returns the internal ID."""
        self._counter += 1
        proxy_id = f"{self._prefix}-{self._counter}"
        self._proxy_to_client[proxy_id] = client_id
        return proxy_id

    def unwrap(self, proxy_id: str) -> Any:
        """Look up original client ID by proxy ID. Removes the mapping."""
        return self._proxy_to_client.pop(proxy_id)

    def next_internal_id(self) -> str:
        """Generate an internal ID not mapped to any client (for proxy-originated requests)."""
        self._counter += 1
        return f"{self._prefix}-{self._counter}"
