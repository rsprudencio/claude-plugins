"""Tests for bin/setup_replication.py with mocked psycopg connections."""

from unittest.mock import MagicMock, patch, call
import sys
import os

# Ensure bin/ is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


class TestPgVersionDetection:
    """Tests for _detect_pg_version."""

    def test_pg_version_17(self):
        from setup_replication import _detect_pg_version

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchone.return_value = ("17.2",)

        version = _detect_pg_version(mock_conn)
        assert version == 17

    def test_pg_version_18(self):
        from setup_replication import _detect_pg_version

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchone.return_value = ("18.0",)

        version = _detect_pg_version(mock_conn)
        assert version == 18


class TestCentralSetup:
    """Tests for setup_central with mocked connections."""

    @patch("setup_replication._get_connection")
    def test_central_ddl_execution(self, mock_get_conn):
        from setup_replication import setup_central

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        # _detect_pg_version
        mock_cur.fetchone.return_value = ("17.2",)
        mock_get_conn.return_value = mock_conn

        setup_central("postgresql://test:test@localhost/jarvis")

        # Verify cursor.execute was called multiple times (user, grant, publication)
        assert mock_cur.execute.call_count >= 3
        mock_conn.close.assert_called_once()

        # Check that DDL was executed (verify key SQL fragments)
        all_sql = " ".join(
            str(c.args[0]) for c in mock_cur.execute.call_args_list
            if c.args
        )
        assert "pg_roles" in all_sql  # user creation check
        assert "GRANT" in all_sql
        assert "jarvis_pub" in all_sql


class TestLocalSetup:
    """Tests for setup_local with mocked connections."""

    @patch("setup_replication._get_connection")
    def test_local_subscription_creation(self, mock_get_conn):
        from setup_replication import setup_local

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        # Sequence of fetchone calls:
        # 1) _detect_pg_version -> ("17.2",)
        # 2) subscription exists check -> None (doesn't exist yet)
        mock_cur.fetchone.side_effect = [("17.2",), None]
        mock_get_conn.return_value = mock_conn

        setup_local(
            "postgresql://test:test@localhost/jarvis",
            "postgresql://central:central@central-host/jarvis",
        )

        all_sql = " ".join(
            str(c.args[0]) for c in mock_cur.execute.call_args_list
            if c.args
        )
        assert "jarvis_local_pub" in all_sql
        assert "central_sub" in all_sql
        assert "SUBSCRIPTION" in all_sql
        mock_conn.close.assert_called_once()

    @patch("setup_replication._get_connection")
    def test_local_skips_existing_subscription(self, mock_get_conn):
        """If central_sub already exists, don't recreate it."""
        from setup_replication import setup_local

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        # subscription exists check -> returns a row (already exists)
        mock_cur.fetchone.side_effect = [("17.2",), (1,)]
        mock_get_conn.return_value = mock_conn

        setup_local(
            "postgresql://test:test@localhost/jarvis",
            "postgresql://central:central@central-host/jarvis",
        )

        all_sql = " ".join(
            str(c.args[0]) for c in mock_cur.execute.call_args_list
            if c.args
        )
        # Should create local publication but NOT create subscription
        assert "jarvis_local_pub" in all_sql
        assert "CREATE SUBSCRIPTION" not in all_sql
