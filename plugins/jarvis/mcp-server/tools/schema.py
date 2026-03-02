"""PostgreSQL + pgvector schema and connection management.

Provides the singleton connection pool and schema initialization for
the core.memories and vault.documents tables. Replaces the single-table
public.jarvis design (v2.x) with dual-schema architecture (v3.0).

Schemas:
- core: memories (observations, patterns, strategic, etc.)
- vault: indexed vault file chunks
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger("jarvis-core")

# Singleton pool state
_pool = None
_pool_cache_key: tuple | None = None

# ── Schema SQL ────────────────────────────────────────────────────────

CORE_SCHEMA_SQL = """\
CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS core;

CREATE TABLE IF NOT EXISTS core.memories (
    id TEXT PRIMARY KEY,
    document TEXT NOT NULL,
    embedding halfvec({dimensions}) NOT NULL,

    -- Classification columns
    category TEXT NOT NULL DEFAULT 'observation'
        CHECK (category IN ('observation', 'pattern', 'learning', 'decision',
                            'summary', 'code', 'relationship', 'hint', 'plan',
                            'worklog', 'memory')),
    scope TEXT NOT NULL DEFAULT 'global'
        CHECK (scope IN ('global', 'project')),
    project TEXT,
    source TEXT NOT NULL DEFAULT 'auto-extract',
    importance_score FLOAT NOT NULL DEFAULT 0.5
        CHECK (importance_score >= 0.0 AND importance_score <= 1.0),
    retrieval_count FLOAT NOT NULL DEFAULT 0.0,

    -- Lifecycle
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'superseded', 'deleted')),
    superseded_by TEXT,
    deleted_at TIMESTAMPTZ,

    -- Remaining flexible metadata
    metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Cross-field integrity
DO $$ BEGIN
    ALTER TABLE core.memories ADD CONSTRAINT chk_scope_project
        CHECK ((scope = 'project' AND project IS NOT NULL) OR (scope = 'global'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE core.memories ADD CONSTRAINT chk_superseded_by
        CHECK ((status = 'superseded' AND superseded_by IS NOT NULL) OR (status != 'superseded'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Indexes
CREATE INDEX IF NOT EXISTS idx_core_embedding ON core.memories
    USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 200);
CREATE INDEX IF NOT EXISTS idx_core_metadata ON core.memories USING gin (metadata jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_core_category ON core.memories (category);
CREATE INDEX IF NOT EXISTS idx_core_active ON core.memories (status) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_core_importance ON core.memories (importance_score DESC);

-- Active view (query default — excludes superseded + deleted)
CREATE OR REPLACE VIEW core.active_memories AS
    SELECT * FROM core.memories WHERE status = 'active';

-- updated_at trigger function (shared by both schemas)
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_core_memories_updated_at'
    ) THEN
        CREATE TRIGGER trg_core_memories_updated_at
            BEFORE UPDATE ON core.memories
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
END;
$$;
"""


VAULT_SCHEMA_SQL = """\
CREATE SCHEMA IF NOT EXISTS vault;

CREATE TABLE IF NOT EXISTS vault.documents (
    id TEXT PRIMARY KEY,
    document TEXT NOT NULL,
    embedding halfvec({dimensions}) NOT NULL,

    -- Vault-specific columns
    parent_file TEXT NOT NULL,
    directory TEXT NOT NULL DEFAULT '',
    vault_type TEXT NOT NULL DEFAULT 'document',
    title TEXT NOT NULL DEFAULT '',
    chunk_index INTEGER NOT NULL DEFAULT 0,
    chunk_total INTEGER NOT NULL DEFAULT 1,
    chunk_heading TEXT NOT NULL DEFAULT '',
    importance_score FLOAT NOT NULL DEFAULT 0.5,

    -- Remaining flexible metadata
    metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_vault_embedding ON vault.documents
    USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 200);
CREATE INDEX IF NOT EXISTS idx_vault_metadata ON vault.documents USING gin (metadata jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_vault_parent_file ON vault.documents (parent_file);
CREATE INDEX IF NOT EXISTS idx_vault_directory ON vault.documents (directory);
CREATE INDEX IF NOT EXISTS idx_vault_importance ON vault.documents (importance_score DESC);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_vault_documents_updated_at'
    ) THEN
        CREATE TRIGGER trg_vault_documents_updated_at
            BEFORE UPDATE ON vault.documents
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
END;
$$;
"""


CORE_META_SQL = """\
CREATE TABLE IF NOT EXISTS core.meta (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_core_meta_updated_at'
    ) THEN
        CREATE TRIGGER trg_core_meta_updated_at
            BEFORE UPDATE ON core.meta
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
END;
$$;
"""


MIGRATION_SQL = """\
-- Step 1: Vault rows → vault.documents
INSERT INTO vault.documents (id, document, embedding, parent_file, directory,
    vault_type, title, chunk_index, chunk_total, chunk_heading,
    importance_score, metadata, created_at, updated_at)
SELECT id, document, embedding,
    COALESCE(metadata->>'parent_file', REPLACE(id, 'vault::', '')),
    COALESCE(metadata->>'directory', ''),
    COALESCE(metadata->>'vault_type', 'document'),
    COALESCE(metadata->>'title', ''),
    CASE WHEN metadata->>'chunk_index' ~ '^\\d+$'
         THEN (metadata->>'chunk_index')::int ELSE 0 END,
    CASE WHEN metadata->>'chunk_total' ~ '^\\d+$'
         THEN (metadata->>'chunk_total')::int ELSE 1 END,
    COALESCE(metadata->>'chunk_heading', ''),
    CASE WHEN metadata->>'importance_score' ~ '^\\d+\\.?\\d*$'
         THEN LEAST((metadata->>'importance_score')::float, 1.0) ELSE 0.5 END,
    metadata - 'parent_file' - 'directory' - 'vault_type' - 'title'
            - 'chunk_index' - 'chunk_total' - 'chunk_heading'
            - 'importance_score' - 'tier' - 'namespace' - 'type'
            - 'promoted' - 'source',
    created_at, updated_at
FROM jarvis WHERE id LIKE 'vault::%%'
ON CONFLICT (id) DO NOTHING;

-- Step 2: Memory/content rows → core.memories
INSERT INTO core.memories (id, document, embedding, category, scope, project,
    source, importance_score, retrieval_count, status, superseded_by,
    metadata, created_at, updated_at)
SELECT id, document, embedding,
    CASE WHEN id LIKE 'memory::%%' THEN 'memory'
         WHEN metadata->>'type' IN ('observation','pattern','learning','decision',
              'summary','code','relationship','hint','plan','worklog','memory')
         THEN metadata->>'type'
         ELSE 'observation' END,
    CASE WHEN metadata->>'scope' IN ('global','project')
         THEN metadata->>'scope' ELSE 'global' END,
    NULLIF(metadata->>'project', ''),
    COALESCE(metadata->>'source', 'auto-extract'),
    CASE WHEN metadata->>'importance_score' ~ '^\\d+\\.?\\d*$'
         THEN LEAST((metadata->>'importance_score')::float, 1.0) ELSE 0.5 END,
    CASE WHEN metadata->>'retrieval_count' ~ '^\\d+\\.?\\d*$'
         THEN (metadata->>'retrieval_count')::float ELSE 0.0 END,
    CASE WHEN metadata->>'status' IN ('active','superseded','deleted')
         THEN metadata->>'status' ELSE 'active' END,
    NULLIF(metadata->>'superseded_by', ''),
    metadata - 'type' - 'scope' - 'project' - 'source' - 'importance_score'
            - 'retrieval_count' - 'status' - 'superseded_by' - 'tier'
            - 'namespace' - 'promoted' - 'promoted_at' - 'original_tier2_id',
    created_at, updated_at
FROM jarvis WHERE id NOT LIKE 'vault::%%'
ON CONFLICT (id) DO NOTHING;

-- Step 3: Migrate meta table
INSERT INTO core.meta (key, value, updated_at)
SELECT key, value, updated_at FROM jarvis_meta
ON CONFLICT (key) DO NOTHING;

-- Step 4: Bump schema version
INSERT INTO core.meta (key, value) VALUES ('schema_version', '{{"version": 3}}')
ON CONFLICT (key) DO UPDATE SET value = '{{"version": 3}}'::jsonb;
"""


def _get_pool():
    """Get or create singleton connection pool with config-based invalidation.

    Cache-key pattern ensures singleton per connection string.
    Pool is created with pgvector type registration on each connection.
    """
    global _pool, _pool_cache_key
    from .config import get_postgres_config, get_embedding_config

    cfg = get_postgres_config()
    emb = get_embedding_config()
    key = (cfg["url"], emb["dimensions"])

    if _pool is not None and _pool_cache_key == key:
        return _pool

    if _pool is not None:
        try:
            _pool.close()
        except Exception:
            pass

    import psycopg_pool
    from pgvector.psycopg import register_vector

    _pool = psycopg_pool.ConnectionPool(
        conninfo=cfg["url"],
        min_size=1,
        max_size=5,
        open=True,
        configure=lambda conn: register_vector(conn),
    )
    _pool_cache_key = key
    logger.info("PostgreSQL connection pool created for %s", cfg["url"].split("@")[-1])
    return _pool


def reset_pool() -> None:
    """Close and reset the connection pool. Used in tests and config changes."""
    global _pool, _pool_cache_key
    if _pool is not None:
        try:
            _pool.close()
        except Exception:
            pass
    _pool = None
    _pool_cache_key = None


def ensure_schema() -> None:
    """Create both schemas, tables, and indexes. Run migration if needed.

    Safe to call multiple times (all DDL uses IF NOT EXISTS / IF NOT EXISTS guards).
    Called at server startup to handle first-run setup and migrations.
    """
    from .config import get_embedding_config

    emb = get_embedding_config()
    dims = emb["dimensions"]

    pool = _get_pool()
    with pool.connection() as conn:
        # Advisory lock to prevent concurrent migration
        conn.execute("SELECT pg_advisory_lock(42424242)")
        try:
            # pgvector preflight
            row = conn.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            ).fetchone()
            if row:
                logger.info("pgvector version: %s", row[0])

            # Create both schemas and tables (idempotent)
            conn.execute(CORE_SCHEMA_SQL.format(dimensions=dims))
            conn.execute(VAULT_SCHEMA_SQL.format(dimensions=dims))
            conn.execute(CORE_META_SQL)

            # Check if migration is needed
            old_table = conn.execute(
                "SELECT to_regclass('public.jarvis')"
            ).fetchone()
            has_old_table = old_table and old_table[0] is not None

            if has_old_table:
                # Check if migration already done
                schema_ver = conn.execute(
                    "SELECT value FROM core.meta WHERE key = 'schema_version'"
                ).fetchone()
                already_migrated = (
                    schema_ver
                    and isinstance(schema_ver[0], dict)
                    and schema_ver[0].get("version", 0) >= 3
                )

                if not already_migrated:
                    logger.info("Migrating data from public.jarvis to core/vault schemas...")
                    conn.execute(MIGRATION_SQL.format(dimensions=dims))
                    logger.info("Migration complete")

            conn.commit()
        finally:
            conn.execute("SELECT pg_advisory_unlock(42424242)")

    logger.info("Schema verified (dimensions=%d)", dims)


# ── Query helpers ─────────────────────────────────────────────────────


def execute_query(
    sql: str,
    params: tuple | dict | None = None,
    *,
    fetch: str = "all",
) -> Any:
    """Execute a SQL query and return results.

    Args:
        sql: SQL query string with %s or %(name)s placeholders.
        params: Query parameters.
        fetch: "all" returns list of dicts, "one" returns single dict or None,
               "none" returns None (for INSERT/UPDATE/DELETE without RETURNING).

    Returns:
        Query results as list of dicts, single dict, or None.
    """
    pool = _get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if fetch == "none":
                conn.commit()
                return None
            columns = [desc.name for desc in cur.description]
            if fetch == "one":
                row = cur.fetchone()
                return dict(zip(columns, row)) if row else None
            rows = cur.fetchall()
            return [dict(zip(columns, row)) for row in rows]


def execute_write(
    sql: str,
    params: tuple | dict | None = None,
    *,
    returning: bool = False,
) -> dict | None:
    """Execute a write query (INSERT/UPDATE/DELETE).

    Args:
        sql: SQL statement.
        params: Query parameters.
        returning: If True, fetch and return the RETURNING row as dict.

    Returns:
        Dict of the RETURNING row if returning=True, else None.
    """
    pool = _get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            result = None
            if returning and cur.description:
                row = cur.fetchone()
                if row:
                    columns = [desc.name for desc in cur.description]
                    result = dict(zip(columns, row))
            conn.commit()
            return result


def execute_batch(
    sql: str,
    params_list: list[tuple],
) -> int:
    """Execute a parameterized query for multiple parameter sets.

    Uses executemany for efficient batch operations.

    Returns:
        Number of rows affected.
    """
    if not params_list:
        return 0
    pool = _get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, params_list)
            count = cur.rowcount
            conn.commit()
            return count


def metadata_to_jsonb(metadata: dict) -> str:
    """Serialize metadata dict to JSONB-compatible JSON string."""
    return json.dumps(metadata, default=str)


def jsonb_to_metadata(jsonb_val) -> dict:
    """Deserialize JSONB value to a Python dict.

    psycopg auto-deserializes JSONB to dict, but this handles
    edge cases (None, string).
    """
    if jsonb_val is None:
        return {}
    if isinstance(jsonb_val, str):
        return json.loads(jsonb_val)
    return dict(jsonb_val)


# ── core.meta CRUD ───────────────────────────────────────────────────


def get_meta(key: str) -> dict | None:
    """Get a value from core.meta by key.

    Returns the JSONB value as a dict, or None if the key doesn't exist.
    """
    result = execute_query(
        "SELECT value FROM core.meta WHERE key = %s",
        (key,),
        fetch="one",
    )
    if result is None:
        return None
    val = result["value"]
    if isinstance(val, str):
        return json.loads(val)
    return dict(val) if val else {}


def set_meta(key: str, value: dict) -> None:
    """Upsert a value into core.meta.

    Uses ON CONFLICT DO UPDATE for atomic upsert.
    """
    execute_write(
        """INSERT INTO core.meta (key, value)
           VALUES (%s, %s)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
        (key, json.dumps(value, default=str)),
    )


def get_all_meta() -> dict[str, dict]:
    """Get all rows from core.meta as a {key: value} dict."""
    rows = execute_query("SELECT key, value FROM core.meta", fetch="all")
    result = {}
    for row in rows:
        val = row["value"]
        if isinstance(val, str):
            val = json.loads(val)
        result[row["key"]] = dict(val) if val else {}
    return result


# ── Model consistency ────────────────────────────────────────────────


class ModelMismatchError(Exception):
    """Raised when the embedding config doesn't match what's stored in core.meta.

    Mixed embedding spaces produce garbage search results silently.
    This error forces the operator to either align the config or
    explicitly re-embed (--force-model-record).
    """
    pass


def check_model_consistency() -> None:
    """Verify embedding config matches what's stored in core.meta.

    First run: records the current config + schema version.
    Subsequent runs: compares model name and dimensions.
    Bypass: set JARVIS_SKIP_MODEL_CHECK=1 env var.
    """
    if os.environ.get("JARVIS_SKIP_MODEL_CHECK") == "1":
        logger.info("Model consistency check skipped (JARVIS_SKIP_MODEL_CHECK=1)")
        return

    from .config import get_embedding_config

    emb = get_embedding_config()
    stored = get_meta("embedding_config")

    if stored is None:
        # First run — record current config
        set_meta("embedding_config", {
            "model": emb["model"],
            "dimensions": emb["dimensions"],
            "vector_type": "halfvec",
        })
        set_meta("schema_version", {"version": 3})
        logger.info("Recorded embedding config in core.meta: %s (%dd)",
                     emb["model"], emb["dimensions"])
        return

    # Compare model and dimensions
    if stored.get("model") != emb["model"]:
        raise ModelMismatchError(
            f"Embedding model mismatch: database has '{stored.get('model')}' "
            f"but config specifies '{emb['model']}'. "
            f"Re-embed with --force-model-record or set JARVIS_SKIP_MODEL_CHECK=1."
        )

    stored_dims = int(stored.get("dimensions", 0))
    if stored_dims != emb["dimensions"]:
        raise ModelMismatchError(
            f"Embedding dimensions mismatch: database has {stored_dims} "
            f"but config specifies {emb['dimensions']}. "
            f"Re-embed with --force-model-record or set JARVIS_SKIP_MODEL_CHECK=1."
        )
