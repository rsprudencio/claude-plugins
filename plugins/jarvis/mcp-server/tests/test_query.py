"""Tests for memory query module."""
import os
import pytest
from tools.query import (
    query_vault, memory_read, memory_stats,
    _compute_relevance, _extract_preview, _translate_filter,
    _display_path
)


class TestComputeRelevance:
    """Tests for distance-to-relevance conversion."""

    def test_zero_distance_is_max_relevance(self):
        assert _compute_relevance(0.0) == 1.0

    def test_max_distance_is_zero_relevance(self):
        assert _compute_relevance(2.0) == 0.0

    def test_mid_distance(self):
        assert _compute_relevance(1.0) == 0.5

    def test_high_importance_boost(self):
        base = _compute_relevance(0.5, "medium")
        boosted = _compute_relevance(0.5, "high")
        assert boosted == base + 0.10

    def test_low_importance_penalty(self):
        base = _compute_relevance(0.5, "medium")
        penalized = _compute_relevance(0.5, "low")
        assert penalized == base - 0.05

    def test_clamped_to_zero(self):
        # Very high distance + low importance should not go below 0
        result = _compute_relevance(2.0, "low")
        assert result == 0.0

    def test_clamped_to_one(self):
        # Zero distance + high importance should not exceed 1
        result = _compute_relevance(0.0, "high")
        assert result == 1.0


class TestExtractPreview:
    """Tests for content preview extraction."""

    def test_strips_frontmatter(self):
        content = "---\ntype: note\nimportance: high\n---\n# Title\n\nActual content here."
        preview = _extract_preview(content)
        assert "---" not in preview
        assert "type: note" not in preview
        assert "Actual content here." in preview

    def test_strips_heading(self):
        content = "# My Heading\n\nBody text follows."
        preview = _extract_preview(content)
        assert "My Heading" not in preview
        assert "Body text follows." in preview

    def test_truncates_at_word_boundary(self):
        content = "A " * 100  # 200 chars
        preview = _extract_preview(content, max_len=150)
        assert len(preview) <= 154  # max_len + "..."
        assert preview.endswith("...")

    def test_short_content_not_truncated(self):
        content = "Short text."
        preview = _extract_preview(content)
        assert preview == "Short text."
        assert "..." not in preview

    def test_collapses_whitespace(self):
        content = "Line one.\n\n\nLine two.\n\nLine three."
        preview = _extract_preview(content)
        assert "  " not in preview


class TestTranslateFilter:
    """Tests for filter translation to ChromaDB where syntax."""

    def test_none_filter(self):
        assert _translate_filter(None) is None

    def test_empty_filter(self):
        assert _translate_filter({}) is None

    def test_single_directory_filter(self):
        result = _translate_filter({"directory": "journal"})
        assert result == {"directory": "journal"}

    def test_vault_entry_type_maps_to_vault_type(self):
        """Entry-type values like 'note' should map to vault_type field."""
        result = _translate_filter({"type": "note"})
        assert result == {"vault_type": "note"}

    def test_content_type_maps_to_type(self):
        """Content-type values like 'vault' should map to universal type field."""
        result = _translate_filter({"type": "vault"})
        assert result == {"type": "vault"}

    def test_memory_type_maps_to_type(self):
        """Content-type 'memory' should map to universal type field."""
        result = _translate_filter({"type": "memory"})
        assert result == {"type": "memory"}

    def test_multiple_filters_use_and(self):
        result = _translate_filter({"directory": "journal", "type": "note"})
        assert "$and" in result
        assert {"directory": "journal"} in result["$and"]
        assert {"vault_type": "note"} in result["$and"]

    def test_tags_filter_uses_contains(self):
        result = _translate_filter({"tags": "work,python"})
        assert result == {"tags": {"$contains": "work"}}

    def test_importance_filter(self):
        result = _translate_filter({"importance": "high"})
        assert result == {"importance": "high"}

    def test_empty_values_ignored(self):
        result = _translate_filter({"directory": "", "type": ""})
        assert result is None


class TestDisplayPath:
    """Tests for namespace stripping in display paths."""

    def test_vault_prefix_stripped(self):
        assert _display_path("vault::notes/test.md") == "notes/test.md"

    def test_bare_id_unchanged(self):
        assert _display_path("notes/test.md") == "notes/test.md"

    def test_memory_prefix_stripped(self):
        assert _display_path("memory::global::jarvis-trajectory") == "jarvis-trajectory"

    def test_obs_prefix_stripped(self):
        assert _display_path("obs::1738857000000") == "1738857000000"


class TestQueryVault:
    """Integration tests for vault semantic search."""

    def _reset_chromadb(self, mock_config):
        """Reset ChromaDB singleton for test isolation."""
        import tools.memory as mem
        mem._chroma_client = None
        mem._DB_DIR = str(mock_config.vault_path / ".test_query_db")

    def _index_test_files(self, mock_config):
        """Create and index test files."""
        from tools.memory import index_vault

        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        (notes_dir / "auth-decisions.md").write_text(
            "---\ntype: note\nimportance: high\ntags:\n  - security\n  - work\n---\n"
            "# Authentication Decisions\n\nWe decided to use OAuth 2.0 with PKCE flow."
        )
        (notes_dir / "python-tips.md").write_text(
            "---\ntype: note\nimportance: medium\n---\n"
            "# Python Tips\n\nUse list comprehensions for cleaner code."
        )

        journal_dir = mock_config.vault_path / "journal" / "jarvis" / "2026" / "02"
        journal_dir.mkdir(parents=True, exist_ok=True)
        (journal_dir / "20260207-test-entry.md").write_text(
            "---\ntype: journal\nimportance: medium\n---\n"
            "# Test Journal Entry\n\nDiscussed authentication architecture today."
        )

        index_vault()

    def test_query_vault_basic(self, mock_config):
        self._reset_chromadb(mock_config)
        self._index_test_files(mock_config)

        result = query_vault("authentication decisions")
        assert result["success"] is True
        assert result["query"] == "authentication decisions"
        assert len(result["results"]) > 0
        assert result["total_in_collection"] >= 3

        # Check result format
        first = result["results"][0]
        assert "rank" in first
        assert "path" in first
        assert "title" in first
        assert "type" in first
        assert "importance" in first
        assert "relevance" in first
        assert "preview" in first

        # Paths should NOT have vault:: prefix (stripped for display)
        for r in result["results"]:
            assert not r["path"].startswith("vault::")

        import tools.memory as mem
        mem._chroma_client = None

    def test_query_vault_with_filter(self, mock_config):
        self._reset_chromadb(mock_config)
        self._index_test_files(mock_config)

        result = query_vault("authentication", filter={"directory": "notes"})
        assert result["success"] is True
        for r in result["results"]:
            # All results should be from notes directory
            assert r["path"].startswith("notes/")

        import tools.memory as mem
        mem._chroma_client = None

    def test_query_vault_empty(self, mock_config):
        self._reset_chromadb(mock_config)
        # Don't index anything

        result = query_vault("anything")
        assert result["success"] is True
        assert result["results"] == []
        assert "No documents indexed" in result.get("message", "")

        import tools.memory as mem
        mem._chroma_client = None

    def test_query_vault_n_results_cap(self, mock_config):
        self._reset_chromadb(mock_config)
        self._index_test_files(mock_config)

        # Request more than 20 should be capped
        result = query_vault("test", n_results=50)
        assert result["success"] is True
        # Should not exceed 20 or total docs (whichever is smaller)
        assert len(result["results"]) <= 20

        import tools.memory as mem
        mem._chroma_client = None


class TestMemoryRead:
    """Tests for document read by ID."""

    def _reset_and_index(self, mock_config):
        import tools.memory as mem
        mem._chroma_client = None
        mem._DB_DIR = str(mock_config.vault_path / ".test_read_db")

        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        (notes_dir / "read-test.md").write_text("# Read Test\n\nContent for reading.")

        from tools.memory import index_file
        index_file("notes/read-test.md")

    def test_memory_read_basic(self, mock_config):
        self._reset_and_index(mock_config)

        # Read using bare path (backward compatible)
        result = memory_read(["notes/read-test.md"])
        assert result["success"] is True
        assert len(result["documents"]) == 1
        assert result["documents"][0]["id"] == "notes/read-test.md"
        assert "Content for reading" in result["documents"][0]["document"]
        assert "metadata" in result["documents"][0]

        import tools.memory as mem
        mem._chroma_client = None

    def test_memory_read_with_namespace(self, mock_config):
        """Should also work when called with full namespaced ID."""
        self._reset_and_index(mock_config)

        result = memory_read(["vault::notes/read-test.md"])
        assert result["success"] is True
        assert len(result["documents"]) == 1
        # Display path should have prefix stripped
        assert result["documents"][0]["id"] == "notes/read-test.md"

        import tools.memory as mem
        mem._chroma_client = None

    def test_memory_read_missing(self, mock_config):
        self._reset_and_index(mock_config)

        result = memory_read(["notes/read-test.md", "notes/nonexistent.md"])
        assert result["success"] is True
        assert len(result["documents"]) == 1
        assert "notes/nonexistent.md" in result["not_found"]

        import tools.memory as mem
        mem._chroma_client = None

    def test_memory_read_no_ids(self):
        result = memory_read([])
        assert result["success"] is False
        assert "No IDs" in result["error"]


class TestMemoryStats:
    """Tests for memory statistics."""

    def _reset_and_index(self, mock_config):
        import tools.memory as mem
        mem._chroma_client = None
        mem._DB_DIR = str(mock_config.vault_path / ".test_stats_db")

        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        (notes_dir / "stat-note-1.md").write_text(
            "---\ntype: note\n---\n# Note One\n\nFirst note."
        )
        (notes_dir / "stat-note-2.md").write_text(
            "---\ntype: idea\n---\n# Idea One\n\nAn idea."
        )

        from tools.memory import index_vault
        index_vault()

    def test_memory_stats(self, mock_config):
        self._reset_and_index(mock_config)

        result = memory_stats()
        assert result["success"] is True
        assert result["total_documents"] >= 2
        assert len(result["samples"]) > 0

        sample = result["samples"][0]
        assert "path" in sample
        assert "title" in sample
        assert "type" in sample
        # Paths should not have vault:: prefix
        assert not sample["path"].startswith("vault::")

        import tools.memory as mem
        mem._chroma_client = None

    def test_memory_stats_empty(self, mock_config):
        import tools.memory as mem
        mem._chroma_client = None
        mem._DB_DIR = str(mock_config.vault_path / ".test_stats_empty_db")

        result = memory_stats()
        assert result["success"] is True
        assert result["total_documents"] == 0
        assert result["samples"] == []
        assert "No documents indexed" in result.get("message", "")

        mem._chroma_client = None
