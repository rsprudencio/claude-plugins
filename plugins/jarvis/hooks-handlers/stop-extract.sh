#!/usr/bin/env bash
set -euo pipefail

# Auto-Extract Stop Hook Handler
#
# Reads Stop hook JSON from stdin, checks config, then routes:
# - disabled: exit 0 immediately
# - background/background-api/background-cli: tail transcript to temp file,
#   spawn extract_observation.py in background, exit immediately

# Read all stdin (Stop hook input JSON)
INPUT=$(cat)

# Resolve paths relative to this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MCP_SERVER_DIR="$PLUGIN_ROOT/mcp-server"

# Run Python check to determine action
# Output format: "skip:disabled" or "proceed:MODE:TRANSCRIPT_PATH:SESSION_ID:MAX_LINES"
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
    max_lines = config.get('max_transcript_lines', 100)

    print(f'proceed:{mode}:{transcript_path}:{session_id}:{max_lines}')
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

# Parse proceed components (MODE:TRANSCRIPT_PATH:SESSION_ID:MAX_LINES)
# Remove "proceed:" prefix
PARAMS="${CHECK_RESULT#proceed:}"

# Extract fields by splitting on colons
MODE="${PARAMS%%:*}"
PARAMS="${PARAMS#*:}"

TRANSCRIPT_PATH="${PARAMS%%:*}"
PARAMS="${PARAMS#*:}"

SESSION_ID="${PARAMS%%:*}"
PARAMS="${PARAMS#*:}"

MAX_LINES="${PARAMS}"

# Validate transcript file exists
if [ ! -f "$TRANSCRIPT_PATH" ]; then
    exit 0
fi

# Create temp file with last N lines of transcript
TEMP_FILE="/tmp/jarvis-turn-$$.jsonl"
tail -n "$MAX_LINES" "$TRANSCRIPT_PATH" > "$TEMP_FILE" 2>/dev/null || exit 0

# Spawn extraction in background and return immediately
nohup python3 "$SCRIPT_DIR/extract_observation.py" "$MCP_SERVER_DIR" "$MODE" "$TEMP_FILE" "$SESSION_ID" \
    >/dev/null 2>&1 & disown

exit 0
