#!/bin/bash
# Jarvis MCP Transport Mode Switcher
# Switches between local (stdio), container (Docker on localhost), and remote modes.
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

remove_http_mcp() {
    claude mcp remove --scope user jarvis-core 2>/dev/null || true
    claude mcp remove --scope user jarvis-todoist-api 2>/dev/null || true
}

add_http_mcp() {
    local base_url="$1"
    claude mcp add --transport http --scope user jarvis-core "${base_url}:8741/mcp"
    claude mcp add --transport http --scope user jarvis-todoist-api "${base_url}:8742/mcp"
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
            info "Plugin .mcp.json servers are active"
            ;;
        container)
            info "Mode: ${CYAN}container${NC} (Docker on localhost)"
            info "MCP Core:    http://localhost:8741/mcp"
            info "MCP Todoist: http://localhost:8742/mcp"
            info "Plugin stdio servers will early-exit on startup"
            ;;
        remote)
            info "Mode: ${CYAN}remote${NC} (Docker on remote host)"
            info "Remote URL:  ${remote_url:-<not set>}"
            if [ -n "$remote_url" ]; then
                info "MCP Core:    ${remote_url}:8741/mcp"
                info "MCP Todoist: ${remote_url}:8742/mcp"
            fi
            info "Plugin stdio servers will early-exit on startup"
            ;;
        *)
            warn "Unknown mode: $mode"
            ;;
    esac
    echo ""
}

cmd_local() {
    echo ""
    echo -e "${BOLD}Switching to LOCAL mode${NC}"
    echo ""

    update_config "local"
    ok "Config updated: mcp_transport = local"

    remove_http_mcp
    ok "HTTP MCP entries removed"

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

    remove_http_mcp
    add_http_mcp "http://localhost"
    ok "HTTP MCP entries configured (localhost:8741, localhost:8742)"

    echo ""
    warn "Restart Claude Code to apply changes."
    info "Make sure your Docker container is running:"
    info "  docker compose -f ~/.jarvis/docker-compose.yml up -d"
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

    remove_http_mcp
    add_http_mcp "$url"
    ok "HTTP MCP entries configured (${url}:8741, ${url}:8742)"

    echo ""
    warn "Restart Claude Code to apply changes."
    info "Make sure the remote container is running and reachable at $url"
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
