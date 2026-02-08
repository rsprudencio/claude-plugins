"""Vault file operations for Jarvis.

All operations verify setup was completed before proceeding:
1. vault_confirmed flag is set (setup was run)
2. Vault directory exists
3. All paths stay within vault boundaries
"""
import os
from pathlib import Path
from typing import Tuple

from .config import get_verified_vault_path

# Sensitive path components that should never be accessed, even if within vault
# These are checked as path COMPONENTS (directory/file names), not substrings
FORBIDDEN_COMPONENTS = {'.ssh', '.aws', '.gnupg', '.env'}


def validate_vault_path(relative_path: str) -> Tuple[bool, str, str]:
    """Validate that a path is safe to access within the vault.

    Args:
        relative_path: Path relative to vault root

    Returns:
        Tuple of (is_valid, full_path, error_message)
    """
    # FIRST: Verify config integrity (setup-time permission check)
    vault_path, error = get_verified_vault_path()
    if error:
        return False, "", f"PERMISSION DENIED: {error}"

    # Normalize and resolve the full path
    full_path = os.path.normpath(os.path.join(vault_path, relative_path))
    resolved = os.path.realpath(full_path)
    vault_resolved = os.path.realpath(vault_path)

    # Check vault boundary - path must be within vault
    if not (resolved.startswith(vault_resolved + os.sep) or resolved == vault_resolved):
        return False, "", f"Path escapes vault boundary: {relative_path}"

    # Check for forbidden path components (defense in depth)
    path_parts = set(Path(relative_path).parts)
    forbidden_found = path_parts & FORBIDDEN_COMPONENTS
    if forbidden_found:
        return False, "", f"Forbidden path component: {forbidden_found.pop()}"

    return True, full_path, ""


def write_vault_file(relative_path: str, content: str) -> dict:
    """Write a file within the vault directory.

    Requires setup-time permission grant (vault_confirmed in config).

    Args:
        relative_path: Path relative to vault root (e.g., "journal/2026/01/entry.md")
        content: File content to write

    Returns:
        dict with success status, path, and vault_path (or error)
    """
    valid, full_path, error = validate_vault_path(relative_path)
    if not valid:
        return {"success": False, "error": error}

    try:
        # Create parent directories if needed
        parent_dir = os.path.dirname(full_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        # Write the file
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(content)

        vault_path, _ = get_verified_vault_path()
        return {
            "success": True,
            "path": relative_path,
            "full_path": full_path,
            "vault_path": vault_path
        }
    except PermissionError:
        return {"success": False, "error": f"Permission denied writing to: {relative_path}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def read_vault_file(relative_path: str) -> dict:
    """Read a file from within the vault directory.

    Args:
        relative_path: Path relative to vault root

    Returns:
        dict with success status, content, and path (or error)
    """
    valid, full_path, error = validate_vault_path(relative_path)
    if not valid:
        return {"success": False, "error": error}

    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return {
            "success": True,
            "content": content,
            "path": relative_path
        }
    except FileNotFoundError:
        return {"success": False, "error": f"File not found: {relative_path}"}
    except PermissionError:
        return {"success": False, "error": f"Permission denied reading: {relative_path}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def list_vault_dir(relative_path: str = ".") -> dict:
    """List contents of a directory within the vault.

    Args:
        relative_path: Path relative to vault root (default: vault root)

    Returns:
        dict with success status, directories list, files list (or error)
    """
    valid, full_path, error = validate_vault_path(relative_path)
    if not valid:
        return {"success": False, "error": error}

    try:
        if not os.path.isdir(full_path):
            return {"success": False, "error": f"Not a directory: {relative_path}"}

        entries = os.listdir(full_path)
        dirs = sorted([e for e in entries if os.path.isdir(os.path.join(full_path, e))])
        files = sorted([e for e in entries if os.path.isfile(os.path.join(full_path, e))])

        return {
            "success": True,
            "path": relative_path,
            "directories": dirs,
            "files": files
        }
    except PermissionError:
        return {"success": False, "error": f"Permission denied listing: {relative_path}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def append_vault_file(relative_path: str, content: str, separator: str = "\n") -> dict:
    """Append content to an existing file within the vault directory.

    Unlike write_vault_file, this does NOT create new files — the file must
    already exist. Uses O(1) file append (no read needed).

    Args:
        relative_path: Path relative to vault root
        content: Content to append
        separator: String prepended before content (default: newline).
                   Use "" for direct concatenation.

    Returns:
        dict with success status, path, and bytes_appended (or error)
    """
    valid, full_path, error = validate_vault_path(relative_path)
    if not valid:
        return {"success": False, "error": error}

    if not os.path.isfile(full_path):
        return {"success": False, "error": f"File not found: {relative_path} (append requires existing file)"}

    try:
        payload = separator + content
        with open(full_path, 'a', encoding='utf-8') as f:
            f.write(payload)

        return {
            "success": True,
            "path": relative_path,
            "bytes_appended": len(payload.encode('utf-8'))
        }
    except PermissionError:
        return {"success": False, "error": f"Permission denied appending to: {relative_path}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def edit_vault_file(relative_path: str, old_string: str, new_string: str,
                    replace_all: bool = False) -> dict:
    """Edit a file within the vault by replacing exact string matches.

    Mirrors Claude Code's Edit tool semantics: old_string must be found in
    the file, and must be unique unless replace_all=True.

    Args:
        relative_path: Path relative to vault root
        old_string: Exact string to find and replace
        new_string: Replacement string (must differ from old_string)
        replace_all: If True, replace all occurrences. If False (default),
                     old_string must appear exactly once.

    Returns:
        dict with success status, path, and replacements count (or error)
    """
    valid, full_path, error = validate_vault_path(relative_path)
    if not valid:
        return {"success": False, "error": error}

    if not os.path.isfile(full_path):
        return {"success": False, "error": f"File not found: {relative_path}"}

    if old_string == new_string:
        return {"success": False, "error": "old_string and new_string are identical (no-op)"}

    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            file_content = f.read()

        count = file_content.count(old_string)

        if count == 0:
            return {"success": False, "error": f"old_string not found in {relative_path}"}

        if count > 1 and not replace_all:
            return {
                "success": False,
                "error": f"old_string appears {count} times in {relative_path}. "
                         f"Use replace_all=true to replace all, or provide a larger "
                         f"string with more context to make it unique."
            }

        if replace_all:
            new_content = file_content.replace(old_string, new_string)
        else:
            new_content = file_content.replace(old_string, new_string, 1)

        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(new_content)

        return {
            "success": True,
            "path": relative_path,
            "replacements": count if replace_all else 1
        }
    except PermissionError:
        return {"success": False, "error": f"Permission denied editing: {relative_path}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def file_exists_in_vault(relative_path: str) -> dict:
    """Check if a file exists within the vault.

    Args:
        relative_path: Path relative to vault root

    Returns:
        dict with success status and exists boolean (or error)
    """
    valid, full_path, error = validate_vault_path(relative_path)
    if not valid:
        return {"success": False, "error": error}

    return {
        "success": True,
        "exists": os.path.exists(full_path),
        "is_file": os.path.isfile(full_path),
        "is_dir": os.path.isdir(full_path),
        "path": relative_path
    }
