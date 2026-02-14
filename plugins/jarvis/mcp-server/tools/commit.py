"""Jarvis commit operations with vault verification.

All operations run in the configured vault directory.
"""
import logging
import os
import re
from typing import Optional

from .git_common import run_git_command
from .git_ops import get_status
from .format_support import is_indexable

logger = logging.getLogger("jarvis-core.commit")


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

    # Clear staging area to prevent pre-staged files from leaking into
    # this commit. Without this, files staged by Obsidian Sync or other
    # processes would silently be included alongside our explicit list.
    run_git_command(["reset", "HEAD"])

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


def get_committed_files() -> list[str]:
    """Get the list of files changed in the most recent commit.

    Returns:
        List of vault-relative file paths from HEAD~1..HEAD.
    """
    success, result = run_git_command(["diff", "--name-only", "HEAD~1", "HEAD"])
    if not success:
        return []
    return [f for f in result.get("stdout", "").strip().split("\n") if f]


def reindex_committed_files() -> dict:
    """Sync ChromaDB with .md files changed in the most recent commit.

    Called after a successful jarvis_commit to keep the index in sync.
    For created/edited files, reindexes content. For deleted files,
    removes stale chunks from ChromaDB. Failures are logged but never
    propagated — indexing must not block the commit response.

    Returns:
        Dict with 'reindexed' (updated/created) and 'unindexed' (deleted) lists.
    """
    from .config import get_vault_path
    from .memory import index_file, unindex_file

    vault_path = get_vault_path()
    reindexed = []
    unindexed = []

    for f in get_committed_files():
        if not is_indexable(f):
            continue
        try:
            full_path = os.path.join(vault_path, f) if vault_path else f
            if os.path.isfile(full_path):
                result = index_file(f)
                if result.get("success"):
                    reindexed.append(f)
            else:
                result = unindex_file(f)
                if result.get("success") and result.get("deleted_chunks", 0) > 0:
                    unindexed.append(f)
        except Exception as e:
            logger.warning(f"Failed to sync index for {f}: {e}")

    if reindexed:
        logger.info(f"Reindexed {len(reindexed)} file(s) after commit")
    if unindexed:
        logger.info(f"Unindexed {len(unindexed)} deleted file(s) after commit")

    return {"reindexed": reindexed, "unindexed": unindexed}


def commit_user_prologue(requested_files: set) -> dict | None:
    """Commit pre-existing dirty vault files as [JARVIS:U] before a Jarvis commit.

    Examines git status and commits any files NOT in the requested set as a
    separate user-change commit.  This ensures Obsidian edits, manual changes,
    etc. get their own audit entry instead of leaking into the Jarvis commit.

    Args:
        requested_files: Set of file paths that belong to the main commit.

    Returns:
        None if no dirty files outside the request, or a dict with commit result.
    """
    # Import protocol here to avoid circular dependency at module level
    from protocol import ProtocolTag, format_commit_message

    status = get_status()
    if not status.get("success"):
        return None  # Can't determine status — skip silently

    # Collect all dirty files (union of staged, unstaged, untracked)
    all_dirty = set(status.get("staged", []))
    all_dirty.update(status.get("unstaged", []))
    all_dirty.update(status.get("untracked", []))

    # Subtract files that belong to the main commit
    user_files = sorted(all_dirty - requested_files)
    if not user_files:
        return None  # No user changes to commit

    logger.info(f"User prologue: committing {len(user_files)} dirty file(s) as [JARVIS:U]")

    # Stage only the user files
    stage_result = stage_files(user_files)
    if not stage_result["success"]:
        return stage_result

    # Build [JARVIS:U] commit
    tag = ProtocolTag(operation="user", trigger_mode="conversational")
    tag_string = tag.to_string()
    commit_msg = format_commit_message("user", "Manual vault updates", tag_string)

    commit_result = execute_commit(commit_msg)
    if not commit_result["success"]:
        return commit_result

    # Reindex any .md files from the user prologue
    index_sync = reindex_committed_files()

    result = {
        "success": True,
        "commit_hash": commit_result["commit_hash"],
        "protocol_tag": tag_string,
        "files_committed": user_files,
    }
    if index_sync["reindexed"]:
        result["reindexed"] = index_sync["reindexed"]
    if index_sync["unindexed"]:
        result["unindexed"] = index_sync["unindexed"]
    return result
