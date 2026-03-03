"""Background sync worker for multi-remote memory routing.

Follows the same asyncio pattern as todoist_sync.py: blocking PG I/O
is offloaded via asyncio.to_thread(), the event loop stays responsive
for MCP request handling.

The worker:
1. Claims pending entries from local.sync_queue (SKIP LOCKED)
2. Groups them by destination
3. Batch-upserts memories to each remote's local.memories table
4. Marks entries done or failed with exponential backoff
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

from .config import get_sync_config, get_embedding_config
from .schema import _get_pool, REMOTE_SCHEMA_SQL
from .sync_queue import (
    claim_pending_syncs,
    mark_synced,
    mark_failed,
    update_synced_to,
)
from .sync_config import resolve_env_vars, redact_dsn

logger = logging.getLogger("jarvis-core")

_STARTUP_DELAY = 10  # seconds — wait for server to settle

# Cache of (url, schema) pairs already ensured — skip DDL after first success
_ensured_schemas: set[tuple[str, str]] = set()


def _compute_content_hash(document: str, model_name: str) -> str:
    """SHA-256(document + NUL + model_name).

    NUL separator prevents prefix collisions (e.g. "abc" + "def" vs "ab" + "cdef").
    Including model_name ensures embedding model upgrades produce new content rows.
    """
    return hashlib.sha256(
        document.encode("utf-8") + b"\x00" + model_name.encode("utf-8")
    ).hexdigest()


def _sync_iteration() -> dict:
    """Run a single sync iteration: claim → group → upsert → mark.

    Returns:
        Summary dict with counts.
    """
    sync_cfg = get_sync_config()
    if not sync_cfg.get("enabled"):
        return {"skipped": True, "reason": "disabled"}

    remotes = sync_cfg.get("remotes", {})
    if not remotes:
        return {"skipped": True, "reason": "no_remotes"}

    pool = _get_pool()
    entries = claim_pending_syncs(pool, batch_size=50)
    if not entries:
        return {"claimed": 0}

    # Group by destination
    by_dest: dict[str, list[dict]] = {}
    for entry in entries:
        dest = entry["destination"]
        by_dest.setdefault(dest, []).append(entry)

    results = {"claimed": len(entries), "synced": 0, "failed": 0}

    for dest, dest_entries in by_dest.items():
        remote_cfg = remotes.get(dest)
        if not remote_cfg:
            # Destination no longer in config — mark failed
            ids = [e["id"] for e in dest_entries]
            mark_failed(pool, ids, f"Remote '{dest}' not found in config")
            results["failed"] += len(ids)
            continue

        try:
            raw_url = remote_cfg.get("url", "")
            resolved_url = resolve_env_vars(raw_url)
            schema = remote_cfg.get("schema", dest)

            # Ensure target schema exists on remote (cached after first success)
            _ensure_remote_schema(resolved_url, schema)

            # Fetch memory documents for this batch
            memory_ids = [e["memory_id"] for e in dest_entries]
            memories = _fetch_memories(pool, memory_ids)

            # Upsert to remote
            _batch_upsert_to_remote(resolved_url, memories, schema=schema)

            # Mark done and update synced_to
            ids = [e["id"] for e in dest_entries]
            mark_synced(pool, ids)
            for mid in memory_ids:
                update_synced_to(pool, mid, dest)
            results["synced"] += len(ids)

        except Exception as e:
            ids = [e_["id"] for e_ in dest_entries]
            mark_failed(pool, ids, str(e))
            results["failed"] += len(ids)
            logger.warning(
                "Sync to '%s' failed: %s",
                dest,
                str(e)[:200],
            )

    return results


def _fetch_memories(pool, memory_ids: list[str]) -> list[dict]:
    """Fetch full memory records for a list of IDs.

    Returns dicts with all column values needed for remote upsert.
    """
    if not memory_ids:
        return []

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, document, embedding, category, scope, project,
                          source, importance_score, retrieval_count, status,
                          superseded_by, deleted_at, metadata, synced_to,
                          origin, created_at, updated_at
                   FROM local.memories
                   WHERE id = ANY(%s)""",
                (memory_ids,),
            )
            columns = [desc.name for desc in cur.description]
            rows = cur.fetchall()

    return [dict(zip(columns, row)) for row in rows]


def _ensure_remote_schema(remote_url: str, schema: str) -> None:
    """Ensure the target schema + memories table exist on the remote.

    Idempotent DDL — safe to call multiple times. Results are cached
    per (url, schema) pair so DDL only runs once per process lifetime.

    Args:
        remote_url: Resolved PostgreSQL connection URL.
        schema: Target schema name (already validated as safe PG identifier).
    """
    cache_key = (remote_url, schema)
    if cache_key in _ensured_schemas:
        return

    import psycopg

    dims = get_embedding_config()["dimensions"]
    ddl = REMOTE_SCHEMA_SQL.format(schema=schema, dimensions=dims)

    with psycopg.connect(remote_url, autocommit=True) as conn:
        conn.execute(ddl)

    _ensured_schemas.add(cache_key)
    logger.info("Ensured remote schema '%s' on %s", schema, redact_dsn(remote_url))


def _batch_upsert_to_remote(
    remote_url: str, memories: list[dict], schema: str = "local"
) -> None:
    """Upsert memory records to a remote PostgreSQL instance using CAS.

    Splits each memory into an immutable content row (keyed by SHA-256 hash)
    and a mutable metadata ref row. Content is deduplicated within the batch,
    and both are written in a single transaction (content before refs to
    satisfy the FK constraint).

    Args:
        remote_url: Resolved PostgreSQL connection URL.
        memories: List of memory dicts from _fetch_memories.
        schema: Target schema name on the remote (validated PG identifier).
    """
    if not memories:
        return

    import psycopg
    from psycopg.types.json import Jsonb
    from pgvector.psycopg import register_vector

    from .embedding import get_embedding_service

    model_name = get_embedding_service().model_name

    # SQL safe: schema is validated by sync_config.validate_sync_config
    content_sql = f"""INSERT INTO {schema}.content
                        (hash, content, embedding, embedding_model)
                      VALUES (%s, %s, %s, %s)
                      ON CONFLICT (hash) DO NOTHING"""

    ref_sql = f"""INSERT INTO {schema}.memory_refs
                    (id, content_hash, category, scope, project,
                     source, importance_score, retrieval_count, status,
                     superseded_by, deleted_at, synced_to, origin,
                     metadata, created_at, updated_at)
                  VALUES (%s, %s, %s, %s, %s,
                          %s, %s, %s, %s,
                          %s, %s, %s, %s,
                          %s, %s, %s)
                  ON CONFLICT (id) DO UPDATE SET
                      content_hash = EXCLUDED.content_hash,
                      category = EXCLUDED.category,
                      scope = EXCLUDED.scope,
                      project = EXCLUDED.project,
                      source = EXCLUDED.source,
                      importance_score = EXCLUDED.importance_score,
                      retrieval_count = EXCLUDED.retrieval_count,
                      status = EXCLUDED.status,
                      superseded_by = EXCLUDED.superseded_by,
                      deleted_at = EXCLUDED.deleted_at,
                      origin = EXCLUDED.origin,
                      metadata = EXCLUDED.metadata,
                      updated_at = EXCLUDED.updated_at"""

    # Build content + ref tuples, dedup content within batch
    content_rows: dict[str, tuple] = {}  # hash → (hash, doc, embedding, model)
    ref_rows: list[tuple] = []

    for mem in memories:
        content_hash = _compute_content_hash(mem["document"], model_name)

        # Deduplicate content within batch
        if content_hash not in content_rows:
            content_rows[content_hash] = (
                content_hash,
                mem["document"],
                mem["embedding"],
                model_name,
            )

        metadata = mem["metadata"]
        if isinstance(metadata, dict):
            metadata = Jsonb(metadata)
        synced_to = mem["synced_to"]
        if isinstance(synced_to, list):
            synced_to = synced_to or []

        ref_rows.append((
            mem["id"],
            content_hash,
            mem["category"],
            mem["scope"],
            mem["project"],
            mem["source"],
            mem["importance_score"],
            mem["retrieval_count"],
            mem["status"],
            mem["superseded_by"],
            mem["deleted_at"],
            synced_to,
            mem["origin"],
            metadata,
            mem["created_at"],
            mem["updated_at"],
        ))

    with psycopg.connect(remote_url, autocommit=False) as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            # Content first (FK target must exist before refs)
            for content_tuple in content_rows.values():
                cur.execute(content_sql, content_tuple)
            # Then refs
            for ref_tuple in ref_rows:
                cur.execute(ref_sql, ref_tuple)
        conn.commit()


async def sync_worker_loop():
    """Background loop that processes the sync queue.

    Runs alongside the MCP server via asyncio.gather(). Each iteration
    is offloaded to a thread since PG I/O is blocking.
    """
    await asyncio.sleep(_STARTUP_DELAY)

    config = get_sync_config()
    if not config.get("enabled"):
        logger.debug("Sync worker disabled, exiting")
        return

    interval = config.get("worker_interval_seconds", 30)
    logger.info("Sync worker started (interval=%ds)", interval)

    while True:
        try:
            config = get_sync_config()
            if not config.get("enabled"):
                logger.debug("Sync worker disabled mid-run, stopping")
                return

            result = await asyncio.to_thread(_sync_iteration)
            if result.get("skipped"):
                logger.debug("Sync iteration skipped: %s", result.get("reason"))
            elif result.get("claimed", 0) > 0:
                logger.info(
                    "Sync iteration: claimed=%d synced=%d failed=%d",
                    result.get("claimed", 0),
                    result.get("synced", 0),
                    result.get("failed", 0),
                )
            else:
                logger.debug("Sync iteration: queue empty")

        except Exception:
            logger.exception("Error in sync worker loop (will retry)")

        config = get_sync_config()
        interval = config.get("worker_interval_seconds", 30)
        await asyncio.sleep(interval)
