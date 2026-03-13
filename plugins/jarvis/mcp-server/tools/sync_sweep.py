"""Retroactive sync sweep for orphaned memories.

Scans local.memories for rows that should be synced based on current
routing rules but were written before sync was enabled or before
matching rules were configured.

Uses keyset pagination (created_at, id) — same pattern as sync_pull.py.
Batch transactions avoid holding locks across the full scan.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from .config import get_sync_config
from .routing import evaluate_routing
from .schema import _get_pool
from .sync_config import load_routing_rules
from .sync_queue import enqueue_sync

logger = logging.getLogger("jarvis-core")

# Default batch size — balances memory vs transaction duration
DEFAULT_BATCH_SIZE = 100


def sync_sweep(*, dry_run: bool = False, batch_size: int = DEFAULT_BATCH_SIZE) -> dict:
    """Scan local memories and enqueue missing sync destinations.

    Evaluates current routing rules against all active, locally-originated
    memories. For each memory, computes which destinations it should be
    synced to and enqueues any that are missing from its synced_to array.

    Args:
        dry_run: If True, compute what would be enqueued but don't write.
        batch_size: Number of memories to process per transaction batch.

    Returns:
        Summary dict with scanned/enqueued counts, per-destination breakdown,
        failure counts, and timing.
    """
    start = time.time()

    # Fail-fast: sync must be enabled
    sync_cfg = get_sync_config()
    if not sync_cfg.get("enabled"):
        return {"success": False, "error": "Sync is not enabled"}

    # Fail-fast: rules must exist
    rules = load_routing_rules(sync_cfg)
    if not rules:
        return {"success": False, "error": "No routing rules configured"}

    strategy = sync_cfg.get("strategy", "first-match")
    project_groups = sync_cfg.get("project_groups", {})

    pool = _get_pool()

    # Capture scan ceiling — concurrent writes during sweep are excluded
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT max(created_at) FROM local.memories "
                "WHERE status = 'active' AND origin = 'local'"
            )
            row = cur.fetchone()
            ceiling = row[0] if row and row[0] else None

    if ceiling is None:
        return {
            "success": True,
            "scanned": 0,
            "needing_sync": 0,
            "enqueued": 0,
            "by_destination": {},
            "failed_count": 0,
            "sample_failures": [],
            "dry_run": dry_run,
            "elapsed_seconds": round(time.time() - start, 2),
        }

    # Keyset pagination state
    cursor_ts = datetime.min.replace(tzinfo=timezone.utc)
    cursor_id = ""

    scanned = 0
    needing_sync = 0
    enqueued = 0
    by_destination: dict[str, int] = {}
    failed_count = 0
    sample_failures: list[dict] = []

    while True:
        # Fetch a batch
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, document, category, scope, project,
                              importance_score, metadata, synced_to, created_at
                       FROM local.memories
                       WHERE status = 'active'
                         AND origin = 'local'
                         AND created_at <= %s
                         AND (created_at, id) > (%s, %s)
                       ORDER BY created_at ASC, id ASC
                       LIMIT %s""",
                    (ceiling, cursor_ts, cursor_id, batch_size),
                )
                columns = [desc.name for desc in cur.description]
                rows = cur.fetchall()

        if not rows:
            break

        batch = [dict(zip(columns, r)) for r in rows]
        # Track enqueue candidates for this batch
        batch_enqueue: list[tuple[str, list[str]]] = []

        for mem in batch:
            scanned += 1
            try:
                # Build routing input
                raw_meta = mem.get("metadata")
                routing_metadata = dict(raw_meta) if raw_meta else {}
                memory_dict = {
                    "category": mem["category"],
                    "scope": mem["scope"],
                    "project": mem["project"],
                    "importance_score": mem["importance_score"],
                    "metadata": routing_metadata,
                }

                decision = evaluate_routing(
                    memory_dict, rules, strategy, project_groups,
                )

                if not decision.destinations:
                    continue

                # Compute missing = destinations - already synced
                synced = set(mem.get("synced_to") or [])
                missing = [d for d in decision.destinations if d not in synced]

                if missing:
                    needing_sync += 1
                    batch_enqueue.append((mem["id"], missing))
                    for dest in missing:
                        by_destination[dest] = by_destination.get(dest, 0) + 1

            except Exception as e:
                failed_count += 1
                if len(sample_failures) < 5:
                    sample_failures.append({
                        "id": mem.get("id", "unknown"),
                        "error": str(e),
                    })

        # Enqueue the batch (one transaction)
        if batch_enqueue and not dry_run:
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    for mem_id, destinations in batch_enqueue:
                        try:
                            enqueued += enqueue_sync(cur, mem_id, destinations)
                        except Exception as e:
                            failed_count += 1
                            if len(sample_failures) < 5:
                                sample_failures.append({
                                    "id": mem_id,
                                    "error": f"enqueue: {e}",
                                })
                conn.commit()

        # Advance cursor to last row in batch
        last = batch[-1]
        cursor_ts = last["created_at"]
        cursor_id = last["id"]

        if len(rows) < batch_size:
            break

    elapsed = round(time.time() - start, 2)
    logger.info(
        "Sync sweep: scanned=%d needing_sync=%d enqueued=%d failed=%d "
        "dry_run=%s elapsed=%.2fs",
        scanned, needing_sync, enqueued, failed_count, dry_run, elapsed,
    )

    return {
        "success": True,
        "scanned": scanned,
        "needing_sync": needing_sync,
        "enqueued": enqueued,
        "by_destination": by_destination,
        "failed_count": failed_count,
        "sample_failures": sample_failures,
        "dry_run": dry_run,
        "elapsed_seconds": elapsed,
    }
