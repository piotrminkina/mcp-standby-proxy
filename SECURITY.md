# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in `mcp-standby-proxy`, please
report it privately via [GitHub Security Advisories][ghsa]. Do not open a
public issue or pull request for security-sensitive reports.

[ghsa]: https://github.com/piotrminkina/mcp-standby-proxy/security/advisories/new

Expected response: acknowledgment within 7 days. This is a personal
project maintained on a best-effort basis — disclosure timelines are
discussed case by case.

## Threat Model

The proxy is designed under two explicit trust assumptions:

### 1. The YAML config file is trusted input

`lifecycle.start` and `lifecycle.stop` execute as **arbitrary shell
commands** with the proxy process's full privileges. `backend.env` is
merged into the child process environment for stdio backends. There is
no sandbox and no command validation beyond YAML schema parsing.

**Running `mcp-standby-proxy` against a config file you did not author
or review is equivalent to executing an arbitrary shell script.** Do
not:

- Run configs downloaded from third parties, pasted from chat, or
  committed to repositories you don't control.
- Run configs containing `env` values sourced from untrusted input.
- Expose the proxy binary as a service that accepts config paths from
  the network or other untrusted input.

### 2. The MCP client / backend pair is trusted

The proxy is a thin JSON-RPC relay. It does not authenticate clients or
backends, does not validate tool-call arguments, and does not sandbox
what the backend returns to the client. Security properties of the
`tools/call` round-trip are whatever the client and backend agree to.

## Supported Versions

Active development tracks the `master` branch. The project is currently
pre-release (no tagged versions). Once tagged releases begin, only the
latest minor release will receive security fixes.
