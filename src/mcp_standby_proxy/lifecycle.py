import asyncio
import logging

from mcp_standby_proxy.config import LifecycleConfig
from mcp_standby_proxy.errors import HealthcheckError, StartError
from mcp_standby_proxy.healthcheck import run_healthcheck
from mcp_standby_proxy.state import BackendState, StateMachine

logger = logging.getLogger(__name__)


async def _run_command(
    command: str,
    args: list[str],
    timeout: int,
    server_name: str,
    label: str,
) -> tuple[int | None, str]:
    """Run command + args, wait up to timeout seconds.

    Returns (exit_code, stderr). exit_code is None on timeout.
    """
    logger.debug("[%s] Running %s command: %s %s", server_name, label, command, args)
    proc = await asyncio.create_subprocess_exec(
        command,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=float(timeout),
        )
        return proc.returncode, stderr.decode(errors="replace")
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning(
            "[%s] %s command timed out after %ds", server_name, label, timeout
        )
        return None, f"{label} command timed out after {timeout}s"


class LifecycleManager:
    """Orchestrates start/stop commands and healthcheck for a backend MCP server.

    All state transitions must be called with state_machine.lock held.
    """

    def __init__(
        self,
        config: LifecycleConfig,
        state_machine: StateMachine,
        server_name: str,
    ) -> None:
        self._config = config
        self._sm = state_machine
        self._server_name = server_name

    async def start(self) -> None:
        """Execute start command and poll healthcheck.

        Transitions: COLD -> STARTING -> HEALTHY (or FAILED on error).
        Must be called with state_machine.lock held.
        Raises StartError or HealthcheckError on failure.
        """
        await self._sm.transition(BackendState.STARTING)
        logger.info("[%s] Starting backend", self._server_name)

        start_cfg = self._config.start
        exit_code, stderr = await _run_command(
            start_cfg.command,
            start_cfg.args,
            start_cfg.timeout,
            self._server_name,
            "start",
        )

        if exit_code != 0:
            await self._sm.transition(BackendState.FAILED)
            raise StartError(exit_code=exit_code, stderr=stderr)

        logger.debug("[%s] Start command succeeded, polling healthcheck", self._server_name)

        try:
            await run_healthcheck(self._config.healthcheck, self._server_name)
        except HealthcheckError:
            await self._sm.transition(BackendState.FAILED)
            raise

        await self._sm.transition(BackendState.HEALTHY)
        logger.info("[%s] Backend is healthy", self._server_name)

    async def stop(self) -> None:
        """Execute stop command.

        Transitions: current -> STOPPING -> COLD.
        Must be called with state_machine.lock held.
        Stop failures are logged but do not prevent COLD transition.
        """
        await self._sm.transition(BackendState.STOPPING)
        logger.info("[%s] Stopping backend", self._server_name)

        stop_cfg = self._config.stop
        exit_code, stderr = await _run_command(
            stop_cfg.command,
            stop_cfg.args,
            stop_cfg.timeout,
            self._server_name,
            "stop",
        )

        if exit_code != 0:
            logger.warning(
                "[%s] Stop command failed (exit=%s): %s",
                self._server_name,
                exit_code,
                stderr,
            )

        await self._sm.transition(BackendState.COLD)
        logger.info("[%s] Backend stopped", self._server_name)
