"""Tests for unified retrieve module — routing reads/searches."""

import pytest
from tools.retrieve import retrieve
from tools.tier2 import tier2_write


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
        result = retrieve(query="q", id="i", name="n", list_type="tier2")
        assert not result["success"]


class TestRetrieveQuery:
    """Test semantic search routing."""

    def test_query_routes_to_semantic_search(self, mock_config):
        """Query parameter triggers semantic search."""
        # Write something to search for
        tier2_write(content="ChromaDB is a vector database", content_type="observation")

        result = retrieve(query="vector database")
        # May or may not find results depending on embeddings, but should not error
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

    def test_tier2_id_routes_to_tier2_read(self, mock_config):
        """obs:: ID routes to tier2_read with retrieval count increment."""
        write_result = tier2_write(
            content="Test observation",
            content_type="observation",
        )
        doc_id = write_result["id"]

        result = retrieve(id=doc_id)
        assert result["success"]
        assert result["found"]
        assert result["content"] == "Test observation"
        assert result["metadata"]["retrieval_count"] == "1.0"

    def test_tier2_id_increments_count(self, mock_config):
        """Multiple reads increment retrieval count."""
        write_result = tier2_write(
            content="Counter test",
            content_type="observation",
        )
        doc_id = write_result["id"]

        retrieve(id=doc_id)
        retrieve(id=doc_id)
        result = retrieve(id=doc_id)
        assert result["metadata"]["retrieval_count"] == "3.0"

    def test_pattern_id_routes_to_tier2_read(self, mock_config):
        """pattern:: ID routes to tier2_read."""
        write_result = tier2_write(
            content="Pattern content",
            content_type="pattern",
            name="test-pattern",
        )

        result = retrieve(id=write_result["id"])
        assert result["success"]
        assert result["found"]

    def test_vault_id_routes_to_doc_read(self, mock_config):
        """vault:: ID routes to doc_read (Tier 1)."""
        # This may not find indexed content since no indexing,
        # but should route correctly without error
        result = retrieve(id="vault::notes/nonexistent.md")
        assert isinstance(result, dict)

    def test_nonexistent_tier2_id(self, mock_config):
        """Reading nonexistent tier2 doc returns found=False."""
        result = retrieve(id="obs::nonexistent")
        assert result["success"]
        assert not result["found"]


class TestRetrieveByName:
    """Test memory read by name routing."""

    def test_name_routes_to_memory_read(self, mock_config):
        """Name parameter routes to memory_read."""
        from tools.store import store

        # Create a memory first
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

    def test_list_tier2(self, mock_config):
        """list_type='tier2' routes to tier2_list."""
        import time

        tier2_write(content="Obs 1", content_type="observation")
        time.sleep(0.01)  # Avoid timestamp-based ID collision
        tier2_write(content="Obs 2", content_type="observation")

        result = retrieve(list_type="tier2")
        assert result["success"]
        assert result["total"] >= 2

    def test_list_tier2_with_filter(self, mock_config):
        """list_type='tier2' with type_filter."""
        tier2_write(content="Obs", content_type="observation")
        tier2_write(content="Pat", content_type="pattern", name="p1")

        result = retrieve(list_type="tier2", type_filter="observation")
        assert result["success"]
        for doc in result["documents"]:
            assert doc["metadata"]["type"] == "observation"

    def test_list_tier2_with_min_importance(self, mock_config):
        """list_type='tier2' with min_importance filter."""
        tier2_write(content="Low", content_type="observation", importance_score=0.3)
        tier2_write(content="High", content_type="observation", importance_score=0.9)

        result = retrieve(list_type="tier2", min_importance=0.8)
        assert result["success"]
        for doc in result["documents"]:
            assert float(doc["metadata"]["importance_score"]) >= 0.8

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
        """list_type='tier2' with session_id filters results."""
        import time

        tier2_write(
            content="Session A", content_type="observation", session_id="sess-aaa"
        )
        time.sleep(0.01)  # Avoid timestamp-based ID collision
        tier2_write(
            content="Session B", content_type="observation", session_id="sess-bbb"
        )

        result = retrieve(
            list_type="tier2", session_id="sess-aaa", include_content=True
        )
        assert result["success"]
        assert result["total"] >= 1
        contents = [d["content"] for d in result["documents"]]
        assert "Session A" in contents
        assert "Session B" not in contents

    def test_sort_by_passthrough(self, mock_config):
        """sort_by parameter passes through to tier2_list."""
        tier2_write(content="Low", content_type="observation", importance_score=0.3)
        tier2_write(content="High", content_type="observation", importance_score=0.9)

        result = retrieve(list_type="tier2", sort_by="importance_asc")
        assert result["success"]
        if result["total"] >= 2:
            scores = [
                float(d["metadata"]["importance_score"]) for d in result["documents"]
            ]
            assert scores == sorted(scores)


class TestRetrieveIncludeContent:
    """Test include_content passthrough in retrieve()."""

    def test_tier2_default_excludes_content(self, mock_config):
        """retrieve(list_type='tier2') defaults include_content=False."""
        tier2_write(content="Tier2 default test", content_type="observation")

        result = retrieve(list_type="tier2")
        assert result["success"]
        # retrieve passes include_content=False to _list_content,
        # but _list_content passes it to tier2_list which defaults True internally.
        # However, retrieve's default is False, so tier2_list receives False.
        for doc in result["documents"]:
            assert "content" not in doc

    def test_tier2_include_content_true(self, mock_config):
        """retrieve(list_type='tier2', include_content=True) includes content."""
        tier2_write(content="Tier2 visible", content_type="observation")

        result = retrieve(list_type="tier2", include_content=True)
        assert result["success"]
        found = any(
            doc.get("content") == "Tier2 visible" for doc in result["documents"]
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
