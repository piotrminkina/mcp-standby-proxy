# Contributing

Thanks for your interest in `mcp-standby-proxy`. This document covers the
development environment, how to run tests and linters, and the repo's
conventions.

## Development environment

The project uses a **DevContainer** (Podman + `@devcontainers/cli`) for full
isolation of dependencies from the host OS. See
[ADR-001](docs/adr/ADR-001-devcontainer-isolation.md) for the rationale.

### Start the DevContainer

```bash
devcontainer up --workspace-folder . --docker-path podman
```

### Run project commands inside it

All project commands (`uv sync`, tests, lint, type checks) run inside the
DevContainer:

```bash
devcontainer exec --workspace-folder . --docker-path podman uv sync
devcontainer exec --workspace-folder . --docker-path podman uv run pytest
devcontainer exec --workspace-folder . --docker-path podman uv run pytest -m smoke
devcontainer exec --workspace-folder . --docker-path podman uv run ruff check src/ tests/
devcontainer exec --workspace-folder . --docker-path podman uv run mypy src/
```

Do **not** install project dependencies or run tools on the host — the
DevContainer is the canonical environment.

### Tear down

```bash
podman rm -f $(podman ps -aq --filter label=devcontainer.local_folder=$(pwd))
```

## Testing

- **Unit + integration tests:** `uv run pytest` — fast, no external services.
- **Smoke tests:** `uv run pytest -m smoke` — opt-in, runs against real
  in-process MCP servers (FastMCP + uvicorn). Not in the default run.
- **Bugfix methodology:** reproduce first (red), fix (green), refactor. See
  [`CLAUDE.md`](CLAUDE.md) for details.

## Git hooks

Local git hooks (including `gitlint` for conventional-commit validation) live
in `.githooks/`. Activate them once after cloning:

```bash
git config core.hooksPath .githooks
```

## Commit conventions

- Format: `type(scope): description` — e.g., `feat(transport): add stdio backend`.
- Types: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`.
- Scopes match modules: `proxy`, `lifecycle`, `transport`, `config`, `cache`,
  `healthcheck`, `cli`, `idle`. Use `repo` for project-wide changes.
- Subject line under 72 characters. Body explains the **why**, not the what.
- Full conventions: [`CLAUDE.md`](CLAUDE.md).

## Coding conventions

See [`CLAUDE.md`](CLAUDE.md) for:

- Type hints, import ordering, async conventions.
- Logging (stderr-only; stdout reserved for JSON-RPC).
- Error handling (catch at system boundaries, propagate internally).
- Test patterns (pytest, monkeypatch, mock MCP servers).

## Reporting bugs

Open an issue at
<https://github.com/piotrminkina/mcp-standby-proxy/issues> with:

- What you tried (command, config excerpt with sensitive values redacted).
- Expected vs actual behaviour.
- Full stderr log (run with `-vv` for DEBUG).
- Proxy version + MCP client name/version + OS.

For security issues, prefer not disclosing publicly — open an issue with the
`security` label and minimal detail, or reach out directly.

## Proposing features

Open an issue describing the use case. If the feature intersects with an
existing FR in [`docs/plans/prd.md`](docs/plans/prd.md), reference it. For
anything non-trivial, expect a design discussion before a PR.

## Pull requests

- Branch naming: `<type>/<short-description>` — e.g., `feat/sse-transport`,
  `fix/healthcheck-timeout`.
- One cohesive logical change per commit. Bundle code + tests + docs for a
  feature together.
- CI is not yet configured; run the full test suite locally before
  requesting review.
- The PR should pass `ruff check` and `mypy --strict` cleanly.
