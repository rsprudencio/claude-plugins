#!/usr/bin/env python3
"""Auto-Extract background worker: analyzes conversation turns and extracts observations.

Usage: python3 extract_observation.py <mcp_server_dir> <mode> <transcript_path> [session_id]

The Stop hook fires after every conversation round. This script uses a
per-session line watermark (similar to Filebeat/Kafka consumer offsets)
to track the last processed transcript line, avoiding re-analysis of
already-seen turns.

Pipeline:
1. Read watermark for this session → know where we left off
2. Read new transcript lines from watermark+1 onward
3. Parse ALL new user→assistant turns (forward scan)
4. Substance gate: total conversation text exceeds char threshold
5. Build session prompt including ALL turns (budget truncates, not excludes)
6. Call Haiku to extract 1-3 behavioral observations
7. Store observations via tier2_write
8. Advance watermark to last line read
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Import anthropic at module level for easier testing (imported conditionally in function)
try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore

# Haiku model ID
HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Per-session watermark directory
WATERMARK_DIR = Path.home() / ".jarvis" / "state" / "sessions"
WATERMARK_MAX_AGE = 2592000  # 30 days in seconds

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
Project: {project_name}
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

SESSION_EXTRACTION_PROMPT = """\
You are analyzing a coding session between a user and an AI assistant.

## What Started This Conversation
{first_user_text}

## Full Conversation ({turn_count} turns)
{turns_content}

## All Tools Used
{all_tools}

## Files Referenced
{relevant_files}

## Project Context
Project: {project_name} | Branch: {git_branch} | {total_token_usage}

---

TASK 1: Extract OBSERVATIONS (behavioral insights)

Extract observations about:
- User preferences, workflow patterns, or behavioral tendencies
- Architectural decisions or technical choices made
- Project context, structure, or conventions discovered
- Important patterns or insights that would be useful to remember across sessions

DO NOT extract:
- Routine file operations or trivial tool calls
- Temporary debugging or exploratory work
- Secrets, credentials, or PII
- Redundant observations (each should capture a distinct insight)

---

TASK 2: Extract WORKLOG entry (what the user was working on)

Infer what the user is trying to achieve in this session. Focus on their
intent and goals, not just mechanical actions.

## Known Workstreams
{known_workstreams}

For the primary task the user worked on, create a worklog entry:
- task_summary: What they were trying to achieve (1 sentence, intent-focused)
- workstream: Match to a known workstream above, or suggest a new descriptive name. Use "misc" for one-off tasks.
- activity_type: coding | debugging | reviewing | configuring | planning | discussing | researching | other

Examples of good vs bad task_summaries:
- GOOD: "Investigating VMPulse log errors after cluster-2 alerts"
- BAD:  "Read log files and ran grep"
- GOOD: "Adding Docker containerization for Jarvis MCP servers"
- BAD:  "Edited Dockerfile and entrypoint.sh"

DO NOT create a worklog entry for:
- Quick one-off questions or lookups (e.g., "what's the alias for netstat")
- Conversations about Jarvis itself (meta/config work)
- Trivial file reads with no meaningful follow-up

---

Respond with JSON only:
{{
  "observations": [
    {{
      "content": "The observation (1-3 sentences, markdown OK)",
      "importance_score": 0.3-0.8,
      "tags": ["tag1", "tag2"],
      "scope": "project" or "global"
    }}
  ],
  "worklog": {{
    "task_summary": "One-sentence intent-focused description",
    "workstream": "ProjectName or misc",
    "activity_type": "coding|debugging|...|other",
    "tags": ["tag1", "tag2"]
  }}
}}

scope: "project" if the observation is specific to this codebase (e.g., project conventions, file structure, architecture). "global" if it's a universal pattern (e.g., user preference, general workflow habit).

Return 1-3 observations. Each must capture a distinct, non-redundant insight.
Return an empty observations array if nothing is worth remembering.
Return ONE worklog object or null (not an array).
Set worklog to null if this was a quick question, meta-work, or nothing meaningful.
"""


# Tools that don't produce meaningful file path context
_SKIP_FILE_TOOLS = {"Bash", "WebFetch", "WebSearch", "WebSearch", "AskUserQuestion"}

# Keys in tool_use input that may contain file paths
_FILE_PATH_KEYS = ("file_path", "relative_path", "path")

# Maximum number of file paths to include
_MAX_FILE_PATHS = 10

# Session-level extraction constants
_FIRST_USER_MAX_CHARS = 300       # Cap for first user message context
_MIN_CHARS_PER_TURN = 150         # Floor allocation per turn in budget
_BUDGET_BASE = 2000               # Base chars (matches current single-turn behavior)
_BUDGET_OUTPUT_SCALE = 0.04       # 4% of output tokens as chars (1% * 4 chars/token)
_BUDGET_HARD_MAX = 8000           # Ceiling on content budget
_MAX_OBSERVATIONS = 3             # Cap on observations per extraction
_MAX_WORKLOGS = 1                 # One worklog per extraction (single primary task)
_HAIKU_MAX_TOKENS = 1000          # Up from 800 (room for worklog in same response)
_WORKLOG_ACTIVITY_TYPES = frozenset({
    "coding", "debugging", "reviewing", "configuring",
    "planning", "discussing", "researching", "other",
})


def _parse_output_tokens(token_usage: str) -> int:
    """Extract output token count from 'N in, M out' format.

    Args:
        token_usage: String like "1234 in, 567 out"

    Returns:
        Output token count, or 0 on parse failure
    """
    try:
        # Format: "1234 in, 567 out"
        parts = token_usage.split(",")
        if len(parts) >= 2:
            out_part = parts[1].strip()  # "567 out"
            return int(out_part.split()[0])
    except (ValueError, IndexError):
        pass
    return 0


def read_watermark(session_id: str) -> int:
    """Read the last-extracted line number for a session.

    Args:
        session_id: Claude Code session ID

    Returns:
        Last extracted line number (0-based), or -1 if no watermark exists
    """
    watermark_file = WATERMARK_DIR / f"{session_id}.json"
    try:
        if not watermark_file.exists():
            return -1
        with open(watermark_file) as f:
            data = json.load(f)
        return int(data.get("last_extracted_line", -1))
    except (json.JSONDecodeError, ValueError, TypeError, OSError):
        return -1


def write_watermark(session_id: str, last_line: int) -> None:
    """Atomically write the watermark for a session.

    Uses tempfile + os.replace for POSIX-atomic rename, preventing
    corrupt reads if the process crashes mid-write.

    Args:
        session_id: Claude Code session ID
        last_line: Last processed line number (0-based)
    """
    WATERMARK_DIR.mkdir(parents=True, exist_ok=True)
    watermark_file = WATERMARK_DIR / f"{session_id}.json"
    data = {
        "last_extracted_line": last_line,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    # Atomic write: write to temp file in same dir, then rename
    fd, tmp_path = tempfile.mkstemp(dir=WATERMARK_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp_path, watermark_file)
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_transcript_from(transcript_path: str, start_line: int,
                         max_lines: int = 500) -> tuple[list[tuple[int, str]], int]:
    """Read transcript JSONL lines from a starting position.

    Args:
        transcript_path: Path to the transcript JSONL file
        start_line: 0-based line index to start reading from
        max_lines: Maximum lines to read (safety cap)

    Returns:
        Tuple of:
        - List of (absolute_line_index, line_text) tuples
        - Total line count in the file
    """
    indexed_lines = []
    total_lines = 0
    try:
        with open(transcript_path) as f:
            for i, line in enumerate(f):
                total_lines = i + 1
                if i >= start_line and len(indexed_lines) < max_lines:
                    stripped = line.strip()
                    if stripped:
                        indexed_lines.append((i, stripped))
    except OSError:
        return [], 0
    return indexed_lines, total_lines


def parse_all_turns(indexed_lines: list[tuple[int, str]]) -> list[dict]:
    """Parse ALL user→assistant turns from indexed transcript lines.

    Walks FORWARD through lines, collecting every complete user→assistant
    pair as a turn. Each turn includes metadata useful for scoring.

    Args:
        indexed_lines: List of (absolute_line_index, line_text) tuples

    Returns:
        List of turn dicts, each with keys:
        - user_text, assistant_text, tool_names, token_usage
        - relevant_files, start_line_idx, end_line_idx
    """
    turns = []
    pending_user = None
    pending_user_line = -1
    all_file_paths_seen = set()
    all_file_paths_ordered = []

    for abs_idx, line in indexed_lines:
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue

        msg_type = entry.get("type")

        # Skip metadata types
        if msg_type in ("system", "progress", "file-history-snapshot"):
            continue

        if msg_type == "user":
            # Extract user text (content can be a string or list of blocks)
            content = entry.get("message", {}).get("content", [])
            if isinstance(content, str):
                pending_user = content.strip()
            else:
                texts = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                pending_user = "\n".join(texts).strip()
            pending_user_line = abs_idx

        elif msg_type == "assistant" and pending_user is not None:
            # Extract assistant text, tools, and file paths
            content = entry.get("message", {}).get("content", [])
            assistant_texts = []
            tool_names = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    assistant_texts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_names.append(block.get("name", "unknown"))

            assistant_text = "\n".join(assistant_texts).strip()

            # Deduplicate tool names
            seen_tools = set()
            unique_tools = []
            for tool in tool_names:
                if tool not in seen_tools:
                    seen_tools.add(tool)
                    unique_tools.append(tool)

            # Extract file paths from this assistant turn
            turn_files = extract_file_paths_from_tools(content)
            for fp in turn_files:
                if fp not in all_file_paths_seen:
                    all_file_paths_seen.add(fp)
                    all_file_paths_ordered.append(fp)

            # Token usage
            usage = entry.get("message", {}).get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)

            turns.append({
                "user_text": pending_user,
                "assistant_text": assistant_text,
                "tool_names": unique_tools,
                "token_usage": f"{input_tokens} in, {output_tokens} out",
                "relevant_files": list(all_file_paths_ordered)[:_MAX_FILE_PATHS],
                "start_line_idx": pending_user_line,
                "end_line_idx": abs_idx,
            })

            pending_user = None
            pending_user_line = -1

    return turns


def pick_best_turn(turns: list[dict], min_chars: int = 200) -> dict | None:
    """Select the most substantive turn from a list by scoring.

    Deprecated: Use filter_substantive_turns() for multi-turn extraction.

    Scoring formula: total_chars + (unique_tools * 100) + (has_files * 200)

    Args:
        turns: List of turn dicts from parse_all_turns()
        min_chars: Minimum total characters (user + assistant) to consider

    Returns:
        The highest-scoring turn, or None if all are below threshold
    """
    best = None
    best_score = -1

    for turn in turns:
        total_chars = len(turn.get("user_text", "")) + len(turn.get("assistant_text", ""))
        if total_chars < min_chars:
            continue

        unique_tools = len(set(turn.get("tool_names", [])))
        has_files = 1 if turn.get("relevant_files") else 0
        score = total_chars + (unique_tools * 100) + (has_files * 200)

        if score > best_score:
            best_score = score
            best = turn

    return best


def filter_substantive_turns(turns: list[dict], min_chars: int = 200) -> list[dict]:
    """Return all turns meeting the substance threshold, in order.

    Same char filter as pick_best_turn, but returns ALL matching turns
    instead of picking a single winner.

    Args:
        turns: List of turn dicts from parse_all_turns()
        min_chars: Minimum total characters (user + assistant) to consider

    Returns:
        List of qualifying turns in original order
    """
    result = []
    for turn in turns:
        total_chars = len(turn.get("user_text", "")) + len(turn.get("assistant_text", ""))
        if total_chars >= min_chars:
            result.append(turn)
    return result


def extract_first_user_message(transcript_path: str, max_scan_lines: int = 50) -> str:
    """Extract the first user message from the transcript (from line 0).

    Reads from the beginning of the file (independent of watermark)
    to find what started this conversation.

    Args:
        transcript_path: Path to the transcript JSONL file
        max_scan_lines: Maximum lines to scan before giving up

    Returns:
        First user message text truncated to _FIRST_USER_MAX_CHARS, or "" on any error
    """
    try:
        with open(transcript_path) as f:
            for i, line in enumerate(f):
                if i >= max_scan_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if entry.get("type") == "user":
                    content = entry.get("message", {}).get("content", [])
                    if isinstance(content, str):
                        text = content.strip()
                    else:
                        texts = [
                            block.get("text", "")
                            for block in content
                            if isinstance(block, dict) and block.get("type") == "text"
                        ]
                        text = "\n".join(texts).strip()
                    return text[:_FIRST_USER_MAX_CHARS]
    except OSError:
        pass
    return ""


def compute_content_budget(turns: list[dict]) -> int:
    """Compute character budget for session prompt based on output token volume.

    Formula: min(_BUDGET_BASE + total_output_tokens * _BUDGET_OUTPUT_SCALE, _BUDGET_HARD_MAX)

    Output tokens scale the budget because they're a direct proxy for
    "how much work happened." Sessions with trivial turns don't deserve
    a bigger budget just because there are many turns.

    Args:
        turns: List of substantive turn dicts (each has token_usage field)

    Returns:
        Character budget for the session prompt content
    """
    total_output = 0
    for turn in turns:
        total_output += _parse_output_tokens(turn.get("token_usage", ""))

    budget = _BUDGET_BASE + int(total_output * _BUDGET_OUTPUT_SCALE)
    return min(budget, _BUDGET_HARD_MAX)


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



def truncate(text: str, max_chars: int) -> str:
    """Truncate text to max_chars with ellipsis if needed."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


def build_turn_prompt(turn: dict, project_name: str = "", git_branch: str = "") -> str:
    """Build the extraction prompt for Haiku from parsed turn.

    Args:
        turn: Parsed turn dict from parse_transcript_turn
        project_name: Current project directory name
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
        project_name=project_name or "unknown",
        git_branch=git_branch or "unknown",
        token_usage=token_usage,
    )


def build_session_prompt(turns: list[dict], first_user_text: str, budget: int,
                         project_name: str = "", git_branch: str = "",
                         workstreams: list[str] | None = None) -> str:
    """Build a session-level extraction prompt combining ALL turns.

    Two-pass budget allocation (truncate, never exclude):
    1. Short turns (user+assistant <= _MIN_CHARS_PER_TURN) pass at full text
    2. Remaining budget is distributed proportionally among long turns
    3. Within each long turn's share: ~25% user text, ~75% assistant text

    All turns are always included — budget controls truncation of verbose
    responses, not exclusion of entire turns. Short turns carry decision
    signals that Haiku needs to see the full conversation flow.

    Args:
        turns: List of ALL turn dicts from parse_all_turns()
        first_user_text: Opening message from extract_first_user_message()
        budget: Character budget from compute_content_budget()
        project_name: Current project directory name
        git_branch: Current git branch name
        workstreams: Known workstream names for categorization (or None)

    Returns:
        Formatted session extraction prompt string
    """
    if not turns:
        return ""

    # Pass 1: Classify turns as short or long, measure sizes
    turn_sizes = []
    short_total = 0
    long_indices = []
    long_weights = []

    for i, turn in enumerate(turns):
        raw_size = len(turn.get("user_text", "")) + len(turn.get("assistant_text", ""))
        turn_sizes.append(raw_size)
        if raw_size <= _MIN_CHARS_PER_TURN:
            short_total += raw_size
        else:
            long_indices.append(i)
            long_weights.append(raw_size)

    # Pass 2: Distribute remaining budget among long turns
    remaining_budget = max(0, budget - short_total)
    total_long_weight = sum(long_weights) if long_weights else 1

    # Build turn content blocks
    all_tools = set()
    total_input = 0
    total_output = 0
    turn_blocks = []

    for i, turn in enumerate(turns):
        raw_size = turn_sizes[i]

        if raw_size <= _MIN_CHARS_PER_TURN:
            # Short turn — include at full text (no truncation)
            user_text = turn.get("user_text", "")
            assistant_text = turn.get("assistant_text", "")
        else:
            # Long turn — proportional share of remaining budget
            share = max(_MIN_CHARS_PER_TURN, int(remaining_budget * raw_size / total_long_weight))
            user_budget = max(50, share // 4)
            assistant_budget = share - user_budget
            user_text = truncate(turn.get("user_text", ""), user_budget)
            assistant_text = truncate(turn.get("assistant_text", ""), assistant_budget)

        tools = turn.get("tool_names", [])
        tool_str = ", ".join(tools) if tools else "None"

        block = f"### Turn {i + 1}\nUser: {user_text}\nAssistant: {assistant_text}\nTools: {tool_str}"
        turn_blocks.append(block)

        # Aggregate metadata
        all_tools.update(tools)
        out_tokens = _parse_output_tokens(turn.get("token_usage", ""))
        in_tokens = 0
        try:
            in_tokens = int(turn.get("token_usage", "").split(",")[0].strip().split()[0])
        except (ValueError, IndexError):
            pass
        total_input += in_tokens
        total_output += out_tokens

    turns_content = "\n\n".join(turn_blocks)

    # Use relevant_files from the last turn (already accumulated by parse_all_turns)
    last_turn_files = turns[-1].get("relevant_files", []) if turns else []
    files_list = "\n".join(f"- {f}" for f in last_turn_files) if last_turn_files else "None"

    all_tools_str = ", ".join(sorted(all_tools)) if all_tools else "None"
    total_usage = f"{total_input} in, {total_output} out"

    # Format known workstreams for the prompt
    if workstreams:
        workstreams_str = ", ".join(workstreams)
    else:
        workstreams_str = "None yet (suggest appropriate names)"

    return SESSION_EXTRACTION_PROMPT.format(
        first_user_text=first_user_text or "(not available)",
        turn_count=len(turns),
        turns_content=turns_content,
        all_tools=all_tools_str,
        relevant_files=files_list,
        project_name=project_name or "unknown",
        git_branch=git_branch or "unknown",
        total_token_usage=total_usage,
        known_workstreams=workstreams_str,
    )


def _log_extraction(backend: str, input_tokens: int, output_tokens: int,
                    observation_stored: bool = False, obs_id: str = None,
                    importance: float = 0.0, tags: list = None,
                    prompt: str = "", observation_content: str = "",
                    scope: str = "", hook_input: str = "",
                    debug: bool = False):
    """Log full extraction pipeline: raw hook input → prompt → result.

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
        debug: Enable logging (default False)
    """
    if not debug:
        return

    try:
        from _colors import C_GREEN, C_YELLOW, C_RESET, divider_thick, divider_section

        # Calculate costs
        input_cost = (input_tokens / 1_000_000) * 1.00
        output_cost = (output_tokens / 1_000_000) * 5.00
        total_cost = input_cost + output_cost

        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        status_color = C_GREEN if observation_stored else C_YELLOW
        status = "STORED" if observation_stored else "SKIPPED"
        tags_str = ",".join(tags) if tags else "none"

        lines = []
        lines.append(divider_thick())
        lines.append(
            f"{timestamp} | {backend:4s} | "
            f"in:{input_tokens:6d} out:{output_tokens:4d} | "
            f"${total_cost:.6f} | {status_color}{status}{C_RESET}"
        )

        # Log verbatim hook input from Claude Code
        if hook_input:
            lines.append(divider_section("HOOK INPUT"))
            lines.append(hook_input)

        # Log the prompt built for Haiku
        if prompt:
            lines.append(divider_section("PROMPT"))
            lines.append(prompt)

        # Log the result
        lines.append(divider_section("RESULT"))
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
            max_tokens=_HAIKU_MAX_TOKENS,
            temperature=0,
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
        # Prevent infinite recursion: --no-session-persistence stops the
        # spawned claude -p session from writing a transcript, so our Stop
        # hook has nothing to extract from even if it fires.
        # JARVIS_EXTRACTING env var is a safety net — hook scripts check it
        # and exit immediately if set.
        env = os.environ.copy()
        env["JARVIS_EXTRACTING"] = "1"

        result = subprocess.run(
            [claude_bin, "-p", "--model", "haiku", "--no-session-persistence"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
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


def normalize_extraction_response(parsed: dict | None) -> list[dict]:
    """Normalize Haiku response into a list of observation dicts.

    Handles both the new multi-observation schema and the legacy single-observation schema
    for backward compatibility.

    Args:
        parsed: Parsed JSON dict from Haiku response

    Returns:
        List of observation dicts (each with content, importance_score, tags, scope).
        Empty list if nothing valid.
    """
    if not parsed or not isinstance(parsed, dict):
        return []

    # New schema: {"observations": [...]}
    if "observations" in parsed:
        obs_list = parsed["observations"]
        if not isinstance(obs_list, list):
            return []
        return [
            obs for obs in obs_list
            if isinstance(obs, dict) and obs.get("content", "").strip()
        ]

    # Legacy schema: {"has_observation": true, "content": ...}
    if parsed.get("has_observation") and parsed.get("content", "").strip():
        return [{
            "content": parsed["content"],
            "importance_score": parsed.get("importance_score", 0.5),
            "tags": parsed.get("tags", []),
            "scope": parsed.get("scope", ""),
        }]

    return []


def normalize_worklog_response(parsed: dict | None) -> list[dict]:
    """Normalize Haiku response into a list of worklog dicts (max 1 element).

    Extracts the `worklog` object (singular) from the parsed JSON.
    Also accepts `worklogs` array for robustness (takes first element).

    Args:
        parsed: Parsed JSON dict from Haiku response

    Returns:
        Single-element list [worklog_dict] if valid, empty [] otherwise.
        Returns a list for consistency with the observation pipeline loop pattern.
    """
    if not parsed or not isinstance(parsed, dict):
        return []

    wl = parsed.get("worklog")

    # Also accept "worklogs" array for robustness
    if wl is None:
        worklogs_list = parsed.get("worklogs")
        if isinstance(worklogs_list, list) and worklogs_list:
            wl = worklogs_list[0]

    if not isinstance(wl, dict):
        return []

    # Validate: requires task_summary (non-empty string)
    task_summary = wl.get("task_summary", "")
    if not isinstance(task_summary, str) or not task_summary.strip():
        return []

    # Validate and default activity_type
    activity_type = wl.get("activity_type", "other")
    if activity_type not in _WORKLOG_ACTIVITY_TYPES:
        activity_type = "other"

    # Default workstream to "misc" if missing/empty
    workstream = wl.get("workstream", "misc")
    if not isinstance(workstream, str) or not workstream.strip():
        workstream = "misc"

    # Default tags
    tags = wl.get("tags", [])
    if not isinstance(tags, list):
        tags = []

    return [{
        "task_summary": task_summary.strip(),
        "workstream": workstream.strip(),
        "activity_type": activity_type,
        "tags": tags,
    }]


def jaccard_similarity(text_a: str, text_b: str) -> float:
    """Compute Jaccard word-overlap similarity between two texts.

    Args:
        text_a: First text
        text_b: Second text

    Returns:
        Jaccard similarity (0.0-1.0), 0.0 if both empty
    """
    words_a = set(text_a.lower().split())
    words_b = set(text_b.lower().split())
    if not words_a and not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


# ---------------------------------------------------------------------------
# Dedup: embedding relevance for observations, Jaccard for worklogs
# ---------------------------------------------------------------------------

_DEDUP_JACCARD_THRESHOLD = 0.7
_DEDUP_RELEVANCE_THRESHOLD = 0.95


def _log_dedup(content_type: str, text: str, matched: str, score: float,
               threshold: float, metric: str = "jaccard",
               debug: bool = False):
    """Log a dedup discard to the debug file.

    Args:
        content_type: "observation" or "worklog"
        text: Proposed new text (truncated in log)
        matched: Existing text that triggered the match (truncated in log)
        score: Similarity score (Jaccard or relevance depending on metric)
        threshold: Threshold that was exceeded
        metric: "jaccard" or "relevance" (for log labeling)
        debug: Whether debug logging is enabled
    """
    if not debug:
        return
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        TOKEN_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_LOG_FILE, "a") as f:
            f.write(
                f"{timestamp} | DEDUP {content_type:11s} | "
                f"{metric}={score:.3f} >= {threshold:.2f} | "
                f"new={text[:80]!r} | matched={matched[:80]!r}\n"
            )
    except Exception:
        pass


def _has_jaccard_duplicate(text: str, candidates: list[str],
                           threshold: float = _DEDUP_JACCARD_THRESHOLD,
                           content_type: str = "", debug: bool = False) -> bool:
    """Check if any candidate text is a Jaccard duplicate of the given text.

    Args:
        text: Proposed new text
        candidates: List of existing content strings to compare against
        threshold: Jaccard similarity threshold (default 0.7).
            Higher = more similar (1.0 = identical, 0.0 = no overlap).
            Lower threshold catches more duplicates.
        content_type: "observation" or "worklog" (for debug logging)
        debug: Whether to log dedup events to debug file

    Returns:
        True if any candidate exceeds the threshold
    """
    for existing in candidates:
        score = jaccard_similarity(text, existing)
        if score >= threshold:
            _log_dedup(content_type, text, existing, score, threshold, debug)
            return True
    return False


def is_duplicate_observation(content: str, threshold: float = _DEDUP_RELEVANCE_THRESHOLD,
                             debug: bool = False) -> bool:
    """Check if a semantically similar observation already exists globally.

    Uses ChromaDB embedding relevance score directly. If the top result's
    relevance >= threshold, the observation is considered a duplicate.
    Zero additional API calls (embeddings computed on store).

    Args:
        content: Observation text to check for duplicates
        threshold: Minimum relevance to consider duplicate (default 0.95).
            Higher = stricter (fewer false positives). Lower = more aggressive.
            Scale: 0.0 = unrelated, 1.0 = identical meaning.
        debug: Whether to log dedup events to debug file
    """
    from tools.query import query_vault

    result = query_vault(
        query=content,
        n_results=1,
        filter={"type": "observation"},
    )
    if not result.get("success") or not result.get("results"):
        return False

    top = result["results"][0]
    relevance = top.get("relevance", 0.0)

    if relevance >= threshold:
        _log_dedup("observation", content, top.get("preview", ""),
                   relevance, threshold, metric="relevance", debug=debug)
        return True
    return False


def is_duplicate_worklog(task_summary: str, session_id: str,
                         threshold: float = _DEDUP_JACCARD_THRESHOLD,
                         debug: bool = False) -> bool:
    """Check if a similar worklog already exists for this session.

    Session-scoped: queries only worklogs from the same session, then
    Jaccard confirms word-level overlap.
    """
    from tools.tier2 import tier2_list

    result = tier2_list(
        content_type="worklog",
        session_id=session_id,
        sort_by="created_at_desc",
    )
    if not result.get("success") or not result.get("documents"):
        return False

    candidates = [d.get("content", "") for d in result["documents"]]
    return _has_jaccard_duplicate(task_summary, candidates, threshold,
                                 content_type="worklog", debug=debug)


def discover_workstreams(limit: int = 30) -> list[str]:
    """Discover known workstream names from recent worklog entries.

    Queries ChromaDB for recent worklogs and extracts unique workstream
    values from metadata.

    Args:
        limit: Maximum number of recent worklogs to scan (default 30)

    Returns:
        Sorted list of unique workstream names
    """
    from tools.tier2 import tier2_list

    result = tier2_list(
        content_type="worklog",
        limit=limit,
        sort_by="created_at_desc",
    )

    if not result.get("success") or not result.get("documents"):
        return []

    workstreams = set()
    for doc in result["documents"]:
        ws = doc.get("metadata", {}).get("workstream", "")
        if ws and ws != "misc":
            workstreams.add(ws)

    return sorted(workstreams)


def store_worklog(task_summary: str, workstream: str, activity_type: str,
                  tags: list, source_label: str,
                  project_path: str = "", git_branch: str = "",
                  relevant_files: list | None = None,
                  session_id: str = "", transcript_line: int = -1) -> dict:
    """Store a worklog entry via tier2_write.

    Args:
        task_summary: What the user was working on (intent-focused)
        workstream: Workstream category (e.g., "VMPulse", "misc")
        activity_type: Type of activity (coding, debugging, etc.)
        tags: List of tags
        source_label: Source identifier
        project_path: Full path to project directory
        git_branch: Current git branch name
        relevant_files: List of file paths referenced
        session_id: Claude Code session ID
        transcript_line: Absolute line index in transcript JSONL

    Returns:
        Result dict from tier2_write
    """
    from tools.tier2 import tier2_write

    extra = {"workstream": workstream, "activity_type": activity_type}
    if project_path:
        project_dir = os.path.basename(project_path)
        extra["project_path"] = project_path
        extra["project_dir"] = project_dir
    if git_branch:
        extra["git_branch"] = git_branch
    if relevant_files:
        extra["relevant_files"] = ",".join(relevant_files)
    if session_id:
        extra["session_id"] = session_id
    if transcript_line >= 0:
        extra["transcript_line"] = str(transcript_line)

    return tier2_write(
        content=task_summary,
        content_type="worklog",
        importance_score=0.5,  # Worklogs are equally important; ordering is temporal
        source=source_label,
        tags=tags,
        extra_metadata=extra,
        skip_secret_scan=False,
    )


def main():
    """Main entry point: watermark-based multi-turn extraction pipeline.

    Flow:
    1. Read watermark → know where we left off
    2. Read new lines from transcript
    3. Parse all turns → find complete user→assistant pairs
    4. Extract first user message (from line 0, for context)
    5. Substance gate → total conversation text exceeds threshold
    6. Compute content budget from ALL turns
    7. Build session prompt with ALL turns (budget truncates, never excludes)
    8. Call Haiku → extract 1-3 observations
    9. Normalize response → handle new/legacy schema
    10. Store each observation → persist to Tier 2
    11. Advance watermark → mark position for next invocation

    Watermark advance rules:
    - No new lines: no advance (already current)
    - No complete turns: advance (incomplete data won't improve)
    - Below substance gate: advance (too little text for Haiku)
    - Haiku failure: NO advance (retry when Haiku available)
    - Empty observations: advance (Haiku evaluated, nothing interesting)
    - Observations stored: advance
    - Storage failure: advance (Haiku already ran, no point retrying)
    """
    # Args: <mcp_server_dir> <mode> <transcript_path> [session_id] [project_path] [git_branch]
    if len(sys.argv) < 4:
        print("Usage: extract_observation.py <mcp_server_dir> <mode> <transcript_path> [session_id] [project_path] [git_branch]", file=sys.stderr)
        sys.exit(1)

    mcp_server_dir = sys.argv[1]
    mode = sys.argv[2]
    transcript_path = sys.argv[3]
    session_id = sys.argv[4] if len(sys.argv) >= 5 else "unknown"
    project_path = sys.argv[5] if len(sys.argv) >= 6 else ""
    git_branch = sys.argv[6] if len(sys.argv) >= 7 else ""
    hook_input = os.environ.get("JARVIS_HOOK_INPUT", "")
    sys.path.insert(0, mcp_server_dir)

    # Load config for thresholds
    from tools.config import get_auto_extract_config, get_worklog_config
    config = get_auto_extract_config()
    debug = config.get("debug", False)
    min_chars = config.get("min_turn_chars", 200)
    max_lines = config.get("max_transcript_lines", 500)
    max_observations = config.get("max_observations", _MAX_OBSERVATIONS)

    # Dedup thresholds (observations use embedding relevance, worklogs use Jaccard)
    obs_dedup_threshold = config.get("dedup_threshold", _DEDUP_RELEVANCE_THRESHOLD)

    # Worklog config
    worklog_config = get_worklog_config()
    worklog_enabled = worklog_config.get("enabled", True)
    worklog_dedup_threshold = worklog_config.get("dedup_threshold", _DEDUP_JACCARD_THRESHOLD)

    # Step 1: Read watermark
    watermark = read_watermark(session_id)

    # Step 2: Read new transcript lines
    start_from = watermark + 1  # Start after last processed line
    indexed_lines, total_lines = read_transcript_from(transcript_path, start_from, max_lines)

    if not indexed_lines:
        print("No new transcript lines since last extraction", file=sys.stderr)
        sys.exit(0)

    # Step 3: Parse all turns
    turns = parse_all_turns(indexed_lines)
    last_line_read = indexed_lines[-1][0]  # Absolute index of last line we read

    if not turns:
        print("No complete turns in new transcript lines", file=sys.stderr)
        write_watermark(session_id, last_line_read)
        sys.exit(0)

    # Step 4: Extract first user message (from line 0, independent of watermark)
    first_user_text = extract_first_user_message(transcript_path)

    # Step 5: Substance gate — total conversation text must exceed threshold
    # This is a cost optimization: don't call Haiku on near-empty sessions.
    # If the conversation has enough text, Haiku decides what's worth remembering.
    total_text = sum(
        len(t.get("user_text", "")) + len(t.get("assistant_text", ""))
        for t in turns
    )
    if total_text < min_chars:
        print(f"Conversation too short ({total_text} chars < {min_chars})", file=sys.stderr)
        write_watermark(session_id, last_line_read)
        sys.exit(0)

    # Step 6: Compute content budget from ALL turns
    budget = compute_content_budget(turns)

    # Step 6b: Discover known workstreams for categorization
    workstreams = []
    if worklog_enabled:
        try:
            workstreams = discover_workstreams()
        except Exception:
            pass  # Non-critical — prompt will say "None yet"

    # Step 7: Build session prompt with ALL turns
    project_name = os.path.basename(project_path) if project_path else ""
    prompt = build_session_prompt(
        turns, first_user_text, budget,
        project_name=project_name, git_branch=git_branch,
        workstreams=workstreams if worklog_enabled else None,
    )

    # Step 8: Call Haiku
    extraction_result = call_haiku(prompt, mode=mode)

    if extraction_result is None:
        # Haiku failure — do NOT advance watermark (retry next time)
        print("Haiku extraction failed, watermark NOT advanced", file=sys.stderr)
        sys.exit(0)

    raw_extraction, input_tokens, output_tokens, backend = extraction_result

    # Step 9: Normalize response (observations)
    observations = normalize_extraction_response(raw_extraction)

    # Step 9b: Normalize worklog from same Haiku response (max 1)
    worklogs = []
    if worklog_enabled:
        worklogs = normalize_worklog_response(raw_extraction)

    if not observations and not worklogs:
        print("No observations or worklogs extracted (routine session)", file=sys.stderr)
        _log_extraction(backend, input_tokens, output_tokens,
                        observation_stored=False, prompt=prompt,
                        hook_input=hook_input, debug=debug)
        write_watermark(session_id, last_line_read)
        sys.exit(0)

    # Step 10: Store each observation (capped at max_observations)
    # Use relevant_files from last turn (already accumulated by parse_all_turns)
    relevant_files = turns[-1].get("relevant_files", []) if turns else []
    # Use end_line_idx from last turn for tracing
    absolute_line = turns[-1].get("end_line_idx", -1) if turns else -1

    stored_count = 0
    for i, obs in enumerate(observations[:max_observations]):
        content = obs.get("content", "").strip()
        if not content:
            continue

        importance = float(obs.get("importance_score", 0.5))
        importance = max(0.0, min(1.0, importance))  # Clamp to valid range

        tags = obs.get("tags", [])
        if not isinstance(tags, list):
            tags = []

        scope = obs.get("scope", "")
        if scope not in ("project", "global"):
            scope = ""

        if is_duplicate_observation(content, threshold=obs_dedup_threshold, debug=debug):
            print(f"Skipping duplicate observation {i + 1}", file=sys.stderr)
            continue

        result = store_observation(
            content, importance, tags, "auto-extract:stop-hook",
            project_path=project_path, git_branch=git_branch,
            relevant_files=relevant_files, scope=scope,
            session_id=session_id, transcript_line=absolute_line,
        )

        if result.get("success"):
            obs_id = result.get('id', 'unknown')
            stored_count += 1
            print(f"Stored observation {stored_count}: {obs_id}", file=sys.stderr)
            # Log prompt + hook_input only for first observation to avoid debug log bloat
            _log_extraction(backend, input_tokens, output_tokens,
                            observation_stored=True, obs_id=obs_id,
                            importance=importance, tags=tags,
                            prompt=prompt if i == 0 else "",
                            observation_content=content,
                            scope=scope,
                            hook_input=hook_input if i == 0 else "",
                            debug=debug)
        else:
            print(f"Failed to store observation: {result.get('error')}", file=sys.stderr)
            _log_extraction(backend, input_tokens, output_tokens,
                            observation_stored=False,
                            prompt=prompt if i == 0 else "",
                            observation_content=content,
                            hook_input=hook_input if i == 0 else "",
                            debug=debug)

    if stored_count == 0 and observations:
        print("No observations stored (all empty or failed)", file=sys.stderr)

    # Step 10b: Store worklog entry (if not a duplicate)
    if worklogs:
        wl = worklogs[0]  # Max 1 worklog per extraction
        if not is_duplicate_worklog(wl["task_summary"], session_id, threshold=worklog_dedup_threshold, debug=debug):
            wl_result = store_worklog(
                task_summary=wl["task_summary"],
                workstream=wl["workstream"],
                activity_type=wl["activity_type"],
                tags=wl.get("tags", []),
                source_label="auto-extract:stop-hook:worklog",
                project_path=project_path, git_branch=git_branch,
                relevant_files=relevant_files, session_id=session_id,
                transcript_line=absolute_line,
            )
            if wl_result.get("success"):
                print(f"Stored worklog: {wl_result.get('id')} [{wl['workstream']}]", file=sys.stderr)
            else:
                print(f"Failed to store worklog: {wl_result.get('error')}", file=sys.stderr)
        else:
            print("Worklog skipped (duplicate in session)", file=sys.stderr)

    # Step 11: Advance watermark (even on storage failure — Haiku already ran)
    write_watermark(session_id, last_line_read)


if __name__ == "__main__":
    main()
