#!/usr/bin/env python3
"""Auto-Extract background worker: calls Haiku to extract observations from tool output.

Usage: echo '{"tool_name": ..., "tool_input": ..., "tool_result": ...}' | python3 extract_observation.py <mcp_server_dir>

The MCP server directory is passed as the first argument so we can import tools.tier2.
"""
import json
import os
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


def call_haiku(tool_name: str, tool_input: dict, tool_output: str) -> dict | None:
    """Call Haiku to extract an observation from tool output.

    Returns:
        Parsed JSON dict with has_observation, content, importance_score, topics.
        None if API call fails or no observation found.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("No ANTHROPIC_API_KEY found, skipping extraction", file=sys.stderr)
        return None

    try:
        import anthropic
    except ImportError:
        print("anthropic package not installed, skipping extraction", file=sys.stderr)
        return None

    prompt = EXTRACTION_PROMPT.format(
        tool_name=tool_name,
        tool_input_summary=summarize_tool_input(tool_input),
        tool_output=truncate_for_prompt(tool_output),
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        # Parse response
        text = response.content[0].text.strip()

        # Handle potential markdown code blocks around JSON
        if text.startswith("```"):
            # Strip ```json or ``` markers
            lines = text.split("\n")
            text = "\n".join(
                line for line in lines
                if not line.strip().startswith("```")
            ).strip()

        result = json.loads(text)
        return result
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"Failed to parse Haiku response: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Haiku API call failed: {e}", file=sys.stderr)
        return None


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
    # Get MCP server dir from args
    if len(sys.argv) < 2:
        print("Usage: extract_observation.py <mcp_server_dir>", file=sys.stderr)
        sys.exit(1)

    mcp_server_dir = sys.argv[1]
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

    # Call Haiku for extraction
    extraction = call_haiku(tool_name, tool_input, tool_result)
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
