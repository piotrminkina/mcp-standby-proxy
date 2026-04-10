# mcp-standby-proxy

Lightweight stdio proxy for MCP (Model Context Protocol) servers that eliminates
unnecessary backend startup when using AI agents.

Each proxy instance sits between an MCP client and a real MCP server backend. It
serves cached tool schemas instantly on startup and only starts the backend
infrastructure when the agent makes a real `tools/call` request. Backend lifecycle
is controlled via configurable shell commands — the proxy is agnostic to the
underlying runtime (containers, service managers, bare processes).

## How it works

```
MCP Client ←stdio→ mcp-standby-proxy ←SSE/HTTP/stdio→ Real MCP Server
                         │
                   cache.json (tools/list response)
```

1. MCP client spawns proxy as a stdio subprocess
2. Client sends `tools/list` → proxy responds from local cache (no backend started)
3. Client sends `tools/call` → proxy starts the backend, waits for healthcheck, forwards the request
4. Subsequent `tools/call` requests are forwarded directly (backend already running)
5. On session end (SIGTERM / stdin EOF) → proxy stops the backend

**Result:** Zero backend processes at session start. Backends start only when needed.

## Getting Started

### Prerequisites

- [uv](https://docs.astral.sh/uv/) (manages Python versions automatically)
- A running MCP server backend to proxy (e.g., a Docker Compose stack)

### Installation

```bash
git clone <repository-url>
cd mcp-standby-proxy
uv sync
```

### Quick Start

1. Copy the example config and edit it for your server:

```bash
cp examples/kroki.yaml my-server.yaml
# Edit my-server.yaml: set server.name, backend.url, and lifecycle commands
```

2. Run the proxy:

```bash
uv run mcp-standby-proxy serve -c my-server.yaml
```

3. Register in your MCP client config (e.g., Claude Desktop):

```json
{
  "mcpServers": {
    "my-server": {
      "command": "uv",
      "args": [
        "run",
        "--project", "/path/to/mcp-standby-proxy",
        "mcp-standby-proxy", "serve",
        "-c", "/path/to/my-server.yaml"
      ]
    }
  }
}
```

## Configuration

See [`examples/kroki.yaml`](examples/kroki.yaml) for a full reference config.

```yaml
version: 1

server:
  name: "my-mcp-server"          # Reported in MCP initialize response
  version: "1.0.0"

backend:
  transport: sse                  # sse | stdio (streamable_http: future)
  url: "http://localhost:5090/sse" # Required for sse transport

lifecycle:
  start:
    command: "systemctl"
    args: ["--user", "start", "my-server.socket"]
    timeout: 30                   # Seconds before start command is killed
  stop:
    command: "systemctl"
    args: ["--user", "stop", "my-server.service"]
    timeout: 30
  healthcheck:
    type: http                    # http | tcp | command
    url: "http://localhost:5090/sse"
    interval: 1                   # Seconds between polls
    max_attempts: 60              # Total attempts before giving up

cache:
  path: "./my_server_cache.json"  # Parent directory must exist
```

### Cold cache bootstrap

On first run (no cache file), the proxy starts the backend, fetches
`tools/list`, `resources/list`, and `prompts/list`, saves the results to the
cache file, and serves future `tools/list` requests from cache without starting
the backend.

### Notes

- `idle_timeout` and `auto_refresh` are accepted in the config but ignored in
  the current MVP. They are reserved for post-MVP features.
- Only `sse` transport is fully implemented in MVP. `stdio` and
  `streamable_http` raise an error at startup.

## CLI

| Command | Description |
|---------|-------------|
| `serve -c <config.yaml>` | Run the proxy (stdio transport) |
| `-v` | INFO logging to stderr |
| `-vv` | DEBUG logging to stderr |

## Security

Lifecycle commands in the YAML config execute with the proxy process's full
privileges. **Review lifecycle commands before using a third-party config file.**

## Development

```bash
uv sync                          # Install dependencies
uv run pytest                    # Run tests
uv run ruff check src/ tests/    # Lint
```

For Nuitka builds (post-MVP), use the Docker Compose `dev` service:

```bash
command docker compose run --rm dev bash
```

## Project Status

**MVP implementation complete.**

See [PRD](docs/plans/prd.md) for requirements, [Tech Stack](docs/plans/tech-stack.md)
for technology choices, and [Tech Spec](docs/plans/tech-spec.md) for architecture details.

## Roadmap (post-MVP)

- `stdio` and `streamable_http` backend transports
- Idle timeout: automatically stop backend after inactivity
- Cache auto-refresh: compare live vs cached on reconnect
- Nuitka binary builds for zero-dependency distribution
- Graceful restart on config change

## License

Not yet specified.
