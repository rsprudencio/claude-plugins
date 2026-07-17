#!/usr/bin/env bash
set -euo pipefail

# Anti-recursion guard: skip if called from within an extraction subprocess
if [ -n "${JARVIS_EXTRACTING:-}" ]; then
    exit 0
fi

# Context Enrichment Hook Handler
#
# Reads UserPromptSubmit hook JSON from stdin and pipes it to Python
# for semantic memory injection. Single Python invocation for speed.
# stdout from Python is passed through to Claude as injected context.
#
# Exit 0 always (silent on errors — never block the user's message).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Persist the prompt for harness-neutral Stop extraction, then pass the exact
# same hook payload to the retrieval handler. State capture is best-effort and
# never changes the context-enrichment output.
INPUT=$(cat)
printf '%s' "$INPUT" | python3 "$SCRIPT_DIR/turn_state.py" capture 2>/dev/null || true
printf '%s' "$INPUT" | python3 "$SCRIPT_DIR/context_enrichment.py" --hook 2>/dev/null || true

exit 0
