"""Dynamic schema registry for multi-schema search and sync.

Tracks which PostgreSQL schemas exist, their kind (local/obsidian/remote),
and capabilities. Enables N-schema search by allowing query layers to
iterate get_searchable_schemas() instead of hardcoding schema names.

The registry is rebuilt at server startup (after ensure_schema) and
updated when remotes are connected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .namespaces import SCHEMA_LOCAL, SCHEMA_OBSIDIAN

logger = logging.getLogger("jarvis-core")


class SchemaKind(str, Enum):
    """Kind of schema in the registry."""
    LOCAL = "local"        # Local memories (observations, patterns, strategic, etc.)
    OBSIDIAN = "obsidian"  # Indexed vault file chunks
    REMOTE = "remote"      # Remote mirror schema (from another Jarvis instance)


@dataclass
class SchemaEntry:
    """A registered schema with its capabilities."""
    name: str                          # PostgreSQL schema name
    kind: SchemaKind                   # Schema kind
    table: str                         # Primary table name (e.g., "memories", "documents")
    searchable: bool = True            # Include in cross-schema search
    writable: bool = True              # Allow writes (False for read-only mirrors)
    remote_name: Optional[str] = None  # Remote config name (for REMOTE kind)
    metadata: dict = field(default_factory=dict)  # Extensible metadata


# Module-level registry state
_registry: list[SchemaEntry] = []


# Valid schema name pattern (alphanumeric + underscore, no dots or special chars)
_VALID_SCHEMA_RE = None


def is_valid_schema_name(name: str) -> bool:
    """Check if a schema name is valid for PostgreSQL.

    Must be alphanumeric + underscore, 1-63 chars, not start with pg_.
    """
    global _VALID_SCHEMA_RE
    if _VALID_SCHEMA_RE is None:
        import re
        _VALID_SCHEMA_RE = re.compile(r'^[a-z][a-z0-9_]{0,62}$')

    if not _VALID_SCHEMA_RE.match(name):
        return False
    if name.startswith("pg_"):
        return False
    return True


def get_searchable_schemas(kind: Optional[SchemaKind] = None) -> list[SchemaEntry]:
    """Get all schemas that should be included in cross-schema search.

    Args:
        kind: Optional filter by schema kind.

    Returns:
        List of searchable SchemaEntry objects, ordered local-first.
    """
    entries = [e for e in _registry if e.searchable]
    if kind is not None:
        entries = [e for e in entries if e.kind == kind]
    return entries


def get_registry() -> list[SchemaEntry]:
    """Get a copy of the full registry."""
    return list(_registry)


def rebuild_registry() -> list[SchemaEntry]:
    """Rebuild the registry from known schemas.

    Called at server startup after ensure_schema(). Registers the two
    built-in schemas (local + obsidian). Remote schemas are added
    separately via register_remote().

    Returns:
        The rebuilt registry.
    """
    global _registry
    _registry = [
        SchemaEntry(
            name=SCHEMA_LOCAL,
            kind=SchemaKind.LOCAL,
            table="memories",
            searchable=True,
            writable=True,
        ),
        SchemaEntry(
            name=SCHEMA_OBSIDIAN,
            kind=SchemaKind.OBSIDIAN,
            table="documents",
            searchable=True,
            writable=True,
        ),
    ]
    logger.info("Schema registry rebuilt: %d schemas", len(_registry))
    return _registry


def register_obsidian() -> SchemaEntry:
    """Register the obsidian schema (lazy creation).

    Idempotent: if already registered, returns existing entry.
    Used when obsidian schema is created on-demand (e.g., first vault index).
    """
    for entry in _registry:
        if entry.kind == SchemaKind.OBSIDIAN:
            return entry

    entry = SchemaEntry(
        name=SCHEMA_OBSIDIAN,
        kind=SchemaKind.OBSIDIAN,
        table="documents",
        searchable=True,
        writable=True,
    )
    _registry.append(entry)
    logger.info("Registered obsidian schema")
    return entry


def register_remote(
    name: str,
    remote_name: str,
    *,
    searchable: bool = True,
    writable: bool = False,
    metadata: Optional[dict] = None,
) -> SchemaEntry:
    """Register a remote mirror schema.

    Args:
        name: PostgreSQL schema name (e.g., "remote_work")
        remote_name: Remote config name for connection lookup
        searchable: Include in cross-schema search (default True)
        writable: Allow writes (default False — mirrors are read-only)
        metadata: Optional metadata dict

    Returns:
        The registered SchemaEntry.

    Raises:
        ValueError: If schema name is invalid or already registered.
    """
    if not is_valid_schema_name(name):
        raise ValueError(f"Invalid schema name: {name!r}")

    for entry in _registry:
        if entry.name == name:
            raise ValueError(f"Schema already registered: {name!r}")

    entry = SchemaEntry(
        name=name,
        kind=SchemaKind.REMOTE,
        table="memories",
        searchable=searchable,
        writable=writable,
        remote_name=remote_name,
        metadata=metadata or {},
    )
    _registry.append(entry)
    logger.info("Registered remote schema: %s (remote=%s)", name, remote_name)
    return entry


def unregister(name: str) -> bool:
    """Remove a schema from the registry.

    Args:
        name: Schema name to remove.

    Returns:
        True if removed, False if not found.
    """
    global _registry
    before = len(_registry)
    _registry = [e for e in _registry if e.name != name]
    removed = len(_registry) < before
    if removed:
        logger.info("Unregistered schema: %s", name)
    return removed
