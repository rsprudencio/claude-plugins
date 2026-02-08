#!/usr/bin/env python3
"""Auto-Extract background worker: calls Haiku to extract observations from tool output.

Usage: echo '{"tool_name": ..., "tool_input": ..., "tool_result": ...}' | python3 extract_observation.py <mcp_server_dir> [mode]

The MCP server directory is passed as the first argument so we can import tools.tier2.
Mode is optional: "background" (smart, default), "background-api", or "background-cli".
"""
import json
import os
import shutil
import subprocess
import sys

# Max chars of tool output to include in extraction prompt
MAX_OUTPUT_CHARS = 2000

# Haiku model ID
HAIKU_MODEL = "claude-haiku-4-5-20251001"

EXTRACTION_PROMPT = """\
You are analyzing a tool call result to extract useful observations for a personal knowledge management system.

Tool: {tool_name}
Input: {tool_input_summary}

Output (possibly truncated):
{tool_output}

If this tool call reveals something structurally meaningful — such as codebase structure, \
user preferences, decisions made, workflow patterns, project architecture, or important context \
that would be useful to remember across sessions — extract it as a concise observation.

Respond with JSON only:
{{
  "has_observation": true/false,
  "content": "The observation text (1-3 sentences, markdown OK)",
  "importance_score": 0.3-0.8,
  "topics": ["topic1", "topic2"]
}}

If the output is routine, trivial, or contains nothing worth remembering, set has_observation to false.
Do NOT extract observations about secrets, passwords, API keys, or personal identifiable information."""


def truncate_for_prompt(text: str, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    """Truncate text for inclusion in extraction prompt."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... (truncated, {len(text)} total chars)"


def summarize_tool_input(tool_input: dict) -> str:
    """Create a brief summary of tool input for the prompt."""
    summary = json.dumps(tool_input, default=str)
    if len(summary) > 500:
        return summary[:500] + "..."
    return summary


def _build_prompt(tool_name: str, tool_input: dict, tool_output: str) -> str:
    """Build the extraction prompt for Haiku."""
    return EXTRACTION_PROMPT.format(
        tool_name=tool_name,
        tool_input_summary=summarize_tool_input(tool_input),
        tool_output=truncate_for_prompt(tool_output),
    )


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


def call_haiku_api(tool_name: str, tool_input: dict, tool_output: str) -> dict | None:
    """Call Haiku via Anthropic SDK (fast, requires ANTHROPIC_API_KEY).

    Returns:
        Parsed JSON dict with has_observation, content, importance_score, topics.
        None if API call fails or no API key found.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    try:
        import anthropic
    except ImportError:
        print("anthropic package not installed, skipping API extraction", file=sys.stderr)
        return None

    prompt = _build_prompt(tool_name, tool_input, tool_output)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text
        result = _parse_haiku_text(text)
        if result is None:
            print(f"Failed to parse Haiku API response", file=sys.stderr)
        return result
    except Exception as e:
        print(f"Haiku API call failed: {e}", file=sys.stderr)
        return None


def call_haiku_cli(tool_name: str, tool_input: dict, tool_output: str) -> dict | None:
    """Call Haiku via Claude CLI (slower, uses OAuth from Keychain).

    Uses `claude -p --model haiku` in non-interactive mode.
    Inherits OAuth credentials from the user's Claude Code installation.

    Returns:
        Parsed JSON dict with has_observation, content, importance_score, topics.
        None if CLI call fails or claude binary not found.
    """
    claude_bin = shutil.which("claude")
    if not claude_bin:
        print("claude binary not found on PATH, skipping CLI extraction", file=sys.stderr)
        return None

    prompt = _build_prompt(tool_name, tool_input, tool_output)

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

        parsed = _parse_haiku_text(result.stdout)
        if parsed is None:
            print(f"Failed to parse Haiku CLI response", file=sys.stderr)
        return parsed
    except subprocess.TimeoutExpired:
        print("Claude CLI timed out after 30s", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Claude CLI call failed: {e}", file=sys.stderr)
        return None


def call_haiku(tool_name: str, tool_input: dict, tool_output: str, mode: str = "background") -> dict | None:
    """Route extraction to the appropriate backend based on mode.

    Modes:
        background: Smart fallback — try API first, fall back to CLI.
        background-api: Force Anthropic SDK only (needs ANTHROPIC_API_KEY).
        background-cli: Force Claude CLI only (uses OAuth).

    Returns:
        Parsed JSON dict with has_observation, content, importance_score, topics.
        None if extraction fails.
    """
    if mode == "background-api":
        return call_haiku_api(tool_name, tool_input, tool_output)

    if mode == "background-cli":
        return call_haiku_cli(tool_name, tool_input, tool_output)

    # Smart "background" mode: try API first (fast), fall back to CLI
    result = call_haiku_api(tool_name, tool_input, tool_output)
    if result is not None:
        return result

    return call_haiku_cli(tool_name, tool_input, tool_output)


def store_observation(content: str, importance_score: float, topics: list, source_tool: str) -> dict:
    """Store an observation via tier2_write.

    Args:
        content: Observation text
        importance_score: 0.0-1.0
        topics: List of topic tags
        source_tool: Name of the tool that generated this observation

    Returns:
        Result dict from tier2_write
    """
    from tools.tier2 import tier2_write

    return tier2_write(
        content=content,
        content_type="observation",
        importance_score=importance_score,
        source=f"auto-extract:{source_tool}",
        topics=topics,
        skip_secret_scan=False,  # Always scan for secrets
    )


def main():
    """Main entry point: read hook data from stdin, extract and store observation."""
    # Get MCP server dir and optional mode from args
    if len(sys.argv) < 2:
        print("Usage: extract_observation.py <mcp_server_dir> [mode]", file=sys.stderr)
        sys.exit(1)

    mcp_server_dir = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) >= 3 else "background"
    sys.path.insert(0, mcp_server_dir)

    # Read hook data from stdin
    try:
        hook_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError) as e:
        print(f"Failed to read hook data: {e}", file=sys.stderr)
        sys.exit(1)

    tool_name = hook_data.get("tool_name", "unknown")
    tool_input = hook_data.get("tool_input", {})
    tool_result = hook_data.get("tool_result", "")

    # Call Haiku for extraction using the specified mode
    extraction = call_haiku(tool_name, tool_input, tool_result, mode=mode)
    if extraction is None:
        sys.exit(0)

    if not extraction.get("has_observation", False):
        sys.exit(0)

    # Store the observation
    content = extraction.get("content", "")
    if not content:
        sys.exit(0)

    importance = float(extraction.get("importance_score", 0.5))
    # Clamp importance to valid range
    importance = max(0.0, min(1.0, importance))

    topics = extraction.get("topics", [])
    if not isinstance(topics, list):
        topics = []

    result = store_observation(content, importance, topics, tool_name)
    if result.get("success"):
        print(f"Stored observation: {result.get('id')}", file=sys.stderr)
    else:
        print(f"Failed to store observation: {result.get('error')}", file=sys.stderr)


if __name__ == "__main__":
    main()
