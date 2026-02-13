#!/bin/bash
# Jarvis MCP Server - Docker Entrypoint
# Manages jarvis-core and optionally jarvis-todoist uvicorn processes.

set -e

CORE_PORT="${JARVIS_CORE_PORT:-8741}"
TODOIST_PORT="${JARVIS_TODOIST_PORT:-8742}"
CORE_PID=""
TODOIST_PID=""

# --- Git configuration for mounted vault ---
if [ -d "/vault" ]; then
    git config --global safe.directory /vault
fi

# Windows host CRLF handling
if [ "${JARVIS_AUTOCRLF}" = "true" ]; then
    git config --global core.autocrlf true
fi

# --- Graceful shutdown ---
cleanup() {
    echo "[jarvis] Shutting down..."
    [ -n "$CORE_PID" ] && kill "$CORE_PID" 2>/dev/null
    [ -n "$TODOIST_PID" ] && kill "$TODOIST_PID" 2>/dev/null
    wait
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
wait_for_health() {
    local port="$1"
    local name="$2"
    local max_retries=30
    local i=0

    while [ $i -lt $max_retries ]; do
        if curl -sf "http://localhost:${port}/health" > /dev/null 2>&1; then
            echo "[jarvis] ${name} is ready on port ${port}"
            return 0
        fi
        i=$((i + 1))
        sleep 1
    done
    echo "[jarvis] ERROR: ${name} failed to start on port ${port}"
    return 1
}

# --- Start jarvis-core ---
echo "[jarvis] Starting jarvis-core on port ${CORE_PORT}..."
cd /app/jarvis-core
uvicorn http_app:app \
    --host 0.0.0.0 \
    --port "${CORE_PORT}" \
    --log-level info \
    --no-access-log &
CORE_PID=$!

# --- Conditionally start jarvis-todoist ---
if has_todoist_token; then
    echo "[jarvis] Todoist token found, starting jarvis-todoist on port ${TODOIST_PORT}..."
    cd /app/jarvis-todoist
    uvicorn http_app:app \
        --host 0.0.0.0 \
        --port "${TODOIST_PORT}" \
        --log-level info \
        --no-access-log &
    TODOIST_PID=$!
else
    echo "[jarvis] No Todoist token found, skipping jarvis-todoist."
fi

# --- Wait for health ---
wait_for_health "${CORE_PORT}" "jarvis-core"

if [ -n "$TODOIST_PID" ]; then
    wait_for_health "${TODOIST_PORT}" "jarvis-todoist"
fi

echo "[jarvis] All services started successfully."

# --- Wait for any process to exit, then shutdown ---
wait -n
EXIT_CODE=$?
echo "[jarvis] A process exited with code ${EXIT_CODE}, shutting down..."
cleanup
