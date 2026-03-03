"""Namespace ID generation and parsing for the unified jarvis collection.

Shared namespace infrastructure is provided by jarvis_common.namespaces
and re-exported here for backward compatibility.
"""

# Re-export all shared namespace functions — no import changes needed anywhere
from jarvis_common.namespaces import (  # noqa: F401
    # Namespace constants
    NAMESPACE_VAULT,
    NAMESPACE_MEMORY_GLOBAL,
    NAMESPACE_OBS,
    NAMESPACE_PATTERN,
    NAMESPACE_SUMMARY,
    NAMESPACE_CODE,
    NAMESPACE_REL,
    NAMESPACE_HINT,
    NAMESPACE_PLAN,
    NAMESPACE_LEARNING,
    NAMESPACE_DECISION,
    NAMESPACE_WORKLOG,
    # Content type enum
    ContentType,
    ALL_TYPES,
    CONTENT_TYPES,
    TIER2_TYPES,  # Backward compatibility alias — removed in v3.1
    # Valid categories for local.memories.category column
    VALID_CATEGORIES,
    # Schema constants
    SCHEMA_LOCAL,
    SCHEMA_OBSIDIAN,
    # Deprecated aliases
    SCHEMA_CORE,
    SCHEMA_VAULT,
    # Schema routing
    schema_for_id,
    # ID generators
    vault_id,
    global_memory_id,
    project_memory_id,
    memory_namespace,
    observation_id,
    pattern_id,
    summary_id,
    code_id,
    relationship_id,
    hint_id,
    plan_id,
    learning_id,
    decision_id,
    worklog_id,
    # ID parser
    ParsedId,
    parse_id,
    # Internal helpers (used by tests)
    _slugify,
)
