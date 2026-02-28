# Jarvis MCP Server - Docker Image
# Runs ChromaDB (port 8743), jarvis-core (port 8741), jarvis-obsidian
# (port 8744), and optionally jarvis-todoist (port 8742) via Streamable
# HTTP transport.

# ---- Builder stage: install Python dependencies ----
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

# Install all dependencies via uv (single layer for caching)
# Core: mcp>=1.0.0, chromadb>=1.5.0, uvicorn, starlette (transitive)
# Todoist: todoist-api-python>=3.0.0
RUN uv pip install --system --no-cache \
    "mcp>=1.0.0" \
    "chromadb>=1.5.0" \
    "tokenizers>=0.13.0" \
    "todoist-api-python>=3.0.0" \
    uvicorn

# ---- Model fetch stage: download and verify ONNX model ----
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

# Copy pre-verified ONNX model from model-fetch stage
COPY --from=model-fetch /models /app/models
ENV JARVIS_MODEL_DIR=/app/models/cross-encoder

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

# ---- Entrypoint ----
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Version (set via --build-arg for health endpoint reporting)
ARG JARVIS_VERSION=unknown
ENV JARVIS_VERSION=$JARVIS_VERSION

# Default ports
ENV JARVIS_CORE_PORT=8741
ENV JARVIS_TODOIST_PORT=8742
ENV CHROMA_PORT=8743
ENV JARVIS_OBSIDIAN_PORT=8744

EXPOSE 8741 8742 8743 8744

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -sf http://localhost:${JARVIS_CORE_PORT}/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
