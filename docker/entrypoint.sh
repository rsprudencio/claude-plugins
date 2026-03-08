#!/bin/bash
# Jarvis MCP Server - Docker Entrypoint
# Manages embedded PostgreSQL (pgvector), jarvis-core, jarvis-obsidian,
# and optionally jarvis-todoist.
#
# PostgreSQL is embedded by default for single-user deployments.
# Set POSTGRES_URL to use an external database (team/managed deployments).

set -e

CORE_PORT="${JARVIS_CORE_PORT:-8741}"
TODOIST_PORT="${JARVIS_TODOIST_PORT:-8742}"
OBSIDIAN_PORT="${JARVIS_OBSIDIAN_PORT:-8744}"
EXPLORER_PORT="${JARVIS_EXPLORER_PORT:-8750}"
# pgdata lives inside the container filesystem by default (not on the bind mount)
# because macOS VirtioFS doesn't support chown, which PostgreSQL requires.
# Use PGDATA env var to override (e.g., for a dedicated Docker volume).
PGDATA="${PGDATA:-/var/lib/postgresql/data}"
CORE_PID=""
TODOIST_PID=""
OBSIDIAN_PID=""
EXPLORER_PID=""
PG_STARTED=false

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
JARVIS_INTERNAL_TOKEN="${JARVIS_INTERNAL_TOKEN:-$(python3 -c 'import secrets; print(secrets.token_hex(32))')}"
export JARVIS_INTERNAL_TOKEN

# --- Graceful shutdown ---
cleanup() {
    echo "[jarvis] Shutting down..."
    [ -n "$CORE_PID" ] && kill "$CORE_PID" 2>/dev/null
    [ -n "$TODOIST_PID" ] && kill "$TODOIST_PID" 2>/dev/null
    [ -n "$OBSIDIAN_PID" ] && kill "$OBSIDIAN_PID" 2>/dev/null
    [ -n "$EXPLORER_PID" ] && kill "$EXPLORER_PID" 2>/dev/null
    # Wait up to 10s for jarvis-core to drain in-flight requests
    local timeout=10
    while [ $timeout -gt 0 ] && kill -0 "$CORE_PID" 2>/dev/null; do
        sleep 1
        timeout=$((timeout - 1))
    done
    # Stop PostgreSQL after MCP servers are done
    if [ "$PG_STARTED" = "true" ]; then
        echo "[jarvis] Stopping embedded PostgreSQL..."
        su postgres -c "pg_ctl stop -D '${PGDATA}' -m fast" 2>/dev/null || true
    fi
    echo "[jarvis] Shutdown complete."
    exit 0
}
trap cleanup SIGTERM SIGINT

# --- Check for Todoist token ---
has_todoist_token() {
    if [ -n "$TODOIST_API_TOKEN" ]; then
        return 0
    fi
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
    CURL_TLS_FLAGS="-k"
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

# --- Embedded PostgreSQL ---
start_embedded_postgres() {
    echo "[jarvis] Starting embedded PostgreSQL..."

    # Ensure data directory exists with correct ownership
    mkdir -p "${PGDATA}"
    chown -R postgres:postgres "${PGDATA}"

    # First-run: initialize database cluster
    if [ ! -f "${PGDATA}/PG_VERSION" ]; then
        echo "[jarvis] First run — initializing PostgreSQL data directory..."
        su postgres -c "initdb -D '${PGDATA}' --encoding=UTF8 --locale=C"

        # Write postgresql.conf (internal-only, tuned for single-user)
        cat > "${PGDATA}/postgresql.conf" <<PGCONF
listen_addresses = '127.0.0.1'
port = 5432
wal_level = logical
shared_buffers = 128MB
work_mem = 4MB
maintenance_work_mem = 64MB
max_connections = 20
max_replication_slots = 10
max_wal_senders = 10
logging_collector = off
log_destination = 'stderr'
PGCONF

        # Trust local connections only (PG is not exposed outside container)
        cat > "${PGDATA}/pg_hba.conf" <<PGHBA
# TYPE  DATABASE  USER  ADDRESS       METHOD
local   all       all                 trust
host    all       all   127.0.0.1/32  trust
PGHBA

        chown postgres:postgres "${PGDATA}/postgresql.conf" "${PGDATA}/pg_hba.conf"
    fi

    # Start PostgreSQL
    su postgres -c "pg_ctl start -D '${PGDATA}' -l '${PGDATA}/postgresql.log' -w -t 30"
    PG_STARTED=true

    # Wait for pg_isready
    local i=0
    while [ $i -lt 30 ]; do
        if pg_isready -h 127.0.0.1 -p 5432 -q 2>/dev/null; then
            echo "[jarvis] PostgreSQL is ready"
            break
        fi
        i=$((i + 1))
        sleep 1
    done

    if [ $i -eq 30 ]; then
        echo "[jarvis] ERROR: PostgreSQL failed to start within 30s" >&2
        cat "${PGDATA}/postgresql.log" >&2
        exit 1
    fi

    # Create database, jarvis role, and run init.sql (all idempotent)
    # Pipe init.sql via stdin so root reads the file (postgres user may lack /app access)
    su postgres -c "psql -h 127.0.0.1 -p 5432 -tc \"SELECT 1 FROM pg_database WHERE datname='jarvis'\" | grep -q 1" || \
        su postgres -c "createdb -h 127.0.0.1 -p 5432 jarvis"
    su postgres -c "psql -h 127.0.0.1 -p 5432 -d jarvis" < /app/init.sql

    # Create 'jarvis' role matching config.json default (postgresql://jarvis:jarvis@...)
    # so docker exec and external scripts work without POSTGRES_URL override
    su postgres -c "psql -h 127.0.0.1 -p 5432 -d jarvis" <<'ROLES'
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'jarvis') THEN
        CREATE ROLE jarvis WITH LOGIN PASSWORD 'jarvis';
    END IF;
END $$;
GRANT ALL PRIVILEGES ON DATABASE jarvis TO jarvis;
GRANT ALL ON SCHEMA public TO jarvis;
GRANT ALL ON ALL TABLES IN SCHEMA public TO jarvis;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO jarvis;
-- Read access to local/obsidian schemas (for memory-explorer and external tools)
GRANT USAGE ON SCHEMA local TO jarvis;
GRANT SELECT ON ALL TABLES IN SCHEMA local TO jarvis;
ALTER DEFAULT PRIVILEGES IN SCHEMA local GRANT SELECT ON TABLES TO jarvis;
GRANT USAGE ON SCHEMA obsidian TO jarvis;
GRANT SELECT ON ALL TABLES IN SCHEMA obsidian TO jarvis;
ALTER DEFAULT PRIVILEGES IN SCHEMA obsidian GRANT SELECT ON TABLES TO jarvis;
ROLES

    echo "[jarvis] Embedded PostgreSQL initialized (database: jarvis)"
}

# --- Wait for external PostgreSQL ---
wait_for_external_postgres() {
    echo "[jarvis] Using external PostgreSQL: ${POSTGRES_URL%%@*}@***"
    echo "[jarvis] Waiting for PostgreSQL..."
    local pg_ready=false
    for i in $(seq 1 30); do
        if python3 -c "
import psycopg
try:
    conn = psycopg.connect('$POSTGRES_URL', connect_timeout=2)
    conn.execute('SELECT 1')
    conn.close()
except Exception:
    raise SystemExit(1)
" 2>/dev/null; then
            pg_ready=true
            break
        fi
        sleep 1
    done
    if [ "$pg_ready" = true ]; then
        echo "[jarvis] PostgreSQL is ready"
    else
        echo "[jarvis] ERROR: PostgreSQL not reachable"
        exit 1
    fi
}

# --- Start PostgreSQL (embedded or wait for external) ---
if [ -z "${POSTGRES_URL}" ]; then
    start_embedded_postgres
    export POSTGRES_URL="postgresql://postgres@127.0.0.1:5432/jarvis"
else
    wait_for_external_postgres
fi

# --- Build TLS args ---
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

# --- Start memory-explorer ---
echo "[jarvis] Starting memory-explorer on port ${EXPLORER_PORT}..."
cd /app/memory-explorer
uvicorn app:app \
    --host 0.0.0.0 \
    --port "${EXPLORER_PORT}" \
    --log-level warning \
    --no-access-log &
EXPLORER_PID=$!

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
