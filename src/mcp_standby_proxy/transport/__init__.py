from mcp_standby_proxy.config import BackendConfig, BackendTransport as BackendTransportEnum
from mcp_standby_proxy.errors import ConfigError
from mcp_standby_proxy.transport.base import BackendTransport

__all__ = ["BackendTransport", "create_transport"]


def create_transport(config: BackendConfig) -> BackendTransport:
    """Create transport based on config.backend.transport.

    Only SSE is supported in MVP. Raises ConfigError for unsupported transports.
    """
    if config.transport == BackendTransportEnum.SSE:
        from mcp_standby_proxy.transport.sse import SseTransport
        assert config.url is not None
        return SseTransport(config.url)

    raise ConfigError(
        f"Transport '{config.transport.value}' is not implemented in MVP. "
        "Only 'sse' transport is supported."
    )
