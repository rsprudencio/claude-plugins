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
        assert "jarvis-setup" in error.lower()

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
        config_module._config_cache = None

        # First load
        config1 = config_module.get_config()

        # Second load should return same object
        config2 = config_module.get_config()

        assert config1 is config2

    def test_invalid_json_returns_empty_dict(self, tmp_path, monkeypatch):
        """Invalid JSON should be handled gracefully."""
        from tools import config as config_module

        # Create config with invalid JSON
        config_dir = tmp_path / ".jarvis"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "config.json"
        config_file.write_text("{invalid json")

        # Mock home to point to tmp_path
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        config_module._config_cache = None

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
