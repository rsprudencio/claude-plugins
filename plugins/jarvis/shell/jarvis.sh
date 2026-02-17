#!/bin/bash
# Jarvis AI Assistant launcher
# Discovers installed plugins, concatenates system prompts, and launches Claude.
# Install to a PATH directory (e.g. ~/.local/bin/jarvis) and chmod +x.
# Source: https://github.com/rsprudencio/jarvis
set -e

system_prompt=""
settings_file=""
found_core=false

# Get installed plugin paths from Claude's plugin system (canonical source of truth)
plugin_paths=$(claude plugin list --json 2>/dev/null | python3 -c "
import sys, json
for p in json.load(sys.stdin):
    pid = p.get('id', '')
    if pid.startswith('jarvis'):
        print(f\"{pid.split('@')[0]}|{p['installPath']}\")
" 2>/dev/null) || true

while IFS='|' read -r name plugin_path; do
    [ -z "$name" ] && continue
    if [ -f "$plugin_path/system-prompt.md" ]; then
        system_prompt="${system_prompt}$(command cat "$plugin_path/system-prompt.md")"$'\n\n---\n\n'
        if [ "$name" = "jarvis" ]; then
            found_core=true
            settings_file="$plugin_path/settings.json"
        fi
    fi
done <<< "$plugin_paths"

# Require core plugin
if [ "$found_core" = false ]; then
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
    # Wait for health (up to 15s)
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

# Build extra args
extra_args=()
if [ -f "$settings_file" ]; then
    extra_args+=(--settings "$settings_file")
fi

# Launch Claude with concatenated prompts and settings
exec env __JARVIS_CLAUDE_STATUSLINE__=1 claude \
    --append-system-prompt "$system_prompt" \
    "${extra_args[@]}" \
    "$@"
