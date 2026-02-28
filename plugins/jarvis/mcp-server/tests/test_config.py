"""Tests for config verification."""

import pytest
from tools.config import verify_config, get_verified_vault_path


class TestVerifyConfig:
    """Tests for verify_config function."""

    def test_valid_config_passes(self, mock_config):
        """Valid config with vault_confirmed should pass."""
        valid, error = verify_config()
        assert valid is True
        assert error == ""

    def test_missing_vault_confirmed_fails(self, unconfirmed_config):
        """Config without vault_confirmed should fail."""
        valid, error = verify_config()
        assert valid is False
        assert "not confirmed" in error.lower()
        assert "jarvis-settings" in error.lower()

    def test_missing_config_file_fails(self, no_config):
        """Missing config file should fail."""
        valid, error = verify_config()
        assert valid is False
        assert "no vault_path" in error.lower()

    def test_missing_vault_path_fails(self, mock_config):
        """Config without vault_path should fail."""
        mock_config.delete_key("vault_path")
        valid, error = verify_config()
        assert valid is False
        assert "no vault_path" in error.lower()

    def test_nonexistent_vault_directory_fails(self, mock_config):
        """Config pointing to nonexistent directory should fail."""
        mock_config.set(vault_path="/nonexistent/path/12345")
        valid, error = verify_config()
        assert valid is False
        assert "not found" in error.lower()

    def test_vault_confirmed_false_fails(self, mock_config):
        """vault_confirmed: false should fail."""
        mock_config.set(vault_confirmed=False)
        valid, error = verify_config()
        assert valid is False
        assert "not confirmed" in error.lower()


class TestGetVerifiedVaultPath:
    """Tests for get_verified_vault_path function."""

    def test_returns_path_when_valid(self, mock_config):
        """Should return vault path when config is valid."""
        path, error = get_verified_vault_path()
        assert error == ""
        assert path == str(mock_config.vault_path)

    def test_returns_error_when_invalid(self, unconfirmed_config):
        """Should return error when config is invalid."""
        path, error = get_verified_vault_path()
        assert path == ""
        assert "not confirmed" in error.lower()

    def test_expands_home_directory(self, mock_config):
        """Should expand ~ in vault path."""
        import os

        home = os.path.expanduser("~")
        mock_config.set(vault_path="~/test_vault_12345")

        # Create the directory temporarily
        test_path = os.path.join(home, "test_vault_12345")
        os.makedirs(test_path, exist_ok=True)

        try:
            path, error = get_verified_vault_path()
            assert error == ""
            assert path == test_path
            assert "~" not in path
        finally:
            os.rmdir(test_path)


class TestConfigCaching:
    """Tests for config caching behavior."""

    def test_config_cached_after_first_load(self, mock_config):
        """Config should be cached after first load."""
        from tools import config as config_module

        # Clear cache
        config_module.clear_config_cache()

        # First load
        config1 = config_module.get_config()

        # Second load should return same object
        config2 = config_module.get_config()

        assert config1 is config2

    def test_invalid_json_returns_empty_dict(self, tmp_path, monkeypatch):
        """Invalid JSON should be handled gracefully."""
        from tools import config as config_module
        import jarvis_common.config as common_config_module

        # Create config with invalid JSON
        config_dir = tmp_path / ".jarvis"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "config.json"
        config_file.write_text("{invalid json")

        # Mock home to point to tmp_path
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        # Clear cache (canonical is jarvis_common, clear_config_cache handles it)
        config_module.clear_config_cache()

        # Should raise JSONDecodeError
        with pytest.raises(Exception):  # json.JSONDecodeError
            config_module.get_config()


class TestGetVaultPath:
    """Tests for get_vault_path without verification."""

    def test_returns_vault_path_when_configured(self, mock_config):
        """Should return vault_path from config."""
        from tools.config import get_vault_path

        path = get_vault_path()
        assert path == str(mock_config.vault_path)

    def test_returns_cwd_when_not_configured(self, no_config):
        """Should fall back to cwd when vault_path not configured."""
        from tools.config import get_vault_path
        import os

        path = get_vault_path()
        assert path == os.getcwd()


class TestGetDebugInfo:
    """Tests for get_debug_info diagnostics."""

    def test_returns_all_diagnostic_fields(self, mock_config):
        """Should return complete diagnostic information."""
        from tools.config import get_debug_info

        info = get_debug_info()

        # Check all expected fields present
        assert "config_path" in info
        assert "config_exists" in info
        assert "config_contents" in info
        assert "resolved_vault_path" in info
        assert "cwd" in info
        assert "home" in info

        # Check field types
        assert isinstance(info["config_path"], str)
        assert isinstance(info["config_exists"], bool)
        assert isinstance(info["config_contents"], dict)
        assert isinstance(info["resolved_vault_path"], str)
        assert isinstance(info["cwd"], str)
        assert isinstance(info["home"], str)

    def test_shows_config_exists_true_when_configured(self, mock_config, monkeypatch):
        """Should show config_exists=True when config file present."""
        from tools.config import get_debug_info

        # Create config at the path get_debug_info will check
        config_dir = mock_config.vault_path.parent / ".jarvis"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.json").write_text('{"vault_path": "test"}')
        monkeypatch.setattr("pathlib.Path.home", lambda: mock_config.vault_path.parent)

        info = get_debug_info()
        assert info["config_exists"] is True

    def test_shows_empty_config_when_not_configured(self, no_config):
        """Should show empty config_contents when not configured."""
        from tools.config import get_debug_info

        info = get_debug_info()
        # Config file may exist but be empty/minimal
        assert isinstance(info["config_contents"], dict)
        # Should not have vault_path configured
        assert info["config_contents"].get("vault_path") is None


class TestWorklogConfig:
    """Tests for worklog configuration getter."""

    def test_defaults(self, mock_config):
        """Should return defaults when not configured."""
        from tools.config import get_worklog_config

        config = get_worklog_config()
        assert config["enabled"] is True
        assert config["dedup_threshold"] == 0.7

    def test_override_from_config(self, mock_config):
        """Should merge user config over defaults."""
        from tools.config import get_worklog_config

        mock_config.set(memory={"worklog": {"enabled": False, "dedup_threshold": 0.8}})
        config = get_worklog_config()
        assert config["enabled"] is False
        assert config["dedup_threshold"] == 0.8


class TestEnvVarOverrides:
    """Tests for JARVIS_HOME and JARVIS_VAULT_PATH environment variable overrides."""

    def test_jarvis_home_overrides_config_path(self, tmp_path, monkeypatch):
        """JARVIS_HOME env var should override default ~/.jarvis config path."""
        from tools import config as config_module

        config_module.clear_config_cache()

        # Create config in custom JARVIS_HOME
        jarvis_home = tmp_path / "custom_jarvis"
        jarvis_home.mkdir()
        config_file = jarvis_home / "config.json"
        config_file.write_text(
            '{"vault_path": "/custom/vault", "vault_confirmed": true}'
        )

        monkeypatch.setenv("JARVIS_HOME", str(jarvis_home))

        config = config_module.get_config()
        assert config["vault_path"] == "/custom/vault"

        config_module.clear_config_cache()

    def test_jarvis_vault_path_overrides_config(
        self, tmp_path, mock_config, monkeypatch
    ):
        """JARVIS_VAULT_PATH env var should override vault_path from config."""
        from tools.config import get_vault_path

        # Create an actual directory for the env var to point to
        custom_vault = tmp_path / "docker_vault"
        custom_vault.mkdir()

        monkeypatch.setenv("JARVIS_VAULT_PATH", str(custom_vault))

        path = get_vault_path()
        assert path == str(custom_vault)

    def test_jarvis_vault_path_skips_nonexistent_dir(self, mock_config, monkeypatch):
        """JARVIS_VAULT_PATH should be ignored if directory doesn't exist."""
        from tools.config import get_vault_path

        monkeypatch.setenv("JARVIS_VAULT_PATH", "/nonexistent/docker/vault/12345")

        # Should fall back to config vault_path
        path = get_vault_path()
        assert path == str(mock_config.vault_path)

    def test_docker_mode_skips_vault_confirmed(
        self, tmp_path, mock_config, monkeypatch
    ):
        """In Docker mode (JARVIS_VAULT_PATH set), vault_confirmed is not required."""
        from tools.config import verify_config

        # Remove vault_confirmed
        mock_config.delete_key("vault_confirmed")

        # Set JARVIS_VAULT_PATH to an existing directory
        custom_vault = tmp_path / "docker_vault"
        custom_vault.mkdir()
        monkeypatch.setenv("JARVIS_VAULT_PATH", str(custom_vault))

        valid, error = verify_config()
        assert valid is True
        assert error == ""

    def test_docker_mode_verify_checks_directory_exists(self, monkeypatch):
        """In Docker mode, verify_config should still check that the directory exists."""
        from tools.config import verify_config

        monkeypatch.setenv("JARVIS_VAULT_PATH", "/nonexistent/docker/vault/12345")

        valid, error = verify_config()
        assert valid is False
        assert "not found" in error.lower()

    def test_debug_info_shows_docker_mode(self, tmp_path, mock_config, monkeypatch):
        """get_debug_info should report docker_mode when env var is set."""
        from tools.config import get_debug_info

        custom_vault = tmp_path / "docker_vault"
        custom_vault.mkdir()
        monkeypatch.setenv("JARVIS_VAULT_PATH", str(custom_vault))

        info = get_debug_info()
        assert info["docker_mode"] is True

    def test_debug_info_shows_no_docker_mode(self, mock_config, monkeypatch):
        """get_debug_info should report docker_mode=False normally."""
        from tools.config import get_debug_info

        monkeypatch.delenv("JARVIS_VAULT_PATH", raising=False)

        info = get_debug_info()
        assert info["docker_mode"] is False

    def test_resolve_jarvis_home_default(self, monkeypatch):
        """_resolve_jarvis_home should default to ~/.jarvis."""
        from tools.config import _resolve_jarvis_home
        from pathlib import Path

        monkeypatch.delenv("JARVIS_HOME", raising=False)

        result = _resolve_jarvis_home()
        assert result == Path.home() / ".jarvis"

    def test_resolve_jarvis_home_env_override(self, tmp_path, monkeypatch):
        """_resolve_jarvis_home should use JARVIS_HOME when set."""
        from tools.config import _resolve_jarvis_home

        monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "custom"))

        result = _resolve_jarvis_home()
        assert str(result) == str(tmp_path / "custom")


class TestGetChromaConfig:
    """Tests for get_chroma_config with URL parsing, env overrides, and api_key."""

    def test_defaults(self, mock_config):
        """Should return sensible defaults when nothing configured."""
        from tools.config import get_chroma_config

        cfg = get_chroma_config()
        assert cfg["host"] == "localhost"
        assert cfg["port"] == 8743
        assert cfg["ssl"] is False
        assert cfg["headers"] == {}

    def test_config_override(self, mock_config):
        """Should use values from config file."""
        from tools.config import get_chroma_config

        mock_config.set(memory={"chroma_host": "db.example.com", "chroma_port": 9999})
        cfg = get_chroma_config()
        assert cfg["host"] == "db.example.com"
        assert cfg["port"] == 9999

    def test_env_var_override(self, mock_config, monkeypatch):
        """Env vars should take precedence over config file."""
        from tools.config import get_chroma_config

        mock_config.set(memory={"chroma_host": "config-host"})
        monkeypatch.setenv("CHROMA_HOST", "env-host")
        monkeypatch.setenv("CHROMA_PORT", "7777")
        cfg = get_chroma_config()
        assert cfg["host"] == "env-host"
        assert cfg["port"] == 7777

    def test_url_parsing_http(self, mock_config):
        """chroma_url should be decomposed into host/port/ssl."""
        from tools.config import get_chroma_config

        mock_config.set(memory={"chroma_url": "http://chroma.local:9000"})
        cfg = get_chroma_config()
        assert cfg["host"] == "chroma.local"
        assert cfg["port"] == 9000
        assert cfg["ssl"] is False

    def test_url_parsing_https(self, mock_config):
        """HTTPS URL should set ssl=True and default port 443."""
        from tools.config import get_chroma_config

        mock_config.set(memory={"chroma_url": "https://chroma.prod.com"})
        cfg = get_chroma_config()
        assert cfg["host"] == "chroma.prod.com"
        assert cfg["port"] == 443
        assert cfg["ssl"] is True

    def test_url_parsing_bare_host_port(self, mock_config):
        """Bare host:port without scheme should work."""
        from tools.config import get_chroma_config

        mock_config.set(memory={"chroma_url": "10.0.0.5:8743"})
        cfg = get_chroma_config()
        assert cfg["host"] == "10.0.0.5"
        assert cfg["port"] == 8743
        assert cfg["ssl"] is False

    def test_url_env_var_override(self, mock_config, monkeypatch):
        """CHROMA_URL env var should override config host/port."""
        from tools.config import get_chroma_config

        mock_config.set(memory={"chroma_host": "old-host"})
        monkeypatch.setenv("CHROMA_URL", "https://new-host:9999")
        cfg = get_chroma_config()
        assert cfg["host"] == "new-host"
        assert cfg["port"] == 9999
        assert cfg["ssl"] is True

    def test_api_key_convenience(self, mock_config):
        """api_key + auth_header should be merged into headers."""
        from tools.config import get_chroma_config

        mock_config.set(memory={"chroma_api_key": "secret123"})
        cfg = get_chroma_config()
        assert cfg["headers"]["X-Chroma-Token"] == "secret123"

    def test_api_key_custom_header(self, mock_config):
        """Custom auth_header should be used when specified."""
        from tools.config import get_chroma_config

        mock_config.set(
            memory={
                "chroma_api_key": "bearer-token",
                "chroma_auth_header": "Authorization",
            }
        )
        cfg = get_chroma_config()
        assert cfg["headers"]["Authorization"] == "bearer-token"
        assert "X-Chroma-Token" not in cfg["headers"]

    def test_api_key_env_var(self, mock_config, monkeypatch):
        """CHROMA_API_KEY env var should override config."""
        from tools.config import get_chroma_config

        monkeypatch.setenv("CHROMA_API_KEY", "env-secret")
        cfg = get_chroma_config()
        assert cfg["headers"]["X-Chroma-Token"] == "env-secret"

    def test_api_key_merges_with_existing_headers(self, mock_config):
        """api_key should merge with manually-specified headers."""
        from tools.config import get_chroma_config

        mock_config.set(
            memory={
                "chroma_headers": {"X-Custom": "value"},
                "chroma_api_key": "token",
            }
        )
        cfg = get_chroma_config()
        assert cfg["headers"]["X-Custom"] == "value"
        assert cfg["headers"]["X-Chroma-Token"] == "token"

    def test_no_api_key_no_extra_header(self, mock_config):
        """When api_key is empty, no auth header should be added."""
        from tools.config import get_chroma_config

        cfg = get_chroma_config()
        assert "X-Chroma-Token" not in cfg["headers"]

    def test_invalid_url_raises(self, mock_config):
        """Invalid URL should raise ValueError."""
        from tools.config import _parse_chroma_url

        with pytest.raises(ValueError, match="cannot parse hostname"):
            _parse_chroma_url("://no-host")


class TestParseChromaUrl:
    """Tests for _parse_chroma_url helper."""

    def test_full_http_url(self):
        from tools.config import _parse_chroma_url

        host, port, ssl = _parse_chroma_url("http://localhost:8743")
        assert host == "localhost"
        assert port == 8743
        assert ssl is False

    def test_full_https_url(self):
        from tools.config import _parse_chroma_url

        host, port, ssl = _parse_chroma_url("https://chroma.example.com:9443")
        assert host == "chroma.example.com"
        assert port == 9443
        assert ssl is True

    def test_https_default_port(self):
        from tools.config import _parse_chroma_url

        host, port, ssl = _parse_chroma_url("https://chroma.example.com")
        assert port == 443
        assert ssl is True

    def test_http_default_port(self):
        from tools.config import _parse_chroma_url

        host, port, ssl = _parse_chroma_url("http://localhost")
        assert port == 8743  # Our default, not 80

    def test_bare_host_port(self):
        from tools.config import _parse_chroma_url

        host, port, ssl = _parse_chroma_url("192.168.1.50:8743")
        assert host == "192.168.1.50"
        assert port == 8743
        assert ssl is False

    def test_bare_host_only(self):
        from tools.config import _parse_chroma_url

        host, port, ssl = _parse_chroma_url("chroma.local")
        assert host == "chroma.local"
        assert port == 8743
        assert ssl is False
