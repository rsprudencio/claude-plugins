#!/usr/bin/env python3
"""Upgrade embedding model from 384d vector to 768d halfvec.

Zero-downtime migration for existing Jarvis v3 installations:
1. Pre-flight: verify current schema, report row count
2. Add new halfvec(768) column (instant DDL)
3. Re-embed documents in resumable batches
4. Verify completeness (abort if NULLs remain)
5. Create HNSW index CONCURRENTLY (non-blocking)
6. Atomic column swap (rename old→embedding_old, new→embedding)
7. Update jarvis_meta (model, dimensions, vector_type, schema_version)
8. Optional --cleanup: drop embedding_old column

Usage:
    python bin/upgrade_embedding_model.py --pg-url URL [options]
    jarvis-upgrade-embedding --pg-url URL [options]
"""

import argparse
import logging
import os
import sys
import time

# Allow imports from the parent mcp-server directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logger = logging.getLogger("jarvis-upgrade-embedding")

DEFAULT_MODEL = "ibm-granite/granite-embedding-english-r2"
DEFAULT_DIMENSIONS = 768
DEFAULT_BATCH_SIZE = 100


def get_connection(pg_url: str):
    """Create a raw psycopg connection (not pooled)."""
    import psycopg
    from pgvector.psycopg import register_vector

    conn = psycopg.connect(pg_url, autocommit=False)
    register_vector(conn)
    return conn


def preflight(conn, expected_old_dims: int = 384) -> dict:
    """Pre-flight checks: verify schema state and return stats.

    Returns dict with keys: row_count, has_new_column, pending_count,
    current_type, current_dims.
    """
    cur = conn.cursor()

    # Check current column type and dimensions
    cur.execute("""
        SELECT udt_name, character_maximum_length, numeric_precision
        FROM information_schema.columns
        WHERE table_name = 'jarvis' AND column_name = 'embedding'
    """)
    col_info = cur.fetchone()
    if col_info is None:
        raise RuntimeError("Table 'jarvis' or column 'embedding' not found")

    # Check total rows
    cur.execute("SELECT count(*) FROM jarvis")
    row_count = cur.fetchone()[0]

    # Check if new column already exists (resuming)
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'jarvis' AND column_name = 'embedding_new'
    """)
    has_new_column = cur.fetchone() is not None

    pending_count = 0
    if has_new_column:
        cur.execute("SELECT count(*) FROM jarvis WHERE embedding_new IS NULL")
        pending_count = cur.fetchone()[0]

    return {
        "row_count": row_count,
        "has_new_column": has_new_column,
        "pending_count": pending_count,
    }


def add_new_column(conn, dimensions: int) -> None:
    """Add the embedding_new halfvec column if it doesn't exist."""
    conn.execute(
        f"ALTER TABLE jarvis ADD COLUMN IF NOT EXISTS embedding_new halfvec({dimensions})"
    )
    conn.commit()
    logger.info("Added embedding_new halfvec(%d) column", dimensions)


def batch_reembed(
    conn,
    model_name: str,
    dimensions: int,
    batch_size: int,
    dry_run: bool = False,
) -> int:
    """Re-embed documents in batches. Resumable via WHERE embedding_new IS NULL.

    Returns total number of documents re-embedded.
    """
    from tools.embedding import EmbeddingService

    svc = EmbeddingService(
        model_name=model_name,
        dimensions=dimensions,
        device=os.environ.get("EMBEDDING_DEVICE", "cpu"),
        backend=os.environ.get("EMBEDDING_BACKEND", "onnx"),
    )

    total_processed = 0
    while True:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, document FROM jarvis WHERE embedding_new IS NULL LIMIT %s",
            (batch_size,),
        )
        batch = cur.fetchall()
        if not batch:
            break

        if dry_run:
            logger.info("[DRY RUN] Would re-embed %d documents", len(batch))
            total_processed += len(batch)
            break  # Only preview first batch in dry-run

        ids = [row[0] for row in batch]
        texts = [row[1] for row in batch]

        embeddings = svc.encode_batch(texts, batch_size=batch_size)

        # Update each row
        update_cur = conn.cursor()
        for doc_id, emb in zip(ids, embeddings):
            update_cur.execute(
                "UPDATE jarvis SET embedding_new = %s::halfvec WHERE id = %s",
                (emb, doc_id),
            )
        conn.commit()

        total_processed += len(batch)
        logger.info("Re-embedded %d documents (total: %d)", len(batch), total_processed)

        # Track progress in jarvis_meta
        from tools.schema import set_meta
        set_meta("upgrade_embedding_progress", {
            "total_processed": total_processed,
            "status": "in_progress",
            "model": model_name,
            "dimensions": dimensions,
        })

    return total_processed


def verify_completeness(conn) -> bool:
    """Verify all rows have been re-embedded. Returns True if complete."""
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM jarvis WHERE embedding_new IS NULL")
    null_count = cur.fetchone()[0]
    if null_count > 0:
        logger.error("%d documents still have NULL embedding_new", null_count)
        return False
    return True


def create_new_index(conn, dry_run: bool = False) -> None:
    """Create HNSW index on the new column CONCURRENTLY.

    CONCURRENTLY requires autocommit mode.
    """
    index_sql = (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_jarvis_embedding_new "
        "ON jarvis USING hnsw (embedding_new halfvec_cosine_ops) "
        "WITH (m = 16, ef_construction = 200)"
    )
    if dry_run:
        logger.info("[DRY RUN] Would create index: %s", index_sql)
        return

    # CONCURRENTLY requires autocommit
    conn.autocommit = True
    try:
        logger.info("Creating HNSW index CONCURRENTLY (this may take a while)...")
        start = time.time()
        conn.execute(index_sql)
        elapsed = time.time() - start
        logger.info("Index created in %.1f seconds", elapsed)
    finally:
        conn.autocommit = False


def atomic_column_swap(conn, dry_run: bool = False) -> None:
    """Atomically swap embedding columns in a single transaction.

    1. Drop old index
    2. Rename embedding → embedding_old
    3. Rename embedding_new → embedding
    """
    swap_sql = [
        "DROP INDEX IF EXISTS idx_jarvis_embedding",
        "ALTER TABLE jarvis RENAME COLUMN embedding TO embedding_old",
        "ALTER TABLE jarvis RENAME COLUMN embedding_new TO embedding",
        "ALTER INDEX IF EXISTS idx_jarvis_embedding_new RENAME TO idx_jarvis_embedding",
    ]

    if dry_run:
        for sql in swap_sql:
            logger.info("[DRY RUN] Would execute: %s", sql)
        return

    for sql in swap_sql:
        conn.execute(sql)
    conn.commit()
    logger.info("Column swap complete: embedding_new → embedding")


def update_meta(
    conn,
    model_name: str,
    dimensions: int,
    dry_run: bool = False,
) -> None:
    """Update jarvis_meta with new embedding config and schema version."""
    if dry_run:
        logger.info("[DRY RUN] Would update jarvis_meta: model=%s, dims=%d", model_name, dimensions)
        return

    from tools.schema import set_meta

    set_meta("embedding_config", {
        "model": model_name,
        "dimensions": dimensions,
        "vector_type": "halfvec",
    })
    set_meta("schema_version", {"version": 2})
    set_meta("upgrade_embedding_progress", {
        "status": "completed",
        "model": model_name,
        "dimensions": dimensions,
    })
    logger.info("Updated jarvis_meta: model=%s, dims=%d, vector_type=halfvec, schema_version=2",
                model_name, dimensions)


def cleanup_old_column(conn, dry_run: bool = False) -> None:
    """Drop the embedding_old column after successful migration."""
    if dry_run:
        logger.info("[DRY RUN] Would drop embedding_old column")
        return

    conn.execute("ALTER TABLE jarvis DROP COLUMN IF EXISTS embedding_old")
    conn.commit()
    logger.info("Dropped embedding_old column")


def main():
    parser = argparse.ArgumentParser(
        description="Upgrade Jarvis embedding model from 384d vector to 768d halfvec."
    )
    parser.add_argument(
        "--pg-url",
        help="PostgreSQL connection URL (overrides POSTGRES_URL env / config)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Documents per re-embedding batch (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Target embedding model (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--dimensions",
        type=int,
        default=DEFAULT_DIMENSIONS,
        help=f"Target embedding dimensions (default: {DEFAULT_DIMENSIONS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview migration steps without making changes",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Drop the old embedding column after successful migration",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    # Resolve connection URL
    pg_url = args.pg_url or os.environ.get("POSTGRES_URL")
    if not pg_url:
        try:
            from tools.config import get_postgres_config
            pg_url = get_postgres_config()["url"]
        except Exception:
            logger.error("No --pg-url provided, POSTGRES_URL not set, and config unavailable")
            sys.exit(1)

    if args.pg_url:
        os.environ["POSTGRES_URL"] = args.pg_url

    logger.info("Embedding model upgrade: %s (%dd halfvec)", args.model, args.dimensions)
    if args.dry_run:
        logger.info("DRY RUN — no changes will be made")

    conn = get_connection(pg_url)

    try:
        # 1. Pre-flight
        logger.info("─── Pre-flight ───")
        stats = preflight(conn)
        logger.info("  Rows: %d", stats["row_count"])
        logger.info("  New column exists: %s", stats["has_new_column"])
        if stats["has_new_column"]:
            logger.info("  Pending re-embed: %d", stats["pending_count"])

        if stats["row_count"] == 0:
            logger.info("No documents to migrate. Updating schema and meta only.")
            if not args.dry_run:
                from tools.schema import ensure_schema
                os.environ["EMBEDDING_MODEL"] = args.model
                os.environ["EMBEDDING_DIMENSIONS"] = str(args.dimensions)
                os.environ["JARVIS_SKIP_MODEL_CHECK"] = "1"
                ensure_schema()
                update_meta(conn, args.model, args.dimensions)
            logger.info("Done.")
            return

        # 2. Add new column
        logger.info("─── Adding new column ───")
        if not args.dry_run:
            add_new_column(conn, args.dimensions)
        else:
            logger.info("[DRY RUN] Would add halfvec(%d) column", args.dimensions)

        # 3. Re-embed in batches
        logger.info("─── Re-embedding documents ───")
        processed = batch_reembed(
            conn,
            model_name=args.model,
            dimensions=args.dimensions,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
        )
        logger.info("Re-embedded %d documents", processed)

        # 4. Verify
        if not args.dry_run:
            logger.info("─── Verifying completeness ───")
            if not verify_completeness(conn):
                logger.error("Aborting: not all documents re-embedded. Re-run to resume.")
                sys.exit(1)
            logger.info("All documents re-embedded successfully")

        # 5. Create new index
        logger.info("─── Creating HNSW index ───")
        create_new_index(conn, dry_run=args.dry_run)

        # 6. Atomic column swap
        logger.info("─── Swapping columns ───")
        atomic_column_swap(conn, dry_run=args.dry_run)

        # 7. Update meta
        logger.info("─── Updating metadata ───")
        update_meta(conn, args.model, args.dimensions, dry_run=args.dry_run)

        # 8. Optional cleanup
        if args.cleanup:
            logger.info("─── Cleanup ───")
            cleanup_old_column(conn, dry_run=args.dry_run)

        logger.info("Migration complete!")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
