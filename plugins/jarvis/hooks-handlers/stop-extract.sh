#!/usr/bin/env bash
set -euo pipefail

# Auto-Extract Stop Hook Handler
#
# Reads Stop hook JSON from stdin, checks config, then routes:
# - disabled: exit 0 immediately
# - background/background-api/background-cli: pass transcript path directly
#   to extract_observation.py in background, exit immediately
#
# The Python script handles per-session watermarking internally —
# no temp files or line counting needed here.

# Read all stdin (Stop hook input JSON)
INPUT=$(cat)

# Resolve paths relative to this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MCP_SERVER_DIR="$PLUGIN_ROOT/mcp-server"

# Run Python check to determine action
# Output format: "skip:disabled" or "proceed:MODE:TRANSCRIPT_PATH:SESSION_ID"
CHECK_RESULT=$(python3 -c "
import sys, json, os
sys.path.insert(0, '$MCP_SERVER_DIR')
from tools.config import get_auto_extract_config

try:
    hook_data = json.loads(sys.stdin.read())
    config = get_auto_extract_config()
    mode = config.get('mode', 'background')

    if mode == 'disabled':
        print('skip:disabled')
        sys.exit(0)

    # Extract transcript_path and expand ~ to home directory
    transcript_path = hook_data.get('transcript_path', '')
    if not transcript_path:
        print('skip:no_transcript_path')
        sys.exit(0)

    # Safely expand ~ using Python (not shell eval)
    transcript_path = os.path.expanduser(transcript_path)

    session_id = hook_data.get('session_id', 'unknown')

    print(f'proceed:{mode}:{transcript_path}:{session_id}')
except Exception as e:
    print(f'skip:error:{e}', file=sys.stderr)
    print('skip:error')
" <<< "$INPUT" 2>/dev/null || echo "skip:error")

# Parse result
ACTION="${CHECK_RESULT%%:*}"

# If skip, exit silently
if [ "$ACTION" = "skip" ]; then
    exit 0
fi

# Parse proceed components (MODE:TRANSCRIPT_PATH:SESSION_ID)
# Remove "proceed:" prefix
PARAMS="${CHECK_RESULT#proceed:}"

# Extract fields by splitting on colons
MODE="${PARAMS%%:*}"
PARAMS="${PARAMS#*:}"

TRANSCRIPT_PATH="${PARAMS%%:*}"
PARAMS="${PARAMS#*:}"

SESSION_ID="${PARAMS}"

# Validate transcript file exists
if [ ! -f "$TRANSCRIPT_PATH" ]; then
    exit 0
fi

# Capture project context from working directory
PROJECT_PATH="$PWD"
GIT_BRANCH="$(git -C "$PWD" branch --show-current 2>/dev/null || echo "")"

# Spawn extraction in background, passing raw hook JSON via env for debug logging
JARVIS_HOOK_INPUT="$INPUT" nohup python3 "$SCRIPT_DIR/extract_observation.py" "$MCP_SERVER_DIR" "$MODE" "$TRANSCRIPT_PATH" "$SESSION_ID" \
    "$PROJECT_PATH" "$GIT_BRANCH" \
    >/dev/null 2>&1 & disown

exit 0
