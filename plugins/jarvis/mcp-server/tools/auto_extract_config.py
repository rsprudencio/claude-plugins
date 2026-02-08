"""Auto-Extract filtering logic for PostToolUse hook.

Determines whether a tool call should be observed and extracted.
Contains skip lists, dedup, and the main filter_hook_input() entry point.

This module is imported by both:
- post-tool-use.sh (via python3 -c) for inline filtering
- extract_observation.py for background mode validation
"""
import hashlib
import os
import time
from pathlib import Path

# --- Constants ---

# Tools that should NEVER trigger observation extraction.
# Includes: Claude internals, read-only tools, anti-recursion tools.
SKIP_TOOLS = frozenset({
    # Claude Code internals
    "Glob", "Read", "Grep", "Write", "Edit", "Task", "AskUserQuestion",
    "WebFetch", "WebSearch", "NotebookEdit", "Skill",
    "TodoRead", "TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet",
    "EnterPlanMode", "ExitPlanMode",
    # Anti-recursion: Tier 2 tools that auto-extract writes TO
    "jarvis_tier2_write", "jarvis_tier2_read", "jarvis_tier2_list",
    "jarvis_tier2_delete", "jarvis_promote",
    # Jarvis read-only / diagnostic tools
    "jarvis_debug_config", "jarvis_resolve_path", "jarvis_list_paths",
    "jarvis_collection_stats", "jarvis_doc_read", "jarvis_file_exists",
    "jarvis_list_vault_dir", "jarvis_read_vault_file",
    "jarvis_memory_read", "jarvis_memory_list",
    "jarvis_index_vault", "jarvis_index_file",
})

# Bash commands that are too trivial to observe.
SKIP_BASH_COMMANDS = frozenset({
    "cd", "ls", "pwd", "echo", "cat", "head", "tail", "wc",
    "which", "whoami", "date", "true", "false", "test",
    "git status", "git branch", "git log", "git diff", "git show",
    "git tag", "git remote", "git rev-parse",
})

# Minimum output length worth observing
MIN_OUTPUT_LENGTH = 50

# Dedup window: same tool+output hash within this many seconds is suppressed
DEDUP_WINDOW_SECONDS = 300

# Dedup directory
DEDUP_DIR = Path("/tmp/jarvis-auto-extract-dedup")


# --- Filtering Functions ---

def should_skip_tool(tool_name: str, config: dict) -> bool:
    """Check if tool should be skipped based on skip lists and user overrides.

    Args:
        tool_name: The tool name from hook input
        config: Auto-extract config dict (from get_auto_extract_config)

    Returns:
        True if the tool should be skipped
    """
    # Build effective skip set with user overrides
    effective_skip = set(SKIP_TOOLS)

    # Add user-specified tools to skip
    for tool in config.get("skip_tools_add", []):
        effective_skip.add(tool)

    # Remove user-specified tools from skip (allow observing)
    for tool in config.get("skip_tools_remove", []):
        effective_skip.discard(tool)

    # Check MCP tool name formats: mcp__server__tool or just tool_name
    # Strip common MCP prefixes for matching
    bare_name = tool_name
    if tool_name.startswith("mcp__plugin_jarvis_core__"):
        bare_name = tool_name[len("mcp__plugin_jarvis_core__"):]

    return bare_name in effective_skip or tool_name in effective_skip


def should_skip_bash_command(tool_input: dict) -> bool:
    """Check if a Bash tool call is too trivial to observe.

    Args:
        tool_input: The tool_input dict from hook data (contains 'command' for Bash)

    Returns:
        True if the bash command is trivial
    """
    command = tool_input.get("command", "").strip()
    if not command:
        return True

    # Check exact matches
    if command in SKIP_BASH_COMMANDS:
        return True

    # Check prefix matches (e.g., "ls -la" starts with "ls")
    first_word = command.split()[0] if command.split() else ""
    if first_word in SKIP_BASH_COMMANDS:
        return True

    # Check git read-only commands (prefix match)
    for skip_cmd in SKIP_BASH_COMMANDS:
        if skip_cmd.startswith("git ") and command.startswith(skip_cmd):
            return True

    return False


def should_skip_output(tool_result: str) -> bool:
    """Check if tool output is too short or indicates failure.

    Args:
        tool_result: The tool_result string from hook data

    Returns:
        True if output should be skipped
    """
    if not tool_result:
        return True

    if len(tool_result) < MIN_OUTPUT_LENGTH:
        return True

    return False


def check_dedup(tool_name: str, tool_result: str) -> bool:
    """Check if this exact tool+result combination was seen recently.

    Uses file-based dedup with zero-byte sentinel files in /tmp.
    Files are named by SHA-256 hash and checked by mtime.

    Args:
        tool_name: Tool name
        tool_result: Tool result string

    Returns:
        True if this is a duplicate (should skip)
    """
    try:
        DEDUP_DIR.mkdir(parents=True, exist_ok=True)

        # Hash tool name + first 500 chars of result
        content = f"{tool_name}:{tool_result[:500]}"
        hash_hex = hashlib.sha256(content.encode()).hexdigest()[:16]
        dedup_file = DEDUP_DIR / hash_hex

        now = time.time()

        if dedup_file.exists():
            mtime = dedup_file.stat().st_mtime
            if now - mtime < DEDUP_WINDOW_SECONDS:
                return True  # Duplicate within window
            # Expired, will be refreshed below

        # Create/refresh sentinel file
        dedup_file.touch()
        return False
    except OSError:
        # If /tmp is unavailable, don't block
        return False


def check_prerequisites(config: dict) -> dict:
    """Check if auto-extract prerequisites are met for the configured mode.

    Returns a status dict with:
    - mode: Current mode
    - healthy: Whether the feature can operate
    - issues: List of problems found
    - details: Additional diagnostic info
    """
    mode = config.get("mode", "background")
    issues = []
    details = {
        "mode": mode,
        "skip_tools_add": config.get("skip_tools_add", []),
        "skip_tools_remove": config.get("skip_tools_remove", []),
    }

    if mode == "disabled":
        return {
            "mode": mode,
            "healthy": True,
            "status": "Auto-Extract is disabled",
            "issues": [],
            "details": details,
        }

    if mode == "inline":
        return {
            "mode": mode,
            "healthy": True,
            "status": "Auto-Extract is active (inline mode — uses session model)",
            "issues": [],
            "details": details,
        }

    if mode in ("background", "background-api", "background-cli"):
        # Check ANTHROPIC_API_KEY availability
        has_api_key = bool(os.environ.get("ANTHROPIC_API_KEY"))

        # Check anthropic package
        has_anthropic = False
        try:
            import importlib.util
            has_anthropic = importlib.util.find_spec("anthropic") is not None
        except (ImportError, ValueError):
            pass

        # Check claude CLI availability
        import shutil
        has_claude_cli = shutil.which("claude") is not None

        details["has_api_key"] = has_api_key
        details["has_anthropic_package"] = has_anthropic
        details["has_claude_cli"] = has_claude_cli

        if mode == "background-api":
            if not has_api_key:
                issues.append("ANTHROPIC_API_KEY not found in environment — required for background-api mode")
            if not has_anthropic:
                issues.append("'anthropic' package not installed — run: pip install anthropic")
        elif mode == "background-cli":
            if not has_claude_cli:
                issues.append("'claude' binary not found on PATH — required for background-cli mode")
        else:
            # Smart "background" mode: at least one backend must be available
            api_ok = has_api_key and has_anthropic
            cli_ok = has_claude_cli
            if not api_ok and not cli_ok:
                issues.append("No extraction backend available — set ANTHROPIC_API_KEY or ensure 'claude' is on PATH")
            # Informational: note which backends are available
            backends = []
            if api_ok:
                backends.append("API")
            if cli_ok:
                backends.append("CLI")
            details["available_backends"] = backends

        if issues:
            return {
                "mode": mode,
                "healthy": False,
                "status": f"Auto-Extract has {len(issues)} issue(s) — observations will NOT be captured",
                "issues": issues,
                "details": details,
            }

        status_suffix = {
            "background": "smart fallback",
            "background-api": "Anthropic SDK",
            "background-cli": "Claude CLI",
        }[mode]
        return {
            "mode": mode,
            "healthy": True,
            "status": f"Auto-Extract is active ({mode} mode — {status_suffix})",
            "issues": [],
            "details": details,
        }

    # Unknown mode
    valid_modes = "disabled, background, background-api, background-cli, inline"
    issues.append(f"Unknown mode '{mode}' — valid modes: {valid_modes}")
    return {
        "mode": mode,
        "healthy": False,
        "status": "Auto-Extract has invalid configuration",
        "issues": issues,
        "details": details,
    }


def filter_hook_input(hook_data: dict, config: dict) -> tuple:
    """Main entry point: determine if a tool call should be observed.

    Args:
        hook_data: Full hook JSON from stdin (tool_name, tool_input, tool_result)
        config: Auto-extract config dict

    Returns:
        Tuple of (should_skip: bool, reason: str)
        If should_skip is False, reason contains the mode to use.
    """
    # Check mode first
    mode = config.get("mode", "background")
    if mode == "disabled":
        return (True, "disabled")

    tool_name = hook_data.get("tool_name", "")
    tool_input = hook_data.get("tool_input", {})
    tool_result = hook_data.get("tool_result", "")

    # Check tool skip list
    if should_skip_tool(tool_name, config):
        return (True, f"skip_tool:{tool_name}")

    # Special handling for Bash commands
    if tool_name == "Bash" and should_skip_bash_command(tool_input):
        return (True, "skip_bash_trivial")

    # Check output quality
    if should_skip_output(tool_result):
        return (True, "skip_output_short")

    # Check dedup
    if check_dedup(tool_name, tool_result):
        return (True, "dedup")

    # All checks passed — proceed with configured mode
    return (False, mode)
