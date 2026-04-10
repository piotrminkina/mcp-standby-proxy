import asyncio
import logging
import sys
from pathlib import Path

from mcp_standby_proxy.cache import CacheManager
from mcp_standby_proxy.config import ProxyConfig
from mcp_standby_proxy.jsonrpc import JsonRpcReader, JsonRpcWriter
from mcp_standby_proxy.lifecycle import LifecycleManager
from mcp_standby_proxy.router import MessageRouter
from mcp_standby_proxy.state import BackendState, StateMachine
from mcp_standby_proxy.transport import create_transport


class _ServerNameFormatter(logging.Formatter):
    """Log formatter that includes [server_name] in the message."""

    def __init__(self, server_name: str) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)s [%(server_name)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        self._server_name = server_name

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "server_name"):
            record.server_name = self._server_name  # type: ignore[attr-defined]
        return super().format(record)


def _setup_logging(server_name: str, verbose: int = 0) -> None:
    """Configure root logger to write to stderr."""
    level = logging.WARNING
    if verbose == 1:
        level = logging.INFO
    elif verbose >= 2:
        level = logging.DEBUG

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_ServerNameFormatter(server_name))

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)


class ProxyRunner:
    """Wires all components together and runs the proxy event loop."""

    def __init__(self, config: ProxyConfig, verbose: int = 0) -> None:
        self._config = config
        self._verbose = verbose
        self._shutdown_event = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self._router: MessageRouter | None = None
        self._sm: StateMachine | None = None

    async def run(self) -> None:
        """Main entry point. Reads messages from stdin until EOF or shutdown."""
        _setup_logging(self._config.server.name, self._verbose)
        logger = logging.getLogger(__name__)
        logger.info("Starting mcp-standby-proxy for '%s'", self._config.server.name)

        # Wire components
        sm = StateMachine()
        self._sm = sm
        cache_manager = CacheManager(Path(self._config.cache.path))
        lifecycle_manager = LifecycleManager(self._config.lifecycle, sm, self._config.server.name)
        writer = JsonRpcWriter(sys.stdout.buffer)

        def _transport_factory():
            return create_transport(self._config.backend)

        router = MessageRouter(
            config=self._config,
            state_machine=sm,
            lifecycle_manager=lifecycle_manager,
            cache_manager=cache_manager,
            transport_factory=_transport_factory,
            writer=writer,
        )
        self._router = router

        # Set up stdin reader
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
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
