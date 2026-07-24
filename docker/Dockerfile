# Jarvis MCP Server - Docker Image
# Runs embedded PostgreSQL (pgvector), jarvis-core (port 8741),
# jarvis-obsidian (port 8744), and optionally jarvis-todoist (port 8742)
# via Streamable HTTP transport.
#
# PostgreSQL is embedded by default for single-user deployments.
# Set POSTGRES_URL to use an external database (team/managed deployments).

# ---- Builder stage: install Python dependencies ----
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

# Install all dependencies via uv (single layer for caching)
# Retrieval inference is host-only. Keep the image free of model runtimes and weights.
# Core: mcp, numpy, psycopg, pgvector
# Todoist: todoist-api-python
RUN uv pip install --system --no-cache \
    "mcp>=1.0.0" \
    "numpy>=1.26.0" \
    "psycopg[pool]>=3.1.0" \
    "pgvector>=0.3.0" \
    "todoist-api-python>=3.0.0" \
    "requests>=2.31.0" \
    "boto3>=1.34.0" \
    "fastapi>=0.110.0" \
    uvicorn

# ---- Runtime stage ----
FROM python:3.12-slim

# Add PGDG apt repository for PostgreSQL 17 + pgvector (embedded server)
# Auto-detect Debian codename (bookworm, trixie, etc.) for correct PGDG repo
RUN apt-get update && apt-get install -y --no-install-recommends \
    gnupg curl ca-certificates lsb-release && \
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | \
    gpg --dearmor -o /etc/apt/trusted.gpg.d/pgdg.gpg && \
    echo "deb http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > \
    /etc/apt/sources.list.d/pgdg.list && \
    apt-get update && apt-get install -y --no-install-recommends \
    git \
    postgresql-17 \
    postgresql-17-pgvector \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/usr/lib/postgresql/17/bin:${PATH}"

# Copy installed Python packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Native host inference is the only inference path shipped in this image.
# The services run outside Docker (for example through launchd + llama.cpp/Metal).
ENV EMBEDDING_BACKEND=host
ENV EMBEDDING_DIMENSIONS=384
ENV JARVIS_EMBEDDING_MODEL_ID=ibm-granite/granite-embedding-small-english-r2
ENV JARVIS_MODEL_HOST_MODEL=ibm-granite/granite-embedding-small-english-r2
ENV JARVIS_MODEL_HOST_URL=http://host.docker.internal:8751
ENV RERANKING_ENABLED=true
ENV RERANKING_BACKEND=host
ENV RERANKING_MODEL=BAAI/bge-reranker-v2-m3
ENV JARVIS_RERANK_HOST_URL=http://host.docker.internal:8752

# ---- Copy jarvis-common shared package ----
WORKDIR /app/jarvis-common
COPY lib/jarvis-common/ .
RUN uv pip install --system /app/jarvis-common/

# ---- Copy jarvis-core source ----
# Uses directory COPY so new .py files are included automatically.
# Tests, __pycache__, pyproject.toml etc. are excluded via .dockerignore.
WORKDIR /app/jarvis-core
COPY plugins/jarvis/mcp-server/ .

# ---- Copy jarvis-todoist source ----
WORKDIR /app/jarvis-todoist
COPY plugins/jarvis-todoist/mcp-server/ .

# ---- Copy jarvis-obsidian source ----
WORKDIR /app/jarvis-obsidian
COPY plugins/jarvis-obsidian/mcp-server/ .

# ---- Copy memory-explorer ----
WORKDIR /app/memory-explorer
COPY apps/memory-explorer/app.py apps/memory-explorer/app_admin.py ./

# ---- PostgreSQL init script ----
COPY docker/init.sql /app/init.sql

# ---- Entrypoint ----
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Version (set via --build-arg for health endpoint reporting)
ARG JARVIS_VERSION=unknown
ENV JARVIS_VERSION=$JARVIS_VERSION

# Default ports
ENV JARVIS_CORE_PORT=8741
ENV JARVIS_TODOIST_PORT=8742
ENV JARVIS_OBSIDIAN_PORT=8744

EXPOSE 8741 8742 8744 8750

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -sf http://localhost:${JARVIS_CORE_PORT}/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
