#!/usr/bin/env python3
"""Auto-Extract background worker: analyzes conversation turns and extracts observations.

Usage: python3 extract_observation.py <mcp_server_dir> <mode> <temp_jsonl_file> [session_id]

The Stop hook creates a temp JSONL file with the last N lines of the transcript.
This script:
1. Parses the last conversation turn (user + assistant messages)
2. Checks substance (min total text) and cooldown thresholds
3. Calls Haiku to extract behavioral observations
4. Stores observations via tier2_write
5. Cleans up the temp file
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Import anthropic at module level for easier testing (imported conditionally in function)
try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore

# Haiku model ID
HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Cooldown sentinel file
COOLDOWN_SENTINEL = Path("/tmp/jarvis-auto-extract-cooldown")

# Token usage log for cost tracking (debug mode)
TOKEN_LOG_FILE = Path.home() / ".jarvis" / "debug.auto-extraction.log"

EXTRACTION_PROMPT = """\
You are analyzing a conversation turn between a user and an AI assistant working on code.

## User's Message
{user_text}

## Assistant's Response
{assistant_text}

## Tools Used
{tool_names}

## Files Referenced
{relevant_files}

## Project Context
Project: {project_dir}
Branch: {git_branch}
Token usage: {token_usage}

Extract observations about:
- User preferences, workflow patterns, or behavioral tendencies
- Architectural decisions or technical choices made
- Project context, structure, or conventions discovered
- Important patterns or insights that would be useful to remember across sessions

DO NOT extract:
- Routine file operations or trivial tool calls
- Temporary debugging or exploratory work
- Secrets, credentials, or PII

Respond with JSON only:
{{
  "has_observation": true/false,
  "content": "The observation (1-3 sentences, markdown OK)",
  "importance_score": 0.3-0.8,
  "tags": ["tag1", "tag2"],
  "scope": "project" or "global"
}}

scope: "project" if the observation is specific to this codebase (e.g., project conventions, file structure, architecture). "global" if it's a universal pattern (e.g., user preference, general workflow habit).

If the turn is routine or contains nothing worth remembering, set has_observation to false.
"""


# Tools that don't produce meaningful file path context
_SKIP_FILE_TOOLS = {"Bash", "WebFetch", "WebSearch", "WebSearch", "AskUserQuestion"}

# Keys in tool_use input that may contain file paths
_FILE_PATH_KEYS = ("file_path", "relative_path", "path")

# Maximum number of file paths to include
_MAX_FILE_PATHS = 10


def extract_file_paths_from_tools(assistant_content: list) -> list[str]:
    """Extract file paths from tool_use blocks in assistant content.

    Scans tool_use blocks for input fields like file_path, relative_path, path.
    Skips tools that don't produce meaningful file context (Bash, WebFetch, etc.).
    Deduplicates and caps at _MAX_FILE_PATHS.

    Args:
        assistant_content: List of content blocks from assistant message

    Returns:
        Deduplicated list of file paths (max 10)
    """
    seen = set()
    paths = []

    for block in assistant_content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue
        if block.get("name", "") in _SKIP_FILE_TOOLS:
            continue

        tool_input = block.get("input", {})
        if not isinstance(tool_input, dict):
            continue

        for key in _FILE_PATH_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value and value not in seen:
                seen.add(value)
                paths.append(value)

    return paths[:_MAX_FILE_PATHS]


def parse_transcript_turn(lines: list[str]) -> dict | None:
    """Parse the last conversation turn from transcript JSONL lines.

    Scans backwards to find:
    - Last assistant message (with text blocks and tool_use blocks)
    - Preceding user message (with text blocks)

    Also scans ALL assistant messages for file paths (not just the last one),
    since file-touching tools (Read, Edit, Grep) happen mid-conversation.

    Returns:
        Dict with keys: user_text, assistant_text, tool_names, token_usage,
                        relevant_files, assistant_line
        None if parsing fails or no valid turn found
    """
    assistant_msg = None
    assistant_line_idx = -1
    user_msg = None

    # Scan backwards to find last assistant, then preceding user
    for reverse_idx, line in enumerate(reversed(lines)):
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue

        msg_type = entry.get("type")

        # Skip metadata types
        if msg_type in ("system", "progress", "file-history-snapshot"):
            continue

        if msg_type == "assistant" and assistant_msg is None:
            assistant_msg = entry
            assistant_line_idx = len(lines) - 1 - reverse_idx
        elif msg_type == "user" and assistant_msg is not None and user_msg is None:
            user_msg = entry
            break  # Found complete turn

    if not assistant_msg or not user_msg:
        return None

    # Extract user text
    user_content = user_msg.get("message", {}).get("content", [])
    user_texts = [
        block.get("text", "")
        for block in user_content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    user_text = "\n".join(user_texts).strip()

    # Extract assistant text and tool names
    assistant_content = assistant_msg.get("message", {}).get("content", [])
    assistant_texts = []
    tool_names = []

    for block in assistant_content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            assistant_texts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_names.append(block.get("name", "unknown"))

    assistant_text = "\n".join(assistant_texts).strip()

    # Deduplicate tool names while preserving order
    seen = set()
    unique_tools = []
    for tool in tool_names:
        if tool not in seen:
            seen.add(tool)
            unique_tools.append(tool)

    # Scan ALL assistant turns for file paths (not just the last one)
    all_file_paths = []
    for line in lines:
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if entry.get("type") != "assistant":
            continue
        content = entry.get("message", {}).get("content", [])
        all_file_paths.extend(extract_file_paths_from_tools(content))

    # Deduplicate while preserving order
    seen_paths = set()
    relevant_files = []
    for p in all_file_paths:
        if p not in seen_paths:
            seen_paths.add(p)
            relevant_files.append(p)
    relevant_files = relevant_files[:_MAX_FILE_PATHS]

    # Extract token usage
    usage = assistant_msg.get("message", {}).get("usage", {})
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    token_usage = f"{input_tokens} in, {output_tokens} out"

    return {
        "user_text": user_text,
        "assistant_text": assistant_text,
        "tool_names": unique_tools,
        "token_usage": token_usage,
        "relevant_files": relevant_files,
        "assistant_line": assistant_line_idx,
    }


def check_substance(turn: dict, min_chars: int = 200) -> bool:
    """Check if turn has enough substance to warrant extraction.

    Args:
        turn: Parsed turn dict from parse_transcript_turn
        min_chars: Minimum total characters (user + assistant text)

    Returns:
        True if turn meets substance threshold
    """
    total_chars = len(turn.get("user_text", "")) + len(turn.get("assistant_text", ""))
    return total_chars >= min_chars


def check_cooldown(cooldown_seconds: int = 120) -> bool:
    """Check if cooldown period has passed since last extraction.

    Uses a file-based sentinel at /tmp/jarvis-auto-extract-cooldown.
    At most one extraction per N seconds (system-wide, across all sessions).

    Args:
        cooldown_seconds: Minimum seconds between extractions

    Returns:
        True if cooldown has passed (extraction allowed)
    """
    try:
        now = time.time()

        if COOLDOWN_SENTINEL.exists():
            mtime = COOLDOWN_SENTINEL.stat().st_mtime
            if now - mtime < cooldown_seconds:
                return False  # Still in cooldown

        # Create or refresh sentinel
        COOLDOWN_SENTINEL.touch()
        return True
    except OSError:
        # If /tmp is unavailable, don't block
        return True


def truncate(text: str, max_chars: int) -> str:
    """Truncate text to max_chars with ellipsis if needed."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


def build_turn_prompt(turn: dict, project_dir: str = "", git_branch: str = "") -> str:
    """Build the extraction prompt for Haiku from parsed turn.

    Args:
        turn: Parsed turn dict from parse_transcript_turn
        project_dir: Current project directory name
        git_branch: Current git branch name

    Returns:
        Formatted extraction prompt string
    """
    user_text = truncate(turn.get("user_text", ""), 500)
    assistant_text = truncate(turn.get("assistant_text", ""), 1500)
    tool_names = turn.get("tool_names", [])
    tool_list = ", ".join(tool_names) if tool_names else "None"
    token_usage = turn.get("token_usage", "unknown")
    relevant_files = turn.get("relevant_files", [])
    files_list = "\n".join(f"- {f}" for f in relevant_files) if relevant_files else "None"

    return EXTRACTION_PROMPT.format(
        user_text=user_text,
        assistant_text=assistant_text,
        tool_names=tool_list,
        relevant_files=files_list,
        project_dir=project_dir or "unknown",
        git_branch=git_branch or "unknown",
        token_usage=token_usage,
    )


def _log_extraction(backend: str, input_tokens: int, output_tokens: int,
                    observation_stored: bool = False, obs_id: str = None,
                    importance: float = 0.0, tags: list = None,
                    prompt: str = "", observation_content: str = "",
                    scope: str = "", hook_input: str = "",
                    raw_lines: list = None, debug: bool = False):
    """Log full extraction pipeline: raw hook input → transcript → prompt → result.

    Logs a structured multi-line block per extraction for complete auditability.
    Each section shows a stage of the pipeline so bugs and drift are visible.

    Haiku pricing (as of 2026):
    - Input: $1.00 per 1M tokens
    - Output: $5.00 per 1M tokens

    Args:
        backend: "API" or "CLI"
        input_tokens: Input token count
        output_tokens: Output token count
        observation_stored: Whether an observation was actually stored
        obs_id: Observation ID if stored (e.g., "obs::1770561133783")
        importance: Importance score if stored
        tags: List of tags if stored
        prompt: The full prompt sent to Haiku
        observation_content: The content Haiku generated (empty if skipped)
        scope: "project" or "global" classification
        hook_input: Raw JSON from Claude Code's stop hook (verbatim stdin)
        raw_lines: Raw JSONL transcript lines before parsing
        debug: Enable logging (default False)
    """
    if not debug:
        return

    try:
        # ANSI color codes for terminal readability (cat, tail -f, less -R)
        C_CYAN = "\033[36m"
        C_DIM = "\033[2m"
        C_GREEN = "\033[32m"
        C_YELLOW = "\033[33m"
        C_RESET = "\033[0m"

        # Calculate costs
        input_cost = (input_tokens / 1_000_000) * 1.00
        output_cost = (output_tokens / 1_000_000) * 5.00
        total_cost = input_cost + output_cost

        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        status_color = C_GREEN if observation_stored else C_YELLOW
        status = "STORED" if observation_stored else "SKIPPED"
        tags_str = ",".join(tags) if tags else "none"

        lines = []
        lines.append(f"{C_CYAN}{'═' * 80}{C_RESET}")
        lines.append(
            f"{timestamp} | {backend:4s} | "
            f"in:{input_tokens:6d} out:{output_tokens:4d} | "
            f"${total_cost:.6f} | {status_color}{status}{C_RESET}"
        )

        # Log verbatim hook input from Claude Code
        if hook_input:
            lines.append(f"{C_DIM}{'─' * 37} HOOK INPUT {'─' * 32}{C_RESET}")
            lines.append(hook_input)

        # Log raw transcript JSONL (last user + assistant entries)
        if raw_lines:
            lines.append(f"{C_DIM}{'─' * 35} RAW TRANSCRIPT {'─' * 29}{C_RESET}")
            # Show only the last user and assistant JSONL entries (not all 100 lines)
            for raw_line in raw_lines[-10:]:
                lines.append(raw_line)
            if len(raw_lines) > 10:
                lines.append(f"  ... ({len(raw_lines)} total lines, showing last 10)")

        # Log the prompt built for Haiku
        if prompt:
            lines.append(f"{C_DIM}{'─' * 40} PROMPT {'─' * 33}{C_RESET}")
            lines.append(prompt)

        # Log the result
        lines.append(f"{C_DIM}{'─' * 40} RESULT {'─' * 33}{C_RESET}")
        if observation_stored and obs_id:
            lines.append(f"  ID:         {obs_id}")
            lines.append(f"  Content:    {observation_content}")
            lines.append(f"  Importance: {importance:.2f}")
            lines.append(f"  Scope:      {scope or 'unset'}")
            lines.append(f"  Tags:       {tags_str}")
        else:
            lines.append(f"  {C_YELLOW}has_observation: false (routine turn){C_RESET}")

        lines.append("")  # Blank line separator

        with open(TOKEN_LOG_FILE, "a") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        # Debug logging must never disrupt primary flow
        pass


def _parse_haiku_text(text: str) -> dict | None:
    """Parse Haiku response text into observation dict.

    Handles plain JSON and JSON wrapped in markdown code blocks.
    Returns None on parse failure.
    """
    text = text.strip()

    # Handle potential markdown code blocks around JSON
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(
            line for line in lines
            if not line.strip().startswith("```")
        ).strip()

    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _extract_with_backend(backend_name: str, backend_fn, prompt: str) -> tuple[dict, int, int] | None:
    """Common wrapper for API/CLI extraction.

    Args:
        backend_name: "API" or "CLI" for logging
        backend_fn: Backend function that returns (response_text, input_tokens, output_tokens) or None
        prompt: The extraction prompt

    Returns:
        Tuple of (parsed_observation_dict, input_tokens, output_tokens) or None
        Token counts returned so caller can log after knowing storage outcome
    """
    result = backend_fn(prompt)
    if result is None:
        return None

    response_text, input_tokens, output_tokens = result

    # Parse response
    parsed = _parse_haiku_text(response_text)
    if parsed is None:
        print(f"Failed to parse Haiku {backend_name} response", file=sys.stderr)
        return None

    return (parsed, input_tokens, output_tokens)


def _call_api_backend(prompt: str) -> tuple[str, int, int] | None:
    """Backend: Call Haiku via Anthropic SDK.

    Returns:
        Tuple of (response_text, input_tokens, output_tokens) or None on failure
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    if anthropic is None:
        print("anthropic package not installed, skipping API extraction", file=sys.stderr)
        return None

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return (
            response.content[0].text,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
    except Exception as e:
        print(f"Haiku API call failed: {e}", file=sys.stderr)
        return None


def call_haiku_api(prompt: str) -> tuple[dict, int, int] | None:
    """Call Haiku via Anthropic SDK (fast, requires ANTHROPIC_API_KEY).

    Args:
        prompt: The extraction prompt string

    Returns:
        Tuple of (parsed_dict, input_tokens, output_tokens) or None if failed.
        Token counts returned for logging after storage outcome is known.
    """
    return _extract_with_backend("API", _call_api_backend, prompt)


def _call_cli_backend(prompt: str) -> tuple[str, int, int] | None:
    """Backend: Call Haiku via Claude CLI.

    Returns:
        Tuple of (response_text, estimated_input_tokens, estimated_output_tokens) or None on failure
    """
    claude_bin = shutil.which("claude")
    if not claude_bin:
        print("claude binary not found on PATH, skipping CLI extraction", file=sys.stderr)
        return None

    try:
        result = subprocess.run(
            [claude_bin, "-p", "--model", "haiku"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            print(f"Claude CLI exited with code {result.returncode}", file=sys.stderr)
            return None

        # Estimate token usage (CLI doesn't expose exact counts)
        # Rough estimate: ~4 chars per token for English text
        est_input = len(prompt) // 4
        est_output = len(result.stdout) // 4

        return (result.stdout, est_input, est_output)

    except subprocess.TimeoutExpired:
        print("Claude CLI timed out after 30s", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Claude CLI call failed: {e}", file=sys.stderr)
        return None


def call_haiku_cli(prompt: str) -> tuple[dict, int, int] | None:
    """Call Haiku via Claude CLI (slower, uses OAuth from Keychain).

    Uses `claude -p --model haiku` in non-interactive mode.
    Inherits OAuth credentials from the user's Claude Code installation.

    Args:
        prompt: The extraction prompt string

    Returns:
        Tuple of (parsed_dict, estimated_input_tokens, estimated_output_tokens) or None if failed.
        Token counts returned for logging after storage outcome is known.
    """
    return _extract_with_backend("CLI", _call_cli_backend, prompt)


def call_haiku(prompt: str, mode: str = "background") -> tuple[dict, int, int, str] | None:
    """Route extraction to the appropriate backend based on mode.

    Args:
        prompt: The extraction prompt string
        mode: "background" (smart fallback), "background-api", or "background-cli"

    Returns:
        Tuple of (parsed_dict, input_tokens, output_tokens, backend_used) or None if failed.
        backend_used is "API" or "CLI" for logging purposes.
    """
    if mode == "background-api":
        result = call_haiku_api(prompt)
        return (*result, "API") if result else None

    if mode == "background-cli":
        result = call_haiku_cli(prompt)
        return (*result, "CLI") if result else None

    # Smart "background" mode: try API first (fast), fall back to CLI
    result = call_haiku_api(prompt)
    if result is not None:
        return (*result, "API")

    result = call_haiku_cli(prompt)
    return (*result, "CLI") if result else None


def store_observation(content: str, importance_score: float, tags: list, source_label: str,
                      project_path: str = "", git_branch: str = "",
                      relevant_files: list | None = None, scope: str = "",
                      session_id: str = "", transcript_line: int = -1) -> dict:
    """Store an observation via tier2_write.

    Args:
        content: Observation text
        importance_score: 0.0-1.0
        tags: List of tags
        source_label: Source identifier (e.g., "auto-extract:stop-hook")
        project_path: Full path to project directory
        git_branch: Current git branch name
        relevant_files: List of file paths referenced in the turn
        scope: "project" or "global" classification from Haiku
        session_id: Claude Code session ID for tracing
        transcript_line: Absolute line index in transcript JSONL (-1 = unknown)

    Returns:
        Result dict from tier2_write
    """
    from tools.tier2 import tier2_write

    extra = {}
    if project_path:
        extra["project_path"] = project_path
    if git_branch:
        extra["git_branch"] = git_branch
    if relevant_files:
        extra["relevant_files"] = ",".join(relevant_files)
    if scope:
        extra["scope"] = scope
    if session_id:
        extra["session_id"] = session_id
    if transcript_line >= 0:
        extra["transcript_line"] = str(transcript_line)

    return tier2_write(
        content=content,
        content_type="observation",
        importance_score=importance_score,
        source=source_label,
        tags=tags,
        extra_metadata=extra or None,
        skip_secret_scan=False,  # Always scan for secrets
    )


def main():
    """Main entry point: read transcript, extract and store observation."""
    # Get args: <mcp_server_dir> <mode> <temp_jsonl_file> [session_id] [project_path] [git_branch] [total_transcript_lines]
    if len(sys.argv) < 4:
        print("Usage: extract_observation.py <mcp_server_dir> <mode> <temp_jsonl_file> [session_id] [project_path] [git_branch] [total_transcript_lines]", file=sys.stderr)
        sys.exit(1)

    mcp_server_dir = sys.argv[1]
    mode = sys.argv[2]
    temp_file = Path(sys.argv[3])
    session_id = sys.argv[4] if len(sys.argv) >= 5 else "unknown"
    project_path = sys.argv[5] if len(sys.argv) >= 6 else ""
    git_branch = sys.argv[6] if len(sys.argv) >= 7 else ""
    total_transcript_lines = int(sys.argv[7]) if len(sys.argv) >= 8 else 0
    hook_input = os.environ.get("JARVIS_HOOK_INPUT", "")
    sys.path.insert(0, mcp_server_dir)

    try:
        # Read temp JSONL file
        if not temp_file.exists():
            print(f"Temp file not found: {temp_file}", file=sys.stderr)
            sys.exit(0)

        with open(temp_file) as f:
            lines = [line.strip() for line in f if line.strip()]

        # Parse transcript turn
        turn = parse_transcript_turn(lines)
        if turn is None:
            print("No valid turn found in transcript", file=sys.stderr)
            sys.exit(0)

        # Load config for thresholds
        from tools.config import get_auto_extract_config
        config = get_auto_extract_config()
        debug = config.get("debug", False)

        # Check substance
        min_chars = config.get("min_turn_chars", 200)
        if not check_substance(turn, min_chars):
            print(f"Turn too short (< {min_chars} chars), skipping", file=sys.stderr)
            sys.exit(0)

        # Check cooldown
        cooldown_seconds = config.get("cooldown_seconds", 120)
        if not check_cooldown(cooldown_seconds):
            print(f"Cooldown active (< {cooldown_seconds}s since last extraction), skipping", file=sys.stderr)
            sys.exit(0)

        # Build prompt and call Haiku
        project_dir = os.path.basename(project_path) if project_path else ""
        prompt = build_turn_prompt(turn, project_dir=project_dir, git_branch=git_branch)
        extraction_result = call_haiku(prompt, mode=mode)
        if extraction_result is None:
            print("Haiku extraction failed", file=sys.stderr)
            sys.exit(0)

        extraction, input_tokens, output_tokens, backend = extraction_result

        if not extraction.get("has_observation", False):
            print("No observation extracted (routine turn)", file=sys.stderr)
            _log_extraction(backend, input_tokens, output_tokens,
                            observation_stored=False, prompt=prompt,
                            hook_input=hook_input, raw_lines=lines, debug=debug)
            sys.exit(0)

        # Store the observation
        content = extraction.get("content", "")
        if not content:
            print("Empty observation content, skipping", file=sys.stderr)
            _log_extraction(backend, input_tokens, output_tokens,
                            observation_stored=False, prompt=prompt,
                            hook_input=hook_input, raw_lines=lines, debug=debug)
            sys.exit(0)

        importance = float(extraction.get("importance_score", 0.5))
        importance = max(0.0, min(1.0, importance))  # Clamp to valid range

        tags = extraction.get("tags", [])
        if not isinstance(tags, list):
            tags = []

        # Extract scope from Haiku response (default to empty if missing)
        scope = extraction.get("scope", "")
        if scope not in ("project", "global"):
            scope = ""

        # Get relevant_files from the parsed turn
        relevant_files = turn.get("relevant_files", [])

        # Compute absolute transcript line from tail offset
        tail_line = turn.get("assistant_line", -1)
        if total_transcript_lines > 0 and tail_line >= 0:
            max_lines = int(config.get("max_transcript_lines", 100))
            tail_start = max(0, total_transcript_lines - max_lines)
            absolute_line = tail_start + tail_line
        else:
            absolute_line = tail_line

        result = store_observation(
            content, importance, tags, "auto-extract:stop-hook",
            project_path=project_path, git_branch=git_branch,
            relevant_files=relevant_files, scope=scope,
            session_id=session_id, transcript_line=absolute_line,
        )
        if result.get("success"):
            obs_id = result.get('id', 'unknown')
            print(f"Stored observation: {obs_id}", file=sys.stderr)
            _log_extraction(backend, input_tokens, output_tokens,
                            observation_stored=True, obs_id=obs_id,
                            importance=importance, tags=tags,
                            prompt=prompt, observation_content=content,
                            scope=scope, hook_input=hook_input,
                            raw_lines=lines, debug=debug)
        else:
            print(f"Failed to store observation: {result.get('error')}", file=sys.stderr)
            _log_extraction(backend, input_tokens, output_tokens,
                            observation_stored=False, prompt=prompt,
                            observation_content=content,
                            hook_input=hook_input, raw_lines=lines,
                            debug=debug)

    finally:
        # Always clean up temp file
        try:
            if temp_file.exists():
                temp_file.unlink()
        except OSError as e:
            print(f"Failed to clean up temp file: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
