import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, field_validator, model_validator

from mcp_standby_proxy.errors import ConfigError

# Grammar: <integer><unit>, no spaces, case-sensitive.
# Decimal: B, KB, MB, GB (1 KB = 1000 B)
# Binary:  KiB, MiB, GiB (1 KiB = 1024 B)
_SIZE_PATTERN = re.compile(r"^(\d+)(B|KB|MB|GB|KiB|MiB|GiB)$")
_SIZE_MULTIPLIERS: dict[str, int] = {
    "B": 1,
    "KB": 1_000,
    "MB": 1_000_000,
    "GB": 1_000_000_000,
    "KiB": 1_024,
    "MiB": 1_048_576,
    "GiB": 1_073_741_824,
}
_MIN_SIZE_BYTES = 1_000          # 1 KB
_MAX_SIZE_BYTES = 10_000_000_000 # 10 GB


def _parse_size(value: str) -> int:
    """Parse a size string (e.g. '10MB', '500KiB') to bytes.

    Raises ValueError on invalid format or out-of-range values.
    """
    m = _SIZE_PATTERN.match(value)
    if not m:
        raise ValueError(
            f"Invalid size string '{value}'. "
            "Expected format: <integer><unit> with no spaces. "
            "Accepted units: B, KB, MB, GB, KiB, MiB, GiB. "
            "Example: '10MB', '500KiB'."
        )
    amount = int(m.group(1))
    unit = m.group(2)
    bytes_ = amount * _SIZE_MULTIPLIERS[unit]
    if not (_MIN_SIZE_BYTES <= bytes_ <= _MAX_SIZE_BYTES):
        raise ValueError(
            f"Size '{value}' ({bytes_} bytes) is out of range. "
            "Allowed range: 1 KB – 10 GB."
        )
    return bytes_


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


class LogFileLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

    def to_logging_level(self) -> int:
        import logging as _logging
        level: int = getattr(_logging, self.value.upper())
        return level


class LoggingFileConfig(BaseModel):
    path: str
    level: LogFileLevel = LogFileLevel.INFO
    max_size: str = "10MB"
    backup_count: int = 3

    @field_validator("path")
    @classmethod
    def validate_path_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("logging.file.path must be non-empty")
        return v

    @field_validator("max_size")
    @classmethod
    def validate_max_size(cls, v: str) -> str:
        _parse_size(v)  # raises ValueError on invalid format or range
        return v

    @field_validator("backup_count")
    @classmethod
    def validate_backup_count(cls, v: int) -> int:
        if not (1 <= v <= 100):
            raise ValueError("logging.file.backup_count must be between 1 and 100")
        return v

    @property
    def max_size_bytes(self) -> int:
        return _parse_size(self.max_size)


class LoggingConfig(BaseModel):
    file: LoggingFileConfig

    @model_validator(mode="before")
    @classmethod
    def require_file_section(cls, values: Any) -> Any:
        if not isinstance(values, dict) or "file" not in values or not values["file"]:
            raise ValueError(
                "logging section requires a non-empty 'file' sub-section. "
                "Use 'logging: {file: {path: ...}}' or remove the 'logging' key entirely."
            )
        return values


class ProxyConfig(BaseModel):
    version: int
    server: ServerConfig
    backend: BackendConfig
    lifecycle: LifecycleConfig
    cache: CacheConfig
    logging: LoggingConfig | None = None

    @field_validator("version")
    @classmethod
    def validate_version(cls, v: int) -> int:
        if v != 1:
            raise ValueError(f"Unsupported config version: {v}. Only version 1 is supported.")
        return v


def _resolve_path(raw: str, config_dir: Path) -> Path:
    p = Path(raw)
    if p.is_absolute():
        return p.resolve()
    return (config_dir / p).resolve()


@dataclass(frozen=True)
class LoadedConfig:
    """Result of loading a config file. Bundles the parsed config with
    path-resolution context derived from the config file's location."""

    config: ProxyConfig
    config_dir: Path
    resolved_cache_path: Path
    resolved_log_path: Path | None  # None when logging.file is not configured


def load_config(path: Path) -> LoadedConfig:
    """Load and validate proxy configuration from a YAML file.

    Resolves relative paths (cache.path, logging.file.path) against the config
    file's parent directory. Raises ConfigError on any failure (file not found,
    parse error, validation error).
    """
    try:
        raw: Any = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Failed to parse YAML config: {exc}") from exc

    try:
        config = ProxyConfig.model_validate(raw)
    except Exception as exc:
        raise ConfigError(f"Invalid configuration: {exc}") from exc

    config_dir = path.resolve().parent
    resolved_cache_path = _resolve_path(config.cache.path, config_dir)

    resolved_log_path: Path | None = None
    if config.logging is not None:
        resolved_log_path = _resolve_path(config.logging.file.path, config_dir)

    return LoadedConfig(
        config=config,
        config_dir=config_dir,
        resolved_cache_path=resolved_cache_path,
        resolved_log_path=resolved_log_path,
    )
