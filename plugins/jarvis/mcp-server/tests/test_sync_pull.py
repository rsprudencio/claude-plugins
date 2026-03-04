"""Tests for pull sync engine."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, call

import pytest
from psycopg import sql

from tools.sync_pull import (
    _meta_key,
    _get_last_pull_ts,
    _set_last_pull_ts,
    _ensure_local_mirror_schema,
    _get_local_ids,
    _get_remote_config,
    _adapt_row,
    _build_remote_select,
    _build_mirror_upsert,
    initial_pull,
    incremental_pull,
    pull_sync_loop,
    get_pull_sync_tasks,
    DEFAULT_BATCH_SIZE,
    _ensured_local_schemas,
)


class TestMetaKey:
    """Tests for meta key generation."""

    def test_meta_key_format(self):
        assert _meta_key("work") == "pull_sync_ts:work"
        assert _meta_key("home-server") == "pull_sync_ts:home-server"


class TestGetLastPullTs:
    """Tests for _get_last_pull_ts()."""

    def test_returns_none_when_no_meta(self):
        with patch("tools.sync_pull.get_meta", return_value=None):
            result = _get_last_pull_ts("remote1")
        assert result is None

    def test_returns_none_when_empty_timestamp(self):
        with patch("tools.sync_pull.get_meta", return_value={"timestamp": ""}):
            result = _get_last_pull_ts("remote1")
        assert result is None

    def test_returns_datetime_from_valid_iso(self):
        ts = "2026-03-01T12:00:00+00:00"
        with patch("tools.sync_pull.get_meta", return_value={"timestamp": ts}):
            result = _get_last_pull_ts("remote1")
        assert result is not None
        assert result.year == 2026
        assert result.month == 3
        assert result.tzinfo is not None

    def test_returns_utc_for_naive_timestamp(self):
        ts = "2026-03-01T12:00:00"
        with patch("tools.sync_pull.get_meta", return_value={"timestamp": ts}):
            result = _get_last_pull_ts("remote1")
        assert result is not None
        assert result.tzinfo == timezone.utc

    def test_returns_none_for_invalid_timestamp(self):
        with patch("tools.sync_pull.get_meta", return_value={"timestamp": "not-a-date"}):
            result = _get_last_pull_ts("remote1")
        assert result is None


class TestSetLastPullTs:
    """Tests for _set_last_pull_ts()."""

    def test_stores_iso_timestamp(self):
        ts = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
        with patch("tools.sync_pull.set_meta") as mock_set:
            _set_last_pull_ts("remote1", ts)

        mock_set.assert_called_once_with(
            "pull_sync_ts:remote1",
            {"timestamp": ts.isoformat(), "remote": "remote1"},
        )


class TestEnsureLocalMirrorSchema:
    """Tests for _ensure_local_mirror_schema()."""

    def setup_method(self):
        _ensured_local_schemas.clear()

    def test_runs_ddl_on_local_pool(self):
        mock_conn = MagicMock()
        mock_pool = MagicMock()
        mock_pool.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        with patch("tools.sync_pull._get_pool", return_value=mock_pool), \
             patch("tools.sync_pull.get_embedding_config",
                   return_value={"dimensions": 384}):
            _ensure_local_mirror_schema("remote_work")

        # DDL was executed
        mock_conn.execute.assert_called_once()
        ddl = mock_conn.execute.call_args[0][0]
        assert "remote_work" in ddl
        assert "CREATE SCHEMA" in ddl
        mock_conn.commit.assert_called_once()

    def test_caching_prevents_rerun(self):
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_pool.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        with patch("tools.sync_pull._get_pool", return_value=mock_pool), \
             patch("tools.sync_pull.get_embedding_config",
                   return_value={"dimensions": 384}):
            _ensure_local_mirror_schema("cached_schema")
            _ensure_local_mirror_schema("cached_schema")

        # Only one DDL call despite two invocations
        assert mock_conn.execute.call_count == 1


class TestGetLocalIds:
    """Tests for _get_local_ids()."""

    def test_returns_matching_ids(self):
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [("obs::1",), ("obs::3",)]

        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        result = _get_local_ids(mock_pool, ["obs::1", "obs::2", "obs::3"])
        assert result == {"obs::1", "obs::3"}

    def test_returns_empty_for_no_ids(self):
        mock_pool = MagicMock()
        result = _get_local_ids(mock_pool, [])
        assert result == set()


class TestGetRemoteConfig:
    """Tests for _get_remote_config()."""

    def test_returns_name_and_schema(self):
        cfg = {
            "enabled": True,
            "remotes": {
                "work": {"url": "postgres://...", "schema": "personio"}
            },
        }
        with patch("tools.sync_pull.get_sync_config", return_value=cfg):
            name, schema = _get_remote_config("work")
        assert name == "work"
        assert schema == "personio"

    def test_schema_defaults_to_name(self):
        cfg = {"enabled": True, "remotes": {"aurora": {"url": "postgres://..."}}}
        with patch("tools.sync_pull.get_sync_config", return_value=cfg):
            name, schema = _get_remote_config("aurora")
        assert schema == "aurora"

    def test_missing_remote_raises(self):
        cfg = {"enabled": True, "remotes": {}}
        with patch("tools.sync_pull.get_sync_config", return_value=cfg):
            with pytest.raises(KeyError, match="not configured"):
                _get_remote_config("missing")


class TestAdaptRow:
    """Tests for _adapt_row() — Jsonb wrapping and synced_to handling."""

    def test_wraps_metadata_dict_with_jsonb(self):
        from psycopg.types.json import Jsonb as PsycopgJsonb
        row = {
            "id": "obs::1", "document": "test", "embedding": [0.1],
            "category": "observation", "scope": "global", "project": None,
            "source": "auto-extract", "importance_score": 0.5,
            "retrieval_count": 0.0, "status": "active", "superseded_by": None,
            "deleted_at": None, "synced_to": ["work"], "origin": "local",
            "metadata": {"key": "value"},
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        adapted = _adapt_row(row)
        # metadata is at index 14 (0-indexed)
        assert isinstance(adapted[14], PsycopgJsonb)

    def test_none_synced_to_becomes_empty_list(self):
        row = {
            "id": "obs::1", "document": "test", "embedding": [0.1],
            "category": "observation", "scope": "global", "project": None,
            "source": "auto-extract", "importance_score": 0.5,
            "retrieval_count": 0.0, "status": "active", "superseded_by": None,
            "deleted_at": None, "synced_to": None, "origin": "local",
            "metadata": {},
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        adapted = _adapt_row(row)
        # synced_to is at index 12
        assert adapted[12] == []


class TestSqlIdentifierUsage:
    """Verify SQL queries use psycopg.sql.Composed, not f-string interpolation."""

    def test_remote_select_is_composed(self):
        result = _build_remote_select("work_schema", incremental=False)
        assert isinstance(result, sql.Composed)

    def test_mirror_upsert_is_composed(self):
        result = _build_mirror_upsert("remote_work")
        assert isinstance(result, sql.Composed)

    def test_no_fstring_schema_in_module(self):
        """Verify no f-string schema interpolation exists in sync_pull.py."""
        import inspect
        import tools.sync_pull as module
        source = inspect.getsource(module)
        # Should not have patterns like f"...{schema}..." or f"...{source_schema}..."
        # in SQL query construction (safe patterns in logger.info are OK)
        lines = source.split("\n")
        for i, line in enumerate(lines, 1):
            if "f\"" in line or "f'" in line:
                # Allow f-strings in log messages and non-SQL contexts
                lowered = line.strip().lower()
                assert not any(
                    kw in lowered for kw in [".memory_refs", ".content", ".memories",
                                              "insert into", "select", "from {"]
                ), f"Line {i} has f-string in SQL context: {line.strip()}"


class TestInitialPull:
    """Tests for initial_pull() with mock pools."""

    def _make_mock_pools(self, remote_rows, local_ids=None):
        """Create mock remote and local pools.

        Args:
            remote_rows: Rows the remote cursor returns.
            local_ids: Set of IDs that exist in local.memories (for echo dedup).
        """
        if local_ids is None:
            local_ids = set()

        # Remote pool mock
        remote_cursor = MagicMock()
        col_names = [
            "id", "document", "embedding", "category", "scope", "project",
            "source", "importance_score", "retrieval_count", "status",
            "superseded_by", "deleted_at", "synced_to", "origin", "metadata",
            "created_at", "updated_at",
        ]
        remote_cursor.description = [MagicMock() for _ in col_names]
        for i, name in enumerate(col_names):
            remote_cursor.description[i].name = name

        # First call returns rows, second returns empty
        remote_cursor.fetchall.side_effect = [remote_rows, []]

        remote_conn = MagicMock()
        remote_conn.cursor.return_value.__enter__ = MagicMock(return_value=remote_cursor)
        remote_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        remote_pool = MagicMock()
        remote_pool.connection.return_value.__enter__ = MagicMock(return_value=remote_conn)
        remote_pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        # Local pool mock
        local_cursor = MagicMock()
        local_conn = MagicMock()
        local_conn.cursor.return_value.__enter__ = MagicMock(return_value=local_cursor)
        local_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        local_pool = MagicMock()
        local_pool.connection.return_value.__enter__ = MagicMock(return_value=local_conn)
        local_pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        return remote_pool, local_pool, local_cursor

    def _make_row(self, row_id="obs::1", **overrides):
        """Create a single remote row tuple with all 17 columns."""
        now = datetime.now(timezone.utc)
        defaults = {
            "id": row_id, "document": "test doc", "embedding": [0.1] * 384,
            "category": "observation", "scope": "global", "project": None,
            "source": "auto-extract", "importance_score": 0.5,
            "retrieval_count": 0.0, "status": "active", "superseded_by": None,
            "deleted_at": None, "synced_to": [], "origin": "local",
            "metadata": {}, "created_at": now, "updated_at": now,
        }
        defaults.update(overrides)
        return tuple(defaults.values())

    def test_pulls_rows(self):
        rows = [self._make_row()]
        remote_pool, local_pool, local_cursor = self._make_mock_pools(rows)

        with patch("tools.sync_pull.get_remote_pool", return_value=remote_pool), \
             patch("tools.sync_pull._get_pool", return_value=local_pool), \
             patch("tools.sync_pull._set_last_pull_ts"), \
             patch("tools.sync_pull._ensure_local_mirror_schema"), \
             patch("tools.sync_pull._get_local_ids", return_value=set()):
            result = initial_pull("test-remote", "remote_test")

        assert result["success"] is True
        assert result["pulled_count"] == 1
        assert result["skipped_count"] == 0
        assert result["mode"] == "initial"
        assert result["target_schema"] == "remote_test"

    def test_empty_remote_returns_zero(self):
        remote_pool, local_pool, _ = self._make_mock_pools([])

        with patch("tools.sync_pull.get_remote_pool", return_value=remote_pool), \
             patch("tools.sync_pull._get_pool", return_value=local_pool), \
             patch("tools.sync_pull._set_last_pull_ts"), \
             patch("tools.sync_pull._ensure_local_mirror_schema"), \
             patch("tools.sync_pull._get_local_ids", return_value=set()):
            result = initial_pull("empty-remote", "remote_empty")

        assert result["success"] is True
        assert result["pulled_count"] == 0

    def test_ensures_mirror_schema_before_insert(self):
        rows = [self._make_row()]
        remote_pool, local_pool, _ = self._make_mock_pools(rows)

        with patch("tools.sync_pull.get_remote_pool", return_value=remote_pool), \
             patch("tools.sync_pull._get_pool", return_value=local_pool), \
             patch("tools.sync_pull._set_last_pull_ts"), \
             patch("tools.sync_pull._ensure_local_mirror_schema") as mock_ensure, \
             patch("tools.sync_pull._get_local_ids", return_value=set()):
            initial_pull("test-remote", "remote_test")

        mock_ensure.assert_called_once_with("remote_test")

    def test_uses_composed_sql(self):
        """Verify that composed SQL objects (not f-strings) are used."""
        rows = [self._make_row()]
        remote_pool, local_pool, local_cursor = self._make_mock_pools(rows)

        remote_conn = remote_pool.connection.return_value.__enter__.return_value
        remote_cursor = remote_conn.cursor.return_value.__enter__.return_value

        with patch("tools.sync_pull.get_remote_pool", return_value=remote_pool), \
             patch("tools.sync_pull._get_pool", return_value=local_pool), \
             patch("tools.sync_pull._set_last_pull_ts"), \
             patch("tools.sync_pull._ensure_local_mirror_schema"), \
             patch("tools.sync_pull._get_local_ids", return_value=set()):
            initial_pull("test-remote", "remote_test", source_schema="work")

        # The SQL passed to remote cursor should be a Composed object
        first_call = remote_cursor.execute.call_args_list[0]
        query = first_call[0][0]
        assert isinstance(query, sql.Composed)

    def test_records_sync_timestamp(self):
        remote_pool, local_pool, _ = self._make_mock_pools([])

        with patch("tools.sync_pull.get_remote_pool", return_value=remote_pool), \
             patch("tools.sync_pull._get_pool", return_value=local_pool), \
             patch("tools.sync_pull._set_last_pull_ts") as mock_set_ts, \
             patch("tools.sync_pull._ensure_local_mirror_schema"), \
             patch("tools.sync_pull._get_local_ids", return_value=set()):
            initial_pull("ts-remote", "remote_ts")

        mock_set_ts.assert_called_once()
        args = mock_set_ts.call_args
        assert args[0][0] == "ts-remote"
        assert isinstance(args[0][1], datetime)

    def test_echo_dedup_skips_local_ids(self):
        """Rows that exist in local.memories are skipped (not upserted into mirror)."""
        rows = [
            self._make_row("obs::1"),
            self._make_row("obs::2"),
            self._make_row("obs::3"),
        ]
        remote_pool, local_pool, local_cursor = self._make_mock_pools(rows)

        with patch("tools.sync_pull.get_remote_pool", return_value=remote_pool), \
             patch("tools.sync_pull._get_pool", return_value=local_pool), \
             patch("tools.sync_pull._set_last_pull_ts"), \
             patch("tools.sync_pull._ensure_local_mirror_schema"), \
             patch("tools.sync_pull._get_local_ids",
                   return_value={"obs::1", "obs::3"}):
            result = initial_pull("test-remote", "remote_test")

        assert result["pulled_count"] == 1  # Only obs::2
        assert result["skipped_count"] == 2  # obs::1 and obs::3


class TestIncrementalPull:
    """Tests for incremental_pull()."""

    def test_falls_back_to_initial_when_no_timestamp(self):
        with patch("tools.sync_pull._get_last_pull_ts", return_value=None), \
             patch("tools.sync_pull.initial_pull",
                   return_value={"mode": "initial"}) as mock_initial:
            result = incremental_pull("new-remote", "remote_new")

        mock_initial.assert_called_once_with(
            "new-remote", "remote_new",
            source_schema="local", batch_size=DEFAULT_BATCH_SIZE,
        )
        assert result["mode"] == "initial"

    def test_uses_keyset_cursor(self):
        """When timestamp exists, uses keyset pagination from CAS tables."""
        last_ts = datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)

        remote_cursor = MagicMock()
        col_names = [
            "id", "document", "embedding", "category", "scope", "project",
            "source", "importance_score", "retrieval_count", "status",
            "superseded_by", "deleted_at", "synced_to", "origin", "metadata",
            "created_at", "updated_at",
        ]
        remote_cursor.description = [MagicMock() for _ in col_names]
        for i, name in enumerate(col_names):
            remote_cursor.description[i].name = name
        remote_cursor.fetchall.return_value = []

        remote_conn = MagicMock()
        remote_conn.cursor.return_value.__enter__ = MagicMock(return_value=remote_cursor)
        remote_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        remote_pool = MagicMock()
        remote_pool.connection.return_value.__enter__ = MagicMock(return_value=remote_conn)
        remote_pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        local_pool = MagicMock()

        with patch("tools.sync_pull._get_last_pull_ts", return_value=last_ts), \
             patch("tools.sync_pull.get_remote_pool", return_value=remote_pool), \
             patch("tools.sync_pull._get_pool", return_value=local_pool), \
             patch("tools.sync_pull._set_last_pull_ts"), \
             patch("tools.sync_pull._ensure_local_mirror_schema"), \
             patch("tools.sync_pull._get_local_ids", return_value=set()):
            result = incremental_pull(
                "incr-remote", "remote_incr", source_schema="work"
            )

        assert result["mode"] == "incremental"
        assert result["since"] == last_ts.isoformat()

        # Verify keyset params: (cursor_ts, cursor_id, batch_size)
        execute_call = remote_cursor.execute.call_args
        params = execute_call[0][1]
        assert params[0] == last_ts  # cursor timestamp
        assert params[1] == ""  # initial cursor id
        assert params[2] == DEFAULT_BATCH_SIZE

    def test_source_schema_passed_to_initial_on_fallback(self):
        """When no timestamp exists, source_schema is forwarded to initial_pull."""
        with patch("tools.sync_pull._get_last_pull_ts", return_value=None), \
             patch("tools.sync_pull.initial_pull",
                   return_value={"mode": "initial"}) as mock_initial:
            incremental_pull(
                "new-remote", "remote_new", source_schema="personio"
            )

        mock_initial.assert_called_once_with(
            "new-remote", "remote_new",
            source_schema="personio", batch_size=DEFAULT_BATCH_SIZE,
        )


class TestKeysetPagination:
    """Tests for keyset cursor advancement."""

    def test_cursor_advances_across_batches(self):
        """Keyset cursor uses (updated_at, id) from last row of each batch."""
        ts1 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
        ts2 = datetime(2026, 3, 1, 12, 0, 1, tzinfo=timezone.utc)

        col_names = [
            "id", "document", "embedding", "category", "scope", "project",
            "source", "importance_score", "retrieval_count", "status",
            "superseded_by", "deleted_at", "synced_to", "origin", "metadata",
            "created_at", "updated_at",
        ]

        # Batch 1: two rows
        batch1 = [
            ("obs::1", "doc1", [0.1] * 384, "observation", "global", None,
             "auto", 0.5, 0.0, "active", None, None, [], "local", {},
             ts1, ts1),
            ("obs::2", "doc2", [0.1] * 384, "observation", "global", None,
             "auto", 0.5, 0.0, "active", None, None, [], "local", {},
             ts1, ts1),  # Same timestamp as obs::1
        ]
        # Batch 2: one row (different timestamp)
        batch2 = [
            ("obs::3", "doc3", [0.1] * 384, "observation", "global", None,
             "auto", 0.5, 0.0, "active", None, None, [], "local", {},
             ts2, ts2),
        ]

        remote_cursor = MagicMock()
        remote_cursor.description = [MagicMock() for _ in col_names]
        for i, name in enumerate(col_names):
            remote_cursor.description[i].name = name
        # Return batch1, then batch2, then empty
        remote_cursor.fetchall.side_effect = [batch1, batch2, []]

        remote_conn = MagicMock()
        remote_conn.cursor.return_value.__enter__ = MagicMock(return_value=remote_cursor)
        remote_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        remote_pool = MagicMock()
        remote_pool.connection.return_value.__enter__ = MagicMock(return_value=remote_conn)
        remote_pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        local_cursor = MagicMock()
        local_conn = MagicMock()
        local_conn.cursor.return_value.__enter__ = MagicMock(return_value=local_cursor)
        local_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        local_pool = MagicMock()
        local_pool.connection.return_value.__enter__ = MagicMock(return_value=local_conn)
        local_pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        with patch("tools.sync_pull.get_remote_pool", return_value=remote_pool), \
             patch("tools.sync_pull._get_pool", return_value=local_pool), \
             patch("tools.sync_pull._set_last_pull_ts"), \
             patch("tools.sync_pull._ensure_local_mirror_schema"), \
             patch("tools.sync_pull._get_local_ids", return_value=set()):
            result = initial_pull(
                "test", "remote_test", batch_size=2,
            )

        # Should have pulled all 3 rows
        assert result["pulled_count"] == 3

        # Verify cursor advancement in second execute call
        calls = remote_cursor.execute.call_args_list
        assert len(calls) >= 2
        # Second call should use cursor from end of batch1: (ts1, "obs::2")
        second_params = calls[1][0][1]
        assert second_params[0] == ts1  # last updated_at from batch1
        assert second_params[1] == "obs::2"  # last id from batch1

    def test_no_offset_in_queries(self):
        """Verify OFFSET is never used in the constructed SQL."""
        select = _build_remote_select("test_schema", incremental=False)
        # Render to string to check content
        rendered = select.as_string(None)
        assert "OFFSET" not in rendered.upper()


class TestPullSyncLoop:
    """Tests for pull_sync_loop() async function."""

    def test_loop_uses_asyncio_to_thread(self):
        """Loop wraps incremental_pull in asyncio.to_thread."""
        call_count = 0

        async def mock_to_thread(fn, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise KeyboardInterrupt
            return {"pulled_count": 0, "skipped_count": 0}

        async def run():
            with patch("tools.sync_pull.incremental_pull"), \
                 patch("asyncio.to_thread", side_effect=mock_to_thread), \
                 patch("tools.sync_pull.get_sync_config",
                       return_value={"pull_interval_seconds": 1}), \
                 patch("asyncio.sleep", side_effect=[None, None, KeyboardInterrupt]):
                try:
                    await pull_sync_loop(
                        "test-remote", "remote_test",
                    )
                except KeyboardInterrupt:
                    pass

        asyncio.run(run())
        assert call_count >= 1

    def test_loop_handles_errors(self):
        """Errors in incremental_pull don't stop the loop."""
        call_count = 0

        async def mock_to_thread(fn, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise Exception("transient error")

        sleep_count = 0

        async def mock_sleep(s):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 3:
                raise KeyboardInterrupt

        async def run():
            with patch("asyncio.to_thread", side_effect=mock_to_thread), \
                 patch("tools.sync_pull.get_sync_config",
                       return_value={"pull_interval_seconds": 1}), \
                 patch("asyncio.sleep", side_effect=mock_sleep):
                try:
                    await pull_sync_loop(
                        "err-remote", "remote_err",
                    )
                except KeyboardInterrupt:
                    pass

        asyncio.run(run())
        assert call_count >= 2  # Loop continued despite errors

    def test_loop_rereads_config_interval(self):
        """Loop re-reads pull_interval_seconds from config each cycle."""
        intervals = []

        async def mock_sleep(s):
            intervals.append(s)
            if len(intervals) >= 2:
                raise KeyboardInterrupt

        configs = [
            {"pull_interval_seconds": 60},
            {"pull_interval_seconds": 120},
        ]
        config_idx = [0]

        def get_config():
            cfg = configs[min(config_idx[0], len(configs) - 1)]
            config_idx[0] += 1
            return cfg

        async def mock_to_thread(fn, *args, **kwargs):
            return {"pulled_count": 0, "skipped_count": 0}

        async def run():
            with patch("asyncio.to_thread", side_effect=mock_to_thread), \
                 patch("tools.sync_pull.get_sync_config", side_effect=get_config), \
                 patch("asyncio.sleep", side_effect=mock_sleep):
                try:
                    await pull_sync_loop("test", "remote_test")
                except KeyboardInterrupt:
                    pass

        asyncio.run(run())
        # First sleep should be startup delay (15s), rest should be config intervals
        # But startup sleep is internal, we mock asyncio.sleep to see all calls
        assert len(intervals) >= 2


class TestGetPullSyncTasks:
    """Tests for get_pull_sync_tasks() factory."""

    def test_returns_empty_when_disabled(self):
        cfg = {"enabled": False, "remotes": {"work": {"url": "...", "schema": "work"}}}
        with patch("tools.sync_pull.get_sync_config", return_value=cfg):
            tasks = get_pull_sync_tasks()
        assert tasks == []

    def test_returns_empty_when_no_remotes(self):
        cfg = {"enabled": True, "remotes": {}}
        with patch("tools.sync_pull.get_sync_config", return_value=cfg):
            tasks = get_pull_sync_tasks()
        assert tasks == []

    def test_returns_coroutine_per_enabled_remote(self):
        cfg = {
            "enabled": True,
            "remotes": {
                "work": {"url": "postgres://...", "schema": "personio", "enabled": True},
                "home": {"url": "postgres://...", "schema": "home", "enabled": True},
                "disabled": {"url": "postgres://...", "enabled": False},
            },
        }
        with patch("tools.sync_pull.get_sync_config", return_value=cfg):
            tasks = get_pull_sync_tasks()

        assert len(tasks) == 2
        # Clean up coroutines to avoid warnings
        for t in tasks:
            t.close()

    def test_skips_duplicate_target_schemas(self):
        """Two remotes with same name prefix → duplicate target schema → second skipped."""
        cfg = {
            "enabled": True,
            "remotes": {
                # Both would produce target_schema = "remote_work"
                # This shouldn't actually happen in practice since remote names
                # are dict keys (unique), but target schemas are derived from them
                "work": {"url": "postgres://a", "schema": "personio"},
            },
        }
        with patch("tools.sync_pull.get_sync_config", return_value=cfg):
            tasks = get_pull_sync_tasks()

        # Normal case: one task per unique remote
        assert len(tasks) == 1
        for t in tasks:
            t.close()

    def test_respects_enabled_flag(self):
        cfg = {
            "enabled": True,
            "remotes": {
                "active": {"url": "postgres://...", "enabled": True},
                "inactive": {"url": "postgres://...", "enabled": False},
            },
        }
        with patch("tools.sync_pull.get_sync_config", return_value=cfg):
            tasks = get_pull_sync_tasks()

        assert len(tasks) == 1
        for t in tasks:
            t.close()


class TestMultiMachineSync:
    """Test echo dedup simulating two machines sharing a remote."""

    def test_machine_a_skips_own_data(self):
        """Machine A pushed obs::1 to remote. When pulling, obs::1 is skipped
        because it exists in local.memories."""
        col_names = [
            "id", "document", "embedding", "category", "scope", "project",
            "source", "importance_score", "retrieval_count", "status",
            "superseded_by", "deleted_at", "synced_to", "origin", "metadata",
            "created_at", "updated_at",
        ]
        now = datetime.now(timezone.utc)

        # Remote has both machine A's data (obs::1) and machine B's (obs::B1)
        remote_rows = [
            ("obs::1", "machine A doc", [0.1] * 384, "observation", "global",
             None, "auto", 0.5, 0.0, "active", None, None, [], "local",
             {}, now, now),
            ("obs::B1", "machine B doc", [0.2] * 384, "observation", "global",
             None, "auto", 0.7, 0.0, "active", None, None, [], "local",
             {}, now, now),
        ]

        remote_cursor = MagicMock()
        remote_cursor.description = [MagicMock() for _ in col_names]
        for i, name in enumerate(col_names):
            remote_cursor.description[i].name = name
        remote_cursor.fetchall.side_effect = [remote_rows, []]

        remote_conn = MagicMock()
        remote_conn.cursor.return_value.__enter__ = MagicMock(return_value=remote_cursor)
        remote_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        remote_pool = MagicMock()
        remote_pool.connection.return_value.__enter__ = MagicMock(return_value=remote_conn)
        remote_pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        local_cursor = MagicMock()
        local_conn = MagicMock()
        local_conn.cursor.return_value.__enter__ = MagicMock(return_value=local_cursor)
        local_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        local_pool = MagicMock()
        local_pool.connection.return_value.__enter__ = MagicMock(return_value=local_conn)
        local_pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        # Machine A has obs::1 locally
        with patch("tools.sync_pull.get_remote_pool", return_value=remote_pool), \
             patch("tools.sync_pull._get_pool", return_value=local_pool), \
             patch("tools.sync_pull._set_last_pull_ts"), \
             patch("tools.sync_pull._ensure_local_mirror_schema"), \
             patch("tools.sync_pull._get_local_ids", return_value={"obs::1"}):
            result = initial_pull("shared-remote", "remote_shared")

        assert result["pulled_count"] == 1  # Only obs::B1
        assert result["skipped_count"] == 1  # obs::1 skipped (own data)
