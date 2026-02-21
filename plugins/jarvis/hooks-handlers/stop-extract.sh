#!/usr/bin/env bash
set -euo pipefail

# Anti-recursion guard: skip if called from within an extraction subprocess
# (claude -p sessions spawned by extract_observation.py inherit this env var)
if [ -n "${JARVIS_EXTRACTING:-}" ]; then
    exit 0
fi

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

# Run Python check to determine action
# Output format: "skip\tREASON" or "proceed\tMODE\tTRANSCRIPT_PATH\tSESSION_ID"
CHECK_RESULT=$(python3 -c "
import json, os, sys

def load_mode():
    jarvis_home = os.environ.get('JARVIS_HOME')
    if jarvis_home:
        config_path = os.path.join(jarvis_home, 'config.json')
    else:
        config_path = os.path.expanduser('~/.jarvis/config.json')
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        data = {}
    memory = data.get('memory', {})
    auto_extract = memory.get('auto_extract', {})
    mode = auto_extract.get('mode', 'background')
    return mode if isinstance(mode, str) and mode else 'background'

try:
    hook_data = json.loads(sys.stdin.read())
    mode = load_mode()

    if mode == 'disabled':
        print('skip\tdisabled')
        sys.exit(0)

    # Extract transcript_path and expand ~ to home directory
    transcript_path = hook_data.get('transcript_path', '')
    if not transcript_path:
        print('skip\tno_transcript_path')
        sys.exit(0)

    # Safely expand ~ using Python (not shell eval)
    transcript_path = os.path.expanduser(transcript_path)

    session_id = hook_data.get('session_id', 'unknown')

    print(f'proceed\t{mode}\t{transcript_path}\t{session_id}')
except Exception as e:
    print(f'skip\terror:{e}', file=sys.stderr)
    print('skip\terror')
" <<< "$INPUT" 2>/dev/null || printf 'skip\terror\n')

# Parse result
ACTION="${CHECK_RESULT%%$'\t'*}"

# If skip, exit silently
if [ "$ACTION" = "skip" ]; then
    exit 0
fi

# Parse proceed components
IFS=$'\t' read -r _ MODE TRANSCRIPT_PATH SESSION_ID <<< "$CHECK_RESULT"

# Validate transcript file exists
if [ ! -f "$TRANSCRIPT_PATH" ]; then
    exit 0
fi

# Capture project context from working directory
PROJECT_PATH="$PWD"
GIT_BRANCH="$(git -C "$PWD" branch --show-current 2>/dev/null || echo "")"

# Spawn extraction in background, passing raw hook JSON via env for debug logging
JARVIS_HOOK_INPUT="$INPUT" nohup python3 "$SCRIPT_DIR/extract_observation.py" "$MODE" "$TRANSCRIPT_PATH" "$SESSION_ID" \
    "$PROJECT_PATH" "$GIT_BRANCH" \
    >/dev/null 2>&1 & disown

exit 0
