"""E2E tests for Phase 8: consolidation schema, supersession DAG, and decay integration.

Tests run against real PostgreSQL + pgvector to verify:
- consolidation_run_id column works
- Supersession cycle prevention trigger fires
- Self-supersession constraint enforced
- Transactional consolidation (INSERT + UPDATE atomic)
- Undo consolidation restores originals
"""

import psycopg
import pytest

from tests.conftest import MockEmbeddingService


# ── Schema DDL Tests ─────────────────────────────────────────────────


class TestConsolidationDDL:
    """Verify Phase 8 schema changes are applied correctly."""

    def test_consolidation_run_id_column_exists(self, e2e_config):
        """local.memories has consolidation_run_id column."""
        from tools.schema import execute_query

        rows = execute_query(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema = 'local' AND table_name = 'memories'
               AND column_name = 'consolidation_run_id'"""
        )
        assert len(rows) == 1

    def test_consolidation_run_id_index_exists(self, e2e_config):
        """Partial index on consolidation_run_id exists."""
        from tools.schema import execute_query

        rows = execute_query(
            """SELECT indexname FROM pg_indexes
               WHERE tablename = 'memories' AND schemaname = 'local'
               AND indexname = 'idx_local_consolidation_run'"""
        )
        assert len(rows) == 1


class TestSupersessionConstraints:
    """Self-supersession and cycle prevention."""

    def test_self_supersession_rejected(self, e2e_config):
        """Cannot set superseded_by = own ID."""
        from tools.schema import _get_pool

        emb = MockEmbeddingService(384)
        pool = _get_pool()

        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO local.memories (id, document, embedding, status)
                       VALUES ('self-ref-test', 'test doc', %s::halfvec, 'active')""",
                    (emb.encode("test"),),
                )
                conn.commit()

        with pytest.raises(Exception, match="[Cc]ycle"):
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE local.memories
                           SET status = 'superseded', superseded_by = 'self-ref-test'
                           WHERE id = 'self-ref-test'"""
                    )
                    conn.commit()

    def test_direct_cycle_rejected(self, e2e_config):
        """A→B→A cycle is rejected by trigger."""
        from tools.schema import _get_pool

        emb = MockEmbeddingService(384)
        pool = _get_pool()

        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO local.memories (id, document, embedding, status)
                       VALUES ('cycle-a', 'doc a', %s::halfvec, 'active'),
                              ('cycle-b', 'doc b', %s::halfvec, 'active')""",
                    (emb.encode("a"), emb.encode("b")),
                )
                # A superseded by B
                cur.execute(
                    """UPDATE local.memories
                       SET status = 'superseded', superseded_by = 'cycle-b'
                       WHERE id = 'cycle-a'"""
                )
                conn.commit()

        # Now try B superseded by A (should fail — cycle)
        with pytest.raises(Exception, match="[Cc]ycle"):
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE local.memories
                           SET status = 'superseded', superseded_by = 'cycle-a'
                           WHERE id = 'cycle-b'"""
                    )
                    conn.commit()

    def test_valid_supersession_allowed(self, e2e_config):
        """Non-cyclic supersession works fine."""
        from tools.schema import _get_pool

        emb = MockEmbeddingService(384)
        pool = _get_pool()

        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO local.memories (id, document, embedding, status)
                       VALUES ('valid-old', 'old doc', %s::halfvec, 'active'),
                              ('valid-new', 'new doc', %s::halfvec, 'active')""",
                    (emb.encode("old"), emb.encode("new")),
                )
                cur.execute(
                    """UPDATE local.memories
                       SET status = 'superseded', superseded_by = 'valid-new'
                       WHERE id = 'valid-old'"""
                )
                conn.commit()

        # Verify it worked
        from tools.schema import execute_query
        row = execute_query(
            "SELECT status, superseded_by FROM local.memories WHERE id = 'valid-old'",
            fetch="one",
        )
        assert row["status"] == "superseded"
        assert row["superseded_by"] == "valid-new"


class TestConsolidationRunTracking:
    """Consolidation run_id for batch tracking and undo."""

    def test_consolidation_run_id_stored(self, e2e_config):
        """consolidation_run_id is stored and queryable."""
        from tools.schema import _get_pool, execute_query

        emb = MockEmbeddingService(384)
        pool = _get_pool()

        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO local.memories
                       (id, document, embedding, consolidation_run_id)
                       VALUES ('consol-1', 'doc', %s::halfvec, 'run-abc')""",
                    (emb.encode("doc"),),
                )
                conn.commit()

        row = execute_query(
            "SELECT consolidation_run_id FROM local.memories WHERE id = 'consol-1'",
            fetch="one",
        )
        assert row["consolidation_run_id"] == "run-abc"

    def test_query_by_run_id(self, e2e_config):
        """Can query all memories in a consolidation run."""
        from tools.schema import _get_pool, execute_query

        emb = MockEmbeddingService(384)
        pool = _get_pool()

        with pool.connection() as conn:
            with conn.cursor() as cur:
                for i in range(3):
                    cur.execute(
                        """INSERT INTO local.memories
                           (id, document, embedding, consolidation_run_id)
                           VALUES (%s, %s, %s::halfvec, 'run-xyz')""",
                        (f"batch-{i}", f"doc {i}", emb.encode(f"doc {i}")),
                    )
                conn.commit()

        rows = execute_query(
            "SELECT id FROM local.memories WHERE consolidation_run_id = 'run-xyz'"
        )
        assert len(rows) == 3


class TestTransactionalConsolidation:
    """Verify the transactional apply and undo of consolidation."""

    def test_apply_and_undo_roundtrip(self, e2e_config):
        """Apply consolidation → originals superseded → undo → originals restored."""
        from tools.schema import _get_pool, execute_query

        emb = MockEmbeddingService(384)
        pool = _get_pool()

        # Create 3 original memories
        with pool.connection() as conn:
            with conn.cursor() as cur:
                for i in range(3):
                    cur.execute(
                        """INSERT INTO local.memories
                           (id, document, embedding, importance_score, status)
                           VALUES (%s, %s, %s::halfvec, 0.7, 'active')""",
                        (f"orig-{i}", f"original {i}", emb.encode(f"original {i}")),
                    )
                conn.commit()

        # Simulate consolidation: insert consolidated + supersede originals
        run_id = "test-roundtrip-run"
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # Insert consolidated
                cur.execute(
                    """INSERT INTO local.memories
                       (id, document, embedding, category, importance_score,
                        source, status, consolidation_run_id, metadata)
                       VALUES ('consolidated-rt', 'consolidated summary', %s::halfvec,
                               'summary', 0.8, 'consolidation', 'active', %s,
                               '{"source_ids": ["orig-0", "orig-1", "orig-2"]}'::jsonb)""",
                    (emb.encode("consolidated"), run_id),
                )
                # Supersede originals
                cur.execute(
                    """UPDATE local.memories
                       SET status = 'superseded',
                           superseded_by = 'consolidated-rt',
                           consolidation_run_id = %s
                       WHERE id IN ('orig-0', 'orig-1', 'orig-2')""",
                    (run_id,),
                )
                conn.commit()

        # Verify originals are superseded
        active = execute_query(
            "SELECT id FROM local.active_memories WHERE id LIKE 'orig-%%'"
        )
        assert len(active) == 0

        # Verify consolidated is active
        consol = execute_query(
            "SELECT id FROM local.active_memories WHERE id = 'consolidated-rt'",
            fetch="one",
        )
        assert consol is not None

        # Now undo
        from tools.consolidation import undo_consolidation
        result = undo_consolidation(run_id)
        assert result["restored_count"] == 3
        assert result["deleted_count"] == 1

        # Verify originals are back
        active_after = execute_query(
            "SELECT id FROM local.active_memories WHERE id LIKE 'orig-%%'"
        )
        assert len(active_after) == 3

        # Verify consolidated is gone from active view
        consol_after = execute_query(
            "SELECT id FROM local.active_memories WHERE id = 'consolidated-rt'",
            fetch="one",
        )
        assert consol_after is None


class TestActiveViewWithSupersession:
    """local.active_memories view correctly excludes superseded entries."""

    def test_superseded_excluded_from_active(self, e2e_config):
        from tools.schema import _get_pool, execute_query

        emb = MockEmbeddingService(384)
        pool = _get_pool()

        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO local.memories (id, document, embedding, status)
                       VALUES ('act-1', 'active doc', %s::halfvec, 'active')""",
                    (emb.encode("active"),),
                )
                cur.execute(
                    """INSERT INTO local.memories (id, document, embedding, status, superseded_by)
                       VALUES ('sup-1', 'superseded doc', %s::halfvec, 'superseded', 'act-1')""",
                    (emb.encode("superseded"),),
                )
                conn.commit()

        active = execute_query("SELECT id FROM local.active_memories")
        active_ids = {r["id"] for r in active}
        assert "act-1" in active_ids
        assert "sup-1" not in active_ids
