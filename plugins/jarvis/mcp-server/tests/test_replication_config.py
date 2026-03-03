"""Tests for get_sync_config (replaced get_replication_config in Phase 7)."""

import os


class TestSyncConfig:
    """Tests for multi-remote sync configuration getter."""

    def test_defaults_disabled(self, mock_config):
        """Default config has enabled=false."""
        from tools.config import get_sync_config

        cfg = get_sync_config()
        assert cfg["enabled"] is False
        assert cfg["strategy"] == "first-match"
        assert cfg["default_action"] == "local-only"
        assert cfg["worker_interval_seconds"] == 30
        assert cfg["remotes"] == {}
        assert cfg["rules"] == []
        assert cfg["project_groups"] == {}

    def test_env_override_enabled(self, mock_config, monkeypatch):
        """JARVIS_SYNC_ENABLED env var overrides config."""
        from tools.config import get_sync_config

        monkeypatch.setenv("JARVIS_SYNC_ENABLED", "true")
        cfg = get_sync_config()
        assert cfg["enabled"] is True

    def test_env_override_enabled_numeric(self, mock_config, monkeypatch):
        """JARVIS_SYNC_ENABLED=1 is truthy."""
        from tools.config import get_sync_config

        monkeypatch.setenv("JARVIS_SYNC_ENABLED", "1")
        cfg = get_sync_config()
        assert cfg["enabled"] is True

    def test_env_override_enabled_false(self, mock_config, monkeypatch):
        """JARVIS_SYNC_ENABLED=false disables sync."""
        from tools.config import get_sync_config

        monkeypatch.setenv("JARVIS_SYNC_ENABLED", "false")
        cfg = get_sync_config()
        assert cfg["enabled"] is False

    def test_env_override_strategy(self, mock_config, monkeypatch):
        """JARVIS_SYNC_STRATEGY env var overrides config."""
        from tools.config import get_sync_config

        monkeypatch.setenv("JARVIS_SYNC_STRATEGY", "all-match")
        cfg = get_sync_config()
        assert cfg["strategy"] == "all-match"

    def test_config_file_override(self, mock_config):
        """Config file values override defaults."""
        import json
        from tools.config import get_sync_config

        data = json.loads(mock_config.path.read_text())
        data.setdefault("memory", {})["sync"] = {
            "enabled": True,
            "strategy": "all-match",
            "remotes": {
                "work": {"url": "postgresql://work-host:5432/jarvis"}
            },
        }
        mock_config.path.write_text(json.dumps(data))
        import jarvis_common.config as cc
        cc._config_cache = None
        import tools.config as tc
        tc._config_cache = None

        cfg = get_sync_config()
        assert cfg["enabled"] is True
        assert cfg["strategy"] == "all-match"
        assert "work" in cfg["remotes"]
        # Defaults still present for unset keys
        assert cfg["default_action"] == "local-only"
        assert cfg["worker_interval_seconds"] == 30

    def test_env_precedence_over_config(self, mock_config, monkeypatch):
        """Env vars take precedence over config file values."""
        import json
        from tools.config import get_sync_config

        data = json.loads(mock_config.path.read_text())
        data.setdefault("memory", {})["sync"] = {
            "enabled": True,
            "strategy": "first-match",
        }
        mock_config.path.write_text(json.dumps(data))
        import jarvis_common.config as cc
        cc._config_cache = None
        import tools.config as tc
        tc._config_cache = None

        monkeypatch.setenv("JARVIS_SYNC_ENABLED", "false")
        monkeypatch.setenv("JARVIS_SYNC_STRATEGY", "all-match")

        cfg = get_sync_config()
        assert cfg["enabled"] is False
        assert cfg["strategy"] == "all-match"
