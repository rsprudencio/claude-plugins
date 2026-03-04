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
# Core: mcp, psycopg, pgvector, sentence-transformers, onnxruntime
# Todoist: todoist-api-python
RUN uv pip install --system --no-cache \
    "mcp>=1.0.0" \
    "tokenizers>=0.13.0" \
    "sentence-transformers[onnx]>=3.0.0" \
    "onnxruntime>=1.17.0" \
    "psycopg[pool]>=3.1.0" \
    "pgvector>=0.3.0" \
    "todoist-api-python>=3.0.0" \
    uvicorn

# ---- Model fetch stage: download and verify cross-encoder ONNX model ----
FROM alpine:3.20 AS model-fetch

RUN apk add --no-cache wget

# HuggingFace commit c5ee24cb — ms-marco-MiniLM-L-6-v2
ARG HF_BASE=https://huggingface.co/cross-encoder/ms-marco-MiniLM-L-6-v2/resolve/main
ARG MODEL_HASH=5d3e70fd0c9ff14b9b5169a51e957b7a9c74897afd0a35ce4bd318150c1d4d4a
ARG TOKENIZER_HASH=d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66

WORKDIR /models/cross-encoder
RUN wget -q "${HF_BASE}/onnx/model.onnx" -O model.onnx && \
    echo "${MODEL_HASH}  model.onnx" | sha256sum -c && \
    wget -q "${HF_BASE}/tokenizer.json" -O tokenizer.json && \
    echo "${TOKENIZER_HASH}  tokenizer.json" | sha256sum -c

# ---- Embedding model fetch: granite-embedding-small-english-r2 INT8 quantized ----
FROM alpine:3.20 AS embedding-fetch

RUN apk add --no-cache wget

# onnx-community/granite-embedding-small-english-r2-ONNX (384d, ModernBERT, INT8)
# ~52MB on disk. model_quantized.onnx renamed to model.onnx for runtime compat.
ARG EMB_BASE=https://huggingface.co/onnx-community/granite-embedding-small-english-r2-ONNX/resolve/main
WORKDIR /models/embedding/onnx
RUN wget -q "${EMB_BASE}/onnx/model_quantized.onnx" -O model.onnx && \
    echo "a3fad524afc3f060216a8ddbb1ac89c9b6498fba8995b5718bde879076a2e9ba  model.onnx" | sha256sum -c && \
    wget -q "${EMB_BASE}/onnx/model_quantized.onnx_data" -O model.onnx_data && \
    echo "1f4cf47e4adec7f7ae09db03d071ba8667e07f9a4203142c7efa8d37fe453597  model.onnx_data" | sha256sum -c

WORKDIR /models/embedding
RUN wget -q "${EMB_BASE}/tokenizer.json" -O tokenizer.json && \
    echo "feeb83348dcb033bc6b9d2e1f7906ca9eb2d122845000c9416d894d7c2927149  tokenizer.json" | sha256sum -c && \
    wget -q "${EMB_BASE}/config.json" -O config.json && \
    echo "1a1710c20911da8c96179716bf44058e54cca6fa7952cce77be83ef05edae3ee  config.json" | sha256sum -c && \
    wget -q "${EMB_BASE}/tokenizer_config.json" -O tokenizer_config.json && \
    wget -q "${EMB_BASE}/special_tokens_map.json" -O special_tokens_map.json

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

# Copy pre-verified cross-encoder ONNX model
COPY --from=model-fetch /models /app/models
ENV JARVIS_MODEL_DIR=/app/models/cross-encoder

# Copy pre-fetched embedding model (onnx-community INT8 quantized, ~52MB)
COPY --from=embedding-fetch /models/embedding /app/models/embedding
ARG EMBEDDING_DIMS=384
ENV EMBEDDING_MODEL=/app/models/embedding
ENV EMBEDDING_DIMENSIONS=${EMBEDDING_DIMS}

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

EXPOSE 8741 8742 8744

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -sf http://localhost:${JARVIS_CORE_PORT}/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
