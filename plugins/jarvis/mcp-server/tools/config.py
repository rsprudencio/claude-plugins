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
        return False, "No vault_path configured. Run /jarvis-settings to set up your vault"

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
    - min_turn_chars: Minimum total text in a turn to trigger extraction (default 200)
    - max_transcript_lines: Max new lines to read from transcript per invocation (default 500)
    - debug: Enable detailed logging to ~/.jarvis/debug.auto-extraction.log (default False)

    Per-session watermarks (at ~/.jarvis/state/sessions/) replace the old global
    cooldown — each session tracks its own last-processed position independently.

    Config lives at memory.auto_extract in ~/.jarvis/config.json.
    """
    config = get_config()
    defaults = {
        "mode": "background",
        "min_turn_chars": 200,
        "max_transcript_lines": 500,
        "debug": False,
    }
    memory_config = config.get("memory", {})
    return {**defaults, **memory_config.get("auto_extract", {})}


def get_chunking_config() -> dict:
    """Get markdown chunking configuration with defaults.

    Returns config dict with:
    - enabled: Whether chunking is active (default True)
    - min_chunk_chars: Minimum chunk size before merging (default 200)
    - max_chunk_chars: Maximum chunk size before paragraph splitting (default 1500)
    - heading_levels: Which heading levels to split on (default [2, 3])

    Config lives at memory.chunking in ~/.jarvis/config.json.
    """
    config = get_config()
    defaults = {
        "enabled": True,
        "min_chunk_chars": 200,
        "max_chunk_chars": 1500,
        "heading_levels": [2, 3],
    }
    memory_config = config.get("memory", {})
    return {**defaults, **memory_config.get("chunking", {})}


def get_scoring_config() -> dict:
    """Get importance scoring configuration with defaults.

    Returns config dict with:
    - enabled: Whether scoring is active (default True)
    - recency_half_life_days: Exponential decay half-life (default 7.0)
    - type_weights: Override base weights per vault_type (default {})
    - concept_patterns: Override/extend regex->bonus patterns (default {})

    Config lives at memory.scoring in ~/.jarvis/config.json.
    """
    config = get_config()
    defaults = {
        "enabled": True,
        "recency_half_life_days": 7.0,
        "type_weights": {},
        "concept_patterns": {},
    }
    memory_config = config.get("memory", {})
    return {**defaults, **memory_config.get("scoring", {})}


def get_per_prompt_config() -> dict:
    """Get per-prompt semantic search configuration with defaults.

    Returns config dict with:
    - enabled: Master switch for per-prompt search (default True)
    - threshold: Minimum relevance score for injection (default 0.5)
    - max_results: Maximum memories to inject per prompt (default 5)
    - max_content_length: Character limit per memory preview (default 500)

    Config lives at memory.per_prompt_search in ~/.jarvis/config.json.
    """
    config = get_config()
    defaults = {
        "enabled": True,
        "threshold": 0.5,
        "max_results": 5,
        "max_content_length": 500,
        "debug": False,
    }
    memory_config = config.get("memory", {})
    return {**defaults, **memory_config.get("per_prompt_search", {})}


def get_expansion_config() -> dict:
    """Get query expansion configuration with defaults.

    Returns config dict with:
    - enabled: Whether expansion is active (default True)
    - max_expansion_terms: Cap on added terms (default 5)
    - synonyms: Override/extend trigger->terms mappings (default {})
    - intent_patterns: Custom intent patterns (default [])

    Config lives at memory.expansion in ~/.jarvis/config.json.
    """
    config = get_config()
    defaults = {
        "enabled": True,
        "max_expansion_terms": 5,
        "synonyms": {},
        "intent_patterns": [],
    }
    memory_config = config.get("memory", {})
    return {**defaults, **memory_config.get("expansion", {})}


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
