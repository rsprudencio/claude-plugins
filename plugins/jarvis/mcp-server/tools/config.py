"""Jarvis configuration loader with setup verification."""
import json
import os
from pathlib import Path
from typing import Tuple

_config_cache = None


def get_config() -> dict:
    """Load config from ~/.config/jarvis/config.json with caching."""
    global _config_cache
    if _config_cache is None:
        config_path = Path.home() / ".config" / "jarvis" / "config.json"
        if config_path.exists():
            with open(config_path) as f:
                _config_cache = json.load(f)
        else:
            _config_cache = {}
    return _config_cache


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


def get_debug_info() -> dict:
    """Return diagnostic info for troubleshooting config issues."""
    config_path = Path.home() / ".config" / "jarvis" / "config.json"
    return {
        "config_path": str(config_path),
        "config_exists": config_path.exists(),
        "config_contents": get_config(),
        "resolved_vault_path": get_vault_path(),
        "cwd": os.getcwd(),
        "home": str(Path.home())
    }
