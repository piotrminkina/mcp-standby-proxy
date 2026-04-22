# ADR-001: DevContainers for Development Environment Isolation

**Status:** ACCEPTED
**Date:** 2026-04-10 (original) · 2026-04-22 (container engine revised from Podman to Docker)

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

OCI-standard development environment defined by `devcontainer.json`. Supported by
VS Code, JetBrains Gateway, GitHub Codespaces, and the open-source `devcontainer` CLI.

- Full OCI-level isolation (Python, uv, system libraries, tools)
- Single `devcontainer up` for zero-config onboarding
- `devcontainer.json` is an open standard — no vendor lock-in
- Features registry composes the image — no custom Dockerfile needed
- Claude Code CLI works inside the container via `devcontainer exec` or workspace mount

### 2. Docker Compose (dev service)

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

Use **DevContainers** with **Docker** as the container engine.

### Container engine: Docker

An earlier version of this ADR chose Podman for its whitelistability in the Claude
Code sandbox (Podman allows one-time approval; Docker prompts on every invocation).
That property is still real and still valuable for AI-agent workflows. However, two
practical blockers emerged in production use that outweigh the sandbox benefit:

1. **Rootless Podman + BuildKit fails fresh DevContainer builds.** When
   `devcontainer up` triggers a fresh build with feature installation, the apt GPG
   verification step inside the build stage fails with `mkstemp ... Permission denied`
   on `/tmp`. This is a known interaction between rootless user namespaces and
   `apt-get update`'s temp file handling under `podman buildx`. Docker rootful daemon
   writes as root and does not hit this class of failure. Empirically verified:
   identical `devcontainer.json` fails on Podman, succeeds on Docker.
2. **JetBrains IDE integration breaks on Podman.** JetBrains Gateway / Remote Dev
   consistently reports a `user not found` error when connecting to a Podman-backed
   DevContainer. The Podman compat socket emulates enough of Docker's API for
   most tooling but misses edge cases JetBrains depends on. Docker works
   out-of-the-box.

The remaining Docker downside — no whitelist in the Claude Code sandbox — is
mitigated by the DevContainer boundary itself: AI agents run commands via
`devcontainer exec`, which is a Node.js binary (not a container CLI prompt) and
requires no per-invocation approval.

| Engine | Sandbox prompt for CLI | Fresh build with features | JetBrains IDE |
|--------|-----------------------|---------------------------|---------------|
| Podman | Yes/No + whitelist option | ❌ apt-GPG fails in rootless buildx | ❌ `user not found` |
| Docker | Yes/No only | ✅ works | ✅ works |
| `devcontainer` CLI | No prompt (Node.js binary) | N/A — delegates to engine | N/A |

### File structure

```
.devcontainer/
    devcontainer.json     # base image, features, overrideFeatureInstallOrder, postCreate
```

No custom Dockerfile. The base image is `python:3.12-slim`; everything else
(uv, shellcheck, git, common-utils, Python tooling) is composed from the
DevContainer Features registry.

### Verified toolchain inside container

| Tool | Version |
|------|---------|
| Python | 3.12.13 |
| uv | 0.11.7 |
| git | os-provided |
| shellcheck | 0.11.0 |
| user | `devcontainer` (non-root, via common-utils feature) |

## Consequences

### Positive

- Every contributor (human or AI agent) gets an identical environment with one command.
- Host requires only Docker — no Python, uv, or project-specific tooling.
- Fresh DevContainer build works reliably across machines (apt-GPG issue from
  Podman rootless buildx is eliminated).
- JetBrains IDE (Gateway / Remote Dev) works out-of-the-box.
- Open-source ready — zero onboarding friction; Docker is the most widely installed
  container runtime among developers.

### Negative

- ~100-300 MB RAM overhead for the running container.
- Docker is not whitelistable in the Claude Code sandbox — each `docker`/`podman`
  invocation on the host may prompt. Mitigated in practice: AI agents run commands
  via `devcontainer exec` (Node.js binary, no prompt), so the per-container-CLI
  prompt happens only on `devcontainer up` and teardown.
- Docker Desktop licensing (macOS/Windows Enterprise) may be a blocker for some
  contributors; on Linux, the open-source `docker` engine has no licensing cost.

### Neutral

- `uv.lock` remains the dependency lockfile inside the container — no change to
  dependency management strategy.
- Podman is still a valid drop-in for read-only operations
  (`devcontainer exec --docker-path podman`), though fresh builds and JetBrains
  integration will fail as documented above.

### Enforcement

All project commands (`uv sync`, `uv run`, `pytest`, `ruff`, etc.) must be executed
inside the DevContainer. Running project tooling on the host is not supported.
