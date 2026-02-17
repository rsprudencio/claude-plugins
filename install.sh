#!/bin/bash
# Jarvis Plugin Installer
# curl -fsSL https://raw.githubusercontent.com/rsprudencio/jarvis/refs/heads/master/install.sh | bash
set -e

JARVIS_HOME="${JARVIS_HOME:-$HOME/.jarvis}"
MARKETPLACE_NAME="jarvis-plugins"
MARKETPLACE_REPO="https://github.com/rsprudencio/jarvis"

# ── Interactive input setup ──
# When piped (curl | bash), stdin is the script — we need /dev/tty for prompts
HAS_TTY=false
if (exec </dev/tty) 2>/dev/null; then
    exec 3</dev/tty
    HAS_TTY=true
else
    exec 3</dev/null
fi

# Read from terminal (fd 3), fall back to default if unavailable
ask() {
    local prompt="$1" varname="$2" default="$3"
    if [ "$HAS_TTY" = true ]; then
        read -r -p "$prompt" "$varname" <&3 || true
    fi
    eval "local val=\$$varname"
    if [ -z "$val" ]; then
        eval "$varname='$default'"
    fi
}

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

if [ "$HAS_TTY" = false ]; then
    warn "No terminal detected — using all defaults (non-interactive mode)"
    echo ""
fi

echo -e "${CYAN}"
echo "  ╦╔═╗╦═╗╦  ╦╦╔═╗"
echo "  ║╠═╣╠╦╝╚╗╔╝║╚═╗"
echo " ╚╝╩ ╩╩╚═ ╚╝ ╩╚═╝"
echo -e "${NC}"
echo -e "  ${BOLD}AI Assistant Plugin Installer${NC}"
echo ""

# ═══════════════════════════════════════════════
# 📦 Install Core Plugin
# ═══════════════════════════════════════════════

DOCKER_IMAGE="ghcr.io/rsprudencio/jarvis:latest"

# Verify Claude CLI exists (silent check)
if ! command -v claude >/dev/null 2>&1; then
    fail "Claude Code CLI not found"
    echo ""
    echo -e "  Install Claude Code first: ${BLUE}https://claude.ai/code${NC}"
    echo ""
    exit 1
fi

# Verify Python 3 exists (silent check)
if command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD="python3"
else
    fail "Python 3 not found"
    echo ""
    echo -e "  ${BOLD}macOS:${NC} ${BLUE}brew install python@3.12${NC}"
    echo -e "  ${BOLD}Linux:${NC} ${BLUE}sudo apt install python3${NC} (Debian/Ubuntu)"
    echo -e "  Or download from: ${BLUE}https://python.org/downloads/${NC}"
    echo ""
    exit 1
fi

echo -e "${BOLD}📦 Install Core Plugin${NC}"
echo ""

# Add marketplace (may fail if already added — that's OK)
claude plugin marketplace add rsprudencio/jarvis >/dev/null 2>&1 || true

# Verify marketplace is available
if ! claude plugin marketplace list 2>/dev/null | grep -q "jarvis-plugins"; then
    fail "Could not add marketplace"
    echo -e "  Run manually: ${BLUE}claude plugin marketplace add rsprudencio/jarvis${NC}"
    exit 1
fi

# Install core plugin
echo -e "  Installing ${BLUE}jarvis@jarvis-plugins${NC}..."
claude plugin install jarvis@jarvis-plugins >/dev/null 2>&1 || {
    fail "Could not install jarvis plugin"
    echo ""
    echo -e "  Try manually: ${BLUE}claude plugin install jarvis@jarvis-plugins${NC}"
    exit 1
}

ok "Plugin installed"
echo ""

# ═══════════════════════════════════════════════
# ✅ Check Prerequisites
# ═══════════════════════════════════════════════

# ═══════════════════════════════════════════════
# ✅ Check Prerequisites
# ═══════════════════════════════════════════════

echo -e "${BOLD}✅ Check Prerequisites${NC}"
echo ""

# Docker is required
HAS_DOCKER=false
if command -v docker >/dev/null 2>&1; then
    if docker compose version >/dev/null 2>&1; then
        HAS_DOCKER=true
    fi
fi

if [ "$HAS_DOCKER" = false ]; then
    fail "Docker with Compose not found"
    echo ""
    echo -e "  Jarvis requires Docker to run the MCP server."
    echo -e "  Install: ${BLUE}https://docs.docker.com/get-docker/${NC}"
    echo ""
    exit 1
fi
ok "Docker with Compose found"
echo ""

# ═══════════════════════════════════════════════
# 🧩 Install Optional Extensions
# ═══════════════════════════════════════════════

echo -e "${BOLD}🧩 Install Optional Extensions${NC}"
echo ""
echo -e "  ${BOLD}Available extensions:${NC}"
echo -e "    ${CYAN}[1]${NC} jarvis-todoist   — Task management via Todoist"
echo -e "    ${CYAN}[2]${NC} jarvis-strategic — Strategic analysis & briefings"
echo -e "    ${CYAN}[3]${NC} Both"
echo -e "    ${CYAN}[4]${NC} Skip"
echo ""
ask "  Choice [4]: " EXT_CHOICE "4"

case "$EXT_CHOICE" in
    1)
        claude plugin install "jarvis-todoist@$MARKETPLACE_NAME" 2>/dev/null && ok "jarvis-todoist installed" || warn "jarvis-todoist install failed"
        ;;
    2)
        claude plugin install "jarvis-strategic@$MARKETPLACE_NAME" 2>/dev/null && ok "jarvis-strategic installed" || warn "jarvis-strategic install failed"
        ;;
    3)
        claude plugin install "jarvis-todoist@$MARKETPLACE_NAME" 2>/dev/null && ok "jarvis-todoist installed" || warn "jarvis-todoist install failed"
        claude plugin install "jarvis-strategic@$MARKETPLACE_NAME" 2>/dev/null && ok "jarvis-strategic installed" || warn "jarvis-strategic install failed"
        ;;
    *)
        info "Skipping extensions"
        ;;
esac

echo ""

# Resolve installed plugin directory from Claude's plugin system (post-extensions)
PLUGIN_DIR=$(claude plugin list --json 2>/dev/null | $PYTHON_CMD -c "
import sys, json
for p in json.load(sys.stdin):
    if p.get('id', '').startswith('jarvis@'):
        print(p['installPath'])
        break
" 2>/dev/null)

if [ -z "$PLUGIN_DIR" ] || [ ! -d "$PLUGIN_DIR" ]; then
    fail "Plugin directory not found"
    echo "    Run: claude plugin list --json"
    echo "    Try reinstalling: claude plugin install jarvis@$MARKETPLACE_NAME"
    exit 1
fi


# ═══════════════════════════════════════════════
# ⚙️  Configure Jarvis
# ═══════════════════════════════════════════════

echo -e "${BOLD}⚙️  Configure Jarvis${NC}"
echo ""

# Check for existing config
if [ -f "$JARVIS_HOME/config.json" ]; then
    EXISTING_VAULT=$(python3 -c "import json; print(json.load(open('$JARVIS_HOME/config.json')).get('vault_path', 'not set'))" 2>/dev/null || echo "not set")
    echo -e "  Existing config found (vault: ${CYAN}$EXISTING_VAULT${NC})"
    ask "  Reconfigure? [y/N]: " RECONFIG "N"
    if [ "$RECONFIG" != "y" ] && [ "$RECONFIG" != "Y" ]; then
        info "Keeping existing config\n"
        VAULT_PATH="$EXISTING_VAULT"
        SKIP_CONFIG=true
    fi
fi

if [ "$SKIP_CONFIG" != true ]; then
    echo "  Where should Jarvis store your knowledge vault?"
    echo -e "  ${BOLD}(A vault is a folder of markdown files — like an Obsidian vault)${NC}"
    echo ""

    # Detect common vault locations
    VAULT_OPTIONS=()
    VAULT_LABELS=()

    # Always offer starter vault
    VAULT_OPTIONS+=("$HOME/.jarvis/vault")
    VAULT_LABELS+=("~/.jarvis/vault/ (starter vault — good for trying Jarvis)")

    # Detect Obsidian vaults
    for candidate in "$HOME/Documents/Obsidian" "$HOME/Documents/obsidian" "$HOME/Obsidian" "$HOME/obsidian" "$HOME/vaults" "$HOME/notes"; do
        if [ -d "$candidate" ]; then
            VAULT_OPTIONS+=("$candidate")
            VAULT_LABELS+=("$candidate/ (detected)")
        fi
    done

    # Display options
    for i in "${!VAULT_LABELS[@]}"; do
        echo -e "    ${CYAN}[$((i+1))]${NC} ${VAULT_LABELS[$i]}"
    done
    CUSTOM_IDX=$((${#VAULT_OPTIONS[@]} + 1))
    echo -e "    ${CYAN}[$CUSTOM_IDX]${NC} Enter custom path"
    echo ""

    ask "  Choice [1]: " VAULT_CHOICE "1"

    # Check if VAULT_CHOICE is a valid number
    if [[ "$VAULT_CHOICE" =~ ^[0-9]+$ ]]; then
        # It's a number - use menu logic
        if [ "$VAULT_CHOICE" -eq "$CUSTOM_IDX" ]; then
            ask "  Enter vault path: " VAULT_PATH "$HOME/.jarvis/vault"
            # Expand ~ if present
            VAULT_PATH="${VAULT_PATH/#\~/$HOME}"
        elif [ "$VAULT_CHOICE" -ge 1 ] && [ "$VAULT_CHOICE" -le "${#VAULT_OPTIONS[@]}" ]; then
            VAULT_PATH="${VAULT_OPTIONS[$((VAULT_CHOICE-1))]}"
        else
            # Invalid number - use default
            VAULT_PATH="${VAULT_OPTIONS[0]}"
        fi
    else
        # User pasted a path directly - treat as custom input
        VAULT_PATH="$VAULT_CHOICE"
        # Expand ~ if present
        VAULT_PATH="${VAULT_PATH/#\~/$HOME}"
    fi

    ok "Vault: $VAULT_PATH"
    echo ""

    # File format selection
    echo -e "  ${BOLD}File Format${NC}"
    echo "  Choose the format for new vault files (existing files are always readable)."
    echo ""
    echo -e "    ${CYAN}[1]${NC} Markdown (.md) — Standard, works with Obsidian (recommended)"
    echo -e "    ${CYAN}[2]${NC} Org-mode (.org) — For Emacs/Org-mode users"
    echo ""
    ask "  Choice [1]: " FORMAT_CHOICE "1"

    case "$FORMAT_CHOICE" in
        2)
            FILE_FORMAT="org"
            ok "Format: Org-mode (.org)"
            ;;
        *)
            FILE_FORMAT="md"
            ok "Format: Markdown (.md)"
            ;;
    esac
    echo ""
fi

# Shell integration (always offer executable install, independent of config)
echo -e "  ${BOLD}Shell Integration${NC}"
echo "  The 'jarvis' command launches Claude with your Jarvis identity."
echo -e "  ${YELLOW}⚠️  Highly recommended — this is the only way to make Claude fully impersonate Jarvis.${NC}"
echo ""
ask "  Install 'jarvis' command to your PATH? [Y/n]: " SHELL_SETUP "Y"

echo ""

# ═══════════════════════════════════════════════
# 💾 Write Configuration
# ═══════════════════════════════════════════════

mkdir -p "$JARVIS_HOME"

if [ "$SKIP_CONFIG" != true ]; then
    echo -e "${BOLD}💾 Write Configuration${NC}"
    echo ""
    # Create vault directory
    mkdir -p "$VAULT_PATH"
    ok "Vault directory ready: $VAULT_PATH"

    # Create memory DB directory
    mkdir -p "$JARVIS_HOME/db"
    ok "Memory DB directory ready: $JARVIS_HOME/db/"

    # Write config from shipped template (SSoT) with user values substituted
    TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    TEMPLATE="$PLUGIN_DIR/defaults/config.json"
    if [ -f "$TEMPLATE" ]; then
        $PYTHON_CMD -c "
import json, sys
with open(sys.argv[1]) as f:
    cfg = json.load(f)
cfg['vault_path'] = sys.argv[2]
cfg['vault_confirmed'] = True
cfg['configured_at'] = sys.argv[3]
cfg['file_format'] = sys.argv[4]
json.dump(cfg, sys.stdout, indent=2)
" "$TEMPLATE" "$VAULT_PATH" "$TIMESTAMP" "${FILE_FORMAT:-md}" > "$JARVIS_HOME/config.json"
    else
        # Fallback: minimal config if template not found in plugin distribution
        cat > "$JARVIS_HOME/config.json" << FALLBACKEOF
{
  "vault_path": "$VAULT_PATH",
  "vault_confirmed": true,
  "configured_at": "$TIMESTAMP"
}
FALLBACKEOF
    fi
    ok "Config written: $JARVIS_HOME/config.json (all defaults visible)"
fi

# Shell integration (independent of config — always runs if user said Y)
if [ "$SHELL_SETUP" = "Y" ] || [ "$SHELL_SETUP" = "y" ]; then
    # Clean up old shell function injection if present
    for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile"; do
        if grep -q "# Jarvis AI Assistant START" "$rc" 2>/dev/null; then
            sed -i.bak '/# Jarvis AI Assistant START/,/# Jarvis AI Assistant END/d' "$rc"
            rm -f "$rc.bak"
            info "Removed old shell function from $rc"
        fi
    done

    # Detect best install directory
    INSTALL_DIR=""
    if [ -d "$HOME/.local/bin" ] && echo "$PATH" | grep -q "$HOME/.local/bin"; then
        INSTALL_DIR="$HOME/.local/bin"
    elif [ -d "/usr/local/bin" ] && [ -w "/usr/local/bin" ]; then
        INSTALL_DIR="/usr/local/bin"
    else
        INSTALL_DIR="$HOME/.local/bin"
        mkdir -p "$INSTALL_DIR"
    fi

    # Install the jarvis executable
    SHELL_SCRIPT="$PLUGIN_DIR/shell/jarvis.sh"
    if [ -f "$SHELL_SCRIPT" ]; then
        cp "$SHELL_SCRIPT" "$INSTALL_DIR/jarvis"
        chmod +x "$INSTALL_DIR/jarvis"
        ok "Installed: $INSTALL_DIR/jarvis"
    else
        warn "jarvis.sh not found at $SHELL_SCRIPT"
    fi

    # Check if directory is in PATH
    if ! command -v jarvis >/dev/null 2>&1; then
        warn "$INSTALL_DIR is not in your PATH"
        info "Add to your shell config: export PATH=\"$INSTALL_DIR:\$PATH\""
    fi
else
    info "Skipping shell integration"
    echo -e "  You can still activate Jarvis inside any Claude session by typing: ${BLUE}/jarvis:jarvis${NC}"
fi

echo ""

# ═══════════════════════════════════════════════
# 🐳 Docker Setup (if Docker method selected)
# ═══════════════════════════════════════════════

echo -e "${BOLD}🐳 Docker Setup${NC}"
echo ""

# Optional Todoist API token
echo -e "  ${BOLD}Todoist Integration (optional)${NC}"
echo "  If you use Todoist, enter your API token for task management."
echo -e "  Get one at: ${BLUE}https://app.todoist.com/app/settings/integrations/developer${NC}"
echo ""
ask "  Todoist API token (press Enter to skip): " TODOIST_TOKEN ""

if [ -n "$TODOIST_TOKEN" ]; then
    ok "Todoist token saved"
else
    info "Skipping Todoist (can add later in config)"
fi
echo ""

# Pull Docker image
info "Pulling Docker image: $DOCKER_IMAGE"
if docker pull "$DOCKER_IMAGE" 2>&1 | tail -2; then
    ok "Docker image pulled"
else
    warn "Could not pull image from GHCR"
    info "You may need to build locally: docker build -f docker/Dockerfile -t jarvis-local ."
    echo ""
    ask "  Continue without Docker image? [y/N]: " CONTINUE_NO_IMAGE "N"
    if [ "$CONTINUE_NO_IMAGE" != "y" ] && [ "$CONTINUE_NO_IMAGE" != "Y" ]; then
        fail "Docker image required. Build locally or check your network."
        exit 1
    fi
    DOCKER_IMAGE="jarvis-local"
fi
echo ""

# Write docker-compose.yml for user
COMPOSE_FILE="$JARVIS_HOME/docker-compose.yml"
cat > "$COMPOSE_FILE" << COMPOSEEOF
services:
  jarvis:
    image: $DOCKER_IMAGE
    ports:
      - "8741:8741"
      - "8742:8742"
      - "8743:8743"
    volumes:
      - "$VAULT_PATH:/vault"
      - "$JARVIS_HOME:/config"
    environment:
      - JARVIS_HOME=/config
      - JARVIS_VAULT_PATH=/vault
      - CHROMA_HOST=127.0.0.1
      - CHROMA_PORT=8743
      - TODOIST_API_TOKEN=${TODOIST_TOKEN:-}
      - JARVIS_AUTOCRLF=false
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:8741/health"]
      interval: 30s
      timeout: 5s
      start_period: 20s
      retries: 3
COMPOSEEOF
ok "Docker Compose file: $COMPOSE_FILE"

# Start the container
info "Starting Jarvis container..."
if docker compose -f "$COMPOSE_FILE" up -d 2>&1; then
    # Wait for health
    HEALTH_OK=false
    for i in $(seq 1 30); do
        if curl -sf http://localhost:8741/health > /dev/null 2>&1; then
            HEALTH_OK=true
            break
        fi
        sleep 1
    done

    if [ "$HEALTH_OK" = true ]; then
        ok "Jarvis MCP server is running"
        HEALTH_RESP=$(curl -sf http://localhost:8741/health 2>/dev/null)
        info "$HEALTH_RESP"
    else
        warn "Container started but health check failed — check: docker compose -f $COMPOSE_FILE logs"
    fi
else
    fail "Docker compose failed to start"
    echo "  Debug: docker compose -f $COMPOSE_FILE logs"
fi
echo ""

# Write management helper script
HELPER_SCRIPT="$JARVIS_HOME/jarvis-docker.sh"
cat > "$HELPER_SCRIPT" << 'HELPEREOF'
#!/bin/bash
# Jarvis Docker management helper
COMPOSE_FILE="${JARVIS_HOME:-$HOME/.jarvis}/docker-compose.yml"

case "${1:-status}" in
    start)   docker compose -f "$COMPOSE_FILE" up -d ;;
    stop)    docker compose -f "$COMPOSE_FILE" down ;;
    restart) docker compose -f "$COMPOSE_FILE" restart ;;
    logs)    docker compose -f "$COMPOSE_FILE" logs -f --tail=50 ;;
    status)  docker compose -f "$COMPOSE_FILE" ps ;;
    update)
        docker compose -f "$COMPOSE_FILE" pull
        docker compose -f "$COMPOSE_FILE" up -d
        ;;
    *)
        echo "Usage: jarvis-docker.sh {start|stop|restart|logs|status|update}"
        exit 1
        ;;
esac
HELPEREOF
chmod +x "$HELPER_SCRIPT"
ok "Management helper: $HELPER_SCRIPT"
echo ""

# Copy service helper script
TRANSPORT_SRC="$PLUGIN_DIR/scripts/jarvis-transport.sh"
TRANSPORT_DEST="$JARVIS_HOME/jarvis-transport.sh"
if [ -f "$TRANSPORT_SRC" ]; then
    cp "$TRANSPORT_SRC" "$TRANSPORT_DEST"
    chmod +x "$TRANSPORT_DEST"
    ok "Service helper: $TRANSPORT_DEST"
fi
echo ""


# ═══════════════════════════════════════════════
# Complete!
# ═══════════════════════════════════════════════

echo -e "${BOLD}═══════════════════════════════════════════════${NC}"
echo -e "${BOLD}  Installation Complete!${NC}"
echo -e "${BOLD}═══════════════════════════════════════════════${NC}"
echo ""
echo -e "  Vault:       ${CYAN}$VAULT_PATH${NC}"
echo -e "  Compose:     ${CYAN}$JARVIS_HOME/docker-compose.yml${NC}"
echo -e "  MCP Core:    ${CYAN}http://localhost:8741/mcp${NC}"
echo -e "  MCP Todoist: ${CYAN}http://localhost:8742/mcp${NC}"

if [ "$SHELL_SETUP" = "Y" ] || [ "$SHELL_SETUP" = "y" ]; then
    echo -e "  Shell:       ${CYAN}jarvis${NC} installed to ${INSTALL_DIR:-PATH}"
fi

echo -e "  Config:      ${CYAN}$JARVIS_HOME/config.json${NC}"
echo ""
echo -e "  ${BOLD}Quick Start:${NC}"
echo -e "    ${BLUE}\$ jarvis${NC}                     — Launch Jarvis"
echo -e "    ${BLUE}\$ jarvis \"/jarvis-recall AI tools\"${NC}  — Search your vault"
echo -e "    ${BLUE}/jarvis-settings${NC}              — Update configuration"

echo ""
echo -e "  ${BOLD}Docker Management:${NC}"
echo -e "    ${BLUE}\$ $JARVIS_HOME/jarvis-docker.sh status${NC}   — Check container"
echo -e "    ${BLUE}\$ $JARVIS_HOME/jarvis-docker.sh logs${NC}     — View logs"
echo -e "    ${BLUE}\$ $JARVIS_HOME/jarvis-docker.sh restart${NC}  — Restart"
echo -e "    ${BLUE}\$ $JARVIS_HOME/jarvis-docker.sh update${NC}   — Pull & restart"

echo ""

if [ "$SHELL_SETUP" = "Y" ] || [ "$SHELL_SETUP" = "y" ]; then
    if ! command -v jarvis >/dev/null 2>&1; then
        echo -e "  ${YELLOW}Add to your shell config to use 'jarvis':${NC}"
        echo -e "    ${BLUE}export PATH=\"${INSTALL_DIR:-\$HOME/.local/bin}:\$PATH\"${NC}"
        echo ""
    fi
fi

echo -e "  ${BOLD}First time? Just run: jarvis${NC}"
echo ""
