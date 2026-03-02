"""PostgreSQL + pgvector schema and connection management.

Provides the singleton connection pool and schema initialization for
the jarvis table. Replaces ChromaDB client management (v2.x) with
psycopg connection pooling (v3.0).
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

SCHEMA_SQL = """\
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS jarvis (
    id TEXT PRIMARY KEY,
    document TEXT NOT NULL,
    embedding halfvec({dimensions}) NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_jarvis_embedding ON jarvis
    USING hnsw (embedding halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 200);

CREATE INDEX IF NOT EXISTS idx_jarvis_metadata ON jarvis
    USING gin (metadata jsonb_path_ops);

CREATE INDEX IF NOT EXISTS idx_jarvis_tier2 ON jarvis ((metadata->>'tier'))
    WHERE metadata->>'tier' = 'tier2';

CREATE INDEX IF NOT EXISTS idx_jarvis_parent_file ON jarvis ((metadata->>'parent_file'))
    WHERE metadata->>'parent_file' IS NOT NULL;

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
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_jarvis_updated_at'
    ) THEN
        CREATE TRIGGER trg_jarvis_updated_at
            BEFORE UPDATE ON jarvis
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
END;
$$;
"""


META_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS jarvis_meta (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_jarvis_meta_updated_at'
    ) THEN
        CREATE TRIGGER trg_jarvis_meta_updated_at
            BEFORE UPDATE ON jarvis_meta
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
END;
$$;
"""


def _get_pool():
    """Get or create singleton connection pool with config-based invalidation.

    Same cache-key pattern as the former ChromaDB HttpClient singleton.
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
    """Create the jarvis table and indexes if they don't exist.

    Safe to call multiple times (all CREATE statements use IF NOT EXISTS).
    Called at server startup to handle first-run setup.
    """
    from .config import get_embedding_config

    emb = get_embedding_config()
    sql = SCHEMA_SQL.format(dimensions=emb["dimensions"])

    pool = _get_pool()
    with pool.connection() as conn:
        conn.execute(sql)
        conn.execute(META_SCHEMA_SQL)
        conn.commit()
    logger.info("Schema verified (dimensions=%d)", emb["dimensions"])


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
    """Serialize metadata dict to JSONB-compatible JSON string.

    ChromaDB required all metadata values to be str/int/float.
    JSONB is more flexible but we keep the same convention for compatibility.
    """
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


# ── jarvis_meta CRUD ─────────────────────────────────────────────────


def get_meta(key: str) -> dict | None:
    """Get a value from jarvis_meta by key.

    Returns the JSONB value as a dict, or None if the key doesn't exist.
    """
    result = execute_query(
        "SELECT value FROM jarvis_meta WHERE key = %s",
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
    """Upsert a value into jarvis_meta.

    Uses ON CONFLICT DO UPDATE for atomic upsert.
    """
    execute_write(
        """INSERT INTO jarvis_meta (key, value)
           VALUES (%s, %s)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
        (key, json.dumps(value, default=str)),
    )


def get_all_meta() -> dict[str, dict]:
    """Get all rows from jarvis_meta as a {key: value} dict."""
    rows = execute_query("SELECT key, value FROM jarvis_meta", fetch="all")
    result = {}
    for row in rows:
        val = row["value"]
        if isinstance(val, str):
            val = json.loads(val)
        result[row["key"]] = dict(val) if val else {}
    return result


# ── Model consistency ────────────────────────────────────────────────


class ModelMismatchError(Exception):
    """Raised when the embedding config doesn't match what's stored in jarvis_meta.

    Mixed embedding spaces produce garbage search results silently.
    This error forces the operator to either align the config or
    explicitly re-embed (--force-model-record).
    """
    pass


def check_model_consistency() -> None:
    """Verify embedding config matches what's stored in jarvis_meta.

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
        set_meta("schema_version", {"version": 2})
        logger.info("Recorded embedding config in jarvis_meta: %s (%dd)",
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
