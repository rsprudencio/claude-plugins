"""Schema migration e2e tests — real PostgreSQL.

Verifies migration from the legacy public.jarvis single table to the
dual-schema architecture (local.memories + obsidian.documents).
Tests data fidelity, column promotion from JSONB, and edge cases.
"""

import json
import os

import psycopg
import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.environ.get("E2E_POSTGRES_URL"),
        reason="E2E_POSTGRES_URL not set",
    ),
]


# ── Legacy schema DDL (from master branch) ────────────────────────────

LEGACY_SCHEMA_SQL = """\
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS jarvis (
    id TEXT PRIMARY KEY,
    document TEXT NOT NULL,
    embedding halfvec({dimensions}) NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

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

LEGACY_META_SQL = """\
CREATE TABLE IF NOT EXISTS jarvis_meta (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def migration_db(e2e_config):
    """Set up a legacy public.jarvis table and seed test data for migration.

    Returns dict with db_url and seeded IDs.
    """
    db_url = e2e_config["db_url"]
    conn = psycopg.connect(db_url, autocommit=True)

    # Create legacy schema
    conn.execute(LEGACY_SCHEMA_SQL.format(dimensions=384))
    conn.execute(LEGACY_META_SQL)

    # Seed legacy data
    from tools.embedding import get_embedding_service
    emb = get_embedding_service()

    # 1. Vault document
    vault_emb = emb.encode("OAuth authentication guide")
    vault_meta = json.dumps({
        "type": "vault",
        "tier": "file",
        "namespace": "vault::",
        "parent_file": "notes/auth-guide.md",
        "directory": "notes",
        "vault_type": "document",
        "title": "Auth Guide",
        "chunk_index": "0",
        "chunk_total": "1",
        "chunk_heading": "",
        "importance_score": "0.8",
        "source": "vault-index",
    })
    conn.execute(
        "INSERT INTO jarvis (id, document, embedding, metadata) "
        "VALUES (%s, %s, %s::halfvec, %s::jsonb)",
        ("vault::notes/auth-guide.md", "OAuth authentication guide",
         str(vault_emb), vault_meta),
    )

    # 2. Observation (tier2)
    obs_emb = emb.encode("User prefers dark mode")
    obs_meta = json.dumps({
        "type": "observation",
        "tier": "chromadb",
        "namespace": "obs::",
        "source": "auto-extract",
        "importance_score": "0.7",
        "retrieval_count": "3",
        "status": "active",
        "tags": ["preference", "ui"],
        "session_id": "sess-001",
        "promoted": "false",
    })
    conn.execute(
        "INSERT INTO jarvis (id, document, embedding, metadata) "
        "VALUES (%s, %s, %s::halfvec, %s::jsonb)",
        ("obs::abc123", "User prefers dark mode",
         str(obs_emb), obs_meta),
    )

    # 3. Strategic memory
    mem_emb = emb.encode("Always write tests first")
    mem_meta = json.dumps({
        "type": "memory",
        "tier": "file",
        "namespace": "memory::",
        "scope": "global",
        "source": "user",
        "importance_score": "0.9",
        "retrieval_count": "5",
    })
    conn.execute(
        "INSERT INTO jarvis (id, document, embedding, metadata) "
        "VALUES (%s, %s, %s::halfvec, %s::jsonb)",
        ("memory::global::test-principle", "Always write tests first",
         str(mem_emb), mem_meta),
    )

    # 4. Superseded observation
    sup_emb = emb.encode("Old observation")
    sup_meta = json.dumps({
        "type": "observation",
        "tier": "chromadb",
        "namespace": "obs::",
        "source": "auto-extract",
        "importance_score": "0.5",
        "retrieval_count": "1",
        "status": "superseded",
        "superseded_by": "obs::newer",
    })
    conn.execute(
        "INSERT INTO jarvis (id, document, embedding, metadata) "
        "VALUES (%s, %s, %s::halfvec, %s::jsonb)",
        ("obs::superseded-old", "Old observation",
         str(sup_emb), sup_meta),
    )

    # 5. Legacy meta
    conn.execute(
        "INSERT INTO jarvis_meta (key, value) VALUES (%s, %s::jsonb)",
        ("schema_version", json.dumps({"version": 2})),
    )
    conn.execute(
        "INSERT INTO jarvis_meta (key, value) VALUES (%s, %s::jsonb)",
        ("embedding_model", json.dumps({"model": "test-model", "dimensions": 384})),
    )

    conn.close()

    yield {
        "db_url": db_url,
        "vault_id": "vault::notes/auth-guide.md",
        "obs_id": "obs::abc123",
        "mem_id": "memory::global::test-principle",
        "sup_id": "obs::superseded-old",
    }

    # Cleanup: drop legacy tables
    try:
        conn = psycopg.connect(db_url, autocommit=True)
        conn.execute("DROP TABLE IF EXISTS jarvis CASCADE")
        conn.execute("DROP TABLE IF EXISTS jarvis_meta CASCADE")
        conn.close()
    except Exception:
        pass


# ── Tests ─────────────────────────────────────────────────────────────


def test_migration_moves_vault_to_vault_schema(migration_db):
    """Vault rows migrate to obsidian.documents with proper columns."""
    from tools.schema import MIGRATION_SQL

    db_url = migration_db["db_url"]
    conn = psycopg.connect(db_url, autocommit=True)
    conn.execute(MIGRATION_SQL.format(dimensions=384))

    # Verify vault document migrated
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, parent_file, directory, vault_type, title, "
            "chunk_index, chunk_total, importance_score "
            "FROM obsidian.documents WHERE id = %s",
            (migration_db["vault_id"],),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[1] == "notes/auth-guide.md"  # parent_file
        assert row[2] == "notes"                 # directory
        assert row[3] == "document"              # vault_type
        assert row[4] == "Auth Guide"            # title
        assert row[5] == 0                       # chunk_index
        assert row[6] == 1                       # chunk_total
        assert abs(row[7] - 0.8) < 0.01         # importance_score

    conn.close()


def test_migration_moves_observations_to_core(migration_db):
    """Observation rows migrate to local.memories with proper columns."""
    from tools.schema import MIGRATION_SQL

    db_url = migration_db["db_url"]
    conn = psycopg.connect(db_url, autocommit=True)
    conn.execute(MIGRATION_SQL.format(dimensions=384))

    with conn.cursor() as cur:
        cur.execute(
            "SELECT category, scope, source, importance_score, "
            "retrieval_count, status "
            "FROM local.memories WHERE id = %s",
            (migration_db["obs_id"],),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "observation"           # category
        assert row[1] == "global"                # scope
        assert row[2] == "auto-extract"          # source
        assert abs(row[3] - 0.7) < 0.01         # importance_score
        assert row[4] == 3.0                     # retrieval_count
        assert row[5] == "active"                # status

    conn.close()


def test_migration_memory_prefix_gets_category_memory(migration_db):
    """memory:: prefixed IDs get category='memory'."""
    from tools.schema import MIGRATION_SQL

    db_url = migration_db["db_url"]
    conn = psycopg.connect(db_url, autocommit=True)
    conn.execute(MIGRATION_SQL.format(dimensions=384))

    with conn.cursor() as cur:
        cur.execute(
            "SELECT category, scope FROM local.memories WHERE id = %s",
            (migration_db["mem_id"],),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "memory"
        assert row[1] == "global"

    conn.close()


def test_migration_preserves_superseded_status(migration_db):
    """Superseded status and superseded_by are preserved as columns."""
    from tools.schema import MIGRATION_SQL

    db_url = migration_db["db_url"]
    conn = psycopg.connect(db_url, autocommit=True)
    conn.execute(MIGRATION_SQL.format(dimensions=384))

    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, superseded_by FROM local.memories WHERE id = %s",
            (migration_db["sup_id"],),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "superseded"
        assert row[1] == "obs::newer"

    conn.close()


def test_migration_strips_promoted_fields_from_metadata(migration_db):
    """Promoted fields are removed from JSONB metadata during migration."""
    from tools.schema import MIGRATION_SQL

    db_url = migration_db["db_url"]
    conn = psycopg.connect(db_url, autocommit=True)
    conn.execute(MIGRATION_SQL.format(dimensions=384))

    with conn.cursor() as cur:
        cur.execute(
            "SELECT metadata FROM local.memories WHERE id = %s",
            (migration_db["obs_id"],),
        )
        row = cur.fetchone()
        meta = row[0] if isinstance(row[0], dict) else json.loads(row[0])

        # These should have been stripped during migration
        assert "type" not in meta
        assert "tier" not in meta
        assert "namespace" not in meta
        assert "scope" not in meta
        assert "source" not in meta
        assert "importance_score" not in meta
        assert "retrieval_count" not in meta
        assert "status" not in meta
        assert "promoted" not in meta

        # Remaining metadata should be preserved
        assert "session_id" in meta
        assert "tags" in meta

    conn.close()


def test_migration_meta_table(migration_db):
    """jarvis_meta data migrates to local.meta."""
    from tools.schema import MIGRATION_SQL

    db_url = migration_db["db_url"]
    conn = psycopg.connect(db_url, autocommit=True)
    conn.execute(MIGRATION_SQL.format(dimensions=384))

    with conn.cursor() as cur:
        # Schema version should be bumped to 3
        cur.execute(
            "SELECT value FROM local.meta WHERE key = 'schema_version'"
        )
        row = cur.fetchone()
        sv = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        assert sv["version"] == 3

        # Original embedding_model should be preserved
        cur.execute(
            "SELECT value FROM local.meta WHERE key = 'embedding_model'"
        )
        row = cur.fetchone()
        assert row is not None

    conn.close()


def test_migration_row_count_matches(migration_db):
    """Total migrated rows equal original row count."""
    from tools.schema import MIGRATION_SQL

    db_url = migration_db["db_url"]
    conn = psycopg.connect(db_url, autocommit=True)
    conn.execute(MIGRATION_SQL.format(dimensions=384))

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jarvis")
        original = cur.fetchone()[0]

        cur.execute("SELECT count(*) FROM local.memories")
        core_count = cur.fetchone()[0]

        cur.execute("SELECT count(*) FROM obsidian.documents")
        vault_count = cur.fetchone()[0]

        assert core_count + vault_count == original

    conn.close()


def test_migration_is_idempotent(migration_db):
    """Running migration twice doesn't duplicate rows (ON CONFLICT DO NOTHING)."""
    from tools.schema import MIGRATION_SQL

    db_url = migration_db["db_url"]
    conn = psycopg.connect(db_url, autocommit=True)

    # Run twice
    conn.execute(MIGRATION_SQL.format(dimensions=384))
    conn.execute(MIGRATION_SQL.format(dimensions=384))

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM local.memories")
        core_count = cur.fetchone()[0]

        cur.execute("SELECT count(*) FROM obsidian.documents")
        vault_count = cur.fetchone()[0]

        # Should be same as one run (3 memories + 1 vault doc)
        assert core_count == 3  # obs, memory, superseded-obs
        assert vault_count == 1

    conn.close()
