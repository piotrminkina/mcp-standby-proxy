import asyncio
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

from mcp_standby_proxy.cache import CacheManager
from mcp_standby_proxy.config import LoadedConfig, LoggingFileConfig
from mcp_standby_proxy.jsonrpc import JsonRpcReader, JsonRpcWriter
from mcp_standby_proxy.lifecycle import LifecycleManager
from mcp_standby_proxy.router import MessageRouter
from mcp_standby_proxy.state import BackendState, StateMachine
from mcp_standby_proxy.transport import BackendTransport, create_transport


class _ServerNameFormatter(logging.Formatter):
    """Log formatter that includes [server_name] in the message."""

    def __init__(self, server_name: str) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)s [%(server_name)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        self._server_name = server_name

    def format(self, record: logging.LogRecord) -> str:
        # Copy the record to avoid mutating the shared LogRecord across handlers.
        # Multiple handlers with different server names would otherwise stomp on
        # each other when the first handler sets record.server_name.
        r = logging.makeLogRecord(record.__dict__)
        r.server_name = self._server_name
        return super().format(r)


def _setup_logging(
    server_name: str,
    verbose: int = 0,
    log_file_config: LoggingFileConfig | None = None,
    resolved_log_path: Path | None = None,
) -> None:
    """Configure root logger with stderr handler and optional file handler.

    Stderr level is controlled by verbose (0=WARNING, 1=INFO, 2+=DEBUG).
    File handler level is independent (log_file_config.level).
    Root logger is set to min(stderr_level, file_level) so records reach
    whichever handler needs them (FR-21.2).

    FR-21.5/FR-21.6: file handler construction failures produce a single
    stderr warning; the file channel is not installed and the proxy continues.
    FR-21.7: stdout handlers are rejected by assertion.
    """
    stderr_level = logging.WARNING
    if verbose == 1:
        stderr_level = logging.INFO
    elif verbose >= 2:
        stderr_level = logging.DEBUG

    formatter = _ServerNameFormatter(server_name)

    stderr_handler = logging.StreamHandler(sys.stderr)
    assert stderr_handler.stream is not sys.stdout, "stderr handler must not write to stdout"
    stderr_handler.setLevel(stderr_level)
    stderr_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.addHandler(stderr_handler)

    effective_root_level = stderr_level

    if log_file_config is not None and resolved_log_path is not None:
        file_level = log_file_config.level.to_logging_level()
        effective_root_level = min(stderr_level, file_level)

        # Set root level early so the startup INFO/WARNING messages below are not
        # suppressed by the default root-level WARNING gate.
        root.setLevel(effective_root_level)

        try:
            resolved_log_path.parent.mkdir(parents=True, exist_ok=True)
            rotating_handler = logging.handlers.RotatingFileHandler(
                filename=resolved_log_path,
                maxBytes=log_file_config.max_size_bytes,
                backupCount=log_file_config.backup_count,
                encoding="utf-8",
            )
            rotating_handler.setLevel(file_level)
            rotating_handler.setFormatter(formatter)
            root.addHandler(rotating_handler)
            # Write directly to stderr, bypassing the logging module entirely,
            # so the path announcement appears regardless of -v/-vv (FR-21.5).
            sys.stderr.write(f"file logging enabled: path={resolved_log_path}\n")
            sys.stderr.flush()
        except Exception as exc:
            # Same direct write so the degradation notice is never filtered (FR-21.6).
            sys.stderr.write(f"file logging disabled: {exc}\n")
            sys.stderr.flush()

    root.setLevel(effective_root_level)

    # FR-21.7 / FR-19.4: scan all active handlers — none may write to stdout.
    for _h in root.handlers:
        if isinstance(_h, logging.StreamHandler) and _h.stream is sys.stdout:
            raise AssertionError(
                f"Handler {_h!r} writes to stdout — violates FR-19.4/FR-21.7. "
                "Stdout is reserved exclusively for JSON-RPC traffic."
            )


class ProxyRunner:
    """Wires all components together and runs the proxy event loop."""

    def __init__(self, loaded: LoadedConfig, verbose: int = 0) -> None:
        self._config = loaded.config
        self._config_dir = loaded.config_dir
        self._resolved_cache_path = loaded.resolved_cache_path
        self._resolved_log_path = loaded.resolved_log_path
        self._verbose = verbose
        self._shutdown_event = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._router: MessageRouter | None = None
        self._sm: StateMachine | None = None

    async def run(self) -> None:
        """Main entry point. Reads messages from stdin until EOF or shutdown."""
        log_file_config = (
            self._config.logging.file if self._config.logging is not None else None
        )
        _setup_logging(
            self._config.server.name,
            self._verbose,
            log_file_config=log_file_config,
            resolved_log_path=self._resolved_log_path,
        )
        logger = logging.getLogger(__name__)
        logger.info("Starting mcp-standby-proxy for '%s'", self._config.server.name)

        # Wire components
        sm = StateMachine()
        self._sm = sm
        cache_manager = CacheManager(self._resolved_cache_path)
        lifecycle_manager = LifecycleManager(
            self._config.lifecycle,
            sm,
            self._config.server.name,
            cwd=self._config_dir,
        )
        writer = JsonRpcWriter(sys.stdout.buffer)

        def _transport_factory() -> BackendTransport:
            return create_transport(self._config.backend, cwd=self._config_dir)

        router = MessageRouter(
            config=self._config,
            state_machine=sm,
            lifecycle_manager=lifecycle_manager,
            cache_manager=cache_manager,
            transport_factory=_transport_factory,
            writer=writer,
        )
        self._router = router

        # Set up stdin reader — 16 MB limit to handle large tool payloads.
        # asyncio.StreamReader's default 64 KB limit triggers LimitOverrunError
        # on tools/call requests with large arguments.
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        rpc_reader = JsonRpcReader(reader)

        # Register signal handlers
        import signal
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._shutdown_event.set)

        # Main message loop
        try:
            while not self._shutdown_event.is_set():
                read_task = asyncio.create_task(rpc_reader.read_message())
                shutdown_task = asyncio.create_task(self._shutdown_event.wait())

                done, pending = await asyncio.wait(
                    {read_task, shutdown_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass

                if shutdown_task in done:
                    logger.info("Shutdown signal received")
                    break

                if read_task in done:
                    message = read_task.result()
                    if message is None:
                        logger.info("stdin EOF, shutting down")
                        break
                    task = asyncio.create_task(router.handle_message(message))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)

        finally:
            await self._cleanup()

    async def _cleanup(self) -> None:
        """Gracefully shut down: wait for in-flight tasks, stop backend."""
        logger = logging.getLogger(__name__)
        logger.info("Shutting down proxy")

        # Wait for in-flight message handlers
        if self._tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks, return_exceptions=True),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.warning("In-flight tasks did not complete within timeout")

        # Close transport and stop backend if needed
        if self._router is not None:
            await self._router.close()

        if self._sm is not None:
            state = self._sm.state

            # STOPPING: wait for lifecycle to reach COLD before exiting
            if state == BackendState.STOPPING:
                try:
                    await self._sm.wait_for(BackendState.COLD, timeout=10.0)
                except asyncio.TimeoutError:
                    logger.warning("Timed out waiting for STOPPING→COLD transition")

            # FAILED: no backend running, nothing to stop
            elif state == BackendState.FAILED:
                pass

            # STARTING / HEALTHY / ACTIVE: stop the backend
            elif state in (BackendState.ACTIVE, BackendState.HEALTHY, BackendState.STARTING):
                from mcp_standby_proxy.lifecycle import LifecycleManager as _LM
                if self._router is not None and hasattr(self._router, "_lifecycle"):
                    lifecycle: _LM = self._router._lifecycle
                    try:
                        async with self._sm.lock:
                            await lifecycle.stop()
                    except Exception as exc:
                        logger.warning("Error during stop: %s", exc)
