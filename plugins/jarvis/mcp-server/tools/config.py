"""Jarvis configuration loader with setup verification."""

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


def get_memory_config() -> dict:
    """Get memory subsystem configuration with defaults.

    Returns config dict with keys: secret_detection, importance_scoring,
    recency_boost_days, default_importance.
    Backward-compatible: configs without 'memory' section get defaults.
    """
    defaults = {
        "secret_detection": True,
        "importance_scoring": True,
        "recency_boost_days": 7,
        "default_importance": 0.5,
    }
    return _merge_with_defaults(defaults, _get_memory_section())


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
    defaults = {
        "importance_threshold": 0.85,
        "retrieval_count_threshold": 3,
        "age_importance_days": 30,
        "age_importance_score": 0.7,
        "on_promoted_file_deleted": "remove",
    }
    return _merge_with_defaults(defaults, _get_config_section("promotion"))


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
    - dedup_threshold: Embedding relevance threshold for observation dedup (default 0.95).
        Higher = stricter (fewer false positives). Lower = catches more duplicates.
        Scale: 0.0 = unrelated, 1.0 = identical meaning.
    - debug: Enable detailed logging to ~/.jarvis/debug.auto-extraction.log (default False)

    Per-session watermarks (at ~/.jarvis/state/sessions/) replace the old global
    cooldown — each session tracks its own last-processed position independently.

    Config lives at memory.auto_extract in ~/.jarvis/config.json.
    """
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
    """Get markdown chunking configuration with defaults.

    Returns config dict with:
    - enabled: Whether chunking is active (default True)
    - min_chunk_chars: Minimum chunk size before merging (default 200)
    - max_chunk_chars: Maximum chunk size before paragraph splitting (default 1500)
    - heading_levels: Which heading levels to split on (default [2, 3])

    Config lives at memory.chunking in ~/.jarvis/config.json.
    """
    defaults = {
        "enabled": True,
        "min_chunk_chars": 200,
        "max_chunk_chars": 1500,
        "heading_levels": [2, 3],
    }
    return _merge_with_defaults(defaults, _get_memory_section("chunking"))


def get_scoring_config() -> dict:
    """Get importance scoring configuration with defaults.

    Returns config dict with:
    - enabled: Whether scoring is active (default True)
    - recency_half_life_days: Exponential decay half-life (default 7.0)
    - type_weights: Override base weights per vault_type (default {})
    - concept_patterns: Override/extend regex->bonus patterns (default {})

    Config lives at memory.scoring in ~/.jarvis/config.json.
    """
    defaults = {
        "enabled": True,
        "recency_half_life_days": 7.0,
        "type_weights": {},
        "concept_patterns": {},
    }
    return _merge_with_defaults(defaults, _get_memory_section("scoring"))


def get_per_prompt_config() -> dict:
    """Get per-prompt semantic search configuration with defaults.

    Returns config dict with:
    - enabled: Master switch for per-prompt search (default True)
    - threshold: Minimum relevance score for injection (default 0.5)
    - budget: Total character budget for injection (default 8000, split 50/50)

    Config lives at memory.per_prompt_search in ~/.jarvis/config.json.
    """
    defaults = {
        "enabled": True,
        "threshold": 0.5,
        "budget": 8000,
        "debug": False,
        "passive_retrieval_increment": 0.01,
    }
    return _merge_with_defaults(defaults, _get_memory_section("per_prompt_search"))


def get_file_format() -> str:
    """Get configured file format for new file creation.

    Returns 'md' or 'org'. Defaults to 'md' if not set or invalid.
    Config key: file_format (top-level in ~/.jarvis/config.json).
    """
    config = get_config()
    fmt = config.get("file_format", "md")
    return fmt if fmt in ("md", "org") else "md"


def get_expansion_config() -> dict:
    """Get query expansion configuration with defaults.

    Returns config dict with:
    - enabled: Whether expansion is active (default True)
    - max_expansion_terms: Cap on added terms (default 5)
    - synonyms: Override/extend trigger->terms mappings (default {})
    - intent_patterns: Custom intent patterns (default [])

    Config lives at memory.expansion in ~/.jarvis/config.json.
    """
    defaults = {
        "enabled": True,
        "max_expansion_terms": 5,
        "synonyms": {},
        "intent_patterns": [],
    }
    return _merge_with_defaults(defaults, _get_memory_section("expansion"))


def get_reranking_config() -> dict:
    """Get cross-encoder reranking configuration with defaults.

    Returns config dict with:
    - enabled: Whether reranking is active (default True)
    - candidate_count: How many results to over-fetch for reranking (default 100)
    - top_k: Final result count after reranking (default 10)
    - alpha: Blend weight for reranker vs vector scores (default 0.7)
    - max_latency_ms: Latency budget for reranking in milliseconds (default 1000)
    - batch_size: Tokenization batch size for ONNX inference (default 32)

    Config lives at memory.reranking in ~/.jarvis/config.json.
    """
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
    """Get conflict detection configuration with defaults.

    Returns config dict with:
    - enabled: Master switch for conflict detection (default True)
    - use_llm: Whether to verify conflicts with Haiku LLM (default False)
    - similarity_threshold: Minimum embedding similarity to consider (default 0.7)
    - divergence_threshold: Maximum word Jaccard for conflict signal (default 0.4)
    - max_candidates: Maximum candidates to evaluate per write (default 10)

    Config lives at memory.conflict_detection in ~/.jarvis/config.json.
    """
    defaults = {
        "enabled": True,
        "use_llm": False,
        "similarity_threshold": 0.7,
        "divergence_threshold": 0.4,
        "max_candidates": 10,
    }
    return _merge_with_defaults(defaults, _get_memory_section("conflict_detection"))


def get_staleness_config() -> dict:
    """Get observation staleness tracking configuration with defaults.

    Returns config dict with:
    - enabled: Master switch for staleness detection (default True)
    - penalty: Relevance score penalty applied to stale observations (default 0.15)

    Config lives at memory.staleness in ~/.jarvis/config.json.
    """
    defaults = {"enabled": True, "penalty": 0.15}
    return _merge_with_defaults(defaults, _get_memory_section("staleness"))


def get_pattern_detection_config() -> dict:
    """Get pattern detection configuration with defaults.

    Returns config dict with:
    - enabled: Master switch for pattern detection (default True)
    - scan_interval_seconds: Seconds between detection scans (default 300)
    - similarity_threshold: Jaccard threshold to merge observations into a candidate (default 0.3)
    - promotion_threshold: Min observations before promoting a candidate to pattern (default 3)
    - max_candidates: Maximum in-memory candidates before LRU eviction (default 200)
    - candidate_expiry_days: Days before an inactive candidate expires (default 7)
    - lookback_minutes: How far back to scan for new observations each cycle (default 10)
    - merge_threshold: Jaccard threshold to merge new pattern into existing one (default 0.7)

    Config lives at memory.pattern_detection in ~/.jarvis/config.json.
    """
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


def get_telemetry_config() -> dict:
    """Get ChromaDB telemetry configuration with defaults.

    Returns config dict with:
    - enabled: Master switch for telemetry logging (default True)
    - log_reads: Log read operations (get, query, count, peek) (default False)
    - log_writes: Log write operations (upsert, add, delete) (default True)
    - probe_interval_seconds: Seconds between health probe cycles (default 300)

    Env var kill-switch: JARVIS_TELEMETRY=0 disables all telemetry regardless
    of config. Useful in Docker to quickly disable without config changes.

    Config lives at memory.telemetry in ~/.jarvis/config.json.
    """
    if os.environ.get("JARVIS_TELEMETRY", "").strip() == "0":
        return {
            "enabled": False,
            "log_reads": False,
            "log_writes": False,
            "probe_interval_seconds": 300,
        }
    defaults = {
        "enabled": True,
        "log_reads": False,
        "log_writes": True,
        "probe_interval_seconds": 300,
    }
    return _merge_with_defaults(defaults, _get_memory_section("telemetry"))


def get_todoist_prompt_alerts_config() -> dict:
    """Get Todoist per-prompt alert configuration with defaults.

    Returns config dict with:
    - enabled: Master switch for Todoist prompt alerts (default False)
    - sync_interval_seconds: Seconds between API syncs (default 900)
    - max_per_category: Max tasks to show per alert type (default 3)
    - api_timeout_seconds: HTTP timeout for Todoist API calls (default 5)
    - debug: Enable detailed logging (default False)

    Config lives at todoist.prompt_alerts in ~/.jarvis/config.json.
    """
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
    """Get worklog configuration with defaults.

    Returns config dict with:
    - enabled: Whether worklog extraction is active (default True)
    - dedup_threshold: Jaccard word-overlap threshold for session dedup (default 0.7).
        Higher = more similar (1.0 = identical). Lower value catches more duplicates.

    Config lives at memory.worklog in ~/.jarvis/config.json.
    """
    defaults = {
        "enabled": True,
        "dedup_threshold": 0.7,
    }
    return _merge_with_defaults(defaults, _get_memory_section("worklog"))


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
