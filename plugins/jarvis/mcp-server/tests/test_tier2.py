"""Tests for Tier 2 (ChromaDB-first) CRUD operations."""
import pytest
from tools.tier2 import tier2_write, tier2_read, tier2_list, tier2_delete
from tools.memory import _get_collection


class TestTier2Write:
    """Test tier2_write function."""
    
    def test_write_observation_basic(self, mock_config):
        """Test writing a basic observation."""
        result = tier2_write(
            content="Test observation content",
            content_type="observation"
        )
        assert result["success"]
        assert "obs::" in result["id"]
        assert result["content_type"] == "observation"
        assert result["importance_score"] == 0.5  # default
    
    def test_write_pattern_requires_name(self, mock_config):
        """Test that pattern type requires a name."""
        result = tier2_write(
            content="Test pattern",
            content_type="pattern"
        )
        assert not result["success"]
        assert "requires a name" in result["error"]
        
    def test_write_pattern_with_name(self, mock_config):
        """Test writing a pattern with name."""
        result = tier2_write(
            content="Test pattern content",
            content_type="pattern",
            name="test-pattern"
        )
        assert result["success"]
        assert "pattern::test-pattern" in result["id"]
    
    def test_write_plan_requires_name(self, mock_config):
        """Test that plan type requires a name."""
        result = tier2_write(
            content="Test plan",
            content_type="plan"
        )
        assert not result["success"]
        assert "requires a name" in result["error"]
    
    def test_write_summary_with_session_id(self, mock_config):
        """Test writing a summary with session ID."""
        result = tier2_write(
            content="Session summary",
            content_type="summary",
            session_id="test-session-123"
        )
        assert result["success"]
        assert "summary::" in result["id"]
    
    def test_write_with_custom_importance(self, mock_config):
        """Test writing with custom importance score."""
        result = tier2_write(
            content="Important observation",
            content_type="observation",
            importance_score=0.9
        )
        assert result["success"]
        assert result["importance_score"] == 0.9
    
    def test_write_invalid_importance_range(self, mock_config):
        """Test validation of importance score range."""
        result = tier2_write(
            content="Test",
            content_type="observation",
            importance_score=1.5
        )
        assert not result["success"]
        assert "between 0.0 and 1.0" in result["error"]
    
    def test_write_with_tags(self, mock_config):
        """Test writing with tags."""
        result = tier2_write(
            content="Test content",
            content_type="observation",
            tags=["work", "jarvis", "testing"]
        )
        assert result["success"]

        # Verify tags in metadata
        collection = _get_collection()
        doc = collection.get(ids=[result["id"]])
        assert doc["metadatas"][0]["tags"] == "work,jarvis,testing"
    
    def test_write_invalid_content_type(self, mock_config):
        """Test validation of content type."""
        result = tier2_write(
            content="Test",
            content_type="invalid_type"
        )
        assert not result["success"]
        assert "Invalid content_type" in result["error"]
    
    def test_write_secret_detection(self, mock_config):
        """Test secret detection blocks write."""
        result = tier2_write(
            content="API key: AKIAIOSFODNN7EXAMPLE",
            content_type="observation"
        )
        assert not result["success"]
        assert "Secret detected" in result["error"]
        assert "detections" in result
    
    def test_write_skip_secret_scan(self, mock_config):
        """Test skipping secret scan."""
        result = tier2_write(
            content="API key: AKIAIOSFODNN7EXAMPLE",
            content_type="observation",
            skip_secret_scan=True
        )
        assert result["success"]
    
    def test_write_relationship(self, mock_config):
        """Test writing a relationship."""
        result = tier2_write(
            content="Person A knows Person B",
            content_type="relationship",
            name="person-a::person-b"
        )
        assert result["success"]
        assert "rel::" in result["id"]
    
    def test_write_hint(self, mock_config):
        """Test writing a hint."""
        result = tier2_write(
            content="Use git when committing",
            content_type="hint",
            name="git-workflow::0"
        )
        assert result["success"]
        assert "hint::" in result["id"]


class TestTier2Read:
    """Test tier2_read function."""
    
    def test_read_existing_doc(self, mock_config):
        """Test reading an existing document."""
        # Write first
        write_result = tier2_write(
            content="Test observation",
            content_type="observation"
        )
        doc_id = write_result["id"]
        
        # Read
        result = tier2_read(doc_id)
        assert result["success"]
        assert result["found"]
        assert result["id"] == doc_id
        assert result["content"] == "Test observation"
        assert "metadata" in result
        assert result["metadata"]["retrieval_count"] == "1"
    
    def test_read_increments_count(self, mock_config):
        """Test that reading increments retrieval count."""
        # Write
        write_result = tier2_write(
            content="Test",
            content_type="observation"
        )
        doc_id = write_result["id"]
        
        # Read multiple times
        for i in range(1, 4):
            result = tier2_read(doc_id)
            assert result["metadata"]["retrieval_count"] == str(i)
    
    def test_read_not_found(self, mock_config):
        """Test reading non-existent document."""
        result = tier2_read("obs::nonexistent")
        assert result["success"]
        assert not result["found"]


class TestTier2List:
    """Test tier2_list function."""
    
    def test_list_all(self, mock_config):
        """Test listing all Tier 2 documents."""
        # Write some docs
        tier2_write(content="Obs 1", content_type="observation")
        tier2_write(content="Pattern 1", content_type="pattern", name="p1")
        
        result = tier2_list()
        assert result["success"]
        assert result["total"] >= 2
        assert len(result["documents"]) >= 2
    
    def test_list_by_content_type(self, mock_config):
        """Test filtering by content type."""
        # Write docs
        tier2_write(content="Obs 1", content_type="observation")
        tier2_write(content="Pattern 1", content_type="pattern", name="p1")
        
        result = tier2_list(content_type="observation")
        assert result["success"]
        for doc in result["documents"]:
            assert doc["metadata"]["type"] == "observation"
    
    def test_list_by_min_importance(self, mock_config):
        """Test filtering by minimum importance."""
        # Write docs with different importance
        tier2_write(content="Low", content_type="observation", importance_score=0.3)
        tier2_write(content="High", content_type="observation", importance_score=0.9)
        
        result = tier2_list(min_importance=0.8)
        assert result["success"]
        for doc in result["documents"]:
            assert float(doc["metadata"]["importance_score"]) >= 0.8
    
    def test_list_by_source(self, mock_config):
        """Test filtering by source."""
        tier2_write(content="Auto", content_type="observation", source="auto-extract")
        tier2_write(content="Manual", content_type="observation", source="manual")
        
        result = tier2_list(source="manual")
        assert result["success"]
        for doc in result["documents"]:
            assert doc["metadata"]["source"] == "manual"
    
    def test_list_with_limit(self, mock_config):
        """Test limit parameter."""
        # Write many docs
        for i in range(10):
            tier2_write(content=f"Doc {i}", content_type="observation")
        
        result = tier2_list(limit=5)
        assert result["success"]
        assert len(result["documents"]) <= 5
        assert result["returned"] <= 5
    
    def test_list_empty_collection(self, mock_config):
        """Test listing with empty collection."""
        # Clear collection
        collection = _get_collection()
        ids = collection.get()["ids"]
        if ids:
            collection.delete(ids=ids)
        
        result = tier2_list()
        assert result["success"]
        assert result["total"] == 0
        assert len(result["documents"]) == 0
    
    def test_list_invalid_content_type(self, mock_config):
        """Test with invalid content type."""
        result = tier2_list(content_type="invalid")
        assert not result["success"]
        assert "Invalid content_type" in result["error"]


class TestTier2ListSortBy:
    """Test tier2_list sort_by parameter."""

    def test_sort_by_importance_desc(self, mock_config):
        """Default sort returns highest importance first."""
        tier2_write(content="Low imp", content_type="observation", importance_score=0.3)
        tier2_write(content="High imp", content_type="observation", importance_score=0.9)
        tier2_write(content="Mid imp", content_type="observation", importance_score=0.6)

        result = tier2_list(sort_by="importance_desc")
        assert result["success"]
        scores = [float(d["metadata"]["importance_score"]) for d in result["documents"]]
        assert scores == sorted(scores, reverse=True)

    def test_sort_by_importance_asc(self, mock_config):
        """Ascending sort returns lowest importance first."""
        tier2_write(content="Low imp", content_type="observation", importance_score=0.3)
        tier2_write(content="High imp", content_type="observation", importance_score=0.9)

        result = tier2_list(sort_by="importance_asc")
        assert result["success"]
        scores = [float(d["metadata"]["importance_score"]) for d in result["documents"]]
        assert scores == sorted(scores)

    def test_sort_by_created_at_desc(self, mock_config):
        """Created_at desc returns most recent first."""
        import time
        tier2_write(content="First", content_type="observation")
        time.sleep(0.05)  # Ensure distinct timestamps
        tier2_write(content="Second", content_type="observation")

        result = tier2_list(sort_by="created_at_desc")
        assert result["success"]
        dates = [d["metadata"]["created_at"] for d in result["documents"]]
        assert dates == sorted(dates, reverse=True)

    def test_sort_by_none(self, mock_config):
        """sort_by='none' returns results without sorting."""
        tier2_write(content="A", content_type="observation", importance_score=0.3)
        tier2_write(content="B", content_type="observation", importance_score=0.9)

        result = tier2_list(sort_by="none")
        assert result["success"]
        assert result["total"] >= 2

    def test_sort_by_invalid(self, mock_config):
        """Invalid sort_by returns error."""
        result = tier2_list(sort_by="bogus")
        assert not result["success"]
        assert "Invalid sort_by" in result["error"]


class TestTier2Delete:
    """Test tier2_delete function."""
    
    def test_delete_existing(self, mock_config):
        """Test deleting an existing document."""
        # Write first
        write_result = tier2_write(
            content="Test",
            content_type="observation"
        )
        doc_id = write_result["id"]
        
        # Delete
        result = tier2_delete(doc_id)
        assert result["success"]
        assert result["deleted"]
        assert result["id"] == doc_id
        
        # Verify deletion
        read_result = tier2_read(doc_id)
        assert not read_result["found"]
    
    def test_delete_nonexistent(self, mock_config):
        """Test deleting non-existent document."""
        result = tier2_delete("obs::nonexistent")
        assert result["success"]
        assert not result["deleted"]
        assert result["reason"] == "not found"


class TestTier2Lifecycle:
    """Test full Tier 2 lifecycle."""
    
    def test_full_cycle(self, mock_config):
        """Test write -> read -> list -> delete cycle."""
        # Write
        write_result = tier2_write(
            content="Lifecycle test",
            content_type="observation",
            importance_score=0.75,
            tags=["test", "lifecycle"]
        )
        assert write_result["success"]
        doc_id = write_result["id"]

        # Read
        read_result = tier2_read(doc_id)
        assert read_result["success"]
        assert read_result["found"]
        assert read_result["content"] == "Lifecycle test"
        assert read_result["metadata"]["importance_score"] == "0.75"
        assert read_result["metadata"]["tags"] == "test,lifecycle"
        
        # List
        list_result = tier2_list(content_type="observation")
        assert list_result["success"]
        assert any(doc["id"] == doc_id for doc in list_result["documents"])
        
        # Delete
        delete_result = tier2_delete(doc_id)
        assert delete_result["success"]
        assert delete_result["deleted"]
        
        # Verify not in list
        list_result2 = tier2_list(content_type="observation")
        assert not any(doc["id"] == doc_id for doc in list_result2["documents"])


class TestTier2LearningDecision:
    """Test learning and decision content types."""

    def test_write_learning(self, mock_config):
        """Test writing a learning."""
        result = tier2_write(
            content="PostToolUse hooks have empty tool_result",
            content_type="learning"
        )
        assert result["success"]
        assert "learning::" in result["id"]
        assert result["content_type"] == "learning"

    def test_write_decision_requires_name(self, mock_config):
        """Test that decision type requires a name."""
        result = tier2_write(
            content="Use Python",
            content_type="decision"
        )
        assert not result["success"]
        assert "requires a name" in result["error"]

    def test_write_decision_with_name(self, mock_config):
        """Test writing a decision with name."""
        result = tier2_write(
            content="Use Python MCP server over TypeScript",
            content_type="decision",
            name="python-mcp-decision"
        )
        assert result["success"]
        assert "decision::python-mcp-decision" in result["id"]


class TestTier2ExtraMetadata:
    """Test extra_metadata passthrough."""

    def test_extra_metadata_stored(self, mock_config):
        """Test that extra_metadata is stored in ChromaDB."""
        result = tier2_write(
            content="Observation with context",
            content_type="observation",
            extra_metadata={"project_dir": "jarvis-plugin", "git_branch": "master"},
        )
        assert result["success"]

        read_result = tier2_read(result["id"])
        assert read_result["metadata"]["project_dir"] == "jarvis-plugin"
        assert read_result["metadata"]["git_branch"] == "master"

    def test_extra_metadata_none(self, mock_config):
        """Test that None extra_metadata is fine."""
        result = tier2_write(
            content="No extra metadata",
            content_type="observation",
            extra_metadata=None,
        )
        assert result["success"]


class TestTier2Upsert:
    """Test tier2_upsert function."""

    def test_upsert_updates_content(self, mock_config):
        """Test that upsert updates existing document content."""
        from tools.tier2 import tier2_upsert

        # Write original
        write_result = tier2_write(
            content="Original content",
            content_type="observation",
            importance_score=0.5,
        )
        doc_id = write_result["id"]

        # Upsert with new content
        result = tier2_upsert(doc_id, "Updated content", {"type": "observation", "importance_score": "0.5"})
        assert result["success"]
        assert result["updated"]

        # Verify update
        read_result = tier2_read(doc_id)
        assert read_result["content"] == "Updated content"

    def test_upsert_updates_metadata(self, mock_config):
        """Test that upsert updates metadata."""
        from tools.tier2 import tier2_upsert

        write_result = tier2_write(
            content="Test",
            content_type="observation",
            importance_score=0.5,
        )
        doc_id = write_result["id"]

        tier2_upsert(doc_id, "Test", {"type": "observation", "importance_score": "0.9"})

        read_result = tier2_read(doc_id)
        assert float(read_result["metadata"]["importance_score"]) == 0.9
