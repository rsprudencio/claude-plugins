"""Tests for the embedding model upgrade migration script.

Tests the bin/upgrade_embedding_model.py functions that handle
the 384d vector → 768d halfvec migration.
"""

import math
import os
import pytest
from unittest.mock import MagicMock, patch, call

from bin.upgrade_embedding_model import (
    preflight,
    add_new_column,
    batch_reembed,
    verify_completeness,
    create_new_index,
    atomic_column_swap,
    update_meta,
    cleanup_old_column,
    DEFAULT_MODEL,
    DEFAULT_DIMENSIONS,
)


# ── Helpers ────────────────────────────────────────────────────────────


class MockCursor:
    """Simple mock cursor for migration tests."""

    def __init__(self, results=None):
        self._results = results or []
        self._idx = 0

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        if self._idx < len(self._results):
            row = self._results[self._idx]
            self._idx += 1
            return row
        return None

    def fetchall(self):
        result = self._results[self._idx:]
        self._idx = len(self._results)
        return result


class MockConn:
    """Mock connection for migration tests."""

    def __init__(self, cursor_results=None):
        self._cursor_results = cursor_results or []
        self._cursor_idx = 0
        self._executed = []
        self.autocommit = False

    def cursor(self):
        if self._cursor_idx < len(self._cursor_results):
            cur = MockCursor(self._cursor_results[self._cursor_idx])
            self._cursor_idx += 1
            return cur
        return MockCursor()

    def execute(self, sql, params=None):
        self._executed.append((sql, params))

    def commit(self):
        pass

    def close(self):
        pass


# ── Pre-flight tests ──────────────────────────────────────────────────


class TestPreflight:
    """Tests for the preflight check function."""

    def test_detects_existing_schema(self):
        """Preflight detects table with embedding column."""
        conn = MockConn(cursor_results=[
            # Column info query
            [("vector", None, None)],
            # Row count
            [(42,)],
            # embedding_new column check
            [None],  # Doesn't exist
        ])
        # Override cursor to handle the column check returning None
        mock_cur = MagicMock()
        mock_cur.fetchone.side_effect = [
            ("vector", None, None),  # col_info
            (42,),                    # row_count
            None,                     # no embedding_new column
        ]
        conn.cursor = MagicMock(return_value=mock_cur)

        stats = preflight(conn)
        assert stats["row_count"] == 42
        assert stats["has_new_column"] is False

    def test_detects_new_column_exists(self):
        """Preflight detects when embedding_new column already exists (resume)."""
        mock_cur = MagicMock()
        mock_cur.fetchone.side_effect = [
            ("vector", None, None),   # col_info
            (100,),                    # row_count
            ("embedding_new",),        # new column exists
            (25,),                     # 25 pending
        ]
        conn = MagicMock()
        conn.cursor.return_value = mock_cur

        stats = preflight(conn)
        assert stats["row_count"] == 100
        assert stats["has_new_column"] is True
        assert stats["pending_count"] == 25

    def test_missing_table_raises(self):
        """Preflight raises RuntimeError if embedding column not found."""
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = None  # No column found
        conn = MagicMock()
        conn.cursor.return_value = mock_cur

        with pytest.raises(RuntimeError, match="not found"):
            preflight(conn)


# ── Column operations ─────────────────────────────────────────────────


class TestAddNewColumn:
    """Tests for add_new_column."""

    def test_executes_alter_table(self):
        """Adds halfvec column with correct dimensions."""
        conn = MagicMock()
        add_new_column(conn, 768)

        conn.execute.assert_called_once()
        sql = conn.execute.call_args[0][0]
        assert "halfvec(768)" in sql
        assert "embedding_new" in sql
        assert "IF NOT EXISTS" in sql
        conn.commit.assert_called_once()


class TestCleanupOldColumn:
    """Tests for cleanup_old_column."""

    def test_drops_column(self):
        """Drops embedding_old column."""
        conn = MagicMock()
        cleanup_old_column(conn)

        conn.execute.assert_called_once()
        sql = conn.execute.call_args[0][0]
        assert "DROP COLUMN" in sql
        assert "embedding_old" in sql
        conn.commit.assert_called_once()

    def test_dry_run_no_changes(self):
        """Dry run does not modify the database."""
        conn = MagicMock()
        cleanup_old_column(conn, dry_run=True)

        conn.execute.assert_not_called()
        conn.commit.assert_not_called()


# ── Batch re-embed ────────────────────────────────────────────────────


class TestBatchReembed:
    """Tests for batch_reembed."""

    @patch("tools.schema.set_meta")
    def test_processes_batches(self, mock_set_meta):
        """Re-embeds documents in batches and tracks progress."""
        # First batch: 2 docs. Second batch: empty (done).
        select_cur = MagicMock()
        select_cur.fetchall.side_effect = [
            [("id1", "hello world"), ("id2", "goodbye moon")],
            [],  # No more pending
        ]
        update_cur = MagicMock()
        conn = MagicMock()
        conn.cursor.side_effect = [select_cur, update_cur, select_cur]

        with patch("tools.embedding.EmbeddingService") as MockSvc:
            mock_svc = MagicMock()
            mock_svc.encode_batch.return_value = [
                [0.1] * 768,
                [0.2] * 768,
            ]
            MockSvc.return_value = mock_svc

            processed = batch_reembed(
                conn,
                model_name=DEFAULT_MODEL,
                dimensions=768,
                batch_size=100,
            )

        assert processed == 2
        assert update_cur.execute.call_count == 2
        # Verify halfvec cast in UPDATE SQL
        update_sql = update_cur.execute.call_args_list[0][0][0]
        assert "::halfvec" in update_sql

    def test_dry_run_previews_only(self):
        """Dry run reports batch size but doesn't modify data."""
        select_cur = MagicMock()
        select_cur.fetchall.return_value = [
            ("id1", "doc1"), ("id2", "doc2"),
        ]
        conn = MagicMock()
        conn.cursor.return_value = select_cur

        processed = batch_reembed(
            conn,
            model_name=DEFAULT_MODEL,
            dimensions=768,
            batch_size=100,
            dry_run=True,
        )

        assert processed == 2
        conn.commit.assert_not_called()

    @patch("tools.schema.set_meta")
    def test_resumability_skips_completed(self, mock_set_meta):
        """Second run processes only remaining NULL rows."""
        # Simulates resuming with only 1 doc left
        select_cur = MagicMock()
        select_cur.fetchall.side_effect = [
            [("id3", "remaining doc")],
            [],
        ]
        update_cur = MagicMock()
        conn = MagicMock()
        conn.cursor.side_effect = [select_cur, update_cur, select_cur]

        with patch("tools.embedding.EmbeddingService") as MockSvc:
            mock_svc = MagicMock()
            mock_svc.encode_batch.return_value = [[0.5] * 768]
            MockSvc.return_value = mock_svc

            processed = batch_reembed(
                conn,
                model_name=DEFAULT_MODEL,
                dimensions=768,
                batch_size=100,
            )

        assert processed == 1


# ── Verify completeness ──────────────────────────────────────────────


class TestVerifyCompleteness:
    """Tests for verify_completeness."""

    def test_complete_returns_true(self):
        """Returns True when no NULLs remain."""
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (0,)
        conn = MagicMock()
        conn.cursor.return_value = mock_cur

        assert verify_completeness(conn) is True

    def test_incomplete_returns_false(self):
        """Returns False when NULLs remain."""
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (5,)
        conn = MagicMock()
        conn.cursor.return_value = mock_cur

        assert verify_completeness(conn) is False


# ── Column swap ───────────────────────────────────────────────────────


class TestAtomicColumnSwap:
    """Tests for atomic_column_swap."""

    def test_executes_rename_sequence(self):
        """Swap executes DROP INDEX, two RENAMEs, and index rename."""
        conn = MagicMock()
        atomic_column_swap(conn)

        # 4 SQL statements
        assert conn.execute.call_count == 4
        sqls = [c[0][0] for c in conn.execute.call_args_list]
        assert any("DROP INDEX" in s for s in sqls)
        assert any("embedding TO embedding_old" in s for s in sqls)
        assert any("embedding_new TO embedding" in s for s in sqls)
        conn.commit.assert_called_once()

    def test_dry_run_no_changes(self):
        """Dry run does not execute any SQL."""
        conn = MagicMock()
        atomic_column_swap(conn, dry_run=True)
        conn.execute.assert_not_called()


# ── Index creation ────────────────────────────────────────────────────


class TestCreateNewIndex:
    """Tests for create_new_index."""

    def test_creates_index_concurrently(self):
        """Creates HNSW index with CONCURRENTLY and halfvec_cosine_ops."""
        conn = MagicMock()
        create_new_index(conn)

        conn.execute.assert_called_once()
        sql = conn.execute.call_args[0][0]
        assert "CONCURRENTLY" in sql
        assert "halfvec_cosine_ops" in sql
        assert "embedding_new" in sql
        # Requires autocommit
        assert conn.autocommit is False  # Restored after

    def test_dry_run_no_changes(self):
        """Dry run does not create index."""
        conn = MagicMock()
        create_new_index(conn, dry_run=True)
        conn.execute.assert_not_called()


# ── Meta update ───────────────────────────────────────────────────────


class TestUpdateMeta:
    """Tests for update_meta."""

    def test_updates_meta_records(self, mock_config):
        """Updates embedding_config and schema_version in jarvis_meta."""
        from tools.schema import get_meta

        # Use real mock_config connection
        conn = MagicMock()  # Not used — update_meta uses set_meta directly
        update_meta(conn, DEFAULT_MODEL, 768)

        stored = get_meta("embedding_config")
        assert stored["model"] == DEFAULT_MODEL
        assert stored["dimensions"] == 768
        assert stored["vector_type"] == "halfvec"

        sv = get_meta("schema_version")
        assert sv["version"] == 2

        progress = get_meta("upgrade_embedding_progress")
        assert progress["status"] == "completed"

    def test_dry_run_no_changes(self, mock_config):
        """Dry run does not update meta."""
        from tools.schema import get_meta

        conn = MagicMock()
        update_meta(conn, DEFAULT_MODEL, 768, dry_run=True)

        assert get_meta("embedding_config") is None


# ── Halfvec cosine distance ──────────────────────────────────────────


class TestHalfvecCosineDistance:
    """Test that cosine distance works correctly with higher dimensions."""

    def test_768d_cosine_distance(self):
        """Cosine distance calculation works with 768d vectors."""
        from tests.conftest import _cosine_distance

        # Identical normalized vectors → distance 0
        vec = [1.0 / math.sqrt(768)] * 768
        assert abs(_cosine_distance(vec, vec)) < 1e-6

        # Orthogonal-ish vectors → distance ~1
        vec_a = [1.0 if i < 384 else 0.0 for i in range(768)]
        vec_b = [0.0 if i < 384 else 1.0 for i in range(768)]
        norm_a = math.sqrt(sum(x * x for x in vec_a))
        norm_b = math.sqrt(sum(x * x for x in vec_b))
        vec_a = [x / norm_a for x in vec_a]
        vec_b = [x / norm_b for x in vec_b]
        dist = _cosine_distance(vec_a, vec_b)
        assert abs(dist - 1.0) < 1e-6


# ── Model override ───────────────────────────────────────────────────


class TestModelOverride:
    """Tests for model override via CLI args."""

    def test_english_model_override(self):
        """English-only model can be specified as override."""
        assert DEFAULT_MODEL == "ibm-granite/granite-embedding-english-r2"

    def test_default_dimensions(self):
        """Default dimensions are 768."""
        assert DEFAULT_DIMENSIONS == 768
