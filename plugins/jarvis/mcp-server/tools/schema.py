"""PostgreSQL + pgvector schema and connection management.

Provides the singleton connection pool and schema initialization for
the local.memories and obsidian.documents tables. Replaces the single-table
public.jarvis design (v2.x) with dual-schema architecture (v3.0).

Schemas:
- local: memories (observations, patterns, strategic, etc.)
- obsidian: indexed vault file chunks
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

LOCAL_SCHEMA_SQL = """\
CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS local;

CREATE TABLE IF NOT EXISTS local.memories (
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
    ALTER TABLE local.memories ADD CONSTRAINT chk_scope_project
        CHECK ((scope = 'project' AND project IS NOT NULL) OR (scope = 'global'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE local.memories ADD CONSTRAINT chk_superseded_by
        CHECK ((status = 'superseded' AND superseded_by IS NOT NULL) OR (status != 'superseded'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Indexes
CREATE INDEX IF NOT EXISTS idx_local_embedding ON local.memories
    USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 200);
CREATE INDEX IF NOT EXISTS idx_local_metadata ON local.memories USING gin (metadata jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_local_category ON local.memories (category);
CREATE INDEX IF NOT EXISTS idx_local_active ON local.memories (status) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_local_importance ON local.memories (importance_score DESC);

-- Search-only windows for canonical memories that exceed an inference context.
-- The full document remains in local.memories and ID reads always return it.
CREATE TABLE IF NOT EXISTS local.memory_chunks (
    parent_id TEXT NOT NULL REFERENCES local.memories(id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    chunk_index INTEGER NOT NULL,
    chunk_total INTEGER NOT NULL,
    document TEXT NOT NULL,
    embedding halfvec({dimensions}) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (parent_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_local_memory_chunks_embedding
    ON local.memory_chunks
    USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 200);
CREATE INDEX IF NOT EXISTS idx_local_memory_chunks_parent
    ON local.memory_chunks (parent_id);

-- Active view (query default — excludes superseded + deleted)
CREATE OR REPLACE VIEW local.active_memories AS
    SELECT * FROM local.memories WHERE status = 'active';

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
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_local_memories_updated_at'
    ) THEN
        CREATE TRIGGER trg_local_memories_updated_at
            BEFORE UPDATE ON local.memories
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
END;
$$;
"""

# Deprecated alias
CORE_SCHEMA_SQL = LOCAL_SCHEMA_SQL


# Retrieval observability is intentionally kept in its own tables. Candidate
# text never belongs here: locators and scores are enough to replay a trace and
# prevent the telemetry store from becoming a second copy of the vault.
RETRIEVAL_TELEMETRY_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS local.retrieval_events (
    id UUID PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    user_name TEXT,
    purpose TEXT NOT NULL,
    pipeline TEXT NOT NULL DEFAULT 'semantic',
    status TEXT NOT NULL DEFAULT 'complete',
    outcome TEXT NOT NULL DEFAULT 'unknown',
    query_text TEXT,
    query_sha256 TEXT NOT NULL,
    query_ref TEXT,
    query_length INTEGER NOT NULL DEFAULT 0,
    query_window_count INTEGER NOT NULL DEFAULT 1,
    model_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    config_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    funnel JSONB NOT NULL DEFAULT '{}'::jsonb,
    latency JSONB NOT NULL DEFAULT '{}'::jsonb,
    delivery JSONB NOT NULL DEFAULT '{}'::jsonb,
    shadow_status TEXT NOT NULL DEFAULT 'disabled'
        CHECK (shadow_status IN ('disabled', 'pending', 'running', 'complete',
                                 'partial', 'failed', 'skipped')),
    shadow_attempts INTEGER NOT NULL DEFAULT 0,
    shadow_started_at TIMESTAMPTZ,
    shadow_finished_at TIMESTAMPTZ,
    shadow_error TEXT
);

CREATE TABLE IF NOT EXISTS local.retrieval_candidates (
    event_id UUID NOT NULL REFERENCES local.retrieval_events(id) ON DELETE CASCADE,
    candidate_key TEXT NOT NULL,
    schema_name TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    parent_id TEXT,
    parent_file TEXT,
    chunk_index INTEGER,
    query_window_index INTEGER NOT NULL DEFAULT 0,
    vector_rank INTEGER,
    final_rank INTEGER,
    similarity DOUBLE PRECISION,
    pre_score DOUBLE PRECISION,
    raw_bge_logit DOUBLE PRECISION,
    bge_probability DOUBLE PRECISION,
    blended_score DOUBLE PRECISION,
    display_cost INTEGER,
    terminal_reason TEXT,
    returned BOOLEAN NOT NULL DEFAULT false,
    delivered BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (event_id, candidate_key)
);

CREATE TABLE IF NOT EXISTS local.retrieval_feedback (
    event_id UUID PRIMARY KEY REFERENCES local.retrieval_events(id) ON DELETE CASCADE,
    verdict TEXT NOT NULL CHECK (verdict IN ('useful', 'mixed', 'noisy', 'missed', 'unsure')),
    expected_missing_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    note TEXT,
    user_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS local.retrieval_candidate_feedback (
    event_id UUID NOT NULL,
    candidate_key TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('relevant', 'irrelevant', 'unsure')),
    note TEXT,
    user_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, candidate_key),
    FOREIGN KEY (event_id, candidate_key)
        REFERENCES local.retrieval_candidates(event_id, candidate_key)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_retrieval_events_created
    ON local.retrieval_events (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_retrieval_events_expires
    ON local.retrieval_events (expires_at);
CREATE INDEX IF NOT EXISTS idx_retrieval_events_purpose
    ON local.retrieval_events (purpose, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_retrieval_events_shadow
    ON local.retrieval_events (shadow_status, created_at)
    WHERE shadow_status IN ('pending', 'running');
CREATE INDEX IF NOT EXISTS idx_retrieval_candidates_event
    ON local.retrieval_candidates (event_id, vector_rank);
CREATE INDEX IF NOT EXISTS idx_retrieval_candidate_doc
    ON local.retrieval_candidates (schema_name, doc_id);
"""


OBSIDIAN_SCHEMA_SQL = """\
CREATE SCHEMA IF NOT EXISTS obsidian;

CREATE TABLE IF NOT EXISTS obsidian.documents (
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
CREATE INDEX IF NOT EXISTS idx_obsidian_embedding ON obsidian.documents
    USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 200);
CREATE INDEX IF NOT EXISTS idx_obsidian_metadata ON obsidian.documents USING gin (metadata jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_obsidian_parent_file ON obsidian.documents (parent_file);
CREATE INDEX IF NOT EXISTS idx_obsidian_directory ON obsidian.documents (directory);
CREATE INDEX IF NOT EXISTS idx_obsidian_importance ON obsidian.documents (importance_score DESC);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_obsidian_documents_updated_at'
    ) THEN
        CREATE TRIGGER trg_obsidian_documents_updated_at
            BEFORE UPDATE ON obsidian.documents
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
END;
$$;
"""

# Deprecated alias
VAULT_SCHEMA_SQL = OBSIDIAN_SCHEMA_SQL


# Phase 1 hybrid retrieval — statistical (lexical) recall channel.
# Generated STORED tsvector columns + GIN indexes give a full-text recall
# channel alongside the bi-encoder ANN channel. `to_tsvector('english', …)`
# (explicit regconfig) is IMMUTABLE — required for a GENERATED column; the
# one-argument form is only STABLE and would be rejected here.
#
# NOTE: to_tsvector emits a harmless NOTICE ("word is too long to be indexed")
# for any single token longer than 2047 bytes; the token is skipped, not an
# error, and the DDL still succeeds.
#
# BODY CAP: a single tsvector may hold at most 1MB of lexeme data. A stored,
# indexed memory can legitimately hold a whole pasted transcript/log (many
# unique tokens — hashes, ids, code), whose to_tsvector would exceed that limit
# and make BOTH the generated-column ALTER (rewritten for every existing row)
# and every future INSERT of such a row FAIL. The body input is therefore capped
# with ``left(document, 200000)`` (~200KB → well under 1MB even for all-unique
# tokens) inside the generated expression; title/heading are short and left
# uncapped. Lexical recall reads the head of the document, which is where the
# informative material lives.
#
# All statements are additive and idempotent (ADD COLUMN IF NOT EXISTS /
# CREATE INDEX IF NOT EXISTS), so ensure_schema can run this on every startup.
LEXICAL_SCHEMA_SQL = """\
-- obsidian.documents: title (A) + chunk_heading (B) + body (D), weighted.
ALTER TABLE obsidian.documents ADD COLUMN IF NOT EXISTS tsv tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(chunk_heading, '')), 'B') ||
        setweight(to_tsvector('english', left(document, 200000)), 'D')
    ) STORED;
CREATE INDEX IF NOT EXISTS idx_obsidian_tsv
    ON obsidian.documents USING gin (tsv);

-- local.memories: body only (D).
ALTER TABLE local.memories ADD COLUMN IF NOT EXISTS tsv tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', left(document, 200000)), 'D')
    ) STORED;
CREATE INDEX IF NOT EXISTS idx_local_tsv
    ON local.memories USING gin (tsv);

-- Retrieval channel provenance: 'semantic' | 'lexical' | 'both'.
ALTER TABLE local.retrieval_candidates ADD COLUMN IF NOT EXISTS channel TEXT;

-- Shadow retry backoff. Without a next-attempt gate a failed job is reclaimed
-- on the very next poll, so max_attempts burns in seconds and a brief model-host
-- outage permanently censors those events from the calibration corpus.
ALTER TABLE local.retrieval_events
    ADD COLUMN IF NOT EXISTS shadow_next_attempt_at TIMESTAMPTZ;
"""


LOCAL_META_SQL = """\
CREATE TABLE IF NOT EXISTS local.meta (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_local_meta_updated_at'
    ) THEN
        CREATE TRIGGER trg_local_meta_updated_at
            BEFORE UPDATE ON local.meta
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
END;
$$;
"""

# Deprecated alias
CORE_META_SQL = LOCAL_META_SQL


MIGRATION_SQL = """\
-- Step 1: Vault rows → obsidian.documents
INSERT INTO obsidian.documents (id, document, embedding, parent_file, directory,
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

-- Step 2: Memory/content rows → local.memories
INSERT INTO local.memories (id, document, embedding, category, scope, project,
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
INSERT INTO local.meta (key, value, updated_at)
SELECT key, value, updated_at FROM jarvis_meta
ON CONFLICT (key) DO NOTHING;

-- Step 4: Bump schema version
INSERT INTO local.meta (key, value) VALUES ('schema_version', '{{"version": 3}}')
ON CONFLICT (key) DO UPDATE SET value = '{{"version": 3}}'::jsonb;
"""


CONSOLIDATION_SCHEMA_SQL = """\
-- Phase 8: LLM-driven consolidation support
ALTER TABLE local.memories ADD COLUMN IF NOT EXISTS
    consolidation_run_id TEXT;

-- Self-supersession prevention
DO $$ BEGIN
    ALTER TABLE local.memories ADD CONSTRAINT chk_no_self_supersession
        CHECK (id != superseded_by);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Cycle prevention trigger (A→B→A and longer chains)
CREATE OR REPLACE FUNCTION local.prevent_supersession_cycle() RETURNS trigger AS $$
BEGIN
    IF NEW.superseded_by IS NULL THEN
        RETURN NEW;
    END IF;
    IF EXISTS (
        WITH RECURSIVE chain AS (
            SELECT NEW.superseded_by AS node_id
            UNION ALL
            SELECT m.superseded_by
            FROM local.memories m
            JOIN chain c ON m.id = c.node_id
            WHERE m.superseded_by IS NOT NULL
        )
        SELECT 1 FROM chain WHERE node_id = NEW.id
    ) THEN
        RAISE EXCEPTION 'Supersession cycle detected: % would create a loop', NEW.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_supersession_cycle'
    ) THEN
        CREATE TRIGGER trg_supersession_cycle
            BEFORE INSERT OR UPDATE OF superseded_by ON local.memories
            FOR EACH ROW WHEN (NEW.superseded_by IS NOT NULL)
            EXECUTE FUNCTION local.prevent_supersession_cycle();
    END IF;
END;
$$;

-- Index for consolidation run queries
CREATE INDEX IF NOT EXISTS idx_local_consolidation_run
    ON local.memories (consolidation_run_id)
    WHERE consolidation_run_id IS NOT NULL;
"""


SYNC_SCHEMA_SQL = """\
-- Phase 7: Multi-remote sync columns on local.memories
ALTER TABLE local.memories ADD COLUMN IF NOT EXISTS
    synced_to TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE local.memories ADD COLUMN IF NOT EXISTS
    origin TEXT NOT NULL DEFAULT 'local';

-- Routing composite index (only local, active memories need routing)
CREATE INDEX IF NOT EXISTS idx_local_routing
    ON local.memories (category, scope, project)
    WHERE status = 'active' AND origin = 'local';

-- Sync outbox queue
CREATE TABLE IF NOT EXISTS local.sync_queue (
    id SERIAL PRIMARY KEY,
    memory_id TEXT NOT NULL REFERENCES local.memories(id) ON DELETE CASCADE,
    destination TEXT NOT NULL,
    version INT NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'done', 'failed', 'dlq')),
    attempts INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 5,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_attempt TIMESTAMPTZ,
    next_retry_at TIMESTAMPTZ DEFAULT now(),
    error TEXT,
    UNIQUE (memory_id, destination, version)
);

CREATE INDEX IF NOT EXISTS idx_sync_queue_pending
    ON local.sync_queue (next_retry_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_sync_queue_dlq
    ON local.sync_queue (destination) WHERE status = 'dlq';
"""


REMOTE_SCHEMA_SQL = """\
CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS {schema};

-- Drop legacy flat table (nuke existing data on first CAS deployment)
DROP TABLE IF EXISTS {schema}.memories CASCADE;

-- CAS content store (immutable, deduped by hash)
CREATE TABLE IF NOT EXISTS {schema}.content (
    hash TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    embedding halfvec({dimensions}) NOT NULL,
    embedding_model TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_{schema}_content_embedding ON {schema}.content
    USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 200);

-- Mutable metadata references (FK to content)
CREATE TABLE IF NOT EXISTS {schema}.memory_refs (
    id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL REFERENCES {schema}.content(hash) ON DELETE RESTRICT,
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
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'superseded', 'deleted')),
    superseded_by TEXT,
    deleted_at TIMESTAMPTZ,
    synced_to TEXT[] NOT NULL DEFAULT '{{}}',
    origin TEXT NOT NULL DEFAULT 'local',
    metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_{schema}_refs_content_hash ON {schema}.memory_refs (content_hash);
CREATE INDEX IF NOT EXISTS idx_{schema}_refs_metadata ON {schema}.memory_refs USING gin (metadata jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_{schema}_refs_category ON {schema}.memory_refs (category);
CREATE INDEX IF NOT EXISTS idx_{schema}_refs_active ON {schema}.memory_refs (status) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_{schema}_refs_importance ON {schema}.memory_refs (importance_score DESC);

-- Backward-compat view for direct remote queries
CREATE OR REPLACE VIEW {schema}.active_memories AS
    SELECT r.id, c.content AS document, c.embedding,
           r.category, r.scope, r.project, r.source,
           r.importance_score, r.retrieval_count,
           r.status, r.superseded_by, r.deleted_at,
           r.synced_to, r.origin, r.metadata,
           r.created_at, r.updated_at,
           r.content_hash, c.embedding_model
    FROM {schema}.memory_refs r
    JOIN {schema}.content c ON c.hash = r.content_hash
    WHERE r.status = 'active';

-- updated_at trigger function (idempotent — may already exist on remote)
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
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_{schema}_refs_updated_at'
    ) THEN
        CREATE TRIGGER trg_{schema}_refs_updated_at
            BEFORE UPDATE ON {schema}.memory_refs
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
END;
$$;
"""


LOCAL_MIRROR_SQL = """\
CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {schema}.memories (
    id TEXT PRIMARY KEY,
    document TEXT NOT NULL,
    embedding halfvec({dimensions}),

    -- Classification columns (same as local.memories)
    category TEXT DEFAULT 'observation',
    scope TEXT DEFAULT 'global',
    project TEXT,
    source TEXT DEFAULT 'auto-extract',
    importance_score FLOAT DEFAULT 0.5,
    retrieval_count FLOAT DEFAULT 0,

    -- Lifecycle
    status TEXT DEFAULT 'active',
    superseded_by TEXT,
    deleted_at TIMESTAMPTZ,

    -- Sync metadata
    synced_to TEXT[] DEFAULT '{{}}',
    origin TEXT DEFAULT 'local',
    consolidation_run_id TEXT,

    -- Remaining flexible metadata
    metadata JSONB DEFAULT '{{}}'::jsonb,

    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Indexes (match local.memories for consistent HNSW search performance)
CREATE INDEX IF NOT EXISTS idx_{schema}_embedding ON {schema}.memories
    USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 200);
CREATE INDEX IF NOT EXISTS idx_{schema}_active ON {schema}.memories (status)
    WHERE status = 'active';

-- Active view for query convenience
CREATE OR REPLACE VIEW {schema}.active_memories AS
    SELECT * FROM {schema}.memories WHERE status = 'active';

-- Reuse the shared updated_at trigger function (created by LOCAL_SCHEMA_SQL)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_{schema}_memories_updated_at'
    ) THEN
        CREATE TRIGGER trg_{schema}_memories_updated_at
            BEFORE UPDATE ON {schema}.memories
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
END;
$$;
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


RENAME_SCHEMA_SQL = """\
-- Rename old schema names to new ones (idempotent)
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'core')
       AND NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'local')
    THEN
        ALTER SCHEMA core RENAME TO local;
    END IF;
END $$;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'vault')
       AND NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'obsidian')
    THEN
        ALTER SCHEMA vault RENAME TO obsidian;
    END IF;
END $$;
"""


def ensure_schema() -> None:
    """Create both schemas, tables, and indexes. Run migration if needed.

    Safe to call multiple times (all DDL uses IF NOT EXISTS guards).
    Called at server startup to handle first-run setup and migrations.
    Renames core→local and vault→obsidian on existing databases.
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

            # Rename old schemas if they exist (core→local, vault→obsidian)
            conn.execute(RENAME_SCHEMA_SQL)

            # Create both schemas and tables (idempotent)
            conn.execute(LOCAL_SCHEMA_SQL.format(dimensions=dims))
            conn.execute(OBSIDIAN_SCHEMA_SQL.format(dimensions=dims))
            conn.execute(LOCAL_META_SQL)
            conn.execute(RETRIEVAL_TELEMETRY_SCHEMA_SQL)

            # Phase 1 hybrid retrieval: lexical tsvector columns + channel
            # column (idempotent, additive). Runs after obsidian.documents,
            # local.memories, and local.retrieval_candidates exist.
            conn.execute(LEXICAL_SCHEMA_SQL)

            # Phase 7: sync columns + queue table (idempotent)
            conn.execute(SYNC_SCHEMA_SQL)

            # Phase 8: consolidation support (idempotent)
            conn.execute(CONSOLIDATION_SCHEMA_SQL)

            # Check if migration is needed (from legacy public.jarvis)
            old_table = conn.execute(
                "SELECT to_regclass('public.jarvis')"
            ).fetchone()
            has_old_table = old_table and old_table[0] is not None

            if has_old_table:
                # Check if migration already done
                schema_ver = conn.execute(
                    "SELECT value FROM local.meta WHERE key = 'schema_version'"
                ).fetchone()
                already_migrated = (
                    schema_ver
                    and isinstance(schema_ver[0], dict)
                    and schema_ver[0].get("version", 0) >= 3
                )

                if not already_migrated:
                    logger.info("Migrating data from public.jarvis to local/obsidian schemas...")
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


# ── local.meta CRUD ──────────────────────────────────────────────────


def get_meta(key: str) -> dict | None:
    """Get a value from local.meta by key.

    Returns the JSONB value as a dict, or None if the key doesn't exist.
    """
    result = execute_query(
        "SELECT value FROM local.meta WHERE key = %s",
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
    """Upsert a value into local.meta.

    Uses ON CONFLICT DO UPDATE for atomic upsert.
    """
    execute_write(
        """INSERT INTO local.meta (key, value)
           VALUES (%s, %s)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
        (key, json.dumps(value, default=str)),
    )


def get_all_meta() -> dict[str, dict]:
    """Get all rows from local.meta as a {key: value} dict."""
    rows = execute_query("SELECT key, value FROM local.meta", fetch="all")
    result = {}
    for row in rows:
        val = row["value"]
        if isinstance(val, str):
            val = json.loads(val)
        result[row["key"]] = dict(val) if val else {}
    return result


# ── Model consistency ────────────────────────────────────────────────


class ModelMismatchError(Exception):
    """Raised when the embedding config doesn't match what's stored in local.meta.

    Mixed embedding spaces produce garbage search results silently.
    This error forces the operator to either align the config or
    re-embed (bin/reindex_embeddings.py).
    """
    pass


# Identities recorded by retired deployments. Pre-3.5 Docker images embedded
# in-container (ONNX INT8 weights at a fixed path); those vectors measurably
# differ from the host llama.cpp space (host-inference/PROOF.md: 0.99 cosine
# agreement), so upgrades must re-embed rather than relabel.
_LEGACY_MODEL_IDENTITIES = {"/app/models/embedding"}

_REINDEX_REMEDY = (
    "Re-embed with bin/reindex_embeddings.py (Docker: docker exec --user postgres "
    "--env 'POSTGRES_URL=postgresql:///jarvis?host=/var/run/postgresql' "
    "-w /app/jarvis-core <container> python bin/reindex_embeddings.py). "
    "bin/init_db.py --force-model-record keeps the EXISTING vectors under the new "
    "identity without re-embedding (mixes embedding spaces; degrades ranking), and "
    "JARVIS_SKIP_MODEL_CHECK=1 only silences this check."
)


def check_model_consistency() -> None:
    """Verify embedding config matches what's stored in local.meta.

    First run: records the current config + schema version.
    Subsequent runs: compares model name and dimensions.
    Bypass: set JARVIS_SKIP_MODEL_CHECK=1 env var.
    """
    if os.environ.get("JARVIS_SKIP_MODEL_CHECK") == "1":
        logger.info("Model consistency check skipped (JARVIS_SKIP_MODEL_CHECK=1)")
        return

    from .config import get_contextual_embeddings_enabled, get_embedding_config
    from .embedding import get_embedding_model_identity

    emb = get_embedding_config()
    model_identity = get_embedding_model_identity(emb)
    contextual = bool(get_contextual_embeddings_enabled())
    stored = get_meta("embedding_config")

    if stored is None:
        # First run — record current config
        set_meta("embedding_config", {
            "model": model_identity,
            "dimensions": emb["dimensions"],
            "vector_type": "halfvec",
            "contextual_chunks": contextual,
        })
        set_meta("schema_version", {"version": 6})
        logger.info("Recorded embedding config in local.meta: %s (%dd)",
                     emb["model"], emb["dimensions"])
        return

    # Compare model and dimensions
    if stored.get("model") != model_identity:
        if stored.get("model") in _LEGACY_MODEL_IDENTITIES:
            raise ModelMismatchError(
                f"Database vectors were built by the retired in-container model "
                f"'{stored.get('model')}' (pre-3.5 image); config now specifies "
                f"'{model_identity}'. The two embedding spaces are close but not "
                f"identical, so search quality silently degrades without a "
                f"re-embed. {_REINDEX_REMEDY}"
            )
        raise ModelMismatchError(
            f"Embedding model mismatch: database has '{stored.get('model')}' "
            f"but config specifies '{model_identity}'. {_REINDEX_REMEDY}"
        )

    stored_dims = int(stored.get("dimensions", 0))
    if stored_dims != emb["dimensions"]:
        raise ModelMismatchError(
            f"Embedding dimensions mismatch: database has {stored_dims} "
            f"but config specifies {emb['dimensions']}. {_REINDEX_REMEDY}"
        )

    # Chunk-context augmentation is part of the embedding-space identity for
    # vault chunks: flipping the flag (or upgrading) without re-embedding
    # leaves a mixed space. WARN rather than refuse — the degradation is
    # gradual ranking skew, not garbage, and refusing would take the embedded
    # PostgreSQL down with the server, leaving no way to run the reindex.
    stored_contextual = bool(stored.get("contextual_chunks", False))
    if stored_contextual != contextual:
        logger.critical(
            "Chunk-context augmentation mismatch: vault vectors were indexed "
            "with contextual_chunks=%s but config now says %s. Vault ranking "
            "is skewed until re-embedded — run jarvis_index_vault(force=true) "
            "or bin/reindex_embeddings.py --store obsidian (or restore "
            "memory.chunking.contextual_embeddings).",
            stored_contextual, contextual,
        )
