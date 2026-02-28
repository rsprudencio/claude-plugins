"""Shared Jarvis configuration loader.

Provides config reading, vault path resolution, and verification
used by all Jarvis MCP servers. Core-specific getters (scoring,
chunking, etc.) remain in the core plugin's tools/config.py.
"""

import json
import os
from pathlib import Path
from typing import Tuple

_config_cache = None


# ── Merge helpers ──────────────────────────────────────────────────────


def _as_dict(value) -> dict:
    """Return *value* if it is a dict, otherwise an empty dict."""
    return value if isinstance(value, dict) else {}


def _merge_with_defaults(defaults: dict, overrides) -> dict:
    """Shallow-merge *overrides* (may be non-dict) onto *defaults*."""
    return {**defaults, **_as_dict(overrides)}


def _get_config_section(section: str) -> dict:
    """Return a top-level config section as a dict (safe for missing keys)."""
    return _as_dict(get_config().get(section, {}))


def _get_memory_section(subsection: str | None = None) -> dict:
    """Return the memory config, or a nested subsection of it."""
    mem = _get_config_section("memory")
    if subsection is None:
        return mem
    return _as_dict(mem.get(subsection, {}))


# ── Path resolution ───────────────────────────────────────────────────


def _resolve_jarvis_home() -> Path:
    """Resolve the Jarvis home directory.

    Checks JARVIS_HOME env var first, falls back to ~/.jarvis.
    """
    env_home = os.environ.get("JARVIS_HOME")
    if env_home:
        return Path(env_home)
    return Path.home() / ".jarvis"


def get_config() -> dict:
    """Load config from $JARVIS_HOME/config.json with caching.

    Config path resolution order:
    1. JARVIS_HOME env var (for Docker)
    2. ~/.jarvis/config.json (default)
    """
    global _config_cache
    if _config_cache is None:
        config_path = _resolve_jarvis_home() / "config.json"
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
    """Get vault_path, checking env var first, then config, then cwd.

    Resolution order:
    1. JARVIS_VAULT_PATH env var (for Docker)
    2. vault_path in config.json
    3. Current working directory (fallback)

    WARNING: This does NOT verify config integrity. For secure operations,
    use get_verified_vault_path() instead.
    """
    env_vault = os.environ.get("JARVIS_VAULT_PATH")
    if env_vault and os.path.isdir(env_vault):
        return env_vault
    config = get_config()
    vault_path = config.get("vault_path")
    if vault_path:
        return os.path.expanduser(vault_path)
    return os.getcwd()


def verify_config() -> Tuple[bool, str]:
    """Verify config exists and was set up properly.

    Checks:
    1. vault_path is configured (via env var or config)
    2. vault_confirmed flag is set (setup was run) — skipped in Docker mode
    3. Vault directory exists

    Docker mode: When JARVIS_VAULT_PATH env var is set, skip the
    vault_confirmed check since Docker config is managed externally.

    Returns:
        Tuple of (is_valid, error_message). If valid, error_message is empty.
    """
    # In Docker mode (env var set), use env var directly
    env_vault = os.environ.get("JARVIS_VAULT_PATH")
    if env_vault:
        if not os.path.isdir(env_vault):
            return False, f"Vault directory not found: {env_vault}"
        return True, ""

    config = get_config()

    # Check vault_path exists
    if not config.get("vault_path"):
        return (
            False,
            "No vault_path configured. Run /jarvis-settings to set up your vault",
        )

    # Check setup was completed (not just a random config file)
    if not config.get("vault_confirmed"):
        return False, "Vault not confirmed. Run /jarvis-settings to complete setup"

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

    Uses get_vault_path() which respects JARVIS_VAULT_PATH env var.

    Returns:
        Tuple of (vault_path, error). If error, vault_path is empty.
    """
    valid, error = verify_config()
    if not valid:
        return "", error
    return get_vault_path(), ""


def get_file_format() -> str:
    """Get configured file format for new file creation.

    Returns 'md' or 'org'. Defaults to 'md' if not set or invalid.
    Config key: file_format (top-level in ~/.jarvis/config.json).
    """
    config = get_config()
    fmt = config.get("file_format", "md")
    return fmt if fmt in ("md", "org") else "md"


def get_chroma_config() -> dict:
    """Get ChromaDB HTTP client configuration.

    Resolution order: env vars (Docker) > config file > defaults.

    Connection can be specified as either:
    - A single URL: ``chroma_url: "https://chroma.example.com:8743"``
    - Separate fields: ``chroma_host``, ``chroma_port``, ``chroma_ssl``

    Auth can be specified as either:
    - Convenience: ``chroma_api_key`` + ``chroma_auth_header`` (default X-Chroma-Token)
    - Manual: ``chroma_headers: {"Authorization": "Bearer xxx"}``
    """
    memory = _get_memory_section()

    # --- Resolve connection endpoint ---
    raw_url = (
        os.environ.get("CHROMA_URL")
        or memory.get("chroma_url", "")
    )
    if raw_url:
        host, port, ssl = _parse_chroma_url(raw_url)
    else:
        host = os.environ.get("CHROMA_HOST") or memory.get("chroma_host", "localhost")
        try:
            port = int(os.environ.get("CHROMA_PORT") or memory.get("chroma_port", 8743))
        except (TypeError, ValueError):
            port = 8743
        ssl = memory.get("chroma_ssl", False)

    # --- Resolve auth headers ---
    headers = dict(memory.get("chroma_headers", {}))
    api_key = os.environ.get("CHROMA_API_KEY") or memory.get("chroma_api_key", "")
    if api_key:
        auth_header = memory.get("chroma_auth_header", "X-Chroma-Token")
        headers[auth_header] = api_key

    return {
        "host": host,
        "port": port,
        "ssl": ssl,
        "headers": headers,
        "data_path": (
            os.environ.get("CHROMA_DATA_PATH")
            or memory.get("chroma_data_path", "~/.jarvis/db")
        ),
    }


def _parse_chroma_url(raw_url: str) -> tuple:
    """Parse a ChromaDB URL into (host, port, ssl).

    Accepts: "host:port", "http://host:port", "https://host:port"
    """
    from urllib.parse import urlparse

    raw_url = raw_url.strip()
    if "://" not in raw_url:
        raw_url = f"http://{raw_url}"
    parsed = urlparse(raw_url)
    if not parsed.hostname:
        raise ValueError(f"Invalid chroma_url: cannot parse hostname from '{raw_url}'")
    ssl = parsed.scheme == "https"
    port = parsed.port or (443 if ssl else 8743)
    return parsed.hostname, int(port), ssl
