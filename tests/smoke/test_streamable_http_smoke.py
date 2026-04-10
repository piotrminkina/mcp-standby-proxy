"""Smoke test: full connect → initialize → tools/list cycle against a real server."""

import pytest

from mcp_standby_proxy.transport.streamable_http import StreamableHttpTransport


@pytest.mark.smoke
async def test_full_cycle_against_real_server(streamable_http_server: str) -> None:
    """Connect to a live FastMCP server and verify the MCP handshake + tools/list."""
    transport = StreamableHttpTransport(streamable_http_server)

    await transport.connect()
    assert transport.is_connected()

    # MCP handshake: initialize
    init_resp = await transport.request(
        "initialize",
        params={
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "smoke-test", "version": "0.0.1"},
        },
        id="init-1",
    )
    assert init_resp["id"] == "init-1"
    assert "result" in init_resp
    assert "protocolVersion" in init_resp["result"]

    # Notify server that client is initialized
    await transport.notify("notifications/initialized")

    # Retrieve tool list — the server exposes the 'echo' tool
    tools_resp = await transport.request("tools/list", id="tools-1")
    assert tools_resp["id"] == "tools-1"
    tool_names = [t["name"] for t in tools_resp["result"]["tools"]]
    assert "echo" in tool_names

    await transport.close()
    assert not transport.is_connected()
