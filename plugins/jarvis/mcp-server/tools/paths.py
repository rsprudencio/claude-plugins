"""Configurable path resolution for Jarvis vault operations.

All vault paths are resolved through this module to ensure:
1. Paths are configurable via ~/.jarvis/config.json
2. Defaults match current behavior (backward compatible)
3. Vault-relative paths are properly joined with vault_path
4. Absolute paths get ~ expansion
5. Template variables ({YYYY}, {MM}, {WW}) are supported
"""
import os
from pathlib import Path
from typing import Optional

from . import config as _config


# --- Default path values (match current hardcoded behavior) ---

_VAULT_RELATIVE_DEFAULTS = {
    "journal_jarvis": "journal/jarvis",
    "journal_daily": "journal/daily",
    "journal_summaries": "journal/jarvis/{YYYY}/summaries",
    "notes": "notes",
    "work": "work",
    "inbox": "inbox",
    "inbox_todoist": "inbox/todoist",
    "templates": "templates",
    "people": "people",
    "documents": "documents",
    "strategic": ".jarvis/strategic",
    "observations_promoted": "journal/jarvis/observations",
}

_ABSOLUTE_DEFAULTS = {
    "db_path": "~/.jarvis/memory_db",
    "project_memories_path": "~/.jarvis/memories",
}

# Set of path names that are absolute (not vault-relative)
_ABSOLUTE_PATHS = set(_ABSOLUTE_DEFAULTS.keys())

# Set of path names considered sensitive (ask-first access)
SENSITIVE_PATHS = {"people", "documents"}


class PathNotConfiguredError(Exception):
    """Raised when a path name is not in config or defaults."""
    pass


def get_path(
    name: str,
    substitutions: Optional[dict] = None,
    ensure_exists: bool = False,
) -> str:
    """Resolve a named path to an absolute filesystem path.

    Args:
        name: Path identifier (e.g., "journal_jarvis", "inbox", "db_path")
        substitutions: Template variable replacements (e.g., {"YYYY": "2026"})
        ensure_exists: If True, create the directory if it does not exist

    Returns:
        Absolute path string

    Raises:
        PathNotConfiguredError: If name is unknown
        ValueError: If vault_path is not configured (for vault-relative paths)
    """
    config = _config.get_config()
    is_absolute = name in _ABSOLUTE_PATHS

    # 1. Look up in config, fall back to defaults
    if is_absolute:
        raw = config.get("memory", {}).get(name, _ABSOLUTE_DEFAULTS.get(name))
    else:
        raw = config.get("paths", {}).get(name, _VAULT_RELATIVE_DEFAULTS.get(name))

    if raw is None:
        raise PathNotConfiguredError(
            f"Unknown path name: '{name}'. "
            f"Valid names: {sorted(list(_VAULT_RELATIVE_DEFAULTS) + list(_ABSOLUTE_DEFAULTS))}"
        )

    # 2. Apply template substitutions
    if substitutions:
        for key, value in substitutions.items():
            raw = raw.replace(f"{{{key}}}", str(value))

    # 3. Resolve to absolute path
    if is_absolute:
        resolved = os.path.expanduser(os.path.expandvars(raw))
    else:
        vault_path, error = _config.get_verified_vault_path()
        if error:
            raise ValueError(f"Cannot resolve vault-relative path '{name}': {error}")
        resolved = os.path.normpath(os.path.join(vault_path, raw))

    # 4. Optionally ensure directory exists
    if ensure_exists:
        os.makedirs(resolved, exist_ok=True)

    return resolved


def get_relative_path(name: str) -> str:
    """Get the raw relative path string (without vault_path prefix).

    Useful for passing to MCP tools that expect vault-relative paths.

    Args:
        name: Path identifier (must be vault-relative, not absolute)

    Returns:
        Raw relative path string (e.g., "journal/jarvis")

    Raises:
        ValueError: If name is an absolute path
        PathNotConfiguredError: If name is unknown
    """
    if name in _ABSOLUTE_PATHS:
        raise ValueError(f"'{name}' is an absolute path, not vault-relative")

    config = _config.get_config()
    result = config.get("paths", {}).get(name, _VAULT_RELATIVE_DEFAULTS.get(name))
    if result is None:
        raise PathNotConfiguredError(
            f"Unknown path name: '{name}'. "
            f"Valid vault-relative names: {sorted(_VAULT_RELATIVE_DEFAULTS)}"
        )
    return result


def is_sensitive_path(name: str) -> bool:
    """Check if a named path is classified as sensitive (ask-first access)."""
    return name in SENSITIVE_PATHS


def validate_paths_config() -> list:
    """Validate all paths in config. Returns list of warnings.

    Checks for:
    - Unknown path keys (typos)
    - Absolute paths in vault-relative section
    - Path traversal (..)
    """
    warnings = []
    config = _config.get_config()

    paths = config.get("paths", {})
    for name, value in paths.items():
        if name not in _VAULT_RELATIVE_DEFAULTS:
            warnings.append(f"Unknown path key: '{name}' (will be ignored)")
        if os.path.isabs(value):
            warnings.append(f"Path '{name}' should be relative, got absolute: '{value}'")
        if ".." in Path(value).parts:
            warnings.append(f"Path '{name}' contains traversal: '{value}'")

    memory = config.get("memory", {})
    for name, value in memory.items():
        if name not in _ABSOLUTE_DEFAULTS and name not in (
            "secret_detection", "importance_scoring",
            "recency_boost_days", "default_importance",
        ):
            warnings.append(f"Unknown memory key: '{name}' (will be ignored)")

    return warnings


def list_all_paths() -> dict:
    """Return all configured paths with their resolved values.

    Used by diagnostic tools (jarvis_list_paths).
    """
    config = _config.get_config()
    result = {"vault_relative": {}, "absolute": {}}

    for name in _VAULT_RELATIVE_DEFAULTS:
        try:
            result["vault_relative"][name] = {
                "configured": config.get("paths", {}).get(name),
                "default": _VAULT_RELATIVE_DEFAULTS[name],
                "resolved": get_path(name),
            }
        except (ValueError, PathNotConfiguredError):
            result["vault_relative"][name] = {
                "configured": config.get("paths", {}).get(name),
                "default": _VAULT_RELATIVE_DEFAULTS[name],
                "resolved": None,
                "error": "vault_path not configured",
            }

    for name in _ABSOLUTE_DEFAULTS:
        result["absolute"][name] = {
            "configured": config.get("memory", {}).get(name),
            "default": _ABSOLUTE_DEFAULTS[name],
            "resolved": get_path(name),
        }

    return result
