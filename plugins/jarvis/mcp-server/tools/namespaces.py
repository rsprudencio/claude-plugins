"""Namespace ID generation and parsing for the unified jarvis collection.

All document IDs in the jarvis ChromaDB collection follow the pattern:
    <namespace>::<content-specific-id>

This module provides:
- ID generators for each namespace
- ID parser to decompose any ID
- Namespace constants for filtering
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

# Content type enum (for metadata 'type' field)
# Using (str, Enum) so values work as plain strings in ChromaDB metadata,
# JSON serialization, and == comparisons with raw strings.
class ContentType(str, Enum):
    # Tier 1 (file-backed)
    VAULT = "vault"
    MEMORY = "memory"
    # Tier 2 (ephemeral)
    OBSERVATION = "observation"    # Short captured insight (auto-extract default)
    PATTERN = "pattern"            # Recurring behavior or preference
    LEARNING = "learning"          # Problem/solution pair, technique, debugging case study
    DECISION = "decision"          # Architectural/strategic choice with rationale
    SUMMARY = "summary"            # Time-period or session aggregation
    CODE = "code"                  # Code snippet or technique reference
    RELATIONSHIP = "relationship"  # Entity relationship mapping
    HINT = "hint"                  # Contextual suggestion
    PLAN = "plan"                  # Strategy or task plan


ALL_TYPES = [t.value for t in ContentType]
TIER2_TYPES = [t.value for t in ContentType if t not in (ContentType.VAULT, ContentType.MEMORY)]

# --- Tier Constants ---

TIER_FILE = "file"
TIER_CHROMADB = "chromadb"
TIER_1_PREFIXES = frozenset({"vault::", "memory::"})
TIER_2_PREFIXES = frozenset({"obs::", "pattern::", "summary::", "code::", "rel::", "hint::", "plan::", "learning::", "decision::"})


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


# --- Tier Detection ---

def get_tier(doc_id: str) -> str:
    """Determine document tier from ID prefix (O(1) operation).
    
    Returns:
        TIER_FILE for Tier 1 (vault::, memory::)
        TIER_CHROMADB for Tier 2 (obs::, pattern::, summary::, code::, rel::, hint::, plan::)
        TIER_FILE for bare paths (legacy vault documents)
    """
    # Check Tier 2 prefixes first (most specific)
    for prefix in TIER_2_PREFIXES:
        if doc_id.startswith(prefix):
            return TIER_CHROMADB
    
    # Check Tier 1 prefixes
    for prefix in TIER_1_PREFIXES:
        if doc_id.startswith(prefix):
            return TIER_FILE
    
    # Bare path defaults to Tier 1 (file-backed vault document)
    return TIER_FILE


# --- ID Parser ---

@dataclass
class ParsedId:
    """Decomposed document ID."""
    namespace: str       # "vault", "memory", "obs", "pattern", "summary", "code", "rel", "hint", "plan", "learning", "decision"
    full_prefix: str     # "vault::", "memory::global::", "obs::", etc.
    content_id: str      # The part after the prefix
    tier: str = TIER_FILE  # "file" or "chromadb"
    chunk: Optional[int] = None  # For vault chunks only


def parse_id(doc_id: str) -> ParsedId:
    """Parse a namespaced document ID into its components.

    Handles all known namespace prefixes. Legacy IDs (no prefix)
    are treated as vault documents for backward compatibility.
    """
    tier = get_tier(doc_id)
    
    if doc_id.startswith("vault::"):
        content = doc_id[7:]
        chunk = None
        if "#chunk-" in content:
            content, chunk_str = content.rsplit("#chunk-", 1)
            chunk = int(chunk_str)
        return ParsedId("vault", "vault::", content, tier, chunk)

    if doc_id.startswith("memory::global::"):
        return ParsedId("memory", "memory::global::", doc_id[16:], tier)

    if doc_id.startswith("memory::"):
        parts = doc_id.split("::", 2)
        project = parts[1] if len(parts) > 1 else ""
        name = parts[2] if len(parts) > 2 else ""
        return ParsedId("memory", f"memory::{project}::", name, tier)

    if doc_id.startswith("obs::"):
        return ParsedId("obs", "obs::", doc_id[5:], tier)

    if doc_id.startswith("pattern::"):
        return ParsedId("pattern", "pattern::", doc_id[9:], tier)

    if doc_id.startswith("summary::"):
        return ParsedId("summary", "summary::", doc_id[9:], tier)

    if doc_id.startswith("code::"):
        return ParsedId("code", "code::", doc_id[6:], tier)

    if doc_id.startswith("rel::"):
        return ParsedId("rel", "rel::", doc_id[5:], tier)

    if doc_id.startswith("hint::"):
        return ParsedId("hint", "hint::", doc_id[6:], tier)

    if doc_id.startswith("plan::"):
        return ParsedId("plan", "plan::", doc_id[6:], tier)

    if doc_id.startswith("learning::"):
        return ParsedId("learning", "learning::", doc_id[10:], tier)

    if doc_id.startswith("decision::"):
        return ParsedId("decision", "decision::", doc_id[10:], tier)

    # Bare path without namespace prefix — default to vault
    return ParsedId("vault", "vault::", doc_id, tier)


# --- Helpers ---

def _slugify(text: str) -> str:
    """Convert text to a URL-safe slug."""
    slug = text.lower().strip().replace(" ", "-")
    slug = re.sub(r'[^a-z0-9-]', '', slug)
    slug = re.sub(r'-+', '-', slug).strip('-')
    return slug
