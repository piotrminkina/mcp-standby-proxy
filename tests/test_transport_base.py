from typing import Any

import pytest

from mcp_standby_proxy.config import BackendConfig, BackendTransport as BackendTransportEnum
from mcp_standby_proxy.errors import ConfigError
from mcp_standby_proxy.transport import BackendTransport, create_transport


class _ConcreteTransport:
    """A concrete class satisfying BackendTransport protocol for structural check."""

    async def connect(self) -> None:
        pass

    async def request(self, method: str, params: Any = None, id: Any = None) -> dict:
        return {}

    async def notify(self, method: str, params: Any = None) -> None:
        pass

    async def close(self) -> None:
        pass

    def is_connected(self) -> bool:
        return False


def test_concrete_class_satisfies_protocol() -> None:
    """A class implementing all methods is structurally compatible with BackendTransport."""
    transport: BackendTransport = _ConcreteTransport()
    assert transport.is_connected() is False


def test_create_transport_streamable_http_returns_streamable_http_transport() -> None:
    config = BackendConfig(
        transport=BackendTransportEnum.STREAMABLE_HTTP,
        url="http://localhost:8080/mcp",
    )
    from mcp_standby_proxy.transport.streamable_http import StreamableHttpTransport
    transport = create_transport(config)
    assert isinstance(transport, StreamableHttpTransport)


def test_create_transport_stdio_raises_config_error() -> None:
    config = BackendConfig(
        transport=BackendTransportEnum.STDIO,
        command="npx",
    )
    with pytest.raises(ConfigError, match="not implemented"):
        create_transport(config)


def test_create_transport_sse_returns_sse_transport() -> None:
    config = BackendConfig(
        transport=BackendTransportEnum.SSE,
        url="http://localhost:5090/sse",
    )
    from mcp_standby_proxy.transport.sse import SseTransport
    transport = create_transport(config)
    assert isinstance(transport, SseTransport)
