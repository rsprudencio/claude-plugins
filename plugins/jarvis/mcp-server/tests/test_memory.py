"""Tests for memory indexing module."""
import os
import pytest
from tools.memory import (
    _parse_frontmatter, _extract_title, _build_metadata,
    _should_skip, index_vault, index_file,
)


class TestParseFrontmatter:
    """Tests for YAML frontmatter parsing."""

    def test_basic_frontmatter(self):
        content = "---\ntype: note\nimportance: high\n---\n# Title\nBody"
        fm = _parse_frontmatter(content)
        assert fm["type"] == "note"
        assert fm["importance"] == "high"

    def test_frontmatter_with_tags_list(self):
        content = "---\ntags:\n  - jarvis\n  - work\n  - python\n---\n# Title"
        fm = _parse_frontmatter(content)
        assert "tags" in fm
        assert "jarvis" in fm["tags"]
        assert "work" in fm["tags"]

    def test_no_frontmatter(self):
        content = "# Just a title\n\nSome body text."
        fm = _parse_frontmatter(content)
        assert fm == {}

    def test_quoted_values(self):
        content = '---\njarvis_id: "20260206143052"\ntitle: "My Note"\n---\n'
        fm = _parse_frontmatter(content)
        assert fm["jarvis_id"] == "20260206143052"
        assert fm["title"] == "My Note"


class TestExtractTitle:
    """Tests for title extraction."""

    def test_h1_heading(self):
        content = "---\ntype: note\n---\n# My Great Title\n\nBody here."
        title = _extract_title(content, "my-file.md")
        assert title == "My Great Title"

    def test_fallback_to_filename(self):
        content = "No heading here, just text."
        title = _extract_title(content, "my-great-note.md")
        assert title == "My Great Note"

    def test_h2_not_used(self):
        content = "## This is H2, not H1\n\nBody."
        title = _extract_title(content, "fallback-name.md")
        assert title == "Fallback Name"


class TestBuildMetadata:
    """Tests for ChromaDB metadata construction."""

    def test_universal_fields_present(self):
        meta = _build_metadata({}, "notes/test.md")
        assert meta["type"] == "vault"
        assert meta["namespace"] == "vault::"
        assert meta["source"] == "vault-index"
        assert "created_at" in meta
        assert "updated_at" in meta
        assert meta["chunk_index"] == 0
        assert meta["chunk_total"] == 1

    def test_vault_type_from_frontmatter(self):
        fm = {"type": "incident-log", "tags": "jarvis,work", "importance": "high"}
        meta = _build_metadata(fm, "journal/jarvis/2026/01/entry.md")
        # Universal type is always "vault" for vault content
        assert meta["type"] == "vault"
        # Old frontmatter type is preserved as vault_type
        assert meta["vault_type"] == "incident-log"
        assert meta["tags"] == "jarvis,work"
        assert meta["importance"] == "high"
        assert meta["directory"] == "journal"
        assert meta["has_frontmatter"] == "true"

    def test_vault_type_inferred_from_directory(self):
        meta = _build_metadata({}, "notes/my-note.md")
        assert meta["vault_type"] == "note"
        assert meta["importance"] == "medium"
        assert meta["has_frontmatter"] == "false"

    def test_directory_inference(self):
        assert _build_metadata({}, "journal/test.md")["vault_type"] == "journal"
        assert _build_metadata({}, "work/test.md")["vault_type"] == "work"
        assert _build_metadata({}, "inbox/test.md")["vault_type"] == "inbox"
        assert _build_metadata({}, "random/test.md")["vault_type"] == "unknown"

    def test_all_inferred_have_vault_type(self):
        """All vault metadata must have type=vault and a vault_type."""
        for path in ("notes/a.md", "journal/b.md", "work/c.md"):
            meta = _build_metadata({}, path)
            assert meta["type"] == "vault"
            assert "vault_type" in meta


class TestShouldSkip:
    """Tests for file skip logic."""

    def test_skip_obsidian(self):
        assert _should_skip(".obsidian/plugins/foo.md", False) is True

    def test_skip_git(self):
        assert _should_skip(".git/config", False) is True

    def test_skip_templates(self):
        assert _should_skip("templates/daily.md", False) is True

    def test_skip_sensitive_by_default(self):
        assert _should_skip("documents/passport.md", False) is True
        assert _should_skip("people/john.md", False) is True

    def test_include_sensitive_when_requested(self):
        assert _should_skip("documents/passport.md", True) is False
        assert _should_skip("people/john.md", True) is False

    def test_allow_normal_dirs(self):
        assert _should_skip("notes/my-note.md", False) is False
        assert _should_skip("journal/jarvis/2026/01/entry.md", False) is False


class TestIndexVault:
    """Integration tests for bulk vault indexing."""

    def test_index_vault_requires_config(self, no_config):
        result = index_vault()
        assert result["success"] is False
        assert "no vault_path" in result["error"].lower()

    def test_index_vault_with_files(self, mock_config):
        """Should index .md files with namespaced IDs."""
        import tools.memory as mem
        mem._chroma_client = None
        mock_config.set(memory={"db_path": str(mock_config.vault_path / ".test_memory_db")})

        # Create test files
        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        (notes_dir / "test-note.md").write_text("# Test Note\n\nSome content here.")
        (notes_dir / "another.md").write_text("---\ntype: idea\n---\n# Another\n\nIdea content.")

        result = index_vault()
        assert result["success"] is True
        assert result["files_indexed"] >= 2
        assert result["collection_total"] >= 2

        # Verify IDs have vault:: prefix
        collection = mem._get_collection()
        all_data = collection.get()
        for doc_id in all_data["ids"]:
            assert doc_id.startswith("vault::"), f"ID {doc_id} missing vault:: prefix"

        # Verify metadata has universal fields
        for meta in all_data["metadatas"]:
            assert meta["type"] == "vault"
            assert meta["namespace"] == "vault::"
            assert "vault_type" in meta
            assert "created_at" in meta

        mem._chroma_client = None

    def test_index_vault_skips_templates(self, mock_config):
        """Should skip templates directory."""
        import tools.memory as mem
        mem._chroma_client = None
        mock_config.set(memory={"db_path": str(mock_config.vault_path / ".test_memory_db2")})

        templates_dir = mock_config.vault_path / "templates"
        templates_dir.mkdir(exist_ok=True)
        (templates_dir / "daily.md").write_text("# Template\n\nContent")

        result = index_vault()
        assert result["success"] is True
        assert result["files_skipped"] >= 1

        mem._chroma_client = None


class TestIndexFile:
    """Tests for single file indexing."""

    def test_index_single_file(self, mock_config):
        """Should index a single file with namespaced ID."""
        import tools.memory as mem
        mem._chroma_client = None
        mock_config.set(memory={"db_path": str(mock_config.vault_path / ".test_memory_db3")})

        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        (notes_dir / "single.md").write_text("# Single File\n\nTest content for indexing.")

        result = index_file("notes/single.md")
        assert result["success"] is True
        assert result["id"] == "vault::notes/single.md"
        assert result["title"] == "Single File"
        assert result["chunks"] == 1
        assert result["metadata"]["type"] == "vault"
        assert result["metadata"]["vault_type"] == "note"

        mem._chroma_client = None

    def test_index_nonexistent_file(self, mock_config):
        result = index_file("notes/does-not-exist.md")
        assert result["success"] is False
        assert "not found" in result["error"].lower()


class TestCollectionCreation:
    """Tests for ChromaDB collection creation."""

    def test_fresh_install_creates_jarvis(self, mock_config):
        """If no collection exists, _get_collection() creates 'jarvis'."""
        import tools.memory as mem
        mem._chroma_client = None
        mock_config.set(memory={"db_path": str(mock_config.vault_path / ".test_fresh_install_db")})

        collection = mem._get_collection()
        assert collection.name == "jarvis"

        mem._chroma_client = None



class TestChunkingIntegration:
    """Tests for chunking integration in the indexing pipeline."""

    def test_index_file_with_headings_creates_chunks(self, mock_config):
        """A file with H2 headings should produce multiple chunks."""
        import tools.memory as mem
        mem._chroma_client = None
        mock_config.set(memory={"db_path": str(mock_config.vault_path / ".test_chunk_h2_db")})

        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        content = (
            "---\ntype: note\n---\n# Big Doc\n\n"
            "## Section One\n\n" + "Content A. " * 60 + "\n\n"
            "## Section Two\n\n" + "Content B. " * 60 + "\n\n"
            "## Section Three\n\n" + "Content C. " * 60
        )
        (notes_dir / "chunked.md").write_text(content)

        result = index_file("notes/chunked.md")
        assert result["success"] is True
        assert result["chunks"] >= 2

        # Verify chunk IDs in collection
        collection = mem._get_collection()
        all_data = collection.get(include=["metadatas"])
        chunk_ids = [i for i in all_data["ids"] if "chunked.md" in i]
        assert len(chunk_ids) >= 2

        # Verify chunk metadata
        for i, doc_id in enumerate(all_data["ids"]):
            meta = all_data["metadatas"][i]
            if "chunked.md" in doc_id:
                assert "parent_file" in meta
                assert meta["parent_file"] == "notes/chunked.md"
                assert "chunk_heading" in meta
                assert meta["chunk_total"] >= 2

        mem._chroma_client = None

    def test_index_file_without_headings_single_doc(self, mock_config):
        """Short file without headings should produce a single document."""
        import tools.memory as mem
        mem._chroma_client = None
        mock_config.set(memory={"db_path": str(mock_config.vault_path / ".test_chunk_single_db")})

        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        (notes_dir / "short.md").write_text("# Short Note\n\nJust a brief note.")

        result = index_file("notes/short.md")
        assert result["success"] is True
        assert result["chunks"] == 1
        assert result["id"] == "vault::notes/short.md"

        mem._chroma_client = None

    def test_index_file_chunk_ids_format(self, mock_config):
        """Multi-chunk IDs should use vault::path#chunk-N format."""
        import tools.memory as mem
        mem._chroma_client = None
        mock_config.set(memory={"db_path": str(mock_config.vault_path / ".test_chunk_ids_db")})

        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        content = (
            "## Section A\n\n" + "Alpha content. " * 60 + "\n\n"
            "## Section B\n\n" + "Beta content. " * 60
        )
        (notes_dir / "multi.md").write_text(content)

        result = index_file("notes/multi.md")
        assert result["success"] is True
        assert result["chunks"] >= 2

        collection = mem._get_collection()
        all_data = collection.get()
        for doc_id in all_data["ids"]:
            assert doc_id.startswith("vault::notes/multi.md#chunk-")

        mem._chroma_client = None

    def test_reindex_updates_chunk_count(self, mock_config):
        """Re-indexing a file should clean up old chunks and create new ones."""
        import tools.memory as mem
        mem._chroma_client = None
        mock_config.set(memory={"db_path": str(mock_config.vault_path / ".test_reindex_db")})

        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)

        # First index: 3 sections
        content_v1 = "\n\n".join([
            f"## Section {i}\n\n" + f"Content {i}. " * 60
            for i in range(3)
        ])
        (notes_dir / "evolving.md").write_text(content_v1)
        result1 = index_file("notes/evolving.md")
        chunks_v1 = result1["chunks"]

        # Re-index: 2 sections
        content_v2 = "\n\n".join([
            f"## Section {i}\n\n" + f"Updated content {i}. " * 60
            for i in range(2)
        ])
        (notes_dir / "evolving.md").write_text(content_v2)
        result2 = index_file("notes/evolving.md")

        # Old chunks should be cleaned up
        collection = mem._get_collection()
        all_data = collection.get()
        evolving_ids = [i for i in all_data["ids"] if "evolving.md" in i]
        assert len(evolving_ids) == result2["chunks"]

        mem._chroma_client = None

    def test_index_vault_with_chunking(self, mock_config):
        """Bulk indexing should also produce chunks."""
        import tools.memory as mem
        mem._chroma_client = None
        mock_config.set(memory={"db_path": str(mock_config.vault_path / ".test_vault_chunk_db")})

        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        content = (
            "## Part 1\n\n" + "First part. " * 60 + "\n\n"
            "## Part 2\n\n" + "Second part. " * 60
        )
        (notes_dir / "bulk-test.md").write_text(content)

        result = index_vault()
        assert result["success"] is True
        assert result["files_indexed"] == 1  # 1 file
        assert result["chunks_total"] >= 2  # Multiple chunks from headings

        mem._chroma_client = None

    def test_importance_score_in_metadata(self, mock_config):
        """Indexed files should have importance_score float in metadata."""
        import tools.memory as mem
        mem._chroma_client = None
        mock_config.set(memory={"db_path": str(mock_config.vault_path / ".test_score_db")})

        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        (notes_dir / "scored.md").write_text(
            "---\nimportance: high\n---\n# Important Decision\n\nArchitecture decision content."
        )

        result = index_file("notes/scored.md")
        assert result["success"] is True
        meta = result["metadata"]
        assert "importance_score" in meta
        score = float(meta["importance_score"])
        assert 0.0 <= score <= 1.0
        # High frontmatter + "decision"/"architecture" concepts should yield good score
        assert score >= 0.7

        mem._chroma_client = None

    def test_per_chunk_importance_scoring(self, mock_config):
        """Chunks should get individual importance scores based on their content."""
        import tools.memory as mem
        mem._chroma_client = None
        mock_config.set(memory={"db_path": str(mock_config.vault_path / ".test_perchunk_score_db")})

        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        # Chunk 0: has "architecture decision" concepts → higher score
        # Chunk 1: generic filler content → lower score
        content = (
            "## Architecture Decision\n\n"
            + "This is a critical architecture decision about the system. " * 30 + "\n\n"
            "## Shopping List\n\n"
            + "Buy milk and eggs from the store. " * 30
        )
        (notes_dir / "mixed-importance.md").write_text(content)

        index_file("notes/mixed-importance.md")

        collection = mem._get_collection()
        all_data = collection.get(include=["metadatas"])
        # Group scores by heading prefix (chunks may split into continuations)
        arch_scores = [
            float(m["importance_score"])
            for m in all_data["metadatas"]
            if m["chunk_heading"].startswith("Architecture Decision")
        ]
        shopping_scores = [
            float(m["importance_score"])
            for m in all_data["metadatas"]
            if m["chunk_heading"].startswith("Shopping List")
        ]
        assert len(arch_scores) >= 1
        assert len(shopping_scores) >= 1
        # "architecture decision" concepts should score higher than generic filler
        assert max(arch_scores) > max(shopping_scores)

        mem._chroma_client = None

    def test_parent_file_metadata(self, mock_config):
        """All indexed chunks should have parent_file metadata."""
        import tools.memory as mem
        mem._chroma_client = None
        mock_config.set(memory={"db_path": str(mock_config.vault_path / ".test_parent_db")})

        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        (notes_dir / "parent-test.md").write_text("# Simple\n\nJust content.")

        index_file("notes/parent-test.md")

        collection = mem._get_collection()
        all_data = collection.get(include=["metadatas"])
        for meta in all_data["metadatas"]:
            assert meta.get("parent_file") == "notes/parent-test.md"

        mem._chroma_client = None


class TestTierMetadata:
    """Tests for tier field in metadata."""
    
    def test_build_metadata_includes_tier(self, mock_config):
        """Test that _build_metadata includes tier field."""
        import tools.memory as mem
        mem._chroma_client = None
        mock_config.set(memory={"db_path": str(mock_config.vault_path / ".test_tier_metadata_db")})
        
        # Index a file
        test_file = mock_config.vault_path / "notes" / "test-tier.md"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("# Test Tier\nTesting tier metadata")
        
        result = index_file("notes/test-tier.md")
        assert result["success"]
        assert "tier" in result["metadata"]
        assert result["metadata"]["tier"] == "file"
        
        mem._chroma_client = None
