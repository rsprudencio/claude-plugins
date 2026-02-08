#!/usr/bin/env bash
set -euo pipefail

# Auto-Extract PostToolUse Hook Handler
#
# Reads tool call JSON from stdin, filters via Python, then routes:
# - disabled: exit 0 immediately
# - background: spawn extract_observation.py (smart: API first, CLI fallback)
# - background-api: spawn extract_observation.py (Anthropic SDK only)
# - background-cli: spawn extract_observation.py (Claude CLI only, uses OAuth)
# - inline: return systemMessage for Claude to call jarvis_tier2_write

# Read all stdin (hook input JSON)
INPUT=$(cat)

# Resolve paths relative to this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MCP_SERVER_DIR="$PLUGIN_ROOT/mcp-server"

# Run Python filter to determine action
# Output format: "skip:<reason>" or "proceed:<mode>"
FILTER_RESULT=$(python3 -c "
import sys, json
sys.path.insert(0, '$MCP_SERVER_DIR')
from tools.auto_extract_config import filter_hook_input
from tools.config import get_auto_extract_config
try:
    hook_data = json.loads(sys.stdin.read())
    config = get_auto_extract_config()
    should_skip, reason = filter_hook_input(hook_data, config)
    if should_skip:
        print(f'skip:{reason}')
    else:
        print(f'proceed:{reason}')
except Exception as e:
    print(f'skip:filter_error:{e}', file=sys.stderr)
    print('skip:filter_error')
" <<< "$INPUT" 2>/dev/null || echo "skip:filter_error")

# Parse result
ACTION="${FILTER_RESULT%%:*}"
MODE="${FILTER_RESULT#*:}"

# If skip, exit silently
if [ "$ACTION" = "skip" ]; then
    exit 0
fi

# Route by mode
case "$MODE" in
    background|background-api|background-cli)
        # Spawn extraction in background and return immediately
        # Pass mode so extract_observation.py routes to the right backend
        nohup python3 "$SCRIPT_DIR/extract_observation.py" "$MCP_SERVER_DIR" "$MODE" <<< "$INPUT" \
            >/dev/null 2>&1 & disown
        exit 0
        ;;
    inline)
        # Return systemMessage instructing Claude to extract inline
        TOOL_NAME=$(echo "$INPUT" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('tool_name','unknown'))" 2>/dev/null || echo "unknown")
        cat << EOF
{
  "systemMessage": "The tool call to '$TOOL_NAME' just completed. If this tool call revealed something structurally meaningful about the codebase, user preferences, decisions made, or patterns discovered — silently call jarvis_tier2_write with content_type='observation' and an appropriate importance_score. If the result is routine or uninteresting, do nothing. Do NOT mention this to the user."
}
EOF
        exit 0
        ;;
    *)
        # Unknown mode, skip
        exit 0
        ;;
esac
