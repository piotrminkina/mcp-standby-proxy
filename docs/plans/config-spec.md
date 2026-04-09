# Configuration Specification — mcp-standby-proxy

**Status:** APPROVED
**Date:** 2026-04-10
**Related:** [PRD](prd.md) | [Tech Stack](tech-stack.md) | [Tech Spec](tech-spec.md)

---

## 1. Overview

**Format:** YAML
**Loading:** Pydantic v2 model + `pyyaml`. Schema auto-generated from the model (FR-5.2).
**Topology:** One config file per proxy instance, path via `-c` / `--config` CLI arg (FR-5.1).
**Precedence:** Config file only. No env var override layer, no defaults file merging.
**Sensitive data:** Plain text in config. For secrets, set env vars on the proxy process
directly — the proxy does not interpolate `${VAR}` in config values.

## 2. Configuration Schema

```yaml
# mcp-standby-proxy configuration
# One file per MCP server instance

# Required: schema version for forward compatibility
version: 1                                    # int, required, must equal 1

# Proxy identity (reported in MCP initialize response)
server:
  name: "kroki"                               # string, required — server name
  version: "1.0.0"                            # string, optional, default: "0.0.0"
  instructions: "Diagram rendering via Kroki" # string, optional — forwarded to client

# Backend MCP server connection
backend:
  # Transport type
  transport: sse                              # enum: sse | streamable_http | stdio, required

  # For sse / streamable_http: endpoint URL
  url: "http://localhost:5090/sse"            # string, required if transport in (sse, streamable_http)

  # For stdio: command to spawn the MCP server child process
  # This is the MCP server binary, NOT the infrastructure start command
  command: "npx"                              # string, required if transport == stdio
  args: ["@zilliz/claude-context-mcp"]        # list[string], optional, default: []
  env:                                        # dict[string, string], optional, default: {}
    MILVUS_ADDRESS: "localhost:19530"          # extra env vars passed to the child process

# Infrastructure lifecycle (start/stop backend stacks)
lifecycle:
  # Command to start infrastructure
  start:
    command: "systemctl"                      # string, required
    args: ["--user", "start", "kroki.socket"] # list[string], optional, default: []
    timeout: 30                               # int, seconds, optional, default: 30

  # Command to stop infrastructure
  stop:
    command: "systemctl"                             # string, required
    args: ["--user", "stop", "kroki-proxy.service"]  # list[string], optional, default: []
    timeout: 30                                      # int, seconds, optional, default: 30

  # Healthcheck: determines when backend is ready after start
  healthcheck:
    type: http                                # enum: http | tcp | command, required
    # For http: URL that must return 2xx
    url: "http://localhost:5090/sse"           # string, required if type == http
    # For tcp: host:port to connect to
    address: "localhost:5090"                  # string, required if type == tcp
    # For command: shell command that must exit 0
    command: "curl -sf http://localhost/health" # string, required if type == command
    interval: 1                               # int, seconds, optional, default: 2
    max_attempts: 60                          # int, optional, default: 30
    timeout: 5                                # int, seconds per attempt, optional, default: 5

  # Idle timeout: stop backend after N seconds of no forwarded requests
  # 0 = never stop. Ignored in MVP (FR-9 post-MVP).
  idle_timeout: 300                           # int, seconds, optional, default: 300

# Cache file for capability responses
cache:
  path: "./kroki_cache.json"                  # string, required — path to cache JSON file
  # Auto-refresh cache when backend connects (compare live vs cached).
  # Ignored in MVP (FR-10 post-MVP).
  auto_refresh: true                          # bool, optional, default: true
```

## 3. Parameter Reference

### `version`

| Parameter | Type | Default | Required | Validation |
|-----------|------|---------|----------|------------|
| `version` | int | — | yes | Must equal `1`. Reject unknown versions with clear error. |

### `server`

| Parameter | Type | Default | Required | Validation |
|-----------|------|---------|----------|------------|
| `server.name` | string | — | yes | Non-empty. Used in logs and `initialize` response. |
| `server.version` | string | `"0.0.0"` | no | Semantic version string. |
| `server.instructions` | string | `null` | no | Free text. Forwarded to client in `initialize`. |

### `backend`

| Parameter | Type | Default | Required | Validation |
|-----------|------|---------|----------|------------|
| `backend.transport` | enum | — | yes | One of: `sse`, `streamable_http`, `stdio`. |
| `backend.url` | string | — | conditional | Required if transport is `sse` or `streamable_http`. Must be a valid URL. |
| `backend.command` | string | — | conditional | Required if transport is `stdio`. Absolute or PATH-resolvable. |
| `backend.args` | list[string] | `[]` | no | Arguments passed to child process. |
| `backend.env` | dict[str, str] | `{}` | no | Extra env vars for child process. Merged with proxy's env. |

### `lifecycle`

| Parameter | Type | Default | Required | Validation |
|-----------|------|---------|----------|------------|
| `lifecycle.start.command` | string | — | yes | Non-empty. Must be executable. |
| `lifecycle.start.args` | list[string] | `[]` | no | — |
| `lifecycle.start.timeout` | int | `30` | no | Seconds. Range: 1–600. |
| `lifecycle.stop.command` | string | — | yes | Non-empty. Must be executable. |
| `lifecycle.stop.args` | list[string] | `[]` | no | — |
| `lifecycle.stop.timeout` | int | `30` | no | Seconds. Range: 1–600. |
| `lifecycle.healthcheck.type` | enum | — | yes | One of: `http`, `tcp`, `command`. |
| `lifecycle.healthcheck.url` | string | — | conditional | Required if type is `http`. Must be valid URL. |
| `lifecycle.healthcheck.address` | string | — | conditional | Required if type is `tcp`. Format: `host:port`. |
| `lifecycle.healthcheck.command` | string | — | conditional | Required if type is `command`. Non-empty. |
| `lifecycle.healthcheck.interval` | int | `2` | no | Seconds between polls. Range: 1–60. |
| `lifecycle.healthcheck.max_attempts` | int | `30` | no | Range: 1–600. |
| `lifecycle.healthcheck.timeout` | int | `5` | no | Seconds per attempt. Range: 1–60. |
| `lifecycle.idle_timeout` | int | `300` | no | Seconds. 0 = never. Ignored in MVP. |

### `cache`

| Parameter | Type | Default | Required | Validation |
|-----------|------|---------|----------|------------|
| `cache.path` | string | — | yes | File path. Parent directory must exist. |
| `cache.auto_refresh` | bool | `true` | no | Ignored in MVP. |

## 4. Validation Rules

Cross-field constraints enforced at config load time (fail-fast):

1. **Transport → URL:** If `backend.transport` is `sse` or `streamable_http`,
   then `backend.url` is required and must start with `http://` or `https://`.
2. **Transport → command:** If `backend.transport` is `stdio`, then
   `backend.command` is required. `backend.url` is ignored.
3. **Healthcheck type → fields:** If `healthcheck.type` is `http`, then
   `healthcheck.url` is required. If `tcp`, then `healthcheck.address` is
   required. If `command`, then `healthcheck.command` is required.
4. **Start command idempotency:** Not validated by the proxy. Document in error
   messages: "Ensure your start command is idempotent (running it when already
   started is a no-op)." (FR-2.6)
5. **Cache path parent:** `cache.path` parent directory must exist at config
   load time. The cache file itself may not exist (cold bootstrap).

## 5. Configuration Variants

### A. SSE backend (diagram renderer)

```yaml
version: 1
server:
  name: "kroki"
  version: "1.0.0"
backend:
  transport: sse
  url: "http://localhost:5090/sse"
lifecycle:
  start:
    command: "systemctl"
    args: ["--user", "start", "kroki.socket"]
  stop:
    command: "systemctl"
    args: ["--user", "stop", "kroki-proxy.service"]
  healthcheck:
    type: http
    url: "http://localhost:5090/sse"
    interval: 1
    max_attempts: 60
  idle_timeout: 300
cache:
  path: "./kroki_cache.json"
  auto_refresh: true
```

### B. stdio backend + external infrastructure (code indexer with vector DB)

```yaml
version: 1
server:
  name: "claude-context"
  version: "1.0.0"
backend:
  transport: stdio
  command: "npx"
  args: ["@zilliz/claude-context-mcp"]
  env:
    MILVUS_ADDRESS: "localhost:19530"
    EMBEDDING_PROVIDER: "Ollama"
    OLLAMA_HOST: "http://localhost:11434"
    OLLAMA_MODEL: "nomic-embed-text"
lifecycle:
  start:
    command: "systemctl"
    args: ["--user", "start", "milvus.socket"]
  stop:
    command: "systemctl"
    args: ["--user", "stop", "milvus-proxy.service"]
  healthcheck:
    type: http
    url: "http://localhost:9091/healthz"
    interval: 1
    max_attempts: 60
  idle_timeout: 600
cache:
  path: "./claude_context_cache.json"
  auto_refresh: true
```

### C. Streamable HTTP backend (web scraper)

```yaml
version: 1
server:
  name: "firecrawl"
  version: "1.0.0"
backend:
  transport: streamable_http
  url: "http://localhost:5100/mcp"
lifecycle:
  start:
    command: "bash"
    args: ["-c", "cd /path/to/firecrawl && docker compose up -d"]
    timeout: 60
  stop:
    command: "bash"
    args: ["-c", "cd /path/to/firecrawl && docker compose down"]
    timeout: 30
  healthcheck:
    type: http
    url: "http://localhost:5100/mcp"
    interval: 2
    max_attempts: 30
  idle_timeout: 300
cache:
  path: "./firecrawl_cache.json"
  auto_refresh: true
```

## 6. Cache File Format

The proxy manages cache autonomously. Format below is for reference — users
do not create or edit cache files manually.

```json
{
  "cache_version": 1,
  "capabilities": {
    "tools": {"listChanged": true},
    "resources": {},
    "prompts": {"listChanged": true}
  },
  "tools/list": {
    "tools": [
      {
        "name": "generate_diagram",
        "description": "Generate a diagram image",
        "inputSchema": {"type": "object", "properties": {"...": "..."}}
      }
    ]
  },
  "resources/list": {
    "resources": []
  },
  "prompts/list": {
    "prompts": []
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `cache_version` | int | Must match proxy's current version. Mismatch → delete + cold bootstrap. |
| `capabilities` | object | Backend's declared capabilities from `InitializeResult`. |
| `tools/list` | object | Raw `result` payload from backend's `tools/list` response. |
| `resources/list` | object | Raw `result` payload. Present only if backend declares `resources`. |
| `prompts/list` | object | Raw `result` payload. Present only if backend declares `prompts`. |

The proxy does not parse or validate schema contents — pure pass-through.
Each `*/list` key stores exactly the `result` field from the backend's JSON-RPC response.
