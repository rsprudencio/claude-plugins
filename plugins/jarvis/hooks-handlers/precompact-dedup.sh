#!/usr/bin/env bash
set -euo pipefail

# Anti-recursion guard
if [ -n "${JARVIS_EXTRACTING:-}" ]; then
    exit 0
fi

# PreCompact Dedup Gate Hook Handler
#
# Fires before context window compaction. Records which memories were
# injected so that the next UserPromptSubmit can skip re-injecting them.
# Exit 0 always (silent on errors — never block compaction).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 "$SCRIPT_DIR/precompact_dedup.py" --hook 2>/dev/null || true

exit 0
