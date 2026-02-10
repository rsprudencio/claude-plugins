#!/usr/bin/env bash
set -euo pipefail

# Per-Prompt Semantic Search Hook Handler
#
# Reads UserPromptSubmit hook JSON from stdin and pipes it to Python
# for search. Single Python invocation for speed (avoids multiple startups).
# stdout from Python is passed through to Claude as injected context.
#
# Exit 0 always (silent on errors — never block the user's message).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MCP_SERVER_DIR="$PLUGIN_ROOT/mcp-server"

# Pipe stdin (hook JSON) directly to Python — single process handles everything
python3 "$SCRIPT_DIR/prompt_search.py" "$MCP_SERVER_DIR" --hook 2>/dev/null || true

exit 0
