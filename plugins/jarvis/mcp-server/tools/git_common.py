"""Shared git utilities for secure vault operations.

All git operations run within the configured vault directory.
"""
import subprocess
import logging
import os
from typing import Optional, Tuple

from .config import get_verified_vault_path

logger = logging.getLogger("jarvis-core.git")

# Environment that disables git pager to prevent hanging on interactive prompts
GIT_ENV = {**os.environ, "GIT_PAGER": ""}

# Timeouts for git commands (seconds)
GIT_TIMEOUT = 30
GIT_TIMEOUT_LONG = 60  # For filter-branch and other slow operations


def run_git_command(
    args: list[str],
    timeout: int = GIT_TIMEOUT,
    check: bool = False
) -> Tuple[bool, dict]:
    """Run a git command in the vault directory.

    This is the ONLY way git commands should be executed to ensure:
    1. Commands run in the correct repository (vault)
    2. Vault setup was completed (vault_confirmed flag)
    3. Consistent error handling and logging

    Args:
        args: Git command arguments (e.g., ["status", "--short"])
        timeout: Command timeout in seconds
        check: If True, raise CalledProcessError on non-zero exit

    Returns:
        Tuple of (success: bool, result: dict)
        Result dict contains:
            - success: bool
            - stdout: str (if successful)
            - stderr: str (if failed)
            - returncode: int
            - error: str (if failed)
    """
    # Get verified vault path
    vault_path, error = get_verified_vault_path()
    if error:
        return False, {
            "success": False,
            "error": f"PERMISSION DENIED: {error}"
        }

    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            check=check,
            env=GIT_ENV,
            timeout=timeout,
            cwd=vault_path  # ✅ Always run in vault
        )

        if result.returncode != 0:
            return False, {
                "success": False,
                "returncode": result.returncode,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
                "error": result.stderr.strip() or result.stdout.strip() or "Command failed"
            }

        return True, {
            "success": True,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode
        }

    except subprocess.TimeoutExpired:
        logger.error(f"Git command timed out: git {' '.join(args)}")
        return False, {
            "success": False,
            "error": f"Command timed out after {timeout}s"
        }
    except subprocess.CalledProcessError as e:
        logger.error(f"Git command failed: {e}")
        return False, {
            "success": False,
            "returncode": e.returncode,
            "stderr": e.stderr.strip() if e.stderr else "",
            "error": e.stderr.strip() if e.stderr else str(e)
        }
    except Exception as e:
        logger.error(f"Unexpected error running git command: {e}", exc_info=True)
        return False, {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }


def get_vault_path_safe() -> Optional[str]:
    """Get vault path without error details (for logging/debugging).

    Returns:
        Vault path string or None if not configured.
    """
    vault_path, _ = get_verified_vault_path()
    return vault_path or None
