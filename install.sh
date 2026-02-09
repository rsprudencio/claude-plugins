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

echo -e "${CYAN}"
echo "  ╦╔═╗╦═╗╦  ╦╦╔═╗"
echo "  ║╠═╣╠╦╝╚╗╔╝║╚═╗"
echo " ╚╝╩ ╩╩╚═ ╚╝ ╩╚═╝"
echo -e "${NC}"
echo -e "  ${BOLD}AI Assistant Plugin Installer${NC} v${JARVIS_VERSION}"
echo ""

# ═══════════════════════════════════════════════
# 📦 Install Core Plugin
# ═══════════════════════════════════════════════

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
claude plugin marketplace add rsprudencio/claude-plugins >/dev/null 2>&1 || true

# Verify marketplace is available
if ! claude plugin marketplace list 2>/dev/null | grep -q "raph-claude-plugins"; then
    fail "Could not add marketplace"
    echo -e "  Run manually: ${BLUE}claude plugin marketplace add rsprudencio/claude-plugins${NC}"
    exit 1
fi

# Install core plugin
echo -e "  Installing ${BLUE}jarvis@raph-claude-plugins${NC}..."
claude plugin install jarvis@raph-claude-plugins >/dev/null 2>&1 || {
    fail "Could not install jarvis plugin"
    echo ""
    echo -e "  Try manually: ${BLUE}claude plugin install jarvis@raph-claude-plugins${NC}"
    exit 1
}

ok "Plugin installed"
echo ""

# ═══════════════════════════════════════════════
# ✅ Check Prerequisites
# ═══════════════════════════════════════════════

echo -e "${BOLD}✅ Check Prerequisites${NC}"
echo ""

# Resolve plugin directory
PLUGIN_DIR=$($PYTHON_CMD -c "
import sys, json
try:
    result = sys.stdin.read()
    for p in json.loads(result):
        if p.get('id', '').startswith('jarvis@'):
            print(p['installPath'])
            break
except:
    pass
" < <(claude plugin list --json 2>/dev/null))

if [ -z "$PLUGIN_DIR" ] || [ ! -d "$PLUGIN_DIR/mcp-server/tools" ]; then
    warn "Could not find installed plugin directory - using basic checks"

    # Fallback: Basic bash checks
    if command -v uvx >/dev/null 2>&1 || command -v uv >/dev/null 2>&1; then
        ok "uv/uvx found"
    else
        fail "uv/uvx not found"
        echo ""
        echo -e "  Install: ${BLUE}curl -LsSf https://astral.sh/uv/install.sh | sh${NC}"
        echo ""
        exit 1
    fi

    if command -v git >/dev/null 2>&1; then
        ok "git found"
    else
        fail "git not found"
        echo ""
        echo -e "  ${BOLD}macOS:${NC} ${BLUE}xcode-select --install${NC} or ${BLUE}brew install git${NC}"
        echo -e "  ${BOLD}Linux:${NC} ${BLUE}sudo apt install git${NC} (Debian/Ubuntu)"
        echo ""
        exit 1
    fi
else
    # Run comprehensive checks using installed plugin's Python modules
    $PYTHON_CMD - "$PLUGIN_DIR/mcp-server" <<'PYEOF'
import sys
import os

# Get plugin directory from command line argument
mcp_server_dir = sys.argv[1] if len(sys.argv) > 1 else None

if mcp_server_dir and os.path.exists(mcp_server_dir):
    sys.path.insert(0, mcp_server_dir)

from tools.system_check import run_system_check, format_check_result

result = run_system_check()
print(format_check_result(result, verbose=False))
sys.exit(0 if result["healthy"] else 1)
PYEOF

    if [ $? -ne 0 ]; then
        echo ""
        echo -e "${RED}Prerequisites not met. Fix the issues above and re-run.${NC}"
        exit 1
    fi
fi

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
# 🔌 Validate MCP Server
# ═══════════════════════════════════════════════

echo -e "${BOLD}🔌 Validate MCP Server${NC}"
echo ""

if [ ! -d "$PLUGIN_DIR/mcp-server" ]; then
    fail "MCP server not found in plugin directory"
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
fi

# Shell integration (always ask, independent of config)
echo -e "  ${BOLD}Shell Integration${NC}"
echo "  The 'jarvis' command launches Claude with your Jarvis identity."
echo -e "  ${YELLOW}⚠️  Highly recommended — this is the only way to make Claude fully impersonate Jarvis.${NC}"
echo ""
ask "  Add/update 'jarvis' command in your shell? [Y/n]: " SHELL_SETUP "Y"

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
fi

# Shell integration (independent of config — always runs if user said Y)
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

        # Copy shell function content into RC (no version-pinned source path)
        SHELL_FUNCTION_FILE="$PLUGIN_DIR/shell/jarvis.$SHELL_TYPE"
        if [ -f "$SHELL_FUNCTION_FILE" ]; then
            {
                echo ""
                echo "# Jarvis AI Assistant START"
                cat "$SHELL_FUNCTION_FILE"
                echo "# Jarvis AI Assistant END"
            } >> "$SHELL_RC"
            ok "Shell function added to $SHELL_RC"
        else
            warn "Shell function not found at $SHELL_FUNCTION_FILE"
            echo "    Expected: $PLUGIN_DIR/shell/jarvis.$SHELL_TYPE"
        fi
    else
        warn "Could not detect shell config file"
        echo "    Manually add the jarvis function from $PLUGIN_DIR/shell/"
    fi
else
    info "Skipping shell integration"
    echo -e "  You can still activate Jarvis inside any Claude session by typing: ${BLUE}/jarvis:jarvis${NC}"
fi

echo ""

# ═══════════════════════════════════════════════
# 📚 Index Vault
# ═══════════════════════════════════════════════

echo -e "${BOLD}📚 Index Vault${NC}"
echo ""

if [ -d "$VAULT_PATH" ]; then
    MD_COUNT=$(find "$VAULT_PATH" -name "*.md" -not -path "*/.obsidian/*" -not -path "*/templates/*" -not -path "*/.jarvis/*" 2>/dev/null | wc -l | tr -d ' ')
    if [ "$MD_COUNT" -gt 5 ]; then
        info "Found $MD_COUNT .md files in vault"

        # Offer to index now (only if meaningful content exists)
        ask "  Index now for semantic search? [Y/n]: " INDEX_NOW "Y"

        if [ "$INDEX_NOW" = "Y" ] || [ "$INDEX_NOW" = "y" ]; then
            info "Indexing vault (may take a moment)..."

            # Call jarvis_index_vault via MCP server (requires full handshake)
            INDEX_OUTPUT=$(printf '%s\n%s\n%s\n' \
                '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"installer","version":"0.1"}}}' \
                '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
                '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"jarvis_index_vault","arguments":{}}}' | \
                timeout 120 uvx --from "$PLUGIN_DIR/mcp-server" jarvis-tools 2>/dev/null || true)

            if echo "$INDEX_OUTPUT" | grep -q '"result"' 2>/dev/null; then
                ok "Vault indexed ($MD_COUNT files) — semantic search ready"
            else
                warn "Indexing failed or timed out"
                info "You can index later by asking Jarvis: 'index my vault'"
            fi
        else
            info "Skipped — index later by asking Jarvis: 'index my vault'"
        fi
    elif [ "$MD_COUNT" -gt 0 ]; then
        info "Found $MD_COUNT .md files in vault"
        info "Index later: ask Jarvis 'index my vault' or run /jarvis-settings"
        ok "Vault ready"
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
echo -e "    ${BLUE}\$ jarvis \"/jarvis-recall AI tools\"${NC}  — Search your vault"
echo -e "    ${BLUE}/jarvis-settings${NC}              — Update configuration"
echo ""

if [ "$SHELL_SETUP" = "Y" ] || [ "$SHELL_SETUP" = "y" ]; then
    echo -e "  ${YELLOW}Remember to reload your shell:${NC}"
    echo -e "    ${BLUE}source $SHELL_RC${NC}"
    echo ""
fi

echo -e "  ${BOLD}First time? Just run: jarvis${NC}"
echo ""
