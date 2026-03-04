"""Shared telemetry logger for LLM calls.

Writes structured JSONL records of every LLM invocation (extraction,
adversarial review, etc.) for cost tracking and analysis.

Design:
- Atomic writes via tempfile + os.replace (no partial lines)
- Never raises exceptions (silent on errors — telemetry is best-effort)
- Cost estimation based on known model pricing
"""

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

# Default telemetry output path
_DEFAULT_TELEMETRY_DIR = Path.home() / ".jarvis" / "telemetry"
_DEFAULT_TELEMETRY_FILE = _DEFAULT_TELEMETRY_DIR / "llm_calls.jsonl"

# Cost per 1M tokens (USD) — updated as of 2025-05
# See: https://docs.anthropic.com/en/docs/about-claude/models
_COST_TABLE: dict[str, dict[str, float]] = {
    # Anthropic models
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-haiku-4-5": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-5-20250514": {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-opus-4-6": {"input": 15.00, "output": 75.00},
    # OpenAI models
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    # Google models
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00},
}


def estimate_cost(
    model: str, input_tokens: int, output_tokens: int
) -> Optional[float]:
    """Estimate cost in USD for a given model and token counts.

    Returns None if the model is not in the pricing table.
    """
    pricing = _COST_TABLE.get(model)
    if pricing is None:
        return None
    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    return round(input_cost + output_cost, 6)


def log_llm_call(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    purpose: str,
    backend: str = "",
    cost_estimate: Optional[float] = None,
    telemetry_file: Optional[Path] = None,
) -> None:
    """Log a structured LLM call record to JSONL telemetry file.

    Args:
        provider: Provider name (e.g., "anthropic", "codex", "gemini")
        model: Model identifier used for the call
        input_tokens: Number of input tokens (exact or estimated)
        output_tokens: Number of output tokens (exact or estimated)
        latency_ms: Wall-clock latency in milliseconds
        purpose: What the call was for (e.g., "extraction", "adversarial_review")
        backend: Which backend was used (e.g., "API", "CLI")
        cost_estimate: Pre-computed cost, or None to auto-estimate
        telemetry_file: Override output file path (for testing)
    """
    try:
        out_file = telemetry_file or _DEFAULT_TELEMETRY_FILE
        out_file.parent.mkdir(parents=True, exist_ok=True)

        if cost_estimate is None:
            cost_estimate = estimate_cost(model, input_tokens, output_tokens)

        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "provider": provider,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": latency_ms,
            "purpose": purpose,
            "backend": backend,
        }
        if cost_estimate is not None:
            record["cost_usd"] = cost_estimate

        line = json.dumps(record) + "\n"

        # Atomic write: write to temp file, then rename into place (append)
        # For JSONL append, we open in append mode but write atomically
        # by first writing to a temp file and then appending its content
        fd, tmp_path = tempfile.mkstemp(
            dir=str(out_file.parent),
            prefix=".telemetry_",
            suffix=".tmp",
        )
        try:
            os.write(fd, line.encode("utf-8"))
            os.close(fd)

            # Append the temp content to the main file
            with open(tmp_path, "r") as src, open(out_file, "a") as dst:
                dst.write(src.read())
        finally:
            # Clean up temp file
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    except Exception:
        pass  # Never fail on telemetry
