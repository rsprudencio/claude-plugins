"""Tests for vault file operations."""
import os
import pytest
from tools.file_ops import (
    write_vault_file,
    read_vault_file,
    list_vault_dir,
    file_exists_in_vault,
    validate_vault_path,
)


class TestValidateVaultPath:
    """Tests for path validation."""

    def test_valid_path_passes(self, mock_config):
        """Valid relative path should pass."""
        valid, full_path, error = validate_vault_path("test.txt")
        assert valid is True
        assert error == ""
        assert str(mock_config.vault_path) in full_path

    def test_nested_path_passes(self, mock_config):
        """Nested relative path should pass."""
        valid, full_path, error = validate_vault_path("journal/2026/01/entry.md")
        assert valid is True
        assert "journal/2026/01/entry.md" in full_path

    def test_path_traversal_blocked(self, mock_config):
        """Path traversal attempts should be blocked."""
        valid, _, error = validate_vault_path("../../../etc/passwd")
        assert valid is False
        assert "escapes vault" in error.lower()

    def test_absolute_path_outside_vault_blocked(self, mock_config):
        """Absolute paths outside vault should be blocked."""
        valid, _, error = validate_vault_path("/etc/passwd")
        assert valid is False
        assert "escapes vault" in error.lower()

    def test_forbidden_components_blocked(self, mock_config):
        """Paths containing forbidden components should be blocked."""
        forbidden_paths = [".ssh/id_rsa", ".aws/credentials", ".gnupg/private", ".env"]
        for path in forbidden_paths:
            valid, _, error = validate_vault_path(path)
            assert valid is False, f"Should block {path}"
            assert "forbidden" in error.lower()

    def test_env_in_filename_blocked(self, mock_config):
        """Files named .env should be blocked."""
        valid, _, error = validate_vault_path("project/.env")
        assert valid is False
        assert "forbidden" in error.lower()

    def test_fails_without_vault_confirmed(self, unconfirmed_config):
        """Should fail if vault not confirmed."""
        valid, _, error = validate_vault_path("test.txt")
        assert valid is False
        assert "permission denied" in error.lower()


class TestWriteVaultFile:
    """Tests for write_vault_file function."""

    def test_write_simple_file(self, mock_config):
        """Should write a simple file to vault."""
        result = write_vault_file("test.txt", "Hello World")
        assert result["success"] is True
        assert result["path"] == "test.txt"

        # Verify file exists
        file_path = mock_config.vault_path / "test.txt"
        assert file_path.exists()
        assert file_path.read_text() == "Hello World"

    def test_write_creates_directories(self, mock_config):
        """Should create parent directories as needed."""
        result = write_vault_file("new/nested/dir/file.md", "content")
        assert result["success"] is True

        file_path = mock_config.vault_path / "new/nested/dir/file.md"
        assert file_path.exists()
        assert file_path.read_text() == "content"

    def test_write_overwrites_existing(self, mock_config):
        """Should overwrite existing files."""
        write_vault_file("overwrite.txt", "original")
        write_vault_file("overwrite.txt", "updated")

        file_path = mock_config.vault_path / "overwrite.txt"
        assert file_path.read_text() == "updated"

    def test_write_fails_without_confirmation(self, unconfirmed_config):
        """Should fail if vault not confirmed."""
        result = write_vault_file("test.txt", "content")
        assert result["success"] is False
        assert "permission denied" in result["error"].lower()

    def test_write_blocks_path_traversal(self, mock_config):
        """Should block path traversal attempts."""
        result = write_vault_file("../outside.txt", "malicious")
        assert result["success"] is False
        assert "escapes vault" in result["error"].lower()

    def test_write_handles_unicode(self, mock_config):
        """Should handle unicode content."""
        content = "Hello 世界 🌍 émojis"
        result = write_vault_file("unicode.txt", content)
        assert result["success"] is True

        file_path = mock_config.vault_path / "unicode.txt"
        assert file_path.read_text() == content


class TestReadVaultFile:
    """Tests for read_vault_file function."""

    def test_read_existing_file(self, mock_config):
        """Should read an existing file."""
        # Create file first
        file_path = mock_config.vault_path / "readable.txt"
        file_path.write_text("test content")

        result = read_vault_file("readable.txt")
        assert result["success"] is True
        assert result["content"] == "test content"

    def test_read_nonexistent_file(self, mock_config):
        """Should return error for nonexistent file."""
        result = read_vault_file("nonexistent.txt")
        assert result["success"] is False
        assert "not found" in result["error"].lower()

    def test_read_fails_without_confirmation(self, unconfirmed_config):
        """Should fail if vault not confirmed."""
        result = read_vault_file("test.txt")
        assert result["success"] is False
        assert "permission denied" in result["error"].lower()

    def test_read_blocks_path_traversal(self, mock_config):
        """Should block path traversal attempts."""
        result = read_vault_file("../../etc/passwd")
        assert result["success"] is False
        assert "escapes vault" in result["error"].lower()


class TestListVaultDir:
    """Tests for list_vault_dir function."""

    def test_list_root(self, mock_config):
        """Should list vault root directory."""
        result = list_vault_dir(".")
        assert result["success"] is True
        assert "journal" in result["directories"]
        assert "notes" in result["directories"]
        assert "inbox" in result["directories"]

    def test_list_subdirectory(self, mock_config):
        """Should list subdirectory."""
        result = list_vault_dir("journal")
        assert result["success"] is True
        assert "2026" in result["directories"]

    def test_list_with_files(self, mock_config):
        """Should list both files and directories."""
        # Create a file
        (mock_config.vault_path / "notes" / "test.md").write_text("note")

        result = list_vault_dir("notes")
        assert result["success"] is True
        assert "test.md" in result["files"]

    def test_list_nonexistent_directory(self, mock_config):
        """Should handle nonexistent directory."""
        result = list_vault_dir("nonexistent")
        assert result["success"] is False

    def test_list_fails_without_confirmation(self, unconfirmed_config):
        """Should fail if vault not confirmed."""
        result = list_vault_dir(".")
        assert result["success"] is False
        assert "permission denied" in result["error"].lower()


class TestFileExistsInVault:
    """Tests for file_exists_in_vault function."""

    def test_existing_file(self, mock_config):
        """Should detect existing file."""
        (mock_config.vault_path / "exists.txt").write_text("content")

        result = file_exists_in_vault("exists.txt")
        assert result["success"] is True
        assert result["exists"] is True
        assert result["is_file"] is True
        assert result["is_dir"] is False

    def test_existing_directory(self, mock_config):
        """Should detect existing directory."""
        result = file_exists_in_vault("journal")
        assert result["success"] is True
        assert result["exists"] is True
        assert result["is_file"] is False
        assert result["is_dir"] is True

    def test_nonexistent_path(self, mock_config):
        """Should report nonexistent path."""
        result = file_exists_in_vault("nonexistent.txt")
        assert result["success"] is True
        assert result["exists"] is False

    def test_fails_without_confirmation(self, unconfirmed_config):
        """Should fail if vault not confirmed."""
        result = file_exists_in_vault("test.txt")
        assert result["success"] is False
        assert "permission denied" in result["error"].lower()
