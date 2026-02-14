# Jarvis MCP Server - Docker Image
# Runs jarvis-core (port 8741) and optionally jarvis-todoist (port 8742)
# via Streamable HTTP transport for Claude Code integration.

# ---- Builder stage: install Python dependencies ----
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

# Install all dependencies via uv (single layer for caching)
# Core: mcp>=1.0.0, chromadb>=0.5.0, uvicorn, starlette (transitive)
# Todoist: todoist-api-python>=3.0.0
RUN uv pip install --system --no-cache \
    "mcp>=1.0.0" \
    "chromadb>=0.5.0" \
    "todoist-api-python>=3.0.0" \
    uvicorn

# ---- Runtime stage ----
FROM python:3.12-slim

# git is needed for vault git operations; curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# ---- Copy jarvis-core source ----
WORKDIR /app/jarvis-core
COPY plugins/jarvis/mcp-server/server.py .
COPY plugins/jarvis/mcp-server/protocol.py .
COPY plugins/jarvis/mcp-server/http_app.py .
COPY plugins/jarvis/mcp-server/tools/ ./tools/
COPY plugins/jarvis/mcp-server/defaults/ ./defaults/

# ---- Copy jarvis-todoist source ----
WORKDIR /app/jarvis-todoist
COPY plugins/jarvis-todoist/mcp-server/server.py .
COPY plugins/jarvis-todoist/mcp-server/todoist_api.py .
COPY plugins/jarvis-todoist/mcp-server/http_app.py .

# ---- Entrypoint ----
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Version (set via --build-arg for health endpoint reporting)
ARG JARVIS_VERSION=unknown
ENV JARVIS_VERSION=$JARVIS_VERSION

# Default ports
ENV JARVIS_CORE_PORT=8741
ENV JARVIS_TODOIST_PORT=8742

EXPOSE 8741 8742

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:${JARVIS_CORE_PORT}/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
