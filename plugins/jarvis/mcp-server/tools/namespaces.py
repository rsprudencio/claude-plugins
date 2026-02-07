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
from typing import Optional

# --- Namespace Constants ---

NAMESPACE_VAULT = "vault::"
NAMESPACE_MEMORY_GLOBAL = "memory::global::"
NAMESPACE_OBS = "obs::"
NAMESPACE_PATTERN = "pattern::"
NAMESPACE_SUMMARY = "summary::"
NAMESPACE_CODE = "code::"

# Content type values (for metadata 'type' field)
TYPE_VAULT = "vault"
TYPE_MEMORY = "memory"
TYPE_OBSERVATION = "observation"
TYPE_PATTERN = "pattern"
TYPE_SUMMARY = "summary"
TYPE_CODE = "code"

ALL_TYPES = [TYPE_VAULT, TYPE_MEMORY, TYPE_OBSERVATION, TYPE_PATTERN, TYPE_SUMMARY, TYPE_CODE]


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


# --- ID Parser ---

@dataclass
class ParsedId:
    """Decomposed document ID."""
    namespace: str       # "vault", "memory", "obs", "pattern", "summary", "code"
    full_prefix: str     # "vault::", "memory::global::", "obs::", etc.
    content_id: str      # The part after the prefix
    chunk: Optional[int] = None  # For vault chunks only


def parse_id(doc_id: str) -> ParsedId:
    """Parse a namespaced document ID into its components.

    Handles all known namespace prefixes. Legacy IDs (no prefix)
    are treated as vault documents for backward compatibility.
    """
    if doc_id.startswith("vault::"):
        content = doc_id[7:]
        chunk = None
        if "#chunk-" in content:
            content, chunk_str = content.rsplit("#chunk-", 1)
            chunk = int(chunk_str)
        return ParsedId("vault", "vault::", content, chunk)

    if doc_id.startswith("memory::global::"):
        return ParsedId("memory", "memory::global::", doc_id[16:])

    if doc_id.startswith("memory::"):
        parts = doc_id.split("::", 2)
        project = parts[1] if len(parts) > 1 else ""
        name = parts[2] if len(parts) > 2 else ""
        return ParsedId("memory", f"memory::{project}::", name)

    if doc_id.startswith("obs::"):
        return ParsedId("obs", "obs::", doc_id[5:])

    if doc_id.startswith("pattern::"):
        return ParsedId("pattern", "pattern::", doc_id[9:])

    if doc_id.startswith("summary::"):
        return ParsedId("summary", "summary::", doc_id[9:])

    if doc_id.startswith("code::"):
        return ParsedId("code", "code::", doc_id[6:])

    # Bare path without namespace prefix — default to vault
    return ParsedId("vault", "vault::", doc_id)


# --- Helpers ---

def _slugify(text: str) -> str:
    """Convert text to a URL-safe slug."""
    slug = text.lower().strip().replace(" ", "-")
    slug = re.sub(r'[^a-z0-9-]', '', slug)
    slug = re.sub(r'-+', '-', slug).strip('-')
    return slug
