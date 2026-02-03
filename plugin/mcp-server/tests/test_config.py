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
