"""Tests for memory CRUD tool handlers."""
import os
import pytest
from tools.memory_crud import (
    memory_write, memory_read, memory_list, memory_delete,
)


def _reset_chromadb(mock_config):
    """Reset ChromaDB singleton for test isolation."""
    import tools.memory as mem
    mem._chroma_client = None
    mock_config.set(memory={
        "db_path": str(mock_config.vault_path / ".test_crud_db"),
        "project_memories_path": str(mock_config.vault_path / ".jarvis" / "memories"),
    })


def _cleanup_chromadb():
    import tools.memory as mem
    mem._chroma_client = None


class TestMemoryWrite:
    """Tests for memory_write handler."""

    def test_write_basic(self, mock_config):
        _reset_chromadb(mock_config)

        result = memory_write(
            name="test-basic",
            content="# Test\n\nBasic memory content.",
        )
        assert result["success"] is True
        assert result["name"] == "test-basic"
        assert result["scope"] == "global"
        assert result["secret_scan"] == "clean"
        assert result["indexed"] is True
        assert "memory::global::test-basic" == result["id"]

        _cleanup_chromadb()

    def test_write_project_scope(self, mock_config):
        _reset_chromadb(mock_config)

        result = memory_write(
            name="project-ctx",
            content="# Project Context",
            scope="project",
            project="my-app",
        )
        assert result["success"] is True
        assert result["scope"] == "project"
        assert "memory::my-app::project-ctx" == result["id"]

        _cleanup_chromadb()

    def test_write_project_scope_requires_project(self, mock_config):
        _reset_chromadb(mock_config)

        result = memory_write(
            name="orphan",
            content="No project",
            scope="project",
        )
        assert result["success"] is False
        assert "required" in result["error"].lower()

        _cleanup_chromadb()

    def test_write_invalid_name(self, mock_config):
        _reset_chromadb(mock_config)

        result = memory_write(name="Invalid Name", content="test")
        assert result["success"] is False
        assert "invalid" in result["error"].lower()

        _cleanup_chromadb()

    def test_write_invalid_scope(self, mock_config):
        _reset_chromadb(mock_config)

        result = memory_write(name="test", content="test", scope="unknown")
        assert result["success"] is False
        assert "invalid scope" in result["error"].lower()

        _cleanup_chromadb()

    def test_write_invalid_importance(self, mock_config):
        _reset_chromadb(mock_config)

        result = memory_write(name="test", content="test", importance="extreme")
        assert result["success"] is False
        assert "invalid importance" in result["error"].lower()

        _cleanup_chromadb()

    def test_write_secret_detected(self, mock_config):
        _reset_chromadb(mock_config)

        result = memory_write(
            name="has-secret",
            content="api_key = 'sk_live_abcdefghij1234567890'",
        )
        assert result["success"] is False
        assert result["error"] == "SECRET_DETECTED"
        assert len(result["detections"]) > 0

        _cleanup_chromadb()

    def test_write_secret_bypass(self, mock_config):
        _reset_chromadb(mock_config)

        result = memory_write(
            name="has-secret-ok",
            content="api_key = 'sk_live_abcdefghij1234567890'",
            skip_secret_scan=True,
        )
        assert result["success"] is True
        assert result["secret_scan"] == "skipped"

        _cleanup_chromadb()

    def test_write_overwrite(self, mock_config):
        _reset_chromadb(mock_config)

        memory_write(name="overwrite-me", content="V1")
        result = memory_write(name="overwrite-me", content="V2", overwrite=True)
        assert result["success"] is True
        assert result["version"] == 2

        _cleanup_chromadb()

    def test_write_no_overwrite_fails(self, mock_config):
        _reset_chromadb(mock_config)

        memory_write(name="no-overwrite", content="V1")
        result = memory_write(name="no-overwrite", content="V2")
        assert result["success"] is False
        assert result.get("exists") is True

        _cleanup_chromadb()


class TestMemoryRead:
    """Tests for memory_read handler."""

    def test_read_from_chromadb(self, mock_config):
        _reset_chromadb(mock_config)

        memory_write(name="read-test", content="# Read Test\n\nContent here.")
        result = memory_read(name="read-test")
        assert result["success"] is True
        assert result["found"] is True
        assert result["source"] == "chromadb"
        assert "Content here." in result["content"]

        _cleanup_chromadb()

    def test_read_file_fallback(self, mock_config):
        _reset_chromadb(mock_config)

        # Write file directly (bypass ChromaDB)
        from tools.memory_files import resolve_memory_path, write_memory_file
        path, _ = resolve_memory_path("file-only", scope="global")
        write_memory_file(path, "file-only", "File content", "global", None, "medium", [], False)

        result = memory_read(name="file-only")
        assert result["success"] is True
        assert result["found"] is True
        assert result["source"] == "file"
        assert result.get("index_stale") is True

        _cleanup_chromadb()

    def test_read_not_found(self, mock_config):
        _reset_chromadb(mock_config)

        result = memory_read(name="nonexistent")
        assert result["success"] is True
        assert result["found"] is False
        assert "available" in result

        _cleanup_chromadb()

    def test_read_project_scope(self, mock_config):
        _reset_chromadb(mock_config)

        memory_write(name="proj-read", content="Project data", scope="project", project="myapp")
        result = memory_read(name="proj-read", scope="project", project="myapp")
        assert result["success"] is True
        assert result["found"] is True

        _cleanup_chromadb()


class TestMemoryList:
    """Tests for memory_list handler."""

    def test_list_all(self, mock_config):
        _reset_chromadb(mock_config)

        memory_write(name="list-a", content="A", importance="high", tags=["tag1"])
        memory_write(name="list-b", content="B", importance="low")

        result = memory_list()
        assert result["success"] is True
        assert result["total"] >= 2
        names = [m["name"] for m in result["memories"]]
        assert "list-a" in names
        assert "list-b" in names

        _cleanup_chromadb()

    def test_list_with_filter(self, mock_config):
        _reset_chromadb(mock_config)

        memory_write(name="filter-high", content="H", importance="high")
        memory_write(name="filter-low", content="L", importance="low")

        result = memory_list(importance="high")
        names = [m["name"] for m in result["memories"]]
        assert "filter-high" in names
        assert "filter-low" not in names

        _cleanup_chromadb()

    def test_list_indexed_status(self, mock_config):
        _reset_chromadb(mock_config)

        memory_write(name="indexed-check", content="Check")
        result = memory_list()
        for mem in result["memories"]:
            if mem["name"] == "indexed-check":
                assert mem["indexed"] is True
                break

        _cleanup_chromadb()

    def test_list_empty(self, mock_config):
        _reset_chromadb(mock_config)

        result = memory_list(scope="global")
        assert result["success"] is True
        assert isinstance(result["memories"], list)

        _cleanup_chromadb()


class TestMemoryDelete:
    """Tests for memory_delete handler."""

    def test_delete_with_confirm(self, mock_config):
        _reset_chromadb(mock_config)

        memory_write(name="delete-me", content="Goodbye")
        result = memory_delete(name="delete-me", confirm=True)
        assert result["success"] is True
        assert result["file_deleted"] is True
        assert result["index_deleted"] is True

        # Verify it's gone
        read_result = memory_read(name="delete-me")
        assert read_result["found"] is False

        _cleanup_chromadb()

    def test_delete_without_confirm_prompts(self, mock_config):
        _reset_chromadb(mock_config)

        memory_write(name="confirm-gate", content="Protected content")
        result = memory_delete(name="confirm-gate")
        assert result["success"] is True
        assert result["confirmation_required"] is True
        assert "confirm" in result["message"].lower()

        # File should still exist
        read_result = memory_read(name="confirm-gate")
        assert read_result["found"] is True

        _cleanup_chromadb()

    def test_delete_project_scope_no_confirm_needed(self, mock_config):
        _reset_chromadb(mock_config)

        memory_write(name="proj-del", content="Del", scope="project", project="myapp")
        result = memory_delete(name="proj-del", scope="project", project="myapp")
        assert result["success"] is True
        assert result["file_deleted"] is True

        _cleanup_chromadb()

    def test_delete_nonexistent(self, mock_config):
        _reset_chromadb(mock_config)

        result = memory_delete(name="no-such-memory", confirm=True)
        # File doesn't exist, but ChromaDB delete doesn't error on missing IDs
        # So file_deleted=False but index_deleted=True (no-op success)
        assert result["success"] is True
        assert result["file_deleted"] is False

        _cleanup_chromadb()


class TestIntegrationCycle:
    """End-to-end write → read → query → delete cycle."""

    def test_full_lifecycle(self, mock_config):
        _reset_chromadb(mock_config)

        # Write
        write_result = memory_write(
            name="lifecycle-test",
            content="# Lifecycle\n\nThis tests the full memory lifecycle.",
            importance="high",
            tags=["test", "lifecycle"],
        )
        assert write_result["success"] is True

        # Read
        read_result = memory_read(name="lifecycle-test")
        assert read_result["found"] is True
        assert "lifecycle" in read_result["content"].lower()

        # List
        list_result = memory_list()
        names = [m["name"] for m in list_result["memories"]]
        assert "lifecycle-test" in names

        # Query (semantic search should find it)
        from tools.query import query_vault
        query_result = query_vault("memory lifecycle test")
        assert query_result["success"] is True
        # It should appear in results (indexed during write)
        paths = [r["path"] for r in query_result["results"]]
        assert any("lifecycle-test" in p for p in paths)

        # Delete
        delete_result = memory_delete(name="lifecycle-test", confirm=True)
        assert delete_result["success"] is True
        assert delete_result["file_deleted"] is True

        # Verify gone
        gone_result = memory_read(name="lifecycle-test")
        assert gone_result["found"] is False

        _cleanup_chromadb()
