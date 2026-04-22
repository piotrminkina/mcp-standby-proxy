from pathlib import Path

from mcp_standby_proxy.config import BackendConfig, BackendTransport as BackendTransportEnum
from mcp_standby_proxy.transport.base import BackendTransport

__all__ = ["BackendTransport", "create_transport"]


def create_transport(config: BackendConfig, cwd: Path) -> BackendTransport:
    """Create transport based on config.backend.transport.

    SSE and Streamable HTTP are supported. For stdio, spawns a child process
    in the given working directory.
    """
    if config.transport == BackendTransportEnum.SSE:
        from mcp_standby_proxy.transport.sse import SseTransport
        assert config.url is not None
        return SseTransport(config.url)

    if config.transport == BackendTransportEnum.STREAMABLE_HTTP:
        from mcp_standby_proxy.transport.streamable_http import StreamableHttpTransport
        assert config.url is not None
        return StreamableHttpTransport(config.url)

    if config.transport == BackendTransportEnum.STDIO:
        from mcp_standby_proxy.transport.stdio import StdioTransport
        assert config.command is not None
        return StdioTransport(
            command=config.command,
            args=config.args,
            env=config.env,
            cwd=cwd,
        )

    raise AssertionError(f"Unhandled transport type: {config.transport!r}")  # pragma: no cover
