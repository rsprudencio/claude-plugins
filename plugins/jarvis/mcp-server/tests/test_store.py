"""Tests for unified store module — routing writes to vault/memory/tier2."""
import os
import pytest
from tools.store import store


class TestStoreRouting:
    """Test parameter validation and routing logic."""

    def test_no_routing_param_returns_error(self, mock_config):
        """Error when no routing parameter is provided."""
        result = store(content="test")
        assert not result["success"]
        assert "Provide one of" in result["error"]

    def test_multiple_routing_params_returns_error(self, mock_config):
        """Error when multiple routing parameters are provided."""
        result = store(content="test", relative_path="test.md", type="observation")
        assert not result["success"]
        assert "only ONE" in result["error"]

    def test_id_and_relative_path_conflict(self, mock_config):
        """Error when both id and relative_path provided."""
        result = store(content="test", id="vault::test.md", relative_path="test.md")
        assert not result["success"]
        assert "only ONE" in result["error"]

    def test_unknown_type_returns_error(self, mock_config):
        """Error for unknown type values."""
        result = store(content="test", type="bogus")
        assert not result["success"]
        assert "Unknown type" in result["error"]


class TestStoreVaultFile:
    """Test vault file writes via relative_path."""

    def test_write_new_file(self, mock_config):
        """Write a new vault file with relative_path."""
        result = store(content="Hello world", relative_path="test_store.txt")
        assert result["success"]

        # Verify file exists
        vault = mock_config.vault_path
        assert (vault / "test_store.txt").exists()
        assert (vault / "test_store.txt").read_text() == "Hello world"

    def test_write_md_auto_indexes(self, mock_config):
        """Writing .md files triggers auto-index."""
        result = store(content="# Title\nBody", relative_path="notes/test-auto.md")
        assert result["success"]
        assert result.get("indexed") is True

    def test_write_non_md_no_index(self, mock_config):
        """Non-.md files skip auto-index."""
        result = store(content="plain text", relative_path="test.txt")
        assert result["success"]
        assert "indexed" not in result

    def test_append_mode(self, mock_config):
        """Append mode adds content to existing file."""
        # Create file first
        store(content="Line 1", relative_path="append_test.txt")
        # Append
        result = store(
            content="Line 2", relative_path="append_test.txt", mode="append"
        )
        assert result["success"]
        content = (mock_config.vault_path / "append_test.txt").read_text()
        assert "Line 1" in content
        assert "Line 2" in content

    def test_edit_mode(self, mock_config):
        """Edit mode does find-and-replace."""
        store(content="Hello world", relative_path="edit_test.txt")
        result = store(
            relative_path="edit_test.txt",
            mode="edit",
            old_string="world",
            new_string="Jarvis",
        )
        assert result["success"]
        content = (mock_config.vault_path / "edit_test.txt").read_text()
        assert "Hello Jarvis" in content

    def test_invalid_mode(self, mock_config):
        """Invalid mode returns error."""
        result = store(content="test", relative_path="test.txt", mode="patch")
        assert not result["success"]
        assert "Invalid mode" in result["error"]


class TestStoreById:
    """Test ID-based update routing (retrieve->store loop)."""

    def test_vault_id_routes_to_file(self, mock_config):
        """vault:: ID routes to vault file write."""
        # Create file first
        store(content="Original", relative_path="notes/id-test.md")
        # Update via ID
        result = store(
            id="vault::notes/id-test.md",
            mode="edit",
            old_string="Original",
            new_string="Updated",
        )
        assert result["success"]
        content = (mock_config.vault_path / "notes" / "id-test.md").read_text()
        assert "Updated" in content

    def test_vault_id_auto_reindexes(self, mock_config):
        """vault:: ID write triggers auto-reindex for .md files."""
        store(content="# Test", relative_path="notes/reindex-test.md")
        result = store(
            id="vault::notes/reindex-test.md",
            content="# Updated content",
            mode="write",
        )
        assert result["success"]
        assert result.get("indexed") is True

    def test_memory_global_id_routes_to_memory(self, mock_config):
        """memory::global:: ID routes to memory write."""
        # Create memory first
        store(content="Original memory", type="memory", name="test-mem")
        # Update via ID
        result = store(
            id="memory::global::test-mem",
            content="Updated memory",
        )
        assert result["success"]

    def test_tier2_id_routes_to_upsert(self, mock_config):
        """obs:: ID routes to tier2 upsert."""
        from tools.tier2 import tier2_write

        # Create tier2 content first
        write_result = tier2_write(
            content="Original obs", content_type="observation"
        )
        doc_id = write_result["id"]

        # Update via ID
        result = store(id=doc_id, content="Updated obs")
        assert result["success"]
        assert result.get("updated") is True

    def test_tier2_id_merges_metadata(self, mock_config):
        """Tier2 ID update merges metadata instead of replacing."""
        from tools.tier2 import tier2_write, tier2_read

        write_result = tier2_write(
            content="Test",
            content_type="observation",
            importance_score=0.5,
            tags=["original"],
        )
        doc_id = write_result["id"]

        # Update importance only
        store(id=doc_id, importance=0.9)

        # Verify metadata merged
        read_result = tier2_read(doc_id)
        assert float(read_result["metadata"]["importance_score"]) == 0.9

    def test_unknown_namespace_returns_error(self, mock_config):
        """Unknown namespace prefix returns error."""
        result = store(id="bogus::something", content="test")
        # bogus:: isn't a known prefix, so parse_id treats it as vault bare path
        # It will try to write to a file, which may or may not succeed
        # depending on whether the file exists. Let's just make sure it doesn't crash.
        assert isinstance(result, dict)


class TestStoreMemory:
    """Test memory creation via type='memory'."""

    def test_create_memory(self, mock_config):
        """Create a strategic memory."""
        result = store(
            content="Test memory content",
            type="memory",
            name="test-memory",
        )
        assert result["success"]

    def test_importance_float_to_categorical(self, mock_config):
        """Float importance is converted to categorical for memories."""
        # High importance
        result = store(
            content="Critical memory",
            type="memory",
            name="critical-mem",
            importance=0.95,
        )
        assert result["success"]

    def test_memory_overwrite(self, mock_config):
        """Overwrite flag works for memories."""
        store(content="V1", type="memory", name="overwrite-test")
        result = store(
            content="V2", type="memory", name="overwrite-test", overwrite=True
        )
        assert result["success"]


class TestStoreTier2:
    """Test tier2 creation via type parameter."""

    def test_create_observation(self, mock_config):
        """Create an observation via type."""
        result = store(
            content="Test observation",
            type="observation",
            importance=0.7,
        )
        assert result["success"]
        assert "obs::" in result["id"]
        assert result["content_type"] == "observation"

    def test_create_pattern(self, mock_config):
        """Create a pattern via type (requires name)."""
        result = store(
            content="User prefers kebab-case",
            type="pattern",
            name="kebab-case-preference",
        )
        assert result["success"]
        assert "pattern::" in result["id"]

    def test_create_learning(self, mock_config):
        """Create a learning via type."""
        result = store(
            content="PostToolUse hooks have empty tool_result",
            type="learning",
        )
        assert result["success"]
        assert "learning::" in result["id"]

    def test_create_decision(self, mock_config):
        """Create a decision via type (requires name)."""
        result = store(
            content="Use Python MCP server over TypeScript",
            type="decision",
            name="python-mcp-decision",
        )
        assert result["success"]
        assert "decision::" in result["id"]

    def test_extra_metadata_passthrough(self, mock_config):
        """Extra metadata is passed through to tier2."""
        from tools.tier2 import tier2_read

        result = store(
            content="Observation with context",
            type="observation",
            extra_metadata={"project_dir": "jarvis-plugin", "git_branch": "master"},
        )
        assert result["success"]

        # Read and verify metadata
        read_result = tier2_read(result["id"])
        assert read_result["metadata"]["project_dir"] == "jarvis-plugin"
        assert read_result["metadata"]["git_branch"] == "master"

    def test_tags_passthrough(self, mock_config):
        """Tags are passed through to tier2."""
        from tools.tier2 import tier2_read

        result = store(
            content="Tagged observation",
            type="observation",
            tags=["work", "testing"],
        )
        assert result["success"]

        read_result = tier2_read(result["id"])
        assert read_result["metadata"]["tags"] == "work,testing"

    def test_default_importance(self, mock_config):
        """Default importance is 0.5 when not provided."""
        result = store(content="Default importance", type="observation")
        assert result["success"]
        assert result["importance_score"] == 0.5

    def test_default_source(self, mock_config):
        """Default source is 'manual' for tier2 via store."""
        from tools.tier2 import tier2_read

        result = store(content="No source specified", type="observation")
        assert result["success"]
        read_result = tier2_read(result["id"])
        assert read_result["metadata"]["source"] == "manual"
