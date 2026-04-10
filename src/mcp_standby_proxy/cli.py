import asyncio
from pathlib import Path

import click

from mcp_standby_proxy.config import load_config
from mcp_standby_proxy.proxy import ProxyRunner


@click.group()
def main() -> None:
    """mcp-standby-proxy — lazy-start proxy for MCP servers."""


@main.command()
@click.option(
    "-c",
    "--config",
    required=True,
    type=click.Path(exists=True),
    help="Path to YAML config file.",
)
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="Increase log verbosity. Use -v for INFO, -vv for DEBUG.",
)
def serve(config: str, verbose: int) -> None:
    """Start the proxy in stdio mode."""
    loaded = load_config(Path(config))
    runner = ProxyRunner(loaded, verbose=verbose)
    asyncio.run(runner.run())
