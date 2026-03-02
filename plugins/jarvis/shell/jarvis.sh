#!/bin/bash
# Jarvis AI Assistant launcher
# Auto-starts PostgreSQL + Docker container and launches Claude with Jarvis plugins.
# Install to a PATH directory (e.g. ~/.local/bin/jarvis) and chmod +x.
# Source: https://github.com/rsprudencio/jarvis
set -e

# Verify core plugin is installed
if ! claude plugin list --json 2>/dev/null | python3 -c "
import sys, json
plugins = json.load(sys.stdin)
if not any(p.get('id', '').startswith('jarvis@') for p in plugins):
    sys.exit(1)
" 2>/dev/null; then
    echo "Error: Jarvis core plugin not installed."
    echo "Install with: claude plugin install jarvis@jarvis-plugins"
    exit 1
fi

JARVIS_HOME="${JARVIS_HOME:-$HOME/.jarvis}"
compose_file="$JARVIS_HOME/docker-compose.yml"

# ── Ensure PostgreSQL is available ──────────────────────────────────
# Strategy: check pg_isready (native), then Docker, then compose up.
ensure_postgres() {
    # 1. Check if PG is already reachable (native or Docker)
    if command -v pg_isready >/dev/null 2>&1 && pg_isready -q 2>/dev/null; then
        return 0
    fi

    # 2. Check if Docker PG container is running and healthy
    if docker compose -f "$compose_file" ps --format json 2>/dev/null | \
       python3 -c "
import sys, json
for line in sys.stdin:
    svc = json.loads(line)
    if 'postgres' in svc.get('Service','') and svc.get('Health','') == 'healthy':
        sys.exit(0)
sys.exit(1)
" 2>/dev/null; then
        return 0
    fi

    # 3. Start via docker compose
    if [ -f "$compose_file" ]; then
        echo "Starting PostgreSQL..."
        docker compose -f "$compose_file" up -d postgres 2>&1
        for i in $(seq 1 15); do
            if docker compose -f "$compose_file" exec -T postgres pg_isready -U jarvis -q 2>/dev/null; then
                echo "PostgreSQL is ready."
                return 0
            fi
            sleep 1
        done
        echo "Warning: PostgreSQL started but readiness check timed out."
        return 1
    fi

    echo "Warning: PostgreSQL not found. Install via Homebrew or Docker."
    echo "  macOS:  brew install postgresql@17 pgvector"
    echo "  Docker: docker compose -f $compose_file up -d"
    return 1
}

# ── Auto-start MCP server ──────────────────────────────────────────
if [ -f "$compose_file" ] && ! curl -sf http://localhost:8741/health > /dev/null 2>&1; then
    ensure_postgres
    echo "Starting Jarvis container..."
    docker compose -f "$compose_file" up -d 2>&1
    for i in $(seq 1 15); do
        if curl -sf http://localhost:8741/health > /dev/null 2>&1; then
            echo "Container is healthy."
            break
        fi
        sleep 1
    done
    if ! curl -sf http://localhost:8741/health > /dev/null 2>&1; then
        echo "Warning: Container started but health check failed."
        echo "Check: docker compose -f $compose_file logs"
    fi
fi

# Launch Claude — MCP servers inject instructions automatically
exec claude "$@"
