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
