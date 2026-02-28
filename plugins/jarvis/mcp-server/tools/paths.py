"""Configurable path resolution for Jarvis vault operations.

Shared path infrastructure is provided by jarvis_common.paths and
re-exported here for backward compatibility.
"""

# Re-export all shared path functions — no import changes needed anywhere
from jarvis_common.paths import (  # noqa: F401
    PathNotConfiguredError,
    SENSITIVE_PATHS,
    _VAULT_RELATIVE_DEFAULTS,
    _ABSOLUTE_DEFAULTS,
    get_path,
    get_relative_path,
    is_sensitive_path,
    validate_paths_config,
    list_all_paths,
)
