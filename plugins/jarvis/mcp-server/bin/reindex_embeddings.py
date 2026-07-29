#!/usr/bin/env python3
"""Atomically re-embed Jarvis' namespaced PostgreSQL stores.

All inference happens into temporary staging tables. Live vectors are only
replaced after every selected document has a validated replacement, and the
final replacement is one transaction. This keeps the old embedding space
fully usable if inference fails partway through.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass

# Allow imports from the parent mcp-server directory.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logger = logging.getLogger("jarvis-reindex-embeddings")


@dataclass(frozen=True)
class StoreSpec:
    name: str
    table: str
    trigger: str
    staging_table: str
    chunk_table: str | None = None
    chunk_staging_table: str | None = None
    # Vault fragments are embedded with a document-context prefix
    # (tools/chunk_context.py); re-embedding their STORED raw text would
    # silently strip that context and diverge from the live index path.
    contextual: bool = False


STORES = {
    "local": StoreSpec(
        name="local",
        table="local.memories",
        trigger="trg_local_memories_updated_at",
        staging_table="jarvis_reindex_local_stage",
        chunk_table="local.memory_chunks",
        chunk_staging_table="jarvis_reindex_local_chunk_stage",
    ),
    "obsidian": StoreSpec(
        name="obsidian",
        table="obsidian.documents",
        trigger="trg_obsidian_documents_updated_at",
        staging_table="jarvis_reindex_obsidian_stage",
        contextual=True,
    ),
}


def resolve_stores(selection: str) -> list[StoreSpec]:
    if selection == "all":
        return [STORES["local"], STORES["obsidian"]]
    return [STORES[selection]]


def get_connection(pg_url: str):
    import psycopg
    from pgvector.psycopg import register_vector

    conn = psycopg.connect(pg_url, autocommit=False)
    register_vector(conn)
    return conn


def validate_embeddings(
    embeddings: list[list[float]], expected_count: int, dimensions: int
) -> None:
    if len(embeddings) != expected_count:
        raise RuntimeError(
            f"inference returned {len(embeddings)} vectors for {expected_count} documents"
        )
    for index, vector in enumerate(embeddings):
        if len(vector) != dimensions:
            raise RuntimeError(
                f"vector {index} has {len(vector)} dimensions; expected {dimensions}"
            )


def count_store(conn, spec: StoreSpec) -> int:
    row = conn.execute(f"SELECT count(*) FROM {spec.table}").fetchone()
    return int(row[0])


def require_maintenance_ownership(conn, specs: list[StoreSpec]) -> None:
    """Fail before inference unless the connection can alter every live table."""
    for spec in specs:
        current_user, owner, allowed = conn.execute(
            """SELECT current_user,
                      pg_get_userbyid(class.relowner),
                      (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)
                      OR pg_has_role(current_user, class.relowner, 'MEMBER')
               FROM pg_class AS class
               WHERE class.oid = %s::regclass""",
            (spec.table,),
        ).fetchone()
        if not allowed:
            raise RuntimeError(
                f"reindex requires owner-level maintenance access to {spec.table}; "
                f"connected as {current_user}, table owner is {owner}"
            )


def stage_store(
    conn, spec: StoreSpec, service, dimensions: int, batch_size: int,
    coverage: dict | None = None,
) -> int:
    """Generate a complete replacement set without modifying the live table.

    ``coverage``, when supplied, accumulates the augmentation coverage this
    staging pass ACHIEVED — ``chunked_files`` and ``files_with_summary`` as sets
    of ``parent_file`` — so ``main`` can record the honest embedding-space
    identity instead of restating the configured mode. This script only READS
    the summary cache, so an empty cache produces a mechanical space; saying
    otherwise is what previously disarmed the startup consistency check.
    """
    conn.execute(
        f"CREATE TEMP TABLE {spec.staging_table} ("
        f"id TEXT PRIMARY KEY, embedding halfvec({dimensions}) NOT NULL"
        ") ON COMMIT PRESERVE ROWS"
    )
    if spec.chunk_staging_table:
        conn.execute(
            f"CREATE TEMP TABLE {spec.chunk_staging_table} ("
            "parent_id TEXT NOT NULL, chunk_index INTEGER NOT NULL, "
            "chunk_total INTEGER NOT NULL, document TEXT NOT NULL, "
            f"embedding halfvec({dimensions}) NOT NULL, "
            "PRIMARY KEY (parent_id, chunk_index)"
            ") ON COMMIT PRESERVE ROWS"
        )
    if spec.contextual:
        rows = conn.execute(
            f"SELECT id, document, title, chunk_heading, chunk_total, parent_file "
            f"FROM {spec.table} ORDER BY id"
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT id, document FROM {spec.table} ORDER BY id"
        ).fetchall()
    conn.commit()

    total = len(rows)
    for offset in range(0, total, batch_size):
        batch = rows[offset : offset + batch_size]
        if spec.chunk_staging_table:
            from tools.document_index import prepare_document

            prepared_rows = [
                (row, prepare_document(row[1], service, batch_size=batch_size))
                for row in batch
            ]
            embeddings = [prepared.canonical_embedding for _, prepared in prepared_rows]
            validate_embeddings(embeddings, len(batch), dimensions)
            with conn.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO {spec.staging_table} (id, embedding) "
                    "VALUES (%s, %s::halfvec)",
                    [(row[0], prepared.canonical_embedding)
                     for row, prepared in prepared_rows],
                )
                chunk_rows = []
                for row, prepared in prepared_rows:
                    if not prepared.is_chunked:
                        continue
                    chunk_total = len(prepared.windows)
                    chunk_rows.extend(
                        (row[0], index, chunk_total, window, vector)
                        for index, (window, vector) in enumerate(
                            zip(prepared.windows, prepared.window_embeddings)
                        )
                    )
                if chunk_rows:
                    cur.executemany(
                        f"INSERT INTO {spec.chunk_staging_table} "
                        "(parent_id, chunk_index, chunk_total, document, embedding) "
                        "VALUES (%s, %s, %s, %s, %s::halfvec)",
                        chunk_rows,
                    )
        else:
            if spec.contextual:
                # Must mirror the live index path (tools/memory.py) — embedding
                # the stored raw text would strip the document-context prefix.
                # Summaries are READ from the cache here, never generated: a
                # re-embed reproduces the space the index built, and generating
                # would silently change it mid-migration.
                from tools.chunk_context import augment_vault_row
                from tools.config import get_contextual_augmentation_mode
                from tools.context_summary import fetch_document_summaries

                contextual_mode = get_contextual_augmentation_mode()
                summaries = fetch_document_summaries(
                    [row[5] or "" for row in batch], conn=conn
                )
                embed_inputs = [
                    augment_vault_row(
                        row[1],
                        parent_file=row[5] or "",
                        title=row[2] or "",
                        chunk_heading=row[3] or "",
                        chunk_total=row[4],
                        mode=contextual_mode,
                        summary=summaries.get(row[5] or ""),
                    )
                    for row in batch
                ]
                if coverage is not None:
                    for row in batch:
                        parent_file = row[5] or ""
                        try:
                            is_chunk = int(row[4] or 1) > 1
                        except (TypeError, ValueError):
                            is_chunk = False
                        if not parent_file or not is_chunk:
                            continue
                        coverage.setdefault("chunked_files", set()).add(parent_file)
                        if summaries.get(parent_file):
                            coverage.setdefault(
                                "files_with_summary", set()
                            ).add(parent_file)
            else:
                embed_inputs = [row[1] for row in batch]
            embeddings = service.encode_batch(embed_inputs, batch_size=batch_size)
            validate_embeddings(embeddings, len(batch), dimensions)
            with conn.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO {spec.staging_table} (id, embedding) "
                    "VALUES (%s, %s::halfvec)",
                    [(row[0], vector) for row, vector in zip(batch, embeddings)],
                )
        conn.commit()
        logger.info(
            "%s: staged %d/%d vectors", spec.table, min(offset + len(batch), total), total
        )
    return total


def apply_staged(
    conn,
    staged: list[tuple[StoreSpec, int]],
    *,
    model_identity: str,
    dimensions: int,
    backend: str,
    contextual_state: str | None = None,
    contextual_coverage: dict | None = None,
) -> dict[str, int]:
    """Atomically replace live vectors while preserving ``updated_at`` values.

    ``contextual_state`` is the augmentation state the staging pass ACHIEVED
    (see ``stage_store``'s ``coverage``); it falls back to the configured mode
    only when the caller has nothing better, e.g. a non-contextual store.
    """
    applied: dict[str, int] = {}
    with conn.transaction():
        for spec, expected_count in staged:
            # The lock closes the race between staging and the final row-count
            # check. A concurrent insert makes us abort instead of leaving one
            # document in the old embedding space.
            conn.execute(f"LOCK TABLE {spec.table} IN ACCESS EXCLUSIVE MODE")
            if spec.chunk_table:
                conn.execute(
                    f"LOCK TABLE {spec.chunk_table} IN ACCESS EXCLUSIVE MODE"
                )
            live_count = count_store(conn, spec)
            staged_count = int(
                conn.execute(
                    f"SELECT count(*) FROM {spec.staging_table}"
                ).fetchone()[0]
            )
            if live_count != expected_count or staged_count != expected_count:
                raise RuntimeError(
                    f"{spec.table} changed during staging "
                    f"(expected={expected_count}, live={live_count}, staged={staged_count})"
                )

            if expected_count:
                # Embedding-only maintenance must not make every memory look
                # freshly edited. The trigger change and UPDATE are transactional;
                # any failure rolls both back, leaving the trigger enabled.
                conn.execute(f"ALTER TABLE {spec.table} DISABLE TRIGGER {spec.trigger}")
                cursor = conn.execute(
                    f"UPDATE {spec.table} AS live SET embedding = stage.embedding "
                    f"FROM {spec.staging_table} AS stage WHERE live.id = stage.id"
                )
                if cursor.rowcount != expected_count:
                    raise RuntimeError(
                        f"updated {cursor.rowcount} of {expected_count} rows in {spec.table}"
                    )
                if spec.chunk_table and spec.chunk_staging_table:
                    conn.execute(f"DELETE FROM {spec.chunk_table}")
                    conn.execute(
                        f"INSERT INTO {spec.chunk_table} "
                        "(parent_id, chunk_index, chunk_total, document, embedding) "
                        f"SELECT parent_id, chunk_index, chunk_total, document, embedding "
                        f"FROM {spec.chunk_staging_table}"
                    )
                conn.execute(f"ALTER TABLE {spec.table} ENABLE TRIGGER {spec.trigger}")
            applied[spec.table] = expected_count

        # local.meta embedding_config describes the embedding space of EVERY
        # store, so a partial --store run must not relabel it: that would
        # disarm check_model_consistency() for the stores still holding
        # old-space vectors. Only record the new identity when every other
        # store was covered or is verifiably empty.
        covered = {spec.name for spec, _ in staged}
        stale_stores = []
        for name, spec in STORES.items():
            if name in covered:
                continue
            # SHARE mode blocks concurrent writers until this transaction
            # commits, so "empty" cannot flip between the count and the meta
            # relabel (a stale-model server could otherwise slip an old-space
            # vector in during that window).
            conn.execute(f"LOCK TABLE {spec.table} IN SHARE MODE")
            if count_store(conn, spec):
                stale_stores.append(spec.table)
        if stale_stores:
            logger.warning(
                "local.meta embedding_config left unchanged: %s still hold "
                "vectors from the previous embedding space. Run with "
                "--store all to complete the migration; the startup "
                "consistency check keeps failing until then.",
                ", ".join(stale_stores),
            )
        else:
            from tools.config import get_contextual_augmentation_mode

            record = {
                "model": model_identity,
                "dimensions": dimensions,
                "vector_type": "halfvec",
                "backend": backend,
                # The ACHIEVED state, not a restatement of config: 'mechanical',
                # 'summary' and 'partial-summary' are different embedding spaces,
                # and this script cannot generate the summaries that would make
                # 'summary' true.
                "contextual_chunks": (
                    contextual_state or get_contextual_augmentation_mode()
                ),
            }
            if contextual_coverage is not None:
                record["contextual_coverage"] = contextual_coverage
            conn.execute(
                """INSERT INTO local.meta (key, value)
                   VALUES ('embedding_config', %s::jsonb)
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                (
                    json.dumps(record),
                ),
            )
    return applied


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--store", choices=("all", "local", "obsidian"), default="all"
    )
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--pg-url", help="Override POSTGRES_URL/config")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be >= 1")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    from tools.config import (
        get_contextual_augmentation_mode, get_embedding_config,
        get_postgres_config,
    )
    from tools.embedding import get_embedding_model_identity, get_embedding_service

    pg_url = args.pg_url or os.environ.get("POSTGRES_URL") or get_postgres_config()["url"]
    config = get_embedding_config()
    service = get_embedding_service()
    model_identity = get_embedding_model_identity(config)
    specs = resolve_stores(args.store)
    conn = get_connection(pg_url)
    started = time.perf_counter()
    try:
        counts = {spec.table: count_store(conn, spec) for spec in specs}
        conn.commit()
        logger.info(
            "model=%s backend=%s dimensions=%d stores=%s",
            model_identity,
            service.backend,
            service.dimensions,
            counts,
        )
        if args.dry_run:
            print(json.dumps({"dry_run": True, "counts": counts}, sort_keys=True))
            return 0

        require_maintenance_ownership(conn, specs)
        conn.commit()

        coverage: dict = {}
        staged = [
            (
                spec,
                stage_store(
                    conn,
                    spec,
                    service,
                    dimensions=service.dimensions,
                    batch_size=args.batch_size,
                    coverage=coverage,
                ),
            )
            for spec in specs
        ]
        # Honest augmentation state: this script READS the summary cache, so an
        # empty cache means a mechanical space no matter what config says.
        from tools.memory import resolve_achieved_augmentation

        chunked_files = len(coverage.get("chunked_files", set()))
        files_with_summary = len(coverage.get("files_with_summary", set()))
        contextual_state = None
        contextual_coverage = None
        if any(spec.contextual for spec in specs):
            contextual_state = resolve_achieved_augmentation(
                chunked_files, files_with_summary
            )
            contextual_coverage = {
                "chunked_files": chunked_files,
                "files_with_summary": files_with_summary,
            }
            if contextual_state != get_contextual_augmentation_mode():
                logger.warning(
                    "Augmentation coverage: %d/%d chunked vault files had a "
                    "cached summary, so the re-embedded space is '%s' (config "
                    "says '%s'). This script never generates summaries — run "
                    "bin/generate_summaries.py first, then re-embed.",
                    files_with_summary, chunked_files, contextual_state,
                    get_contextual_augmentation_mode(),
                )
        applied = apply_staged(
            conn,
            staged,
            model_identity=model_identity,
            dimensions=service.dimensions,
            backend=service.backend,
            contextual_state=contextual_state,
            contextual_coverage=contextual_coverage,
        )
        result = {
            "success": True,
            "model": model_identity,
            "backend": service.backend,
            "dimensions": service.dimensions,
            "rows": applied,
            "duration_seconds": round(time.perf_counter() - started, 3),
        }
        if contextual_state is not None:
            result["contextual_augmentation"] = contextual_state
            result["summary_coverage"] = contextual_coverage
        print(json.dumps(result, sort_keys=True))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
