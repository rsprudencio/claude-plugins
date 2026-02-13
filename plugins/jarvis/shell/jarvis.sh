#!/bin/bash
# Jarvis AI Assistant launcher
# Discovers installed plugins, concatenates system prompts, and launches Claude.
# Install to a PATH directory (e.g. ~/.local/bin/jarvis) and chmod +x.
# Source: https://github.com/rsprudencio/claude-plugins
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
    echo "Install with: claude plugin install jarvis@raph-claude-plugins"
    exit 1
fi

# Reconcile .mcp.json in plugin cache to match config's mcp_transport.
# This self-heals after reinstalls/auto-updates that reset .mcp.json to defaults.
JARVIS_HOME="${JARVIS_HOME:-$HOME/.jarvis}"
JARVIS_CONFIG="$JARVIS_HOME/config.json"
if [ -f "$JARVIS_CONFIG" ]; then
    python3 -c "
import json, sys, os

config_path = sys.argv[1]
plugin_paths_raw = sys.argv[2]

with open(config_path) as f:
    cfg = json.load(f)
transport = cfg.get('mcp_transport', 'local')
remote_url = cfg.get('mcp_remote_url', '').rstrip('/')

# Plugin name -> (mcp_key, entry_point, port)
PLUGIN_MAP = {
    'jarvis': ('core', 'jarvis-tools', '8741'),
    'jarvis-todoist': ('api', 'jarvis-todoist-api', '8742'),
}

for line in plugin_paths_raw.strip().splitlines():
    if '|' not in line:
        continue
    name, path = line.split('|', 1)
    if name not in PLUGIN_MAP:
        continue
    mcp_key, entry, port = PLUGIN_MAP[name]
    mcp_file = os.path.join(path, '.mcp.json')
    if not os.path.exists(mcp_file):
        continue

    # Build expected config
    if transport == 'local':
        expected = {mcp_key: {'command': 'uvx', 'args': ['--from', '\${CLAUDE_PLUGIN_ROOT}/mcp-server', entry]}}
    elif transport == 'container':
        expected = {mcp_key: {'type': 'http', 'url': f'http://localhost:{port}/mcp'}}
    elif transport == 'remote' and remote_url:
        expected = {mcp_key: {'type': 'http', 'url': f'{remote_url}:{port}/mcp'}}
    else:
        continue

    # Check and fix if mismatched
    with open(mcp_file) as f:
        current = json.load(f)
    if current != expected:
        with open(mcp_file, 'w') as f:
            json.dump(expected, f, indent=2)
            f.write('\n')
        print(f'Synced {name} plugin to {transport} transport')
" "$JARVIS_CONFIG" "$plugin_paths" 2>/dev/null || true

    # Read transport mode for container auto-start
    transport=$(python3 -c "import json; print(json.load(open('$JARVIS_CONFIG')).get('mcp_transport','local'))" 2>/dev/null || echo "local")

    if [ "$transport" = "container" ]; then
        if ! curl -sf http://localhost:8741/health > /dev/null 2>&1; then
            compose_file="$JARVIS_HOME/docker-compose.yml"
            if [ -f "$compose_file" ]; then
                # Pull version-matched image before starting
                plugin_version=""
                while IFS='|' read -r pname ppath; do
                    if [ "$pname" = "jarvis" ] && [ -f "$ppath/.claude-plugin/plugin.json" ]; then
                        plugin_version=$(python3 -c "import json; print(json.load(open('$ppath/.claude-plugin/plugin.json'))['version'])" 2>/dev/null || true)
                        break
                    fi
                done <<< "$plugin_paths"
                if [ -n "$plugin_version" ]; then
                    echo "Pulling jarvis:$plugin_version..."
                    docker pull "ghcr.io/rsprudencio/jarvis:$plugin_version" 2>/dev/null && \
                        docker tag "ghcr.io/rsprudencio/jarvis:$plugin_version" "ghcr.io/rsprudencio/jarvis:latest" 2>/dev/null || true
                fi
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
            else
                echo "Warning: Container mode but no docker-compose.yml found at $compose_file"
            fi
        fi
    elif [ "$transport" = "remote" ]; then
        remote_url=$(python3 -c "import json; print(json.load(open('$JARVIS_CONFIG')).get('mcp_remote_url',''))" 2>/dev/null || echo "")
        if [ -n "$remote_url" ] && ! curl -sf "${remote_url}:8741/health" > /dev/null 2>&1; then
            echo "Warning: Remote server not reachable at ${remote_url}:8741"
        fi
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
