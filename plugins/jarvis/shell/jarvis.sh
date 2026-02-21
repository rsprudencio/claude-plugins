#!/bin/bash
# Jarvis AI Assistant launcher
# Auto-starts Docker container and launches Claude with Jarvis plugins.
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

# Auto-start Docker container if compose file exists and health check fails
JARVIS_HOME="${JARVIS_HOME:-$HOME/.jarvis}"
compose_file="$JARVIS_HOME/docker-compose.yml"
if [ -f "$compose_file" ] && ! curl -sf http://localhost:8741/health > /dev/null 2>&1; then
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
