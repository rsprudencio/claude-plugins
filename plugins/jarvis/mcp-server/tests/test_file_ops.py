"""Tests for vault file operations."""
import os
import pytest
from tools.file_ops import (
    write_vault_file,
    read_vault_file,
    list_vault_dir,
    file_exists_in_vault,
    validate_vault_path,
    append_vault_file,
    edit_vault_file,
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


class TestEdgeCases:
    """Tests for edge cases and special scenarios."""

    def test_write_empty_content(self, mock_config):
        """Should handle writing empty string content."""
        result = write_vault_file("empty.txt", "")
        assert result["success"] is True

        # Verify file exists and is empty
        file_path = mock_config.vault_path / "empty.txt"
        assert file_path.exists()
        assert file_path.read_text() == ""

    def test_read_binary_file_as_text(self, mock_config):
        """Should handle binary files gracefully when reading as text."""
        # Create a file with binary content
        binary_path = mock_config.vault_path / "binary.bin"
        binary_path.write_bytes(b"\x00\x01\x02\xff\xfe\xfd")

        result = read_vault_file("binary.bin")

        # Should either succeed with decoded content or fail gracefully
        # (depending on implementation - both are acceptable)
        assert "success" in result

    def test_list_empty_directory(self, mock_config):
        """Should handle listing empty directory."""
        # Create empty directory
        empty_dir = mock_config.vault_path / "empty_dir"
        empty_dir.mkdir()

        result = list_vault_dir("empty_dir")
        assert result["success"] is True
        assert result["files"] == []
        assert result["directories"] == []

    def test_symlink_handling(self, mock_config):
        """Should handle symlinks appropriately."""
        import sys

        # Skip on Windows where symlinks require admin
        if sys.platform == "win32":
            pytest.skip("Symlink test skipped on Windows")

        # Create a real file
        real_file = mock_config.vault_path / "real.txt"
        real_file.write_text("real content")

        # Create symlink
        symlink = mock_config.vault_path / "link.txt"
        try:
            symlink.symlink_to(real_file)

            # Test reading through symlink
            result = read_vault_file("link.txt")
            assert result["success"] is True
            assert result["content"] == "real content"

            # Test file_exists with symlink
            exists_result = file_exists_in_vault("link.txt")
            assert exists_result["success"] is True
            assert exists_result["exists"] is True
            assert exists_result["is_file"] is True
        except OSError:
            # If symlink creation fails, skip test
            pytest.skip("Symlink creation not supported")


class TestAppendVaultFile:
    """Tests for append_vault_file function."""

    def test_append_to_existing_file(self, mock_config):
        """Should append content to an existing file."""
        file_path = mock_config.vault_path / "append.txt"
        file_path.write_text("line1")

        result = append_vault_file("append.txt", "line2")
        assert result["success"] is True
        assert result["path"] == "append.txt"
        assert result["bytes_appended"] > 0

        assert file_path.read_text() == "line1\nline2"

    def test_append_with_custom_separator(self, mock_config):
        """Should use custom separator between existing and new content."""
        file_path = mock_config.vault_path / "custom_sep.txt"
        file_path.write_text("a")

        result = append_vault_file("custom_sep.txt", "b", separator="\n\n---\n\n")
        assert result["success"] is True
        assert file_path.read_text() == "a\n\n---\n\nb"

    def test_append_with_empty_separator(self, mock_config):
        """Should concatenate directly with empty separator."""
        file_path = mock_config.vault_path / "no_sep.txt"
        file_path.write_text("hello")

        result = append_vault_file("no_sep.txt", "world", separator="")
        assert result["success"] is True
        assert file_path.read_text() == "helloworld"

    def test_append_requires_existing_file(self, mock_config):
        """Should fail if file does not exist (prevents accidental creation)."""
        result = append_vault_file("nonexistent.txt", "content")
        assert result["success"] is False
        assert "not found" in result["error"].lower()

    def test_append_fails_without_confirmation(self, unconfirmed_config):
        """Should fail if vault not confirmed."""
        result = append_vault_file("test.txt", "content")
        assert result["success"] is False
        assert "permission denied" in result["error"].lower()

    def test_append_blocks_path_traversal(self, mock_config):
        """Should block path traversal attempts."""
        result = append_vault_file("../outside.txt", "malicious")
        assert result["success"] is False
        assert "escapes vault" in result["error"].lower()

    def test_multiple_appends_accumulate(self, mock_config):
        """Multiple appends should accumulate content correctly."""
        file_path = mock_config.vault_path / "multi.txt"
        file_path.write_text("start")

        append_vault_file("multi.txt", "a")
        append_vault_file("multi.txt", "b")
        append_vault_file("multi.txt", "c")

        assert file_path.read_text() == "start\na\nb\nc"

    def test_append_unicode_content(self, mock_config):
        """Should handle unicode content in appends."""
        file_path = mock_config.vault_path / "unicode_append.txt"
        file_path.write_text("Hello")

        result = append_vault_file("unicode_append.txt", "世界 🌍 émojis")
        assert result["success"] is True
        assert file_path.read_text() == "Hello\n世界 🌍 émojis"


class TestEditVaultFile:
    """Tests for edit_vault_file function."""

    def test_simple_find_and_replace(self, mock_config):
        """Should replace a unique string occurrence."""
        file_path = mock_config.vault_path / "edit.txt"
        file_path.write_text("Hello World")

        result = edit_vault_file("edit.txt", "World", "Universe")
        assert result["success"] is True
        assert result["replacements"] == 1
        assert file_path.read_text() == "Hello Universe"

    def test_non_unique_without_replace_all(self, mock_config):
        """Should error when old_string appears multiple times and replace_all=False."""
        file_path = mock_config.vault_path / "dupe.txt"
        file_path.write_text("foo bar foo baz foo")

        result = edit_vault_file("dupe.txt", "foo", "qux")
        assert result["success"] is False
        assert "3 times" in result["error"]
        assert "replace_all" in result["error"]
        # File should be unchanged
        assert file_path.read_text() == "foo bar foo baz foo"

    def test_non_unique_with_replace_all(self, mock_config):
        """Should replace all occurrences when replace_all=True."""
        file_path = mock_config.vault_path / "replace_all.txt"
        file_path.write_text("foo bar foo baz foo")

        result = edit_vault_file("replace_all.txt", "foo", "qux", replace_all=True)
        assert result["success"] is True
        assert result["replacements"] == 3
        assert file_path.read_text() == "qux bar qux baz qux"

    def test_old_string_not_found(self, mock_config):
        """Should error when old_string is not found."""
        file_path = mock_config.vault_path / "notfound.txt"
        file_path.write_text("Hello World")

        result = edit_vault_file("notfound.txt", "Missing", "Replacement")
        assert result["success"] is False
        assert "not found" in result["error"].lower()

    def test_old_equals_new_is_noop(self, mock_config):
        """Should error when old_string equals new_string (no-op)."""
        file_path = mock_config.vault_path / "noop.txt"
        file_path.write_text("Hello World")

        result = edit_vault_file("noop.txt", "Hello", "Hello")
        assert result["success"] is False
        assert "identical" in result["error"].lower()

    def test_edit_fails_without_confirmation(self, unconfirmed_config):
        """Should fail if vault not confirmed."""
        result = edit_vault_file("test.txt", "old", "new")
        assert result["success"] is False
        assert "permission denied" in result["error"].lower()

    def test_edit_blocks_path_traversal(self, mock_config):
        """Should block path traversal attempts."""
        result = edit_vault_file("../outside.txt", "old", "new")
        assert result["success"] is False
        assert "escapes vault" in result["error"].lower()

    def test_edit_requires_existing_file(self, mock_config):
        """Should fail if file does not exist."""
        result = edit_vault_file("nonexistent.txt", "old", "new")
        assert result["success"] is False
        assert "not found" in result["error"].lower()

    def test_edit_multiline_strings(self, mock_config):
        """Should handle multi-line old_string and new_string."""
        file_path = mock_config.vault_path / "multiline.md"
        file_path.write_text("# Title\n\nOld paragraph\nwith two lines.\n\n## Next")

        result = edit_vault_file(
            "multiline.md",
            "Old paragraph\nwith two lines.",
            "New paragraph\nwith different content\nand three lines."
        )
        assert result["success"] is True
        assert file_path.read_text() == "# Title\n\nNew paragraph\nwith different content\nand three lines.\n\n## Next"

    def test_edit_unicode_content(self, mock_config):
        """Should handle unicode in both old and new strings."""
        file_path = mock_config.vault_path / "unicode_edit.txt"
        file_path.write_text("Hello 世界")

        result = edit_vault_file("unicode_edit.txt", "世界", "🌍 World")
        assert result["success"] is True
        assert file_path.read_text() == "Hello 🌍 World"
