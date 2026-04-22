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
git clone https://github.com/piotrminkina/mcp-standby-proxy.git
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

Reference configs per transport:

- [`examples/kroki.yaml`](examples/kroki.yaml) — **SSE** backend, systemd user service lifecycle
- [`examples/firecrawl.yaml`](examples/firecrawl.yaml) — **Streamable HTTP** backend, Docker Compose lifecycle
- [`examples/claude-context.yaml`](examples/claude-context.yaml) — **stdio** backend with a dependency service (Milvus) managed via `systemctl`

```yaml
version: 1

server:
  name: "my-mcp-server"          # Reported in MCP initialize response
  version: "1.0.0"

backend:
  transport: sse                  # sse | streamable_http | stdio
  url: "http://localhost:5090/sse" # Required for sse / streamable_http

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
  path: "./my_server_cache.json"  # Resolved against the config file's directory
```

### Cold cache bootstrap

On first run (no cache file), the proxy starts the backend, fetches
`tools/list`, `resources/list`, and `prompts/list`, saves the results to the
cache file, and serves future `tools/list` requests from cache without starting
the backend.

### Streamable HTTP transport

For backends that use the newer Streamable HTTP protocol instead of SSE:

```yaml
backend:
  transport: streamable_http
  url: "http://localhost:5100/mcp"
```

### Notes

- `idle_timeout` and `auto_refresh` are accepted in the config but ignored in
  the current version. They are reserved for post-MVP features.

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

All project commands run inside the DevContainer for full isolation from the
host (see [ADR-001](docs/adr/ADR-001-devcontainer-isolation.md)):

```bash
devcontainer up --workspace-folder . --docker-path podman
devcontainer exec --workspace-folder . --docker-path podman uv run pytest
devcontainer exec --workspace-folder . --docker-path podman uv run pytest -m smoke
devcontainer exec --workspace-folder . --docker-path podman uv run ruff check src/ tests/
```

## Project Status

**MVP complete.** All three backend transports (SSE, Streamable HTTP, stdio)
are implemented and tested.

See [PRD](docs/plans/prd.md) for the full capability matrix and requirements,
[Tech Stack](docs/plans/tech-stack.md) for technology choices, and
[Tech Spec](docs/plans/tech-spec.md) for architecture details.

## Roadmap (post-MVP)

- Idle timeout: automatically stop backend after inactivity
- Cache auto-refresh: compare live vs cached on reconnect
- `warm` / `validate` CLI subcommands
- Client capability forwarding + server-to-client request forwarding
- Nuitka binary builds for zero-dependency distribution

## License

Copyright (C) 2026 Piotr Minkina

This program is free software: you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the
Free Software Foundation, either version 3 of the License, or (at your
option) any later version.

This program is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General
Public License for more details.

Full license text: [`LICENSE`](LICENSE) or <https://www.gnu.org/licenses/gpl-3.0.html>.

### Why GPLv3

`mcp-standby-proxy` is a developer tool that sits between an MCP client and
a backend. Both talk to the proxy through arms-length boundaries (stdin/stdout
pipes, HTTP sockets) — under established FSF interpretation, using the proxy
does NOT impose GPL on the MCP client, the backend, or anything else that
merely communicates with it. GPLv3 covers only the proxy itself and any
**derivative works** (forks, embedded copies). The intent is reciprocity: if
you improve the proxy and distribute that improved version, those improvements
stay open for everyone.
