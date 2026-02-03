"""Jarvis commit operations with vault verification.

All operations run in the configured vault directory.
"""
import logging
import re
from typing import Optional

from .git_common import run_git_command

logger = logging.getLogger("jarvis-tools.commit")


def stage_files(files: Optional[list[str]] = None, stage_all: bool = False) -> dict:
    """Stage files for commit in the vault repository.

    Args:
        files: List of specific files to stage. If None or empty, stages nothing.
        stage_all: If True, stages all changes (git add -A). Takes precedence over files.

    Returns:
        {"success": bool, "error": str (if failed), "staged_count": int}

    Note:
        - stage_all=True: Stages all changes (staged_count: -1)
        - files=[...]: Stages specific files (staged_count: len(files))
        - files=None or []: Stages nothing (staged_count: 0)

    Safety:
        This function requires an EXPLICIT stage_all=True flag to stage all files.
        Passing None or [] will NOT stage anything, preventing accidental commits.
    """
    # Explicit flag required for staging all files
    if stage_all:
        success, result = run_git_command(["add", "-A"])
        if not success:
            return {
                "success": False,
                "error": "Failed to stage all files",
                "stderr": result.get("stderr", "")
            }
        logger.info("Staged all changes (git add -A)")
        return {"success": True, "staged_count": -1}  # -1 indicates "all"

    # None or empty list stages nothing (safe default)
    if not files:
        logger.info("No files to stage (empty list or None)")
        return {"success": True, "staged_count": 0}

    # Stage specific files
    for file_path in files:
        success, result = run_git_command(["add", file_path])
        if not success:
            return {
                "success": False,
                "error": f"Failed to stage file: {file_path}",
                "stderr": result.get("stderr", "")
            }
    logger.info(f"Staged {len(files)} file(s)")
    return {"success": True, "staged_count": len(files)}


def execute_commit(commit_message: str) -> dict:
    """Execute git commit in the vault repository.

    Args:
        commit_message: The commit message

    Returns:
        {"success": bool, "commit_hash": str, "error": str (if failed)}
    """
    success, result = run_git_command(["commit", "-m", commit_message])

    if not success:
        # Check if it's "nothing to commit"
        error_output = result.get("stdout", "") + result.get("stderr", "")
        if "nothing to commit" in error_output.lower():
            return {
                "success": False,
                "error": "Nothing to commit - working tree clean",
                "nothing_to_commit": True
            }
        return {
            "success": False,
            "error": "Git commit failed",
            "stderr": result.get("stderr", ""),
            "stdout": result.get("stdout", ""),
            "exit_code": result.get("returncode", -1)
        }

    # Get commit hash
    hash_success, hash_result = run_git_command(["rev-parse", "--short", "HEAD"])
    if not hash_success:
        logger.warning("Commit succeeded but couldn't get hash")
        return {
            "success": True,
            "commit_hash": "unknown",
            "message": result.get("stdout", "").strip()
        }

    commit_hash = hash_result.get("stdout", "").strip()
    logger.info(f"Commit successful: {commit_hash}")
    return {
        "success": True,
        "commit_hash": commit_hash,
        "message": result.get("stdout", "").strip()
    }


def get_commit_stats() -> dict:
    """Get stats about the most recent commit.

    Returns:
        {"files_changed": int, "insertions": int, "deletions": int}
    """
    success, result = run_git_command(["diff", "--stat", "HEAD~1", "HEAD"])

    if not success:
        logger.warning("Could not get commit stats")
        return {"files_changed": 0, "insertions": 0, "deletions": 0}

    lines = result.get("stdout", "").strip().split('\n')
    if not lines:
        return {"files_changed": 0, "insertions": 0, "deletions": 0}

    # Last line has summary like "3 files changed, 10 insertions(+), 2 deletions(-)"
    summary = lines[-1]
    files_changed = 0
    insertions = 0
    deletions = 0

    files_match = re.search(r'(\d+) files? changed', summary)
    ins_match = re.search(r'(\d+) insertions?', summary)
    del_match = re.search(r'(\d+) deletions?', summary)

    if files_match:
        files_changed = int(files_match.group(1))
    if ins_match:
        insertions = int(ins_match.group(1))
    if del_match:
        deletions = int(del_match.group(1))

    return {
        "files_changed": files_changed,
        "insertions": insertions,
        "deletions": deletions
    }
