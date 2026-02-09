"""Tests for memory query module."""
import os
import pytest
from tools.query import (
    query_vault, doc_read, collection_stats,
    memory_read, memory_stats,  # backward-compatible aliases
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

    def test_critical_importance_boost(self):
        base = _compute_relevance(0.5, "medium")
        boosted = _compute_relevance(0.5, "critical")
        assert boosted == base + 0.12

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

    def test_recency_boost_within_day(self):
        from datetime import datetime, timezone, timedelta
        recent = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        base = _compute_relevance(0.5, "medium")
        boosted = _compute_relevance(0.5, "medium", updated_at=recent)
        assert boosted == base + 0.08

    def test_recency_boost_within_week(self):
        from datetime import datetime, timezone, timedelta
        few_days = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        base = _compute_relevance(0.5, "medium")
        boosted = _compute_relevance(0.5, "medium", updated_at=few_days)
        assert boosted == base + 0.05

    def test_no_recency_boost_old(self):
        old = "2020-01-01T00:00:00Z"
        base = _compute_relevance(0.5, "medium")
        same = _compute_relevance(0.5, "medium", updated_at=old)
        assert same == base

    def test_no_recency_boost_none(self):
        base = _compute_relevance(0.5, "medium")
        same = _compute_relevance(0.5, "medium", updated_at=None)
        assert same == base

    def test_invalid_date_no_crash(self):
        result = _compute_relevance(0.5, "medium", updated_at="not-a-date")
        assert isinstance(result, float)


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
        # Point db_path at a temp directory via config
        mock_config.set(memory={"db_path": str(mock_config.vault_path / ".test_query_db")})

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

    def test_expansion_metadata_in_response(self, mock_config):
        """When query triggers expansion, response should include expansion info."""
        self._reset_chromadb(mock_config)
        self._index_test_files(mock_config)

        result = query_vault("auth flow setup")
        assert result["success"] is True
        assert "expansion" in result
        assert len(result["expansion"]["terms_added"]) > 0

        import tools.memory as mem
        mem._chroma_client = None

    def test_expansion_disabled_no_metadata(self, mock_config):
        """When query doesn't trigger expansion, no expansion field in response."""
        self._reset_chromadb(mock_config)
        self._index_test_files(mock_config)

        result = query_vault("quantum entanglement")
        assert result["success"] is True
        # No matching synonyms/intents → no expansion field
        assert "expansion" not in result

        import tools.memory as mem
        mem._chroma_client = None

    def test_chunk_deduplication(self, mock_config):
        """Multiple chunks from same file should be deduped to best match."""
        import tools.memory as mem
        mem._chroma_client = None
        mock_config.set(memory={"db_path": str(mock_config.vault_path / ".test_dedup_db")})

        from tools.memory import index_file
        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        # Create a multi-section file about auth
        content = (
            "## Authentication Overview\n\n"
            + "OAuth authentication is used for secure login. " * 30 + "\n\n"
            "## Authorization Rules\n\n"
            + "Authorization controls what users can access. " * 30 + "\n\n"
            "## Unrelated Section\n\n"
            + "This section is about cooking recipes. " * 30
        )
        (notes_dir / "auth-guide.md").write_text(content)
        index_file("notes/auth-guide.md")

        result = query_vault("authentication", n_results=5)
        assert result["success"] is True

        # Should only appear once despite multiple matching chunks
        paths = [r["path"] for r in result["results"]]
        assert paths.count("notes/auth-guide.md") == 1

        mem._chroma_client = None

    def test_importance_score_affects_relevance(self):
        """Documents with higher importance_score should get relevance boost."""
        low_score = _compute_relevance(0.5, importance_score=0.3)
        high_score = _compute_relevance(0.5, importance_score=0.9)
        assert high_score > low_score

    def test_importance_score_backward_compat(self):
        """When importance_score is None, should fall back to string importance."""
        base = _compute_relevance(0.5, "medium", importance_score=None)
        boosted = _compute_relevance(0.5, "high", importance_score=None)
        assert boosted == base + 0.10


class TestDocRead:
    """Tests for document read by ID (renamed from memory_read)."""

    def _reset_and_index(self, mock_config):
        import tools.memory as mem
        mem._chroma_client = None
        mock_config.set(memory={"db_path": str(mock_config.vault_path / ".test_read_db")})

        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        (notes_dir / "read-test.md").write_text("# Read Test\n\nContent for reading.")

        from tools.memory import index_file
        index_file("notes/read-test.md")

    def test_doc_read_basic(self, mock_config):
        self._reset_and_index(mock_config)

        # Read using bare path (backward compatible)
        result = doc_read(["notes/read-test.md"])
        assert result["success"] is True
        assert len(result["documents"]) == 1
        assert result["documents"][0]["id"] == "notes/read-test.md"
        assert "Content for reading" in result["documents"][0]["document"]
        assert "metadata" in result["documents"][0]

        import tools.memory as mem
        mem._chroma_client = None

    def test_doc_read_with_namespace(self, mock_config):
        """Should also work when called with full namespaced ID."""
        self._reset_and_index(mock_config)

        result = doc_read(["vault::notes/read-test.md"])
        assert result["success"] is True
        assert len(result["documents"]) == 1
        # Display path should have prefix stripped
        assert result["documents"][0]["id"] == "notes/read-test.md"

        import tools.memory as mem
        mem._chroma_client = None

    def test_doc_read_missing(self, mock_config):
        self._reset_and_index(mock_config)

        result = doc_read(["notes/read-test.md", "notes/nonexistent.md"])
        assert result["success"] is True
        assert len(result["documents"]) == 1
        assert "notes/nonexistent.md" in result["not_found"]

        import tools.memory as mem
        mem._chroma_client = None

    def test_doc_read_no_ids(self):
        result = doc_read([])
        assert result["success"] is False
        assert "No IDs" in result["error"]

    def test_backward_compat_alias(self):
        """memory_read should still work as alias for doc_read."""
        assert memory_read is doc_read


class TestCollectionStats:
    """Tests for collection statistics (renamed from memory_stats)."""

    def _reset_and_index(self, mock_config):
        import tools.memory as mem
        mem._chroma_client = None
        mock_config.set(memory={"db_path": str(mock_config.vault_path / ".test_stats_db")})

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

    def test_collection_stats_basic(self, mock_config):
        self._reset_and_index(mock_config)

        result = collection_stats()
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

    def test_collection_stats_detailed(self, mock_config):
        self._reset_and_index(mock_config)

        result = collection_stats(detailed=True)
        assert result["success"] is True
        assert "type_breakdown" in result
        assert "namespace_breakdown" in result
        assert "storage_bytes" in result
        assert "storage_mb" in result

        # All indexed docs should be vault type
        assert result["type_breakdown"].get("vault", 0) >= 2
        assert result["namespace_breakdown"].get("vault::", 0) >= 2

        import tools.memory as mem
        mem._chroma_client = None

    def test_collection_stats_empty(self, mock_config):
        import tools.memory as mem
        mem._chroma_client = None
        mock_config.set(memory={"db_path": str(mock_config.vault_path / ".test_stats_empty_db")})

        result = collection_stats()
        assert result["success"] is True
        assert result["total_documents"] == 0
        assert result["samples"] == []
        assert "No documents indexed" in result.get("message", "")

        mem._chroma_client = None

    def test_backward_compat_alias(self):
        """memory_stats should still work as alias for collection_stats."""
        assert memory_stats is collection_stats



class TestTierAwareQuery:
    """Tests for tier-aware query results."""
    
    def test_query_includes_tier_field(self, mock_config):
        """Test that query results include tier field."""
        import tools.memory as mem
        mem._chroma_client = None
        mock_config.set(memory={"db_path": str(mock_config.vault_path / ".test_tier_query_db")})
        
        # Index a file
        from tools.memory import index_file
        test_file = mock_config.vault_path / "notes" / "test.md"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("# Test\nTest content")
        index_file("notes/test.md")
        
        # Query
        result = query_vault("test")
        assert result["success"]
        assert len(result["results"]) > 0
        
        # Check tier field
        for res in result["results"]:
            assert "tier" in res
            assert res["tier"] == "file"  # Vault files are Tier 1
        
        mem._chroma_client = None
    
    def test_query_includes_source_field(self, mock_config):
        """Test that query results include source field."""
        import tools.memory as mem
        mem._chroma_client = None
        mock_config.set(memory={"db_path": str(mock_config.vault_path / ".test_source_query_db")})
        
        # Index a file
        from tools.memory import index_file
        test_file = mock_config.vault_path / "notes" / "test.md"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("# Test\nTest content")
        index_file("notes/test.md")
        
        # Query
        result = query_vault("test")
        assert result["success"]
        
        # Check source field
        for res in result["results"]:
            assert "source" in res
            assert res["source"] == "file"  # Tier 1 files have source="file"
        
        mem._chroma_client = None
    
    def test_query_mixed_tier_results(self, mock_config):
        """Test query with both Tier 1 and Tier 2 results."""
        import tools.memory as mem
        mem._chroma_client = None
        mock_config.set(memory={"db_path": str(mock_config.vault_path / ".test_mixed_tier_db")})
        
        # Index a Tier 1 file
        from tools.memory import index_file
        test_file = mock_config.vault_path / "notes" / "test.md"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("# Test\nTest content for tier testing")
        index_file("notes/test.md")
        
        # Add a Tier 2 observation
        from tools.tier2 import tier2_write
        tier2_write(
            content="Tier 2 test content for tier testing",
            content_type="observation",
            importance_score=0.8
        )
        
        # Query
        result = query_vault("tier testing")
        assert result["success"]
        assert len(result["results"]) >= 2
        
        # Should have both tiers
        tiers = {res["tier"] for res in result["results"]}
        assert "file" in tiers or "chromadb" in tiers
        
        mem._chroma_client = None
    
    def test_query_increments_tier2_retrieval_count(self, mock_config):
        """Test that querying increments Tier 2 retrieval counts."""
        import tools.memory as mem
        mem._chroma_client = None
        mock_config.set(memory={"db_path": str(mock_config.vault_path / ".test_retrieval_db")})
        
        # Add Tier 2 observation
        from tools.tier2 import tier2_write, tier2_read
        write_result = tier2_write(
            content="Test observation for retrieval count",
            content_type="observation"
        )
        doc_id = write_result["id"]
        
        # Initial count should be 0
        read_result = tier2_read(doc_id)
        assert read_result["metadata"]["retrieval_count"] == "1"  # Read increments it
        
        # Query (should increment)
        query_vault("retrieval count")
        
        # Check count increased (read again increments, so should be 3)
        read_result2 = tier2_read(doc_id)
        assert int(read_result2["metadata"]["retrieval_count"]) >= 2
        
        mem._chroma_client = None
