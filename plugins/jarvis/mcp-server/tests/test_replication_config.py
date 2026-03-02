"""Tests for get_replication_config."""

import os


class TestReplicationConfig:
    """Tests for replication configuration getter."""

    def test_defaults_disabled(self, mock_config):
        """Default config has mode=disabled."""
        from tools.config import get_replication_config

        cfg = get_replication_config()
        assert cfg["mode"] == "disabled"
        assert cfg["central_url"] == ""
        assert cfg["node_id"] == ""
        assert cfg["publications"] == ["jarvis_pub"]
        assert cfg["replication_user"] == "jarvis_repl"

    def test_env_override_mode(self, mock_config, monkeypatch):
        """JARVIS_REPLICATION_MODE env var overrides config."""
        from tools.config import get_replication_config

        monkeypatch.setenv("JARVIS_REPLICATION_MODE", "central")
        cfg = get_replication_config()
        assert cfg["mode"] == "central"

    def test_env_override_central_url(self, mock_config, monkeypatch):
        """JARVIS_CENTRAL_URL env var overrides config."""
        from tools.config import get_replication_config

        monkeypatch.setenv("JARVIS_CENTRAL_URL", "postgresql://central:5432/jarvis")
        cfg = get_replication_config()
        assert cfg["central_url"] == "postgresql://central:5432/jarvis"

    def test_config_file_override(self, mock_config):
        """Config file values override defaults."""
        import json
        from tools.config import get_replication_config

        data = json.loads(mock_config.path.read_text())
        data.setdefault("memory", {})["replication"] = {
            "mode": "local",
            "central_url": "postgresql://remote-host:5432/jarvis",
            "node_id": "laptop-1",
        }
        mock_config.path.write_text(json.dumps(data))
        import jarvis_common.config as cc
        cc._config_cache = None
        import tools.config as tc
        tc._config_cache = None

        cfg = get_replication_config()
        assert cfg["mode"] == "local"
        assert cfg["central_url"] == "postgresql://remote-host:5432/jarvis"
        assert cfg["node_id"] == "laptop-1"
        # Defaults still present for unset keys
        assert cfg["publications"] == ["jarvis_pub"]

    def test_env_precedence_over_config(self, mock_config, monkeypatch):
        """Env vars take precedence over config file values."""
        import json
        from tools.config import get_replication_config

        data = json.loads(mock_config.path.read_text())
        data.setdefault("memory", {})["replication"] = {
            "mode": "local",
            "central_url": "postgresql://config-host:5432/jarvis",
        }
        mock_config.path.write_text(json.dumps(data))
        import jarvis_common.config as cc
        cc._config_cache = None
        import tools.config as tc
        tc._config_cache = None

        monkeypatch.setenv("JARVIS_REPLICATION_MODE", "central")
        monkeypatch.setenv("JARVIS_CENTRAL_URL", "postgresql://env-host:5432/jarvis")

        cfg = get_replication_config()
        assert cfg["mode"] == "central"
        assert cfg["central_url"] == "postgresql://env-host:5432/jarvis"
