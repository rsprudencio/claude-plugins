#!/bin/bash
# Jarvis Plugin Installer
# curl -fsSL https://raw.githubusercontent.com/rsprudencio/claude-plugins/refs/heads/master/install.sh | bash
set -e

JARVIS_HOME="${JARVIS_HOME:-$HOME/.jarvis}"
MARKETPLACE_NAME="raph-claude-plugins"
MARKETPLACE_REPO="https://github.com/rsprudencio/claude-plugins"
JARVIS_VERSION="1.15.0"

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

echo ""
echo -e "${CYAN}"
echo "  ╦╔═╗╦═╗╦  ╦╦╔═╗"
echo "  ║╠═╣╠╦╝╚╗╔╝║╚═╗"
echo " ╚╝╩ ╩╩╚═ ╚╝ ╩╚═╝"
echo -e "${NC}"
echo -e "  ${BOLD}AI Assistant Plugin Installer${NC} v${JARVIS_VERSION}"
echo ""

# ═══════════════════════════════════════════════
# Step 1: Check Prerequisites
# ═══════════════════════════════════════════════

echo -e "${BOLD}Step 1: Check Prerequisites${NC}"
echo ""

PREREQS_OK=true

# Check Claude Code CLI
if command -v claude &> /dev/null; then
    CLAUDE_VERSION=$(claude --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || echo "unknown")
    ok "Claude Code CLI ($CLAUDE_VERSION)"
else
    fail "Claude Code CLI not found"
    echo ""
    echo -e "  Install Claude Code first: ${BLUE}https://claude.ai/code${NC}"
    echo ""
    exit 1
fi

# Check Python 3.10+
if command -v python3 &> /dev/null; then
    PY_VERSION=$(python3 --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
    PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
    PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
    if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 10 ]; then
        ok "Python $PY_VERSION"
    else
        fail "Python $PY_VERSION found, but 3.10+ required"
        echo ""
        echo -e "  Install Python 3.10+: ${BLUE}https://python.org${NC}"
        echo -e "  macOS: ${BLUE}brew install python@3.12${NC}"
        echo ""
        exit 1
    fi
else
    fail "Python 3 not found"
    echo ""
    echo -e "  Install Python 3.10+: ${BLUE}https://python.org${NC}"
    echo -e "  macOS: ${BLUE}brew install python@3.12${NC}"
    echo ""
    exit 1
fi

# Check uv / uvx (required for MCP server)
if command -v uvx &> /dev/null; then
    UV_VERSION=$(uv --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || echo "unknown")
    ok "uv $UV_VERSION (with uvx)"
elif command -v uv &> /dev/null; then
    # uv exists but uvx doesn't — unusual, warn user
    warn "uv found but uvx not in PATH"
    echo -e "    Try: ${BLUE}uv tool install uvx${NC}"
    echo ""
    PREREQS_OK=false
else
    warn "uv not found — installing..."
    if curl -LsSf https://astral.sh/uv/install.sh | sh 2>/dev/null; then
        # Source the new PATH
        export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
        if command -v uvx &> /dev/null; then
            UV_VERSION=$(uv --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || echo "unknown")
            ok "uv $UV_VERSION installed"
        else
            fail "uv installed but uvx not available after install"
            echo -e "    Restart your terminal and re-run this installer."
            exit 1
        fi
    else
        fail "Failed to install uv"
        echo ""
        echo -e "  Install manually: ${BLUE}https://docs.astral.sh/uv/getting-started/installation/${NC}"
        echo ""
        exit 1
    fi
fi

# Check git (required for vault audit trail)
if command -v git &> /dev/null; then
    GIT_VERSION=$(git --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || echo "unknown")
    ok "git $GIT_VERSION"
else
    fail "git not found"
    echo ""
    echo -e "  Git is required for Jarvis vault operations."
    echo ""
    if [[ "$OSTYPE" == "darwin"* ]]; then
        echo -e "  Install: ${BLUE}xcode-select --install${NC} or ${BLUE}brew install git${NC}"
    elif [[ "$OSTYPE" == "msys" || "$OSTYPE" == "win32" ]]; then
        echo -e "  Install: ${BLUE}https://git-scm.com/download/win${NC}"
    else
        echo -e "  Install: ${BLUE}sudo apt install git${NC} (Debian/Ubuntu)"
    fi
    echo ""
    exit 1
fi

if [ "$PREREQS_OK" != true ]; then
    echo ""
    echo -e "${RED}Prerequisites not met. Fix the issues above and re-run.${NC}"
    exit 1
fi

echo ""

# ═══════════════════════════════════════════════
# Step 2: Install Plugin
# ═══════════════════════════════════════════════

echo -e "${BOLD}Step 2: Install Plugin${NC}"
echo ""

# Add or update marketplace
MARKETPLACE_EXISTS=$(claude plugin marketplace list 2>/dev/null | grep -c "$MARKETPLACE_NAME" || echo "0")

if [ "$MARKETPLACE_EXISTS" -eq "0" ]; then
    info "Adding marketplace..."
    if claude plugin marketplace add "$MARKETPLACE_REPO" 2>/dev/null; then
        ok "Marketplace added"
    else
        fail "Failed to add marketplace"
        echo -e "    Check: ${BLUE}$MARKETPLACE_REPO${NC}"
        exit 1
    fi
else
    info "Updating marketplace..."
    claude plugin marketplace update "$MARKETPLACE_NAME" 2>/dev/null || true
    ok "Marketplace up to date"
fi

# Install core plugin
info "Installing jarvis core plugin..."
if claude plugin install "jarvis@$MARKETPLACE_NAME" 2>/dev/null; then
    ok "Core plugin installed"
else
    # Might already be installed — check
    if claude plugin list 2>/dev/null | grep -q "jarvis@"; then
        ok "Core plugin already installed (updated)"
    else
        fail "Failed to install core plugin"
        exit 1
    fi
fi

# Optional extensions
echo ""
echo -e "  ${BOLD}Optional extensions:${NC}"
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

# ═══════════════════════════════════════════════
# Step 3: Validate MCP Server
# ═══════════════════════════════════════════════

echo -e "${BOLD}Step 3: Validate MCP Server${NC}"
echo ""

# Find installed plugin path
CACHE_BASE="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins/cache/$MARKETPLACE_NAME"
PLUGIN_DIR=$(find "$CACHE_BASE/jarvis" -maxdepth 1 -type d 2>/dev/null | sort -V | tail -1)

if [ -z "$PLUGIN_DIR" ] || [ ! -d "$PLUGIN_DIR/mcp-server" ]; then
    fail "Plugin directory not found at $CACHE_BASE/jarvis/"
    echo "    Try reinstalling: claude plugin install jarvis@$MARKETPLACE_NAME"
    exit 1
fi

info "Testing MCP server startup..."

# Send a minimal JSON-RPC initialize request and check for a response
MCP_OUTPUT=$(echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}' | \
    timeout 30 uvx --from "$PLUGIN_DIR/mcp-server" jarvis-tools 2>/dev/null || true)

if echo "$MCP_OUTPUT" | grep -q '"result"' 2>/dev/null; then
    ok "MCP server responds"
elif echo "$MCP_OUTPUT" | grep -q '"jsonrpc"' 2>/dev/null; then
    ok "MCP server responds"
else
    fail "MCP server failed to start"
    echo ""
    echo "  Common causes:"
    echo "    - ChromaDB dependencies failed to compile"
    echo "    - uvx cache corrupted (fix: uvx cache clean)"
    echo ""
    echo "  Debug:"
    echo -e "    ${BLUE}uvx --from \"$PLUGIN_DIR/mcp-server\" jarvis-tools${NC}"
    echo ""
    echo "  Continuing anyway — the plugin is installed, but semantic memory may not work."
    echo ""
fi

echo ""

# ═══════════════════════════════════════════════
# Step 4: Configure Jarvis
# ═══════════════════════════════════════════════

echo -e "${BOLD}Step 4: Configure Jarvis${NC}"
echo ""

# Check for existing config
if [ -f "$JARVIS_HOME/config.json" ]; then
    EXISTING_VAULT=$(python3 -c "import json; print(json.load(open('$JARVIS_HOME/config.json')).get('vault_path', 'not set'))" 2>/dev/null || echo "not set")
    echo -e "  Existing config found (vault: ${CYAN}$EXISTING_VAULT${NC})"
    ask "  Reconfigure? [y/N]: " RECONFIG "N"
    if [ "$RECONFIG" != "y" ] && [ "$RECONFIG" != "Y" ]; then
        info "Keeping existing config"
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

    # Shell integration
    echo -e "  ${BOLD}Shell Integration${NC}"
    echo "  The 'jarvis' command launches Claude with your Jarvis identity."
    echo ""
    ask "  Add 'jarvis' command to your shell? [Y/n]: " SHELL_SETUP "Y"
fi

echo ""

# ═══════════════════════════════════════════════
# Step 5: Write Config & Create Directories
# ═══════════════════════════════════════════════

echo -e "${BOLD}Step 5: Write Config${NC}"
echo ""

mkdir -p "$JARVIS_HOME"

if [ "$SKIP_CONFIG" != true ]; then
    # Create vault directory
    mkdir -p "$VAULT_PATH"
    ok "Vault directory ready: $VAULT_PATH"

    # Create memory DB directory
    mkdir -p "$JARVIS_HOME/memory_db"
    ok "Memory DB directory ready: $JARVIS_HOME/memory_db/"

    # Write full config with all defaults visible
    TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    cat > "$JARVIS_HOME/config.json" << CONFIGEOF
{
  "vault_path": "$VAULT_PATH",
  "vault_confirmed": true,
  "configured_at": "$TIMESTAMP",
  "version": "$JARVIS_VERSION",
  "memory": {
    "db_path": "~/.jarvis/memory_db",
    "secret_detection": true,
    "importance_scoring": true,
    "recency_boost_days": 7,
    "default_importance": "medium",
    "auto_extract": {
      "mode": "background",
      "min_turn_chars": 200,
      "cooldown_seconds": 120,
      "max_transcript_lines": 100,
      "debug": false
    }
  },
  "promotion": {
    "importance_threshold": 0.85,
    "retrieval_count_threshold": 3,
    "age_importance_days": 30,
    "age_importance_score": 0.7,
    "on_promoted_file_deleted": "remove"
  },
  "paths": {
    "journal_jarvis": "journal/jarvis",
    "journal_daily": "journal/daily",
    "notes": "notes",
    "work": "work",
    "inbox": "inbox",
    "inbox_todoist": "inbox/todoist",
    "templates": "templates",
    "strategic": ".jarvis/strategic",
    "observations_promoted": "journal/jarvis/observations",
    "patterns_promoted": "journal/jarvis/patterns",
    "learnings_promoted": "journal/jarvis/learnings",
    "decisions_promoted": "journal/jarvis/decisions"
  }
}
CONFIGEOF
    ok "Config written: $JARVIS_HOME/config.json (all defaults visible)"

    # Shell integration
    if [ "$SHELL_SETUP" = "Y" ] || [ "$SHELL_SETUP" = "y" ]; then
        # Detect shell
        SHELL_RC=""
        if [ -n "$ZSH_VERSION" ] || [ "$SHELL" = "$(command -v zsh)" ] || [ -f "$HOME/.zshrc" ]; then
            SHELL_RC="$HOME/.zshrc"
            SHELL_TYPE="zsh"
        elif [ -f "$HOME/.bashrc" ]; then
            SHELL_RC="$HOME/.bashrc"
            SHELL_TYPE="bash"
        elif [ -f "$HOME/.bash_profile" ]; then
            SHELL_RC="$HOME/.bash_profile"
            SHELL_TYPE="bash"
        fi

        if [ -n "$SHELL_RC" ]; then
            # Check if already installed
            if grep -q "# Jarvis AI Assistant" "$SHELL_RC" 2>/dev/null; then
                # Remove old version before adding new
                sed -i.bak '/# Jarvis AI Assistant START/,/# Jarvis AI Assistant END/d' "$SHELL_RC" 2>/dev/null || true
                rm -f "$SHELL_RC.bak"
            fi

            CACHE_DIR_TEMPLATE="\${CLAUDE_CONFIG_DIR:-\$HOME/.claude}/plugins/cache/$MARKETPLACE_NAME"

            cat >> "$SHELL_RC" << 'SHELLEOF'

# Jarvis AI Assistant START
jarvis() {
  local cache_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins/cache/raph-claude-plugins"
  local system_prompt=""

  for plugin in jarvis jarvis-todoist jarvis-strategic; do
    local plugin_dir=""
    # Find latest version directory
    if [ -d "$cache_dir/$plugin" ]; then
      plugin_dir=$(find "$cache_dir/$plugin" -maxdepth 1 -type d 2>/dev/null | sort -V | tail -1)
    fi

    if [ -n "$plugin_dir" ] && [ -f "$plugin_dir/system-prompt.md" ]; then
      if [ -n "$system_prompt" ]; then
        system_prompt+=$'\n\n---\n\n'
      fi
      system_prompt+="$(cat "$plugin_dir/system-prompt.md")"
    fi
  done

  if [ -n "$system_prompt" ]; then
    claude --append-system-prompt "$system_prompt" "$@"
  else
    echo "Warning: No jarvis plugins found. Install with:"
    echo "  claude plugin install jarvis@raph-claude-plugins"
    claude "$@"
  fi
}
# Jarvis AI Assistant END
SHELLEOF
            ok "Shell function added to $SHELL_RC"
            info "Run: source $SHELL_RC"
        else
            warn "Could not detect shell config file"
            echo "    Manually add the jarvis function from plugins/jarvis/shell/"
        fi
    fi
fi

echo ""

# ═══════════════════════════════════════════════
# Step 6: Index Vault (if applicable)
# ═══════════════════════════════════════════════

echo -e "${BOLD}Step 6: Index Vault${NC}"
echo ""

if [ -d "$VAULT_PATH" ]; then
    MD_COUNT=$(find "$VAULT_PATH" -name "*.md" -not -path "*/.obsidian/*" -not -path "*/templates/*" -not -path "*/.jarvis/*" 2>/dev/null | wc -l | tr -d ' ')
    if [ "$MD_COUNT" -gt 0 ]; then
        info "Found $MD_COUNT .md files in vault"
        info "Indexing will happen automatically on first Jarvis session"
        ok "Vault ready for semantic search"
    else
        info "Vault is empty — start by running: jarvis"
        info "Jarvis will create journal entries and notes as you use it"
    fi
else
    info "Vault directory will be created on first use"
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

if [ "$SHELL_SETUP" = "Y" ] || [ "$SHELL_SETUP" = "y" ]; then
    echo -e "  Shell:       ${CYAN}jarvis${NC} command added to $SHELL_RC"
fi

echo -e "  Config:      ${CYAN}$JARVIS_HOME/config.json${NC}"
echo ""
echo -e "  ${BOLD}Quick Start:${NC}"
echo -e "    ${BLUE}\$ jarvis${NC}                     — Launch Jarvis"
echo -e "    ${BLUE}\$ jarvis \"/recall AI tools\"${NC}  — Search your vault"
echo -e "    ${BLUE}/jarvis-settings${NC}              — Update configuration"
echo ""

if [ "$SHELL_SETUP" = "Y" ] || [ "$SHELL_SETUP" = "y" ]; then
    echo -e "  ${YELLOW}Remember to reload your shell:${NC}"
    echo -e "    ${BLUE}source $SHELL_RC${NC}"
    echo ""
fi

echo -e "  ${BOLD}First time? Just run: jarvis${NC}"
echo ""
