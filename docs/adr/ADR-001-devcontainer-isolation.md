# ADR-001: DevContainers for Development Environment Isolation

**Status:** ACCEPTED
**Date:** 2026-04-10

## Context

The project requires full isolation of development dependencies from the host OS to
ensure maximum portability between developer environments — including AI coding agents
(Claude Code CLI) that operate in sandboxed contexts without access to host tooling
(`uv`, PyPI).

During initial team-based implementation, agents were blocked because `uv` was not
available in the sandbox and network access to PyPI was restricted. This demonstrated
that relying on host-installed tooling is a non-starter for reproducible development.

## Alternatives Considered

### 1. DevContainers (selected)

OCI-standard development environment defined by `devcontainer.json` + `Dockerfile`.
Supported by VS Code, JetBrains Gateway, GitHub Codespaces, and the open-source
`devcontainer` CLI.

- Full OCI-level isolation (Python, uv, system libraries, tools)
- Single `devcontainer up` for zero-config onboarding
- `devcontainer.json` is an open standard — no vendor lock-in
- Multi-stage Dockerfile reusable for dev and Nuitka builds (post-MVP)
- Claude Code CLI works inside the container via `devcontainer exec` or workspace mount

### 2. Docker/Podman Compose (dev service)

Manual container orchestration via `docker compose run`.

- Same isolation level as DevContainers
- No standardized IDE integration — each project wires it differently
- No lifecycle hooks, extension management, or port forwarding conventions
- Viable as a backend for DevContainers (`dockerComposeFile` field), not a replacement

### 3. Nix Flakes

Declarative, reproducible environment manager without containers.

- Strongest reproducibility guarantees (system-level lockfile)
- Steep learning curve disproportionate to project scale (~800 LOC personal tool)
- Exotic in the Python ecosystem — raises contributor friction for open-source
- ~2-5 GB local cache

### 4. uv standalone (host-installed)

`uv` manages Python and virtualenv directly on the host.

- Does not isolate system libraries (gcc, openssl, native deps)
- Requires `uv` pre-installed on every host — the exact problem we hit
- Insufficient for the stated isolation requirement

## Decision

Use **DevContainers** with **Podman** as the container engine.

### Container engine: Podman over Docker

Spike testing revealed a critical difference in Claude Code sandbox behavior:

| Engine | Sandbox prompt | Whitelistable |
|--------|---------------|---------------|
| `command docker` | Yes/No only | No — requires approve every invocation |
| `podman` | Yes/No + whitelist option | Yes — one-time approve, then automatic |
| `devcontainer` CLI | No prompt (Node.js binary) | N/A — delegates to engine underneath |

Podman is whitelistable, meaning AI agents can run container commands without repeated
approval prompts after initial authorization. Docker does not offer this option.

Both engines require `dangerouslyDisableSandbox: true` for actual container operations
(filesystem/socket access), but Podman's whitelist capability makes automated workflows
significantly smoother.

### File structure

```
.devcontainer/
    devcontainer.json     # features (common-utils), env, lifecycle hooks
    Dockerfile            # dev stage: Python 3.12 + uv + git + gnupg + shellcheck
```

### Verified toolchain inside container

| Tool | Version |
|------|---------|
| Python | 3.12.13 |
| uv | 0.11.6 |
| git | 2.47.3 |
| GnuPG | 2.4.7 |
| shellcheck | 0.10.0 |
| user | `devcontainer` (non-root, via common-utils feature) |

Docker Compose may be added as a backend if multi-service orchestration is needed
later (e.g., integration tests against real SSE backends).

## Consequences

### Positive

- Every contributor (human or AI agent) gets an identical environment with one command
- Host requires only Podman (or Docker) — no Python, uv, or project-specific tooling
- Podman is whitelistable in Claude Code sandbox — unblocks automated agent workflows
- Dockerfile is reusable for CI and Nuitka binary builds
- Open-source ready — zero onboarding friction

### Negative

- ~100-300 MB RAM overhead for the running container
- JetBrains Gateway/Remote Dev support is functional but less polished than VS Code
- Initial Dockerfile authoring cost (one-time)
- All `devcontainer exec` invocations require `--docker-path podman` flag

### Neutral

- `uv.lock` remains the dependency lockfile inside the container — no change to
  dependency management strategy
- Docker works as a drop-in replacement (omit `--docker-path podman`)

### Enforcement

All project commands (`uv sync`, `uv run`, `pytest`, `ruff`, etc.) must be executed
inside the DevContainer. Running project tooling on the host is not supported.
