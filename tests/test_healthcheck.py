import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from mcp_standby_proxy.config import HealthcheckConfig, HealthcheckType
from mcp_standby_proxy.errors import HealthcheckError
from mcp_standby_proxy.healthcheck import run_healthcheck


def _http_config(max_attempts: int = 1, interval: int = 1) -> HealthcheckConfig:
    return HealthcheckConfig(
        type=HealthcheckType.HTTP,
        url="http://localhost:9999/health",
        max_attempts=max_attempts,
        interval=interval,
        timeout=1,
    )


def _tcp_config(address: str = "localhost:9999", max_attempts: int = 1) -> HealthcheckConfig:
    return HealthcheckConfig(
        type=HealthcheckType.TCP,
        address=address,
        max_attempts=max_attempts,
        interval=1,
        timeout=1,
    )


def _command_config(command: str = "true", max_attempts: int = 1) -> HealthcheckConfig:
    return HealthcheckConfig(
        type=HealthcheckType.COMMAND,
        command=command,
        max_attempts=max_attempts,
        interval=1,
        timeout=5,
    )


def _http_client_patch(mock_get_fn):
    """Context manager that patches httpx.AsyncClient with a given get() implementation."""
    mock_client = AsyncMock()
    mock_client.get = mock_get_fn
    mock_client_cls = MagicMock()
    mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return patch("mcp_standby_proxy.healthcheck.httpx.AsyncClient", mock_client_cls)


async def test_http_healthcheck_passes_on_first_attempt() -> None:
    ok_response = MagicMock()
    ok_response.is_success = True

    with _http_client_patch(AsyncMock(return_value=ok_response)):
        await run_healthcheck(_http_config(max_attempts=3), "test")


async def test_http_healthcheck_passes_on_third_attempt() -> None:
    fail_response = MagicMock(is_success=False)
    ok_response = MagicMock(is_success=True)
    call_count = 0

    async def _get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return ok_response if call_count >= 3 else fail_response

    with _http_client_patch(_get), patch("asyncio.sleep", new=AsyncMock()):
        config = _http_config(max_attempts=5, interval=1)
        await run_healthcheck(config, "test")
    assert call_count == 3


async def test_http_healthcheck_max_attempts_raises() -> None:
    fail_response = MagicMock(is_success=False)

    with _http_client_patch(AsyncMock(return_value=fail_response)), \
            patch("asyncio.sleep", new=AsyncMock()):
        config = _http_config(max_attempts=3, interval=1)
        with pytest.raises(HealthcheckError) as exc_info:
            await run_healthcheck(config, "test")
    assert exc_info.value.attempts == 3


async def test_tcp_healthcheck_passes_with_real_server() -> None:
    """Use a real asyncio server on a random port to test TCP healthcheck."""
    server = await asyncio.start_server(
        lambda r, w: w.close(),
        "127.0.0.1",
        0,
    )
    port = server.sockets[0].getsockname()[1]
    async with server:
        config = _tcp_config(address=f"127.0.0.1:{port}")
        await run_healthcheck(config, "test")


async def test_tcp_healthcheck_fails_on_closed_port() -> None:
    # Port 1 is reserved and should refuse connections
    config = _tcp_config(address="127.0.0.1:1", max_attempts=2)
    with patch("asyncio.sleep", new=AsyncMock()):
        with pytest.raises(HealthcheckError):
            await run_healthcheck(config, "test")


async def test_command_healthcheck_true_passes() -> None:
    config = _command_config(command="true")
    await run_healthcheck(config, "test")


async def test_command_healthcheck_false_fails() -> None:
    config = _command_config(command="false", max_attempts=2)
    with patch("asyncio.sleep", new=AsyncMock()):
        with pytest.raises(HealthcheckError) as exc_info:
            await run_healthcheck(config, "test")
    assert exc_info.value.attempts == 2


async def test_http_per_attempt_timeout_retries_on_recovery() -> None:
    """Per-attempt timeout: httpx raises TimeoutException, retries succeed later."""
    call_count = 0

    async def _slow_then_ok(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise httpx.TimeoutException("timeout", request=None)
        response = MagicMock()
        response.is_success = True
        return response

    with _http_client_patch(_slow_then_ok), patch("asyncio.sleep", new=AsyncMock()):
        config = _http_config(max_attempts=5, interval=1)
        await run_healthcheck(config, "test")
    assert call_count == 3


async def test_command_healthcheck_runs_with_config_dir_as_cwd(tmp_path) -> None:
    """Command-type healthcheck passes config_dir as cwd to subprocess."""
    config = _command_config(command="true")

    with patch("asyncio.create_subprocess_shell") as mock_shell:
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)
        mock_shell.return_value = mock_proc

        await run_healthcheck(config, "test", cwd=tmp_path)

    mock_shell.assert_called_once()
    _, kwargs = mock_shell.call_args
    assert kwargs.get("cwd") == tmp_path


async def test_healthcheck_logs_attempts(caplog) -> None:
    fail_response = MagicMock(is_success=False)
    ok_response = MagicMock(is_success=True)
    call_count = 0

    async def _get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return ok_response if call_count == 2 else fail_response

    with _http_client_patch(_get), patch("asyncio.sleep", new=AsyncMock()):
        config = _http_config(max_attempts=3, interval=1)
        with caplog.at_level(logging.DEBUG, logger="mcp_standby_proxy.healthcheck"):
            await run_healthcheck(config, "myserver")

    debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("myserver" in m and "attempt" in m.lower() for m in debug_msgs)

    info_msgs = [r.message for r in caplog.records if r.levelno == logging.INFO]
    assert any("myserver" in m and "passed" in m.lower() for m in info_msgs)
