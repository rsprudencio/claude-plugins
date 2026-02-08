"""Jarvis configuration loader with setup verification."""
import json
import os
from pathlib import Path
from typing import Tuple

_config_cache = None


def get_config() -> dict:
    """Load config from ~/.jarvis/config.json with caching."""
    global _config_cache
    if _config_cache is None:
        config_path = Path.home() / ".jarvis" / "config.json"
        if config_path.exists():
            with open(config_path) as f:
                _config_cache = json.load(f)
        else:
            _config_cache = {}
    return _config_cache


def clear_config_cache():
    """Invalidate the cached config, forcing a re-read on next access."""
    global _config_cache
    _config_cache = None


def get_vault_path() -> str:
    """Get vault_path from config, falling back to cwd if not set.

    WARNING: This does NOT verify config integrity. For secure operations,
    use get_verified_vault_path() instead.
    """
    config = get_config()
    vault_path = config.get("vault_path")
    if vault_path:
        return os.path.expanduser(vault_path)
    return os.getcwd()


def verify_config() -> Tuple[bool, str]:
    """Verify config exists and was set up properly.

    Checks:
    1. vault_path is configured
    2. vault_confirmed flag is set (setup was run)
    3. Vault directory exists

    Returns:
        Tuple of (is_valid, error_message). If valid, error_message is empty.
    """
    config = get_config()

    # Check vault_path exists
    if not config.get("vault_path"):
        return False, "No vault_path configured. Run /jarvis:jarvis-setup"

    # Check setup was completed (not just a random config file)
    if not config.get("vault_confirmed"):
        return False, "Vault not confirmed. Run /jarvis:jarvis-setup to grant write access"

    # Verify vault directory exists
    vault_path = os.path.expanduser(config["vault_path"])
    if not os.path.isdir(vault_path):
        return False, f"Vault directory not found: {vault_path}"

    return True, ""


def get_verified_vault_path() -> Tuple[str, str]:
    """Get vault path after verifying setup was completed.

    This should be used for all write operations to ensure:
    1. Setup was run (vault_confirmed is set)
    2. Vault directory exists

    Returns:
        Tuple of (vault_path, error). If error, vault_path is empty.
    """
    valid, error = verify_config()
    if not valid:
        return "", error
    return os.path.expanduser(get_config()["vault_path"]), ""


def get_memory_config() -> dict:
    """Get memory subsystem configuration with defaults.

    Returns config dict with keys: secret_detection, importance_scoring,
    recency_boost_days, default_importance.
    Backward-compatible: configs without 'memory' section get defaults.
    """
    config = get_config()
    defaults = {
        "secret_detection": True,
        "importance_scoring": True,
        "recency_boost_days": 7,
        "default_importance": "medium",
    }
    return {**defaults, **config.get("memory", {})}


def get_promotion_config() -> dict:
    """Get promotion subsystem configuration with defaults.
    
    Returns config dict with promotion thresholds and behavior:
    - importance_threshold: Minimum importance score to auto-promote (0.85)
    - retrieval_count_threshold: Min retrieval count for promotion (3)
    - age_importance_days: Days after which age+importance combo triggers (30)
    - age_importance_score: Importance threshold for aged content (0.7)
    - on_promoted_file_deleted: What to do when promoted file is deleted
      ("remove" or "revert_to_chromadb")
    
    Backward-compatible: configs without 'promotion' section get defaults.
    """
    config = get_config()
    defaults = {
        "importance_threshold": 0.85,
        "retrieval_count_threshold": 3,
        "age_importance_days": 30,
        "age_importance_score": 0.7,
        "on_promoted_file_deleted": "remove",
    }
    return {**defaults, **config.get("promotion", {})}


def get_auto_extract_config() -> dict:
    """Get auto-extract configuration with defaults.

    Returns config dict with:
    - mode: Extraction mode (default "background"). Options:
        - "disabled": No extraction
        - "background": Smart fallback — tries API first, falls back to CLI
        - "background-api": Force Anthropic SDK (requires ANTHROPIC_API_KEY)
        - "background-cli": Force Claude CLI (uses OAuth from Keychain)
        - "inline": Uses session model via systemMessage
    - max_observations_per_session: Max observations per session (default 100)
    - skip_tools_add: Additional tools to skip (user-defined)
    - skip_tools_remove: Tools to un-skip from defaults (user-defined)

    Config lives at memory.auto_extract in ~/.jarvis/config.json.
    """
    config = get_config()
    defaults = {
        "mode": "background",
        "max_observations_per_session": 100,
        "skip_tools_add": [],
        "skip_tools_remove": [],
    }
    memory_config = config.get("memory", {})
    return {**defaults, **memory_config.get("auto_extract", {})}


def get_debug_info() -> dict:
    """Return diagnostic info for troubleshooting config issues."""
    from .auto_extract_config import check_prerequisites

    config_path = Path.home() / ".jarvis" / "config.json"
    return {
        "config_path": str(config_path),
        "config_exists": config_path.exists(),
        "config_contents": get_config(),
        "resolved_vault_path": get_vault_path(),
        "cwd": os.getcwd(),
        "home": str(Path.home()),
        "auto_extract": check_prerequisites(get_auto_extract_config()),
    }
