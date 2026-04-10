from mcp_standby_proxy.config import BackendConfig, BackendTransport as BackendTransportEnum
from mcp_standby_proxy.errors import ConfigError
from mcp_standby_proxy.transport.base import BackendTransport

__all__ = ["BackendTransport", "create_transport"]


def create_transport(config: BackendConfig) -> BackendTransport:
    """Create transport based on config.backend.transport.

    SSE and Streamable HTTP are supported. Raises ConfigError for unsupported transports.
    """
    if config.transport == BackendTransportEnum.SSE:
        from mcp_standby_proxy.transport.sse import SseTransport
        assert config.url is not None
        return SseTransport(config.url)

    if config.transport == BackendTransportEnum.STREAMABLE_HTTP:
        from mcp_standby_proxy.transport.streamable_http import StreamableHttpTransport
        assert config.url is not None
        return StreamableHttpTransport(config.url)

    raise ConfigError(
        f"Transport '{config.transport.value}' is not implemented. "
        "Only 'sse' and 'streamable_http' transports are supported."
    )
