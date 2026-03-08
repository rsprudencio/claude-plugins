"""Sync queue management for the multi-remote routing engine.

Manages the local.sync_queue table: enqueue, claim (SKIP LOCKED),
status FSM transitions, DLQ management, and queue statistics.

All functions that take a `cur` parameter operate within the caller's
transaction (no commit). Functions that take a `pool` manage their
own connection lifecycle.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("jarvis-core")


def enqueue_sync(cur, memory_id: str, destinations: list[str],
                 version: int = 1) -> int:
    """Enqueue sync intents for a memory to multiple destinations.

    Called within the content_write transaction — uses the caller's cursor.
    Uses ON CONFLICT DO NOTHING for idempotency (same memory+dest+version
    is silently ignored).

    Args:
        cur: Database cursor (caller owns the transaction).
        memory_id: The local.memories ID to sync.
        destinations: List of remote destination names.
        version: Version counter for tracking re-syncs (default 1).

    Returns:
        Number of rows actually inserted (excludes conflicts).
    """
    if not destinations:
        return 0

    inserted = 0
    for dest in destinations:
        cur.execute(
            """INSERT INTO local.sync_queue (memory_id, destination, version, next_retry_at)
               VALUES (%s, %s, %s, now())
               ON CONFLICT (memory_id, destination, version) DO NOTHING""",
            (memory_id, dest, version),
        )
        inserted += cur.rowcount
    return inserted


def claim_pending_syncs(pool, batch_size: int = 50) -> list[dict]:
    """Claim a batch of pending sync queue entries for processing.

    Uses SELECT ... FOR UPDATE SKIP LOCKED to allow concurrent workers
    without contention. Transitions status from 'pending' to 'sending'.

    Args:
        pool: Connection pool.
        batch_size: Maximum entries to claim per batch.

    Returns:
        List of dicts with queue entry details.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE local.sync_queue
                   SET status = 'sending', last_attempt = now()
                   WHERE id IN (
                       SELECT id FROM local.sync_queue
                       WHERE status = 'pending'
                         AND (next_retry_at IS NULL OR next_retry_at <= now())
                       ORDER BY next_retry_at
                       FOR UPDATE SKIP LOCKED
                       LIMIT %s
                   )
                   RETURNING id, memory_id, destination, version, attempts""",
                (batch_size,),
            )
            rows = cur.fetchall()
            conn.commit()

    return [
        {
            "id": row[0],
            "memory_id": row[1],
            "destination": row[2],
            "version": row[3],
            "attempts": row[4],
        }
        for row in rows
    ]


def mark_synced(pool, queue_ids: list[int]) -> int:
    """Mark queue entries as successfully synced.

    Args:
        pool: Connection pool.
        queue_ids: List of sync_queue IDs to mark done.

    Returns:
        Number of rows updated.
    """
    if not queue_ids:
        return 0

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE local.sync_queue
                   SET status = 'done'
                   WHERE id = ANY(%s) AND status = 'sending'""",
                (queue_ids,),
            )
            count = cur.rowcount
            conn.commit()
    return count


def mark_failed(pool, queue_ids: list[int], error: str) -> int:
    """Mark queue entries as failed with exponential backoff.

    Entries exceeding max_attempts are moved to DLQ.
    Retry delay: 30s * 2^attempts, capped at 5 minutes.

    Args:
        pool: Connection pool.
        queue_ids: List of sync_queue IDs that failed.
        error: Error message to record.

    Returns:
        Number of rows updated.
    """
    if not queue_ids:
        return 0

    with pool.connection() as conn:
        with conn.cursor() as cur:
            # Update attempts, set backoff, or move to DLQ
            cur.execute(
                """UPDATE local.sync_queue
                   SET attempts = attempts + 1,
                       error = %s,
                       status = CASE
                           WHEN attempts + 1 >= max_attempts THEN 'dlq'
                           ELSE 'pending'
                       END,
                       next_retry_at = CASE
                           WHEN attempts + 1 >= max_attempts THEN NULL
                           ELSE now() + LEAST(
                               make_interval(secs => 30 * power(2, attempts)),
                               interval '5 minutes'
                           )
                       END
                   WHERE id = ANY(%s) AND status = 'sending'""",
                (error, queue_ids),
            )
            count = cur.rowcount
            conn.commit()
    return count


def retry_dlq(pool, destination: str | None = None) -> int:
    """Reset DLQ entries to pending for retry.

    Args:
        pool: Connection pool.
        destination: Optional filter — only retry DLQ for this remote.

    Returns:
        Number of entries reset.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            if destination:
                cur.execute(
                    """UPDATE local.sync_queue
                       SET status = 'pending', attempts = 0,
                           next_retry_at = now(), error = NULL
                       WHERE status = 'dlq' AND destination = %s""",
                    (destination,),
                )
            else:
                cur.execute(
                    """UPDATE local.sync_queue
                       SET status = 'pending', attempts = 0,
                           next_retry_at = now(), error = NULL
                       WHERE status = 'dlq'"""
                )
            count = cur.rowcount
            conn.commit()
    return count


def get_queue_stats(pool) -> dict:
    """Get per-destination queue statistics.

    Returns:
        Dict mapping destination to status counts, plus a 'total' key.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT destination, status, count(*) AS cnt
                   FROM local.sync_queue
                   GROUP BY destination, status
                   ORDER BY destination, status"""
            )
            rows = cur.fetchall()

    stats: dict = {}
    total_by_status: dict[str, int] = {}

    for dest, status, cnt in rows:
        if dest not in stats:
            stats[dest] = {}
        stats[dest][status] = cnt
        total_by_status[status] = total_by_status.get(status, 0) + cnt

    stats["_total"] = total_by_status
    return stats


def update_synced_to(pool, memory_id: str, destination: str) -> None:
    """Add a destination to the memory's synced_to array.

    Uses array_append with a check to avoid duplicates.

    Args:
        pool: Connection pool.
        memory_id: The local.memories ID.
        destination: Remote name to add to synced_to.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE local.memories
                   SET synced_to = array_append(synced_to, %s),
                       updated_at = now()
                   WHERE id = %s AND NOT (%s = ANY(synced_to))""",
                (destination, memory_id, destination),
            )
            conn.commit()
