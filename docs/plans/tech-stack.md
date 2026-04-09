# Tech Stack Validation — mcp-standby-proxy

**Status:** APPROVED
**Date:** 2026-04-09
**Related:** [PRD](prd.md) | [Tech Spec](tech-spec.md) | [Config Spec](config-spec.md)

---

## Verdict: Go

Stack is well-matched to the problem, backed by justified architectural decisions
(AD-1 through AD-6), with documented fallbacks for the primary risk vector (MCP SDK
coupling). Spike confirmed that key dependencies and client-side constraints are
manageable.

## Stack Summary

| Component | Version | Role | Risk |
|-----------|---------|------|------|
| Python | 3.12 | Runtime, asyncio | Minimal |
| `mcp` | latest | MCP SDK (hybrid: transports only) | **Medium** — sole coupling risk |
| `httpx` | latest | Async HTTP (healthcheck, transports) | Minimal (transitive dep of mcp) |
| `pydantic` | v2 | Config validation, auto-schema | Minimal (transitive dep of mcp) |
| `click` | 8.x | CLI (serve in MVP; warm, validate post-MVP) | Minimal |
| `logging` (stdlib) | — | Logging on stderr | None (stdlib) |
| `pyyaml` | latest | YAML config parsing | Minimal |
| `anyio` | latest | Async runtime abstraction | Minimal (transitive dep of mcp) |
| `nuitka` | latest | Binary compilation (post-MVP) | Low (fallback: wheel) |
| `uv` | latest | Package manager, lockfile | Minimal |

## Component Analysis

### Python 3.12 + asyncio

**Why:** Async I/O is the natural model for this proxy — concurrent stdin reading,
HTTP/SSE forwarding, healthcheck polling, idle timer. Python is the user's primary
language (zero learning curve).

**Performance:** <5ms overhead per proxied request. Backend response time (100ms-10s)
dominates. Proxy is not the bottleneck.

**Alternative considered:** Go (goroutines, single binary). Rejected — marginal
implementation speed gain does not justify learning a new language under tight
timeline constraints.

### `mcp` SDK — Hybrid Approach (AD-6)

**Why hybrid, not full SDK:**
- **Client side (MCP client → proxy):** Raw stdio JSON-RPC. ~150 lines. Full control
  over routing, caching, `initialize` response. SDK server layer adds complexity
  without benefit for a pass-through proxy.
- **Backend side (proxy → real server):** SDK transport context managers
  (`sse_client()`, `streamablehttp_client()`, `stdio_client()`). Gets battle-tested
  SSE reconnection, HTTP session management, and subprocess lifecycle for free.

**Risk:** SDK is in the critical path. Breaking API change = proxy breaks.

**Mitigations:**
1. Pin via `uv.lock` — no surprise upgrades.
2. Limited API surface — only transport managers + `ClientSession`.
3. Documented fallback (AD-6): drop to raw `JSONRPCMessage` over SDK transport streams.
4. Ultimate fallback: replace SDK transports with raw `httpx` SSE/HTTP.

**Spike finding:** Python SDK has **no default request timeout** (issue #1374). Proxy
must set explicit timeouts on all `ClientSession` calls. Documented, manageable.

**Transparency guarantee:** For all forwarded requests (`tools/call`, `resources/read`),
the proxy operates at the raw JSON-RPC layer — sending/receiving `JSONRPCMessage`
objects via the SDK's transport streams. Only the `id` field is remapped; the `result`
payload passes through without parsing. `ClientSession.call_tool()` and
`ClientSession.list_tools()` are used ONLY for cache bootstrap and schema refresh,
where the proxy needs to interpret the response to extract capabilities.

### `httpx`

Async HTTP client for healthcheck polling and as the underlying transport for SSE
and Streamable HTTP. Already a transitive dependency of the `mcp` SDK — zero
additional cost. De facto standard for async HTTP in Python. No risk.

### `pydantic` v2

Config model with auto-generated JSON Schema (satisfies FR-5.2). Clear error messages
with field paths. Already a transitive dependency of `mcp` SDK. Stable API, no risk.

Fulfills user requirement: schema auto-generated from internal data model structure.

### `click`

CLI framework for subcommands (`serve` in MVP, `warm` and `validate` in post-MVP). Mature, composable,
minimal. Preferred over `typer` (unnecessary `rich` dependency) and `argparse`
(inferior DX). One of the most stable packages in the Python ecosystem.

### `logging` (stdlib)

Standard library logging with a custom formatter outputting
`timestamp level [server_name] message` to stderr. Sufficient for a personal tool
where one person reads the logs. If structured JSON logging is needed later (e.g.,
for centralized log aggregation), `structlog` can be added at that point.

Packages explicitly NOT used for logging:
- `structlog` — processor chains, bound loggers, contextvars integration are overkill
  for ~800 LOC personal tool. Stdlib logging works fine with asyncio.

### `nuitka` (post-MVP)

Compiles Python → C → native binary (`--onefile`). Same distribution model as old
`docker-compose` (which used PyInstaller). 99.9% compatibility with CPython — "boring
Python" dependencies (asyncio, httpx, pydantic) are safe. Fallback: standard wheel
install (`pip install` / `uv pip install`).

Post-MVP concern — does not block implementation.

### `uv`

Fast, lockfile-based package manager. Manages Python versions, virtual environments,
and script execution (`uv run`). Backed by Astral (creators of ruff). Rapidly becoming
the standard. No risk.

## Dependency Graph

```
mcp-standby-proxy
├── mcp              ← sole coupling risk (hybrid approach limits surface)
│   ├── httpx        ← stable, shared
│   ├── pydantic     ← stable, shared
│   └── anyio        ← stable, shared
├── pyyaml           ← stable, zero risk
├── (stdlib logging)   ← no additional dependency
├── click            ← stable, zero risk
└── [dev] nuitka     ← post-MVP, isolated
```

**Single risk vector:** the `mcp` package. All other dependencies are proven, stable
libraries with minimal coupling. The hybrid approach (AD-6) constrains the SDK
dependency to transport managers + ClientSession — a narrow, well-defined surface.

## Spike Results (informing stack decisions)

### Client-side timeouts (Claude Code)

| Component | Operation | Default timeout |
|-----------|-----------|-----------------|
| Claude Code CLI | MCP connection | 30s (`MCP_TIMEOUT` env var) |
| MCP TypeScript SDK | All JSON-RPC requests | 60s (hard timeout) |
| MCP Python SDK (SSE) | HTTP request | 5s (SSE read: 300s) |
| MCP Python SDK (HTTP) | HTTP request | 30s (SSE read: 300s) |

**Impact:** Cold cache bootstrap must complete within 60s (TypeScript SDK hard timeout).
Backends with >30s startup time (Milvus) **cannot** bootstrap via `tools/list` — the
`warm` CLI command (FR-13, post-MVP phase 1) is recommended for these backends. In MVP, the first `tools/list` request triggers cold bootstrap — backends with >60s startup may hit client-side timeouts.

### Client shutdown behavior

Signal sequence: SIGINT → SIGTERM → SIGKILL (escalation).

Known issues: orphaned processes (GitHub issues #1935, #33947). No stdin EOF signal
confirmed. Proxy must handle SIGINT, SIGTERM, and stdin EOF defensively (FR-2.5).

**Impact:** FR-15 (startup cleanup / orphan detection) is important for robustness (post-MVP phase 1).

## Conditions That Would Change Verdict

| Condition | Likelihood | Impact |
|-----------|-----------|--------|
| MCP SDK rewritten incompatibly with hybrid approach | Very low | Fallback to raw httpx |
| Python 3.12 EOL without upgrade path | Minimal (EOL: 2028) | Standard upgrade |
| Nuitka fails to compile `mcp` SDK | Low | Fallback to wheel distribution |

No condition is probable enough to block the project.
