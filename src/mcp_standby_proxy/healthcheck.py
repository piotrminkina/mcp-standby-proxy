import asyncio
import logging

import httpx

from mcp_standby_proxy.config import HealthcheckConfig, HealthcheckType
from mcp_standby_proxy.errors import HealthcheckError

logger = logging.getLogger(__name__)


async def _check_http(url: str, timeout: float) -> bool:
    """Return True if the URL responds with a 2xx status code within timeout."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=timeout)
            return response.is_success
    except Exception:
        return False


async def _check_tcp(address: str, timeout: float) -> bool:
    """Return True if a TCP connection to host:port succeeds within timeout."""
    parts = address.rsplit(":", 1)
    if len(parts) != 2:
        return False
    host, port_str = parts
    try:
        port = int(port_str)
    except ValueError:
        return False
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


async def _check_command(command: str, timeout: float) -> bool:
    """Return True if the shell command exits with code 0 within timeout."""
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            return_code = await asyncio.wait_for(proc.wait(), timeout=timeout)
            return return_code == 0
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False
    except Exception:
        return False


async def run_healthcheck(config: HealthcheckConfig, server_name: str) -> None:
    """Poll healthcheck until success or max_attempts exceeded.

    Raises HealthcheckError if max attempts exceeded.
    Logs each attempt at DEBUG level, success at INFO.
    """
    last_error = ""

    for attempt in range(1, config.max_attempts + 1):
        logger.debug(
            "[%s] Healthcheck attempt %d/%d",
            server_name,
            attempt,
            config.max_attempts,
        )

        success = False
        try:
            if config.type == HealthcheckType.HTTP:
                assert config.url is not None
                success = await _check_http(config.url, float(config.timeout))
            elif config.type == HealthcheckType.TCP:
                assert config.address is not None
                success = await _check_tcp(config.address, float(config.timeout))
            elif config.type == HealthcheckType.COMMAND:
                assert config.command is not None
                success = await _check_command(config.command, float(config.timeout))
        except Exception as exc:
            last_error = str(exc)
            success = False

        if success:
            logger.info(
                "[%s] Healthcheck passed on attempt %d", server_name, attempt
            )
            return

        if attempt < config.max_attempts:
            await asyncio.sleep(float(config.interval))

    raise HealthcheckError(
        attempts=config.max_attempts,
        last_error=last_error or "all attempts failed",
    )
