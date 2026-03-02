"""Jarvis configuration loader with setup verification.

Shared config infrastructure is provided by jarvis_common.config and
re-exported here for backward compatibility. Core-specific getters
(scoring, chunking, etc.) remain in this module.
"""

import os
from pathlib import Path

import jarvis_common.config as _common_config

# Bridge: _config_cache lives in jarvis_common.config (canonical location).
# Tests that set config_module._config_cache = None are patching this module,
# not jarvis_common — so we keep a local reference that the conftest clears
# alongside the canonical one.
_config_cache = None


# Re-export shared config functions — no import changes needed anywhere
from jarvis_common.config import (  # noqa: F401
    get_config,
    clear_config_cache,
    get_vault_path,
    verify_config,
    get_verified_vault_path,
    get_file_format,
    _as_dict,
    _merge_with_defaults,
    _resolve_jarvis_home,
)


# Override _get_config_section and _get_memory_section to call the
# module-level get_config (this module's re-exported copy), so that
# @patch("tools.config.get_config") in tests works correctly.
def _get_config_section(section: str) -> dict:
    """Return a top-level config section as a dict (safe for missing keys)."""
    return _as_dict(get_config().get(section, {}))


def _get_memory_section(subsection: str | None = None) -> dict:
    """Return the memory config, or a nested subsection of it."""
    mem = _get_config_section("memory")
    if subsection is None:
        return mem
    return _as_dict(mem.get(subsection, {}))


# ── Core-specific getters ─────────────────────────────────────────────
# These are only needed by jarvis-core, not by jarvis-obsidian.


def get_embedding_config() -> dict:
    """Get embedding model configuration with defaults.

    Resolution order: env vars > config file > defaults.

    Supported backends: "onnx" (local ONNX Runtime), "torch" (PyTorch),
    "bedrock" (Amazon Bedrock API — no local model needed).
    """
    defaults = {
        "model": "ibm-granite/granite-embedding-small-english-r2",
        "dimensions": 384,
        "device": "cpu",
        "backend": "onnx",
        "bedrock_region": "eu-central-1",
    }
    mem = _get_memory_section()
    config = {
        "model": (
            os.environ.get("EMBEDDING_MODEL")
            or mem.get("embedding_model", defaults["model"])
        ),
        "dimensions": int(
            os.environ.get("EMBEDDING_DIMENSIONS")
            or mem.get("embedding_dimensions", defaults["dimensions"])
        ),
        "device": (
            os.environ.get("EMBEDDING_DEVICE")
            or mem.get("embedding_device", defaults["device"])
        ),
        "backend": (
            os.environ.get("EMBEDDING_BACKEND")
            or mem.get("embedding_backend", defaults["backend"])
        ),
        "bedrock_region": (
            os.environ.get("BEDROCK_REGION")
            or mem.get("embedding_bedrock_region", defaults["bedrock_region"])
        ),
    }
    return config


def get_postgres_config() -> dict:
    """Get PostgreSQL connection configuration.

    Resolution order: POSTGRES_URL env var > config file > default.
    """
    mem = _get_memory_section()
    url = (
        os.environ.get("POSTGRES_URL")
        or mem.get("postgres_url", "postgresql://jarvis:jarvis@localhost:5432/jarvis")
    )
    return {"url": url}


def get_replication_config() -> dict:
    """Get replication configuration with defaults.

    Resolution order: env vars > config file > defaults.
    """
    defaults = {
        "mode": "disabled",
        "central_url": "",
        "node_id": "",
        "publications": ["jarvis_pub"],
        "replication_user": "jarvis_repl",
    }
    config = _merge_with_defaults(defaults, _get_memory_section("replication"))
    # Env var overrides
    env_mode = os.environ.get("JARVIS_REPLICATION_MODE")
    if env_mode:
        config["mode"] = env_mode
    env_url = os.environ.get("JARVIS_CENTRAL_URL")
    if env_url:
        config["central_url"] = env_url
    return config


def get_memory_config() -> dict:
    """Get memory subsystem configuration with defaults."""
    defaults = {
        "secret_detection": True,
        "importance_scoring": True,
        "recency_boost_days": 7,
        "default_importance": 0.5,
    }
    return _merge_with_defaults(defaults, _get_memory_section())


def get_auto_extract_config() -> dict:
    """Get auto-extract configuration with defaults."""
    defaults = {
        "mode": "background",
        "min_turn_chars": 200,
        "max_transcript_lines": 500,
        "max_observations": 3,
        "dedup_threshold": 0.95,
        "debug": False,
    }
    return _merge_with_defaults(defaults, _get_memory_section("auto_extract"))


def get_chunking_config() -> dict:
    """Get markdown chunking configuration with defaults."""
    defaults = {
        "enabled": True,
        "min_chunk_chars": 200,
        "max_chunk_chars": 1500,
        "heading_levels": [2, 3],
    }
    return _merge_with_defaults(defaults, _get_memory_section("chunking"))


def get_scoring_config() -> dict:
    """Get importance scoring configuration with defaults."""
    defaults = {
        "enabled": True,
        "recency_half_life_days": 7.0,
        "type_weights": {},
        "concept_patterns": {},
    }
    return _merge_with_defaults(defaults, _get_memory_section("scoring"))


def get_per_prompt_config() -> dict:
    """Get per-prompt semantic search configuration with defaults."""
    defaults = {
        "enabled": True,
        "threshold": 0.5,
        "budget": 8000,
        "debug": False,
        "passive_retrieval_increment": 0.01,
    }
    return _merge_with_defaults(defaults, _get_memory_section("per_prompt_search"))


def get_expansion_config() -> dict:
    """Get query expansion configuration with defaults."""
    defaults = {
        "enabled": True,
        "max_expansion_terms": 5,
        "synonyms": {},
        "intent_patterns": [],
    }
    return _merge_with_defaults(defaults, _get_memory_section("expansion"))


def get_reranking_config() -> dict:
    """Get cross-encoder reranking configuration with defaults."""
    defaults = {
        "enabled": True,
        "candidate_count": 100,
        "top_k": 10,
        "alpha": 0.7,
        "max_latency_ms": 1000,
        "batch_size": 32,
    }
    return _merge_with_defaults(defaults, _get_memory_section("reranking"))


def get_conflict_detection_config() -> dict:
    """Get conflict detection configuration with defaults."""
    defaults = {
        "enabled": True,
        "use_llm": False,
        "similarity_threshold": 0.7,
        "divergence_threshold": 0.4,
        "max_candidates": 10,
    }
    return _merge_with_defaults(defaults, _get_memory_section("conflict_detection"))


def get_staleness_config() -> dict:
    """Get observation staleness tracking configuration with defaults."""
    defaults = {"enabled": True, "penalty": 0.15}
    return _merge_with_defaults(defaults, _get_memory_section("staleness"))


def get_pattern_detection_config() -> dict:
    """Get pattern detection configuration with defaults."""
    defaults = {
        "enabled": True,
        "scan_interval_seconds": 300,
        "similarity_threshold": 0.3,
        "promotion_threshold": 3,
        "max_candidates": 200,
        "candidate_expiry_days": 7,
        "lookback_minutes": 10,
        "merge_threshold": 0.7,
    }
    return _merge_with_defaults(defaults, _get_memory_section("pattern_detection"))


def get_todoist_prompt_alerts_config() -> dict:
    """Get Todoist per-prompt alert configuration with defaults."""
    defaults = {
        "enabled": False,
        "sync_interval_seconds": 900,
        "max_per_category": 3,
        "api_timeout_seconds": 5,
        "debug": False,
    }
    todoist_alerts = _as_dict(_get_config_section("todoist").get("prompt_alerts", {}))
    return _merge_with_defaults(defaults, todoist_alerts)


def get_worklog_config() -> dict:
    """Get worklog configuration with defaults."""
    defaults = {
        "enabled": True,
        "dedup_threshold": 0.7,
    }
    return _merge_with_defaults(defaults, _get_memory_section("worklog"))


def get_debug_info() -> dict:
    """Return diagnostic info for troubleshooting config issues."""
    from .auto_extract_config import check_prerequisites

    config_path = _resolve_jarvis_home() / "config.json"
    return {
        "config_path": str(config_path),
        "config_exists": config_path.exists(),
        "config_contents": get_config(),
        "resolved_vault_path": get_vault_path(),
        "cwd": os.getcwd(),
        "home": str(Path.home()),
        "jarvis_home": str(_resolve_jarvis_home()),
        "docker_mode": bool(os.environ.get("JARVIS_VAULT_PATH")),
        "auto_extract": check_prerequisites(get_auto_extract_config()),
    }
