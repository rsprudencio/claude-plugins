#!/bin/bash
# Jarvis MCP Server - Docker Entrypoint
# Manages ChromaDB server, jarvis-core, jarvis-obsidian, and optionally
# jarvis-todoist.

set -e

CORE_PORT="${JARVIS_CORE_PORT:-8741}"
TODOIST_PORT="${JARVIS_TODOIST_PORT:-8742}"
CHROMA_PORT="${CHROMA_PORT:-8743}"
OBSIDIAN_PORT="${JARVIS_OBSIDIAN_PORT:-8744}"
CHROMA_DATA="${JARVIS_HOME:-/config}/db"
CORE_PID=""
TODOIST_PID=""
CHROMA_PID=""
OBSIDIAN_PID=""

# --- Git configuration for mounted vault ---
if [ -d "/vault" ]; then
    git config --global safe.directory /vault
fi

# Windows host CRLF handling
if [ "${JARVIS_AUTOCRLF}" = "true" ]; then
    git config --global core.autocrlf true
fi

# --- TLS configuration ---
TLS_CERT="${JARVIS_TLS_CERT:-}"
TLS_KEY="${JARVIS_TLS_KEY:-}"
TLS_ENABLED=false

if [ -n "$TLS_CERT" ] && [ -n "$TLS_KEY" ]; then
    # Validate both files exist and are readable
    if [ ! -r "$TLS_CERT" ]; then
        echo "[jarvis] ERROR: TLS cert not readable: ${TLS_CERT}" >&2
        exit 1
    fi
    if [ ! -r "$TLS_KEY" ]; then
        echo "[jarvis] ERROR: TLS key not readable: ${TLS_KEY}" >&2
        exit 1
    fi
    TLS_ENABLED=true
    echo "[jarvis] TLS enabled"
elif [ -n "$TLS_CERT" ] || [ -n "$TLS_KEY" ]; then
    echo "[jarvis] ERROR: Both JARVIS_TLS_CERT and JARVIS_TLS_KEY must be set" >&2
    exit 1
fi

# --- Internal hook token ---
# Auto-generate if not provided; hook scripts use this to authenticate
JARVIS_INTERNAL_TOKEN="${JARVIS_INTERNAL_TOKEN:-$(python3 -c 'import secrets; print(secrets.token_hex(32))')}"
export JARVIS_INTERNAL_TOKEN

# --- Graceful shutdown ---
cleanup() {
    echo "[jarvis] Shutting down..."
    [ -n "$CORE_PID" ] && kill "$CORE_PID" 2>/dev/null
    [ -n "$TODOIST_PID" ] && kill "$TODOIST_PID" 2>/dev/null
    [ -n "$OBSIDIAN_PID" ] && kill "$OBSIDIAN_PID" 2>/dev/null
    # Wait up to 10s for jarvis-core to drain in-flight requests
    local timeout=10
    while [ $timeout -gt 0 ] && kill -0 "$CORE_PID" 2>/dev/null; do
        sleep 1
        timeout=$((timeout - 1))
    done
    # Stop ChromaDB after jarvis-core is done
    [ -n "$CHROMA_PID" ] && kill "$CHROMA_PID" 2>/dev/null
    echo "[jarvis] Shutdown complete."
    exit 0
}
trap cleanup SIGTERM SIGINT

# --- Check for Todoist token ---
has_todoist_token() {
    # Check env var
    if [ -n "$TODOIST_API_TOKEN" ]; then
        return 0
    fi
    # Check config file
    local config="${JARVIS_HOME:-/config}/config.json"
    if [ -f "$config" ]; then
        python3 -c "
import json, sys
with open('$config') as f:
    c = json.load(f)
token = c.get('todoist', {}).get('api_token', '')
sys.exit(0 if token else 1)
" 2>/dev/null && return 0
    fi
    return 1
}

# --- Wait for health check ---
HEALTH_SCHEME="http"
CURL_TLS_FLAGS=""
if [ "$TLS_ENABLED" = "true" ]; then
    HEALTH_SCHEME="https"
    CURL_TLS_FLAGS="-k"  # Self-signed cert inside container
fi

wait_for_health() {
    local url="$1"
    local name="$2"
    local max_retries="${3:-30}"
    local i=0

    while [ $i -lt $max_retries ]; do
        if curl -sf $CURL_TLS_FLAGS "${url}" > /dev/null 2>&1; then
            echo "[jarvis] ${name} is ready"
            return 0
        fi
        i=$((i + 1))
        sleep 1
    done
    echo "[jarvis] ERROR: ${name} failed to start"
    return 1
}

# --- Start ChromaDB server ---
echo "[jarvis] Starting ChromaDB server on port ${CHROMA_PORT}..."
mkdir -p "${CHROMA_DATA}"
chroma run \
    --host 0.0.0.0 \
    --port "${CHROMA_PORT}" \
    --path "${CHROMA_DATA}" 2>&1 &
CHROMA_PID=$!

wait_for_health "http://127.0.0.1:${CHROMA_PORT}/api/v2/heartbeat" "ChromaDB" 30

# Set env vars for jarvis-core HttpClient
export CHROMA_HOST=127.0.0.1
export CHROMA_PORT

# --- Build TLS args (bash array — safe for paths with spaces) ---
tls_args=()
if [ "$TLS_ENABLED" = "true" ]; then
    tls_args+=(--ssl-certfile "$TLS_CERT" --ssl-keyfile "$TLS_KEY")
fi

# --- mTLS: client certificate verification ---
TLS_CA="${JARVIS_TLS_CA:-}"
if [ -n "$TLS_CA" ]; then
    if [ ! -r "$TLS_CA" ]; then
        echo "[jarvis] ERROR: TLS CA cert not readable: ${TLS_CA}" >&2
        exit 1
    fi
    if [ "$TLS_ENABLED" != "true" ]; then
        echo "[jarvis] ERROR: JARVIS_TLS_CA requires JARVIS_TLS_CERT and JARVIS_TLS_KEY" >&2
        exit 1
    fi
    # CERT_OPTIONAL (1): request client cert, verify if presented, but don't require.
    # This lets health check curl work without a client cert.
    tls_args+=(--ssl-ca-certs "$TLS_CA" --ssl-cert-reqs 1)
    echo "[jarvis] mTLS enabled (client certs verified against ${TLS_CA})"
fi

# --- Start jarvis-core ---
echo "[jarvis] Starting jarvis-core on port ${CORE_PORT}..."
cd /app/jarvis-core
uvicorn http_app:app \
    --host 0.0.0.0 \
    --port "${CORE_PORT}" \
    --log-level info \
    --no-access-log \
    "${tls_args[@]}" &
CORE_PID=$!

# --- Start jarvis-obsidian ---
echo "[jarvis] Starting jarvis-obsidian on port ${OBSIDIAN_PORT}..."
cd /app/jarvis-obsidian
uvicorn http_app:app \
    --host 0.0.0.0 \
    --port "${OBSIDIAN_PORT}" \
    --log-level info \
    --no-access-log \
    "${tls_args[@]}" &
OBSIDIAN_PID=$!

# Set URL for core's health check detection
export JARVIS_OBSIDIAN_URL="${HEALTH_SCHEME}://127.0.0.1:${OBSIDIAN_PORT}"

# --- Conditionally start jarvis-todoist ---
if has_todoist_token; then
    echo "[jarvis] Todoist token found, starting jarvis-todoist on port ${TODOIST_PORT}..."
    cd /app/jarvis-todoist
    uvicorn http_app:app \
        --host 0.0.0.0 \
        --port "${TODOIST_PORT}" \
        --log-level info \
        --no-access-log \
        "${tls_args[@]}" &
    TODOIST_PID=$!
else
    echo "[jarvis] No Todoist token found, skipping jarvis-todoist."
fi

# --- Wait for health ---
wait_for_health "${HEALTH_SCHEME}://localhost:${CORE_PORT}/health" "jarvis-core" 30

wait_for_health "${HEALTH_SCHEME}://localhost:${OBSIDIAN_PORT}/health" "jarvis-obsidian" 30

if [ -n "$TODOIST_PID" ]; then
    wait_for_health "${HEALTH_SCHEME}://localhost:${TODOIST_PORT}/health" "jarvis-todoist" 30
fi

echo "[jarvis] All services started successfully."

# --- Wait for any process to exit, then shutdown ---
wait -n
EXIT_CODE=$?
echo "[jarvis] A process exited with code ${EXIT_CODE}, shutting down..."
cleanup
