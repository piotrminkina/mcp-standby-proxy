# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Project Overview

Python-based stdio proxy for MCP (Model Context Protocol) servers. Serves cached tool
schemas on startup and starts backends on-demand when the agent calls a tool. One proxy
instance per MCP server, configured via YAML. See [PRD](docs/plans/prd.md) for requirements
and [Tech Stack](docs/plans/tech-stack.md) for technology choices.

## Development Environment

The project uses a **DevContainer** for full isolation of dependencies from the host OS.
See [ADR-001](docs/adr/ADR-001-devcontainer-isolation.md) for rationale.

**Container engine:** Podman (whitelistable in Claude Code sandbox; Docker works too).

### Starting the DevContainer

```bash
devcontainer up --workspace-folder . --docker-path podman
```

### Running commands inside the DevContainer

```bash
devcontainer exec --workspace-folder . --docker-path podman uv sync
devcontainer exec --workspace-folder . --docker-path podman uv run pytest
devcontainer exec --workspace-folder . --docker-path podman uv run mcp-standby-proxy serve -c config.yaml
```

### Tearing down

```bash
podman rm -f $(podman ps -aq --filter label=devcontainer.local_folder=$(pwd))
```

**All project commands (`uv sync`, `uv run pytest`, etc.) must be run inside the
DevContainer.** Do not install project dependencies or run tools on the host.

## Architecture

See [Tech Spec](docs/plans/tech-spec.md) for runtime architecture (transport protocol,
concurrency model, key flows) and [Config Spec](docs/plans/config-spec.md) for
configuration schema and cache format.

Key operational rules for implementation:
- State transitions are serialized via `asyncio.Lock` — no concurrent transitions.
- Requests arriving during transitional states (Starting, Stopping) are queued.
- Stopping always completes before restart — never cancel a stop mid-execution.
- For all proxied requests, operate at raw `JSONRPCMessage` layer — only remap `id`.

## Coding Conventions

- **Indentation:** 4 spaces default; 2 spaces for YAML and JSON (see `.editorconfig`)
- **Type hints:** Use type hints on all public functions and class attributes.
- **Imports:** stdlib first, third-party second, local third. One blank line between groups.
- **Async:** Use `asyncio` as the async backend. `anyio` is a transitive dependency of
  `mcp` SDK — use it only where the SDK requires it.
- **Logging:** stdlib `logging` with format `timestamp level [server_name] message` on
  stderr. No structlog. Add structured fields only if JSON output is needed later.
- **CLI:** `click` with subcommands. MVP has `serve` only.
- **Config:** Pydantic models with YAML loading via `pyyaml`. Schema auto-generated
  from the model.
- **Error handling:** Catch at system boundaries. Internal errors propagate. Use
  `thiserror`-style custom exceptions inheriting from a base `ProxyError`.

## Quality Rules

### Bugfix Methodology: Reproduce First, Fix Second

1. **Red** — write a test that reproduces the exact failure. Verify it fails for the
   right reason against unfixed code.
2. **Green** — implement the minimal fix. Confirm the test passes.
3. **Refactor** — clean up, re-run all tests.

### Testing (pytest)

- Use `pytest` + `pytest-asyncio` for async tests.
- Use fixtures for test setup and dependency injection.
- Use parameterized tests (`@pytest.mark.parametrize`) for testing multiple inputs.
- Use `monkeypatch` for mocking dependencies — prefer over `unittest.mock` where possible.
- Mock MCP servers as async functions, not real processes.
- Mock lifecycle commands with simple scripts (`true`/`false`).
- No real Docker/systemd in tests.

### Conventional Commits

- **Format:** `type(scope): description` — e.g. `feat(proxy): add request queuing
  during Starting state`. Subject line under 72 characters.
- **Types:** `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`.
- **Scopes:** Based on modules: `proxy`, `lifecycle`, `transport`, `config`, `cache`,
  `healthcheck`, `cli`, `idle`. Use `repo` for project-wide changes.
- **Breaking changes:** Use `!` after type/scope or `BREAKING CHANGE:` footer.
- **Enforcement:** Commit message format validated by `gitlint` via `.githooks/commit-msg`.
  Activate with `git config core.hooksPath .githooks`.

### Git

- **Atomic commits:** Each commit = one cohesive logical change. If a feature requires
  code + tests + doc changes, commit them together. Do not bundle unrelated changes.
- **Meaningful descriptions:** Explain *why*, not just *what*. Body may elaborate on
  context, trade-offs, or alternatives considered.
- **Clean history:** Interactive rebase to squash fixups before merging.
- **Branch naming:** `<type>/<short-description>` — e.g. `feat/sse-transport`,
  `fix/healthcheck-timeout`.
- **Git hooks:** `.githooks/` directory. Activate with `git config core.hooksPath .githooks`.

### Docker / Podman

- **Container engine:** Podman is the default. Use `--docker-path podman` with
  `devcontainer` CLI. Docker works identically if preferred.
- **DevContainer as canonical environment:** `.devcontainer/Dockerfile` defines the
  dev image. `devcontainer.json` configures features, user, and lifecycle hooks.
- **Multi-stage builds:** Separate dev, builder, and runtime stages.
- **Non-root user:** Created by `common-utils` feature (`devcontainer` user).
- **Layer caching:** Copy `pyproject.toml` + `uv.lock` before source code.
- **Clean up in the same layer:** Install + purge in a single `RUN`.

### Architecture Decision Records

Record significant decisions in `docs/adr/` when:
- Adding a major dependency
- Changing architectural patterns
- Introducing new integration patterns
- Making decisions that affect the project's direction

Format: `ADR-NNN: Title` with Status, Context, Decision, Consequences sections.

## Script Conventions

All shell scripts in `bin/` must:

1. Use `#!/usr/bin/env bash` shebang and `.bash` extension.
2. Set `set -euo pipefail` immediately after shebang.
3. Pass `shellcheck` with zero warnings.
4. Use `function` keyword for all function definitions.
5. Declare variables at the top of scope (`declare` for globals, `local` for functions).
6. Mark constants with `readonly` after assignment.
7. Use consistent logging: `info()`, `warn()`, `error()`, `ok()`.
8. Exit codes: `0` = success, `1` = failure.
9. No hardcoded home paths — use `$HOME`.

## Key Principles

1. **Read before writing.** Always read existing files before proposing changes.
2. **Minimal changes.** Only modify what is necessary for the task at hand.
3. **Follow existing patterns.** Match style and conventions already in the codebase.

## Documentation Maintenance

When making significant changes (new modules, architecture changes, modified workflows),
update `CLAUDE.md` and `README.md` to keep them in sync with the actual project state.
