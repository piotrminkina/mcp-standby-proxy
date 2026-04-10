import asyncio
import socket
from collections.abc import AsyncGenerator

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def streamable_http_server() -> AsyncGenerator[str, None]:
    """Spin up a real FastMCP server over Streamable HTTP on an ephemeral port.

    Yields the base URL (e.g. http://127.0.0.1:<port>/mcp) and tears down
    the uvicorn server on exit.
    """
    mcp = FastMCP("smoke-test-server")

    @mcp.tool()
    def echo(text: str) -> str:
        """Echo the input text back."""
        return text

    app = mcp.streamable_http_app()
    port = _get_free_port()
    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)

    task = asyncio.ensure_future(server.serve())

    # Wait until uvicorn marks itself as started (polls every 50 ms, max 5 s)
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:
        task.cancel()
        raise RuntimeError("uvicorn did not start within 5 seconds")

    url = f"http://127.0.0.1:{port}/mcp"
    try:
        yield url
    finally:
        server.should_exit = True
        await task
