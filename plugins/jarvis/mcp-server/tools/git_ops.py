"""Git query and management operations with vault verification.

All operations run in the configured vault directory.
"""
import re
import logging
from typing import Optional

from .git_common import run_git_command, GIT_TIMEOUT_LONG

logger = logging.getLogger("jarvis-core.git_ops")


def get_status() -> dict:
    """Get current git status (staged, unstaged, untracked files).

    Returns:
        {
            "success": bool,
            "staged": list[str],
            "unstaged": list[str],
            "untracked": list[str],
            "error": str (if failed)
        }
    """
    success, result = run_git_command(["status", "--porcelain"])

    if not success:
        return {
            "success": False,
            "error": result.get("error", "Failed to get git status")
        }

    staged = []
    unstaged = []
    untracked = []

    for line in result.get("stdout", "").strip().split('\n'):
        if not line:
            continue

        status = line[:2]
        file_path = line[3:]

        # First character: staged status
        # Second character: unstaged status
        if status[0] != ' ' and status[0] != '?':
            staged.append(file_path)
        if status[1] != ' ':
            unstaged.append(file_path)
        if status[0] == '?' and status[1] == '?':
            untracked.append(file_path)

    return {
        "success": True,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked
    }


def parse_last_commit() -> dict:
    """Parse information about the most recent commit.

    Returns:
        {
            "success": bool,
            "commit_hash": str,
            "subject": str,
            "protocol_tag": str | None,
            "files_changed": int,
            "error": str (if failed)
        }
    """
    # Get commit hash
    hash_success, hash_result = run_git_command(["rev-parse", "--short", "HEAD"])
    if not hash_success:
        return {
            "success": False,
            "error": "Failed to get commit hash"
        }
    commit_hash = hash_result.get("stdout", "").strip()

    # Get subject line
    subject_success, subject_result = run_git_command(["log", "-1", "--format=%s"])
    if not subject_success:
        return {
            "success": False,
            "error": "Failed to get commit subject"
        }
    subject = subject_result.get("stdout", "").strip()

    # Get full commit message to extract protocol tag
    msg_success, msg_result = run_git_command(["log", "-1", "--format=%B"])
    protocol_tag = None
    if msg_success:
        full_message = msg_result.get("stdout", "")
        # Look for [JARVIS:...] tag
        tag_match = re.search(r'\[JARVIS:[^\]]+\]', full_message)
        if tag_match:
            protocol_tag = tag_match.group(0)

    # Get files changed count
    stat_success, stat_result = run_git_command(["diff", "--stat", "HEAD~1", "HEAD"])
    files_changed = 0
    if stat_success:
        lines = stat_result.get("stdout", "").strip().split('\n')
        if lines:
            summary = lines[-1]
            files_match = re.search(r'(\d+) files? changed', summary)
            if files_match:
                files_changed = int(files_match.group(1))

    return {
        "success": True,
        "commit_hash": commit_hash,
        "subject": subject,
        "protocol_tag": protocol_tag,
        "files_changed": files_changed
    }


def push_to_remote(branch: Optional[str] = None) -> dict:
    """Push commits to remote repository.

    Args:
        branch: Specific branch to push (optional, defaults to current)

    Returns:
        {"success": bool, "pushed_to": str, "error": str (if failed)}
    """
    if branch:
        success, result = run_git_command(["push", "origin", branch])
        pushed_to = branch
    else:
        success, result = run_git_command(["push"])
        pushed_to = "current branch"

    if not success:
        return {
            "success": False,
            "error": result.get("error", "Push failed"),
            "stderr": result.get("stderr", "")
        }

    logger.info(f"Pushed to remote: {pushed_to}")
    return {
        "success": True,
        "pushed_to": pushed_to,
        "message": result.get("stderr", "").strip()  # Git push outputs to stderr
    }


def move_files(moves: list[dict]) -> dict:
    """Move/rename files using git mv (preserves history).

    Args:
        moves: List of {"source": str, "destination": str} dicts

    Returns:
        {
            "success": bool,
            "moved": list[dict],
            "errors": list[dict] (if any failed)
        }
    """
    moved = []
    errors = []

    for move in moves:
        source = move.get("source")
        destination = move.get("destination")

        if not source or not destination:
            errors.append({
                "source": source,
                "destination": destination,
                "error": "Missing source or destination"
            })
            continue

        success, result = run_git_command(["mv", source, destination])

        if success:
            moved.append({"source": source, "destination": destination})
            logger.info(f"Moved: {source} -> {destination}")
        else:
            errors.append({
                "source": source,
                "destination": destination,
                "error": result.get("error", "Move failed")
            })

    return {
        "success": len(errors) == 0,
        "moved": moved,
        "errors": errors if errors else None
    }


def query_history(
    operation: str = "all",
    since: Optional[str] = None,
    limit: int = 10,
    file_path: Optional[str] = None
) -> dict:
    """Query Jarvis operations from git history.

    Args:
        operation: Filter by operation type (create/edit/delete/move/user/all)
        since: Time filter (e.g., "today", "1 week ago", "2025-01-01")
        limit: Max results to return
        file_path: Filter by file path (optional)

    Returns:
        {
            "success": bool,
            "operations": list[dict],
            "count": int,
            "error": str (if failed)
        }
    """
    # Build git log command
    args = ["log", f"--max-count={limit}", "--format=%H|%s|%ai"]

    if since:
        args.append(f"--since={since}")

    if file_path:
        args.extend(["--", file_path])

    success, result = run_git_command(args)

    if not success:
        return {
            "success": False,
            "error": result.get("error", "Failed to query history")
        }

    operations = []
    for line in result.get("stdout", "").strip().split('\n'):
        if not line:
            continue

        parts = line.split('|')
        if len(parts) < 3:
            continue

        commit_hash, subject, date = parts[0], parts[1], parts[2]

        # Filter by operation if not "all"
        if operation != "all":
            # Check if subject matches operation pattern
            op_patterns = {
                "create": r"Jarvis CREATE:|^\[JARVIS:C",
                "edit": r"Jarvis EDIT:|^\[JARVIS:E",
                "delete": r"Jarvis DELETE:|^\[JARVIS:D",
                "move": r"Jarvis MOVE:|^\[JARVIS:M",
                "user": r"User updates:|^\[JARVIS:U"
            }
            pattern = op_patterns.get(operation)
            if pattern and not re.search(pattern, subject):
                continue

        operations.append({
            "commit_hash": commit_hash[:7],  # Short hash
            "subject": subject,
            "date": date
        })

    return {
        "success": True,
        "operations": operations,
        "count": len(operations)
    }


def rollback_commit(commit_hash: str) -> dict:
    """Rollback a specific commit using git revert.

    Args:
        commit_hash: Commit hash to revert

    Returns:
        {
            "success": bool,
            "revert_hash": str,
            "reverted_commit": str,
            "error": str (if failed)
        }
    """
    success, result = run_git_command(["revert", "--no-edit", commit_hash])

    if not success:
        return {
            "success": False,
            "error": result.get("error", "Revert failed"),
            "stderr": result.get("stderr", "")
        }

    # Get the new revert commit hash
    hash_success, hash_result = run_git_command(["rev-parse", "--short", "HEAD"])
    revert_hash = hash_result.get("stdout", "").strip() if hash_success else "unknown"

    logger.info(f"Reverted commit {commit_hash}, new commit: {revert_hash}")
    return {
        "success": True,
        "revert_hash": revert_hash,
        "reverted_commit": commit_hash
    }


def file_history(file_path: str, limit: int = 10) -> dict:
    """Get Jarvis operation history for a specific file.

    Args:
        file_path: Path to the file (relative to vault)
        limit: Max results to return

    Returns:
        {
            "success": bool,
            "history": list[dict],
            "count": int,
            "error": str (if failed)
        }
    """
    # Use query_history with file filter
    return query_history(operation="all", limit=limit, file_path=file_path)


def rewrite_commit_messages(
    count: int = 1,
    patterns: Optional[list[str]] = None
) -> dict:
    """Rewrite recent commit messages to remove unwanted text patterns.

    WARNING: This rewrites git history. Commit hashes will change.
    Only use on unpushed commits.

    Args:
        count: Number of recent commits to process
        patterns: Sed regex patterns to remove (default: ['Co-Authored-By:.*'])

    Returns:
        {
            "success": bool,
            "commits_rewritten": int,
            "patterns_removed": list[str],
            "old_hashes": list[str],
            "new_hashes": list[str],
            "error": str (if failed)
        }
    """
    if patterns is None:
        patterns = ['Co-Authored-By:.*']

    # Get old commit hashes before rewrite
    old_success, old_result = run_git_command([
        "log", f"-{count}", "--format=%H"
    ])
    old_hashes = old_result.get("stdout", "").strip().split('\n') if old_success else []

    # Build sed expression to remove patterns
    sed_expr = ';'.join([f'/{pattern}/d' for pattern in patterns])

    # Use filter-branch to rewrite commit messages
    success, result = run_git_command(
        [
            "filter-branch", "-f", "--msg-filter",
            f"sed '{sed_expr}'",
            f"HEAD~{count}..HEAD"
        ],
        timeout=GIT_TIMEOUT_LONG
    )

    if not success:
        return {
            "success": False,
            "error": result.get("error", "Commit rewrite failed"),
            "stderr": result.get("stderr", "")
        }

    # Get new commit hashes after rewrite
    new_success, new_result = run_git_command([
        "log", f"-{count}", "--format=%H"
    ])
    new_hashes = new_result.get("stdout", "").strip().split('\n') if new_success else []

    logger.warning(f"Rewrote {count} commit(s) - history changed!")
    return {
        "success": True,
        "commits_rewritten": count,
        "patterns_removed": patterns,
        "old_hashes": [h[:7] for h in old_hashes if h],
        "new_hashes": [h[:7] for h in new_hashes if h]
    }
