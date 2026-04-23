# Deferred work

## Follow-up: root-cause `Write stream closed` SSE transport error (unblocked by FR-21)

**Status:** deferred — diagnostic infrastructure (FR-21 file logging) is now in place.

### Symptom

`MCP error -32603: Write stream closed:` returned to the MCP client when invoking
`mcp__kroki__generate_diagram` (or any tool that triggers an SSE backend request)
through the proxy. The error is intermittent and was previously undiagnosable because
Claude Code does not persist child-process stderr to disk.

### Origin in source

`src/mcp_standby_proxy/transport/sse.py` lines 71-73:

```python
except (anyio.ClosedResourceError, anyio.EndOfStream) as exc:
    self._connected = False
    raise TransportError(f"Write stream closed: {exc}") from exc
```

This is raised when `self._write_stream.send(SessionMessage(...))` fails with
`anyio.ClosedResourceError` — meaning the in-process memory channel between the
proxy and the MCP SDK's SSE reader loop has already been closed when the proxy
tries to hand off the request.

### Hypotheses to eliminate

**(A) Kroki MCP server closes the HTTP POST body early.**
The SDK's TaskGroup crashes when the server closes the connection mid-stream, which
tears down the memory pipe. The subsequent `send` hits a closed pipe.

**(B) Session timeout between `connect()` and the first `request()` call.**
The SSE session expires on the server side between the proxy's `connect()` (during
healthcheck-pass) and the first actual tool call. Pipe appears open locally but
the remote endpoint is gone.

**(C) Stale read loop from a previous request.**
A prior request's read loop consumed the stream's close event; the write end appears
closed to subsequent sends even though the SSE connection is still up.

### Reproduction steps (once FR-21 is enabled)

1. Set `logging.file.level: debug` in the Kroki config
   (see `examples/kroki.yaml` — the `logging` section is present by default).
2. Restart Claude Code so the proxy picks up the new config.
3. Invoke `mcp__kroki__generate_diagram` from Claude Code.
4. If the error fires, capture the full trace from the log file:
   `tail -n 500 .logs/kroki.log`
5. Identify which hypothesis matches the sequence of log events around the failure.

### Red step

Write a `tests/integration/test_sse_write_stream_closed.py` that reproduces the
exact failure path after the first clean capture identifies which hypothesis is
correct. Follow the project's bugfix methodology: Red → Green → Refactor.
