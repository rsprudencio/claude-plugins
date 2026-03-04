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

# Pipe stdin (hook JSON) directly to Python — single process handles everything
python3 "$SCRIPT_DIR/context_enrichment.py" --hook 2>/dev/null || true

exit 0
