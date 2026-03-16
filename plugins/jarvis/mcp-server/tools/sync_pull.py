"""Pull sync engine — create local mirrors of remote data.

Provides initial (full) and incremental (delta) pull from remote Jarvis
instances into local mirror schemas. Each remote gets its own PostgreSQL
schema (e.g., "remote_work") with a flat table mirroring local.memories.

Key design choices:
- ON CONFLICT (id) DO UPDATE for idempotency
- Keyset pagination (updated_at, id) — no OFFSET, no skipped rows
- psycopg.sql.Identifier for all dynamic schema names (SQL injection safe)
- Echo dedup: rows already in local.memories are skipped (mirror = remote - local)
- Sync timestamp tracked in local.meta (per-remote)
- Background loop wrapped in asyncio.to_thread (non-blocking)
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from psycopg import sql
from psycopg.types.json import Jsonb

from .config import get_sync_config, get_embedding_config
from .schema import _get_pool, get_meta, set_meta, LOCAL_MIRROR_SQL
from .remote_connection import get_remote_pool
from .sync_config import redact_dsn

logger = logging.getLogger("jarvis-core")

# Default batch size for pull operations
DEFAULT_BATCH_SIZE = 500

# Meta key prefix for pull sync timestamps
_META_KEY_PREFIX = "pull_sync_ts"

# Startup delay before first pull (let push worker settle)
_PULL_STARTUP_DELAY = 15  # seconds

# Cache of schemas already ensured this process lifetime
_ensured_local_schemas: set[str] = set()


def _meta_key(remote_name: str) -> str:
    """Build the local.meta key for a remote's last pull timestamp."""
    return f"{_META_KEY_PREFIX}:{remote_name}"


def _get_last_pull_ts(remote_name: str) -> Optional[datetime]:
    """Get the last successful pull timestamp for a remote.

    Returns None if no pull has been done yet.
    """
    data = get_meta(_meta_key(remote_name))
    if data is None:
        return None

    ts_str = data.get("timestamp")
    if not ts_str:
        return None

    try:
        dt = datetime.fromisoformat(ts_str)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _set_last_pull_ts(remote_name: str, ts: datetime) -> None:
    """Record the last successful pull timestamp for a remote.

    Persists to local.meta and mirrors into the registry via copy-on-write
    (D3 fix — never mutates SchemaEntry.metadata in-place).
    """
    set_meta(_meta_key(remote_name), {
        "timestamp": ts.isoformat(),
        "remote": remote_name,
    })
    # D3: Update registry metadata via copy-on-write (best-effort)
    try:
        from .schema_registry import update_remote_metadata
        update_remote_metadata(f"remote_{remote_name}", {"last_pull_ts": ts.isoformat()})
    except Exception:
        pass


def _ensure_local_mirror_schema(schema: str, remote_name: str) -> None:
    """Ensure the local mirror schema + table + indexes exist, then register.

    Runs LOCAL_MIRROR_SQL on the local pool. Cached per schema name
    so DDL only executes once per process lifetime. Idempotent.

    D1 fix: validates schema name before use in DDL f-string.
    D2 fix: stores embedding_model in registry metadata at registration.
    D11 fix: register_remote is idempotent, safe to call multiple times.
    """
    if schema in _ensured_local_schemas:
        return

    # D1: Validate schema name before use in DDL f-string
    from .schema_registry import is_valid_schema_name, register_remote
    if not is_valid_schema_name(schema):
        raise ValueError(f"Invalid mirror schema name: {schema!r}")

    emb = get_embedding_config()
    dims = emb["dimensions"]
    model_name = emb.get("model", "unknown")

    ddl = LOCAL_MIRROR_SQL.format(schema=schema, dimensions=dims)

    pool = _get_pool()
    with pool.connection() as conn:
        conn.execute(ddl)
        conn.commit()

    _ensured_local_schemas.add(schema)
    logger.info("Ensured local mirror schema: %s", schema)

    # D2: Register with embedding_model so _cross_schema_search can detect mismatches
    register_remote(
        name=schema,
        remote_name=remote_name,
        searchable=True,
        writable=False,
        metadata={"embedding_model": model_name},
    )


def _get_local_ids(pool, batch_ids: list[str]) -> set[str]:
    """Check which IDs already exist in local.memories.

    Used for echo dedup — skip rows the user already has locally
    so the mirror only stores data from other machines.
    """
    if not batch_ids:
        return set()

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM local.memories WHERE id = ANY(%s)",
                (batch_ids,),
            )
            return {row[0] for row in cur.fetchall()}


def _get_remote_config(remote_name: str) -> tuple[str, str]:
    """Read remote config and return (remote_name, source_schema).

    Source schema defaults to the remote name if not specified.

    Raises:
        KeyError: If remote is not configured.
    """
    sync_cfg = get_sync_config()
    remotes = sync_cfg.get("remotes", {})

    if remote_name not in remotes:
        raise KeyError(f"Remote not configured: {remote_name!r}")

    remote = remotes[remote_name]
    source_schema = remote.get("schema", remote_name)
    return remote_name, source_schema


# Column names for the remote SELECT (CAS JOIN)
_REMOTE_COLUMNS = [
    "id", "document", "embedding",
    "category", "scope", "project",
    "source", "importance_score", "retrieval_count",
    "status", "superseded_by", "deleted_at",
    "synced_to", "origin", "metadata",
    "created_at", "updated_at",
]

# Column names for the local mirror INSERT (flat table)
_MIRROR_COLUMNS = _REMOTE_COLUMNS


def _build_remote_select(source_schema: str, *, incremental: bool) -> sql.Composed:
    """Build the remote SELECT query using sql.Identifier for schema safety.

    Args:
        source_schema: Remote schema name.
        incremental: If True, add WHERE filter for keyset pagination.
    """
    schema_id = sql.Identifier(source_schema)

    base = sql.SQL(
        "SELECT r.id, c.content AS document, c.embedding, "
        "r.category, r.scope, r.project, "
        "r.source, r.importance_score, r.retrieval_count, "
        "r.status, r.superseded_by, r.deleted_at, "
        "r.synced_to, r.origin, r.metadata, "
        "r.created_at, r.updated_at "
        "FROM {schema}.memory_refs r "
        "JOIN {schema}.content c ON c.hash = r.content_hash"
    ).format(schema=schema_id)

    if incremental:
        where = sql.SQL(
            " WHERE r.status = 'active' AND (r.updated_at, r.id) > (%s, %s) "
            "ORDER BY r.updated_at ASC, r.id ASC LIMIT %s"
        )
    else:
        where = sql.SQL(
            " WHERE r.status = 'active' AND (r.updated_at, r.id) > (%s, %s) "
            "ORDER BY r.updated_at ASC, r.id ASC LIMIT %s"
        )

    return base + where


def _build_mirror_upsert(target_schema: str) -> sql.Composed:
    """Build the mirror INSERT ... ON CONFLICT DO UPDATE using sql.Identifier."""
    schema_id = sql.Identifier(target_schema)

    return sql.SQL(
        "INSERT INTO {schema}.memories "
        "(id, document, embedding, "
        "category, scope, project, "
        "source, importance_score, retrieval_count, "
        "status, superseded_by, deleted_at, "
        "synced_to, origin, metadata, "
        "created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET "
        "document = EXCLUDED.document, "
        "embedding = EXCLUDED.embedding, "
        "category = EXCLUDED.category, "
        "scope = EXCLUDED.scope, "
        "project = EXCLUDED.project, "
        "importance_score = EXCLUDED.importance_score, "
        "retrieval_count = EXCLUDED.retrieval_count, "
        "status = EXCLUDED.status, "
        "superseded_by = EXCLUDED.superseded_by, "
        "deleted_at = EXCLUDED.deleted_at, "
        "synced_to = EXCLUDED.synced_to, "
        "origin = EXCLUDED.origin, "
        "metadata = EXCLUDED.metadata, "
        "updated_at = EXCLUDED.updated_at"
    ).format(schema=schema_id)


def _adapt_row(row: dict) -> tuple:
    """Adapt a row dict into an INSERT parameter tuple.

    Wraps metadata with Jsonb() for psycopg3 adaptation and ensures
    synced_to is a proper list.
    """
    metadata = row.get("metadata", {})
    if isinstance(metadata, dict):
        metadata = Jsonb(metadata)

    synced_to = row.get("synced_to")
    if synced_to is None:
        synced_to = []

    return (
        row["id"], row["document"], row["embedding"],
        row["category"], row["scope"], row["project"],
        row["source"], row["importance_score"], row["retrieval_count"],
        row["status"], row["superseded_by"], row.get("deleted_at"),
        synced_to, row.get("origin", "local"), metadata,
        row["created_at"], row["updated_at"],
    )


def initial_pull(
    remote_name: str,
    target_schema: str,
    *,
    source_schema: str = "local",
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict:
    """Full pull from a remote into a local mirror schema.

    Reads all active memories from the remote's CAS tables
    ({source_schema}.memory_refs JOIN {source_schema}.content) and inserts
    them as flat rows into the target schema. Uses ON CONFLICT DO UPDATE
    for idempotency. Skips rows that already exist in local.memories (echo dedup).

    Uses keyset pagination (updated_at, id) instead of OFFSET.

    Args:
        remote_name: Name of the remote (matches sync config).
        target_schema: Local PostgreSQL schema to write into.
        source_schema: Remote schema to read from.
        batch_size: Number of rows to fetch per round-trip.

    Returns:
        Dict with pulled_count, skipped_count, target_schema, and timing.
    """
    start = time.time()

    # Ensure mirror schema exists before first INSERT
    _ensure_local_mirror_schema(target_schema, remote_name)

    remote_pool = get_remote_pool(remote_name)
    local_pool = _get_pool()

    pulled = 0
    skipped = 0

    # Keyset cursor: (updated_at, id) — start from the beginning
    cursor_ts = datetime.min.replace(tzinfo=timezone.utc)
    cursor_id = ""

    select_sql = _build_remote_select(source_schema, incremental=False)
    upsert_sql = _build_mirror_upsert(target_schema)

    while True:
        # Fetch batch from remote
        with remote_pool.connection() as remote_conn:
            with remote_conn.cursor() as cur:
                cur.execute(select_sql, (cursor_ts, cursor_id, batch_size))
                columns = [desc.name for desc in cur.description]
                rows = cur.fetchall()

        if not rows:
            break

        # Convert to dicts
        batch = [dict(zip(columns, r)) for r in rows]

        # Echo dedup: skip IDs that exist in local.memories
        batch_ids = [row["id"] for row in batch]
        local_ids = _get_local_ids(local_pool, batch_ids)

        # Filter and upsert into mirror
        to_upsert = [row for row in batch if row["id"] not in local_ids]
        skipped += len(batch) - len(to_upsert)

        if to_upsert:
            with local_pool.connection() as local_conn:
                with local_conn.cursor() as cur:
                    for row in to_upsert:
                        cur.execute(upsert_sql, _adapt_row(row))
                local_conn.commit()

        pulled += len(to_upsert)

        # Advance cursor to last row in batch
        last = batch[-1]
        cursor_ts = last["updated_at"]
        cursor_id = last["id"]

        if len(rows) < batch_size:
            break

    # Record sync timestamp
    now = datetime.now(timezone.utc)
    _set_last_pull_ts(remote_name, now)

    elapsed = round(time.time() - start, 2)
    logger.info(
        "Initial pull from %s: %d rows → %s (skipped %d local, %0.2fs)",
        remote_name, pulled, target_schema, skipped, elapsed,
    )

    return {
        "success": True,
        "remote": remote_name,
        "target_schema": target_schema,
        "pulled_count": pulled,
        "skipped_count": skipped,
        "elapsed_seconds": elapsed,
        "mode": "initial",
    }


def incremental_pull(
    remote_name: str,
    target_schema: str,
    *,
    source_schema: str = "local",
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict:
    """Incremental pull — only rows updated since last sync.

    Uses the last_pull_ts stored in local.meta as the initial cursor
    timestamp. Falls back to initial_pull if no timestamp exists.
    Uses keyset pagination (updated_at, id) instead of OFFSET.

    Args:
        remote_name: Name of the remote.
        target_schema: Local PostgreSQL schema to write into.
        source_schema: Remote schema to read from.
        batch_size: Number of rows to fetch per round-trip.

    Returns:
        Dict with pulled_count, skipped_count, target_schema, and timing.
    """
    last_ts = _get_last_pull_ts(remote_name)
    if last_ts is None:
        logger.info("No previous pull timestamp for %s — doing initial pull", remote_name)
        return initial_pull(
            remote_name, target_schema,
            source_schema=source_schema, batch_size=batch_size,
        )

    start = time.time()

    # Ensure mirror schema exists
    _ensure_local_mirror_schema(target_schema, remote_name)

    remote_pool = get_remote_pool(remote_name)
    local_pool = _get_pool()

    pulled = 0
    skipped = 0

    # Keyset cursor: start from last pull timestamp
    cursor_ts = last_ts
    cursor_id = ""

    select_sql = _build_remote_select(source_schema, incremental=True)
    upsert_sql = _build_mirror_upsert(target_schema)

    while True:
        with remote_pool.connection() as remote_conn:
            with remote_conn.cursor() as cur:
                cur.execute(select_sql, (cursor_ts, cursor_id, batch_size))
                columns = [desc.name for desc in cur.description]
                rows = cur.fetchall()

        if not rows:
            break

        batch = [dict(zip(columns, r)) for r in rows]

        # Echo dedup
        batch_ids = [row["id"] for row in batch]
        local_ids = _get_local_ids(local_pool, batch_ids)
        to_upsert = [row for row in batch if row["id"] not in local_ids]
        skipped += len(batch) - len(to_upsert)

        if to_upsert:
            with local_pool.connection() as local_conn:
                with local_conn.cursor() as cur:
                    for row in to_upsert:
                        cur.execute(upsert_sql, _adapt_row(row))
                local_conn.commit()

        pulled += len(to_upsert)

        # Advance cursor
        last = batch[-1]
        cursor_ts = last["updated_at"]
        cursor_id = last["id"]

        if len(rows) < batch_size:
            break

    now = datetime.now(timezone.utc)
    _set_last_pull_ts(remote_name, now)

    elapsed = round(time.time() - start, 2)
    logger.info(
        "Incremental pull from %s: %d rows → %s (since %s, skipped %d local, %0.2fs)",
        remote_name, pulled, target_schema, last_ts.isoformat(), skipped, elapsed,
    )

    return {
        "success": True,
        "remote": remote_name,
        "target_schema": target_schema,
        "pulled_count": pulled,
        "skipped_count": skipped,
        "elapsed_seconds": elapsed,
        "mode": "incremental",
        "since": last_ts.isoformat(),
    }


async def pull_sync_loop(
    remote_name: str,
    target_schema: str,
    *,
    source_schema: str = "local",
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> None:
    """Background async loop for continuous incremental pull.

    Wraps incremental_pull in asyncio.to_thread so blocking PG I/O
    doesn't stall the MCP event loop. Re-reads config each iteration
    for dynamic interval changes.

    Args:
        remote_name: Name of the remote.
        target_schema: Local PostgreSQL schema to write into.
        source_schema: Remote schema to read from.
        batch_size: Rows per batch.
    """
    await asyncio.sleep(_PULL_STARTUP_DELAY)

    logger.info(
        "Starting pull sync loop: %s → %s",
        remote_name, target_schema,
    )

    while True:
        try:
            result = await asyncio.to_thread(
                incremental_pull,
                remote_name, target_schema,
                source_schema=source_schema,
                batch_size=batch_size,
            )
            if result["pulled_count"] > 0:
                logger.info(
                    "Pull sync: %d rows from %s (skipped %d)",
                    result["pulled_count"], remote_name,
                    result.get("skipped_count", 0),
                )
        except Exception as e:
            logger.error("Pull sync error from %s: %s", remote_name, e)

        # Re-read interval from config each cycle
        sync_cfg = get_sync_config()
        interval = sync_cfg.get("pull_interval_seconds", 300)
        await asyncio.sleep(interval)


def get_pull_sync_tasks() -> list:
    """Factory: return one pull_sync_loop coroutine per enabled remote.

    Reads sync config, validates no duplicate target schemas,
    and returns a list of coroutines ready for asyncio.gather().

    Returns:
        List of coroutine objects (one per enabled remote with pull).
    """
    sync_cfg = get_sync_config()
    if not sync_cfg.get("enabled"):
        return []

    remotes = sync_cfg.get("remotes", {})
    if not remotes:
        return []

    tasks = []
    seen_schemas: dict[str, str] = {}  # schema → remote_name

    for remote_name, remote_cfg in remotes.items():
        if not remote_cfg.get("enabled", True):
            continue

        source_schema = remote_cfg.get("schema", remote_name)
        target_schema = f"remote_{remote_name}"

        # Validate no duplicate target schemas (DAR F14)
        if target_schema in seen_schemas:
            logger.error(
                "Duplicate pull target schema '%s' for remotes '%s' and '%s' — "
                "skipping '%s'",
                target_schema, seen_schemas[target_schema], remote_name, remote_name,
            )
            continue
        seen_schemas[target_schema] = remote_name

        # D6: Eagerly ensure + register mirror schema at startup so queries
        # can find remote data immediately (before the pull loop runs).
        try:
            _ensure_local_mirror_schema(target_schema, remote_name)
        except Exception as e:
            logger.error(
                "Failed to ensure mirror schema %s at startup (will retry in loop): %s",
                target_schema, e,
            )

        tasks.append(
            pull_sync_loop(
                remote_name, target_schema,
                source_schema=source_schema,
            )
        )
        logger.info(
            "Registered pull sync: %s (%s → %s)",
            remote_name, source_schema, target_schema,
        )

    return tasks
