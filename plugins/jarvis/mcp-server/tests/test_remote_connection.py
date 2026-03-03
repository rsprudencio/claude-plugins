"""Tests for remote connection pool management."""

import pytest
from unittest.mock import patch, MagicMock

from tools.remote_connection import (
    RemoteConfig,
    _build_conninfo,
    _iam_password,
    get_remote_pool,
    close_remote,
    close_all_remotes,
    list_remotes,
    _remote_pools,
)


@pytest.fixture(autouse=True)
def clean_pools():
    """Reset pool state before each test."""
    import tools.remote_connection as mod
    mod._remote_pools = {}
    yield
    mod._remote_pools = {}


class TestRemoteConfig:
    """Tests for RemoteConfig dataclass."""

    def test_defaults(self):
        cfg = RemoteConfig(name="test", host="db.example.com")
        assert cfg.port == 5432
        assert cfg.database == "jarvis"
        assert cfg.user == "jarvis"
        assert cfg.auth_method == "password"
        assert cfg.sslmode == "verify-full"
        assert cfg.max_lifetime == 600

    def test_iam_config(self):
        cfg = RemoteConfig(
            name="aurora",
            host="aurora.cluster.us-east-1.rds.amazonaws.com",
            auth_method="iam",
            region="us-east-1",
        )
        assert cfg.auth_method == "iam"
        assert cfg.region == "us-east-1"

    def test_mtls_config(self):
        cfg = RemoteConfig(
            name="mtls",
            host="secure.example.com",
            auth_method="mtls",
            sslcert="/certs/client.crt",
            sslkey="/certs/client.key",
            sslrootcert="/certs/ca.crt",
        )
        assert cfg.auth_method == "mtls"
        assert cfg.sslcert == "/certs/client.crt"


class TestBuildConninfo:
    """Tests for _build_conninfo()."""

    def test_password_conninfo(self):
        cfg = RemoteConfig(
            name="test",
            host="db.example.com",
            port=5432,
            database="jarvis",
            user="jarvis",
            password="secret123",
        )
        conninfo = _build_conninfo(cfg)
        assert "host=db.example.com" in conninfo
        assert "port=5432" in conninfo
        assert "dbname=jarvis" in conninfo
        assert "user=jarvis" in conninfo
        assert "password='secret123'" in conninfo
        assert "sslmode=verify-full" in conninfo

    def test_mtls_conninfo(self):
        cfg = RemoteConfig(
            name="mtls",
            host="secure.example.com",
            auth_method="mtls",
            sslcert="/certs/client.crt",
            sslkey="/certs/client.key",
            sslrootcert="/certs/ca.crt",
        )
        conninfo = _build_conninfo(cfg)
        assert "password" not in conninfo
        assert "sslcert=/certs/client.crt" in conninfo
        assert "sslkey=/certs/client.key" in conninfo
        assert "sslrootcert=/certs/ca.crt" in conninfo

    def test_iam_conninfo(self):
        """IAM auth generates a token as password."""
        cfg = RemoteConfig(
            name="aurora",
            host="aurora.cluster.rds.amazonaws.com",
            auth_method="iam",
            user="iam_user",
            region="us-east-1",
        )
        with patch("tools.remote_connection._iam_password", return_value="iam-token-123"):
            conninfo = _build_conninfo(cfg)
        assert "password='iam-token-123'" in conninfo

    def test_password_escaping(self):
        cfg = RemoteConfig(
            name="test",
            host="db.example.com",
            password="pass'word",
        )
        conninfo = _build_conninfo(cfg)
        assert "pass\\'word" in conninfo

    def test_no_sslrootcert_when_none(self):
        cfg = RemoteConfig(name="test", host="db.example.com", password="pw")
        conninfo = _build_conninfo(cfg)
        assert "sslrootcert" not in conninfo


class TestIamPassword:
    """Tests for IAM token generation."""

    def test_iam_generates_token(self):
        mock_client = MagicMock()
        mock_client.generate_db_auth_token.return_value = "generated-token-abc"

        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_client

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            token = _iam_password(
                host="aurora.rds.amazonaws.com",
                port=5432,
                user="iam_user",
                region="us-east-1",
            )

        assert token == "generated-token-abc"
        mock_boto3.client.assert_called_once_with("rds", region_name="us-east-1")
        mock_client.generate_db_auth_token.assert_called_once_with(
            DBHostname="aurora.rds.amazonaws.com",
            Port=5432,
            DBUsername="iam_user",
            Region="us-east-1",
        )

    def test_iam_missing_boto3(self):
        with patch.dict("sys.modules", {"boto3": None}):
            # Re-import to trigger ImportError
            import importlib
            import tools.remote_connection as mod
            # Force ImportError by patching the import
            with patch("builtins.__import__", side_effect=ImportError("No module named 'boto3'")):
                with pytest.raises(RuntimeError, match="boto3 is required"):
                    _iam_password("host", 5432, "user")

    def test_iam_boto3_error(self):
        mock_client = MagicMock()
        mock_client.generate_db_auth_token.side_effect = Exception("AWS error")

        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_client

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            with pytest.raises(RuntimeError, match="Failed to generate IAM"):
                _iam_password("host", 5432, "user")


class TestPoolLifecycle:
    """Tests for pool creation and cleanup."""

    def test_close_remote_nonexistent(self):
        result = close_remote("nonexistent")
        assert result is False

    def test_close_all_empty(self):
        result = close_all_remotes()
        assert result == 0

    def test_list_remotes_empty(self):
        assert list_remotes() == []

    def test_close_all_with_mock_pools(self):
        import tools.remote_connection as mod
        mock_pool1 = MagicMock()
        mock_pool2 = MagicMock()
        mod._remote_pools = {"r1": mock_pool1, "r2": mock_pool2}

        count = close_all_remotes()
        assert count == 2
        mock_pool1.close.assert_called_once()
        mock_pool2.close.assert_called_once()
        assert list_remotes() == []

    def test_close_remote_with_mock_pool(self):
        import tools.remote_connection as mod
        mock_pool = MagicMock()
        mod._remote_pools = {"test": mock_pool}

        result = close_remote("test")
        assert result is True
        mock_pool.close.assert_called_once()
        assert "test" not in mod._remote_pools

    def test_close_remote_handles_close_error(self):
        import tools.remote_connection as mod
        mock_pool = MagicMock()
        mock_pool.close.side_effect = Exception("close error")
        mod._remote_pools = {"test": mock_pool}

        # Should not raise
        result = close_remote("test")
        assert result is True
        assert "test" not in mod._remote_pools


class TestGetRemotePool:
    """Tests for get_remote_pool() with mocked config."""

    def test_missing_remote_raises(self):
        with patch("tools.remote_connection._load_remote_config",
                    side_effect=KeyError("not configured")):
            with pytest.raises(KeyError):
                get_remote_pool("nonexistent")

    def test_pool_cached(self):
        """Second call returns same pool."""
        import tools.remote_connection as mod
        mock_pool = MagicMock()
        mock_pool.closed = False
        mod._remote_pools = {"cached": mock_pool}

        result = get_remote_pool("cached")
        assert result is mock_pool

    def test_pool_recreated_if_closed(self):
        """Closed pools are replaced."""
        import tools.remote_connection as mod
        old_pool = MagicMock()
        old_pool.closed = True
        mod._remote_pools = {"stale": old_pool}

        new_pool = MagicMock()

        with patch("tools.remote_connection._load_remote_config") as mock_load, \
             patch("tools.remote_connection._create_pool", return_value=new_pool):
            mock_load.return_value = RemoteConfig(name="stale", host="h")
            result = get_remote_pool("stale")

        assert result is new_pool
        assert mod._remote_pools["stale"] is new_pool
