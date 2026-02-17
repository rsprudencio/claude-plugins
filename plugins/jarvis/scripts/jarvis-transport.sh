#!/bin/bash
# Jarvis MCP Service Manager
# Manages ChromaDB lifecycle and shows service status.
#
# Usage: jarvis-transport.sh <command>
#
# Commands:
#   status          Show service status
#   chroma-start    Start local ChromaDB server
#   chroma-stop     Stop local ChromaDB server
#   chroma-status   Check ChromaDB server status
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

read_memory_key() {
    local key="$1" default="$2"
    if [ ! -f "$CONFIG_FILE" ]; then
        echo "$default"
        return
    fi
    python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    print(json.load(f).get('memory', {}).get(sys.argv[2], sys.argv[3]))
" "$CONFIG_FILE" "$key" "$default"
}

# ── Commands ──

cmd_status() {
    echo ""
    echo -e "${BOLD}Jarvis Service Status${NC}"
    echo ""

    # MCP server health
    echo -e "${BOLD}MCP Servers${NC}"
    echo ""
    info "MCP Core:    http://localhost:8741/mcp"
    info "MCP Todoist: http://localhost:8742/mcp"

    if curl -sf http://localhost:8741/health > /dev/null 2>&1; then
        ok "Core server healthy"
    else
        fail "Core server not reachable"
    fi
    if curl -sf http://localhost:8742/health > /dev/null 2>&1; then
        ok "Todoist server healthy"
    else
        warn "Todoist server not reachable (may not be configured)"
    fi

    # ChromaDB status
    echo ""
    echo -e "${BOLD}ChromaDB${NC}"
    echo ""
    local chroma_port
    chroma_port=$(read_memory_key "chroma_port" "8743")
    local chroma_pidfile="$JARVIS_HOME/state/chroma.pid"

    if [ -f "$chroma_pidfile" ] && kill -0 "$(cat "$chroma_pidfile")" 2>/dev/null; then
        ok "Local server running (PID $(cat "$chroma_pidfile"), port $chroma_port)"
    elif curl -sf "http://127.0.0.1:${chroma_port}/api/v2/heartbeat" >/dev/null 2>&1; then
        ok "Reachable on port $chroma_port"
    else
        fail "Not running on port $chroma_port"
    fi

    # Docker container status
    echo ""
    echo -e "${BOLD}Docker${NC}"
    echo ""
    local compose_file="$JARVIS_HOME/docker-compose.yml"
    if [ -f "$compose_file" ]; then
        if docker compose -f "$compose_file" ps --quiet 2>/dev/null | grep -q .; then
            ok "Container running"
        else
            fail "Container not running"
        fi
    else
        info "No docker-compose.yml found"
    fi
    echo ""
}

# ── ChromaDB Lifecycle ──

cmd_chroma_start() {
    local data_path port pidfile logfile
    data_path=$(read_memory_key "chroma_data_path" "$HOME/.jarvis/db")
    data_path="${data_path/#\~/$HOME}"
    port=$(read_memory_key "chroma_port" "8743")
    pidfile="$JARVIS_HOME/state/chroma.pid"
    logfile="$JARVIS_HOME/logs/chroma.log"

    if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        ok "ChromaDB already running (PID $(cat "$pidfile"), port $port)"
        return 0
    fi

    # Verify chroma CLI is available
    if ! command -v chroma >/dev/null 2>&1; then
        fail "chroma CLI not found"
        info "Install: pip install chromadb"
        return 1
    fi

    mkdir -p "$data_path" "$(dirname "$pidfile")" "$(dirname "$logfile")"

    chroma run --host 127.0.0.1 --port "$port" --path "$data_path" \
        > "$logfile" 2>&1 &
    echo $! > "$pidfile"

    # Wait for health
    for i in $(seq 1 15); do
        if curl -sf "http://127.0.0.1:${port}/api/v2/heartbeat" >/dev/null 2>&1; then
            ok "ChromaDB started (PID $(cat "$pidfile"), port $port)"
            return 0
        fi
        sleep 1
    done

    fail "ChromaDB failed to start (check $logfile)"
    # Clean up pidfile if server didn't come up
    rm -f "$pidfile"
    return 1
}

cmd_chroma_stop() {
    local pidfile="$JARVIS_HOME/state/chroma.pid"

    if [ ! -f "$pidfile" ]; then
        info "ChromaDB is not running (no pidfile)"
        return 0
    fi

    local pid
    pid=$(cat "$pidfile")

    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null
        # Wait for shutdown
        local timeout=5
        while [ $timeout -gt 0 ] && kill -0 "$pid" 2>/dev/null; do
            sleep 1
            timeout=$((timeout - 1))
        done
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
        fi
        ok "ChromaDB stopped (was PID $pid)"
    else
        info "ChromaDB process already gone (stale pidfile)"
    fi

    rm -f "$pidfile"
}

cmd_chroma_status() {
    local port pidfile
    port=$(read_memory_key "chroma_port" "8743")
    pidfile="$JARVIS_HOME/state/chroma.pid"

    echo ""
    echo -e "${BOLD}ChromaDB Status${NC}"
    echo ""

    if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        ok "Running (PID $(cat "$pidfile"))"
    else
        fail "Not running"
    fi

    if curl -sf "http://127.0.0.1:${port}/api/v2/heartbeat" >/dev/null 2>&1; then
        ok "Healthy on port $port"
    else
        fail "Not reachable on port $port"
    fi

    echo ""
}

# ── Main ──

case "${1:-}" in
    status)        cmd_status ;;
    chroma-start)  cmd_chroma_start ;;
    chroma-stop)   cmd_chroma_stop ;;
    chroma-status) cmd_chroma_status ;;
    -h|--help|help|"")
        echo ""
        echo -e "${BOLD}Jarvis MCP Service Manager${NC}"
        echo ""
        echo "Usage: jarvis-transport.sh <command>"
        echo ""
        echo "Commands:"
        echo "  status          Show service status (MCP, ChromaDB, Docker)"
        echo "  chroma-start    Start local ChromaDB server"
        echo "  chroma-stop     Stop local ChromaDB server"
        echo "  chroma-status   Check ChromaDB server status"
        echo ""
        echo "Examples:"
        echo "  jarvis-transport.sh status"
        echo "  jarvis-transport.sh chroma-start"
        echo ""
        ;;
    *)
        fail "Unknown command: $1"
        echo "  Run 'jarvis-transport.sh --help' for usage"
        exit 1
        ;;
esac
