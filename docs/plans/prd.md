# Product Requirements Document (PRD) — mcp-standby-proxy

**Status:** APPROVED (living document)
**Date:** 2026-04-22
**Related:** [Tech Stack](tech-stack.md) | [Tech Spec](tech-spec.md) | [Config Spec](config-spec.md)

---

## 1. Product Overview

**mcp-standby-proxy** is a lightweight stdio proxy for MCP (Model Context Protocol)
servers that eliminates unnecessary backend startup when using AI agents.

Each proxy instance sits between an MCP client and a single real MCP server backend.
It serves cached tool schemas instantly on startup, and only starts the actual backend
infrastructure when the agent makes a real `tools/call` request. Backend lifecycle
is controlled via configurable shell commands — the proxy is agnostic to the
underlying runtime (containers, service managers, bare processes).

**Distribution:** Personal tool, prepared for open-source publication on GitHub.

## 2. User Problem

### Who experiences it

Developers using MCP clients (AI coding assistants, agent frameworks) with multiple
MCP servers running as heavyweight backend stacks on a development workstation.

### The problem

MCP clients connect to **all** registered MCP servers at startup to fetch `tools/list`.
This triggers all backend stacks to start immediately — even when the user has no
intention of using those tools in the current session.

**Concrete impact (typical multi-server setup):**

| MCP Server type | Processes | RAM usage | Startup time |
|-----------------|----------|-----------|--------------|
| Diagram renderer | 6 | ~300-500MB | 5-30s |
| Vector database + code indexer | 3 | ~500MB-1GB | 30-90s |
| Web scraper | 6+ | ~500MB-1GB | 15-60s |
| **Total (all idle)** | **15+** | **~1.5-3GB** | **30-120s** |

### How they solve it today

- **Socket activation** on ports that backends listen on. But MCP clients trigger
  the activation immediately via `tools/list`, so the problem persists.
- **Manually starting/stopping backends** before sessions. Tedious and error-prone.
- **Not registering servers** in the MCP client config. Loses tool availability entirely.
- **Per-project MCP server registration** — register only needed servers per project via client-level settings. Works but requires per-project configuration maintenance and loses global tool availability.

### How mcp-standby-proxy solves it

| Session start (before) | Session start (after) |
|------------------------|-----------------------|
| 15+ processes, 1.5-3GB RAM, 30-120s delay | 0 backend processes, ~25MB per proxy, instant |
| All backends running regardless of need | Backends start only when agent calls a tool |

## 3. Functional Requirements

### 3.0 Implementation status (as of 2026-04-23)

| FR | Title | Phase | Status |
|----|-------|-------|--------|
| FR-1 | Cached schema serving | MVP | implemented |
| FR-2 | Backend lifecycle management | MVP | implemented |
| FR-3 | Request forwarding | MVP | implemented |
| FR-4 | SSE backend transport | MVP | implemented |
| FR-5 | Configuration | MVP | implemented |
| FR-6 | State machine | MVP | implemented |
| FR-7 | Streamable HTTP backend transport | post-MVP | implemented |
| FR-8 | stdio backend transport | post-MVP | implemented |
| FR-9 | Idle timeout with auto-shutdown | post-MVP | proposed |
| FR-10 | Background schema refresh | post-MVP | proposed |
| FR-11 | Standalone binary build | post-MVP | proposed |
| FR-12 | Config validation CLI | post-MVP | proposed |
| FR-13 | Cache pre-warm CLI | post-MVP phase 1 | proposed |
| FR-14 | Progress notifications during cold start | post-MVP phase 1 | proposed (needs spike) |
| FR-15 | Startup cleanup check | post-MVP phase 1 | proposed |
| FR-16 | Cache age warning | post-MVP phase 1 | proposed |
| FR-17 | Client capability forwarding | post-MVP phase 1 | proposed |
| FR-18 | Server-to-client request forwarding | post-MVP | proposed |
| FR-19 | Stderr logging contract | MVP | implemented |
| FR-20 | MCP protocol version handling | MVP | implemented (pass-through) |
| FR-21 | Optional file logging for agent-mode diagnostics | MVP | implemented |

### FR-1: Cached schema serving (MVP)

The proxy must serve `tools/list` responses from a local JSON cache file without
starting or contacting the backend. The cache file stores the complete JSON-RPC
response body as returned by the real backend.

**Sub-requirements:**
- FR-1.1: On `initialize` request, proxy responds with its own server info and
  capabilities. Capability resolution order:
  - FR-1.1a: Cache exists with non-empty `capabilities` → use cached capabilities.
  - FR-1.1b: Cache missing OR `capabilities` is empty → declare default capabilities
    (`{"tools": {}}`) so MCP clients send `tools/list`, enabling cold bootstrap.
  - FR-1.1c: During cold bootstrap, if the backend's `initialize` response has empty
    capabilities, derive them from the methods successfully fetched (`tools/list`
    present → `{"tools": {}}`, etc.). Store derived capabilities in the cache.
- FR-1.2: On `tools/list` request with cache present, proxy returns cached response.
- FR-1.3: On `tools/list` request with no cache, proxy starts the backend, fetches
  the tool list from the live backend, returns the response to the client immediately,
  and saves the cache to disk asynchronously (cold cache bootstrap).
- FR-1.4: Cache file format is generic — stores capabilities and method-keyed responses
  (`tools/list`, `resources/list`, `prompts/list`) without interpretation.
- FR-1.5: Cache file includes a `cache_version` field (integer). On load: if version
  is missing, lower than current, or higher than current, the cache is treated as
  invalid (deleted, cold bootstrap triggered). This prevents stale cache format from
  causing silent errors after proxy upgrades.

**Cache file format:**
```json
{
  "cache_version": 1,
  "capabilities": {"tools": {"listChanged": true}},
  "tools/list": {"tools": [...]},
  "resources/list": {"resources": [...]},
  "prompts/list": {"prompts": [...]}
}
```

### FR-2: Backend lifecycle management (MVP)

The proxy must manage the backend's lifecycle through configurable shell commands.

**Sub-requirements:**
- FR-2.1: On first `tools/call` (or `tools/list` with no cache), proxy executes the
  configured start command and polls the healthcheck until the backend is ready.
- FR-2.2: Start and stop commands are generic (`command` + `args`), not tied to any
  specific container runtime or service manager.
- FR-2.3: Healthcheck supports three modes: HTTP (GET returns 2xx), TCP (port open),
  command (exits 0).
- FR-2.4: Configurable timeouts: start command timeout, healthcheck interval,
  healthcheck max attempts, healthcheck per-attempt timeout.
- FR-2.5: On SIGTERM **or stdin EOF** (pipe closed by MCP client), proxy gracefully
  stops the backend (executes stop command) if it is currently running, then exits.
- FR-2.6: **Start command idempotency is the user's responsibility.** Running `start`
  when the backend is already active must be a no-op or fast return. The proxy does
  not short-circuit based on its own state — the command runs, and the healthcheck
  decides readiness. Document as a configuration requirement in config-spec.md.
- FR-2.7: **Stop command failures are non-fatal.** If the stop command exits non-zero
  or times out, the proxy logs a warning and transitions to `Cold` anyway. Stop
  idempotency is NOT required from the user — a failing stop leaves the backend in
  an unknown state, and the next start attempt handles cleanup (if healthcheck passes
  immediately, restart is fast; if not, start command runs and the user's idempotency
  contract covers it).

### FR-3: Request forwarding (MVP)

The proxy must forward `tools/call` requests to the live backend and return responses.

**Sub-requirements:**
- FR-3.1: After backend reaches Active state, proxy forwards `tools/call` JSON-RPC
  requests to the backend via the configured transport.
- FR-3.2: Proxy remaps JSON-RPC `id` fields to avoid collisions between client-originated
  and proxy-originated requests (initialize, schema refresh). **When FR-18 is enabled,
  this extends to server-originated IDs — see FR-18.6.**
- FR-3.3: Multiple concurrent `tools/call` requests must be supported (the MCP client
  sends them in parallel).
- FR-3.4: Requests arriving while backend is starting are queued and forwarded once
  the backend becomes active.
- FR-3.5: If backend fails to start or healthcheck times out, queued requests receive
  JSON-RPC error responses with diagnostic information.

### FR-4: SSE backend transport (MVP)

The proxy must communicate with SSE-based MCP backends.

**Sub-requirements:**
- FR-4.1: Connect to SSE endpoint (GET), receive `endpoint` event with POST URL.
- FR-4.2: Perform MCP `initialize` handshake with the backend over SSE.
- FR-4.3: Forward `tools/call` via POST to the SSE message endpoint, receive response.
- FR-4.4: Detect transport disconnection and transition to Failed state. No
  auto-reconnection in v1 (see §4 out of scope). Next client request triggers a
  fresh connect via the normal Failed → Cold → Starting path.

### FR-5: Configuration (MVP)

The proxy must be configured via a YAML file with schema validation.

**Sub-requirements:**
- FR-5.1: One YAML config file per proxy instance, path passed via `-c` / `--config`
  CLI argument.
- FR-5.2: Config schema auto-generated from the internal data model (no manual
  schema maintenance).
- FR-5.3: Config sections: `server` (identity), `backend` (transport + connection),
  `lifecycle` (start/stop commands, healthcheck, idle timeout), `cache` (file path,
  auto-refresh flag). Note: MVP config accepts `idle_timeout` and `auto_refresh`
  fields for forward compatibility, but they are ignored until respective post-MVP
  features are implemented.
- FR-5.4: Transport-specific validation: `url` required for SSE/HTTP, `command`
  required for stdio.
- FR-5.5: Relative `cache.path` is resolved against the config file's parent
  directory. The proxy auto-creates intermediate directories (`mkdir -p`
  equivalent) on first cache write. The parent directory is NOT validated at
  config load time — a typo in the path will surface as a write error when the
  cache is actually saved.

### FR-6: State machine (MVP)

The proxy must implement a deterministic backend lifecycle state machine.

**States:** Cold, Starting, Healthy, Active, Failed, Stopping.

**Sub-requirements:**
- FR-6.1: Cold → Starting: triggered by `tools/call` or `*/list` with no cache.
- FR-6.2: Starting → Healthy: healthcheck passes.
- FR-6.3: Healthy → Active: transport connected, MCP handshake complete.
- FR-6.4: Starting/Healthy → Failed: timeout or error. After cooldown → Cold.
- FR-6.5: Active → Stopping: idle timeout (post-MVP) or SIGTERM.
- FR-6.6: Stopping → Cold: stop command complete.
- FR-6.7: Stopping + `tools/call` received → queue the request. Stop command runs to
  completion. After stop completes (Cold), immediately transition to Starting.
  Queued request is processed via the normal cold-start path.
- FR-6.8: Multiple requests arriving during any transitional state (Starting, Stopping)
  are queued. State transitions are serialized — no concurrent transitions. Queued
  requests are drained on terminal states (Active: forward all, Failed: error all).

### FR-7: Streamable HTTP backend transport (post-MVP)

Support for HTTP Streamable MCP backends.

- FR-7.1: POST JSON-RPC to configured URL.
- FR-7.2: Handle both direct JSON and SSE response modes.
- FR-7.3: Session ID tracking (`Mcp-Session-Id` header).
- FR-7.4: Same disconnect handling as FR-4.4. No auto-reconnection in v1.

### FR-8: stdio backend transport (post-MVP)

Support for stdio-based MCP backends with separate infrastructure lifecycle.

- FR-8.1: Two-phase start: infrastructure (`lifecycle.start`) then child process
  (`backend.command`). Satisfied by construction — `lifecycle.start` + healthcheck
  complete before transport connect, which is where the child process spawns.
- FR-8.2: Manage child process lifecycle: spawn, stdin/stdout pipes, graceful
  shutdown (close stdin → wait → SIGTERM → SIGKILL). Child process is spawned in a
  new session (`start_new_session=True`) so signals are delivered to the whole
  process group.
- FR-8.3: **Shutdown timeouts are inherited from the MCP SDK: 2 seconds
  between `close(stdin)` and SIGTERM, and 2 seconds between SIGTERM and
  SIGKILL.** Not user-configurable. Rationale: the SDK's value is
  MCP-spec-aligned; well-behaved MCP servers exit immediately on stdin close,
  and unresponsive servers should be killed faster rather than slower during
  shutdown. Extending timeouts would require forking the SDK's `stdio_client`
  (its `Process` handle is a generator-frame local and the 2s constant is
  not reachable from outside) — the maintenance cost is not justified by any
  observed backend needing more than 2s to exit gracefully. If this surfaces
  as a problem with a legitimate backend, reopen as a separate ticket.
- FR-8.4: Stdout frames exchanged through the child process pipes. Stderr is
  pass-through to the proxy's stderr — no capture, no prefixing (see FR-19).

### FR-9: Idle timeout with auto-shutdown (post-MVP)

- FR-9.1: Configurable idle timeout per instance (seconds since last `tools/call`).
- FR-9.2: On idle timeout: close transport, execute stop command, transition to Cold.
- FR-9.3: `tools/call` during Stopping → queue the request. Stop command runs to
  completion. After stop completes (Cold), immediately transition to Starting. Queued
  request is processed via the normal cold-start path.

### FR-10: Background schema refresh (post-MVP)

- FR-10.1: After entering Active state (when cache already existed), async fetch
  `tools/list` from live backend.
- FR-10.2: Compare with cached response. If different, update cache file on disk.
- FR-10.3: Send `notifications/tools/list_changed` to client so it re-fetches.
- FR-10.4: Extend to `resources/list`, `prompts/list` if backend declares those
  capabilities.

### FR-11: Standalone binary build (post-MVP)

- FR-11.1: Build pipeline that compiles the proxy to a standalone native binary.
- FR-11.2: Binary has zero runtime dependencies (no interpreter required on host).
- FR-11.3: Fallback: standard package install works identically.

### FR-12: Config validation CLI (post-MVP)

- FR-12.1: `mcp-standby-proxy validate -c config.yaml` subcommand.
- FR-12.2: Validates YAML syntax, required fields, transport-specific constraints.
- FR-12.3: Warns if cache file is missing (proxy will bootstrap it).

### FR-13: Cache pre-warm CLI (post-MVP phase 1)

- FR-13.1: `mcp-standby-proxy warm -c config.yaml` subcommand that starts the
  backend, fetches all capabilities, writes the cache file, stops the backend, and exits.
- FR-13.2: Non-interactive batch command (no stdio proxy loop), meant to be run before
  the first session.
- FR-13.3: Exits 0 on success (cache written), 1 on failure (with diagnostic on stderr).

### FR-14: Progress notifications during cold start (post-MVP phase 1)

Prerequisite: spike to verify MCP client displays `notifications/message`. Drop if not
displayed.

- FR-14.1: While backend is starting and requests are queued, proxy sends periodic
  `notifications/message` to the client with status updates (e.g., "Starting
  backend...", "Healthcheck attempt 3/60...").
- FR-14.2: Keeps the connection alive and gives the user visibility into startup
  progress.

### FR-15: Startup cleanup check (post-MVP phase 1)

- FR-15.1: On startup, before entering the proxy loop, run the healthcheck once with
  a short timeout to detect if the backend is already running (e.g., ghost from a
  previous crashed session).
- FR-15.2: If the backend is already running, proxy transitions directly to Healthy
  (skip start command, connect transport immediately).
- FR-15.3: Log a warning: "Backend already running — reusing existing instance."
- FR-15.4: Proxy tracks whether it started the backend (`_proxy_started_backend`
  flag). On shutdown, execute stop command only if the proxy started the backend.

### FR-16: Cache age warning (post-MVP phase 1)

- FR-16.1: On startup, if cache file exists and is older than a configurable threshold
  (default: 7 days), log a warning to stderr: "Cache file is N days old. Consider
  running `mcp-standby-proxy warm` to refresh."
- FR-16.2: Informational only — proxy still serves from cache.

### FR-17: Client capability forwarding (post-MVP phase 1)

The proxy must forward client capabilities to the backend so the backend knows
what the client supports.

**Context:** MCP clients (e.g., Claude Code) declare capabilities in their
`initialize` request (`roots`, `sampling`, `elicitation`). Backends use these to
decide which server-to-client requests to send (e.g., `roots/list` if the client
declared `roots`). Currently the proxy sends `"capabilities": {}` to the backend,
causing backends to log warnings and skip features that require client support.

- FR-17.1: On `initialize` from the client, store `params.capabilities` and
  `params.clientInfo` on the router for later use.
- FR-17.2: When connecting to the backend (`_connect_backend` / `_do_start`),
  forward the stored client capabilities in the `initialize` request to the
  backend. If no client has initialized yet (cold bootstrap triggered by cache
  miss), use `"capabilities": {}` as fallback.
- FR-17.3: Forward `clientInfo` from the real client, optionally augmented with
  proxy metadata (e.g., `"name": "mcp-standby-proxy (Claude Code)"`).

### FR-18: Server-to-client request forwarding (post-MVP)

The proxy must support bidirectional message forwarding: backend-initiated
requests relayed to the client, client responses relayed back to the backend.

**Context:** MCP backends can send requests to the client (e.g., `roots/list`,
`sampling/createMessage`). This requires a persistent read loop on the backend
transport that listens for incoming messages beyond request-response pairs.
Currently the proxy's `BackendTransport.request()` discards any message whose
`id` doesn't match the pending request — server-to-client requests are silently
lost.

- FR-18.1: Add a background reader task on the backend transport that receives
  all incoming messages (responses, requests, notifications).
- FR-18.2: Incoming backend responses are routed to pending `request()` calls
  (existing behavior, refactored from inline read loop).
- FR-18.3: Incoming backend requests (messages with `method` and `id` but
  originated by the server) are forwarded to the client via stdout with
  proxy-remapped IDs.
- FR-18.4: Client responses to server-originated requests are forwarded back
  to the backend via the transport.
- FR-18.5: Incoming backend notifications are forwarded to the client via stdout.
- FR-18.6: ID remapping must prevent collisions between **three** ID spaces:
  client-originated, proxy-originated (initialize, schema refresh), and
  server-originated. Extends FR-3.2.

### FR-19: Stderr logging contract (MVP)

The proxy must log operational events to stderr in a human-readable
line-oriented format. Whether those lines are visible to the user depends
on the process that spawned the proxy (see FR-19.1).

- FR-19.1: Stderr is the **default** log output and is always active. Its
  visibility depends on the process that spawns the proxy — when the proxy
  runs as an MCP stdio subprocess (primary deployment), stderr is captured
  by the parent and may or may not be exposed by it. Claude Desktop
  persists it to `~/Library/Logs/Claude/mcp*.log` (macOS) /
  `%APPDATA%\Claude\logs\mcp*.log` (Windows). Claude Code does not
  persist it (anthropics/claude-code#29035, closed as "not planned" on
  2026-03-26). For deployments where stderr is not user-accessible,
  enable FR-21 file logging. No network syslog and no structured
  telemetry export in v1.
- FR-19.2: Log format: `timestamp level [server_name] message`. Timestamp in
  ISO-8601 local time. Server name taken from `server.name` in config.
- FR-19.3: Log levels: WARNING (default), INFO (`-v`), DEBUG (`-vv`).
- FR-19.4: Client-side transport (stdin/stdout) is JSON-RPC only — **no log
  output may ever appear on stdout**. Violating this would corrupt the JSON-RPC
  stream to the MCP client. This is a hard invariant enforced by the logger
  configuration.
- FR-19.5: For stdio backend transport, the child process's stderr is
  pass-through to the proxy's stderr (no capture, no prefixing). Users get
  interleaved proxy + backend logs on the same stream.
- FR-19.6: Coexistence with FR-21 file logging: when enabled, the file
  channel is additive — stderr output remains unchanged. Both channels share
  the same formatter (FR-19.2). FR-19.4 (no stdout contamination) applies to
  both channels by construction — neither may write to stdout.

### FR-20: MCP protocol version handling (MVP)

The proxy is a thin pass-through — it does not enforce MCP protocol version
compatibility between client and backend. The version negotiation happens
directly between the two via `initialize`; the proxy only participates in its
own `initialize` response to the client.

- FR-20.1: On `initialize` from the client, proxy echoes the client's
  `protocolVersion` field in the `result.protocolVersion`, or substitutes the
  proxy's internally known version if the client didn't declare one.
- FR-20.2: When initializing the backend connection, the proxy uses its
  internally known `protocolVersion`. If the backend responds with a different
  version, the proxy logs a warning but does not fail — the backend's version
  is what actually serves `tools/call`, so downstream behaviour is the backend's
  responsibility.
- FR-20.3: If the client's declared version is incompatible with the backend's,
  the proxy does NOT block the session — tool calls may fail, but the failure
  mode is the backend's (returning errors) not the proxy's. Rationale: proxy
  transparency; version enforcement belongs in the client or the backend, not
  in a relay.
- FR-20.4: Proxy version advertised in its own `serverInfo` is the MCP SDK's
  current protocol constant. Updated only when the SDK is upgraded.

### FR-21: Optional file logging for agent-mode diagnostics (MVP)

The proxy must support optional, opt-in logging to a local file, in addition to
the always-on stderr channel (FR-19).

**Context:** When the proxy is spawned as a subprocess by an MCP client that
does not persist child-process stderr (notably Claude Code — per
anthropics/claude-code#29035), operational events are effectively invisible
to both the user and any AI agent performing diagnostics. Incidents such as
`Write stream closed` from the backend transport cannot be investigated
without running the proxy manually outside of the agent. FR-21 provides a
local plain-text log file as a secondary diagnostic channel without
introducing telemetry infrastructure.

**Sub-requirements:**

- FR-21.1: File logging is **disabled by default**. Activation requires a
  `logging.file` section in the YAML config. Its absence is the supported way
  to run without file logging — no CLI flag, no environment variable.
- FR-21.2: When enabled, the stderr channel and the file channel have
  **independent per-handler level thresholds**: stderr level is controlled
  by `-v`/`-vv` (FR-19.3), file level by `logging.file.level`. The root
  logger is set to the most permissive level required by any active
  handler (`min(stderr_level, file_level)`) so that records destined for
  the file handler are not filtered out by the root logger before dispatch.
  Each handler then applies its own level filter. Users can run stderr at
  WARNING while capturing DEBUG to the file.
- FR-21.3: Each record's header line follows FR-19.2
  (`timestamp level [server_name] message`). Exception tracebacks produced
  by `logger.exception` are appended as additional lines below the header,
  formatted by the stdlib `Formatter.formatException` default — multi-line
  plain text, no JSON. Both channels receive the same rendering.
- FR-21.4: The proxy must rotate log files to prevent unbounded growth. The
  rotation policy is size-based with a configurable maximum file size and a
  configurable number of retained backups (backup count ≥ 1 — rotation
  history is required for the size cap to be enforceable; see config-spec
  for the rationale). Rotation defaults are chosen so that a typical DEBUG
  session (including base64-encoded tool payloads) fits within the
  retained history without manual intervention. The effective in-file cap
  is `max_size + size of the largest single record` because the stdlib
  rotating handler checks size *after* writing each record.
- FR-21.5: Relative `logging.file.path` is resolved against the config file's
  parent directory (same rule as `cache.path`, FR-5.5). The proxy auto-creates
  intermediate directories **on startup, before constructing the file
  handler** — `RotatingFileHandler` opens the file at construction time and
  does not create parents itself. Path validation is deferred in the sense
  that a typo surfaces only when the proxy actually tries to open the file
  at startup: a missing-parent or permission error becomes a
  logged-to-stderr warning, the file channel is disabled for the process
  lifetime, and the proxy continues with stderr-only logging. To give the
  user immediate feedback, the proxy emits an INFO line on stderr at
  startup indicating the resolved log file path (e.g.,
  `file logging enabled: path=/abs/path/to/kroki.log`).
- FR-21.6: File logging failures must never crash the proxy or interrupt the
  stdin/stdout JSON-RPC loop.
  - **Startup (handler construction) failures** — permission denied,
    missing parent after mkdir attempt, read-only filesystem — produce a
    single warning on stderr (`file logging disabled: <reason>`) and the
    file channel is not installed. Stderr logging continues normally.
  - **Runtime I/O failures** — disk full, file deleted externally, inode
    changed — are handled by the stdlib `Handler.handleError` (writes a
    one-line notice to stderr and continues). The proxy does not retry,
    re-open, or crash.
- FR-21.7: FR-19.4 (no stdout contamination) applies unchanged. Enforced at
  setup time by an assertion that rejects any handler whose stream is
  `sys.stdout`.
- FR-21.8: The file channel is **plain text** — human-readable lines only.
  No JSON logs, no structured fields, no export format. Anything beyond a
  plain log file (Prometheus, OpenTelemetry, JSON lines) remains out of
  scope per §4.
- FR-21.9: Child-process stderr from stdio backend transport (FR-19.5) is
  **not** routed through the file channel. The child's stderr is
  pass-through to the proxy's fd 2 at the OS level and never enters
  Python's `logging` dispatch. Users debugging a stdio backend must
  collect the backend's stderr separately (e.g., redirect at the backend
  entry point).

## 4. Project Scope Boundaries

### In scope (MVP)

- Single proxy instance per MCP server (not a multiplexer)
- stdio transport on the client side (the MCP client spawns the proxy)
- SSE transport on the backend side
- JSON cache file for `*/list` responses
- Cold cache bootstrap
- Backend lifecycle: start/stop via configurable shell commands
- HTTP/TCP/command healthcheck
- SIGTERM graceful shutdown
- stdin EOF as shutdown signal
- Config with auto-generated schema
- Stderr logging contract (FR-19)
- MCP protocol version pass-through (FR-20)
- Optional file logging for agent-mode diagnostics (FR-21)

### In scope (post-MVP phase 1)

Quality-of-life improvements. Build after MVP is proven in real sessions.

- Client capability forwarding — FR-17
- Cache pre-warm CLI (`warm` subcommand) — FR-13
- Startup cleanup / orphan backend detection — FR-15
- Cache age warning — FR-16
- Progress notifications during cold start — FR-14 (requires spike)

### In scope (post-MVP)

- Streamable HTTP backend transport — FR-7 *(implemented)*
- stdio backend transport (child process + separate infrastructure) — FR-8 *(implemented)*
- Idle timeout with auto-shutdown — FR-9
- Background schema refresh + `notifications/*/list_changed` — FR-10
- Generic capability caching (`resources/list`, `prompts/list`) — FR-1.4
- Server-to-client request forwarding (bidirectional proxy) — FR-18
- Standalone binary distribution — FR-11
- `validate` CLI subcommand — FR-12

### Out of scope

Permanent non-goals (not "post-MVP"; not on the roadmap):

- Multiplexing multiple backends behind one proxy.
- HTTP/SSE server on the client side (proxy is stdio-only).
- Authentication / authorization (config is trusted input).
- Config hot-reload (restart proxy to pick up changes).
- **JSON-RPC batch support.** MCP clients (Claude Code, Cursor, etc.) do not
  emit batched requests, and the MCP SDK does not generate them. YAGNI.
- Standalone process management with PID files.
- GUI or web dashboard.
- Metrics / telemetry export (Prometheus, OpenTelemetry, structured JSON
  logs, network syslog). Note: the opt-in plain-text file logger defined in
  FR-21 is **not** telemetry export — it is a local, line-oriented log file
  intended for human reading, produced with the same formatter as stderr.
  Anything that requires a client library, a remote endpoint, or a parseable
  schema is out of scope.
- **Auto-reconnection on transport flaps.** The current Failed → cooldown → Cold
  → restart-on-next-call pattern covers the common case; MCP clients retry
  failed requests, so a brief disconnect manifests as one extra round-trip. If
  production usage proves this insufficient, promote to a post-MVP feature then.

## 5. User Stories

### MVP

**US-001: Instant tool availability on session start**

As a developer starting an MCP client session, I want to see all MCP tools available
immediately so that I can start working without waiting for backends to start.

Acceptance criteria:
- MCP client spawns proxy as stdio subprocess.
- Proxy responds to `initialize` within 100ms.
- Proxy responds to `tools/list` from cache within 50ms.
- Zero backend containers are started.
- Agent sees all cached tools as available.

---

**US-002: On-demand backend start on first tool call**

As a developer using an MCP tool for the first time in a session, I want the backend
to start automatically so that I don't have to manually manage backend processes.

Acceptance criteria:
- Agent sends `tools/call` (e.g., `generate_diagram`).
- Proxy executes the configured start command.
- Proxy polls healthcheck until backend is ready.
- Proxy connects to backend via SSE, performs MCP handshake.
- Proxy forwards the `tools/call` and returns the response.
- Total time from `tools/call` to response = backend startup time + backend processing
  time + <100ms proxy overhead.

---

**US-003: Cold cache bootstrap**

As a developer running the proxy for the first time (no cache file exists), I want
the proxy to automatically build its cache so that I don't need any manual setup step.

Acceptance criteria:
- Proxy starts with no cache file present.
- On first `tools/list` from the MCP client, proxy starts backend, fetches tool list.
- Response is returned to the MCP client immediately (no extra round-trip).
- Cache file is written to disk asynchronously.
- On next session start, proxy serves from cache (no backend start needed).

---

**US-004: Queued requests during backend startup**

As a developer whose agent sends multiple tool calls rapidly, I want them all to
succeed even if the backend is still starting.

Acceptance criteria:
- Multiple `tools/call` requests arrive while backend is in Starting state.
- All requests are queued (not rejected).
- Once backend reaches Active state, all queued requests are forwarded.
- Each request receives its correct response (ID mapping preserved).

---

**US-005: Backend start failure**

As a developer whose backend fails to start, I want a clear error message so that
I can diagnose the problem.

Acceptance criteria:
- Start command exits non-zero → proxy returns JSON-RPC error with exit code and stderr.
- Healthcheck exceeds max attempts → proxy returns JSON-RPC error with timeout message.
- All queued requests receive the error response.
- After 10-second cooldown, next `tools/call` triggers a fresh start attempt.
- Proxy remains alive and responsive (does not crash).

---

**US-006: Graceful shutdown on SIGTERM**

As a developer closing an MCP client session, I want the proxy to stop the backend
so that containers don't remain running after the session ends.

Acceptance criteria:
- Proxy receives SIGTERM.
- If backend is Active: close transport connection, execute stop command.
- If backend is Cold: exit immediately.
- Proxy exits with code 0 after cleanup.
- Proxy also exits gracefully when stdin reaches EOF (pipe closed by MCP client).

---

**US-007: Configuration via YAML file**

As a developer setting up the proxy, I want a simple YAML config file so that I can
configure the backend connection and lifecycle commands.

Acceptance criteria:
- Config file is passed via `-c` / `--config` CLI argument.
- Invalid config (missing required fields, wrong types) → clear error message on
  stderr with field path.
- Config schema can be derived from the internal data model programmatically.
- Example configs for SSE, Streamable HTTP, and stdio backends are provided under
  `examples/` in the repo.

---

**US-008: MCP client integration**

As a developer registering the proxy in the MCP client, I want a simple MCP server
entry that replaces the direct backend entry.

Acceptance criteria:
- MCP client config entry points at the proxy binary with `serve -c <config path>`
  (exact syntax documented in `README.md` and `examples/*.yaml`).
- All tools previously available via direct backend connection remain available.
- Tool call results are identical to direct connection (pass-through, no
  transformation).

---

**US-020: Diagnose a proxy incident in agent-mode deployment**

As a developer running the proxy as an MCP subprocess under an agent runtime
that does not persist child-process stderr (e.g., Claude Code), I want to
enable a local debug log file so that I can diagnose transport errors or
lifecycle failures without restarting the proxy outside the agent.

Acceptance criteria:
- Adding a `logging.file` section to the config and restarting the agent is the
  only activation step (no CLI flags, no env vars).
- After the next incident reproduction with `level: debug`, the configured log
  file contains DEBUG entries covering the request path, backend transport
  calls, and any exception tracebacks.
- Running without a `logging.file` section produces identical behavior to
  before FR-21 (stderr-only; no file created, no disk I/O).
- Stderr level remains under control of `-v`/`-vv` regardless of
  `logging.file.level` — the two channels filter independently (FR-21.2).
- The file has bounded size — once `max_size` is exceeded, rotation produces
  numbered backups and oldest beyond `backup_count` are evicted.
- stdout remains pure JSON-RPC throughout (verifiable by
  `jq -c . < stdout-capture` over any capture taken with file logging
  enabled at DEBUG).
- A misconfigured path (unwritable directory, permission denied, non-existent
  parent that cannot be mkdir'd) produces a single stderr warning and does
  not prevent the proxy from running; stderr-only logging continues.
- A resolved log file path is announced on stderr at startup (INFO line) so
  the user sees where logs actually land.

---

### Post-MVP

**US-009: Cache pre-warm**

As a developer setting up the proxy for the first time, I want to pre-build the cache
before my first session so that the first session starts instantly.

Acceptance criteria:
- `mcp-standby-proxy warm -c config.yaml` starts the backend, fetches tool schemas,
  writes cache, stops backend.
- Command exits 0 on success with a message indicating cache path and tool count.
- Command exits 1 on failure with diagnostic information.
- After warm, next `mcp-standby-proxy serve` session serves tools instantly from cache.

---

**US-010: Orphan backend detection**

As a developer whose previous session crashed, I want the proxy to detect and reuse
the still-running backend instead of failing or starting a duplicate.

Acceptance criteria:
- Proxy starts, runs healthcheck, detects backend is already running.
- Proxy skips start command, connects to existing backend directly.
- Log message indicates "Backend already running — reusing existing instance."
- Normal operation continues (tools/call forwarding works).

---

**US-011: Idle auto-shutdown**

As a developer who used a tool earlier in the session but no longer needs it, I want
the backend to stop automatically after a configurable period of inactivity.

Acceptance criteria:
- No `tools/call` for `idle_timeout` seconds → proxy executes stop command.
- Next `tools/call` after shutdown triggers a clean restart (Cold → Starting → Active).
- `tools/call` during Stopping is queued. Stop completes, then backend restarts.
  Queued request is forwarded after Active.

---

**US-012: Background schema refresh**

As a developer whose backend tools changed (updated container image), I want the
proxy to detect the change and notify the MCP client.

Acceptance criteria:
- After entering Active state, proxy fetches `tools/list` from live backend.
- If response differs from cache, cache file is updated on disk.
- Proxy sends `notifications/tools/list_changed` to the MCP client.
- MCP client re-fetches `tools/list` and sees updated tools.

---

**US-013: Manual cache invalidation**

As a developer who wants to force a cache refresh, I want a simple way to reset
the cache.

Acceptance criteria:
- Delete cache file + restart proxy.
- Proxy detects missing cache → cold bootstrap on next `tools/list`.
- New cache reflects current backend tool list.

---

**US-014: Streamable HTTP backend**

As a developer with an HTTP Streamable MCP server, I want the proxy to support
this transport.

Acceptance criteria:
- Proxy connects to the backend's HTTP Streamable endpoint.
- `tools/call` forwarded via POST, response received (JSON or SSE mode).
- Session ID tracked across requests.

---

**US-015: stdio backend with separate infrastructure**

As a developer whose MCP server is a stdio process that depends on external
infrastructure (e.g., a database stack), I want the proxy to manage both the
infrastructure lifecycle and the MCP server process.

Acceptance criteria:
- Proxy starts infrastructure (lifecycle.start), waits for healthcheck.
- Proxy spawns the MCP server child process (backend.command) after infrastructure
  is ready.
- On shutdown: closes child process first (close stdin → wait 2s → SIGTERM → wait
  2s → SIGKILL), then stops infrastructure.

---

**US-016: Standalone binary**

As a developer deploying the proxy, I want a single binary with no runtime
dependencies.

Acceptance criteria:
- `mcp-standby-proxy` binary runs without an interpreter installed on the host.
- Binary size < 15MB.
- Functionality identical to `uv run mcp-standby-proxy`.

---

**US-017: Config validation CLI**

As a developer creating a new config, I want to validate it before running the proxy.

Acceptance criteria:
- `mcp-standby-proxy validate -c config.yaml` exits 0 if valid, 1 if errors.
- Error messages include field path and expected type/value.
- Warning if cache file does not exist (not an error — proxy will bootstrap it).

---

**US-018: Client capabilities forwarded to backend**

As a developer using an MCP backend that supports server-to-client features
(e.g., `roots/list`, `sampling/createMessage`), I want the proxy to forward my
MCP client's capabilities to the backend so that the backend knows what features
are available and doesn't log warnings about missing capabilities.

Acceptance criteria:
- MCP client sends `initialize` with `capabilities: {roots: {listChanged: true}}`.
- Proxy stores client capabilities.
- When proxy connects to backend, `initialize` includes the stored client capabilities.
- Backend does not log "could not infer client capabilities" warnings.
- If backend is started before any client initializes (cold bootstrap), proxy sends
  `"capabilities": {}` as fallback (no regression).

---

**US-019: Server-to-client request forwarding**

As a developer using an MCP backend that needs to query the client (e.g.,
`roots/list` to discover workspace roots), I want the proxy to relay these
requests to my MCP client and return the client's response to the backend.

Acceptance criteria:
- Backend sends `roots/list` request to proxy.
- Proxy forwards request to MCP client via stdout (with remapped ID).
- MCP client responds on stdin.
- Proxy relays response back to the backend.
- Round-trip latency < 100ms proxy overhead.
- Server-originated notifications from backend are forwarded to client.

## 6. Success Metrics

### 6.1 MVP metrics

| Metric | Target | How to measure |
|--------|--------|----------------|
| Backend processes at session start | 0 | Process listing before first `tools/call` |
| Proxy startup time | < 200ms | Time from spawn to `initialize` response |
| `tools/list` response time (cache hit) | < 50ms | Measure in MCP client logs |
| Proxy memory usage (idle) | < 30MB RSS | Process monitor |
| Proxy routing latency (Active state) | < 100ms | Compare `tools/call` round-trip through proxy vs. direct connection to same backend |
| RAM savings vs direct connection | > 90% | ~25MB proxy vs ~300-500MB typical backend stack when idle |
| Backend stop on session end | 100% | Verify no backend processes after MCP client exit (SIGTERM path) |
| stdout contamination | 0 bytes of non-JSON-RPC | `stdout | jq -c .` must never fail on a line (FR-19.4) |

### 6.2 Post-MVP metrics

Targets to verify when each respective feature ships.

| Feature | Metric | Target |
|---------|--------|--------|
| FR-9 Idle timeout | Time from last `tools/call` to Cold | `idle_timeout + stop_duration ± 2s` |
| FR-10 Schema refresh | `list_changed` false-positive rate | 0 (only fire when cache actually differs) |
| FR-10 Schema refresh | Notification latency after backend change | < 5s from Active transition |
| FR-13 Warm CLI | End-to-end time for typical backend | < 60s cold, < 10s warm |
| FR-15 Orphan detection | Detection accuracy | 100% (no duplicate spawn when backend alive) |
| FR-17 Capability forwarding | Backend warning rate for missing capabilities | 0 when client declared capabilities |
| FR-18 Bidi forwarding | Server→client→server round-trip overhead | < 100ms proxy-added latency |
| FR-8.3 Shutdown timeouts | stdio backend shutdown duration on cooperating backend | < 2s (SIGTERM path not reached) |
| FR-8.3 Shutdown timeouts | stdio backend shutdown duration on unresponsive backend | < 5s (2s + 2s + kill) |

## 7. Risks & Challenges

| Risk | Severity | Likelihood | Mitigation |
|------|----------|------------|------------|
| MCP SDK too opinionated for proxy pattern | Medium | Low | Hybrid approach: raw stdio client-side, SDK transports backend-side. Fallback to raw HTTP client if SDK transport is too rigid. |
| Binary compilation breaks on specific dependency | Low | Low | Fallback to standard package distribution. Post-MVP concern. |
| MCP protocol evolution breaks proxy | Low | Low | Proxy is thin pass-through (FR-20) — protocol changes affect transport layer only. SDK tracks protocol changes. |
| MCP clients change startup behavior (stop probing `tools/list`) | Low | Very Low | Proxy is still useful as resource manager (idle shutdown). Monitor MCP ecosystem. |
| SSE reconnection edge cases (network flaps, timeouts) | Medium | Medium | Auto-reconnect explicitly out of scope v1 (see §4); rely on MCP client retry behaviour. Revisit if production usage proves insufficient. |
| Concurrent `tools/call` race conditions in state machine | Medium | Medium | Careful async locking. State transitions protected by async lock primitives. Comprehensive integration tests. |
| MCP client timeouts on `tools/list` during cold bootstrap | High | High (first run) | FR-13 warm CLI pre-builds cache. FR-14 progress notifications keep connection alive. Document client-side timeout configuration. |
| Proxy receives SIGKILL (client crash) — backend left running | Medium | Medium | FR-15 startup cleanup detects and reuses orphan backends. Idle timeout (post-MVP) limits orphan lifetime. |
| Cache format incompatibility after proxy upgrade | Low | Low | FR-1.5 cache versioning. Invalid cache is deleted and rebuilt via cold bootstrap. |
| Backend features degraded due to missing client capabilities | Medium | High | Proxy sends empty capabilities, backend cannot use `roots/list`, `sampling/createMessage`. FR-17 fixes this. FR-18 adds full bidirectional support. |
| Server-to-client requests silently dropped | Medium | Medium | Transport read loop only matches response IDs, discards server-initiated messages. FR-18 introduces background reader task and forwarding. |
| stdio backend with long GC pause on SIGTERM | Low | Low | FR-8.3 2s+2s (SDK default) may SIGKILL a legitimately-slow backend. Reopen and consider a fork of the SDK's `stdio_client` if this surfaces in practice. |
| Stdout contamination corrupts JSON-RPC stream | High | Low | FR-19.4 hard invariant: stderr-only logging. Enforced by log handler configuration; regression risk on new code paths. |
| Incident invisible under agent-mode deployment (stderr captured by parent) | High | High (primary use case under Claude Code) | FR-21 file logging **available** for opt-in. All shipped `examples/*.yaml` include an active `logging` section by default; users who copy an example get file logging out of the box, and can remove the section to disable it. README "File logging" section documents the behavior. Residual risk for users who write configs from scratch without consulting examples — accepted trade-off vs. making file logging default-on in the schema (which would surprise users with unexpected disk I/O). |
| File log grows unbounded during DEBUG with large payloads (e.g., Kroki base64 PNGs) | Medium | Medium | FR-21.4 size-based rotation with configurable retention; `backup_count ≥ 1` required (stdlib `RotatingFileHandler` ignores `max_size` when `backupCount=0`). Defaults tuned for a typical DEBUG session to fit in retained history. |
| Multiple proxy instances writing to the same `logging.file.path` (two Claude Code windows, same config) interleave or corrupt lines | Medium | Low | No enforcement in v1. Each proxy uses single-process `RotatingFileHandler`, which is thread-safe but not multi-process-safe. Users running concurrent sessions on the same config should set different `logging.file.path` values. Post-MVP candidate: `ConcurrentRotatingFileHandler` from `concurrent-log-handler`. |
| Exception tracebacks leak sensitive data (API tokens, credentials) from local variables or param dicts into the log file | Medium | Low | Python's default `Formatter.formatException` does not include local variable reprs — only the stack frames and exception message. Risk is confined to exception *messages* that include sensitive values (e.g., a ConfigError echoing `backend.env`). Mitigation: avoid embedding secret values in exception messages (code-review concern). Not enforced by schema. |
| Sync file I/O on the event loop adds per-record latency under DEBUG with large payloads, regressing the `proxy routing latency < 100ms` success metric | Medium | Medium | MVP accepts sync `FileHandler` / `RotatingFileHandler`. If real measurements show regression, post-MVP migration to `QueueHandler` + listener thread is the planned path. Tracked in tech-spec §2. |
| Failure loops (backend start/fail/retry) produce continuous DEBUG logs with full tracebacks, evicting useful historical data from rotation backups before the user notices | Low | Medium | Accepted. The same rotation policy that caps disk usage also caps retention during outages. Users can raise `backup_count` temporarily during an active incident. |
| **OSS publication — unsolicited issue triage load** | Low | Medium | Document expectations in README (personal tool, best-effort support). Add issue templates. |
| **OSS publication — supply chain via lifecycle.command** | Medium | Low | Config is trusted input (§8). Warn users in README that YAML files downloaded from third parties can execute arbitrary commands at `lifecycle.start` time. |
| **OSS publication — security disclosure process** | Low | Low | `SECURITY.md` with contact method and response-time expectations. |

## 8. Technical Constraints

| Constraint | Impact |
|------------|--------|
| **Client transport is always stdio.** MCP clients spawn servers as subprocesses. | Proxy cannot expose HTTP/SSE on the client side. All client communication is stdin/stdout JSON-RPC. |
| **One proxy instance per MCP server.** Not a multiplexer. | Each backend needs its own config YAML and proxy process. Simple model, but N backends = N processes. |
| **Config is trusted input.** Lifecycle commands are arbitrary shell commands from YAML. | No sandboxing or command validation beyond schema. Acceptable for personal tool. Documented in README for OSS users — YAML files from untrusted sources must not be run. |
| **No JSON-RPC batch support.** | If an MCP client sends batched requests, proxy rejects them with an error. Permanent non-goal (§4). |
| **Interpreter runtime required (MVP).** Standalone binary is post-MVP. | MVP requires a compatible runtime on host (or a version manager that provides it). |
| **Stderr is the default log channel; its visibility depends on the parent (FR-19, FR-21).** No structured telemetry export. | MCP clients spawn the proxy as a stdio subprocess — stderr is captured by the parent. Whether a user can read it depends on the client: Claude Desktop persists it to `~/Library/Logs/Claude/mcp*.log` (macOS) / `%APPDATA%\Claude\logs\mcp*.log` (Windows); Claude Code does not persist it (anthropics/claude-code#29035, closed as "not planned" on 2026-03-26). For diagnostics under clients that do not persist stderr, enable FR-21 file logging. FR-19.4 makes stdout-contamination a hard invariant on all channels. |
| **Cache invalidation is manual (MVP).** Delete cache file + restart proxy. `warm` command available in post-MVP phase 1. | Users must remember to manually invalidate cache after backend updates. No automatic detection of tool changes until schema refresh (post-MVP). |
| **MCP protocol version not enforced by proxy (FR-20).** | Mismatched client/backend versions surface as backend-side errors on tool calls, not proxy rejections. Version negotiation is between client and backend; proxy is transparent. |
| **stdio shutdown timeouts inherited from MCP SDK at 2s + 2s (FR-8.3).** | Not configurable in v1. Backends that legitimately need >4s to exit gracefully would be SIGKILLed. Extending would require forking the SDK's `stdio_client`; revisit if observed. |