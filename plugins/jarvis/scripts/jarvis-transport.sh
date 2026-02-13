#!/bin/bash
# Jarvis MCP Transport Mode Switcher
# Switches between local (stdio), container (Docker on localhost), and remote modes.
# Rewrites .mcp.json files directly in the plugin cache — no `claude mcp add/remove` needed.
#
# Usage: jarvis-transport.sh <command>
#
# Commands:
#   status          Show current transport mode
#   local           Switch to local (stdio) mode
#   container       Switch to Docker container mode (localhost)
#   remote <url>    Switch to remote container mode
set -e

JARVIS_HOME="${JARVIS_HOME:-$HOME/.jarvis}"
CONFIG_FILE="$JARVIS_HOME/config.json"
CLAUDE_CFG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; }
warn() { echo -e "  ${YELLOW}!${NC} $1"; }
info() { echo -e "  ${BLUE}→${NC} $1"; }

# ── Helpers ──

read_config_key() {
    local key="$1" default="$2"
    if [ ! -f "$CONFIG_FILE" ]; then
        echo "$default"
        return
    fi
    python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    print(json.load(f).get(sys.argv[2], sys.argv[3]))
" "$CONFIG_FILE" "$key" "$default"
}

update_config() {
    local mode="$1" remote_url="${2:-}"
    if [ ! -f "$CONFIG_FILE" ]; then
        fail "Config file not found: $CONFIG_FILE"
        echo "  Run the Jarvis installer first or create a config with /jarvis-settings"
        exit 1
    fi
    python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    c = json.load(f)
c['mcp_transport'] = sys.argv[2]
if len(sys.argv) > 3 and sys.argv[3]:
    c['mcp_remote_url'] = sys.argv[3]
elif sys.argv[2] == 'local':
    c['mcp_remote_url'] = ''
with open(sys.argv[1], 'w') as f:
    json.dump(c, f, indent=2)
    f.write('\n')
" "$CONFIG_FILE" "$mode" "$remote_url"
}

# ── .mcp.json Cache Rewriting ──

# Write .mcp.json for a single plugin cache directory.
# Args: $1=dir $2=plugin_type(jarvis|jarvis-todoist) $3=mode(local|container|remote) $4=url(for remote)
rewrite_mcp_json() {
    local dir="$1" plugin_type="$2" mode="$3" url="${4:-}"

    # Plugin-specific values
    local mcp_key entry port
    if [ "$plugin_type" = "jarvis" ]; then
        mcp_key="core"
        entry="jarvis-tools"
        port="8741"
    else
        mcp_key="api"
        entry="jarvis-todoist-api"
        port="8742"
    fi

    local mcp_file="$dir/.mcp.json"

    python3 -c "
import json, sys
key, entry, port, mode, url = sys.argv[1:6]
if mode == 'local':
    server = {'command': 'uvx', 'args': ['--from', '\${CLAUDE_PLUGIN_ROOT}/mcp-server', entry]}
elif mode == 'container':
    server = {'type': 'http', 'url': f'http://localhost:{port}/mcp'}
elif mode == 'remote':
    base = url.rstrip('/')
    server = {'type': 'http', 'url': f'{base}:{port}/mcp'}
else:
    sys.exit(f'Unknown mode: {mode}')
with open(sys.argv[6], 'w') as f:
    json.dump({key: server}, f, indent=2)
    f.write('\n')
" "$mcp_key" "$entry" "$port" "$mode" "$url" "$mcp_file"
}

# Rewrite .mcp.json in all cached version directories for both plugins.
# Args: $1=mode $2=url(for remote)
rewrite_all_caches() {
    local mode="$1" url="${2:-}"
    local cache_base="$CLAUDE_CFG/plugins/cache/raph-claude-plugins"
    local count=0

    if [ ! -d "$cache_base" ]; then
        warn "Plugin cache not found at $cache_base"
        warn "Transport config saved; .mcp.json will be correct on next install."
        return
    fi

    for plugin_type in jarvis jarvis-todoist; do
        local plugin_dir="$cache_base/$plugin_type"
        [ -d "$plugin_dir" ] || continue

        for version_dir in "$plugin_dir"/*/; do
            [ -d "$version_dir" ] || continue
            rewrite_mcp_json "$version_dir" "$plugin_type" "$mode" "$url"
            count=$((count + 1))
        done
    done

    if [ "$count" -eq 0 ]; then
        warn "No plugin cache directories found to rewrite"
    else
        ok "Rewrote .mcp.json in $count cache director$([ "$count" -eq 1 ] && echo "y" || echo "ies")"
    fi
}

# Best-effort cleanup of stale user-scoped HTTP MCP entries from older transport.sh versions.
cleanup_user_mcp() {
    # Only attempt if claude CLI is available and we're NOT inside a session
    if [ -n "${CLAUDECODE:-}" ]; then
        return
    fi
    (unset CLAUDECODE && claude mcp remove --scope user jarvis-core 2>/dev/null) || true
    (unset CLAUDECODE && claude mcp remove --scope user jarvis-todoist-api 2>/dev/null) || true
}

# Show what .mcp.json format is active in the current installed version.
show_cache_info() {
    local cache_base="$CLAUDE_CFG/plugins/cache/raph-claude-plugins"

    for plugin_type in jarvis jarvis-todoist; do
        local plugin_dir="$cache_base/$plugin_type"
        [ -d "$plugin_dir" ] || continue

        # Find the latest version directory (last alphabetically)
        local latest
        latest=$(ls -1d "$plugin_dir"/*/ 2>/dev/null | sort -V | tail -1)
        [ -n "$latest" ] || continue

        local mcp_file="$latest.mcp.json"
        local version
        version=$(basename "$latest")

        if [ -f "$mcp_file" ]; then
            local format
            format=$(python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
for k, v in d.items():
    if 'command' in v:
        print(f'stdio (uvx)')
    elif v.get('type') == 'http':
        print(f'http ({v[\"url\"]})')
    else:
        print('unknown')
    break
" "$mcp_file" 2>/dev/null || echo "error reading")
            info "${plugin_type} v${version}: ${format}"
        fi
    done
}

# ── Commands ──

cmd_status() {
    echo ""
    echo -e "${BOLD}Jarvis MCP Transport Status${NC}"
    echo ""

    local mode
    mode=$(read_config_key "mcp_transport" "local")
    local remote_url
    remote_url=$(read_config_key "mcp_remote_url" "")

    case "$mode" in
        local)
            info "Mode: ${CYAN}local${NC} (stdio via uvx)"
            ;;
        container)
            info "Mode: ${CYAN}container${NC} (Docker on localhost)"
            info "MCP Core:    http://localhost:8741/mcp"
            info "MCP Todoist: http://localhost:8742/mcp"
            ;;
        remote)
            info "Mode: ${CYAN}remote${NC} (Docker on remote host)"
            info "Remote URL:  ${remote_url:-<not set>}"
            if [ -n "$remote_url" ]; then
                info "MCP Core:    ${remote_url}:8741/mcp"
                info "MCP Todoist: ${remote_url}:8742/mcp"
            fi
            ;;
        *)
            warn "Unknown mode: $mode"
            ;;
    esac

    echo ""
    echo -e "${BOLD}Plugin Cache${NC}"
    echo ""
    show_cache_info
    echo ""
}

cmd_local() {
    echo ""
    echo -e "${BOLD}Switching to LOCAL mode${NC}"
    echo ""

    update_config "local"
    ok "Config updated: mcp_transport = local"

    rewrite_all_caches "local"
    cleanup_user_mcp

    # Stop Docker container if running (no longer needed in local mode)
    local compose_file="$JARVIS_HOME/docker-compose.yml"
    if [ -f "$compose_file" ]; then
        if docker compose -f "$compose_file" ps --quiet 2>/dev/null | grep -q .; then
            info "Stopping Docker container (no longer needed in local mode)..."
            docker compose -f "$compose_file" down 2>/dev/null || true
        fi
    fi

    echo ""
    warn "Restart Claude Code to apply changes."
    echo ""
}

cmd_container() {
    echo ""
    echo -e "${BOLD}Switching to CONTAINER mode${NC}"
    echo ""

    update_config "container"
    ok "Config updated: mcp_transport = container"

    rewrite_all_caches "container"
    cleanup_user_mcp

    # Auto-start Docker container
    local compose_file="$JARVIS_HOME/docker-compose.yml"
    if [ -f "$compose_file" ]; then
        info "Starting Docker container..."
        if docker compose -f "$compose_file" up -d 2>&1; then
            # Wait for health check (up to 15s)
            for i in $(seq 1 15); do
                if curl -sf http://localhost:8741/health > /dev/null 2>&1; then
                    ok "Jarvis container is healthy"
                    break
                fi
                sleep 1
            done
            if ! curl -sf http://localhost:8741/health > /dev/null 2>&1; then
                warn "Container started but health check timed out"
                info "Check: docker compose -f $compose_file logs"
            fi
        else
            warn "Docker compose failed — check: docker compose -f $compose_file logs"
        fi
    else
        warn "No docker-compose.yml found at $compose_file"
        info "Run the installer with Docker option to generate one."
    fi

    echo ""
    warn "Restart Claude Code to apply changes."
    echo ""
}

cmd_remote() {
    local url="$1"
    if [ -z "$url" ]; then
        fail "Usage: jarvis-transport.sh remote <url>"
        echo "  Example: jarvis-transport.sh remote http://192.168.1.50"
        exit 1
    fi

    # Strip trailing slash
    url="${url%/}"

    echo ""
    echo -e "${BOLD}Switching to REMOTE mode${NC}"
    echo ""

    update_config "remote" "$url"
    ok "Config updated: mcp_transport = remote, mcp_remote_url = $url"

    rewrite_all_caches "remote" "$url"
    cleanup_user_mcp

    # Health check the remote server
    info "Checking remote server health..."
    if curl -sf "${url}:8741/health" > /dev/null 2>&1; then
        ok "Remote server is healthy at ${url}:8741"
    else
        warn "Remote server not reachable at ${url}:8741"
        info "Make sure the container is running on the remote host."
    fi

    echo ""
    warn "Restart Claude Code to apply changes."
    echo ""
}

# ── Main ──

case "${1:-}" in
    status)    cmd_status ;;
    local)     cmd_local ;;
    container) cmd_container ;;
    remote)    cmd_remote "${2:-}" ;;
    -h|--help|help|"")
        echo ""
        echo -e "${BOLD}Jarvis MCP Transport Switcher${NC}"
        echo ""
        echo "Usage: jarvis-transport.sh <command>"
        echo ""
        echo "Commands:"
        echo "  status          Show current transport mode"
        echo "  local           Switch to local (stdio) mode"
        echo "  container       Switch to Docker container mode (localhost)"
        echo "  remote <url>    Switch to remote container mode"
        echo ""
        echo "Examples:"
        echo "  jarvis-transport.sh status"
        echo "  jarvis-transport.sh container"
        echo "  jarvis-transport.sh remote http://192.168.1.50"
        echo "  jarvis-transport.sh local"
        echo ""
        ;;
    *)
        fail "Unknown command: $1"
        echo "  Run 'jarvis-transport.sh --help' for usage"
        exit 1
        ;;
esac
