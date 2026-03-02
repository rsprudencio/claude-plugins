"""Tests for memory query module."""

import os
import pytest
from unittest.mock import patch
from tools.query import (
    query_vault,
    doc_read,
    collection_stats,
    memory_read,
    memory_stats,  # backward-compatible aliases
    _compute_relevance,
    _extract_preview,
    _display_path,
    _increment_retrieval_counts,
    semantic_context,
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

        recent = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        base = _compute_relevance(0.5, "medium")
        boosted = _compute_relevance(0.5, "medium", updated_at=recent)
        assert boosted == base + 0.08

    def test_recency_boost_within_week(self):
        from datetime import datetime, timezone, timedelta

        few_days = (datetime.now(timezone.utc) - timedelta(days=3)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
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
        content = (
            "---\ntype: note\nimportance: high\n---\n# Title\n\nActual content here."
        )
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

    def test_query_vault_with_filter(self, mock_config):
        self._index_test_files(mock_config)

        result = query_vault("authentication", filter={"directory": "notes"})
        assert result["success"] is True
        for r in result["results"]:
            # All results should be from notes directory
            assert r["path"].startswith("notes/")

    def test_query_vault_empty(self, mock_config):
        # Don't index anything
        result = query_vault("anything")
        assert result["success"] is True
        assert result["results"] == []
        assert "No documents indexed" in result.get("message", "")

    def test_query_vault_n_results_cap(self, mock_config):
        self._index_test_files(mock_config)

        # Request more than 20 should be capped
        result = query_vault("test", n_results=50)
        assert result["success"] is True
        # Should not exceed 20 or total docs (whichever is smaller)
        assert len(result["results"]) <= 20

    def test_expansion_metadata_in_response(self, mock_config):
        """When query triggers expansion, response should include expansion info."""
        self._index_test_files(mock_config)

        result = query_vault("auth flow setup")
        assert result["success"] is True
        assert "expansion" in result
        assert len(result["expansion"]["terms_added"]) > 0

    def test_expansion_disabled_no_metadata(self, mock_config):
        """When query doesn't trigger expansion, no expansion field in response."""
        self._index_test_files(mock_config)

        result = query_vault("quantum entanglement")
        assert result["success"] is True
        # No matching synonyms/intents -> no expansion field
        assert "expansion" not in result

    def test_chunk_deduplication(self, mock_config):
        """Multiple chunks from same file should be deduped to best match."""
        from tools.memory import index_file

        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        # Create a multi-section file about auth
        content = (
            "## Authentication Overview\n\n"
            + "OAuth authentication is used for secure login. " * 30
            + "\n\n"
            "## Authorization Rules\n\n"
            + "Authorization controls what users can access. " * 30
            + "\n\n"
            "## Unrelated Section\n\n" + "This section is about cooking recipes. " * 30
        )
        (notes_dir / "auth-guide.md").write_text(content)
        index_file("notes/auth-guide.md")

        result = query_vault("authentication", n_results=5)
        assert result["success"] is True

        # Should only appear once despite multiple matching chunks
        paths = [r["path"] for r in result["results"]]
        assert paths.count("notes/auth-guide.md") == 1

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

    def _index_test_file(self, mock_config):
        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        (notes_dir / "read-test.md").write_text("# Read Test\n\nContent for reading.")

        from tools.memory import index_file

        index_file("notes/read-test.md")

    def test_doc_read_basic(self, mock_config):
        self._index_test_file(mock_config)

        # Read using bare path (backward compatible)
        result = doc_read(["notes/read-test.md"])
        assert result["success"] is True
        assert len(result["documents"]) == 1
        assert result["documents"][0]["id"] == "vault::notes/read-test.md"
        assert result["documents"][0]["path"] == "notes/read-test.md"
        assert "Content for reading" in result["documents"][0]["document"]
        assert "metadata" in result["documents"][0]

    def test_doc_read_with_namespace(self, mock_config):
        """Should also work when called with full namespaced ID."""
        self._index_test_file(mock_config)

        result = doc_read(["vault::notes/read-test.md"])
        assert result["success"] is True
        assert len(result["documents"]) == 1
        # id is the raw namespaced ID, path is the display form
        assert result["documents"][0]["id"] == "vault::notes/read-test.md"
        assert result["documents"][0]["path"] == "notes/read-test.md"

    def test_doc_read_missing(self, mock_config):
        self._index_test_file(mock_config)

        result = doc_read(["notes/read-test.md", "notes/nonexistent.md"])
        assert result["success"] is True
        assert len(result["documents"]) == 1
        assert "notes/nonexistent.md" in result["not_found"]

    def test_doc_read_no_ids(self):
        result = doc_read([])
        assert result["success"] is False
        assert "No IDs" in result["error"]

    def test_backward_compat_alias(self):
        """memory_read should still work as alias for doc_read."""
        assert memory_read is doc_read


class TestCollectionStats:
    """Tests for collection statistics (renamed from memory_stats)."""

    def _index_test_files(self, mock_config):
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
        self._index_test_files(mock_config)

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

    def test_collection_stats_detailed(self, mock_config):
        self._index_test_files(mock_config)

        result = collection_stats(detailed=True)
        assert result["success"] is True
        assert "type_breakdown" in result
        assert "namespace_breakdown" in result
        assert "storage_bytes" in result
        assert "storage_mb" in result

        # All indexed docs should be vault type
        assert result["type_breakdown"].get("vault", 0) >= 2
        assert result["namespace_breakdown"].get("vault::", 0) >= 2

    def test_collection_stats_empty(self, mock_config):
        result = collection_stats()
        assert result["success"] is True
        assert result["total_documents"] == 0
        assert result["samples"] == []
        assert "No documents indexed" in result.get("message", "")

    def test_backward_compat_alias(self):
        """memory_stats should still work as alias for collection_stats."""
        assert memory_stats is collection_stats


class TestTierAwareQuery:
    """Tests for tier-aware query results."""

    def test_query_includes_tier_field(self, mock_config):
        """Test that query results include tier field."""
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

    def test_query_includes_source_field(self, mock_config):
        """Test that query results include source field."""
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

    def test_query_mixed_tier_results(self, mock_config):
        """Test query with both Tier 1 and Tier 2 results."""
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
            importance_score=0.8,
        )

        # Query
        result = query_vault("tier testing")
        assert result["success"]
        assert len(result["results"]) >= 2

        # Should have both tiers
        tiers = {res["tier"] for res in result["results"]}
        assert "file" in tiers or "chromadb" in tiers

    def test_query_increments_tier2_retrieval_count(self, mock_config):
        """Test that querying increments Tier 2 retrieval counts."""
        # Add Tier 2 observation
        from tools.tier2 import tier2_write, tier2_read

        write_result = tier2_write(
            content="Test observation for retrieval count", content_type="observation"
        )
        doc_id = write_result["id"]

        # Initial count should be 0
        read_result = tier2_read(doc_id)
        assert read_result["metadata"]["retrieval_count"] == "1.0"  # Read increments it

        # Query (should increment)
        query_vault("retrieval count")

        # Check count increased (read again increments, so should be 3)
        read_result2 = tier2_read(doc_id)
        assert float(read_result2["metadata"]["retrieval_count"]) >= 2


class TestIncrementRetrievalCountsFractional:
    """Tests for fractional retrieval count increments."""

    def test_fractional_increment(self, mock_config):
        """increment=0.01 adds 0.01 to count."""
        from tools.tier2 import tier2_write

        write_result = tier2_write(
            content="Test observation for fractional increment",
            content_type="observation",
        )
        doc_id = write_result["id"]

        _increment_retrieval_counts([doc_id], increment=0.01)

        row = mock_config.db.get(doc_id)
        assert row["metadata"]["retrieval_count"] == "0.01"

    def test_rounds_to_two_decimals(self, mock_config):
        """0.01 + 0.01 + 0.01 = 0.03 (no float noise)."""
        from tools.tier2 import tier2_write

        write_result = tier2_write(
            content="Test rounding",
            content_type="observation",
        )
        doc_id = write_result["id"]

        for _ in range(3):
            _increment_retrieval_counts([doc_id], increment=0.01)

        row = mock_config.db.get(doc_id)
        assert row["metadata"]["retrieval_count"] == "0.03"

    def test_default_increment_is_one(self, mock_config):
        """No increment arg -> adds 1.0 (backward compat)."""
        from tools.tier2 import tier2_write

        write_result = tier2_write(
            content="Test default increment",
            content_type="observation",
        )
        doc_id = write_result["id"]

        _increment_retrieval_counts([doc_id])

        row = mock_config.db.get(doc_id)
        assert row["metadata"]["retrieval_count"] == "1.0"

    def test_skips_tier1_ids(self, mock_config):
        """Tier 1 (vault::) IDs are not incremented."""
        from tools.memory import index_file

        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        (notes_dir / "skip-test.md").write_text("# Skip Test\nContent")
        index_file("notes/skip-test.md")

        # Should not crash on vault:: IDs
        _increment_retrieval_counts(
            ["vault::notes/skip-test.md"], increment=0.01
        )

        # Verify no retrieval_count was added to tier 1 doc
        row = mock_config.db.get("vault::notes/skip-test.md")
        meta = row["metadata"]
        assert meta.get("retrieval_count") is None or meta.get("retrieval_count") == "0"


class TestSemanticContextFractionalBump:
    """Tests for fractional retrieval bumps in semantic_context()."""

    def test_bumps_fractionally(self, mock_config):
        """semantic_context() calls increment with configured value."""
        mock_config.set(
            memory={
                "per_prompt_search": {"passive_retrieval_increment": 0.05},
            }
        )

        from tools.tier2 import tier2_write

        write_result = tier2_write(
            content="Observation about career goals and strategic planning",
            content_type="observation",
            importance_score=0.8,
        )
        doc_id = write_result["id"]

        # Call semantic_context (should fractionally increment)
        semantic_context("career goals", threshold=0.0)

        # Check retrieval count was bumped
        row = mock_config.db.get(doc_id)
        count = float(row["metadata"]["retrieval_count"])
        assert count == pytest.approx(0.05, abs=0.001)

    def test_no_bump_when_zero(self, mock_config):
        """passive_retrieval_increment=0 -> no increment call."""
        mock_config.set(
            memory={
                "per_prompt_search": {"passive_retrieval_increment": 0},
            }
        )

        from tools.tier2 import tier2_write

        write_result = tier2_write(
            content="Observation about career goals zero increment",
            content_type="observation",
            importance_score=0.8,
        )
        doc_id = write_result["id"]

        semantic_context("career goals zero increment", threshold=0.0)

        row = mock_config.db.get(doc_id)
        assert row["metadata"]["retrieval_count"] == "0"
