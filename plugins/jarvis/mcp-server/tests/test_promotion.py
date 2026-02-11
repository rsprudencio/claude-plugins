"""Tests for Tier 2 to Tier 1 promotion."""
import os
import pytest
from tools.tier2 import tier2_write, tier2_read
from tools.promotion import check_promotion_criteria, promote
from tools.memory import _get_collection
from tools.paths import get_path


class TestCheckPromotionCriteria:
    """Test check_promotion_criteria function."""
    
    def test_high_importance_triggers(self, mock_config):
        """Test that high importance score triggers promotion."""
        metadata = {
            "importance_score": "0.90",
            "retrieval_count": "1",
            "created_at": "2026-02-01T00:00:00Z"
        }
        result = check_promotion_criteria(metadata)
        assert result["should_promote"]
        assert "importance 0.90" in result["reason"]
    
    def test_high_retrieval_count_triggers(self, mock_config):
        """Test that high retrieval count triggers promotion."""
        metadata = {
            "importance_score": "0.50",
            "retrieval_count": "5",
            "created_at": "2026-02-01T00:00:00Z"
        }
        result = check_promotion_criteria(metadata)
        assert result["should_promote"]
        assert "retrievals 5.00" in result["reason"]

    def test_float_retrieval_count_triggers(self, mock_config):
        """Float retrieval count 3.1 triggers promotion (> 3 threshold)."""
        metadata = {
            "importance_score": "0.50",
            "retrieval_count": "3.1",
            "created_at": "2026-02-01T00:00:00Z"
        }
        result = check_promotion_criteria(metadata)
        assert result["should_promote"]
        assert "retrievals 3.10" in result["reason"]

    def test_float_retrieval_count_below_threshold(self, mock_config):
        """Float retrieval count 2.9 does NOT trigger promotion (< 3 threshold)."""
        metadata = {
            "importance_score": "0.50",
            "retrieval_count": "2.9",
            "created_at": "2026-02-07T00:00:00Z"
        }
        result = check_promotion_criteria(metadata)
        assert not result["should_promote"]
    
    def test_low_both_no_promotion(self, mock_config):
        """Test that low importance and retrieval don't trigger."""
        metadata = {
            "importance_score": "0.50",
            "retrieval_count": "2",
            "created_at": "2026-02-07T00:00:00Z"  # Recent
        }
        result = check_promotion_criteria(metadata)
        assert not result["should_promote"]
        assert result["reason"] == "No criteria met"
    
    def test_age_importance_combo(self, mock_config):
        """Test age + moderate importance combination."""
        # Old document with moderate importance
        metadata = {
            "importance_score": "0.75",
            "retrieval_count": "2",
            "created_at": "2025-12-01T00:00:00Z"  # >60 days ago
        }
        result = check_promotion_criteria(metadata)
        assert result["should_promote"]
        assert "age" in result["reason"]
    
    def test_custom_thresholds(self, mock_config):
        """Test with custom promotion thresholds."""
        # Set custom threshold
        mock_config.set(promotion={
            "importance_threshold": 0.95,
            "retrieval_count_threshold": 10
        })
        
        # Test with default high importance (0.90) - should not trigger now
        metadata = {
            "importance_score": "0.90",
            "retrieval_count": "5"
        }
        result = check_promotion_criteria(metadata)
        assert not result["should_promote"]
        
        # Test with new threshold
        metadata["importance_score"] = "0.96"
        result = check_promotion_criteria(metadata)
        assert result["should_promote"]


class TestPromote:
    """Test promote function."""
    
    def test_promote_observation_full_cycle(self, mock_config):
        """Test full promotion cycle for observation."""
        # Write Tier 2 observation
        write_result = tier2_write(
            content="Important observation to promote",
            content_type="observation",
            importance_score=0.9,
            tags=["test", "promotion"]
        )
        doc_id = write_result["id"]
        
        # Promote
        result = promote(doc_id)
        assert result["success"]
        assert result["file_written"]
        assert result["chromadb_updated"]
        assert result["needs_git_commit"]
        assert "promoted_path" in result
        assert "vault_id" in result
        
        # Verify file was written
        vault_path, _ = mock_config.vault_path, None
        full_path = os.path.join(vault_path, result["promoted_path"])
        assert os.path.exists(full_path)
        
        # Verify content
        with open(full_path) as f:
            content = f.read()
        assert "Important observation to promote" in content
        assert "type: observation" in content
        assert "importance: 0.9" in content
        assert "original_id:" in content
        assert "promoted_at:" in content
    
    def test_promote_pattern(self, mock_config):
        """Test promoting a pattern."""
        write_result = tier2_write(
            content="Behavioral pattern",
            content_type="pattern",
            name="test-pattern",
            importance_score=0.9
        )
        
        result = promote(write_result["id"])
        assert result["success"]
        assert "patterns_promoted" in result["promoted_path"] or "pattern" in result["promoted_path"]
    
    def test_promote_summary(self, mock_config):
        """Test promoting a summary."""
        write_result = tier2_write(
            content="Session summary",
            content_type="summary",
            session_id="test-123",
            importance_score=0.9
        )
        
        result = promote(write_result["id"])
        assert result["success"]
        assert "summaries_promoted" in result["promoted_path"] or "summar" in result["promoted_path"]
    
    def test_promote_not_found(self, mock_config):
        """Test promoting non-existent document."""
        result = promote("obs::nonexistent")
        assert not result["success"]
        assert "not found" in result["error"]
    
    def test_promote_tier1_rejected(self, mock_config):
        """Test that Tier 1 documents can't be promoted."""
        # First create and index a vault file
        from tools.memory import index_file
        test_file = mock_config.vault_path / "notes" / "tier1-test.md"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("# Tier 1 Test\nThis is a file-backed document")
        index_file("notes/tier1-test.md")
        
        # Try to promote using vault:: ID - should reject
        result = promote("vault::notes/tier1-test.md")
        assert not result["success"]
        assert "not Tier 2" in result["error"]
    
    def test_promote_already_promoted(self, mock_config):
        """Test idempotency - promoting twice."""
        # Write and promote
        write_result = tier2_write(
            content="Test",
            content_type="observation",
            importance_score=0.9
        )
        doc_id = write_result["id"]
        
        # First promotion
        result1 = promote(doc_id)
        assert result1["success"]
        
        # The document no longer exists in ChromaDB with obs:: ID
        # So second promotion should fail with "not found"
        result2 = promote(doc_id)
        assert not result2["success"]
        assert "not found" in result2["error"]
    
    def test_promote_creates_directory(self, mock_config):
        """Test that promotion creates target directory if needed."""
        # Ensure directory doesn't exist
        promotion_dir = get_path("observations_promoted")
        if os.path.exists(promotion_dir):
            os.rmdir(promotion_dir)
        
        # Write and promote
        write_result = tier2_write(
            content="Test",
            content_type="observation",
            importance_score=0.9
        )
        
        result = promote(write_result["id"])
        assert result["success"]
        assert os.path.exists(promotion_dir)
    
    def test_promote_verifies_frontmatter(self, mock_config):
        """Test that promoted file has correct frontmatter."""
        write_result = tier2_write(
            content="Test observation",
            content_type="observation",
            name="test-obs",
            importance_score=0.85,
            tags=["tag1", "tag2"],
            source="manual"
        )
        
        result = promote(write_result["id"])
        assert result["success"]
        
        # Read promoted file
        vault_path, _ = mock_config.vault_path, None
        full_path = os.path.join(vault_path, result["promoted_path"])
        with open(full_path) as f:
            content = f.read()
        
        # Verify frontmatter fields
        assert "---" in content
        assert "type: observation" in content
        assert "importance: 0.85" in content
        assert f"original_id: {write_result['id']}" in content
        assert "promoted_at:" in content
        assert "source: manual" in content
        assert "retrieval_count:" in content
        assert "- tag1" in content
        assert "- tag2" in content
    
    def test_promote_updates_chromadb(self, mock_config):
        """Test that promotion updates ChromaDB with new vault:: ID."""
        write_result = tier2_write(
            content="Test",
            content_type="observation",
            importance_score=0.9
        )
        doc_id = write_result["id"]
        
        result = promote(doc_id)
        assert result["success"]
        vault_id = result["vault_id"]
        
        # Verify old ID is gone
        collection = _get_collection()
        old_result = collection.get(ids=[doc_id])
        assert len(old_result["ids"]) == 0
        
        # Verify new vault:: ID exists
        new_result = collection.get(ids=[vault_id])
        assert len(new_result["ids"]) == 1
        assert new_result["metadatas"][0]["tier"] == "file"
        assert new_result["metadatas"][0]["promoted"] == "true"
        assert new_result["metadatas"][0]["original_tier2_id"] == doc_id
    
    def test_promote_unsupported_type(self, mock_config):
        """Test that unsupported types can't be promoted."""
        # Code type doesn't support promotion
        write_result = tier2_write(
            content="code snippet",
            content_type="code",
            name="test.py::func",
            importance_score=0.9
        )

        result = promote(write_result["id"])
        assert not result["success"]
        assert "does not support promotion" in result["error"]

    def test_promote_learning(self, mock_config):
        """Test promoting a learning."""
        write_result = tier2_write(
            content="PostToolUse hooks have empty tool_result",
            content_type="learning",
            importance_score=0.9
        )

        result = promote(write_result["id"])
        assert result["success"]
        assert "learning" in result["promoted_path"]

        # Verify file content
        full_path = os.path.join(mock_config.vault_path, result["promoted_path"])
        with open(full_path) as f:
            content = f.read()
        assert "type: learning" in content

    def test_promote_decision(self, mock_config):
        """Test promoting a decision."""
        write_result = tier2_write(
            content="Use Python MCP server over TypeScript",
            content_type="decision",
            name="python-mcp-decision",
            importance_score=0.9
        )

        result = promote(write_result["id"])
        assert result["success"]
        assert "decision" in result["promoted_path"]

        # Verify file content
        full_path = os.path.join(mock_config.vault_path, result["promoted_path"])
        with open(full_path) as f:
            content = f.read()
        assert "type: decision" in content

    def test_promote_with_project_dir_nests(self, mock_config):
        """Project-scoped observation promotes to nested directory."""
        write_result = tier2_write(
            content="Project-specific observation",
            content_type="observation",
            importance_score=0.9,
            extra_metadata={"project_dir": "my-project"},
        )

        result = promote(write_result["id"])
        assert result["success"]
        assert "my-project" in result["promoted_path"]

        # Verify nested directory exists
        full_path = os.path.join(mock_config.vault_path, result["promoted_path"])
        assert os.path.exists(full_path)
        assert "/my-project/" in full_path

    def test_promote_without_project_dir_flat(self, mock_config):
        """Global observation promotes to flat directory (no nesting)."""
        write_result = tier2_write(
            content="Global observation",
            content_type="observation",
            importance_score=0.9,
        )

        result = promote(write_result["id"])
        assert result["success"]
        # Should NOT have any project subdirectory between the promotion dir and filename
        # The filename should be directly under the observations path (no nested project dir)
        filename = os.path.basename(result["promoted_path"])
        parent_dir = os.path.basename(os.path.dirname(result["promoted_path"]))
        # parent should be the observations dir itself, not a project name
        assert filename.startswith("observation-")
        assert parent_dir != ""  # has a parent dir
        # Verify no nested subdirectory — the promoted path should end with dir/filename
        # (not dir/project/filename)
        assert "observation-" in result["promoted_path"]

    def test_promote_frontmatter_includes_scope_and_project(self, mock_config):
        """Promoted file frontmatter includes scope, project, and files."""
        write_result = tier2_write(
            content="Observation with full context",
            content_type="observation",
            importance_score=0.9,
            extra_metadata={
                "project_dir": "jarvis-plugin",
                "scope": "project",
                "relevant_files": "src/main.py,tests/test_main.py",
            },
        )

        result = promote(write_result["id"])
        assert result["success"]

        full_path = os.path.join(mock_config.vault_path, result["promoted_path"])
        with open(full_path) as f:
            content = f.read()

        assert "scope: project" in content
        assert "project: jarvis-plugin" in content
        assert "- src/main.py" in content
        assert "- tests/test_main.py" in content
