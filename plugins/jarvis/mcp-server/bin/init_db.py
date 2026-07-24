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
        from tools.config import get_contextual_embeddings_enabled
        from tools.embedding import get_embedding_model_identity

        emb = get_embedding_config()
        # Record the same identity check_model_consistency() compares
        # (model_id alias, not the runtime locator) — recording emb["model"]
        # re-trips the mismatch on the very next startup whenever the two
        # differ (host backend, legacy '/app/models/embedding' locator).
        model_identity = get_embedding_model_identity(emb)
        set_meta("embedding_config", {
            "model": model_identity,
            "dimensions": emb["dimensions"],
            "vector_type": "halfvec",
            "contextual_chunks": bool(get_contextual_embeddings_enabled()),
        })
        logger.info(
            "Forced embedding config record: %s (%dd)",
            model_identity, emb["dimensions"],
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
