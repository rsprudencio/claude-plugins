# Next: URL-Based Configuration (Post v1.35.0)

## Context

v1.35.0 replaced `PersistentClient` with `HttpClient` — all ChromaDB access is now HTTP. This unlocked a simpler architecture: everything runs in Docker, the only variables are URLs.

## Current State (v1.35.0)

- `mcp_transport` config key with 3 modes: `local` (stdio), `container`, `remote`
- `mcp_remote_url` for remote mode
- `chroma_host` / `chroma_port` for ChromaDB connection
- Native (uvx/stdio) install path still exists in `install.sh`
- `jarvis-transport.sh` has mode-switching logic
- Server early-exit pattern (stdio servers bail when mode != local)

## Proposed Simplification

**Two URLs. No modes. No babysitting.**

| Service | Config | Default |
|---------|--------|---------|
| MCP server | URL in `.mcp.json` | `http://localhost:8741/mcp` |
| ChromaDB | `chroma_host` + `chroma_port` | `localhost:8743` |

Both default to localhost. User changes them to remote addresses whenever needed. No mode concept — just URLs pointing to wherever the services run.

## Deployment Scenarios

| MCP Server | ChromaDB | How |
|-----------|----------|-----|
| Local container | Same container | Default — `docker compose up` |
| Local container | Remote server | Change `chroma_host` in config |
| Remote container | Same container | Change MCP URL in `.mcp.json` |
| Remote container | Separate remote | Change both URLs |

## What Gets Removed

- `mcp_transport` / `mcp_remote_url` config keys
- Mode-switching logic in `jarvis-transport.sh` (simplify to status + URL helpers)
- Native/stdio code path in `install.sh`
- Server early-exit pattern in `server.py` / todoist `server.py`
- "Native vs Docker" choice in installer — Docker is the only method

## What Changes

- **`install.sh`**: Docker-only. Ask for vault path, optional URLs (default localhost).
- **`entrypoint.sh`**: If `CHROMA_HOST` is `127.0.0.1`/`localhost` → start embedded ChromaDB. Otherwise skip.
- **`.mcp.json`**: Always HTTP type. URL from config.
- **`jarvis.sh`**: Auto-start local Docker. No stdio fallback.
- **`jarvis-transport.sh`**: Simplify to status/diagnostics, maybe URL setter.

## Prerequisites

- v1.35.0 tested end-to-end (ChromaDB HTTP-only, Docker 3-process, local chroma lifecycle)
- Confirm no users depend on native/stdio path before removing it
