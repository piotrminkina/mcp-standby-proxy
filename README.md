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

- Python 3.12+ (or [uv](https://docs.astral.sh/uv/) which manages Python versions automatically)
- A running MCP server backend to proxy (e.g., a Docker Compose stack)

### Installation

```bash
git clone <repository-url>
cd mcp-standby-proxy
uv sync
```

### Configuration

Create a YAML config file for each MCP server you want to proxy:

```yaml
version: 1

server:
  name: "my-mcp-server"
  version: "1.0.0"

backend:
  transport: sse
  url: "http://localhost:5090/sse"

lifecycle:
  start:
    command: "systemctl"
    args: ["--user", "start", "my-server.socket"]
  stop:
    command: "systemctl"
    args: ["--user", "stop", "my-server.service"]
  healthcheck:
    type: http
    url: "http://localhost:5090/sse"
    interval: 1
    max_attempts: 60
    timeout: 5

cache:
  path: "./my_server_cache.json"
```

### Running

```bash
uv run mcp-standby-proxy serve -c my-server.yaml
```

Register in your MCP client config:

```json
{
  "mcpServers": {
    "my-server": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/mcp-standby-proxy",
               "mcp-standby-proxy", "serve", "-c", "/path/to/my-server.yaml"]
    }
  }
}
```

## CLI

| Command | Description |
|---------|-------------|
| `serve -c <config.yaml>` | Run the proxy (stdio transport) |
| `-v` / `-vv` | Increase log verbosity |

## Security

Lifecycle commands in the YAML config execute with the proxy process's full
privileges. **Review lifecycle commands before using a third-party config file.**

## Project Status

**Planning complete.** MVP implementation pending.

See [PRD](docs/plans/prd.md) for requirements and scope, [Tech Stack](docs/plans/tech-stack.md)
for technology choices.

## License

Not yet specified.
