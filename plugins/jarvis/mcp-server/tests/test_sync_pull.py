"""Tests for pull sync engine."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

from tools.sync_pull import (
    _meta_key,
    _get_last_pull_ts,
    _set_last_pull_ts,
    initial_pull,
    incremental_pull,
    pull_sync_loop,
    DEFAULT_BATCH_SIZE,
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


class TestInitialPull:
    """Tests for initial_pull() with mock pools."""

    def _make_mock_pools(self, remote_rows):
        """Create mock remote and local pools that simulate cursor behavior."""
        # Remote pool mock
        remote_cursor = MagicMock()
        remote_cursor.description = [
            MagicMock(name="id"), MagicMock(name="document"),
            MagicMock(name="embedding"), MagicMock(name="category"),
            MagicMock(name="scope"), MagicMock(name="project"),
            MagicMock(name="source"), MagicMock(name="importance_score"),
            MagicMock(name="retrieval_count"), MagicMock(name="status"),
            MagicMock(name="superseded_by"), MagicMock(name="metadata"),
            MagicMock(name="created_at"), MagicMock(name="updated_at"),
        ]
        # Set the .name attribute on each mock description
        for i, name in enumerate(["id", "document", "embedding", "category",
                                   "scope", "project", "source", "importance_score",
                                   "retrieval_count", "status", "superseded_by",
                                   "metadata", "created_at", "updated_at"]):
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

    def test_pulls_rows(self):
        rows = [
            ("obs::1", "test doc", [0.1] * 384, "observation", "global", None,
             "auto-extract", 0.5, 0.0, "active", None, {},
             datetime.now(timezone.utc), datetime.now(timezone.utc)),
        ]
        remote_pool, local_pool, local_cursor = self._make_mock_pools(rows)

        with patch("tools.sync_pull.get_remote_pool", return_value=remote_pool), \
             patch("tools.sync_pull._get_pool", return_value=local_pool), \
             patch("tools.sync_pull._set_last_pull_ts"):
            result = initial_pull("test-remote", "remote_test")

        assert result["success"] is True
        assert result["pulled_count"] == 1
        assert result["mode"] == "initial"
        assert result["target_schema"] == "remote_test"

    def test_empty_remote_returns_zero(self):
        remote_pool, local_pool, _ = self._make_mock_pools([])

        with patch("tools.sync_pull.get_remote_pool", return_value=remote_pool), \
             patch("tools.sync_pull._get_pool", return_value=local_pool), \
             patch("tools.sync_pull._set_last_pull_ts"):
            result = initial_pull("empty-remote", "remote_empty")

        assert result["success"] is True
        assert result["pulled_count"] == 0

    def test_records_sync_timestamp(self):
        remote_pool, local_pool, _ = self._make_mock_pools([])

        with patch("tools.sync_pull.get_remote_pool", return_value=remote_pool), \
             patch("tools.sync_pull._get_pool", return_value=local_pool), \
             patch("tools.sync_pull._set_last_pull_ts") as mock_set_ts:
            initial_pull("ts-remote", "remote_ts")

        mock_set_ts.assert_called_once()
        args = mock_set_ts.call_args
        assert args[0][0] == "ts-remote"
        assert isinstance(args[0][1], datetime)


class TestIncrementalPull:
    """Tests for incremental_pull()."""

    def test_falls_back_to_initial_when_no_timestamp(self):
        with patch("tools.sync_pull._get_last_pull_ts", return_value=None), \
             patch("tools.sync_pull.initial_pull", return_value={"mode": "initial"}) as mock_initial:
            result = incremental_pull("new-remote", "remote_new")

        mock_initial.assert_called_once_with(
            "new-remote", "remote_new", batch_size=DEFAULT_BATCH_SIZE
        )
        assert result["mode"] == "initial"

    def test_uses_timestamp_filter(self):
        """When timestamp exists, fetches only newer rows."""
        last_ts = datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)

        remote_cursor = MagicMock()
        remote_cursor.description = [MagicMock(name=n) for n in
            ["id", "document", "embedding", "category", "scope", "project",
             "source", "importance_score", "retrieval_count", "status",
             "superseded_by", "metadata", "created_at", "updated_at"]]
        for i, name in enumerate(["id", "document", "embedding", "category",
                                   "scope", "project", "source", "importance_score",
                                   "retrieval_count", "status", "superseded_by",
                                   "metadata", "created_at", "updated_at"]):
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
             patch("tools.sync_pull._set_last_pull_ts"):
            result = incremental_pull("incr-remote", "remote_incr")

        assert result["mode"] == "incremental"
        assert result["since"] == last_ts.isoformat()
        # Verify the SQL used the timestamp parameter
        execute_call = remote_cursor.execute.call_args
        assert last_ts in execute_call[0][1]  # timestamp in params


class TestPullSyncLoop:
    """Tests for pull_sync_loop() async function."""

    def test_loop_calls_incremental(self):
        """Loop calls incremental_pull and sleeps."""
        call_count = 0

        def mock_incremental(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise KeyboardInterrupt  # Stop the loop
            return {"pulled_count": 0}

        async def run():
            with patch("tools.sync_pull.incremental_pull", side_effect=mock_incremental), \
                 patch("asyncio.sleep", side_effect=[None, KeyboardInterrupt]):
                try:
                    await pull_sync_loop(
                        "test-remote", "remote_test", interval_seconds=1
                    )
                except KeyboardInterrupt:
                    pass

        asyncio.run(run())
        assert call_count >= 1

    def test_loop_handles_errors(self):
        """Errors in incremental_pull don't stop the loop."""
        call_count = 0

        async def run():
            nonlocal call_count

            def mock_incremental(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                raise Exception("transient error")

            sleep_count = 0

            async def mock_sleep(s):
                nonlocal sleep_count
                sleep_count += 1
                if sleep_count >= 2:
                    raise KeyboardInterrupt

            with patch("tools.sync_pull.incremental_pull", side_effect=mock_incremental), \
                 patch("asyncio.sleep", side_effect=mock_sleep):
                try:
                    await pull_sync_loop(
                        "err-remote", "remote_err", interval_seconds=1
                    )
                except KeyboardInterrupt:
                    pass

        asyncio.run(run())
        assert call_count >= 2  # Loop continued despite errors
