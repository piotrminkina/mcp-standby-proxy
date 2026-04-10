import asyncio
import json
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_standby_proxy.jsonrpc import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    SERVER_NOT_INITIALIZED,
    IdMapper,
    JsonRpcReader,
    JsonRpcWriter,
    make_error,
    make_notification,
    make_response,
)


# --- JsonRpcReader ---

async def test_reader_reads_valid_json_line() -> None:
    data = b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()

    rpc_reader = JsonRpcReader(reader)
    msg = await rpc_reader.read_message()
    assert msg == {"jsonrpc": "2.0", "id": 1, "method": "ping"}


async def test_reader_returns_none_on_eof() -> None:
    reader = asyncio.StreamReader()
    reader.feed_eof()

    rpc_reader = JsonRpcReader(reader)
    msg = await rpc_reader.read_message()
    assert msg is None


async def test_reader_skips_invalid_json() -> None:
    data = b"not json\n" + b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()

    rpc_reader = JsonRpcReader(reader)
    msg = await rpc_reader.read_message()
    # Should skip bad line and return the next valid message
    assert msg == {"jsonrpc": "2.0", "id": 2, "method": "ping"}


async def test_reader_handles_multiple_messages() -> None:
    messages = [
        {"jsonrpc": "2.0", "id": i, "method": "test"}
        for i in range(3)
    ]
    data = b"".join(
        (json.dumps(m) + "\n").encode() for m in messages
    )
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()

    rpc_reader = JsonRpcReader(reader)
    for expected in messages:
        msg = await rpc_reader.read_message()
        assert msg == expected


# --- JsonRpcWriter ---

async def test_writer_writes_json_plus_newline() -> None:
    buf = BytesIO()
    writer = JsonRpcWriter(buf)
    await writer.write_message({"jsonrpc": "2.0", "id": 1, "result": {}})

    buf.seek(0)
    content = buf.read()
    assert content.endswith(b"\n")
    parsed = json.loads(content.strip())
    assert parsed == {"jsonrpc": "2.0", "id": 1, "result": {}}


async def test_writer_with_asyncio_stream_writer() -> None:
    mock_writer = MagicMock(spec=asyncio.StreamWriter)
    mock_writer.write = MagicMock()
    mock_writer.drain = AsyncMock()

    writer = JsonRpcWriter(mock_writer)
    await writer.write_message({"jsonrpc": "2.0", "id": 1, "result": "ok"})

    mock_writer.write.assert_called_once()
    written = mock_writer.write.call_args[0][0]
    assert written.endswith(b"\n")
    parsed = json.loads(written.strip())
    assert parsed["id"] == 1


# --- make_response / make_error / make_notification ---

def test_make_response_structure() -> None:
    r = make_response(id=42, result={"tools": []})
    assert r == {"jsonrpc": "2.0", "id": 42, "result": {"tools": []}}


def test_make_error_structure() -> None:
    err = make_error(id=1, code=INTERNAL_ERROR, message="Oops")
    assert err["jsonrpc"] == "2.0"
    assert err["id"] == 1
    assert err["error"]["code"] == INTERNAL_ERROR
    assert err["error"]["message"] == "Oops"
    assert "data" not in err["error"]


def test_make_error_with_data() -> None:
    err = make_error(id=2, code=METHOD_NOT_FOUND, message="Not found", data={"method": "foo"})
    assert err["error"]["data"] == {"method": "foo"}


def test_make_notification_structure() -> None:
    n = make_notification("notifications/initialized")
    assert n == {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert "id" not in n


def test_make_notification_with_params() -> None:
    n = make_notification("foo", params={"key": "val"})
    assert n["params"] == {"key": "val"}


def test_error_codes_defined() -> None:
    assert PARSE_ERROR == -32700
    assert INVALID_REQUEST == -32600
    assert METHOD_NOT_FOUND == -32601
    assert INTERNAL_ERROR == -32603
    assert SERVER_NOT_INITIALIZED == -32002


# --- IdMapper ---

def test_wrap_returns_sequential_ids() -> None:
    mapper = IdMapper()
    assert mapper.wrap(1) == "p-1"
    assert mapper.wrap(2) == "p-2"
    assert mapper.wrap("abc") == "p-3"


def test_unwrap_returns_original_id() -> None:
    mapper = IdMapper()
    proxy_id = mapper.wrap(42)
    assert mapper.unwrap(proxy_id) == 42


def test_unwrap_removes_mapping() -> None:
    mapper = IdMapper()
    proxy_id = mapper.wrap(42)
    mapper.unwrap(proxy_id)
    with pytest.raises(KeyError):
        mapper.unwrap(proxy_id)


def test_next_internal_id_not_in_mapping() -> None:
    mapper = IdMapper()
    mapper.wrap("client-1")  # uses p-1
    internal_id = mapper.next_internal_id()  # should be p-2
    assert internal_id == "p-2"
    # Should not be in any mapping
    with pytest.raises(KeyError):
        mapper.unwrap(internal_id)


def test_wrap_unwrap_roundtrip() -> None:
    mapper = IdMapper()
    proxy_id = mapper.wrap(42)
    assert proxy_id == "p-1"
    original = mapper.unwrap("p-1")
    assert original == 42


def test_custom_prefix() -> None:
    mapper = IdMapper(prefix="x")
    assert mapper.wrap(1) == "x-1"


def test_multiple_clients_independent_ids() -> None:
    mapper = IdMapper()
    p1 = mapper.wrap("client-a")
    p2 = mapper.wrap("client-b")
    assert p1 != p2
    assert mapper.unwrap(p1) == "client-a"
    assert mapper.unwrap(p2) == "client-b"
