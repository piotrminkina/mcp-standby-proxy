import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from mcp_standby_proxy.config import (
    HealthcheckConfig,
    HealthcheckType,
    LifecycleCommandConfig,
    LifecycleConfig,
)
from mcp_standby_proxy.errors import HealthcheckError, StartError
from mcp_standby_proxy.lifecycle import LifecycleManager
from mcp_standby_proxy.state import BackendState, StateMachine


def _make_lifecycle_config(
    start_command: str = "true",
    start_args: list[str] | None = None,
    stop_command: str = "true",
    healthcheck_type: str = "command",
    healthcheck_command: str = "true",
    timeout: int = 5,
) -> LifecycleConfig:
    return LifecycleConfig(
        start=LifecycleCommandConfig(
            command=start_command,
            args=start_args or [],
            timeout=timeout,
        ),
        stop=LifecycleCommandConfig(command=stop_command, timeout=timeout),
        healthcheck=HealthcheckConfig(
            type=HealthcheckType(healthcheck_type),
            command=healthcheck_command if healthcheck_type == "command" else None,
            url="http://x" if healthcheck_type == "http" else None,
            address="localhost:1" if healthcheck_type == "tcp" else None,
            interval=1,
            max_attempts=1,
            timeout=1,
        ),
    )


def _make_manager(config: LifecycleConfig) -> tuple[LifecycleManager, StateMachine]:
    sm = StateMachine()
    mgr = LifecycleManager(config, sm, "test-server")
    return mgr, sm


async def test_start_success_reaches_healthy() -> None:
    mgr, sm = _make_manager(_make_lifecycle_config())
    async with sm.lock:
        await mgr.start()
    assert sm.state == BackendState.HEALTHY


async def test_start_failing_command_reaches_failed_and_raises() -> None:
    mgr, sm = _make_manager(_make_lifecycle_config(start_command="false"))
    async with sm.lock:
        with pytest.raises(StartError) as exc_info:
            await mgr.start()
    assert sm.state == BackendState.FAILED
    err = exc_info.value
    assert err.exit_code == 1


async def test_start_command_timeout_reaches_failed() -> None:
    mgr, sm = _make_manager(_make_lifecycle_config(start_command="sleep", start_args=["999"], timeout=1))
    async with sm.lock:
        with pytest.raises(StartError) as exc_info:
            await mgr.start()
    assert sm.state == BackendState.FAILED
    assert exc_info.value.exit_code is None
    assert "timed out" in exc_info.value.stderr


async def test_start_command_success_but_healthcheck_failure_reaches_failed() -> None:
    mock_healthcheck = AsyncMock(side_effect=HealthcheckError(attempts=1))

    mgr, sm = _make_manager(_make_lifecycle_config())
    with patch("mcp_standby_proxy.lifecycle.run_healthcheck", mock_healthcheck):
        async with sm.lock:
            with pytest.raises(HealthcheckError):
                await mgr.start()
    assert sm.state == BackendState.FAILED


async def test_stop_success_reaches_cold() -> None:
    mgr, sm = _make_manager(_make_lifecycle_config())
    # Set state to ACTIVE so we can transition to STOPPING -> COLD
    sm._state = BackendState.ACTIVE
    async with sm.lock:
        await mgr.stop()
    assert sm.state == BackendState.COLD


async def test_stop_failing_command_still_reaches_cold() -> None:
    mgr, sm = _make_manager(_make_lifecycle_config(stop_command="false"))
    sm._state = BackendState.ACTIVE
    async with sm.lock:
        await mgr.stop()
    assert sm.state == BackendState.COLD


async def test_start_passes_correct_args_to_command() -> None:
    """Verify that start command receives the configured args."""
    calls: list[tuple[str, list[str]]] = []

    original_exec = asyncio.create_subprocess_exec

    async def _mock_exec(cmd, *args, **kwargs):
        calls.append((cmd, list(args)))
        return await original_exec(cmd, *args, **kwargs)

    config = _make_lifecycle_config(
        start_command="echo",
        start_args=["hello", "world"],
    )
    mgr, sm = _make_manager(config)

    with patch("asyncio.create_subprocess_exec", side_effect=_mock_exec):
        async with sm.lock:
            await mgr.start()

    assert calls[0] == ("echo", ["hello", "world"])


async def test_stop_passes_correct_args_to_command() -> None:
    """Verify that stop command receives the configured args."""
    calls: list[tuple[str, list[str]]] = []

    original_exec = asyncio.create_subprocess_exec

    async def _mock_exec(cmd, *args, **kwargs):
        calls.append((cmd, list(args)))
        return await original_exec(cmd, *args, **kwargs)

    config = LifecycleConfig(
        start=LifecycleCommandConfig(command="true", timeout=5),
        stop=LifecycleCommandConfig(command="echo", args=["stop", "called"], timeout=5),
        healthcheck=HealthcheckConfig(
            type=HealthcheckType.COMMAND,
            command="true",
            interval=1,
            max_attempts=1,
            timeout=1,
        ),
    )
    mgr, sm = _make_manager(config)
    sm._state = BackendState.ACTIVE

    with patch("asyncio.create_subprocess_exec", side_effect=_mock_exec):
        async with sm.lock:
            await mgr.stop()

    assert calls[0] == ("echo", ["stop", "called"])
