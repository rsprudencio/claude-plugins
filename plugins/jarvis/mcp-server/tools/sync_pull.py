"""Pull sync engine — create local mirrors of remote data.

Provides initial (full) and incremental (delta) pull from remote Jarvis
instances into local mirror schemas. Each remote gets its own PostgreSQL
schema (e.g., "remote_work") with the same table structure as local.memories.

Key design choices:
- ON CONFLICT (id) DO UPDATE for idempotency
- Sync timestamp tracked in local.meta (per-remote)
- Batch size tunable for memory/network tradeoff
- Background loop for continuous incremental sync
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from .remote_connection import get_remote_pool
from .schema import _get_pool, get_meta, set_meta

logger = logging.getLogger("jarvis-core")

# Default batch size for pull operations
DEFAULT_BATCH_SIZE = 500

# Meta key prefix for pull sync timestamps
_META_KEY_PREFIX = "pull_sync_ts"


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
    """Record the last successful pull timestamp for a remote."""
    set_meta(_meta_key(remote_name), {
        "timestamp": ts.isoformat(),
        "remote": remote_name,
    })


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
    for idempotency.

    Args:
        remote_name: Name of the remote (matches sync config).
        target_schema: Local PostgreSQL schema to write into.
        source_schema: Remote schema to read from (from remote config).
        batch_size: Number of rows to fetch per round-trip.

    Returns:
        Dict with pulled_count, target_schema, and timing.
    """
    start = time.time()
    remote_pool = get_remote_pool(remote_name)
    local_pool = _get_pool()

    pulled = 0
    offset = 0

    # SQL safe: source_schema is validated by sync_config.validate_sync_config
    remote_select = f"""SELECT r.id, c.content AS document, c.embedding,
                               r.category, r.scope, r.project,
                               r.source, r.importance_score, r.retrieval_count,
                               r.status, r.superseded_by, r.metadata,
                               r.created_at, r.updated_at
                        FROM {source_schema}.memory_refs r
                        JOIN {source_schema}.content c ON c.hash = r.content_hash
                        WHERE r.status = 'active'
                        ORDER BY r.created_at ASC
                        LIMIT %s OFFSET %s"""

    while True:
        # Fetch batch from remote
        with remote_pool.connection() as remote_conn:
            with remote_conn.cursor() as cur:
                cur.execute(remote_select, (batch_size, offset))
                columns = [desc.name for desc in cur.description]
                rows = cur.fetchall()

        if not rows:
            break

        # Upsert into local mirror schema (flat table)
        with local_pool.connection() as local_conn:
            with local_conn.cursor() as cur:
                for row_data in rows:
                    row = dict(zip(columns, row_data))
                    cur.execute(
                        f"""INSERT INTO {target_schema}.memories
                            (id, document, embedding, category, scope, project,
                             source, importance_score, retrieval_count,
                             status, superseded_by, metadata,
                             created_at, updated_at)
                            VALUES (%(id)s, %(document)s, %(embedding)s,
                                    %(category)s, %(scope)s, %(project)s,
                                    %(source)s, %(importance_score)s,
                                    %(retrieval_count)s, %(status)s,
                                    %(superseded_by)s, %(metadata)s,
                                    %(created_at)s, %(updated_at)s)
                            ON CONFLICT (id) DO UPDATE SET
                                document = EXCLUDED.document,
                                embedding = EXCLUDED.embedding,
                                category = EXCLUDED.category,
                                scope = EXCLUDED.scope,
                                project = EXCLUDED.project,
                                importance_score = EXCLUDED.importance_score,
                                retrieval_count = EXCLUDED.retrieval_count,
                                status = EXCLUDED.status,
                                superseded_by = EXCLUDED.superseded_by,
                                metadata = EXCLUDED.metadata,
                                updated_at = EXCLUDED.updated_at""",
                        row,
                    )
                local_conn.commit()

        pulled += len(rows)
        offset += batch_size

        if len(rows) < batch_size:
            break

    # Record sync timestamp
    now = datetime.now(timezone.utc)
    _set_last_pull_ts(remote_name, now)

    elapsed = round(time.time() - start, 2)
    logger.info(
        "Initial pull from %s: %d rows → %s (%0.2fs)",
        remote_name, pulled, target_schema, elapsed,
    )

    return {
        "success": True,
        "remote": remote_name,
        "target_schema": target_schema,
        "pulled_count": pulled,
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

    Uses the last_pull_ts stored in local.meta to filter remote rows.
    Reads from CAS tables ({source_schema}.memory_refs JOIN content).
    Falls back to initial_pull if no timestamp exists.

    Args:
        remote_name: Name of the remote.
        target_schema: Local PostgreSQL schema to write into.
        source_schema: Remote schema to read from (from remote config).
        batch_size: Number of rows to fetch per round-trip.

    Returns:
        Dict with pulled_count, target_schema, and timing.
    """
    last_ts = _get_last_pull_ts(remote_name)
    if last_ts is None:
        logger.info("No previous pull timestamp for %s — doing initial pull", remote_name)
        return initial_pull(
            remote_name, target_schema,
            source_schema=source_schema, batch_size=batch_size,
        )

    start = time.time()
    remote_pool = get_remote_pool(remote_name)
    local_pool = _get_pool()

    pulled = 0
    offset = 0

    # SQL safe: source_schema is validated by sync_config.validate_sync_config
    remote_select = f"""SELECT r.id, c.content AS document, c.embedding,
                               r.category, r.scope, r.project,
                               r.source, r.importance_score, r.retrieval_count,
                               r.status, r.superseded_by, r.metadata,
                               r.created_at, r.updated_at
                        FROM {source_schema}.memory_refs r
                        JOIN {source_schema}.content c ON c.hash = r.content_hash
                        WHERE r.updated_at > %s
                        ORDER BY r.created_at ASC
                        LIMIT %s OFFSET %s"""

    while True:
        with remote_pool.connection() as remote_conn:
            with remote_conn.cursor() as cur:
                cur.execute(remote_select, (last_ts, batch_size, offset))
                columns = [desc.name for desc in cur.description]
                rows = cur.fetchall()

        if not rows:
            break

        with local_pool.connection() as local_conn:
            with local_conn.cursor() as cur:
                for row_data in rows:
                    row = dict(zip(columns, row_data))
                    cur.execute(
                        f"""INSERT INTO {target_schema}.memories
                            (id, document, embedding, category, scope, project,
                             source, importance_score, retrieval_count,
                             status, superseded_by, metadata,
                             created_at, updated_at)
                            VALUES (%(id)s, %(document)s, %(embedding)s,
                                    %(category)s, %(scope)s, %(project)s,
                                    %(source)s, %(importance_score)s,
                                    %(retrieval_count)s, %(status)s,
                                    %(superseded_by)s, %(metadata)s,
                                    %(created_at)s, %(updated_at)s)
                            ON CONFLICT (id) DO UPDATE SET
                                document = EXCLUDED.document,
                                embedding = EXCLUDED.embedding,
                                category = EXCLUDED.category,
                                scope = EXCLUDED.scope,
                                project = EXCLUDED.project,
                                importance_score = EXCLUDED.importance_score,
                                retrieval_count = EXCLUDED.retrieval_count,
                                status = EXCLUDED.status,
                                superseded_by = EXCLUDED.superseded_by,
                                metadata = EXCLUDED.metadata,
                                updated_at = EXCLUDED.updated_at""",
                        row,
                    )
                local_conn.commit()

        pulled += len(rows)
        offset += batch_size

        if len(rows) < batch_size:
            break

    now = datetime.now(timezone.utc)
    _set_last_pull_ts(remote_name, now)

    elapsed = round(time.time() - start, 2)
    logger.info(
        "Incremental pull from %s: %d rows → %s (since %s, %0.2fs)",
        remote_name, pulled, target_schema, last_ts.isoformat(), elapsed,
    )

    return {
        "success": True,
        "remote": remote_name,
        "target_schema": target_schema,
        "pulled_count": pulled,
        "elapsed_seconds": elapsed,
        "mode": "incremental",
        "since": last_ts.isoformat(),
    }


async def pull_sync_loop(
    remote_name: str,
    target_schema: str,
    *,
    interval_seconds: int = 300,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> None:
    """Background async loop for continuous incremental pull.

    Runs incremental_pull at a regular interval. Errors are logged
    but don't stop the loop.

    Args:
        remote_name: Name of the remote.
        target_schema: Local PostgreSQL schema to write into.
        interval_seconds: Seconds between pull cycles (default 5 min).
        batch_size: Rows per batch.
    """
    logger.info(
        "Starting pull sync loop: %s → %s (every %ds)",
        remote_name, target_schema, interval_seconds,
    )

    while True:
        try:
            result = incremental_pull(
                remote_name, target_schema, batch_size=batch_size
            )
            if result["pulled_count"] > 0:
                logger.info(
                    "Pull sync: %d rows from %s",
                    result["pulled_count"], remote_name,
                )
        except Exception as e:
            logger.error("Pull sync error from %s: %s", remote_name, e)

        await asyncio.sleep(interval_seconds)
