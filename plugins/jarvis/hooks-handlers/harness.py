"""Small compatibility boundary for Claude Code and Codex hook protocols.

Jarvis keeps hook business logic harness-neutral.  Only the final stdout
envelope differs: Claude Code consumes plain context, while Codex expects the
``UserPromptSubmit`` context in a structured JSON response.
"""

import json
import os
from collections.abc import Mapping


CLAUDE = "claude"
CODEX = "codex"


def detect_harness(environ: Mapping[str, str] | None = None) -> str:
    """Return the active hook harness.

    Codex sets ``PLUGIN_ROOT`` (and may also provide the legacy-compatible
    ``CLAUDE_PLUGIN_ROOT``). Claude Code provides ``CLAUDE_PLUGIN_ROOT`` only,
    so the Codex-specific variable is the unambiguous signal.
    """
    env = os.environ if environ is None else environ
    return CODEX if env.get("PLUGIN_ROOT") else CLAUDE


def format_user_prompt_submit_output(
    context: str,
    harness: str | None = None,
) -> str:
    """Format injected context for the active ``UserPromptSubmit`` protocol.

    Empty context always remains empty so a no-match or skipped prompt emits
    no hook response in either harness.
    """
    if not context:
        return ""

    active_harness = harness or detect_harness()
    if active_harness == CODEX:
        return json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                }
            },
            ensure_ascii=False,
        )
    return context
