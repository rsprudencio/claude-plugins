"""Dynamic schema registry for multi-schema search and sync.

Tracks which PostgreSQL schemas exist, their kind (local/obsidian/remote),
and capabilities. Enables N-schema search by allowing query layers to
iterate get_searchable_schemas() instead of hardcoding schema names.

The registry is rebuilt at server startup (after ensure_schema) and
updated when remotes are connected.

Thread safety: copy-on-write mutations protected by _lock.
Readers snapshot the list reference (safe due to GIL + atomic assignment).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .namespaces import SCHEMA_LOCAL, SCHEMA_OBSIDIAN

logger = logging.getLogger("jarvis-core")

# D3: Lock for copy-on-write mutations. Readers snapshot the list reference
# without locking (safe due to GIL + atomic reference assignment).
_lock = threading.Lock()


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


# Valid PG identifier pattern (alphanumeric + underscore, no dots or special chars)
_VALID_PG_IDENT_RE = None


def is_valid_pg_identifier(name: str) -> bool:
    """Check if a name is a valid PostgreSQL identifier.

    Must be alphanumeric + underscore, 1-63 chars, not start with pg_.
    Used for both schema names and table names.
    """
    global _VALID_PG_IDENT_RE
    if _VALID_PG_IDENT_RE is None:
        import re
        _VALID_PG_IDENT_RE = re.compile(r'^[a-z][a-z0-9_]{0,62}$')

    if not _VALID_PG_IDENT_RE.match(name):
        return False
    if name.startswith("pg_"):
        return False
    return True


# Backward-compatible alias
def is_valid_schema_name(name: str) -> bool:
    """Check if a schema name is valid for PostgreSQL.

    Alias for is_valid_pg_identifier — kept for backward compatibility.
    """
    return is_valid_pg_identifier(name)


def get_searchable_schemas(kind: Optional[SchemaKind] = None) -> list[SchemaEntry]:
    """Get all schemas that should be included in cross-schema search.

    Readers snapshot the list reference — safe without locking (GIL + atomic assignment).

    Args:
        kind: Optional filter by schema kind.

    Returns:
        List of searchable SchemaEntry objects, ordered local-first.
    """
    registry = _registry  # snapshot
    entries = [e for e in registry if e.searchable]
    if kind is not None:
        entries = [e for e in entries if e.kind == kind]
    return entries


def get_registry() -> list[SchemaEntry]:
    """Get a copy of the full registry."""
    return list(_registry)


def _core_like_schemas() -> frozenset[str]:
    """Return schema names that are core-like (LOCAL or REMOTE).

    Core-like schemas share the local.memories column structure:
    category, importance_score, retrieval_count, created_at.
    They support blended decay scoring and full-content display.
    """
    registry = _registry  # snapshot
    return frozenset(e.name for e in registry if e.kind in (SchemaKind.LOCAL, SchemaKind.REMOTE))


def _discover_remote_schemas() -> list[str]:
    """Discover existing remote mirror schemas from PG information_schema.

    Returns schema names matching the 'remote_*' pattern.
    Gracefully returns [] if the DB is unavailable.
    """
    try:
        from .schema import execute_query
        rows = execute_query(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name LIKE 'remote_%' ORDER BY schema_name",
            fetch="all",
        )
        return [row["schema_name"] for row in rows] if rows else []
    except Exception as e:
        logger.warning("Could not discover remote schemas: %s", e)
        return []


def _get_enabled_remote_names() -> set[str]:
    """Return the set of enabled remote names from sync config.

    Returns empty set if sync is disabled or config is unavailable.
    An empty set is treated as "can't determine — register all discovered".
    """
    try:
        from .config import get_sync_config
        cfg = get_sync_config()
        if not cfg.get("enabled"):
            return set()
        return {
            name for name, rc in cfg.get("remotes", {}).items()
            if rc.get("enabled", True)
        }
    except Exception:
        return set()


def _get_local_embedding_model() -> Optional[str]:
    """Get the active embedding model name from local.meta."""
    try:
        from .schema import get_meta
        meta = get_meta("embedding_config")
        return meta.get("model") if meta else None
    except Exception:
        return None


def rebuild_registry() -> list[SchemaEntry]:
    """Rebuild the registry from known schemas, including auto-discovery of remotes.

    Called at server startup after ensure_schema(). Always registers the two
    built-in schemas (local + obsidian). Additionally auto-discovers existing
    remote_* schemas from PostgreSQL information_schema so remote data is
    immediately available after restart (D6 fix — decoupled from sync lifecycle).

    D10 fix: only registers remotes still enabled in sync config (stale entries pruned).

    Returns:
        The rebuilt registry.
    """
    global _registry

    new_registry: list[SchemaEntry] = [
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

    # D6: Auto-discover existing remote mirror schemas
    embedding_model = _get_local_embedding_model()
    enabled_remotes = _get_enabled_remote_names()
    discovered = _discover_remote_schemas()

    for schema_name in discovered:
        if not is_valid_schema_name(schema_name):
            logger.warning("Skipping discovered schema with invalid name: %r", schema_name)
            continue

        remote_name = schema_name.removeprefix("remote_")

        # D10: Skip remotes no longer enabled in config (stale cleanup)
        # enabled_remotes empty means "can't determine", so register all discovered.
        if enabled_remotes and remote_name not in enabled_remotes:
            logger.info("Skipping stale remote schema (disabled in config): %s", schema_name)
            continue

        metadata: dict = {}
        if embedding_model:
            metadata["embedding_model"] = embedding_model

        new_registry.append(SchemaEntry(
            name=schema_name,
            kind=SchemaKind.REMOTE,
            table="memories",
            searchable=True,
            writable=False,
            remote_name=remote_name,
            metadata=metadata,
        ))
        logger.info("Auto-discovered remote schema: %s", schema_name)

    with _lock:
        _registry = new_registry

    logger.info("Schema registry rebuilt: %d schemas", len(new_registry))
    return new_registry


def register_obsidian() -> SchemaEntry:
    """Register the obsidian schema (lazy creation).

    Idempotent: if already registered, returns existing entry.
    Used when obsidian schema is created on-demand (e.g., first vault index).
    """
    global _registry
    # Fast path: check without lock
    for entry in _registry:
        if entry.kind == SchemaKind.OBSIDIAN:
            return entry

    with _lock:
        # Re-check inside lock (TOCTOU guard)
        for entry in _registry:
            if entry.kind == SchemaKind.OBSIDIAN:
                return entry

        new_entry = SchemaEntry(
            name=SCHEMA_OBSIDIAN,
            kind=SchemaKind.OBSIDIAN,
            table="documents",
            searchable=True,
            writable=True,
        )
        _registry = [*_registry, new_entry]  # copy-on-write

    logger.info("Registered obsidian schema")
    return new_entry


def register_remote(
    name: str,
    remote_name: str,
    *,
    searchable: bool = True,
    writable: bool = False,
    metadata: Optional[dict] = None,
) -> SchemaEntry:
    """Register a remote mirror schema.

    D11 fix: Idempotent — returns existing entry on duplicate instead of raising.
    D3 fix: Copy-on-write under lock.

    Args:
        name: PostgreSQL schema name (e.g., "remote_work")
        remote_name: Remote config name for connection lookup
        searchable: Include in cross-schema search (default True)
        writable: Allow writes (default False — mirrors are read-only)
        metadata: Optional metadata dict

    Returns:
        The registered SchemaEntry (existing or newly created).

    Raises:
        ValueError: If schema name is invalid.
    """
    global _registry
    if not is_valid_schema_name(name):
        raise ValueError(f"Invalid schema name: {name!r}")

    with _lock:
        # D11: Idempotent — return existing entry on duplicate
        for entry in _registry:
            if entry.name == name:
                return entry

        new_entry = SchemaEntry(
            name=name,
            kind=SchemaKind.REMOTE,
            table="memories",
            searchable=searchable,
            writable=writable,
            remote_name=remote_name,
            metadata=metadata or {},
        )
        _registry = [*_registry, new_entry]  # copy-on-write

    logger.info("Registered remote schema: %s (remote=%s)", name, remote_name)
    return new_entry


def update_remote_metadata(name: str, metadata_updates: dict) -> bool:
    """Update metadata for a registered schema without mutating in-place (D3 fix).

    Builds a new registry list with the updated entry, then atomically assigns.
    Safe to call from sync threads while readers iterate the old snapshot.

    Args:
        name: Schema name to update.
        metadata_updates: Key-value pairs to merge into the schema's metadata.

    Returns:
        True if the schema was found and updated, False if not found.
    """
    global _registry
    with _lock:
        new_registry = []
        found = False
        for entry in _registry:
            if entry.name == name:
                new_meta = {**entry.metadata, **metadata_updates}
                new_registry.append(SchemaEntry(
                    name=entry.name,
                    kind=entry.kind,
                    table=entry.table,
                    searchable=entry.searchable,
                    writable=entry.writable,
                    remote_name=entry.remote_name,
                    metadata=new_meta,
                ))
                found = True
            else:
                new_registry.append(entry)
        if found:
            _registry = new_registry  # atomic assignment
    return found


def unregister(name: str) -> bool:
    """Remove a schema from the registry.

    Args:
        name: Schema name to remove.

    Returns:
        True if removed, False if not found.
    """
    global _registry
    with _lock:
        new_registry = [e for e in _registry if e.name != name]
        removed = len(new_registry) < len(_registry)
        if removed:
            _registry = new_registry  # atomic assignment

    if removed:
        logger.info("Unregistered schema: %s", name)
    return removed
