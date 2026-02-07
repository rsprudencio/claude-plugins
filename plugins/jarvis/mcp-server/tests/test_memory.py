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
