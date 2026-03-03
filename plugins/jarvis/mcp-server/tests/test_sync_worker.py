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

    @patch("pgvector.psycopg.register_vector")
    @patch("psycopg.connect")
    def test_upsert_sql_contains_schema_name(self, mock_connect, mock_register):
        """_batch_upsert_to_remote generates SQL with the correct schema."""
        from tools.sync_worker import _batch_upsert_to_remote

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

        sql_executed = mock_cur.execute.call_args[0][0]
        assert "personio.memories" in sql_executed
        assert "local.memories" not in sql_executed

    @patch("tools.sync_worker.get_embedding_config")
    @patch("psycopg.connect")
    def test_ensure_remote_schema_runs_ddl(self, mock_connect, mock_emb):
        """_ensure_remote_schema executes DDL on first call."""
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
        assert "personio.memories" in ddl

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
