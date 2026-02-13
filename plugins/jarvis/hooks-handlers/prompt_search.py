#!/usr/bin/env python3
"""Per-prompt semantic search: queries vault memories relevant to user's prompt.

Usage:
  Hook mode:  echo '{"prompt":"..."}' | python3 prompt_search.py <mcp_server_dir> --hook
  Direct:     python3 prompt_search.py <mcp_server_dir> "query text here"

Called by the UserPromptSubmit hook. Outputs XML-formatted vault memories
to stdout for injection into Claude's context. Silent on errors (exit 0,
no output) to avoid disrupting the user's conversation.
"""
import json
import sys
import time
import xml.sax.saxutils as saxutils
from pathlib import Path
from typing import Tuple


# --- Debug Logging ---

DEBUG_LOG_FILE = Path.home() / ".jarvis" / "debug.per-prompt-search.log"


def _debug_log(action: str, detail: str, prompt: str = "", injected: str = ""):
    """Append a structured debug block to the per-prompt search log.

    Uses shared ANSI colors and section dividers for visual consistency
    with the auto-extract debug log when tailing.

    Args:
        action: SKIP, ERROR, EMPTY, or FOUND
        detail: Summary line (e.g., "460ms | 3/5 | sources...")
        prompt: The user's prompt text
        injected: The full XML output injected into Claude's context (FOUND only)
    """
    try:
        from _colors import C_GREEN, C_YELLOW, C_RESET, divider_thick, divider_section

        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        status_color = C_GREEN if action == "FOUND" else C_YELLOW

        lines = []
        lines.append(divider_thick())
        lines.append(f"{ts} | {status_color}{action:5s}{C_RESET} | {detail}")

        if prompt:
            lines.append(divider_section("PROMPT"))
            lines.append(prompt)

        if injected:
            lines.append(divider_section("INJECTED"))
            lines.append(injected)

        lines.append("")  # Blank line separator

        with open(DEBUG_LOG_FILE, "a") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass  # Never fail on debug logging


# --- Telemetry ---

TELEMETRY_FILE = Path.home() / ".jarvis" / "telemetry" / "prompt_search.jsonl"


def _write_telemetry(prompt: str, query_ms: int, matches: list, result: dict):
    """Append a structured JSONL line for threshold/budget analysis."""
    try:
        TELEMETRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        scores = [m["relevance"] for m in matches]
        n_vault = sum(1 for m in matches if m.get("display_mode") == "reference")
        budget = result.get("budget_used", {})
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "prompt_len": len(prompt),
            "query_ms": query_ms,
            "n_results": len(matches),
            "n_tier2": len(matches) - n_vault,
            "n_vault": n_vault,
            "scores": [round(s, 3) for s in scores],
            "budget_tier2_used": budget.get("tier2", 0),
            "budget_vault_used": budget.get("vault", 0),
        }
        with open(TELEMETRY_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # Never fail on telemetry


# --- Prompt Filtering ---

_SKIP_PATTERNS = {
    "yes", "no", "ok", "sure", "thanks", "thank you", "go ahead",
    "done", "next", "continue", "correct", "right", "got it",
    "sounds good", "perfect", "great", "fine", "agreed",
    "yep", "nope", "nah", "yeah", "yup", "okay",
}


def _should_skip_prompt(query: str) -> Tuple[bool, str]:
    """Determine if prompt is too trivial for semantic search.

    Returns:
        Tuple of (should_skip, reason). Reason is empty string if not skipped.
    """
    stripped = query.strip()

    # Too short
    if len(stripped) < 10:
        return True, "short"

    # Slash commands (have their own handlers)
    if stripped.startswith("/"):
        return True, "slash_cmd"

    # Known confirmation patterns (case-insensitive, strip trailing punctuation)
    normalized = stripped.lower().rstrip(".!?")
    if normalized in _SKIP_PATTERNS:
        return True, "confirmation"

    # Pure code blocks
    if stripped.startswith("```"):
        return True, "code_block"

    # Auto-extract Haiku prompt (fired via `claude -p` subprocess)
    if "You are analyzing a conversation turn between a user and an AI assistant" in stripped[:100]:
        return True, "auto_extract_prompt"

    return False, ""


# --- Prompt Extraction from Hook JSON ---

def _extract_prompt(hook_json: str) -> str:
    """Extract prompt text from UserPromptSubmit hook input JSON."""
    try:
        data = json.loads(hook_json)
        # Try known key names for the prompt text
        prompt = data.get("prompt") or data.get("user_prompt") or data.get("message") or ""
        if isinstance(prompt, dict):
            prompt = prompt.get("text", prompt.get("content", ""))
        return str(prompt)
    except (json.JSONDecodeError, TypeError, AttributeError):
        return ""


# --- Output Formatting ---

def _format_memories(matches: list, query_ms: float) -> str:
    """Format search results as XML for injection into Claude's context.

    Vault items (display_mode="reference") are shown as compact file pointers.
    Tier 2 items (display_mode="full") are shown with full content.
    """
    if not matches:
        return ""

    lines = [f'<relevant-vault-memories count="{len(matches)}" query_ms="{query_ms}">']

    for match in matches:
        display_mode = match.get("display_mode", "full")
        attrs = [
            f'source="{saxutils.escape(match["source"])}"',
            f'relevance="{match["relevance"]}"',
            f'type="{saxutils.escape(match.get("type", "unknown"))}"',
        ]
        if match.get("heading"):
            attrs.append(f'heading="{saxutils.escape(match["heading"])}"')
        if display_mode == "reference":
            attrs.append('ref="vault"')

        content = saxutils.escape(match.get("content", ""))
        lines.append(f'<memory {" ".join(attrs)}>')
        lines.append(content)
        lines.append("</memory>")

    lines.append("</relevant-vault-memories>")
    return "\n".join(lines)


# --- Main Entry Point ---

def main():
    """Run per-prompt semantic search and output results to stdout."""
    if len(sys.argv) < 2:
        sys.exit(0)

    mcp_server_dir = sys.argv[1]

    # Determine prompt text source
    if len(sys.argv) >= 3 and sys.argv[2] == "--hook":
        # Hook mode: read JSON from stdin
        try:
            hook_input = sys.stdin.read()
        except Exception:
            sys.exit(0)
        prompt_text = _extract_prompt(hook_input)
    elif len(sys.argv) >= 3:
        # Direct mode: prompt text as argument
        prompt_text = sys.argv[2]
    else:
        sys.exit(0)

    if not prompt_text:
        sys.exit(0)

    # Skip trivial prompts before importing heavy modules
    skip, reason = _should_skip_prompt(prompt_text)

    # Add MCP server to path for tool imports (needed for config check even if skipped)
    sys.path.insert(0, mcp_server_dir)

    # Load config to check debug flag
    debug = False
    try:
        from tools.config import get_per_prompt_config
        config = get_per_prompt_config()
        debug = config.get("debug", False)
    except Exception:
        if skip:
            sys.exit(0)
        # If config fails but prompt wasn't skipped, continue with defaults
        config = {"enabled": True, "threshold": 0.5, "budget": 8000}

    if not config.get("enabled", True):
        if debug:
            _debug_log("SKIP", "disabled")
        sys.exit(0)

    if skip:
        if debug:
            _debug_log("SKIP", reason, prompt_text)
        sys.exit(0)

    try:
        from tools.query import semantic_context
    except ImportError:
        sys.exit(0)

    # Run search
    try:
        search_start = time.time()
        result = semantic_context(
            query=prompt_text,
            threshold=config.get("threshold", 0.5),
            budget=config.get("budget", 8000),
        )
        query_ms = round((time.time() - search_start) * 1000)
    except Exception as e:
        if debug:
            _debug_log("ERROR", str(e), prompt_text)
        sys.exit(0)

    matches = result.get("matches", [])
    if not matches:
        if debug:
            _debug_log("EMPTY", f"{query_ms}ms | 0 results", prompt_text)
        sys.exit(0)

    # Format output (Claude sees this via stdout)
    output = _format_memories(matches, result.get("query_ms", 0))

    # JSONL telemetry (always on, lightweight)
    _write_telemetry(prompt_text, query_ms, matches, result)

    if debug:
        n_vault = sum(1 for m in matches if m.get("display_mode") == "reference")
        n_tier2 = len(matches) - n_vault
        budget = result.get("budget_used", {})
        sources = " ".join(
            f'{m["source"]}({m["relevance"]})'
            for m in matches
        )
        _debug_log("FOUND",
                   f"{query_ms}ms | {len(matches)} ({n_tier2}t2+{n_vault}v) | "
                   f"budget t2:{budget.get('tier2', 0)}/v:{budget.get('vault', 0)} | {sources}",
                   prompt_text, injected=output)

    if output:
        print(output)

    sys.exit(0)


if __name__ == "__main__":
    main()
