"""Tests for unified retrieve module — routing reads/searches."""

import pytest
from unittest.mock import patch
from tools.retrieve import retrieve
from tools.content import content_write


class TestRetrieveRouting:
    """Test parameter validation and routing logic."""

    def test_no_routing_param_returns_error(self, mock_config):
        """Error when no routing parameter is provided."""
        result = retrieve()
        assert not result["success"]
        assert "Provide one of" in result["error"]

    def test_multiple_routing_params_returns_error(self, mock_config):
        """Error when multiple routing parameters are provided."""
        result = retrieve(query="test", name="test-mem")
        assert not result["success"]
        assert "only ONE" in result["error"]

    def test_query_and_id_conflict(self, mock_config):
        """Error when both query and id provided."""
        result = retrieve(query="test", id="obs::12345")
        assert not result["success"]

    def test_all_four_conflict(self, mock_config):
        """Error when all routing params provided."""
        result = retrieve(query="q", id="i", name="n", list_type="content")
        assert not result["success"]


class TestRetrieveQuery:
    """Test semantic search routing."""

    def test_query_routes_to_semantic_search(self, mock_config):
        """Query parameter triggers semantic search."""
        content_write(content="PostgreSQL is a vector database", content_type="observation")

        result = retrieve(query="vector database")
        assert isinstance(result, dict)

    def test_query_with_filter(self, mock_config):
        """Query with filter passes through."""
        result = retrieve(
            query="test",
            filter={"directory": "notes"},
            n_results=3,
        )
        assert isinstance(result, dict)


class TestRetrieveById:
    """Test ID-based read routing."""

    def test_content_id_routes_to_content_read(self, mock_config):
        """obs:: ID routes to content_read with retrieval count increment."""
        write_result = content_write(
            content="Test observation",
            content_type="observation",
        )
        doc_id = write_result["id"]

        result = retrieve(id=doc_id)
        assert result["success"]
        assert result["found"]
        assert result["content"] == "Test observation"
        assert result["retrieval_count"] == 1.0

    def test_content_id_increments_count(self, mock_config):
        """Multiple reads increment retrieval count."""
        write_result = content_write(
            content="Counter test",
            content_type="observation",
        )
        doc_id = write_result["id"]

        retrieve(id=doc_id)
        retrieve(id=doc_id)
        result = retrieve(id=doc_id)
        assert result["retrieval_count"] == 3.0

    def test_pattern_id_routes_to_content_read(self, mock_config):
        """pattern:: ID routes to content_read."""
        write_result = content_write(
            content="Pattern content",
            content_type="pattern",
            name="test-pattern",
        )

        result = retrieve(id=write_result["id"])
        assert result["success"]
        assert result["found"]

    def test_vault_id_routes_to_doc_read(self, mock_config):
        """vault:: ID routes to doc_read."""
        result = retrieve(id="vault::notes/nonexistent.md")
        assert isinstance(result, dict)

    def test_nonexistent_content_id(self, mock_config):
        """Reading nonexistent content doc returns found=False."""
        result = retrieve(id="obs::nonexistent")
        assert result["success"]
        assert not result["found"]

    def test_core_id_falls_back_to_remote_schemas(self, mock_config):
        """obs:: ID not found locally falls back to remote schemas."""
        remote_doc = {
            "success": True,
            "found": True,
            "id": "obs::9999999999999",
            "content": "Remote observation from another Jarvis instance",
            "category": "observation",
            "scope": "global",
            "project": None,
            "source": "auto-extract",
            "importance_score": 0.7,
            "retrieval_count": 3.0,
            "status": "active",
            "metadata": {"tags": ["security"]},
            "schema": "remote_personio",
            "source_remote": "remote_personio",
        }

        with patch("tools.retrieve._read_from_remote_schemas", return_value=remote_doc):
            result = retrieve(id="obs::9999999999999")

        assert result["success"]
        assert result["found"]
        assert result["content"] == "Remote observation from another Jarvis instance"
        assert result["source_remote"] == "remote_personio"

    def test_local_id_takes_precedence_over_remote(self, mock_config):
        """When doc exists locally, remote fallback is NOT called."""
        write_result = content_write(
            content="Local observation",
            content_type="observation",
        )
        doc_id = write_result["id"]

        with patch("tools.retrieve._read_from_remote_schemas") as mock_remote:
            result = retrieve(id=doc_id)

        assert result["success"]
        assert result["found"]
        assert result["content"] == "Local observation"
        mock_remote.assert_not_called()

    def test_remote_fallback_returns_not_found_when_nowhere(self, mock_config):
        """When not found locally or remotely, returns found=False."""
        with patch("tools.retrieve._read_from_remote_schemas", return_value=None):
            result = retrieve(id="obs::nonexistent_anywhere")

        assert result["success"]
        assert not result["found"]


class TestRetrieveByName:
    """Test memory read by name routing."""

    def test_name_routes_to_memory_read(self, mock_config):
        """Name parameter routes to memory_read."""
        from tools.store import store

        store(content="Memory content", type="memory", name="retrieve-test")

        result = retrieve(name="retrieve-test")
        assert result["success"]
        assert "Memory content" in result.get("content", "")

    def test_nonexistent_memory(self, mock_config):
        """Reading nonexistent memory returns found=False."""
        result = retrieve(name="nonexistent-memory")
        assert result["success"]
        assert not result["found"]


class TestRetrieveList:
    """Test list operations routing."""

    def test_list_content(self, mock_config):
        """list_type='content' routes to content_list."""
        import time

        content_write(content="Obs 1", content_type="observation")
        time.sleep(0.01)
        content_write(content="Obs 2", content_type="observation")

        result = retrieve(list_type="content")
        assert result["success"]
        assert result["total"] >= 2

    def test_list_tier2_alias(self, mock_config):
        """list_type='tier2' is a deprecated alias for 'content'."""
        content_write(content="Obs", content_type="observation")

        result = retrieve(list_type="tier2")
        assert result["success"]

    def test_list_content_with_filter(self, mock_config):
        """list_type='content' with type_filter."""
        content_write(content="Obs", content_type="observation")
        content_write(content="Pat", content_type="pattern", name="p1")

        result = retrieve(list_type="content", type_filter="observation")
        assert result["success"]
        for doc in result["documents"]:
            assert doc["category"] == "observation"

    def test_list_content_with_min_importance(self, mock_config):
        """list_type='content' with min_importance filter."""
        content_write(content="Low", content_type="observation", importance_score=0.3)
        content_write(content="High", content_type="observation", importance_score=0.9)

        result = retrieve(list_type="content", min_importance=0.8)
        assert result["success"]
        for doc in result["documents"]:
            assert doc["importance_score"] >= 0.8

    def test_list_memory(self, mock_config):
        """list_type='memory' routes to memory_list."""
        result = retrieve(list_type="memory")
        assert result["success"]

    def test_invalid_list_type(self, mock_config):
        """Invalid list_type returns error."""
        result = retrieve(list_type="bogus")
        assert not result["success"]
        assert "Invalid list_type" in result["error"]

    def test_session_id_filter(self, mock_config):
        """list_type='content' with session_id filters results."""
        content_write(
            content="Session A", content_type="learning", session_id="sess-aaa"
        )
        content_write(
            content="Session B", content_type="pattern", name="sess-b-pat",
            session_id="sess-bbb",
        )

        result = retrieve(
            list_type="content", session_id="sess-aaa", include_content=True
        )
        assert result["success"]
        assert result["total"] >= 1
        contents = [d["content"] for d in result["documents"]]
        assert "Session A" in contents
        assert "Session B" not in contents

    def test_sort_by_passthrough(self, mock_config):
        """sort_by parameter passes through to content_list."""
        content_write(content="Low", content_type="learning", importance_score=0.3)
        content_write(content="High", content_type="pattern", name="high-pat", importance_score=0.9)

        result = retrieve(list_type="content", sort_by="importance_asc")
        assert result["success"]
        if result["total"] >= 2:
            scores = [d["importance_score"] for d in result["documents"]]
            assert scores == sorted(scores)


class TestRetrieveIncludeContent:
    """Test include_content passthrough in retrieve()."""

    def test_content_default_excludes_content(self, mock_config):
        """retrieve(list_type='content') defaults include_content=False."""
        content_write(content="Content default test", content_type="observation")

        result = retrieve(list_type="content")
        assert result["success"]
        for doc in result["documents"]:
            assert "content" not in doc

    def test_content_include_content_true(self, mock_config):
        """retrieve(list_type='content', include_content=True) includes content."""
        content_write(content="Content visible", content_type="observation")

        result = retrieve(list_type="content", include_content=True)
        assert result["success"]
        found = any(
            doc.get("content") == "Content visible" for doc in result["documents"]
        )
        assert found, "Expected content when include_content=True"

    def test_memory_default_excludes_content(self, mock_config):
        """retrieve(list_type='memory') defaults include_content=False."""
        from tools.store import store

        store(content="Memory list test", type="memory", name="retrieve-list-test")

        result = retrieve(list_type="memory")
        assert result["success"]
        for mem in result.get("memories", []):
            assert "content" not in mem

    def test_memory_include_content_true(self, mock_config):
        """retrieve(list_type='memory', include_content=True) includes content."""
        from tools.store import store

        store(content="Memory body here", type="memory", name="retrieve-content-test")

        result = retrieve(list_type="memory", include_content=True)
        assert result["success"]
        found = False
        for mem in result.get("memories", []):
            if mem["name"] == "retrieve-content-test":
                assert "content" in mem
                assert "Memory body here" in mem["content"]
                found = True
                break
        assert found, "Memory 'retrieve-content-test' not found in list"
