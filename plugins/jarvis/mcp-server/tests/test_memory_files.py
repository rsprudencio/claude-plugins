"""Tests for memory file I/O module."""
import os
import pytest
from tools.memory_files import (
    validate_name, resolve_memory_path, write_memory_file,
    read_memory_file, list_memory_files, delete_memory_file,
    _parse_memory_frontmatter, _format_frontmatter, _strip_frontmatter,
)


class TestValidateName:
    """Tests for memory name slug validation."""

    def test_valid_names(self):
        assert validate_name("jarvis-trajectory") is None
        assert validate_name("my-project") is None
        assert validate_name("abc123") is None
        assert validate_name("a1") is None

    def test_single_char(self):
        assert validate_name("a") is None
        assert validate_name("1") is None

    def test_empty_name(self):
        assert "empty" in validate_name("").lower()

    def test_uppercase_rejected(self):
        assert validate_name("MyMemory") is not None

    def test_spaces_rejected(self):
        assert validate_name("my memory") is not None

    def test_special_chars_rejected(self):
        assert validate_name("my_memory") is not None
        assert validate_name("my.memory") is not None

    def test_leading_hyphen_rejected(self):
        assert validate_name("-leading") is not None

    def test_trailing_hyphen_rejected(self):
        assert validate_name("trailing-") is not None


class TestFrontmatter:
    """Tests for frontmatter generation and parsing."""

    def test_format_roundtrip(self):
        fm_str = _format_frontmatter(
            name="test-mem", scope="global", importance="high",
            tags=["strategic", "goals"], version=1,
            created="2026-02-07T20:00:00Z", modified="2026-02-07T20:00:00Z",
        )
        parsed = _parse_memory_frontmatter(fm_str + "\nBody content here.")
        assert parsed["name"] == "test-mem"
        assert parsed["scope"] == "global"
        assert parsed["importance"] == "high"
        assert "strategic" in parsed["tags"]
        assert "goals" in parsed["tags"]
        assert parsed["version"] == 1

    def test_format_with_project(self):
        fm_str = _format_frontmatter(
            name="context", scope="project", importance="medium",
            tags=[], version=1,
            created="2026-02-07T20:00:00Z", modified="2026-02-07T20:00:00Z",
            project="my-app",
        )
        parsed = _parse_memory_frontmatter(fm_str + "\nContent.")
        assert parsed["project"] == "my-app"
        assert parsed["scope"] == "project"

    def test_strip_frontmatter(self):
        content = "---\nname: test\n---\n# Body\n\nText here."
        body = _strip_frontmatter(content)
        assert "---" not in body
        assert "# Body" in body

    def test_parse_no_frontmatter(self):
        parsed = _parse_memory_frontmatter("Just plain text.")
        assert parsed == {}

    def test_parse_version_as_int(self):
        content = "---\nname: test\nversion: 3\n---\nBody"
        parsed = _parse_memory_frontmatter(content)
        assert parsed["version"] == 3
        assert isinstance(parsed["version"], int)


class TestResolveMemoryPath:
    """Tests for path resolution."""

    def test_global_path(self, mock_config):
        path, error = resolve_memory_path("jarvis-trajectory", scope="global")
        assert error == ""
        assert ".jarvis/strategic/jarvis-trajectory.md" in path

    def test_project_path(self, mock_config):
        path, error = resolve_memory_path("context", scope="project", project="my-app")
        assert error == ""
        assert ".jarvis/memories/my-app/context.md" in path

    def test_project_scope_requires_project(self, mock_config):
        _, error = resolve_memory_path("context", scope="project")
        assert "required" in error.lower()

    def test_invalid_name_rejected(self, mock_config):
        _, error = resolve_memory_path("Invalid Name", scope="global")
        assert error != ""


class TestWriteAndReadMemoryFile:
    """Tests for file write/read operations."""

    def test_write_and_read(self, mock_config):
        path, _ = resolve_memory_path("test-write", scope="global")
        result = write_memory_file(
            path=path, name="test-write", content="# Test\n\nHello world.",
            scope="global", project=None, importance="high",
            tags=["test"], overwrite=False,
        )
        assert result["success"] is True
        assert result["version"] == 1

        read_result = read_memory_file(path)
        assert read_result["success"] is True
        assert "Hello world." in read_result["body"]
        assert read_result["metadata"]["name"] == "test-write"
        assert read_result["metadata"]["importance"] == "high"

    def test_overwrite_bumps_version(self, mock_config):
        path, _ = resolve_memory_path("test-version", scope="global")
        write_memory_file(
            path=path, name="test-version", content="V1",
            scope="global", project=None, importance="medium",
            tags=[], overwrite=False,
        )
        result = write_memory_file(
            path=path, name="test-version", content="V2",
            scope="global", project=None, importance="medium",
            tags=[], overwrite=True,
        )
        assert result["success"] is True
        assert result["version"] == 2

    def test_no_overwrite_fails(self, mock_config):
        path, _ = resolve_memory_path("test-nooverwrite", scope="global")
        write_memory_file(
            path=path, name="test-nooverwrite", content="V1",
            scope="global", project=None, importance="medium",
            tags=[], overwrite=False,
        )
        result = write_memory_file(
            path=path, name="test-nooverwrite", content="V2",
            scope="global", project=None, importance="medium",
            tags=[], overwrite=False,
        )
        assert result["success"] is False
        assert result.get("exists") is True

    def test_read_nonexistent(self):
        result = read_memory_file("/tmp/nonexistent-memory-12345.md")
        assert result["success"] is False
        assert "not found" in result["error"].lower()

    def test_directory_auto_created(self, mock_config):
        path, _ = resolve_memory_path("auto-dir", scope="global")
        result = write_memory_file(
            path=path, name="auto-dir", content="Content",
            scope="global", project=None, importance="medium",
            tags=[], overwrite=False,
        )
        assert result["success"] is True
        assert os.path.isfile(path)


class TestListMemoryFiles:
    """Tests for listing memory files."""

    def test_list_empty(self, mock_config):
        results = list_memory_files(scope="global")
        assert isinstance(results, list)

    def test_list_after_writes(self, mock_config):
        path1, _ = resolve_memory_path("mem-a", scope="global")
        write_memory_file(path1, "mem-a", "A", "global", None, "high", ["tag1"], False)
        path2, _ = resolve_memory_path("mem-b", scope="global")
        write_memory_file(path2, "mem-b", "B", "global", None, "low", ["tag2"], False)

        results = list_memory_files(scope="global")
        names = [m["name"] for m in results]
        assert "mem-a" in names
        assert "mem-b" in names

    def test_list_filter_by_importance(self, mock_config):
        path1, _ = resolve_memory_path("high-mem", scope="global")
        write_memory_file(path1, "high-mem", "A", "global", None, "high", [], False)
        path2, _ = resolve_memory_path("low-mem", scope="global")
        write_memory_file(path2, "low-mem", "B", "global", None, "low", [], False)

        results = list_memory_files(scope="global", importance="high")
        names = [m["name"] for m in results]
        assert "high-mem" in names
        assert "low-mem" not in names

    def test_list_filter_by_tag(self, mock_config):
        path, _ = resolve_memory_path("tagged-mem", scope="global")
        write_memory_file(path, "tagged-mem", "A", "global", None, "medium", ["work", "python"], False)

        results = list_memory_files(scope="global", tag="work")
        names = [m["name"] for m in results]
        assert "tagged-mem" in names

        results = list_memory_files(scope="global", tag="nonexistent")
        names = [m["name"] for m in results]
        assert "tagged-mem" not in names


class TestDeleteMemoryFile:
    """Tests for file deletion."""

    def test_delete_existing(self, mock_config):
        path, _ = resolve_memory_path("to-delete", scope="global")
        write_memory_file(path, "to-delete", "Del", "global", None, "medium", [], False)
        assert os.path.isfile(path)

        result = delete_memory_file(path)
        assert result["success"] is True
        assert not os.path.isfile(path)

    def test_delete_nonexistent(self):
        result = delete_memory_file("/tmp/no-such-file-12345.md")
        assert result["success"] is False
