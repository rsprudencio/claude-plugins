"""Tests for sync queue management (tools/sync_queue.py).

Unit tests use mock pools/cursors. E2E tests in tests/e2e/ cover real PG.
"""

from unittest.mock import MagicMock, patch, call
from contextlib import contextmanager

import pytest


def _mock_pool():
    """Create a mock pool with connection context manager."""
    pool = MagicMock()
    conn = MagicMock()
    cur = MagicMock()

    @contextmanager
    def connection_ctx():
        yield conn

    @contextmanager
    def cursor_ctx():
        yield cur

    pool.connection = connection_ctx
    conn.cursor = cursor_ctx
    conn.commit = MagicMock()

    return pool, conn, cur


class TestEnqueueSync:
    def test_enqueue_single_destination(self):
        from tools.sync_queue import enqueue_sync

        cur = MagicMock()
        cur.rowcount = 1
        result = enqueue_sync(cur, "obs::123", ["remote-a"])
        assert result == 1
        cur.execute.assert_called_once()
        args = cur.execute.call_args[0]
        assert "INSERT INTO local.sync_queue" in args[0]
        assert args[1] == ("obs::123", "remote-a", 1)

    def test_enqueue_multiple_destinations(self):
        from tools.sync_queue import enqueue_sync

        cur = MagicMock()
        cur.rowcount = 1
        result = enqueue_sync(cur, "obs::123", ["remote-a", "remote-b", "remote-c"])
        assert result == 3
        assert cur.execute.call_count == 3

    def test_enqueue_empty_destinations(self):
        from tools.sync_queue import enqueue_sync

        cur = MagicMock()
        result = enqueue_sync(cur, "obs::123", [])
        assert result == 0
        cur.execute.assert_not_called()

    def test_enqueue_custom_version(self):
        from tools.sync_queue import enqueue_sync

        cur = MagicMock()
        cur.rowcount = 1
        enqueue_sync(cur, "obs::123", ["remote-a"], version=2)
        args = cur.execute.call_args[0]
        assert args[1] == ("obs::123", "remote-a", 2)


class TestClaimPendingSyncs:
    def test_claim_returns_entries(self):
        from tools.sync_queue import claim_pending_syncs

        pool, conn, cur = _mock_pool()
        cur.fetchall.return_value = [
            (1, "obs::123", "remote-a", 1, 0),
            (2, "obs::456", "remote-b", 1, 1),
        ]

        entries = claim_pending_syncs(pool, batch_size=10)
        assert len(entries) == 2
        assert entries[0]["id"] == 1
        assert entries[0]["memory_id"] == "obs::123"
        assert entries[0]["destination"] == "remote-a"
        assert entries[1]["attempts"] == 1

    def test_claim_empty_queue(self):
        from tools.sync_queue import claim_pending_syncs

        pool, conn, cur = _mock_pool()
        cur.fetchall.return_value = []

        entries = claim_pending_syncs(pool)
        assert entries == []

    def test_claim_uses_skip_locked(self):
        from tools.sync_queue import claim_pending_syncs

        pool, conn, cur = _mock_pool()
        cur.fetchall.return_value = []

        claim_pending_syncs(pool)
        sql = cur.execute.call_args[0][0]
        assert "SKIP LOCKED" in sql


class TestMarkSynced:
    def test_mark_synced(self):
        from tools.sync_queue import mark_synced

        pool, conn, cur = _mock_pool()
        cur.rowcount = 2
        result = mark_synced(pool, [1, 2])
        assert result == 2
        sql = cur.execute.call_args[0][0]
        assert "status = 'done'" in sql

    def test_mark_synced_empty(self):
        from tools.sync_queue import mark_synced

        pool, conn, cur = _mock_pool()
        result = mark_synced(pool, [])
        assert result == 0
        cur.execute.assert_not_called()


class TestMarkFailed:
    def test_mark_failed_with_backoff(self):
        from tools.sync_queue import mark_failed

        pool, conn, cur = _mock_pool()
        cur.rowcount = 1
        result = mark_failed(pool, [1], "connection refused")
        assert result == 1
        sql = cur.execute.call_args[0][0]
        assert "attempts + 1" in sql
        assert "'dlq'" in sql  # DLQ transition logic present

    def test_mark_failed_empty(self):
        from tools.sync_queue import mark_failed

        pool, conn, cur = _mock_pool()
        result = mark_failed(pool, [], "error")
        assert result == 0
        cur.execute.assert_not_called()


class TestRetryDlq:
    def test_retry_all_dlq(self):
        from tools.sync_queue import retry_dlq

        pool, conn, cur = _mock_pool()
        cur.rowcount = 3
        result = retry_dlq(pool)
        assert result == 3
        sql = cur.execute.call_args[0][0]
        assert "status = 'pending'" in sql
        assert "status = 'dlq'" in sql

    def test_retry_specific_destination(self):
        from tools.sync_queue import retry_dlq

        pool, conn, cur = _mock_pool()
        cur.rowcount = 1
        result = retry_dlq(pool, destination="work")
        assert result == 1
        args = cur.execute.call_args[0]
        assert "destination = %s" in args[0]
        assert args[1] == ("work",)


class TestGetQueueStats:
    def test_stats_grouped_by_destination(self):
        from tools.sync_queue import get_queue_stats

        pool, conn, cur = _mock_pool()
        cur.fetchall.return_value = [
            ("remote-a", "pending", 5),
            ("remote-a", "done", 100),
            ("remote-b", "pending", 2),
            ("remote-b", "dlq", 1),
        ]

        stats = get_queue_stats(pool)
        assert stats["remote-a"]["pending"] == 5
        assert stats["remote-a"]["done"] == 100
        assert stats["remote-b"]["dlq"] == 1
        assert stats["_total"]["pending"] == 7

    def test_stats_empty_queue(self):
        from tools.sync_queue import get_queue_stats

        pool, conn, cur = _mock_pool()
        cur.fetchall.return_value = []

        stats = get_queue_stats(pool)
        assert stats == {"_total": {}}


class TestUpdateSyncedTo:
    def test_update_synced_to(self):
        from tools.sync_queue import update_synced_to

        pool, conn, cur = _mock_pool()
        update_synced_to(pool, "obs::123", "remote-a")
        sql = cur.execute.call_args[0][0]
        assert "array_append" in sql
        assert "NOT" in sql  # dedup check
        conn.commit.assert_called_once()
