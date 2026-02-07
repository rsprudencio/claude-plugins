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
    
    def test_write_with_topics(self, mock_config):
        """Test writing with topic tags."""
        result = tier2_write(
            content="Test content",
            content_type="observation",
            topics=["work", "jarvis", "testing"]
        )
        assert result["success"]
        
        # Verify topics in metadata
        collection = _get_collection()
        doc = collection.get(ids=[result["id"]])
        assert doc["metadatas"][0]["topics"] == "work,jarvis,testing"
    
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
            topics=["test", "lifecycle"]
        )
        assert write_result["success"]
        doc_id = write_result["id"]
        
        # Read
        read_result = tier2_read(doc_id)
        assert read_result["success"]
        assert read_result["found"]
        assert read_result["content"] == "Lifecycle test"
        assert read_result["metadata"]["importance_score"] == "0.75"
        assert read_result["metadata"]["topics"] == "test,lifecycle"
        
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
