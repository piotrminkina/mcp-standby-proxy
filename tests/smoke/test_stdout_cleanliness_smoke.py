"""Smoke test: stdout cleanliness with file logging at DEBUG level (FR-19.4, FR-21).

Spawns the real proxy binary against a live FastMCP Streamable HTTP backend,
sends a full MCP lifecycle (initialize -> tools/list -> tools/call with a large
payload), captures every byte of stdout, and asserts each newline-delimited
line parses as valid JSON-RPC. Also verifies the DEBUG log file is written.
"""
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml


def _rpc(method: str, params: dict, id: int) -> bytes:
    msg = {"jsonrpc": "2.0", "method": method, "params": params, "id": id}
    return (json.dumps(msg) + "\n").encode()


def _notify(method: str, params: dict) -> bytes:
    msg = {"jsonrpc": "2.0", "method": method, "params": params}
    return (json.dumps(msg) + "\n").encode()


@pytest.fixture
async def proxy_against_streamable_http(
    streamable_http_server: str,
    tmp_path: Path,
):
    """Spin up the real proxy binary with file logging at DEBUG, pointed at the
    Streamable HTTP fixture server. Yields (stdin_writer, stdout_reader, log_path).
    """
    log_path = tmp_path / "proxy.log"
    cache_path = tmp_path / "cache.json"

    config = {
        "version": 1,
        "server": {"name": "smoke-proxy"},
        "backend": {
            "transport": "streamable_http",
            "url": streamable_http_server,
        },
        "lifecycle": {
            "start": {"command": "true", "timeout": 5},
            "stop": {"command": "true", "timeout": 5},
            "healthcheck": {
                "type": "tcp",
                "address": f"127.0.0.1:{urlparse(streamable_http_server).port}",
                "interval": 1,
                "max_attempts": 10,
                "timeout": 2,
            },
        },
        "cache": {"path": str(cache_path)},
        "logging": {
            "file": {
                "path": str(log_path),
                "level": "debug",
                "max_size": "10MB",
                "backup_count": 3,
            }
        },
    }
    config_path = tmp_path / "proxy.yaml"
    config_path.write_text(yaml.dump(config))

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "mcp_standby_proxy.cli", "serve", "-c", str(config_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=16 * 1024 * 1024,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None

    try:
        yield proc.stdin, proc.stdout, log_path
    finally:
        proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


@pytest.mark.smoke
async def test_stdout_is_clean_json_rpc_with_file_debug(
    proxy_against_streamable_http,
) -> None:
    """FR-19.4: every byte on stdout must be valid JSON-RPC, even with DEBUG file logging.

    Scenario: initialize -> tools/list (cold-start backend) -> tools/call with 100 KB payload.
    All stdout lines must parse as JSON. DEBUG entries must appear in the log file.
    """
    stdin, stdout, log_path = proxy_against_streamable_http

    collected_lines: list[bytes] = []

    async def read_response(expected_id: int, timeout: float = 30.0) -> dict:
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for response id={expected_id}")
            line = await asyncio.wait_for(stdout.readline(), timeout=remaining)
            if not line:
                raise EOFError("Proxy stdout closed unexpectedly")
            collected_lines.append(line)
            msg = json.loads(line.decode())
            if msg.get("id") == expected_id:
                return msg

    # initialize
    stdin.write(_rpc(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "smoke-test", "version": "0.0.1"},
        },
        id=1,
    ))
    await stdin.drain()
    init_resp = await read_response(expected_id=1)
    assert "result" in init_resp, f"initialize failed: {init_resp}"

    stdin.write(_notify("notifications/initialized", {}))
    await stdin.drain()

    # tools/list — triggers cold-start
    stdin.write(_rpc("tools/list", {}, id=2))
    await stdin.drain()
    tools_resp = await read_response(expected_id=2, timeout=30.0)
    assert "result" in tools_resp, f"tools/list failed: {tools_resp}"
    tool_names = [t["name"] for t in tools_resp["result"].get("tools", [])]
    assert "echo" in tool_names, f"'echo' tool not found; got: {tool_names}"

    # tools/call with ~100 KB payload to stress-test no log leakage to stdout
    large_text = "X" * 100_000
    stdin.write(_rpc(
        "tools/call",
        {"name": "echo", "arguments": {"text": large_text}},
        id=3,
    ))
    await stdin.drain()
    call_resp = await read_response(expected_id=3, timeout=30.0)
    assert "result" in call_resp, f"tools/call failed: {call_resp}"

    # Close stdin to trigger graceful shutdown
    stdin.close()

    # Every collected stdout line must be valid JSON-RPC (FR-19.4)
    for i, raw_line in enumerate(collected_lines):
        line_str = raw_line.decode().strip()
        if not line_str:
            continue
        try:
            parsed = json.loads(line_str)
        except json.JSONDecodeError as exc:
            pytest.fail(
                f"Non-JSON line #{i} on stdout (FR-19.4 violation): {line_str!r}\n"
                f"JSON error: {exc}"
            )
        assert "jsonrpc" in parsed, (
            f"Line #{i} is JSON but not JSON-RPC: {line_str!r}"
        )

    # Log file must exist and contain DEBUG entries (FR-21)
    assert log_path.exists(), "Log file was not created"
    log_content = log_path.read_text()
    assert "DEBUG" in log_content, (
        "No DEBUG entries in log file — file logging at debug level not working"
    )
    assert "[smoke-proxy]" in log_content, (
        "Log file missing [smoke-proxy] server name in entries"
    )
