FROM python:3.12-slim AS mcp-standby-proxy

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      shellcheck \
 && rm -rf /var/lib/apt/lists/*
