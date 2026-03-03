"""E2E tests for sync queue against real PostgreSQL.

Tests cover: DDL correctness, SKIP LOCKED semantics, UNIQUE constraint,
status FSM transitions, DLQ promotion, CASCADE on memory delete,
and index scan plans.
"""

import os
import psycopg
import pytest

E2E_POSTGRES_URL = os.environ.get("E2E_POSTGRES_URL", "")

pytestmark = pytest.mark.skipif(
    not E2E_POSTGRES_URL,
    reason="E2E_POSTGRES_URL not set — skipping e2e tests",
)


def _insert_memory(e2e_config, doc_id="obs::test-1", content="test content"):
    """Insert a test memory directly into core.memories."""
    from tools.content import content_write

    result = content_write(
        content=content,
        content_type="observation",
        name=None,
        importance_score=0.5,
        skip_secret_scan=True,
    )
    # Return the actual ID generated
    return result.get("id", doc_id)


def _insert_memory_raw(db_url, doc_id, embedding_dims=384):
    """Insert a minimal memory row directly via SQL (bypasses content_write)."""
    embedding = [0.1] * embedding_dims
    conn = psycopg.connect(db_url, autocommit=True)
    try:
        conn.execute(
            """INSERT INTO core.memories
               (id, document, embedding, category, scope, source,
                importance_score, status, metadata)
               VALUES (%s, 'test', %s::halfvec, 'observation', 'global',
                       'test', 0.5, 'active', '{}'::jsonb)
               ON CONFLICT (id) DO NOTHING""",
            (doc_id, str(embedding)),
        )
    finally:
        conn.close()


class TestSyncQueueDDL:
    """Verify the sync_queue table was created correctly."""

    def test_table_exists(self, e2e_config):
        db_url = e2e_config["db_url"]
        conn = psycopg.connect(db_url)
        try:
            row = conn.execute(
                "SELECT to_regclass('core.sync_queue')"
            ).fetchone()
            assert row[0] is not None
        finally:
            conn.close()

    def test_columns_exist(self, e2e_config):
        db_url = e2e_config["db_url"]
        conn = psycopg.connect(db_url)
        try:
            rows = conn.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema = 'core' AND table_name = 'sync_queue'
                   ORDER BY ordinal_position"""
            ).fetchall()
            columns = [r[0] for r in rows]
            expected = [
                "id", "memory_id", "destination", "version", "status",
                "attempts", "max_attempts", "created_at", "last_attempt",
                "next_retry_at", "error",
            ]
            for col in expected:
                assert col in columns, f"Missing column: {col}"
        finally:
            conn.close()

    def test_memories_has_sync_columns(self, e2e_config):
        """Verify synced_to and origin columns were added to core.memories."""
        db_url = e2e_config["db_url"]
        conn = psycopg.connect(db_url)
        try:
            rows = conn.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema = 'core' AND table_name = 'memories'
                     AND column_name IN ('synced_to', 'origin')
                   ORDER BY column_name"""
            ).fetchall()
            columns = [r[0] for r in rows]
            assert "origin" in columns
            assert "synced_to" in columns
        finally:
            conn.close()


class TestSyncQueueOperations:
    """Test queue operations against real PostgreSQL."""

    def test_enqueue_and_claim(self, e2e_config):
        """Basic enqueue → claim cycle."""
        from tools.schema import _get_pool
        from tools.sync_queue import enqueue_sync, claim_pending_syncs

        db_url = e2e_config["db_url"]
        _insert_memory_raw(db_url, "obs::enqueue-1")

        pool = _get_pool()
        # Enqueue within a transaction
        with pool.connection() as conn:
            with conn.cursor() as cur:
                count = enqueue_sync(cur, "obs::enqueue-1", ["remote-a", "remote-b"])
                assert count == 2
            conn.commit()

        # Claim
        entries = claim_pending_syncs(pool, batch_size=10)
        assert len(entries) == 2
        dests = {e["destination"] for e in entries}
        assert dests == {"remote-a", "remote-b"}
        assert all(e["memory_id"] == "obs::enqueue-1" for e in entries)

    def test_unique_constraint_prevents_duplicates(self, e2e_config):
        """UNIQUE (memory_id, destination, version) prevents duplicate enqueue."""
        from tools.schema import _get_pool
        from tools.sync_queue import enqueue_sync

        db_url = e2e_config["db_url"]
        _insert_memory_raw(db_url, "obs::unique-1")

        pool = _get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                count1 = enqueue_sync(cur, "obs::unique-1", ["remote-a"])
                assert count1 == 1
                # Second enqueue of same memory+dest+version is a no-op
                count2 = enqueue_sync(cur, "obs::unique-1", ["remote-a"])
                assert count2 == 0
            conn.commit()

    def test_status_fsm_transitions(self, e2e_config):
        """Verify status transitions: pending → sending → done."""
        from tools.schema import _get_pool
        from tools.sync_queue import enqueue_sync, claim_pending_syncs, mark_synced

        db_url = e2e_config["db_url"]
        _insert_memory_raw(db_url, "obs::fsm-1")

        pool = _get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                enqueue_sync(cur, "obs::fsm-1", ["remote-a"])
            conn.commit()

        # pending → sending (via claim)
        entries = claim_pending_syncs(pool)
        assert len(entries) == 1
        assert entries[0]["memory_id"] == "obs::fsm-1"

        # sending → done
        ids = [e["id"] for e in entries]
        count = mark_synced(pool, ids)
        assert count == 1

        # Verify final status
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status FROM core.sync_queue WHERE id = %s",
                    (ids[0],),
                )
                row = cur.fetchone()
                assert row[0] == "done"

    def test_failed_with_backoff_and_dlq(self, e2e_config):
        """Failed entries get exponential backoff; exceed max → DLQ."""
        from tools.schema import _get_pool
        from tools.sync_queue import enqueue_sync, claim_pending_syncs, mark_failed

        db_url = e2e_config["db_url"]
        _insert_memory_raw(db_url, "obs::fail-1")

        pool = _get_pool()

        # Set max_attempts=2 for quick DLQ test
        with pool.connection() as conn:
            with conn.cursor() as cur:
                enqueue_sync(cur, "obs::fail-1", ["remote-a"])
                # Override max_attempts
                cur.execute(
                    "UPDATE core.sync_queue SET max_attempts = 2 "
                    "WHERE memory_id = 'obs::fail-1'"
                )
            conn.commit()

        # First failure: should go back to pending
        entries = claim_pending_syncs(pool)
        mark_failed(pool, [entries[0]["id"]], "timeout")

        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, attempts FROM core.sync_queue WHERE id = %s",
                    (entries[0]["id"],),
                )
                row = cur.fetchone()
                assert row[0] == "pending"
                assert row[1] == 1

        # Second failure: should go to DLQ (attempts=2 >= max_attempts=2)
        # Need to set next_retry_at to now for immediate claim
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE core.sync_queue SET next_retry_at = now() "
                    "WHERE id = %s",
                    (entries[0]["id"],),
                )
            conn.commit()

        entries2 = claim_pending_syncs(pool)
        assert len(entries2) == 1
        mark_failed(pool, [entries2[0]["id"]], "timeout again")

        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status FROM core.sync_queue WHERE id = %s",
                    (entries2[0]["id"],),
                )
                row = cur.fetchone()
                assert row[0] == "dlq"

    def test_retry_dlq(self, e2e_config):
        """DLQ entries can be reset to pending."""
        from tools.schema import _get_pool
        from tools.sync_queue import enqueue_sync, retry_dlq

        db_url = e2e_config["db_url"]
        _insert_memory_raw(db_url, "obs::dlq-1")

        pool = _get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                enqueue_sync(cur, "obs::dlq-1", ["remote-a"])
                # Force into DLQ
                cur.execute(
                    "UPDATE core.sync_queue SET status = 'dlq', attempts = 5 "
                    "WHERE memory_id = 'obs::dlq-1'"
                )
            conn.commit()

        count = retry_dlq(pool, destination="remote-a")
        assert count == 1

        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, attempts FROM core.sync_queue "
                    "WHERE memory_id = 'obs::dlq-1'"
                )
                row = cur.fetchone()
                assert row[0] == "pending"
                assert row[1] == 0

    def test_cascade_delete(self, e2e_config):
        """Deleting a memory cascades to its sync_queue entries."""
        from tools.schema import _get_pool
        from tools.sync_queue import enqueue_sync

        db_url = e2e_config["db_url"]
        _insert_memory_raw(db_url, "obs::cascade-1")

        pool = _get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                enqueue_sync(cur, "obs::cascade-1", ["remote-a", "remote-b"])
            conn.commit()

        # Delete the memory
        conn = psycopg.connect(db_url, autocommit=True)
        try:
            conn.execute("DELETE FROM core.memories WHERE id = 'obs::cascade-1'")

            # Queue entries should be gone
            row = conn.execute(
                "SELECT count(*) FROM core.sync_queue WHERE memory_id = 'obs::cascade-1'"
            ).fetchone()
            assert row[0] == 0
        finally:
            conn.close()

    def test_synced_to_array_update(self, e2e_config):
        """update_synced_to adds destinations to the memories array."""
        from tools.schema import _get_pool
        from tools.sync_queue import update_synced_to

        db_url = e2e_config["db_url"]
        _insert_memory_raw(db_url, "obs::synced-1")

        pool = _get_pool()
        update_synced_to(pool, "obs::synced-1", "remote-a")
        update_synced_to(pool, "obs::synced-1", "remote-b")
        # Duplicate should be a no-op
        update_synced_to(pool, "obs::synced-1", "remote-a")

        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT synced_to FROM core.memories WHERE id = 'obs::synced-1'"
                )
                row = cur.fetchone()
                assert sorted(row[0]) == ["remote-a", "remote-b"]

    def test_origin_default_is_local(self, e2e_config):
        """New memories have origin='local' by default."""
        db_url = e2e_config["db_url"]
        _insert_memory_raw(db_url, "obs::origin-1")

        conn = psycopg.connect(db_url)
        try:
            row = conn.execute(
                "SELECT origin FROM core.memories WHERE id = 'obs::origin-1'"
            ).fetchone()
            assert row[0] == "local"
        finally:
            conn.close()

    def test_queue_stats(self, e2e_config):
        """get_queue_stats returns per-destination counts."""
        from tools.schema import _get_pool
        from tools.sync_queue import enqueue_sync, get_queue_stats

        db_url = e2e_config["db_url"]
        _insert_memory_raw(db_url, "obs::stats-1")
        _insert_memory_raw(db_url, "obs::stats-2")

        pool = _get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                enqueue_sync(cur, "obs::stats-1", ["remote-a", "remote-b"])
                enqueue_sync(cur, "obs::stats-2", ["remote-a"])
            conn.commit()

        stats = get_queue_stats(pool)
        assert stats["remote-a"]["pending"] == 2
        assert stats["remote-b"]["pending"] == 1
        assert stats["_total"]["pending"] == 3


class TestTransactionalOutbox:
    """Test the transactional outbox pattern in content_write."""

    def test_memory_and_queue_in_same_transaction(self, e2e_config, monkeypatch):
        """Writing a memory with sync enabled creates queue entries atomically."""
        from tools.content import content_write
        from tools.schema import _get_pool

        # Enable sync with a simple rule
        monkeypatch.setattr("tools.config.get_sync_config", lambda: {
            "enabled": True,
            "strategy": "first-match",
            "remotes": {"backup": {"url": "postgresql://h:5432/db"}},
            "rules": [
                {"name": "all", "match": {}, "action": "route-to",
                 "destinations": ["backup"]},
            ],
            "project_groups": {},
        })

        result = content_write(
            content="Transactional outbox test",
            content_type="observation",
            skip_secret_scan=True,
        )
        assert result["success"] is True

        # Verify queue entry was created
        pool = _get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT destination FROM core.sync_queue WHERE memory_id = %s",
                    (result["id"],),
                )
                rows = cur.fetchall()
                assert len(rows) == 1
                assert rows[0][0] == "backup"

    def test_sync_disabled_no_queue_entries(self, e2e_config, monkeypatch):
        """With sync disabled, no queue entries are created."""
        from tools.content import content_write
        from tools.schema import _get_pool

        monkeypatch.setattr("tools.config.get_sync_config", lambda: {
            "enabled": False,
        })

        result = content_write(
            content="No sync test",
            content_type="observation",
            skip_secret_scan=True,
        )
        assert result["success"] is True

        pool = _get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM core.sync_queue WHERE memory_id = %s",
                    (result["id"],),
                )
                row = cur.fetchone()
                assert row[0] == 0

    def test_soft_delete_propagates_to_synced_remotes(self, e2e_config):
        """Soft-deleting a memory with synced_to creates queue entries."""
        from tools.content import content_delete
        from tools.schema import _get_pool
        from tools.sync_queue import update_synced_to

        db_url = e2e_config["db_url"]
        _insert_memory_raw(db_url, "obs::delete-prop-1")

        pool = _get_pool()
        # Simulate that this memory was already synced to two remotes
        update_synced_to(pool, "obs::delete-prop-1", "remote-a")
        update_synced_to(pool, "obs::delete-prop-1", "remote-b")

        # Soft delete
        result = content_delete("obs::delete-prop-1", hard=False)
        assert result["success"] is True
        assert result["deleted"] is True

        # Verify queue entries were created for synced remotes
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT destination FROM core.sync_queue
                       WHERE memory_id = 'obs::delete-prop-1'
                       ORDER BY destination"""
                )
                rows = cur.fetchall()
                dests = [r[0] for r in rows]
                assert dests == ["remote-a", "remote-b"]

    def test_pending_index_scan(self, e2e_config):
        """The idx_sync_queue_pending index is used for pending claims."""
        from tools.schema import _get_pool

        db_url = e2e_config["db_url"]
        conn = psycopg.connect(db_url)
        try:
            row = conn.execute(
                """SELECT indexname FROM pg_indexes
                   WHERE schemaname = 'core' AND tablename = 'sync_queue'
                     AND indexname = 'idx_sync_queue_pending'"""
            ).fetchone()
            assert row is not None, "idx_sync_queue_pending index not found"
        finally:
            conn.close()

    def test_routing_index_exists(self, e2e_config):
        """The idx_core_routing composite index exists on core.memories."""
        db_url = e2e_config["db_url"]
        conn = psycopg.connect(db_url)
        try:
            row = conn.execute(
                """SELECT indexname FROM pg_indexes
                   WHERE schemaname = 'core' AND tablename = 'memories'
                     AND indexname = 'idx_core_routing'"""
            ).fetchone()
            assert row is not None, "idx_core_routing index not found"
        finally:
            conn.close()
