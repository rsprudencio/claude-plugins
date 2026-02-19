"""Adversarial plan review via external CLI tools.

Spawns an external AI CLI (Codex, etc.) in a read-only sandbox to stress-test
a plan before implementation. The external model acts as a devil's advocate,
identifying risks, gaps, and assumptions the author may have missed.

Architecture:
    Generic layer (~300 LOC): prompt building, JSON extraction, normalization
    Provider layer (~30 LOC each): CLI-specific command construction

Safety:
    - --sandbox read-only (no filesystem writes)
    - --ephemeral (no session persistence)
    - stdin prompt delivery (avoids ARG_MAX)
    - Best-effort MCP server disabling
    - Vault boundary enforcement for working directory

Standalone CLI usage:
    echo "plan text" | python3 adversarial_review.py '{"max_findings": 5}'
    python3 adversarial_review.py '{"max_findings": 5}' --plan-file /tmp/plan.txt

Exit codes:
    0 = review completed (even if status is "needs_revision")
    1 = execution failure (missing binary, empty plan, parse error, etc.)
"""

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Inlined platform utilities (from tools/platform_utils.py)
# ---------------------------------------------------------------------------


def _which(cmd: str) -> Optional[str]:
    """Find command in PATH with enriched fallback locations."""
    result = shutil.which(cmd)
    if result:
        return result

    home = Path.home()
    extra_dirs: list[Path] = [home / ".local" / "bin", home / ".cargo" / "bin"]
    system = platform.system()

    if system == "Darwin":
        extra_dirs += [Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    elif system == "Windows":
        local_appdata = os.environ.get("LOCALAPPDATA")
        program_files = os.environ.get("PROGRAMFILES")
        if local_appdata:
            extra_dirs += [
                Path(local_appdata) / "Programs" / "Python",
                Path(local_appdata) / "Microsoft" / "WindowsApps",
            ]
        if program_files:
            extra_dirs += [
                Path(program_files) / "Git" / "cmd",
                Path(program_files) / "Python",
            ]

    for d in extra_dirs:
        if not d.exists() or not d.is_dir():
            continue
        candidate = d / cmd
        if system == "Windows" and not candidate.suffix:
            candidate = candidate.with_suffix(".exe")
        if candidate.exists() and candidate.is_file():
            return str(candidate)

    return None


# ---------------------------------------------------------------------------
# Inlined config resolution (from tools/config.py)
# ---------------------------------------------------------------------------


def _get_vault_path() -> str:
    """Resolve vault path: JARVIS_VAULT_PATH env -> config.json -> cwd."""
    env_vault = os.environ.get("JARVIS_VAULT_PATH")
    if env_vault and os.path.isdir(env_vault):
        return env_vault

    jarvis_home = os.environ.get("JARVIS_HOME")
    config_path = (
        Path(jarvis_home) / "config.json"
        if jarvis_home
        else Path.home() / ".jarvis" / "config.json"
    )
    if config_path.exists():
        try:
            with open(config_path) as f:
                vault_path = json.load(f).get("vault_path")
            if vault_path:
                expanded = os.path.expanduser(vault_path)
                if os.path.isdir(expanded):
                    return expanded
        except (json.JSONDecodeError, OSError):
            pass

    return os.getcwd()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT_SECONDS = 240
MIN_TIMEOUT_SECONDS = 10
MAX_TIMEOUT_SECONDS = 600

DEFAULT_MAX_FINDINGS = 8
MAX_ALLOWED_FINDINGS = 20

_VALID_SEVERITIES = {"critical", "high", "medium", "low"}

_REVIEW_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["approved", "needs_revision"],
        },
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low"],
                    },
                    "title": {"type": "string"},
                    "problem": {"type": "string"},
                    "impact": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": ["id", "severity", "title", "problem", "impact", "fix"],
                "additionalProperties": False,
            },
        },
        "counter_proposal": {
            "type": "object",
            "properties": {
                "has_alternative": {"type": "boolean"},
                "description": {"type": "string"},
                "trade_offs": {"type": "string"},
            },
            "required": ["has_alternative", "description", "trade_offs"],
            "additionalProperties": False,
        },
        "agreement": {
            "type": "object",
            "properties": {
                "strengths": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "well_handled": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["strengths", "well_handled"],
            "additionalProperties": False,
        },
    },
    "required": ["status", "summary", "findings", "counter_proposal", "agreement"],
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def _normalize_timeout(value: Any) -> int:
    """Clamp timeout to [MIN, MAX] range, defaulting on bad input."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    return max(MIN_TIMEOUT_SECONDS, min(v, MAX_TIMEOUT_SECONDS))


def _normalize_max_findings(value: Any) -> int:
    """Clamp max_findings to [1, MAX_ALLOWED] range, defaulting on bad input."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_FINDINGS
    return max(1, min(v, MAX_ALLOWED_FINDINGS))


def _resolve_working_directory(cwd: Optional[str]) -> str:
    """Resolve and validate the working directory.

    Falls back to vault path if cwd is None, missing, or outside the vault.
    """
    vault = _get_vault_path()

    if not cwd:
        return vault

    expanded = os.path.realpath(os.path.expanduser(cwd))

    # Must exist
    if not os.path.isdir(expanded):
        return vault

    # Must be within vault boundary
    vault_real = os.path.realpath(vault)
    if not expanded.startswith(vault_real + os.sep) and expanded != vault_real:
        return vault

    return expanded


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _build_prompt(
    plan: str,
    context: Optional[str] = None,
    assumptions: Optional[list[str]] = None,
    focus_areas: Optional[list[str]] = None,
    max_findings: int = DEFAULT_MAX_FINDINGS,
) -> str:
    """Build the adversarial review prompt for the external CLI."""
    parts = [
        "You are a senior engineering reviewer performing an adversarial review.",
        "Your job is to find flaws, risks, and gaps in the following plan.",
        "Be thorough but fair — acknowledge what is well-designed.",
        "",
        "## Plan Under Review",
        "",
        plan,
    ]

    if context:
        parts.extend(["", "## Additional Context", "", context])

    if assumptions:
        parts.extend(["", "## Stated Assumptions"])
        for a in assumptions:
            parts.append(f"- {a}")

    if focus_areas:
        parts.extend(["", "## Focus Areas"])
        for f in focus_areas:
            parts.append(f"- {f}")

    parts.extend([
        "",
        "## Response Requirements",
        "",
        f"Return a JSON object with exactly these fields (max {max_findings} findings):",
        "- status: 'approved' or 'needs_revision'",
        "- summary: 1-2 sentence overall assessment",
        "- findings: array of {id, severity (critical/high/medium/low), title, problem, impact, fix}",
        "- counter_proposal: {has_alternative: bool, description: str, trade_offs: str}",
        "- agreement: {strengths: [str], well_handled: [str]}",
        "",
        "Respond ONLY with the JSON object, no surrounding text.",
    ])

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------


def _extract_json_object(text: str) -> Optional[dict]:
    """Extract a JSON object from potentially messy CLI output.

    Three-tier strategy:
    1. Direct parse (output is pure JSON)
    2. Fenced code block extraction (```json ... ```)
    3. Balanced brace matching (find first { ... last })
    """
    if not text or not text.strip():
        return None

    stripped = text.strip()

    # Tier 1: Direct parse
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    # Tier 2: Fenced code block
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", stripped, re.DOTALL)
    if fence_match:
        try:
            obj = json.loads(fence_match.group(1).strip())
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass

    # Tier 3: Balanced brace matching
    first_brace = stripped.find("{")
    last_brace = stripped.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = stripped[first_brace : last_brace + 1]
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass

    return None


# ---------------------------------------------------------------------------
# Payload normalization
# ---------------------------------------------------------------------------


def _normalize_review_payload(raw: dict, max_findings: int) -> dict:
    """Normalize and validate a raw review payload.

    Ensures required fields exist with correct types, clamps findings count,
    and validates severity values.
    """
    status = raw.get("status", "needs_revision")
    if status not in ("approved", "needs_revision"):
        status = "needs_revision"

    summary = str(raw.get("summary", "No summary provided"))

    # Normalize findings
    raw_findings = raw.get("findings", [])
    if not isinstance(raw_findings, list):
        raw_findings = []

    findings = []
    for i, f in enumerate(raw_findings[:max_findings]):
        if not isinstance(f, dict):
            continue
        severity = str(f.get("severity", "medium")).lower()
        if severity not in _VALID_SEVERITIES:
            severity = "medium"
        findings.append({
            "id": str(f.get("id", f"F{i + 1}")),
            "severity": severity,
            "title": str(f.get("title", "Untitled finding")),
            "problem": str(f.get("problem", "")),
            "impact": str(f.get("impact", "")),
            "fix": str(f.get("fix", "")),
        })

    # Normalize counter_proposal
    raw_cp = raw.get("counter_proposal", {})
    if not isinstance(raw_cp, dict):
        raw_cp = {}
    counter_proposal = {
        "has_alternative": bool(raw_cp.get("has_alternative", False)),
        "description": str(raw_cp.get("description", "")),
        "trade_offs": str(raw_cp.get("trade_offs", "")),
    }

    # Normalize agreement
    raw_ag = raw.get("agreement", {})
    if not isinstance(raw_ag, dict):
        raw_ag = {}
    strengths = raw_ag.get("strengths", [])
    well_handled = raw_ag.get("well_handled", [])
    agreement = {
        "strengths": [str(s) for s in strengths] if isinstance(strengths, list) else [],
        "well_handled": [str(w) for w in well_handled] if isinstance(well_handled, list) else [],
    }

    return {
        "status": status,
        "summary": summary,
        "findings": findings,
        "counter_proposal": counter_proposal,
        "agreement": agreement,
    }


# ---------------------------------------------------------------------------
# Codex provider
# ---------------------------------------------------------------------------


def _codex_build_command(
    binary: str,
    output_path: str,
    schema_path: str,
    working_dir: str,
    model: Optional[str] = None,
    profile: Optional[str] = None,
) -> list[str]:
    """Build the codex exec command line."""
    cmd = [
        binary,
        "exec",
        "--color", "never",
        "--output-last-message", output_path,
        "--output-schema", schema_path,
        "--sandbox", "read-only",
        "--ephemeral",
        "--skip-git-repo-check",
        "--cd", working_dir,
    ]
    # Best-effort MCP disable — only covers servers known at build time.
    # If user has other MCP servers in ~/.codex/config.toml or --profile
    # loads additional ones, those still run (no wildcard disable in Codex).
    for override in [
        "mcp_servers.plugin_jarvis_core.enabled=false",
        "mcp_servers.plugin_jarvis-todoist_api.enabled=false",
    ]:
        cmd.extend(["-c", override])

    if model:
        cmd.extend(["--model", model])
    if profile:
        cmd.extend(["--profile", profile])

    cmd.append("-")  # read prompt from stdin
    return cmd


def _codex_read_response(output_path: str, stdout: str) -> str:
    """Read the Codex response, preferring --output-last-message file."""
    if os.path.isfile(output_path):
        with open(output_path, "r", encoding="utf-8") as f:
            text = f.read().strip()
            if text:
                return text
    return stdout.strip()


PROVIDERS: dict[str, dict[str, Any]] = {
    "codex": {
        "binary": "codex",
        "build_command": _codex_build_command,
        "read_response": _codex_read_response,
    },
}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def adversarial_review(
    plan: str,
    provider: str = "codex",
    context: Optional[str] = None,
    assumptions: Optional[list[str]] = None,
    focus_areas: Optional[list[str]] = None,
    max_findings: int = DEFAULT_MAX_FINDINGS,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    model: Optional[str] = None,
    profile: Optional[str] = None,
    cwd: Optional[str] = None,
    include_raw: bool = False,
) -> dict:
    """Run an adversarial review of a plan using an external AI CLI.

    This is a synchronous function suitable for both direct CLI use
    and wrapping with asyncio.to_thread() in async contexts.

    Args:
        plan: The plan text to review (required, non-empty).
        provider: CLI provider name (default "codex").
        context: Additional context for the reviewer.
        assumptions: List of stated assumptions.
        focus_areas: List of areas to focus on.
        max_findings: Maximum number of findings (clamped to 1-20).
        timeout_seconds: Subprocess timeout (clamped to 10-600).
        model: Model override for the CLI.
        profile: Profile override for the CLI.
        cwd: Working directory (must be within vault).
        include_raw: If True, include raw CLI output in response.

    Returns:
        Dict with success status, review payload, and metadata.
    """
    # Validate plan
    if not plan or not plan.strip():
        return {"success": False, "error": "Plan text is required and cannot be empty"}

    # Validate provider
    if provider not in PROVIDERS:
        return {
            "success": False,
            "error": f"Unknown provider '{provider}'. Available: {', '.join(sorted(PROVIDERS))}",
        }

    prov = PROVIDERS[provider]
    binary_name: str = prov["binary"]
    build_command: Callable = prov["build_command"]
    read_response: Callable = prov["read_response"]

    # Check binary availability
    binary_path = _which(binary_name)
    if not binary_path:
        return {
            "success": False,
            "error": f"'{binary_name}' not found in PATH. Install it to use the '{provider}' provider.",
        }

    # Normalize parameters
    timeout = _normalize_timeout(timeout_seconds)
    max_f = _normalize_max_findings(max_findings)
    working_dir = _resolve_working_directory(cwd)

    # Build prompt
    prompt = _build_prompt(
        plan=plan,
        context=context,
        assumptions=assumptions,
        focus_areas=focus_areas,
        max_findings=max_f,
    )

    # Execute in temp directory for output files
    with tempfile.TemporaryDirectory() as tmpdir:
        output_path = os.path.join(tmpdir, "response.md")
        schema_path = os.path.join(tmpdir, "review_schema.json")

        # Write schema file
        with open(schema_path, "w", encoding="utf-8") as f:
            json.dump(_REVIEW_SCHEMA, f)

        # Build command
        cmd = build_command(
            binary=binary_path,
            output_path=output_path,
            schema_path=schema_path,
            working_dir=working_dir,
            model=model,
            profile=profile,
        )

        # Run subprocess with stdin prompt delivery
        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": f"Review timed out after {timeout} seconds",
                "provider": provider,
            }

        # Read response
        raw_text = read_response(output_path, result.stdout)

        if result.returncode != 0 and not raw_text:
            stderr_snippet = (result.stderr or "")[:500]
            return {
                "success": False,
                "error": f"Provider '{provider}' exited with code {result.returncode}",
                "stderr": stderr_snippet,
                "provider": provider,
            }

        # Parse JSON
        parsed = _extract_json_object(raw_text)
        if parsed is None:
            response: dict = {
                "success": False,
                "error": "Could not parse structured review from provider output",
                "provider": provider,
            }
            if include_raw:
                response["raw_output"] = raw_text[:5000]
            return response

        # Normalize
        review = _normalize_review_payload(parsed, max_f)

        response = {
            "success": True,
            "provider": provider,
            "review": review,
        }
        if include_raw:
            response["raw_output"] = raw_text[:5000]
        return response


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _cli_main() -> int:
    """CLI entry point for standalone execution.

    Usage:
        python3 adversarial_review.py [OPTIONS_JSON] [--plan-file PATH]

    Reads plan from --plan-file if provided, otherwise from stdin.
    OPTIONS_JSON is an optional JSON string with parameters:
        max_findings, context, focus_areas, timeout_seconds,
        model, profile, cwd, include_raw, provider, assumptions

    Returns exit code: 0 = review completed, 1 = failure.
    """
    options: dict = {}
    plan_file: Optional[str] = None

    # Parse arguments
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--plan-file" and i + 1 < len(args):
            plan_file = args[i + 1]
            i += 2
        elif not args[i].startswith("-"):
            # First non-flag argument is the options JSON
            try:
                options = json.loads(args[i])
            except (json.JSONDecodeError, ValueError):
                pass
            i += 1
        else:
            i += 1

    # Read plan text
    if plan_file:
        try:
            with open(plan_file, "r", encoding="utf-8") as f:
                plan = f.read()
        except OSError as e:
            result = {"success": False, "error": f"Could not read plan file: {e}"}
            print(json.dumps(result))
            return 1
    else:
        plan = sys.stdin.read()

    # Extract known parameters from options
    result = adversarial_review(
        plan=plan,
        provider=options.get("provider", "codex"),
        context=options.get("context"),
        assumptions=options.get("assumptions"),
        focus_areas=options.get("focus_areas"),
        max_findings=options.get("max_findings", DEFAULT_MAX_FINDINGS),
        timeout_seconds=options.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
        model=options.get("model"),
        profile=options.get("profile"),
        cwd=options.get("cwd"),
        include_raw=options.get("include_raw", False),
    )

    print(json.dumps(result, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(_cli_main())
