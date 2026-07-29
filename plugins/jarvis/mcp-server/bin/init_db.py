#!/usr/bin/env python3
"""Initialize the Jarvis PostgreSQL database.

Standalone CLI script that creates the schema and records the embedding
config in jarvis_meta. Safe to run multiple times (all DDL is idempotent).

Usage:
    python bin/init_db.py [--pg-url URL] [--dimensions N] [--force-model-record]
    jarvis-init-db [--pg-url URL] [--dimensions N] [--force-model-record]
"""

import argparse
import logging
import os
import sys

# Allow imports from the parent mcp-server directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logger = logging.getLogger("jarvis-init-db")


def measure_contextual_state() -> tuple:
    """Observe the vault's ACTUAL augmentation state from the database.

    Counts chunked vault FILES and how many of them have a cached summary, then
    maps that onto the recorded state marker. Fail-soft: if the query cannot run
    (pre-migration database), fall back to the configured mode with zeroed
    coverage — that is no worse than the old behaviour and never blocks init.

    Returns ``(state, {"chunked_files": N, "files_with_summary": N})``.
    """
    from tools.config import get_contextual_augmentation_mode

    try:
        from tools.memory import resolve_achieved_augmentation
        from tools.schema import execute_query

        row = execute_query(
            """SELECT
                 count(DISTINCT d.parent_file) AS chunked,
                 count(DISTINCT c.parent_file) AS covered
               FROM obsidian.documents d
               LEFT JOIN obsidian.document_context c
                 ON c.parent_file = d.parent_file
               WHERE d.chunk_total > 1
                 AND d.parent_file IS NOT NULL AND d.parent_file <> ''""",
            fetch="one",
        ) or {}
        chunked = int(row.get("chunked") or 0)
        covered = int(row.get("covered") or 0)
        return (
            resolve_achieved_augmentation(chunked, covered),
            {"chunked_files": chunked, "files_with_summary": covered},
        )
    except Exception as exc:
        logger.warning(
            "Could not measure augmentation coverage (%s); recording the "
            "configured mode with zeroed coverage.", exc,
        )
        return (
            get_contextual_augmentation_mode(),
            {"chunked_files": 0, "files_with_summary": 0},
        )


def main():
    parser = argparse.ArgumentParser(
        description="Initialize the Jarvis PostgreSQL database schema."
    )
    parser.add_argument(
        "--pg-url",
        help="PostgreSQL connection URL (overrides POSTGRES_URL env / config)",
    )
    parser.add_argument(
        "--dimensions",
        type=int,
        help="Embedding dimensions (overrides EMBEDDING_DIMENSIONS env / config)",
    )
    parser.add_argument(
        "--force-model-record",
        action="store_true",
        help="Overwrite stored embedding config in jarvis_meta (use after re-embedding)",
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

    # Apply CLI overrides as env vars (picked up by config getters)
    if args.pg_url:
        os.environ["POSTGRES_URL"] = args.pg_url
    if args.dimensions:
        os.environ["EMBEDDING_DIMENSIONS"] = str(args.dimensions)
    if args.force_model_record:
        os.environ["JARVIS_SKIP_MODEL_CHECK"] = "1"

    # Run schema creation
    from tools.schema import ensure_schema, set_meta, check_model_consistency
    from tools.config import get_embedding_config

    logger.info("Creating schema...")
    ensure_schema()
    logger.info("Schema created successfully.")

    if args.force_model_record:
        from tools.embedding import get_embedding_model_identity

        emb = get_embedding_config()
        # Record the same identity check_model_consistency() compares
        # (model_id alias, not the runtime locator) — recording emb["model"]
        # re-trips the mismatch on the very next startup whenever the two
        # differ (host backend, legacy '/app/models/embedding' locator).
        model_identity = get_embedding_model_identity(emb)
        # Augmentation state is MEASURED, not read from config. This flag does
        # not re-embed anything, so stamping the configured mode would relabel a
        # mechanical space as 'summary' and permanently silence the mismatch
        # warning — the exact failure this flag is usually reached for.
        contextual, coverage = measure_contextual_state()
        set_meta("embedding_config", {
            "model": model_identity,
            "dimensions": emb["dimensions"],
            "vector_type": "halfvec",
            # State marker ('none' | 'mechanical' | 'summary' |
            # 'partial-summary'), not a boolean — these are different embedding
            # spaces, and a partially-covered vault is genuinely mixed.
            "contextual_chunks": contextual,
            "contextual_coverage": coverage,
        })
        logger.info(
            "Forced embedding config record: %s (%dd), augmentation=%s "
            "(%d/%d chunked files carry a summary)",
            model_identity, emb["dimensions"], contextual,
            coverage["files_with_summary"], coverage["chunked_files"],
        )
    else:
        # Normal consistency check (records on first run, validates on subsequent)
        try:
            check_model_consistency()
            logger.info("Model consistency check passed.")
        except Exception as e:
            logger.error("Model consistency check failed: %s", e)
            sys.exit(1)

    logger.info("Database initialization complete.")


if __name__ == "__main__":
    main()
