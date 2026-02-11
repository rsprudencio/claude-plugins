"""Auto-Extract configuration and health checks for Stop hook.

Stop hook observes full conversation turns (not individual tool calls),
so this module only handles prerequisites checking — no filtering logic.
"""
import importlib.util
import os
import shutil


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
        "min_turn_chars": config.get("min_turn_chars", 200),
        "max_transcript_lines": config.get("max_transcript_lines", 500),
    }

    if mode == "disabled":
        return {
            "mode": mode,
            "healthy": True,
            "status": "Auto-Extract is disabled",
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
    valid_modes = "disabled, background, background-api, background-cli"
    issues.append(f"Unknown mode '{mode}' — valid modes: {valid_modes}")
    return {
        "mode": mode,
        "healthy": False,
        "status": "Auto-Extract has invalid configuration",
        "issues": issues,
        "details": details,
    }
