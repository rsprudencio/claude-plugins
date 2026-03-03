"""Namespace ID generation and parsing for the unified jarvis collection.

All document IDs follow the pattern:
    <namespace>::<content-specific-id>

This module provides:
- ID generators for each namespace
- ID parser to decompose any ID
- Namespace constants for filtering
- Schema routing based on ID prefix
"""

import re
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

# --- Namespace Constants ---

NAMESPACE_VAULT = "vault::"
NAMESPACE_MEMORY_GLOBAL = "memory::global::"
NAMESPACE_OBS = "obs::"
NAMESPACE_PATTERN = "pattern::"
NAMESPACE_SUMMARY = "summary::"
NAMESPACE_CODE = "code::"
NAMESPACE_REL = "rel::"
NAMESPACE_HINT = "hint::"
NAMESPACE_PLAN = "plan::"
NAMESPACE_LEARNING = "learning::"
NAMESPACE_DECISION = "decision::"
NAMESPACE_WORKLOG = "worklog::"


# Content type enum (for metadata 'type' field and category column)
class ContentType(str, Enum):
    # Vault (indexed file content)
    VAULT = "vault"
    # Memory (strategic, file-backed)
    MEMORY = "memory"
    # Content categories (stored in local.memories)
    OBSERVATION = "observation"
    PATTERN = "pattern"
    LEARNING = "learning"
    DECISION = "decision"
    SUMMARY = "summary"
    CODE = "code"
    RELATIONSHIP = "relationship"
    HINT = "hint"
    PLAN = "plan"
    WORKLOG = "worklog"


ALL_TYPES = [t.value for t in ContentType]

# Content types stored in local.memories (everything except VAULT)
CONTENT_TYPES = [
    t.value for t in ContentType if t != ContentType.VAULT
]

# Backward compatibility alias — will be removed in v3.1
TIER2_TYPES = CONTENT_TYPES

# Valid categories for the local.memories.category column
VALID_CATEGORIES = (
    "observation",
    "pattern",
    "learning",
    "decision",
    "summary",
    "code",
    "relationship",
    "hint",
    "plan",
    "worklog",
    "memory",
)

# --- Schema Constants ---

SCHEMA_LOCAL = "local"
SCHEMA_OBSIDIAN = "obsidian"

# Deprecated aliases — use SCHEMA_LOCAL / SCHEMA_OBSIDIAN
SCHEMA_CORE = SCHEMA_LOCAL
SCHEMA_VAULT = SCHEMA_OBSIDIAN

# Prefixes that route to obsidian.documents
_VAULT_PREFIXES = frozenset({"vault::"})

# Prefixes that route to local.memories
_CORE_PREFIXES = frozenset(
    {
        "memory::",
        "obs::",
        "pattern::",
        "summary::",
        "code::",
        "rel::",
        "hint::",
        "plan::",
        "learning::",
        "decision::",
        "worklog::",
    }
)


# --- Schema Routing ---


def schema_for_id(doc_id: str) -> str:
    """Determine target schema from document ID prefix.

    Returns:
        SCHEMA_OBSIDIAN for vault:: IDs
        SCHEMA_LOCAL for everything else (memory, obs, pattern, etc.)
    """
    if doc_id.startswith("vault::"):
        return SCHEMA_OBSIDIAN
    return SCHEMA_LOCAL


# --- ID Generators ---


def vault_id(relative_path: str, chunk: Optional[int] = None) -> str:
    """Generate a vault document ID."""
    base = f"vault::{relative_path}"
    return f"{base}#chunk-{chunk}" if chunk is not None else base


def global_memory_id(name: str) -> str:
    """Generate a global strategic memory ID."""
    return f"memory::global::{_slugify(name)}"


def project_memory_id(project: str, name: str) -> str:
    """Generate a project-scoped memory ID."""
    return f"memory::{_slugify(project)}::{_slugify(name)}"


def memory_namespace(project: Optional[str] = None) -> str:
    """Return the namespace prefix for memory filtering."""
    if project is None:
        return NAMESPACE_MEMORY_GLOBAL
    return f"memory::{_slugify(project)}::"


def observation_id(timestamp_ms: Optional[int] = None) -> str:
    """Generate an observation ID from epoch milliseconds."""
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    return f"obs::{timestamp_ms}"


def pattern_id(name: str) -> str:
    """Generate a pattern ID from a descriptive name."""
    return f"pattern::{_slugify(name)}"


def summary_id(session_id: Optional[str] = None) -> str:
    """Generate a session summary ID."""
    if session_id is None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        session_id = f"session-{ts}"
    return f"summary::{session_id}"


def code_id(file_path: str, symbol: str = "__module__") -> str:
    """Generate a code chunk ID."""
    return f"code::{file_path}::{symbol}"


def relationship_id(entity_a: str, entity_b: str) -> str:
    """Generate a relationship ID between two entities.

    Entities are sorted alphabetically to ensure consistency regardless of order.
    """
    a, b = sorted([_slugify(entity_a), _slugify(entity_b)])
    return f"rel::{a}::{b}"


def hint_id(topic: str, seq: int = 0) -> str:
    """Generate a hint ID with sequential number for ordering."""
    return f"hint::{_slugify(topic)}::{seq}"


def plan_id(name: str) -> str:
    """Generate a plan ID from a descriptive name."""
    return f"plan::{_slugify(name)}"


def learning_id(timestamp_ms: Optional[int] = None) -> str:
    """Generate a learning ID from epoch milliseconds."""
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    return f"learning::{timestamp_ms}"


def decision_id(name: str) -> str:
    """Generate a decision ID from a descriptive name."""
    return f"decision::{_slugify(name)}"


def worklog_id(timestamp_ms: Optional[int] = None) -> str:
    """Generate a worklog ID from epoch milliseconds."""
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    return f"worklog::{timestamp_ms}"


# --- ID Parser ---


@dataclass
class ParsedId:
    """Decomposed document ID."""

    namespace: str  # "vault", "memory", "obs", "pattern", etc.
    full_prefix: str  # "vault::", "memory::global::", "obs::", etc.
    content_id: str  # The part after the prefix
    schema: str = SCHEMA_LOCAL  # "local" or "obsidian"
    chunk: Optional[int] = None  # For vault chunks only
    project: Optional[str] = None  # For project-scoped memories


def parse_id(doc_id: str) -> ParsedId:
    """Parse a namespaced document ID into its components.

    Handles all known namespace prefixes. Legacy IDs (no prefix)
    are treated as vault documents for backward compatibility.
    """
    schema = schema_for_id(doc_id)

    if doc_id.startswith("vault::"):
        content = doc_id[7:]
        chunk = None
        if "#chunk-" in content:
            content, chunk_str = content.rsplit("#chunk-", 1)
            chunk = int(chunk_str)
        return ParsedId("vault", "vault::", content, schema, chunk)

    if doc_id.startswith("memory::global::"):
        return ParsedId("memory", "memory::global::", doc_id[16:], schema)

    if doc_id.startswith("memory::"):
        parts = doc_id.split("::", 2)
        project = parts[1] if len(parts) > 1 else ""
        name = parts[2] if len(parts) > 2 else ""
        return ParsedId("memory", f"memory::{project}::", name, schema, project=project)

    if doc_id.startswith("obs::"):
        return ParsedId("obs", "obs::", doc_id[5:], schema)

    if doc_id.startswith("pattern::"):
        return ParsedId("pattern", "pattern::", doc_id[9:], schema)

    if doc_id.startswith("summary::"):
        return ParsedId("summary", "summary::", doc_id[9:], schema)

    if doc_id.startswith("code::"):
        return ParsedId("code", "code::", doc_id[6:], schema)

    if doc_id.startswith("rel::"):
        return ParsedId("rel", "rel::", doc_id[5:], schema)

    if doc_id.startswith("hint::"):
        return ParsedId("hint", "hint::", doc_id[6:], schema)

    if doc_id.startswith("plan::"):
        return ParsedId("plan", "plan::", doc_id[6:], schema)

    if doc_id.startswith("learning::"):
        return ParsedId("learning", "learning::", doc_id[10:], schema)

    if doc_id.startswith("decision::"):
        return ParsedId("decision", "decision::", doc_id[10:], schema)

    if doc_id.startswith("worklog::"):
        return ParsedId("worklog", "worklog::", doc_id[9:], schema)

    # Bare path without namespace prefix — default to vault
    return ParsedId("vault", "vault::", doc_id, SCHEMA_OBSIDIAN)


# --- Helpers ---


def _slugify(text: str) -> str:
    """Convert text to a URL-safe slug."""
    slug = text.lower().strip().replace(" ", "-")
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug
