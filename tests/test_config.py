import pytest
import yaml

from mcp_standby_proxy.config import (
    BackendTransport,
    HealthcheckType,
    load_config,
)
from mcp_standby_proxy.errors import ConfigError


def _write_config(tmp_path, data: dict):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(data))
    return config_file


def _make_sse_config(tmp_path) -> dict:
    return {
        "version": 1,
        "server": {"name": "test-server"},
        "backend": {
            "transport": "sse",
            "url": "http://localhost:5090/sse",
        },
        "lifecycle": {
            "start": {"command": "true"},
            "stop": {"command": "true"},
            "healthcheck": {
                "type": "http",
                "url": "http://localhost:5090/health",
            },
        },
        "cache": {"path": str(tmp_path / "cache.json")},
    }


def _make_stdio_config(tmp_path) -> dict:
    return {
        "version": 1,
        "server": {"name": "test-server"},
        "backend": {
            "transport": "stdio",
            "command": "npx",
            "args": ["some-mcp"],
        },
        "lifecycle": {
            "start": {"command": "true"},
            "stop": {"command": "true"},
            "healthcheck": {
                "type": "command",
                "command": "true",
            },
        },
        "cache": {"path": str(tmp_path / "cache.json")},
    }


def test_valid_sse_config_parses(tmp_path) -> None:
    cfg = load_config(_write_config(tmp_path, _make_sse_config(tmp_path)))
    assert cfg.version == 1
    assert cfg.server.name == "test-server"
    assert cfg.server.version == "0.0.0"
    assert cfg.server.instructions is None
    assert cfg.backend.transport == BackendTransport.SSE
    assert cfg.backend.url == "http://localhost:5090/sse"
    assert cfg.backend.args == []
    assert cfg.backend.env == {}
    assert cfg.lifecycle.start.timeout == 30
    assert cfg.lifecycle.stop.timeout == 30
    assert cfg.lifecycle.healthcheck.interval == 2
    assert cfg.lifecycle.healthcheck.max_attempts == 30
    assert cfg.lifecycle.healthcheck.timeout == 5
    assert cfg.lifecycle.idle_timeout == 300
    assert cfg.cache.auto_refresh is True


def test_valid_stdio_config_parses(tmp_path) -> None:
    cfg = load_config(_write_config(tmp_path, _make_stdio_config(tmp_path)))
    assert cfg.backend.transport == BackendTransport.STDIO
    assert cfg.backend.command == "npx"
    assert cfg.backend.args == ["some-mcp"]


def test_missing_backend_url_for_sse_raises(tmp_path) -> None:
    data = _make_sse_config(tmp_path)
    del data["backend"]["url"]
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path, data))


def test_missing_backend_command_for_stdio_raises(tmp_path) -> None:
    data = _make_stdio_config(tmp_path)
    del data["backend"]["command"]
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path, data))


def test_version_2_raises_config_error(tmp_path) -> None:
    data = _make_sse_config(tmp_path)
    data["version"] = 2
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path, data))


def test_missing_server_name_raises(tmp_path) -> None:
    data = _make_sse_config(tmp_path)
    del data["server"]["name"]
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path, data))


def test_missing_lifecycle_start_command_raises(tmp_path) -> None:
    data = _make_sse_config(tmp_path)
    del data["lifecycle"]["start"]["command"]
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path, data))


def test_healthcheck_http_without_url_raises(tmp_path) -> None:
    data = _make_sse_config(tmp_path)
    data["lifecycle"]["healthcheck"] = {"type": "http"}
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path, data))


def test_healthcheck_tcp_without_address_raises(tmp_path) -> None:
    data = _make_sse_config(tmp_path)
    data["lifecycle"]["healthcheck"] = {"type": "tcp"}
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path, data))


def test_healthcheck_command_without_command_raises(tmp_path) -> None:
    data = _make_stdio_config(tmp_path)
    data["lifecycle"]["healthcheck"] = {"type": "command"}
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path, data))


def test_lifecycle_timeout_zero_raises(tmp_path) -> None:
    data = _make_sse_config(tmp_path)
    data["lifecycle"]["start"]["timeout"] = 0
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path, data))


def test_lifecycle_timeout_601_raises(tmp_path) -> None:
    data = _make_sse_config(tmp_path)
    data["lifecycle"]["start"]["timeout"] = 601
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path, data))


def test_cache_path_nonexistent_parent_raises(tmp_path) -> None:
    data = _make_sse_config(tmp_path)
    data["cache"]["path"] = "/nonexistent/dir/cache.json"
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path, data))


def test_idle_timeout_accepted(tmp_path) -> None:
    data = _make_sse_config(tmp_path)
    data["lifecycle"]["idle_timeout"] = 600
    cfg = load_config(_write_config(tmp_path, data))
    assert cfg.lifecycle.idle_timeout == 600


def test_auto_refresh_accepted(tmp_path) -> None:
    data = _make_sse_config(tmp_path)
    data["cache"]["auto_refresh"] = False
    cfg = load_config(_write_config(tmp_path, data))
    assert cfg.cache.auto_refresh is False


def test_load_config_from_yaml_file(tmp_path) -> None:
    cfg = load_config(_write_config(tmp_path, _make_sse_config(tmp_path)))
    assert cfg.server.name == "test-server"
    assert cfg.backend.transport == BackendTransport.SSE


def test_load_config_file_not_found(tmp_path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nonexistent.yaml")


def test_load_config_invalid_yaml(tmp_path) -> None:
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("version: 1\nserver: [invalid: yaml: }{")
    with pytest.raises(ConfigError):
        load_config(bad_yaml)


def test_backend_url_must_start_with_http(tmp_path) -> None:
    data = _make_sse_config(tmp_path)
    data["backend"]["url"] = "ftp://localhost:5090/sse"
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path, data))


def test_streamable_http_transport_requires_url(tmp_path) -> None:
    data = _make_sse_config(tmp_path)
    data["backend"]["transport"] = "streamable_http"
    del data["backend"]["url"]
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path, data))


def test_tcp_healthcheck_valid(tmp_path) -> None:
    data = _make_sse_config(tmp_path)
    data["lifecycle"]["healthcheck"] = {
        "type": "tcp",
        "address": "localhost:5090",
    }
    cfg = load_config(_write_config(tmp_path, data))
    assert cfg.lifecycle.healthcheck.type == HealthcheckType.TCP
    assert cfg.lifecycle.healthcheck.address == "localhost:5090"


def test_server_instructions_optional(tmp_path) -> None:
    data = _make_sse_config(tmp_path)
    data["server"]["instructions"] = "Some instructions"
    cfg = load_config(_write_config(tmp_path, data))
    assert cfg.server.instructions == "Some instructions"
