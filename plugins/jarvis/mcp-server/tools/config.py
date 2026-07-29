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
    "bedrock" (Amazon Bedrock API), and "host" (host-native Jarvis model
    service — no model loaded in the container).
    """
    defaults = {
        "model": "ibm-granite/granite-embedding-small-english-r2",
        "dimensions": 384,
        "device": "cpu",
        "backend": "onnx",
        "bedrock_region": "eu-central-1",
        "host_url": "http://host.docker.internal:8751",
        "host_model": "ibm-granite/granite-embedding-small-english-r2",
        "host_token": "",
        "host_timeout_ms": 2000,
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
        "host_url": (
            os.environ.get("JARVIS_MODEL_HOST_URL")
            or mem.get("embedding_host_url", defaults["host_url"])
        ),
        "host_model": (
            os.environ.get("JARVIS_MODEL_HOST_MODEL")
            or mem.get("embedding_host_model", defaults["host_model"])
        ),
        "host_token": (
            os.environ.get("JARVIS_MODEL_HOST_TOKEN")
            or mem.get("embedding_host_token", defaults["host_token"])
        ),
        "host_timeout_ms": int(
            os.environ.get("JARVIS_MODEL_HOST_TIMEOUT_MS")
            or mem.get("embedding_host_timeout_ms", defaults["host_timeout_ms"])
        ),
    }
    # Stable identity for database consistency checks. ``model`` may be a
    # container-local filesystem path while host inference uses an API alias;
    # both can still represent the exact same embedding space. Custom models
    # retain their runtime name unless an explicit identity is configured.
    config["model_id"] = (
        os.environ.get("JARVIS_EMBEDDING_MODEL_ID")
        or mem.get("embedding_model_id")
        or (config["host_model"] if config["backend"] == "host" else config["model"])
    )
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


def get_sync_config() -> dict:
    """Get multi-remote sync configuration with defaults.

    Resolution order: env vars > config file > defaults.
    """
    defaults = {
        "enabled": False,
        "strategy": "first-match",
        "default_action": "local-only",
        "worker_interval_seconds": 30,
        "pull_interval_seconds": 300,
        "remotes": {},
        "rules": [],
        "project_groups": {},
    }
    config = _merge_with_defaults(defaults, _get_memory_section("sync"))
    # Env var overrides
    env_enabled = os.environ.get("JARVIS_SYNC_ENABLED")
    if env_enabled is not None:
        config["enabled"] = env_enabled.lower() in ("1", "true", "yes")
    env_strategy = os.environ.get("JARVIS_SYNC_STRATEGY")
    if env_strategy:
        config["strategy"] = env_strategy
    return config


def get_project_groups_config() -> dict:
    """Get project groups configuration from the sync section.

    Project groups map logical group names to lists of project identifiers,
    used by sync rules for scoped replication.
    """
    return get_sync_config().get("project_groups", {})


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
        "contextual_embeddings": True,
        "contextual_summaries": {},
    }
    return _merge_with_defaults(defaults, _get_memory_section("chunking"))


def get_contextual_embeddings_enabled() -> bool:
    """Whether chunk embeddings and reranker inputs carry a document-context prefix.

    Rollback switch for contextual chunk augmentation. Lives in the chunking
    section (``memory.chunking.contextual_embeddings``); default True. When True,
    every place a *fragment* is embedded or reranked prepends a compact
    ``Document: …`` prefix (see tools/chunk_context.py); the stored canonical
    text is unaffected. Flip to False to fall back to bare-fragment embeddings
    (requires a force-reindex to take effect on already-stored embeddings).
    """
    return bool(get_chunking_config().get("contextual_embeddings", True))


def get_contextual_summaries_config() -> dict:
    """Get LLM-generated document summary configuration with defaults.

    Lives at ``memory.chunking.contextual_summaries``. One Haiku-written
    situating sentence per FILE is cached in ``obsidian.document_context`` and
    prepended (after the mechanical path/title line) to every fragment of that
    file at embed and rerank time.

    Generation itself is OUT OF BAND — ``bin/generate_summaries.py``. Indexing,
    vault writes, and retrieval only ever READ the cache, so none of these keys
    can add latency or cost to a runtime path.

    Keys:
        enabled: master switch. False ⇒ mechanical augmentation only.
        model: LLM used for generation (recorded alongside each cached summary).
        max_chars: cap on the summary sentence — enforced at generation AND on
            the prefix line at every augmentation site.
        body_excerpt_chars: how much of the document body the prompt may carry.
        max_generations_per_run: default ceiling on LLM calls for ONE
            ``bin/generate_summaries.py`` run (``--limit`` overrides). Files
            beyond it keep mechanical augmentation and are picked up by the next
            run — their content_hash still misses the cache.
        concurrency: default parallel generation calls for that script
            (``--concurrency`` overrides).
        timeout_seconds: per-call wall clock for one generation
            (``--timeout`` overrides).

    The nested dict is merged onto these defaults explicitly because
    ``_merge_with_defaults`` is shallow: a user who sets only ``enabled`` must
    still get the shipped model, caps, and concurrency.
    """
    defaults = {
        "enabled": True,
        "model": "claude-haiku-4-5-20251001",
        "max_chars": 200,
        "body_excerpt_chars": 2000,
        "max_generations_per_run": 500,
        "concurrency": 4,
        "timeout_seconds": 30,
    }
    return _merge_with_defaults(
        defaults, get_chunking_config().get("contextual_summaries", {})
    )


def get_contextual_augmentation_mode() -> str:
    """Resolve the active augmentation mode: 'none' | 'mechanical' | 'summary'.

    These are three DIFFERENT embedding spaces, which is why the mode (not a
    two-value flag) is what gets recorded in ``local.meta.embedding_config``
    and stamped into retrieval telemetry. A two-value flag could not tell
    "mechanical" from "summary" and would let a user silently run a mixed space.
    """
    from .chunk_context import MODE_MECHANICAL, MODE_NONE, MODE_SUMMARY

    if not get_contextual_embeddings_enabled():
        return MODE_NONE
    if get_contextual_summaries_config().get("enabled", True):
        return MODE_SUMMARY
    return MODE_MECHANICAL


def get_scoring_config() -> dict:
    """Get importance scoring configuration with defaults."""
    defaults = {
        "enabled": True,
        "recency_half_life_days": 7.0,
        "type_weights": {},
        "concept_patterns": {},
    }
    return _merge_with_defaults(defaults, _get_memory_section("scoring"))


def get_context_enrichment_config() -> dict:
    """Get context enrichment (per-prompt search) configuration with defaults."""
    defaults = {
        "enabled": True,
        "threshold": 0.85,
        "bge_logit_threshold": -4.0,
        "budget": 8000,
        "max_results": 20,
        "debug": False,
        "passive_retrieval_increment": 0.01,
        "semantic_dedup_enabled": True,
        "semantic_dedup_threshold": 0.86,
    }
    return _merge_with_defaults(defaults, _get_memory_section("context_enrichment"))


def get_retrieval_telemetry_config() -> dict:
    """Get durable semantic-retrieval telemetry configuration."""
    defaults = {
        "enabled": True,
        "retention_days": 30,
        "store_user_prompts": True,
        "candidate_detail_limit": 100,
        "shadow": {
            "enabled": True,
            "candidate_count": 20,
            "delay_seconds": 2,
            "poll_seconds": 2,
            "max_attempts": 3,
            "max_jobs_per_second": 1,
            # Backoff between failed attempts: 30s → 120s → 480s. Retrying
            # every poll cycle burns all attempts inside one model-host outage.
            "retry_base_seconds": 30,
            "retry_backoff_factor": 4,
            "retry_max_seconds": 900,
            # Shadow scoring is a background job: generous budgets, because the
            # live path's 1.5s user-facing timeout systematically censored long
            # multi-window prompts from the calibration corpus.
            "host_timeout_ms": 15000,
            "max_latency_ms": 60000,
        },
    }
    config = _merge_with_defaults(
        defaults, _get_memory_section("retrieval_telemetry")
    )
    env_enabled = os.environ.get("JARVIS_RETRIEVAL_TELEMETRY_ENABLED")
    if env_enabled is not None:
        config["enabled"] = env_enabled.lower() in ("1", "true", "yes")
    return config


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
    """Get cross-encoder reranking configuration with defaults.

    The legacy ONNX backend remains the inactive default. The selected BGE
    reranker is enabled explicitly through config or environment after its host
    service is healthy.
    """
    defaults = {
        "enabled": False,
        "backend": "onnx",
        "model": "BAAI/bge-reranker-v2-m3",
        "candidate_count": 20,
        "top_k": 10,
        "alpha": 0.7,
        "max_latency_ms": 1500,
        "batch_size": 32,
        "host_url": "http://host.docker.internal:8752",
        "host_token": "",
        "host_timeout_ms": 1500,
    }
    config = _merge_with_defaults(defaults, _get_memory_section("reranking"))

    env_enabled = os.environ.get("RERANKING_ENABLED")
    if env_enabled is not None:
        config["enabled"] = env_enabled.lower() in ("1", "true", "yes")
    string_overrides = {
        "backend": "RERANKING_BACKEND",
        "model": "RERANKING_MODEL",
        "host_url": "JARVIS_RERANK_HOST_URL",
        "host_token": "JARVIS_RERANK_HOST_TOKEN",
    }
    for key, env_name in string_overrides.items():
        if value := os.environ.get(env_name):
            config[key] = value
    integer_overrides = {
        "candidate_count": "RERANKING_CANDIDATE_COUNT",
        "max_latency_ms": "RERANKING_MAX_LATENCY_MS",
        "host_timeout_ms": "JARVIS_RERANK_HOST_TIMEOUT_MS",
    }
    for key, env_name in integer_overrides.items():
        if value := os.environ.get(env_name):
            config[key] = int(value)
    return config


def get_lexical_config() -> dict:
    """Get statistical (lexical) recall channel configuration with defaults.

    Phase 1 hybrid retrieval. The lexical channel is recall-only: rare prompt
    terms (high IDF) OR-query the generated tsvector columns to surface
    documents the bi-encoder misses. Lexical scores are never blended with
    cosines — every lexical row carries its true raw cosine for downstream
    scoring, and all rejection belongs to the final judge.

    Keys:
        enabled: master switch for the lexical channel.
        max_df_ratio: keep only lexemes whose document-frequency ratio is at or
            below this (rarity self-selects the informative term).
        max_terms: cap on informative terms per query (rarest first).
        candidate_limit: max lexical rows fetched per schema.
        lexical_rerank_slots: reserved seats in the rerank batch for
            lexical-only rows, IN ADDITION to the candidate-cap winners (low
            cosine would otherwise evict them and defeat the recall channel).
    """
    defaults = {
        "enabled": True,
        "max_df_ratio": 0.10,
        "max_terms": 8,
        "candidate_limit": 30,
        "lexical_rerank_slots": 10,
    }
    return _merge_with_defaults(defaults, _get_memory_section("lexical"))


def get_conflict_detection_config() -> dict:
    """Get conflict detection configuration with defaults."""
    defaults = {
        "enabled": True,
        "use_llm": False,
        "similarity_threshold": 0.85,
        "divergence_threshold": 0.25,
        "max_candidates": 10,
        "same_category_only": True,
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


def get_decay_config() -> dict:
    """Get importance decay configuration with defaults."""
    defaults = {
        "enabled": True,
        "rate_per_month": 0.05,
        "retrieval_half_life_days": 30,
        "retrieval_boost_max": 0.15,
        "min_importance": 0.05,
    }
    return _merge_with_defaults(defaults, _get_memory_section("decay"))


def get_ranking_config() -> dict:
    """Get unified ranking configuration with defaults.

    importance_weight scales the additive importance nudge in the unified
    score (score = similarity + importance_weight * (importance - 0.5)).
    The old similarity_weight key from the removed 0.7/0.3 blend is ignored.
    """
    defaults = {
        "importance_weight": 0.24,
        "overfetch_factor": 5,
    }
    return _merge_with_defaults(defaults, _get_memory_section("ranking"))


def get_consolidation_config() -> dict:
    """Get LLM-driven consolidation configuration with defaults."""
    defaults = {
        "enabled": False,
        "similarity_threshold": 0.85,
        "min_cluster_size": 3,
        "max_clusters": 20,
        "budget_seconds": 60,
        "confidence_threshold": 0.85,
        "auto_apply": False,
    }
    return _merge_with_defaults(defaults, _get_memory_section("consolidation"))


def get_worklog_config() -> dict:
    """Get worklog configuration with defaults."""
    defaults = {
        "enabled": True,
        "dedup_threshold": 0.5,
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
