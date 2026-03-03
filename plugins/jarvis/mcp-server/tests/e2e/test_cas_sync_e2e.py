"""E2E tests for CAS (Content-Addressable Storage) remote sync schema.

Tests cover: CAS DDL correctness, content write-once semantics,
cross-ref dedup, FK restrict, active_memories view, old flat table
drop, and push→pull roundtrip.
"""

import hashlib
import os

import psycopg
import pytest

from tools.schema import REMOTE_SCHEMA_SQL

E2E_POSTGRES_URL = os.environ.get("E2E_POSTGRES_URL", "")

pytestmark = pytest.mark.skipif(
    not E2E_POSTGRES_URL,
    reason="E2E_POSTGRES_URL not set — skipping e2e tests",
)

# Test schema name (isolated from local/obsidian schemas)
CAS_SCHEMA = "cas_test"


@pytest.fixture(autouse=True)
def cas_schema(e2e_config):
    """Create CAS schema for each test, drop on teardown."""
    db_url = e2e_config["db_url"]

    conn = psycopg.connect(db_url, autocommit=True)
    try:
        # Drop and recreate for a clean slate
        conn.execute(f"DROP SCHEMA IF EXISTS {CAS_SCHEMA} CASCADE")
        ddl = REMOTE_SCHEMA_SQL.format(schema=CAS_SCHEMA, dimensions=384)
        conn.execute(ddl)
    finally:
        conn.close()

    yield db_url

    conn = psycopg.connect(db_url, autocommit=True)
    try:
        conn.execute(f"DROP SCHEMA IF EXISTS {CAS_SCHEMA} CASCADE")
    finally:
        conn.close()


def _compute_hash(document: str, model: str = "mock-model") -> str:
    """Mirror of sync_worker._compute_content_hash for test assertions."""
    return hashlib.sha256(
        document.encode("utf-8") + b"\x00" + model.encode("utf-8")
    ).hexdigest()


def _insert_content(conn, doc: str, embedding_dims: int = 384,
                    model: str = "mock-model"):
    """Insert a content row and return its hash."""
    h = _compute_hash(doc, model)
    embedding = [0.1] * embedding_dims
    conn.execute(
        f"""INSERT INTO {CAS_SCHEMA}.content (hash, content, embedding, embedding_model)
            VALUES (%s, %s, %s::halfvec, %s)
            ON CONFLICT (hash) DO NOTHING""",
        (h, doc, str(embedding), model),
    )
    return h


def _insert_ref(conn, ref_id: str, content_hash: str, **kwargs):
    """Insert a memory_refs row."""
    defaults = {
        "category": "observation",
        "scope": "global",
        "project": None,
        "source": "auto-extract",
        "importance_score": 0.5,
        "retrieval_count": 0.0,
        "status": "active",
        "superseded_by": None,
        "deleted_at": None,
        "synced_to": [],
        "origin": "local",
        "metadata": "{}",
    }
    defaults.update(kwargs)
    conn.execute(
        f"""INSERT INTO {CAS_SCHEMA}.memory_refs
            (id, content_hash, category, scope, project, source,
             importance_score, retrieval_count, status, superseded_by,
             deleted_at, synced_to, origin, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)""",
        (ref_id, content_hash,
         defaults["category"], defaults["scope"], defaults["project"],
         defaults["source"], defaults["importance_score"],
         defaults["retrieval_count"], defaults["status"],
         defaults["superseded_by"], defaults["deleted_at"],
         defaults["synced_to"], defaults["origin"], defaults["metadata"]),
    )


class TestCASSchemaCreation:
    """Verify CAS DDL creates expected structures."""

    def test_content_table_exists(self, cas_schema):
        conn = psycopg.connect(cas_schema)
        try:
            row = conn.execute(
                """SELECT COUNT(*) FROM information_schema.tables
                   WHERE table_schema = %s AND table_name = 'content'""",
                (CAS_SCHEMA,),
            ).fetchone()
            assert row[0] == 1
        finally:
            conn.close()

    def test_memory_refs_table_exists(self, cas_schema):
        conn = psycopg.connect(cas_schema)
        try:
            row = conn.execute(
                """SELECT COUNT(*) FROM information_schema.tables
                   WHERE table_schema = %s AND table_name = 'memory_refs'""",
                (CAS_SCHEMA,),
            ).fetchone()
            assert row[0] == 1
        finally:
            conn.close()

    def test_old_flat_table_dropped(self, cas_schema):
        """The legacy {schema}.memories table should not exist."""
        conn = psycopg.connect(cas_schema)
        try:
            row = conn.execute(
                """SELECT COUNT(*) FROM information_schema.tables
                   WHERE table_schema = %s AND table_name = 'memories'""",
                (CAS_SCHEMA,),
            ).fetchone()
            assert row[0] == 0
        finally:
            conn.close()

    def test_hnsw_index_exists(self, cas_schema):
        """HNSW index should exist on content.embedding."""
        conn = psycopg.connect(cas_schema)
        try:
            row = conn.execute(
                """SELECT COUNT(*) FROM pg_indexes
                   WHERE schemaname = %s
                   AND indexname = %s""",
                (CAS_SCHEMA, f"idx_{CAS_SCHEMA}_content_embedding"),
            ).fetchone()
            assert row[0] == 1
        finally:
            conn.close()

    def test_content_hash_index_exists(self, cas_schema):
        """Index should exist on memory_refs.content_hash."""
        conn = psycopg.connect(cas_schema)
        try:
            row = conn.execute(
                """SELECT COUNT(*) FROM pg_indexes
                   WHERE schemaname = %s
                   AND indexname = %s""",
                (CAS_SCHEMA, f"idx_{CAS_SCHEMA}_refs_content_hash"),
            ).fetchone()
            assert row[0] == 1
        finally:
            conn.close()


class TestCASContentSemantics:
    """Verify CAS content table write-once and dedup behavior."""

    def test_content_write_once(self, cas_schema):
        """INSERT same hash twice — second is DO NOTHING."""
        conn = psycopg.connect(cas_schema, autocommit=True)
        try:
            h = _insert_content(conn, "hello world")
            # Insert again with same content
            _insert_content(conn, "hello world")

            row = conn.execute(
                f"SELECT COUNT(*) FROM {CAS_SCHEMA}.content WHERE hash = %s",
                (h,),
            ).fetchone()
            assert row[0] == 1
        finally:
            conn.close()

    def test_content_dedup_two_refs(self, cas_schema):
        """Two refs can point to the same content row."""
        conn = psycopg.connect(cas_schema, autocommit=True)
        try:
            h = _insert_content(conn, "shared doc")
            _insert_ref(conn, "obs::1", h)
            _insert_ref(conn, "obs::2", h)

            # 1 content row, 2 ref rows
            content_count = conn.execute(
                f"SELECT COUNT(*) FROM {CAS_SCHEMA}.content"
            ).fetchone()[0]
            ref_count = conn.execute(
                f"SELECT COUNT(*) FROM {CAS_SCHEMA}.memory_refs"
            ).fetchone()[0]
            assert content_count == 1
            assert ref_count == 2
        finally:
            conn.close()

    def test_fk_restrict_blocks_content_delete(self, cas_schema):
        """DELETE content with existing ref raises FK error."""
        conn = psycopg.connect(cas_schema, autocommit=True)
        try:
            h = _insert_content(conn, "protected doc")
            _insert_ref(conn, "obs::fk-test", h)

            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                conn.execute(
                    f"DELETE FROM {CAS_SCHEMA}.content WHERE hash = %s", (h,)
                )
        finally:
            conn.close()

    def test_ref_update_preserves_content(self, cas_schema):
        """Updating ref metadata doesn't touch the content row."""
        conn = psycopg.connect(cas_schema, autocommit=True)
        try:
            h = _insert_content(conn, "immutable doc")
            _insert_ref(conn, "obs::update-test", h)

            # Update the ref
            conn.execute(
                f"""UPDATE {CAS_SCHEMA}.memory_refs
                    SET importance_score = 0.9
                    WHERE id = 'obs::update-test'"""
            )

            # Content unchanged
            row = conn.execute(
                f"SELECT content FROM {CAS_SCHEMA}.content WHERE hash = %s",
                (h,),
            ).fetchone()
            assert row[0] == "immutable doc"
        finally:
            conn.close()


class TestCASActiveView:
    """Verify the active_memories view."""

    def test_view_joins_correctly(self, cas_schema):
        """View returns expected columns from both tables."""
        conn = psycopg.connect(cas_schema, autocommit=True)
        try:
            h = _insert_content(conn, "view test doc")
            _insert_ref(conn, "obs::view-test", h)

            row = conn.execute(
                f"""SELECT id, document, embedding, category, content_hash,
                           embedding_model
                    FROM {CAS_SCHEMA}.active_memories
                    WHERE id = 'obs::view-test'"""
            ).fetchone()
            assert row is not None
            assert row[0] == "obs::view-test"
            assert row[1] == "view test doc"
            assert row[4] == h
            assert row[5] == "mock-model"
        finally:
            conn.close()

    def test_view_excludes_non_active(self, cas_schema):
        """View only shows active refs."""
        conn = psycopg.connect(cas_schema, autocommit=True)
        try:
            h = _insert_content(conn, "status test doc")
            _insert_ref(conn, "obs::active-1", h, status="active")
            _insert_ref(conn, "obs::deleted-1", h, status="deleted")

            rows = conn.execute(
                f"SELECT id FROM {CAS_SCHEMA}.active_memories"
            ).fetchall()
            ids = [r[0] for r in rows]
            assert "obs::active-1" in ids
            assert "obs::deleted-1" not in ids
        finally:
            conn.close()


class TestCASPushPullRoundtrip:
    """Test push→pull roundtrip through CAS tables."""

    def test_push_pull_roundtrip(self, cas_schema, e2e_config):
        """Push CAS content+ref, read back via JOIN, verify flat row."""
        conn = psycopg.connect(cas_schema, autocommit=True)
        try:
            doc = "roundtrip test document"
            h = _insert_content(conn, doc)
            _insert_ref(
                conn, "obs::roundtrip-1", h,
                category="learning",
                importance_score=0.8,
                source="manual",
            )

            # Simulate pull: read from CAS tables (same query as sync_pull)
            row = conn.execute(
                f"""SELECT r.id, c.content AS document, c.embedding,
                           r.category, r.scope, r.project,
                           r.source, r.importance_score, r.retrieval_count,
                           r.status, r.superseded_by, r.metadata,
                           r.created_at, r.updated_at
                    FROM {CAS_SCHEMA}.memory_refs r
                    JOIN {CAS_SCHEMA}.content c ON c.hash = r.content_hash
                    WHERE r.id = 'obs::roundtrip-1'"""
            ).fetchone()

            assert row is not None
            assert row[0] == "obs::roundtrip-1"
            assert row[1] == doc
            assert row[3] == "learning"
            assert row[7] == 0.8
            assert row[6] == "manual"
        finally:
            conn.close()
