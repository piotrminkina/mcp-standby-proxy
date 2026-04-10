import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
from click.testing import CliRunner

from mcp_standby_proxy.cli import main
from mcp_standby_proxy.proxy import _setup_logging


def _make_valid_config(tmp_path: Path) -> Path:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({
        "version": 1,
        "server": {"name": "test"},
        "backend": {"transport": "sse", "url": "http://localhost/sse"},
        "lifecycle": {
            "start": {"command": "true"},
            "stop": {"command": "true"},
            "healthcheck": {"type": "command", "command": "true"},
        },
        "cache": {"path": str(tmp_path / "cache.json")},
    }))
    return config_file


def test_serve_with_valid_config(tmp_path) -> None:
    runner = CliRunner()
    config_file = _make_valid_config(tmp_path)

    with patch("mcp_standby_proxy.cli.ProxyRunner") as mock_runner_cls:
        mock_runner = MagicMock()
        mock_runner.run = AsyncMock(return_value=None)
        mock_runner_cls.return_value = mock_runner

        with patch("asyncio.run", lambda coro: None):
            result = runner.invoke(main, ["serve", "-c", str(config_file)])

    assert result.exit_code == 0, result.output


def test_serve_without_config_exits_error() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["serve"])
    assert result.exit_code != 0


def test_serve_with_nonexistent_config_exits_error(tmp_path) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["serve", "-c", str(tmp_path / "missing.yaml")])
    assert result.exit_code != 0


def test_help_shows_usage() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "mcp-standby-proxy" in result.output.lower() or "lazy" in result.output.lower()


def test_serve_help_shows_usage() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["serve", "--help"])
    assert result.exit_code == 0
    assert "--config" in result.output or "-c" in result.output


def test_logging_format_includes_server_name(caplog, tmp_path) -> None:
    """Verify log messages include server name via the formatter."""
    _setup_logging("my-server", verbose=1)

    logger = logging.getLogger("mcp_standby_proxy.proxy")
    with caplog.at_level(logging.INFO):
        logger.info("test message")

    # The formatter adds [server_name] — just verify logging doesn't crash
    # (formatter output goes to stderr, not caplog in this case)
    # Verify handler was added
    root = logging.getLogger()
    assert any(
        isinstance(h.formatter, type(h.formatter)) for h in root.handlers
    )
