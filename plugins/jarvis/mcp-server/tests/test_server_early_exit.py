"""Tests for server early-exit when MCP transport is not 'local'."""
import pytest


class TestMainSyncEarlyExit:
    """Tests for main_sync() transport-based early exit."""

    def test_exits_when_transport_is_container(self, mock_config, monkeypatch):
        """main_sync() should sys.exit(0) when transport is 'container'."""
        mock_config.set(mcp_transport="container")

        with pytest.raises(SystemExit) as exc_info:
            from server import main_sync
            main_sync()

        assert exc_info.value.code == 0

    def test_exits_when_transport_is_remote(self, mock_config, monkeypatch):
        """main_sync() should sys.exit(0) when transport is 'remote'."""
        mock_config.set(mcp_transport="remote")

        with pytest.raises(SystemExit) as exc_info:
            from server import main_sync
            main_sync()

        assert exc_info.value.code == 0
