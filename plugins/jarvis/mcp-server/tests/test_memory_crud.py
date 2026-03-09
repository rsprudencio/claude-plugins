"""Tests for memory CRUD tool handlers."""

import os
from unittest.mock import MagicMock, patch
import pytest
from tools.memory_crud import (
    memory_write,
    memory_read,
    memory_list,
    memory_delete,
)



class TestMemoryWrite:
    """Tests for memory_write handler."""

    def test_write_basic(self, mock_config):
        

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



    def test_write_project_scope(self, mock_config):
        

        result = memory_write(
            name="project-ctx",
            content="# Project Context",
            scope="project",
            project="my-app",
        )
        assert result["success"] is True
        assert result["scope"] == "project"
        assert "memory::my-app::project-ctx" == result["id"]



    def test_write_project_scope_requires_project(self, mock_config):
        

        result = memory_write(
            name="orphan",
            content="No project",
            scope="project",
        )
        assert result["success"] is False
        assert "required" in result["error"].lower()



    def test_write_invalid_name(self, mock_config):
        

        result = memory_write(name="Invalid Name", content="test")
        assert result["success"] is False
        assert "invalid" in result["error"].lower()



    def test_write_invalid_scope(self, mock_config):
        

        result = memory_write(name="test", content="test", scope="unknown")
        assert result["success"] is False
        assert "invalid scope" in result["error"].lower()



    def test_write_invalid_importance(self, mock_config):
        

        result = memory_write(name="test", content="test", importance="extreme")
        assert result["success"] is False
        assert "invalid importance" in result["error"].lower()



    def test_write_numeric_importance(self, mock_config):
        

        result = memory_write(name="test-numeric", content="test", importance=0.85)
        assert result["success"] is True



    def test_write_importance_out_of_range(self, mock_config):
        

        # Values are clamped, not rejected
        result = memory_write(name="test-clamp", content="test", importance=1.5)
        assert result["success"] is True



    def test_write_categorical_backward_compat(self, mock_config):
        """Categorical strings are accepted and mapped to numeric."""
        

        result = memory_write(name="test-cat", content="test", importance="high")
        assert result["success"] is True



    def test_write_secret_detected(self, mock_config):
        

        result = memory_write(
            name="has-secret",
            content="api_key = 'sk_live_abcdefghij1234567890'",
        )
        assert result["success"] is False
        assert result["error"] == "SECRET_DETECTED"
        assert len(result["detections"]) > 0



    def test_write_secret_bypass(self, mock_config):
        

        result = memory_write(
            name="has-secret-ok",
            content="api_key = 'sk_live_abcdefghij1234567890'",
            skip_secret_scan=True,
        )
        assert result["success"] is True
        assert result["secret_scan"] == "skipped"



    def test_write_overwrite(self, mock_config):
        

        memory_write(name="overwrite-me", content="V1")
        result = memory_write(name="overwrite-me", content="V2", overwrite=True)
        assert result["success"] is True
        assert result["version"] == 2



    def test_write_no_overwrite_fails(self, mock_config):
        

        memory_write(name="no-overwrite", content="V1")
        result = memory_write(name="no-overwrite", content="V2")
        assert result["success"] is False
        assert result.get("exists") is True




class TestMemoryRead:
    """Tests for memory_read handler."""

    def test_read_from_database(self, mock_config):
        

        memory_write(name="read-test", content="# Read Test\n\nContent here.")
        result = memory_read(name="read-test")
        assert result["success"] is True
        assert result["found"] is True
        assert result["source"] == "database"
        assert "Content here." in result["content"]



    def test_read_file_fallback(self, mock_config):
        

        # Write file directly (bypass database)
        from tools.memory_files import resolve_memory_path, write_memory_file

        path, _ = resolve_memory_path("file-only", scope="global")
        write_memory_file(
            path, "file-only", "File content", "global", None, 0.5, [], False
        )

        result = memory_read(name="file-only")
        assert result["success"] is True
        assert result["found"] is True
        assert result["source"] == "file"
        assert result.get("index_stale") is True



    def test_read_not_found(self, mock_config):
        

        result = memory_read(name="nonexistent")
        assert result["success"] is True
        assert result["found"] is False
        assert "available" in result



    def test_read_project_scope(self, mock_config):
        

        memory_write(
            name="proj-read", content="Project data", scope="project", project="myapp"
        )
        result = memory_read(name="proj-read", scope="project", project="myapp")
        assert result["success"] is True
        assert result["found"] is True




class TestMemoryList:
    """Tests for memory_list handler."""

    def test_list_all(self, mock_config):
        

        memory_write(name="list-a", content="A", importance=0.8, tags=["tag1"])
        memory_write(name="list-b", content="B", importance=0.3)

        result = memory_list()
        assert result["success"] is True
        assert result["total"] >= 2
        names = [m["name"] for m in result["memories"]]
        assert "list-a" in names
        assert "list-b" in names



    def test_list_with_threshold_filter(self, mock_config):
        

        memory_write(name="filter-high", content="H", importance=0.8)
        memory_write(name="filter-low", content="L", importance=0.3)

        result = memory_list(importance=0.7)
        names = [m["name"] for m in result["memories"]]
        assert "filter-high" in names
        assert "filter-low" not in names



    def test_list_indexed_status(self, mock_config):
        

        memory_write(name="indexed-check", content="Check")
        result = memory_list()
        for mem in result["memories"]:
            if mem["name"] == "indexed-check":
                assert mem["indexed"] is True
                break



    def test_list_empty(self, mock_config):
        

        result = memory_list(scope="global")
        assert result["success"] is True
        assert isinstance(result["memories"], list)




class TestMemoryDelete:
    """Tests for memory_delete handler."""

    def test_delete_with_confirm(self, mock_config):
        

        memory_write(name="delete-me", content="Goodbye")
        result = memory_delete(name="delete-me", confirm=True)
        assert result["success"] is True
        assert result["file_deleted"] is True
        assert result["index_deleted"] is True

        # Verify it's gone
        read_result = memory_read(name="delete-me")
        assert read_result["found"] is False



    def test_delete_without_confirm_prompts(self, mock_config):
        

        memory_write(name="confirm-gate", content="Protected content")
        result = memory_delete(name="confirm-gate")
        assert result["success"] is True
        assert result["confirmation_required"] is True
        assert "confirm" in result["message"].lower()

        # File should still exist
        read_result = memory_read(name="confirm-gate")
        assert read_result["found"] is True



    def test_delete_project_scope_no_confirm_needed(self, mock_config):
        

        memory_write(name="proj-del", content="Del", scope="project", project="myapp")
        result = memory_delete(name="proj-del", scope="project", project="myapp")
        assert result["success"] is True
        assert result["file_deleted"] is True



    def test_delete_nonexistent(self, mock_config):
        

        result = memory_delete(name="no-such-memory", confirm=True)
        # File doesn't exist, but DB delete doesn't error on missing IDs
        # So file_deleted=False but index_deleted=True (no-op success)
        assert result["success"] is True
        assert result["file_deleted"] is False




class TestIntegrationCycle:
    """End-to-end write → read → query → delete cycle."""

    def test_full_lifecycle(self, mock_config):
        

        # Write
        write_result = memory_write(
            name="lifecycle-test",
            content="# Lifecycle\n\nThis tests the full memory lifecycle.",
            importance=0.8,
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




class TestMemoryCategoryColumn:
    """Tests for category column in local.memories."""

    def test_memory_write_has_category_memory(self, mock_config):
        """Test that memory_write sets category='memory' in local.memories."""
        from tools.namespaces import global_memory_id

        result = memory_write(
            name="test-cat-memory", content="Testing category column in memories"
        )
        assert result["success"] is True

        # Verify category column via InMemoryDB
        doc_id = global_memory_id("test-cat-memory")
        row = mock_config.db.get_core(doc_id)
        assert row is not None
        assert row["category"] == "memory"
        # tier should NOT be in metadata
        assert "tier" not in row["metadata"]

        # Cleanup
        memory_delete(name="test-cat-memory", confirm=True)



class TestMemoryListIncludeContent:
    """Tests for memory_list include_content parameter."""

    def test_default_excludes_content(self, mock_config):
        """Default include_content=False omits content from results."""
        

        memory_write(name="no-content-test", content="# Body\n\nSome text here.")
        result = memory_list()
        assert result["success"] is True

        for mem in result["memories"]:
            if mem["name"] == "no-content-test":
                assert "content" not in mem
                break



    def test_include_content_true(self, mock_config):
        """include_content=True adds body text to each entry."""
        

        memory_write(name="with-content", content="# Title\n\nBody text for test.")
        result = memory_list(include_content=True)
        assert result["success"] is True

        found = False
        for mem in result["memories"]:
            if mem["name"] == "with-content":
                assert "content" in mem
                assert "Body text for test." in mem["content"]
                found = True
                break
        assert found, "Memory 'with-content' not found in list"



    def test_include_content_false_explicit(self, mock_config):
        """Explicit include_content=False omits content."""
        

        memory_write(name="explicit-false", content="Should not appear.")
        result = memory_list(include_content=False)
        assert result["success"] is True

        for mem in result["memories"]:
            if mem["name"] == "explicit-false":
                assert "content" not in mem
                break



    def test_content_is_frontmatter_stripped(self, mock_config):
        """Content returned by include_content has frontmatter stripped."""


        memory_write(
            name="fm-stripped",
            content="Pure body only.",
            importance=0.8,
            tags=["test"],
        )
        result = memory_list(include_content=True)
        assert result["success"] is True

        for mem in result["memories"]:
            if mem["name"] == "fm-stripped":
                # Should NOT contain frontmatter delimiters
                assert "---" not in mem["content"]
                assert "importance:" not in mem["content"]
                assert mem["content"] == "Pure body only."
                break


_SYNC_CFG_ENABLED = {
    "enabled": True,
    "strategy": "first-match",
    "project_groups": {},
    "remotes": [{"name": "remote-a", "dsn": "postgresql://test:test@remote/db"}],
    "rules": [{"name": "all", "match": {}, "action": "route-to", "destinations": ["remote-a"]}],
}


class TestMemoryWriteSyncRouting:
    """Tests for sync routing in memory_write (Change #1 and #2)."""

    def test_memory_write_enqueues_when_routing_matches(self, mock_config):
        """memory_write enqueues when sync is enabled and routing returns destinations."""
        mock_decision = MagicMock()
        mock_decision.destinations = ["remote-a"]

        with patch("tools.config.get_sync_config", return_value=_SYNC_CFG_ENABLED), \
             patch("tools.sync_config.load_routing_rules", return_value=[]), \
             patch("tools.routing.evaluate_routing", return_value=mock_decision), \
             patch("tools.sync_queue.enqueue_sync") as mock_enqueue:

            result = memory_write(
                name="sync-routing-test",
                content="Important strategic memory.",
                scope="project",
                project="personio-framework",
                importance=0.9,
            )

        assert result["success"] is True
        mock_enqueue.assert_called_once()
        call_args = mock_enqueue.call_args[0]
        assert call_args[1] == result["id"]
        assert call_args[2] == ["remote-a"]

    def test_memory_write_no_enqueue_when_no_match(self, mock_config):
        """memory_write does not enqueue when routing returns no destinations."""
        mock_decision = MagicMock()
        mock_decision.destinations = []

        with patch("tools.config.get_sync_config", return_value=_SYNC_CFG_ENABLED), \
             patch("tools.sync_config.load_routing_rules", return_value=[]), \
             patch("tools.routing.evaluate_routing", return_value=mock_decision), \
             patch("tools.sync_queue.enqueue_sync") as mock_enqueue:

            result = memory_write(name="no-match-mem", content="Global memory.")

        assert result["success"] is True
        mock_enqueue.assert_not_called()

    def test_memory_write_no_enqueue_when_sync_disabled(self, mock_config):
        """memory_write does not enqueue when sync.enabled is False."""
        with patch("tools.config.get_sync_config", return_value={"enabled": False}), \
             patch("tools.sync_queue.enqueue_sync") as mock_enqueue:

            result = memory_write(name="sync-disabled-mem", content="Content.")

        assert result["success"] is True
        mock_enqueue.assert_not_called()

    def test_memory_write_enqueue_failure_does_not_block_write(self, mock_config):
        """Routing exception does not prevent memory_write from succeeding."""
        with patch("tools.config.get_sync_config", side_effect=RuntimeError("config error")):
            result = memory_write(
                name="routing-error-mem",
                content="Should still be written.",
            )

        assert result["success"] is True
        assert result["indexed"] is True

    def test_memory_overwrite_reenqueues(self, mock_config):
        """Second write to same memory re-enqueues (verifies DO UPDATE fix in enqueue_sync)."""
        mock_decision = MagicMock()
        mock_decision.destinations = ["remote-a"]

        with patch("tools.config.get_sync_config", return_value=_SYNC_CFG_ENABLED), \
             patch("tools.sync_config.load_routing_rules", return_value=[]), \
             patch("tools.routing.evaluate_routing", return_value=mock_decision), \
             patch("tools.sync_queue.enqueue_sync") as mock_enqueue:

            memory_write(name="overwrite-sync", content="V1", importance=0.8)
            memory_write(name="overwrite-sync", content="V2", overwrite=True, importance=0.8)

        # enqueue_sync must have been called for both writes
        assert mock_enqueue.call_count == 2


class TestMemoryDeleteSyncPropagation:
    """Tests for delete propagation in memory_delete (Change #3)."""

    def test_memory_delete_propagates_to_synced_remotes(self, mock_config):
        """Deleting a memory that was synced enqueues a delete-sync entry."""
        memory_write(name="synced-delete-me", content="Will be deleted.")

        # Inject synced_to into the mock DB row to simulate a previously synced memory
        from tools.namespaces import global_memory_id
        doc_id = global_memory_id("synced-delete-me")
        mock_config.db.core_rows[doc_id]["synced_to"] = ["remote-a"]

        with patch("tools.sync_queue.enqueue_sync") as mock_enqueue:
            result = memory_delete(name="synced-delete-me", confirm=True)

        assert result["success"] is True
        assert result["file_deleted"] is True
        mock_enqueue.assert_called_once()
        call_args = mock_enqueue.call_args[0]
        assert call_args[1] == doc_id
        assert call_args[2] == ["remote-a"]

    def test_memory_delete_hard_deletes_when_never_synced(self, mock_config):
        """Deleting a memory with no synced_to does a hard DELETE (no enqueue)."""
        memory_write(name="unsynced-delete-me", content="Never synced.")

        from tools.namespaces import global_memory_id
        doc_id = global_memory_id("unsynced-delete-me")

        with patch("tools.sync_queue.enqueue_sync") as mock_enqueue:
            result = memory_delete(name="unsynced-delete-me", confirm=True)

        assert result["success"] is True
        assert result["file_deleted"] is True
        assert result["index_deleted"] is True
        mock_enqueue.assert_not_called()
        # Row should be gone from mock DB (hard deleted, not soft deleted)
        assert mock_config.db.core_rows.get(doc_id) is None

    def test_memory_delete_enqueue_failure_still_soft_deletes(self, mock_config):
        """If enqueue_sync raises, the soft-delete UPDATE still completes."""
        memory_write(name="soft-del-fail", content="Synced but enqueue will fail.")

        from tools.namespaces import global_memory_id
        doc_id = global_memory_id("soft-del-fail")
        mock_config.db.core_rows[doc_id]["synced_to"] = ["remote-a"]

        with patch("tools.sync_queue.enqueue_sync", side_effect=RuntimeError("queue error")):
            result = memory_delete(name="soft-del-fail", confirm=True)

        assert result["success"] is True
        # Row is soft-deleted (status='deleted'), not hard-deleted
        row = mock_config.db.core_rows.get(doc_id)
        assert row is not None
        assert row["status"] == "deleted"


