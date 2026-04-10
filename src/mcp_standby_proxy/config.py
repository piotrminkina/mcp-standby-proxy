from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, field_validator, model_validator

from mcp_standby_proxy.errors import ConfigError


class BackendTransport(str, Enum):
    SSE = "sse"
    STREAMABLE_HTTP = "streamable_http"
    STDIO = "stdio"


class HealthcheckType(str, Enum):
    HTTP = "http"
    TCP = "tcp"
    COMMAND = "command"


class ServerConfig(BaseModel):
    name: str
    version: str = "0.0.0"
    instructions: str | None = None


class BackendConfig(BaseModel):
    transport: BackendTransport
    url: str | None = None
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}

    @model_validator(mode="after")
    def validate_transport_fields(self) -> "BackendConfig":
        if self.transport in (BackendTransport.SSE, BackendTransport.STREAMABLE_HTTP):
            if not self.url:
                raise ValueError(
                    f"backend.url is required when transport is '{self.transport.value}'"
                )
            if not (self.url.startswith("http://") or self.url.startswith("https://")):
                raise ValueError(
                    "backend.url must start with 'http://' or 'https://'"
                )
        if self.transport == BackendTransport.STDIO:
            if not self.command:
                raise ValueError(
                    "backend.command is required when transport is 'stdio'"
                )
        return self


class LifecycleCommandConfig(BaseModel):
    command: str
    args: list[str] = []
    timeout: int = 30

    @field_validator("timeout")
    @classmethod
    def validate_timeout(cls, v: int) -> int:
        if not (1 <= v <= 600):
            raise ValueError("timeout must be between 1 and 600")
        return v


class HealthcheckConfig(BaseModel):
    type: HealthcheckType
    url: str | None = None
    address: str | None = None
    command: str | None = None
    interval: int = 2
    max_attempts: int = 30
    timeout: int = 5

    @field_validator("interval")
    @classmethod
    def validate_interval(cls, v: int) -> int:
        if not (1 <= v <= 60):
            raise ValueError("interval must be between 1 and 60")
        return v

    @field_validator("max_attempts")
    @classmethod
    def validate_max_attempts(cls, v: int) -> int:
        if not (1 <= v <= 600):
            raise ValueError("max_attempts must be between 1 and 600")
        return v

    @field_validator("timeout")
    @classmethod
    def validate_timeout(cls, v: int) -> int:
        if not (1 <= v <= 60):
            raise ValueError("timeout must be between 1 and 60")
        return v

    @model_validator(mode="after")
    def validate_type_fields(self) -> "HealthcheckConfig":
        if self.type == HealthcheckType.HTTP and not self.url:
            raise ValueError("healthcheck.url is required when type is 'http'")
        if self.type == HealthcheckType.TCP and not self.address:
            raise ValueError("healthcheck.address is required when type is 'tcp'")
        if self.type == HealthcheckType.COMMAND and not self.command:
            raise ValueError(
                "healthcheck.command is required when type is 'command'"
            )
        return self


class LifecycleConfig(BaseModel):
    start: LifecycleCommandConfig
    stop: LifecycleCommandConfig
    healthcheck: HealthcheckConfig
    idle_timeout: int = 300


class CacheConfig(BaseModel):
    path: str
    auto_refresh: bool = True

    @field_validator("path")
    @classmethod
    def validate_parent_exists(cls, v: str) -> str:
        parent = Path(v).parent
        if not parent.exists():
            raise ValueError(
                f"cache.path parent directory does not exist: {parent}"
            )
        return v


class ProxyConfig(BaseModel):
    version: int
    server: ServerConfig
    backend: BackendConfig
    lifecycle: LifecycleConfig
    cache: CacheConfig

    @field_validator("version")
    @classmethod
    def validate_version(cls, v: int) -> int:
        if v != 1:
            raise ValueError(f"Unsupported config version: {v}. Only version 1 is supported.")
        return v


def load_config(path: Path) -> ProxyConfig:
    """Load and validate proxy configuration from a YAML file.

    Raises ConfigError on any failure (file not found, parse error, validation error).
    """
    try:
        raw: Any = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Failed to parse YAML config: {exc}") from exc

    try:
        return ProxyConfig.model_validate(raw)
    except Exception as exc:
        raise ConfigError(f"Invalid configuration: {exc}") from exc
