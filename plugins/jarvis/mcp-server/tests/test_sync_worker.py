"""Tests for sync worker background loop (tools/sync_worker.py)."""

from unittest.mock import MagicMock, patch

import pytest


class TestSyncIteration:
    @patch("tools.sync_worker.get_sync_config")
    def test_disabled_skips(self, mock_cfg):
        from tools.sync_worker import _sync_iteration

        mock_cfg.return_value = {"enabled": False}
        result = _sync_iteration()
        assert result["skipped"] is True
        assert result["reason"] == "disabled"

    @patch("tools.sync_worker.get_sync_config")
    def test_no_remotes_skips(self, mock_cfg):
        from tools.sync_worker import _sync_iteration

        mock_cfg.return_value = {"enabled": True, "remotes": {}}
        result = _sync_iteration()
        assert result["skipped"] is True
        assert result["reason"] == "no_remotes"

    @patch("tools.sync_worker.update_synced_to")
    @patch("tools.sync_worker.mark_synced")
    @patch("tools.sync_worker._batch_upsert_to_remote")
    @patch("tools.sync_worker._fetch_memories")
    @patch("tools.sync_worker.claim_pending_syncs")
    @patch("tools.sync_worker._get_pool")
    @patch("tools.sync_worker.get_sync_config")
    def test_empty_queue(self, mock_cfg, mock_pool, mock_claim,
                         mock_fetch, mock_upsert, mock_synced, mock_update):
        from tools.sync_worker import _sync_iteration

        mock_cfg.return_value = {
            "enabled": True,
            "remotes": {"work": {"url": "postgresql://h:5432/db"}},
        }
        mock_pool.return_value = MagicMock()
        mock_claim.return_value = []

        result = _sync_iteration()
        assert result["claimed"] == 0

    @patch("tools.sync_worker.update_synced_to")
    @patch("tools.sync_worker.mark_synced")
    @patch("tools.sync_worker._batch_upsert_to_remote")
    @patch("tools.sync_worker._ensure_remote_schema")
    @patch("tools.sync_worker._fetch_memories")
    @patch("tools.sync_worker.claim_pending_syncs")
    @patch("tools.sync_worker._get_pool")
    @patch("tools.sync_worker.get_sync_config")
    def test_successful_sync(self, mock_cfg, mock_pool, mock_claim,
                             mock_fetch, mock_ensure, mock_upsert,
                             mock_synced, mock_update):
        from tools.sync_worker import _sync_iteration

        mock_cfg.return_value = {
            "enabled": True,
            "remotes": {"work": {"url": "postgresql://h:5432/db"}},
        }
        pool = MagicMock()
        mock_pool.return_value = pool
        mock_claim.return_value = [
            {"id": 1, "memory_id": "obs::123", "destination": "work",
             "version": 1, "attempts": 0},
        ]
        mock_fetch.return_value = [{"id": "obs::123", "document": "test"}]

        result = _sync_iteration()
        assert result["claimed"] == 1
        assert result["synced"] == 1
        assert result["failed"] == 0
        mock_upsert.assert_called_once()
        mock_synced.assert_called_once_with(pool, [1])

    @patch("tools.sync_worker.mark_failed")
    @patch("tools.sync_worker.claim_pending_syncs")
    @patch("tools.sync_worker._get_pool")
    @patch("tools.sync_worker.get_sync_config")
    def test_missing_remote_marks_failed(self, mock_cfg, mock_pool,
                                         mock_claim, mock_failed):
        from tools.sync_worker import _sync_iteration

        mock_cfg.return_value = {
            "enabled": True,
            # Has a remote configured, but the queue entry targets a different one
            "remotes": {"other": {"url": "postgresql://h:5432/db"}},
        }
        pool = MagicMock()
        mock_pool.return_value = pool
        mock_claim.return_value = [
            {"id": 1, "memory_id": "obs::123", "destination": "missing",
             "version": 1, "attempts": 0},
        ]

        result = _sync_iteration()
        assert result["failed"] == 1
        mock_failed.assert_called_once()

    @patch("tools.sync_worker.mark_failed")
    @patch("tools.sync_worker._batch_upsert_to_remote")
    @patch("tools.sync_worker._ensure_remote_schema")
    @patch("tools.sync_worker._fetch_memories")
    @patch("tools.sync_worker.claim_pending_syncs")
    @patch("tools.sync_worker._get_pool")
    @patch("tools.sync_worker.get_sync_config")
    def test_upsert_failure_marks_failed(self, mock_cfg, mock_pool, mock_claim,
                                         mock_fetch, mock_ensure, mock_upsert,
                                         mock_failed):
        from tools.sync_worker import _sync_iteration

        mock_cfg.return_value = {
            "enabled": True,
            "remotes": {"work": {"url": "postgresql://h:5432/db"}},
        }
        pool = MagicMock()
        mock_pool.return_value = pool
        mock_claim.return_value = [
            {"id": 1, "memory_id": "obs::123", "destination": "work",
             "version": 1, "attempts": 0},
        ]
        mock_fetch.return_value = [{"id": "obs::123"}]
        mock_upsert.side_effect = ConnectionError("refused")

        result = _sync_iteration()
        assert result["failed"] == 1
        mock_failed.assert_called_once()

    @patch("tools.sync_worker.update_synced_to")
    @patch("tools.sync_worker.mark_synced")
    @patch("tools.sync_worker.mark_failed")
    @patch("tools.sync_worker._batch_upsert_to_remote")
    @patch("tools.sync_worker._ensure_remote_schema")
    @patch("tools.sync_worker._fetch_memories")
    @patch("tools.sync_worker.claim_pending_syncs")
    @patch("tools.sync_worker._get_pool")
    @patch("tools.sync_worker.get_sync_config")
    def test_groups_by_destination(self, mock_cfg, mock_pool, mock_claim,
                                   mock_fetch, mock_ensure, mock_upsert,
                                   mock_failed, mock_synced, mock_update):
        from tools.sync_worker import _sync_iteration

        mock_cfg.return_value = {
            "enabled": True,
            "remotes": {
                "work": {"url": "postgresql://work:5432/db"},
                "backup": {"url": "postgresql://backup:5432/db"},
            },
        }
        pool = MagicMock()
        mock_pool.return_value = pool
        mock_claim.return_value = [
            {"id": 1, "memory_id": "obs::1", "destination": "work",
             "version": 1, "attempts": 0},
            {"id": 2, "memory_id": "obs::2", "destination": "backup",
             "version": 1, "attempts": 0},
            {"id": 3, "memory_id": "obs::3", "destination": "work",
             "version": 1, "attempts": 0},
        ]
        mock_fetch.return_value = [{"id": "obs::1"}, {"id": "obs::2"}, {"id": "obs::3"}]

        result = _sync_iteration()
        assert result["claimed"] == 3
        # Two upsert calls: one for "work" batch, one for "backup" batch
        assert mock_upsert.call_count == 2


class TestRemoteSchema:
    """Tests for per-remote schema support in sync worker."""

    @patch("tools.sync_worker.update_synced_to")
    @patch("tools.sync_worker.mark_synced")
    @patch("tools.sync_worker._batch_upsert_to_remote")
    @patch("tools.sync_worker._ensure_remote_schema")
    @patch("tools.sync_worker._fetch_memories")
    @patch("tools.sync_worker.claim_pending_syncs")
    @patch("tools.sync_worker._get_pool")
    @patch("tools.sync_worker.get_sync_config")
    def test_explicit_schema_passed_to_upsert(
        self, mock_cfg, mock_pool, mock_claim, mock_fetch,
        mock_ensure, mock_upsert, mock_synced, mock_update
    ):
        """Explicit schema field is threaded through to upsert."""
        from tools.sync_worker import _sync_iteration

        mock_cfg.return_value = {
            "enabled": True,
            "remotes": {
                "aurora": {
                    "url": "postgresql://h:5432/db",
                    "schema": "personio",
                },
            },
        }
        pool = MagicMock()
        mock_pool.return_value = pool
        mock_claim.return_value = [
            {"id": 1, "memory_id": "obs::1", "destination": "aurora",
             "version": 1, "attempts": 0},
        ]
        mock_fetch.return_value = [{"id": "obs::1", "document": "test"}]

        _sync_iteration()

        mock_ensure.assert_called_once_with(
            "postgresql://h:5432/db", "personio"
        )
        mock_upsert.assert_called_once()
        _, kwargs = mock_upsert.call_args
        assert kwargs["schema"] == "personio"

    @patch("tools.sync_worker.update_synced_to")
    @patch("tools.sync_worker.mark_synced")
    @patch("tools.sync_worker._batch_upsert_to_remote")
    @patch("tools.sync_worker._ensure_remote_schema")
    @patch("tools.sync_worker._fetch_memories")
    @patch("tools.sync_worker.claim_pending_syncs")
    @patch("tools.sync_worker._get_pool")
    @patch("tools.sync_worker.get_sync_config")
    def test_schema_defaults_to_remote_name(
        self, mock_cfg, mock_pool, mock_claim, mock_fetch,
        mock_ensure, mock_upsert, mock_synced, mock_update
    ):
        """When schema field omitted, remote name is used as schema."""
        from tools.sync_worker import _sync_iteration

        mock_cfg.return_value = {
            "enabled": True,
            "remotes": {
                "aurora": {"url": "postgresql://h:5432/db"},
            },
        }
        pool = MagicMock()
        mock_pool.return_value = pool
        mock_claim.return_value = [
            {"id": 1, "memory_id": "obs::1", "destination": "aurora",
             "version": 1, "attempts": 0},
        ]
        mock_fetch.return_value = [{"id": "obs::1", "document": "test"}]

        _sync_iteration()

        mock_ensure.assert_called_once_with(
            "postgresql://h:5432/db", "aurora"
        )
        _, kwargs = mock_upsert.call_args
        assert kwargs["schema"] == "aurora"

    @patch("tools.embedding.get_embedding_service")
    @patch("pgvector.psycopg.register_vector")
    @patch("psycopg.connect")
    def test_upsert_sql_contains_schema_name(self, mock_connect, mock_register,
                                              mock_emb_svc):
        """_batch_upsert_to_remote generates SQL with the correct CAS tables."""
        from tools.sync_worker import _batch_upsert_to_remote

        mock_svc = MagicMock()
        mock_svc.model_name = "test-model"
        mock_emb_svc.return_value = mock_svc

        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_connect.return_value = mock_conn

        mem = {
            "id": "obs::1", "document": "test", "embedding": [0.1],
            "category": "observation", "scope": "global", "project": None,
            "source": "auto-extract", "importance_score": 0.5,
            "retrieval_count": 0.0, "status": "active",
            "superseded_by": None, "deleted_at": None,
            "metadata": {}, "synced_to": [], "origin": "local",
            "created_at": "2026-01-01", "updated_at": "2026-01-01",
        }
        _batch_upsert_to_remote("postgresql://h/db", [mem], schema="personio")

        # Should have 2 execute calls: 1 content + 1 ref
        calls = mock_cur.execute.call_args_list
        assert len(calls) == 2
        content_sql = calls[0][0][0]
        ref_sql = calls[1][0][0]
        assert "personio.content" in content_sql
        assert "personio.memory_refs" in ref_sql
        assert "personio.memories" not in content_sql
        assert "personio.memories" not in ref_sql

    @patch("tools.sync_worker.get_embedding_config")
    @patch("psycopg.connect")
    def test_ensure_remote_schema_runs_ddl(self, mock_connect, mock_emb):
        """_ensure_remote_schema executes CAS DDL on first call."""
        import tools.sync_worker as mod
        from tools.sync_worker import _ensure_remote_schema

        mock_emb.return_value = {"dimensions": 384}
        mod._ensured_schemas.discard(("postgresql://h/db", "personio"))

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_connect.return_value = mock_conn

        _ensure_remote_schema("postgresql://h/db", "personio")

        mock_conn.execute.assert_called_once()
        ddl = mock_conn.execute.call_args[0][0]
        assert "CREATE SCHEMA IF NOT EXISTS personio" in ddl
        assert "personio.content" in ddl
        assert "personio.memory_refs" in ddl

        mod._ensured_schemas.discard(("postgresql://h/db", "personio"))

    @patch("tools.sync_worker.get_embedding_config")
    @patch("psycopg.connect")
    def test_ensure_remote_schema_cached(self, mock_connect, mock_emb):
        """_ensure_remote_schema skips DDL on second call (same url+schema)."""
        import tools.sync_worker as mod
        from tools.sync_worker import _ensure_remote_schema

        mock_emb.return_value = {"dimensions": 384}
        mod._ensured_schemas.discard(("postgresql://h/db", "test_cached"))

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_connect.return_value = mock_conn

        _ensure_remote_schema("postgresql://h/db", "test_cached")
        assert mock_connect.call_count == 1

        # Second call — cached, no connection
        _ensure_remote_schema("postgresql://h/db", "test_cached")
        assert mock_connect.call_count == 1  # still 1

        mod._ensured_schemas.discard(("postgresql://h/db", "test_cached"))

    @patch("tools.sync_worker.get_embedding_config")
    @patch("psycopg.connect")
    def test_ensure_remote_schema_different_schemas_not_cached(
        self, mock_connect, mock_emb
    ):
        """Different schema names on same URL are cached independently."""
        import tools.sync_worker as mod
        from tools.sync_worker import _ensure_remote_schema

        mock_emb.return_value = {"dimensions": 384}
        mod._ensured_schemas.discard(("postgresql://h/db", "schema_a"))
        mod._ensured_schemas.discard(("postgresql://h/db", "schema_b"))

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_connect.return_value = mock_conn

        _ensure_remote_schema("postgresql://h/db", "schema_a")
        _ensure_remote_schema("postgresql://h/db", "schema_b")
        assert mock_connect.call_count == 2

        mod._ensured_schemas.discard(("postgresql://h/db", "schema_a"))
        mod._ensured_schemas.discard(("postgresql://h/db", "schema_b"))


class TestContentHash:
    """Tests for _compute_content_hash()."""

    def test_deterministic(self):
        from tools.sync_worker import _compute_content_hash

        h1 = _compute_content_hash("hello world", "model-v1")
        h2 = _compute_content_hash("hello world", "model-v1")
        assert h1 == h2

    def test_model_sensitivity(self):
        """Different model name produces different hash."""
        from tools.sync_worker import _compute_content_hash

        h1 = _compute_content_hash("same doc", "model-v1")
        h2 = _compute_content_hash("same doc", "model-v2")
        assert h1 != h2

    def test_nul_separator(self):
        """NUL separator prevents prefix collisions."""
        from tools.sync_worker import _compute_content_hash

        # Without NUL separator, "abc" + "def" could equal "ab" + "cdef"
        h1 = _compute_content_hash("abc", "def")
        h2 = _compute_content_hash("ab", "cdef")
        assert h1 != h2

    def test_returns_hex_string(self):
        from tools.sync_worker import _compute_content_hash

        h = _compute_content_hash("test", "model")
        assert len(h) == 64  # SHA-256 hex digest
        assert all(c in "0123456789abcdef" for c in h)


class TestCASBatchUpsert:
    """Tests for CAS-aware _batch_upsert_to_remote()."""

    def _make_memory(self, mem_id="obs::1", document="test doc"):
        return {
            "id": mem_id, "document": document, "embedding": [0.1],
            "category": "observation", "scope": "global", "project": None,
            "source": "auto-extract", "importance_score": 0.5,
            "retrieval_count": 0.0, "status": "active",
            "superseded_by": None, "deleted_at": None,
            "metadata": {}, "synced_to": [], "origin": "local",
            "created_at": "2026-01-01", "updated_at": "2026-01-01",
        }

    @patch("tools.embedding.get_embedding_service")
    @patch("pgvector.psycopg.register_vector")
    @patch("psycopg.connect")
    def test_content_dedup_within_batch(self, mock_connect, mock_register,
                                        mock_emb_svc):
        """Two memories with same document produce 1 content INSERT, 2 ref INSERTs."""
        from tools.sync_worker import _batch_upsert_to_remote

        mock_svc = MagicMock()
        mock_svc.model_name = "test-model"
        mock_emb_svc.return_value = mock_svc

        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_connect.return_value = mock_conn

        mem1 = self._make_memory("obs::1", "same document")
        mem2 = self._make_memory("obs::2", "same document")  # same content

        _batch_upsert_to_remote("postgresql://h/db", [mem1, mem2], schema="test")

        calls = mock_cur.execute.call_args_list
        content_calls = [c for c in calls if "content" in c[0][0] and "memory_refs" not in c[0][0]]
        ref_calls = [c for c in calls if "memory_refs" in c[0][0]]
        assert len(content_calls) == 1  # deduped
        assert len(ref_calls) == 2

    @patch("tools.embedding.get_embedding_service")
    @patch("pgvector.psycopg.register_vector")
    @patch("psycopg.connect")
    def test_transaction_ordering(self, mock_connect, mock_register, mock_emb_svc):
        """Content INSERTs execute before ref INSERTs."""
        from tools.sync_worker import _batch_upsert_to_remote

        mock_svc = MagicMock()
        mock_svc.model_name = "test-model"
        mock_emb_svc.return_value = mock_svc

        mock_cur = MagicMock()
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_connect.return_value = mock_conn

        mem = self._make_memory()
        _batch_upsert_to_remote("postgresql://h/db", [mem], schema="test")

        calls = mock_cur.execute.call_args_list
        # First call should be content, second should be ref
        assert "content" in calls[0][0][0] and "memory_refs" not in calls[0][0][0]
        assert "memory_refs" in calls[1][0][0]

    @patch("tools.embedding.get_embedding_service")
    @patch("pgvector.psycopg.register_vector")
    @patch("psycopg.connect")
    def test_empty_batch_is_noop(self, mock_connect, mock_register, mock_emb_svc):
        """Empty memories list doesn't connect to remote."""
        from tools.sync_worker import _batch_upsert_to_remote

        _batch_upsert_to_remote("postgresql://h/db", [], schema="test")
        mock_connect.assert_not_called()


class TestSyncPresets:
    def test_list_presets(self):
        from tools.sync_presets import list_presets

        presets = list_presets()
        assert len(presets) >= 3
        names = [p["name"] for p in presets]
        assert "personal-backup" in names
        assert "work-separation" in names
        assert "privacy-first" in names

    def test_get_preset(self):
        from tools.sync_presets import get_preset

        preset = get_preset("personal-backup")
        assert preset is not None
        assert "rules" in preset
        assert preset["strategy"] == "first-match"

    def test_get_nonexistent_preset(self):
        from tools.sync_presets import get_preset

        assert get_preset("nonexistent") is None
